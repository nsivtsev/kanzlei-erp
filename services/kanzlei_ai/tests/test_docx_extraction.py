import json
import struct
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from kanzlei_ai.documents.parse import TerminalExtractionError

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _xml(body):
    return f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'.encode()


def _package(document, *members):
    buf = BytesIO()
    with ZipFile(buf, "w", compression=ZIP_DEFLATED) as archive:
        if document is not None:
            archive.writestr("word/document.xml", document)
        for name, data in members:
            archive.writestr(name, data)
    return buf.getvalue()


def test_paragraphs_and_table_cells_keep_original_order_and_unicode():
    from kanzlei_ai.documents.docx import parse_docx

    package = _package(
        _xml(
            "<w:p><w:r><w:t>Müller &amp; Söhne</w:t></w:r></w:p>"
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Leistung</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>Betrag</w:t></w:r></w:p></w:tc></w:tr>"
            "<w:tr><w:tc><w:p><w:r><w:t>Проверка | примечание</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>1.249,50 EUR</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            "<w:sdt><w:sdtContent><w:p><w:r><w:t>AZ-2026/17</w:t></w:r></w:p>"
            "</w:sdtContent></w:sdt>"
        )
    )
    parsed = parse_docx(package)
    assert parsed.page_count == 0
    assert [(block.page, block.kind, block.text) for block in parsed.blocks] == [
        (None, "text", "Müller & Söhne"),
        (None, "table", "Leistung | Betrag\nПроверка \\| примечание | 1.249,50 EUR"),
        (None, "text", "AZ-2026/17"),
    ]
    assert json.loads(parsed.artifact_bytes())["blocks"][1]["rows"] == [
        ["Leistung", "Betrag"],
        ["Проверка | примечание", "1.249,50 EUR"],
    ]


def test_extracts_local_link_labels_and_ignores_objects_inclusions_and_relationship_targets():
    from kanzlei_ai.documents.docx import parse_docx

    package = _package(
        _xml(
            '<w:p><w:hyperlink w:anchor="external"><w:r><w:t>Visible label</w:t></w:r>'
            "</w:hyperlink><w:r><w:object><w:t>embedded text</w:t></w:object>"
            "<w:instrText>INCLUDETEXT external</w:instrText></w:r></w:p>"
            '<w:altChunk w:id="external"/>'
            "<w:del><w:p><w:r><w:t>deleted text</w:t></w:r></w:p></w:del>"
        ),
        ("word/_rels/document.xml.rels", b"https://example.invalid/secret"),
        ("word/embeddings/embedded.bin", b"private embedded payload"),
    )
    parsed = parse_docx(package)
    assert [block.text for block in parsed.blocks] == ["Visible label"]
    assert b"external" not in parsed.artifact_bytes()


def test_table_retains_grid_offsets_spans_and_multiple_paragraphs():
    from kanzlei_ai.documents.docx import parse_docx

    parsed = parse_docx(
        _package(
            _xml(
                "<w:tbl><w:tr><w:trPr><w:gridBefore w:val='1'/><w:gridAfter w:val='1'/></w:trPr>"
                "<w:tc><w:tcPr><w:gridSpan w:val='2'/></w:tcPr>"
                "<w:p><w:r><w:t>AZ</w:t><w:noBreakHyphen/><w:t>17</w:t></w:r></w:p>"
                "<w:p><w:r><w:t>Geprüft</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            )
        )
    )
    assert parsed.blocks[0].rows == [["", "AZ‑17\nGeprüft", "", ""]]


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"not a zip", "INVALID_DOCX"),
        (_package(None, ("word/other.xml", b"unrelated")), "INVALID_DOCX"),
        (_package(b"<document/>"), "INVALID_DOCX"),
        (_package(_xml("<w:p>")), "INVALID_DOCX"),
        (
            _package(b'<!DOCTYPE doc SYSTEM "https://example.invalid/ext">' + _xml("")),
            "INVALID_DOCX",
        ),
        (
            _package(b'<!DOCTYPE doc [<!ENTITY x "private">]>' + _xml("<w:p>&x;</w:p>")),
            "INVALID_DOCX",
        ),
        (_package(_xml(""), ("../private", b"x")), "INVALID_DOCX"),
        (_package(_xml(""), ("C:/private", b"x")), "INVALID_DOCX"),
        (_package(_xml(""), ("word\\private", b"x")), "INVALID_DOCX"),
        (_package(_xml(""), ("Word/Document.xml", b"duplicate")), "INVALID_DOCX"),
        (_package(_xml("<w:p/>")), "DOCX_NO_TEXT"),
    ],
)
def test_rejects_malformed_or_unsafe_docx(content, code):
    from kanzlei_ai.documents.docx import parse_docx

    with pytest.raises(TerminalExtractionError, match=f"^{code}$"):
        parse_docx(content)


def test_rejects_encrypted_member():
    from kanzlei_ai.documents.docx import parse_docx

    content = bytearray(_package(_xml("")))
    central = content.index(b"PK\x01\x02")
    struct.pack_into("<H", content, central + 8, 1)
    with pytest.raises(TerminalExtractionError, match="^INVALID_DOCX$"):
        parse_docx(bytes(content))


@pytest.mark.parametrize(
    ("limit", "maximum", "code"),
    [
        ("MAX_SOURCE_BYTES", 10, "SOURCE_LIMIT_EXCEEDED"),
        ("MAX_MEMBERS", 1, "DOCX_PACKAGE_LIMIT_EXCEEDED"),
        ("MAX_EXPANDED_BYTES", 200, "DOCX_PACKAGE_LIMIT_EXCEEDED"),
        ("MAX_XML_BYTES", 20, "DOCX_PACKAGE_LIMIT_EXCEEDED"),
        ("MAX_XML_DEPTH", 3, "DOCX_XML_LIMIT_EXCEEDED"),
        ("MAX_XML_ELEMENTS", 2, "DOCX_XML_LIMIT_EXCEEDED"),
    ],
)
def test_package_and_xml_limits(limit, maximum, code, monkeypatch):
    from kanzlei_ai.documents import docx

    package = _package(_xml("<w:p><w:r><w:t>Current text</w:t></w:r></w:p>"), ("a", b"x" * 100))
    monkeypatch.setattr(docx, limit, maximum)
    with pytest.raises(TerminalExtractionError, match=f"^{code}$"):
        docx.parse_docx(package)
