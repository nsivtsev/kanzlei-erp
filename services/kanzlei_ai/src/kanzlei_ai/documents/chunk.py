"""Deterministic source-version-specific chunks with page provenance."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from kanzlei_ai.documents.parse import TerminalExtractionError
from kanzlei_ai.documents.pdf_table import parse_pdf_table


class OversizedTable(ValueError):
    """A table cannot be split without losing its header/row relationship."""


@dataclass(frozen=True)
class ChunkInput:
    connector_id: str
    source_id: str
    revision: str
    generation: int
    parser_version: str
    content_sha256: str
    blocks: list[dict[str, Any]]


@dataclass(frozen=True)
class Chunk:
    id: str
    connector_id: str
    source_id: str
    revision: str
    generation: int
    parser_version: str
    content_sha256: str
    page: int | None
    kind: str
    ordinal: int
    text: str
    table_fragment: bool = False


def _table_parts(text: str, maximum: int) -> list[tuple[str, str, bool]]:
    table = parse_pdf_table(text)
    if table is None:
        raise TerminalExtractionError("UNSUPPORTED_TABLE_STRUCTURE")
    if len(text) <= maximum:
        return [("table", text, False)]
    if not table.row_lines:
        raise OversizedTable("TABLE_LIMIT_EXCEEDED")
    header_length = len("\n".join(table.header_lines))
    parts = []
    lines = list(table.header_lines)
    length = header_length
    for row in table.row_lines:
        row_length = len(row) + 1
        if header_length + row_length > maximum:
            raise OversizedTable("TABLE_LIMIT_EXCEEDED")
        if length + row_length > maximum:
            parts.append("\n".join(lines))
            lines = list(table.header_lines)
            length = header_length
        lines.append(row)
        length += row_length
    parts.append("\n".join(lines))
    return [
        *(("text", part, False) for part in _text_parts(table.before, maximum)),
        *(("table", part, len(parts) > 1) for part in parts),
        *(("text", part, False) for part in _text_parts(table.after, maximum)),
    ]


def _text_parts(text: str, maximum: int) -> list[str]:
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    result: list[str] = []
    for paragraph in paragraphs:
        while len(paragraph) > maximum:
            split = paragraph.rfind(" ", 0, maximum + 1)
            if split <= 0:
                split = maximum
            result.append(paragraph[:split].strip())
            paragraph = paragraph[split:].strip()
        if paragraph:
            result.append(paragraph)
    return result


def _parts(
    text: str, kind: str, maximum: int, split_pdf_tables: bool
) -> list[tuple[str, str, bool]]:
    if kind == "table":
        if split_pdf_tables:
            return _table_parts(text, maximum)
        if len(text) > maximum:
            raise OversizedTable("TABLE_LIMIT_EXCEEDED")
        return [(kind, text, False)]
    return [(kind, part, False) for part in _text_parts(text, maximum)]


def deterministic_chunks(
    value: ChunkInput, *, max_characters: int = 1800, split_pdf_tables: bool = True
) -> list[Chunk]:
    if not 1 <= max_characters <= 10000:
        raise ValueError("Invalid chunk bound")
    chunks: list[Chunk] = []
    ordinal = 0
    identity = {
        "connector_id": value.connector_id,
        "source_id": value.source_id,
        "revision": value.revision,
        "generation": value.generation,
        "parser_version": value.parser_version,
        "content_sha256": value.content_sha256,
    }
    for block in value.blocks:
        page, kind, text = block["page"], block["kind"], block["text"].strip()
        if not text:
            continue
        for part_kind, part, table_fragment in _parts(text, kind, max_characters, split_pdf_tables):
            seed = json.dumps(
                {**identity, "page": page, "kind": part_kind, "ordinal": ordinal, "text": part},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            chunks.append(
                Chunk(
                    id=hashlib.sha256(seed).hexdigest(),
                    **identity,
                    page=page,
                    kind=part_kind,
                    ordinal=ordinal,
                    text=part,
                    table_fragment=table_fragment,
                )
            )
            ordinal += 1
    return chunks
