"""Deterministic source-version-specific chunks with page provenance."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


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
    page: int
    kind: str
    ordinal: int
    text: str


def _parts(text: str, kind: str, maximum: int) -> list[str]:
    if kind == "table":
        # A table is an atomic claim surface.  Splitting cells into unrelated
        # chunks destroys the row/header association needed for safe answers.
        if len(text) > maximum:
            raise OversizedTable("TABLE_LIMIT_EXCEEDED")
        return [text]
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


def deterministic_chunks(value: ChunkInput, *, max_characters: int = 1800) -> list[Chunk]:
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
        for part in _parts(text, kind, max_characters):
            seed = json.dumps(
                {**identity, "page": page, "kind": kind, "ordinal": ordinal, "text": part},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            chunks.append(
                Chunk(
                    id=hashlib.sha256(seed).hexdigest(),
                    **identity,
                    page=page,
                    kind=kind,
                    ordinal=ordinal,
                    text=part,
                )
            )
            ordinal += 1
    return chunks
