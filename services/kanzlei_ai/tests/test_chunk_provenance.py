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
        blocks=[{"page": 1, "kind": "table", "text": "Header | Value\nrow | " + "x" * 100}],
    )
    with pytest.raises(OversizedTable, match="TABLE_LIMIT_EXCEEDED"):
        deterministic_chunks(value, max_characters=90)


@pytest.mark.parametrize("markdown", [False, True])
def test_long_pdf_table_preserves_all_rows_and_headers(markdown):
    from kanzlei_ai.documents.chunk import ChunkInput, deterministic_chunks

    def line(cells):
        unframed = " | ".join(cells)
        return f"| {unframed} |" if markdown else unframed

    header = [line(["Rechnung", "Beschreibung", "Betrag"])]
    if markdown:
        header.append("| :--- | --- | ---: |")
    rows = [line([f"RE-2026-{i:04}", "Prüfung " * 24, "1.234,56 EUR"]) for i in range(125)]
    table = "\n".join(header + rows)
    assert len(table) > 24_000
    value = ChunkInput(
        "erp", "file:fixture", "a", 1, "parser", "b", [{"page": 2, "kind": "table", "text": table}]
    )

    chunks = deterministic_chunks(value)
    assert len(chunks) > 1
    assert chunks == deterministic_chunks(value)
    reconstructed = []
    for ordinal, chunk in enumerate(chunks):
        assert chunk.page == 2 and chunk.kind == "table" and chunk.ordinal == ordinal
        assert len(chunk.text) <= 1800
        lines = chunk.text.split("\n")
        assert lines[: len(header)] == header
        reconstructed.extend(lines[len(header) :])
    assert reconstructed == rows


def test_pdf_table_counts_header_newlines_and_whole_row_towards_bound():
    from kanzlei_ai.documents.chunk import ChunkInput, OversizedTable, deterministic_chunks

    prefix = "| A | B |\n| :--- | ---: |\n| x | "
    table = prefix + "é" * (1800 - len(prefix) - 2) + " |"
    value = ChunkInput(
        "erp", "file:fixture", "a", 1, "parser", "b", [{"page": 1, "kind": "table", "text": table}]
    )
    assert len(table) == 1800
    assert deterministic_chunks(value)[0].text == table
    with pytest.raises(OversizedTable, match="TABLE_LIMIT_EXCEEDED"):
        deterministic_chunks(value, max_characters=len(table) - 1)


@pytest.mark.parametrize(
    "table",
    [
        "| A | B |\n| --- | --- |\n| x | y | z |",
        "A | B\nx | y\n\nz | w",
        "| A | B |\n| --- | --- |\n| x | y |\n\nz | w",
    ],
)
def test_unsupported_pdf_table_is_a_safe_error(table):
    from kanzlei_ai.documents.chunk import ChunkInput, deterministic_chunks
    from kanzlei_ai.documents.parse import TerminalExtractionError

    value = ChunkInput(
        "erp",
        "file:fixture",
        "a",
        1,
        "parser",
        "b",
        [{"page": 1, "kind": "table", "text": table}],
    )
    with pytest.raises(TerminalExtractionError, match="UNSUPPORTED_TABLE_STRUCTURE"):
        deterministic_chunks(value)


@pytest.mark.parametrize(
    "table",
    [
        "| Leistung | Betrag |\n| --- | --- |",
        "| A | B | C |\n| :--- | --- | ---: |\n| x \\| y | | 1,23 EUR |",
        "A|B|C\n:---|---|---:\nx||1,23 EUR",
        "A | B | C\nx |  | 1,23 EUR",
    ],
)
def test_short_pdf_tables_keep_their_exact_text(table):
    from kanzlei_ai.documents.chunk import ChunkInput, deterministic_chunks

    value = ChunkInput(
        "erp",
        "file:fixture",
        "a",
        1,
        "parser",
        "b",
        [{"page": 1, "kind": "table", "text": table}],
    )
    (chunk,) = deterministic_chunks(value)
    assert chunk.text == table and not chunk.table_fragment


def test_docling_table_caption_and_footnote_remain_searchable_on_the_same_page():
    from kanzlei_ai.documents.chunk import ChunkInput, deterministic_chunks

    header = "| Item | Amount |\n|-|-|"
    rows = ["| Long service description | 1,23 EUR |"] * 4
    prefix, suffix = "Caption from Docling", "Footnote from Docling"
    text = f"{prefix}\n\n" + "\n".join([header, *rows]) + f"\n\n{suffix}"
    value = ChunkInput(
        "erp",
        "file:fixture",
        "a",
        1,
        "parser",
        "b",
        [{"page": 3, "kind": "table", "text": text}],
    )

    assert deterministic_chunks(value)[0].text == text
    chunks = deterministic_chunks(value, max_characters=65)
    assert [(chunk.kind, chunk.text) for chunk in chunks] == [
        ("text", prefix),
        *[("table", f"{header}\n{row}") for row in rows],
        ("text", suffix),
    ]
    assert all(chunk.page == 3 for chunk in chunks)
