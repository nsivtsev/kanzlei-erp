import hashlib

import pytest


def test_chunks_are_deterministic_and_keep_source_version_identity():
    from kanzlei_ai.documents.chunk import ChunkInput, deterministic_chunks

    input = ChunkInput(
        connector_id="fixture-erp",
        source_id="file:alpha",
        revision="a" * 64,
        generation=7,
        parser_version="docling-native-v1",
        content_sha256=hashlib.sha256(b"source bytes").hexdigest(),
        blocks=[
            {"page": 2, "kind": "table", "text": "Leistung | Betrag\nBeratung | 1.249,50 EUR"},
            {"page": 2, "kind": "text", "text": "Zahlbar binnen 14 Tagen."},
        ],
    )
    first = deterministic_chunks(input, max_characters=90)
    second = deterministic_chunks(input, max_characters=90)
    assert first == second
    assert [chunk.page for chunk in first] == [2, 2]
    assert first[0].kind == "table"
    assert first[0].connector_id == "fixture-erp"
    assert first[0].source_id == "file:alpha"
    assert first[0].revision == "a" * 64
    assert first[0].generation == 7
    assert first[0].parser_version == "docling-native-v1"
    assert first[0].content_sha256 == input.content_sha256


def test_equal_bytes_never_merge_different_source_identities():
    from kanzlei_ai.documents.chunk import ChunkInput, deterministic_chunks

    common = dict(
        connector_id="fixture-erp",
        revision="a" * 64,
        generation=1,
        parser_version="docling-native-v1",
        content_sha256="b" * 64,
        blocks=[{"page": 1, "kind": "text", "text": "Gleicher Inhalt"}],
    )
    a = deterministic_chunks(ChunkInput(source_id="file:a", **common))[0]
    b = deterministic_chunks(ChunkInput(source_id="file:b", **common))[0]
    assert a.id != b.id
    assert a.source_id != b.source_id


def test_oversized_table_is_rejected_without_splitting_header_from_rows():
    from kanzlei_ai.documents.chunk import ChunkInput, OversizedTable, deterministic_chunks

    value = ChunkInput(
        connector_id="fixture-erp",
        source_id="file:alpha",
        revision="a" * 64,
        generation=1,
        parser_version="docling-native-v1",
        content_sha256="b" * 64,
        blocks=[{"page": 1, "kind": "table", "text": "Header | Value\\nrow | " + "x" * 100}],
    )
    with pytest.raises(OversizedTable, match="TABLE_LIMIT_EXCEEDED"):
        deterministic_chunks(value, max_characters=90)
