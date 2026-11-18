"""Synthetic native-PDF extraction contracts; no ERP, storage, or model traffic."""

import base64
import hashlib
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "documents"


def test_native_pdf_options_disable_ocr_and_keep_table_structure():
    from kanzlei_ai.documents.parse import native_pdf_options

    options = native_pdf_options()
    assert options.do_ocr is False
    assert options.do_table_structure is True
    assert options.force_backend_text is True


def test_parser_preserves_independent_gold_facts_and_page_provenance():
    from kanzlei_ai.documents.parse import Block, extract_blocks

    gold = json.loads((FIXTURES / "gold_facts.json").read_text())
    blocks = extract_blocks(
        [
            Block(page=1, kind="heading", text="Rechnung RE-2026-0042"),
            Block(page=1, kind="text", text="Kunde: Muster GmbH"),
            Block(page=2, kind="heading", text="Leistungsübersicht"),
            Block(
                page=2,
                kind="table",
                text="Leistung | Menge | Betrag\nBeratung | 5 | 1.249,50 EUR",
            ),
            Block(page=2, kind="heading", text="Leistungsübersicht"),
        ],
        page_count=3,
    )
    text_by_page = {}
    for block in blocks:
        text_by_page[block.page] = text_by_page.get(block.page, "") + "\n" + block.text
    for fact in gold["facts"]:
        assert fact["value"] in text_by_page[fact["page"]]
    table = next(block for block in blocks if block.kind == "table")
    assert table.page == gold["table"]["page"]
    assert " | ".join(gold["table"]["headers"]) in table.text
    assert all(block.page != gold["empty_page"] for block in blocks)


def test_parser_does_not_turn_prose_page_into_table_from_pipe_character():
    from docling.datamodel.document import ConversionStatus

    from kanzlei_ai.documents.chunk import ChunkInput, deterministic_chunks
    from kanzlei_ai.documents.parse import parse_pdf

    class Provenance:
        def __init__(self, page_no, charspan):
            self.page_no = page_no
            self.charspan = charspan

    class TextItem:
        def __init__(self, text, page):
            self.text = text
            self.prov = (Provenance(page, (0, len(text))),)

    prose = "Wort " * 440 + "kurzer Hinweis | mit Trennzeichen"
    table = "Leistung | Betrag\nBeratung | 1.249,50 EUR"
    page_two = "Zusätzlicher Kontext\n" + table

    class Document:
        def num_pages(self):
            return 3

        def iterate_items(self):
            yield TextItem(prose, 1), None
            yield TextItem(page_two, 2), None

    class Converter:
        def convert(self, *_args, **_kwargs):
            return type(
                "Result",
                (),
                {"status": ConversionStatus.SUCCESS, "document": Document()},
            )()

    content = base64.b64decode((FIXTURES / "synthetic-german-native.pdf.b64").read_text())
    parsed = parse_pdf(content, converter=Converter())

    assert [(block.kind, block.text) for block in parsed.blocks if block.page == 1] == [
        ("text", prose)
    ]
    assert [(block.kind, block.text) for block in parsed.blocks if block.page == 2] == [
        ("text", "Zusätzlicher Kontext"),
        ("table", table),
    ]
    chunks = deterministic_chunks(
        ChunkInput(
            connector_id="fixture-erp",
            source_id="file:alpha",
            revision="a" * 64,
            generation=1,
            parser_version=parsed.parser_version,
            content_sha256=parsed.content_sha256,
            blocks=[
                {"page": block.page, "kind": block.kind, "text": block.text}
                for block in parsed.blocks
            ],
        )
    )
    prose_chunks = [chunk for chunk in chunks if chunk.page == 1]
    assert [chunk.kind for chunk in prose_chunks] == ["text", "text"]
    assert all(len(chunk.text) <= 1800 for chunk in prose_chunks)
    assert [(chunk.kind, chunk.text) for chunk in chunks if chunk.page == 2] == [
        ("text", "Zusätzlicher Kontext"),
        ("table", table),
    ]


@pytest.mark.integration
def test_docling_native_fixture_preserves_german_table_and_page_provenance():
    from kanzlei_ai.documents.parse import parse_pdf

    gold = json.loads((FIXTURES / "gold_facts.json").read_text())
    parsed = parse_pdf(base64.b64decode((FIXTURES / "synthetic-german-native.pdf.b64").read_text()))
    assert parsed.page_count == 3
    assert "\ufffd" not in "\n".join(block.text for block in parsed.blocks)
    text_by_page = {}
    for block in parsed.blocks:
        text_by_page[block.page] = text_by_page.get(block.page, "") + "\n" + block.text
    for fact in gold["facts"]:
        assert fact["value"] in text_by_page[fact["page"]]
    table = next(block for block in parsed.blocks if block.kind == "table")
    assert table.page == 2
    assert " | ".join(gold["table"]["headers"]) in table.text
    assert " | ".join(gold["table"]["row"]) in table.text
    assert all(block.page != gold["empty_page"] for block in parsed.blocks)


@pytest.mark.parametrize("reason", ["encrypted", "unsupported"])
def test_explicit_terminal_states_do_not_leak_document_details(reason):
    from kanzlei_ai.documents.parse import TerminalExtractionError, validate_pdf_input

    with pytest.raises(TerminalExtractionError) as error:
        validate_pdf_input(
            b"%PDF-synthetic",
            encrypted=reason == "encrypted",
            supported=reason != "unsupported",
        )
    assert error.value.code == ("ENCRYPTED_PDF" if reason == "encrypted" else "UNSUPPORTED_MEDIA")
    assert b"%PDF" not in str(error.value).encode()


@pytest.mark.integration
def test_real_encrypted_fixture_is_terminal_and_emits_no_document_diagnostics(caplog):
    from kanzlei_ai.documents.parse import TerminalExtractionError, validate_pdf_input

    content = base64.b64decode((FIXTURES / "synthetic-encrypted.pdf.b64").read_text())
    with pytest.raises(TerminalExtractionError, match="ENCRYPTED_PDF"):
        validate_pdf_input(content)
    assert not caplog.text


@pytest.mark.integration
def test_textless_native_pdf_requires_ocr_and_never_publishes_empty_extraction():
    from kanzlei_ai.documents.parse import TerminalExtractionError, parse_pdf

    content = base64.b64decode((FIXTURES / "synthetic-textless.pdf.b64").read_text())
    with pytest.raises(TerminalExtractionError, match="OCR_REQUIRED"):
        parse_pdf(content)


def test_s3_artifact_write_is_atomic_and_never_uses_a_shared_temp_key():
    from kanzlei_ai.documents.artifacts import S3ArtifactStore

    class Client:
        def __init__(self):
            self.calls = []

        def put_object(self, **kwargs):
            self.calls.append(kwargs)

    client = Client()
    store = S3ArtifactStore(client, bucket="private-artifacts", prefix="kanzlei-ai", max_bytes=64)
    key = store.put("connector/source/revision/generation", b"synthetic artifact")
    assert key.startswith("kanzlei-ai/")
    assert len(client.calls) == 1
    assert client.calls[0]["Key"] == key
    assert client.calls[0]["IfNoneMatch"] == "*"
    assert (
        client.calls[0]["Metadata"]["sha256"] == hashlib.sha256(b"synthetic artifact").hexdigest()
    )
