"""Mistral OCR on a selected page of an inline PDF."""

import re
from base64 import b64encode

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
_MAX_OCR_RESPONSE_BYTES = 2 * 1024 * 1024
_PLACEHOLDER = re.compile(r"(!?)\[((?:img|tbl)-\d+\.[a-z]+)\]\(\2\)")
_ATX_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$", re.M)


class MistralOcrError(ExtractionError):
    def __init__(self, code: str, *, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class MistralOcrProvider:
    profile = OCR_MODEL

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

    def recognize_page(self, content: bytes, page: int) -> list[Block]:
        if type(page) is not int or not 1 <= page <= MAX_PAGES:
            raise TerminalExtractionError("OCR_INVALID_PAGE")
        if len(content) > MAX_SOURCE_BYTES:
            raise TerminalExtractionError("SOURCE_LIMIT_EXCEEDED")
        if not content.startswith(b"%PDF-"):
            raise TerminalExtractionError("UNSUPPORTED_MEDIA")
        payload = {
            "model": OCR_MODEL,
            "document": {
                "type": "document_url",
                "document_url": f"data:application/pdf;base64,{b64encode(content).decode('ascii')}",
            },
            "pages": [page - 1],
            "table_format": "markdown",
            "include_image_base64": False,
            "include_blocks": False,
        }
        try:
            with self._http.client(self._transport) as client:
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


def _page_blocks(result: dict, page: int) -> list[Block]:
    pages = result.get("pages")
    if not isinstance(pages, list) or len(pages) != 1 or not isinstance(pages[0], dict):
        raise MistralOcrError("OCR_INVALID_RESPONSE")
    requested = pages[0]
    index, markdown = requested.get("index"), requested.get("markdown")
    if type(index) is not int or index != page - 1 or not isinstance(markdown, str):
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
