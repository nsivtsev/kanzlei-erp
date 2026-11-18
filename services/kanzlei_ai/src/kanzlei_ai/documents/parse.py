"""Docling native-text PDF parsing with explicit limits and safe failures."""

from __future__ import annotations

import hashlib
import logging
import signal
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from io import BytesIO

PARSER_VERSION = "docling-native-v1"
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_PAGES = 200
MAX_PARSE_SECONDS = 50


class ExtractionError(Exception):
    code = "EXTRACTION_UNAVAILABLE"
    retryable = True

    def __str__(self):
        return self.code


class TerminalExtractionError(ExtractionError):
    retryable = False

    def __init__(self, code: str):
        self.code = code


class RetryableExtractionError(ExtractionError):
    pass


class _ParseDeadline(Exception):
    pass


@dataclass(frozen=True)
class NativePdfOptions:
    do_ocr: bool = False
    do_table_structure: bool = True
    force_backend_text: bool = True


@dataclass(frozen=True)
class Block:
    page: int
    kind: str
    text: str


@dataclass(frozen=True)
class ParsedPdf:
    parser_version: str
    content_sha256: str
    page_count: int
    blocks: list[Block]

    def artifact_bytes(self) -> bytes:
        import json

        return json.dumps(
            {
                "parser_version": self.parser_version,
                "content_sha256": self.content_sha256,
                "page_count": self.page_count,
                "blocks": [asdict(block) for block in self.blocks],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()


def native_pdf_options() -> NativePdfOptions:
    return NativePdfOptions()


def validate_pdf_input(content: bytes, *, encrypted: bool = False, supported: bool = True) -> int:
    if not supported or not content.startswith(b"%PDF-"):
        raise TerminalExtractionError("UNSUPPORTED_MEDIA")
    # The encrypted trailer is visible without opening content and avoids
    # passing password-protected input into a converter that cannot use it.
    if encrypted or b"/Encrypt" in content:
        raise TerminalExtractionError("ENCRYPTED_PDF")
    if len(content) > MAX_SOURCE_BYTES:
        raise TerminalExtractionError("SOURCE_LIMIT_EXCEEDED")
    try:
        from pypdf import PdfReader

        logger = logging.getLogger("pypdf")
        previous_level = logger.level
        logger.setLevel(logging.CRITICAL)
        try:
            reader = PdfReader(BytesIO(content), strict=False)
            if reader.is_encrypted:
                raise TerminalExtractionError("ENCRYPTED_PDF")
            page_count = len(reader.pages)
        finally:
            logger.setLevel(previous_level)
    except TerminalExtractionError:
        raise
    except Exception:
        raise TerminalExtractionError("UNSUPPORTED_MEDIA") from None
    if page_count > MAX_PAGES:
        raise TerminalExtractionError("PAGE_LIMIT_EXCEEDED")
    return page_count


def extract_blocks(blocks: Iterable[Block], *, page_count: int) -> list[Block]:
    if page_count > MAX_PAGES:
        raise TerminalExtractionError("PAGE_LIMIT_EXCEEDED")
    return [block for block in blocks if 1 <= block.page <= page_count and block.text.strip()]


def _table_column_count(text: str) -> int | None:
    """Return an explicit pipe-row column count, if this span has one."""
    cells = [cell.strip() for cell in text.split(" | ")]
    if len(cells) < 2 or any(not cell for cell in cells):
        return None
    return len(cells)


def _text_blocks(page: int, parts: list[str]) -> list[Block]:
    """Keep provenanced text as prose unless adjacent rows establish a table."""
    blocks: list[Block] = []
    prose: list[str] = []
    lines = [line.strip() for part in parts for line in part.splitlines()]

    def flush_prose() -> None:
        nonlocal prose
        text = "\n".join(prose).strip()
        if text:
            blocks.append(Block(page=page, kind="text", text=text))
        prose = []

    index = 0
    while index < len(lines):
        columns = _table_column_count(lines[index])
        if columns is None:
            prose.append(lines[index])
            index += 1
            continue
        end = index + 1
        while end < len(lines) and _table_column_count(lines[end]) == columns:
            end += 1
        if end - index < 2:
            prose.append(lines[index])
            index = end
            continue
        flush_prose()
        blocks.append(Block(page=page, kind="table", text="\n".join(lines[index:end])))
        index = end
    flush_prose()
    return blocks


def _converter():
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as error:  # API images deliberately omit heavy document dependencies.
        raise RetryableExtractionError() from error
    native = native_pdf_options()
    options = PdfPipelineOptions()
    options.do_ocr = native.do_ocr
    options.do_table_structure = native.do_table_structure
    options.force_backend_text = native.force_backend_text
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )


def parse_pdf(
    content: bytes, *, converter=None, deadline_seconds: int = MAX_PARSE_SECONDS
) -> ParsedPdf:
    if not 0 < deadline_seconds <= MAX_PARSE_SECONDS:
        raise ValueError("Invalid parse deadline")
    started = time.monotonic()
    previous_handler = None
    try:
        from docling.datamodel.base_models import DocumentStream
        from docling.datamodel.document import ConversionStatus

        # The worker executes this in its main process.  ITIMER_REAL interrupts
        # Docling before it can exceed the lease/soft-time budget; unlike a
        # post-conversion elapsed check, it bounds CPU spent on hostile input.
        if signal.getsignal(signal.SIGALRM) is not None:

            def alarm(*_):
                raise _ParseDeadline

            previous_handler = signal.signal(signal.SIGALRM, alarm)
            signal.setitimer(signal.ITIMER_REAL, deadline_seconds)
        expected_page_count = validate_pdf_input(content)
        result = (converter or _converter()).convert(
            DocumentStream(name="source.pdf", stream=BytesIO(content)),
            max_num_pages=MAX_PAGES,
            max_file_size=MAX_SOURCE_BYTES,
        )
        if result.status is not ConversionStatus.SUCCESS:
            raise TerminalExtractionError("PARSE_INCOMPLETE")
        document = result.document
        page_count = int(document.num_pages())
        if page_count != expected_page_count:
            raise TerminalExtractionError("PARSE_INCOMPLETE")
        blocks: list[Block] = []
        page_text: dict[int, list[str]] = {}
        for item, _level in document.iterate_items():
            provenance = getattr(item, "prov", ())
            if not provenance:
                continue
            if type(item).__name__ == "TableItem":
                # The Docling table exporter retains the header/row shape as
                # Markdown instead of turning cells into unrelated sentences.
                text = item.export_to_markdown(document).strip()
                if text:
                    blocks.append(Block(page=provenance[0].page_no, kind="table", text=text))
                continue
            text = getattr(item, "text", "")
            for source_span in provenance:
                start, end = source_span.charspan
                part = text[start:end].strip()
                if part:
                    page_text.setdefault(source_span.page_no, []).append(part)
        for page, parts in page_text.items():
            # TableItem carries the preferred structure. Some Docling backends
            # instead emit adjacent pipe-delimited text rows within one or more
            # provenance spans; require matching neighbours so one prose
            # separator cannot change a whole page.
            blocks.extend(_text_blocks(page, parts))
        if time.monotonic() - started > deadline_seconds:
            raise TerminalExtractionError("PARSE_TIMEOUT")
        extracted = extract_blocks(blocks, page_count=page_count)
        # OCR is deliberately disabled for this milestone.  A visual-only PDF
        # is not a successful native-text extraction and must never publish an
        # empty, apparently usable result.
        if not extracted:
            raise TerminalExtractionError("OCR_REQUIRED")
        return ParsedPdf(
            parser_version=PARSER_VERSION,
            content_sha256=hashlib.sha256(content).hexdigest(),
            page_count=page_count,
            blocks=extracted,
        )
    except _ParseDeadline:
        raise TerminalExtractionError("PARSE_TIMEOUT") from None
    except TerminalExtractionError:
        raise
    except Exception as error:
        name = type(error).__name__.lower()
        if "encrypt" in name or "password" in name:
            raise TerminalExtractionError("ENCRYPTED_PDF") from None
        raise RetryableExtractionError() from None
    finally:
        if previous_handler is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
