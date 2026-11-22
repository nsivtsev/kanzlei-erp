"""Mistral OCR on a rendered page sent as an inline PNG."""

import math
import re
from base64 import b64encode
from collections.abc import Callable
from contextlib import closing
from io import BytesIO

import httpx

from kanzlei_ai.documents.parse import (
    MAX_PAGES,
    MAX_SOURCE_BYTES,
    Block,
    ExtractionError,
    TerminalExtractionError,
)
from kanzlei_ai.providers.mistral import MistralError, MistralRequestGate, _MistralEndpoint

OCR_MODEL = "mistral-ocr-4-1"
_RENDER_SCALE = 200 / 72  # DPI
_MAX_RENDER_EDGE = 5000
_MAX_RENDER_PIXELS = 16_000_000
_MAX_OCR_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_OCR_RESPONSE_BYTES = 2 * 1024 * 1024
_PLACEHOLDER = re.compile(r"(!?)\[((?:img|tbl)-\d+\.[a-z]+)\]\(\2\)")
_ATX_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$", re.M)


class MistralOcrError(ExtractionError):
    def __init__(self, code: str, *, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class MistralOcrProvider:
    profile = f"{OCR_MODEL}-png200-v1"

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        timeout_seconds: float = 30,
        transport: httpx.BaseTransport | None = None,
        gate: MistralRequestGate | None = None,
    ):
        self._http = _MistralEndpoint(endpoint, api_key, timeout_seconds, gate)
        self._transport = transport

    def recognize_page(
        self, content: bytes, page: int, *, before_request: Callable[[], None] | None = None
    ) -> list[Block]:
        if type(page) is not int or not 1 <= page <= MAX_PAGES:
            raise TerminalExtractionError("OCR_INVALID_PAGE")
        if len(content) > MAX_SOURCE_BYTES:
            raise TerminalExtractionError("SOURCE_LIMIT_EXCEEDED")
        if not content.startswith(b"%PDF-"):
            raise TerminalExtractionError("UNSUPPORTED_MEDIA")
        image = _render_page(content, page)
        payload = {
            "model": OCR_MODEL,
            "document": {
                "type": "image_url",
                "image_url": f"data:image/png;base64,{b64encode(image).decode('ascii')}",
            },
            "table_format": "markdown",
            "include_image_base64": False,
            "include_blocks": False,
        }
        try:
            with self._http.client(self._transport) as client:
                if before_request is not None:
                    before_request()
                result = self._http.post(client, "ocr", payload, limit=_MAX_OCR_RESPONSE_BYTES)
        except MistralError as error:
            if error.status == 429:
                raise MistralOcrError("OCR_RATE_LIMITED", retry_after=error.retry_after) from None
            if error.status == 413:
                raise TerminalExtractionError("SOURCE_LIMIT_EXCEEDED") from None
            if error.status in {400, 415, 422}:
                raise TerminalExtractionError("OCR_REJECTED") from None
            code = {
                "MISTRAL_TIMEOUT": "OCR_TIMEOUT",
                "MISTRAL_INVALID_RESPONSE": "OCR_INVALID_RESPONSE",
                "MISTRAL_RESPONSE_LIMIT_EXCEEDED": "OCR_RESPONSE_LIMIT_EXCEEDED",
            }.get(error.code, "OCR_UNAVAILABLE")
            raise MistralOcrError(code) from None
        return _page_blocks(result, page)


def _render_page(content: bytes, page: int) -> bytes:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise MistralOcrError("OCR_UNAVAILABLE") from None

    try:
        with pdfium.PdfDocument(content) as source:
            if pdfium.raw.FPDF_GetSecurityHandlerRevision(source) >= 0:
                raise TerminalExtractionError("ENCRYPTED_PDF")
            page_count = len(source)
            if page_count > MAX_PAGES:
                raise TerminalExtractionError("PAGE_LIMIT_EXCEEDED")
            if page > page_count:
                raise TerminalExtractionError("OCR_INVALID_PAGE")
            with closing(source[page - 1]) as selected:
                dimensions = selected.get_size()
                if not all(math.isfinite(value) and value > 0 for value in dimensions):
                    raise TerminalExtractionError("UNSUPPORTED_MEDIA")
                width, height = (math.ceil(value * _RENDER_SCALE) for value in dimensions)
                if max(width, height) > _MAX_RENDER_EDGE or width * height > _MAX_RENDER_PIXELS:
                    raise TerminalExtractionError("OCR_IMAGE_LIMIT_EXCEEDED")
                # Only the visible pixels are re-encoded. A copied PDF page can
                # retain resources used exclusively by other pages.
                bitmap = selected.render(
                    scale=_RENDER_SCALE,
                    draw_annots=False,
                    may_draw_forms=False,
                    limit_image_cache=True,
                    rev_byteorder=True,
                )
                with closing(bitmap), bitmap.to_pil() as image, BytesIO() as output:
                    image.save(output, format="PNG")
                    if output.tell() > _MAX_OCR_IMAGE_BYTES:
                        raise TerminalExtractionError("OCR_IMAGE_LIMIT_EXCEEDED")
                    return output.getvalue()
    except pdfium.PdfiumError as error:
        if error.err_code in {pdfium.raw.FPDF_ERR_PASSWORD, pdfium.raw.FPDF_ERR_SECURITY}:
            raise TerminalExtractionError("ENCRYPTED_PDF") from None
        raise TerminalExtractionError("UNSUPPORTED_MEDIA") from None
    except (ValueError, OSError):
        raise TerminalExtractionError("UNSUPPORTED_MEDIA") from None


def _page_blocks(result: dict, page: int) -> list[Block]:
    pages = result.get("pages")
    if not isinstance(pages, list) or len(pages) != 1 or not isinstance(pages[0], dict):
        raise MistralOcrError("OCR_INVALID_RESPONSE")
    requested = pages[0]
    index, markdown = requested.get("index"), requested.get("markdown")
    if type(index) is not int or index != 0 or not isinstance(markdown, str):
        raise MistralOcrError("OCR_INVALID_RESPONSE")

    raw_tables = requested.get("tables")
    if raw_tables is not None and not isinstance(raw_tables, list):
        raise MistralOcrError("OCR_INVALID_RESPONSE")
    tables = {}
    for table in raw_tables or ():
        if not isinstance(table, dict):
            raise MistralOcrError("OCR_INVALID_RESPONSE")
        table_id, text = table.get("id"), table.get("content")
        if (
            not isinstance(table_id, str)
            or not table_id
            or table_id in tables
            or not isinstance(text, str)
            or not text.strip()
            or table.get("format", "markdown") != "markdown"
        ):
            raise MistralOcrError("OCR_INVALID_RESPONSE")
        tables[table_id] = text.strip()

    blocks = []
    start = 0
    for marker in _PLACEHOLDER.finditer(markdown):
        image, table_id = marker.groups()
        if image:
            continue
        if table_id not in tables:
            raise MistralOcrError("OCR_INVALID_RESPONSE")
        _append_text(blocks, page, markdown[start : marker.start()])
        blocks.append(Block(page, "table", tables.pop(table_id)))
        start = marker.end()
    _append_text(blocks, page, markdown[start:])
    blocks.extend(Block(page, "table", text) for text in tables.values())
    return blocks


def _append_text(blocks: list[Block], page: int, markdown: str) -> None:
    text = _PLACEHOLDER.sub(lambda marker: "" if marker[1] else marker[0], markdown)
    # The existing evidence verifier matches invoice headings as plain lines.
    text = _ATX_HEADING.sub(r"\1", text).strip()
    if text:
        blocks.append(Block(page, "text", text))
