"""Read bounded text and tables from a local OOXML document part."""

import json
import stat
import struct
import zlib
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from hashlib import sha256
from io import BytesIO
from typing import Literal
from xml.etree.ElementTree import Element, TreeBuilder
from xml.parsers import expat
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

from kanzlei_ai.connectors.contracts import MAX_SOURCE_BYTES
from kanzlei_ai.documents.parse import TerminalExtractionError

DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PARSER_VERSION = "docx-native-v1"
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 2048
MAX_XML_DEPTH = 128
MAX_XML_ELEMENTS = 100_000
MAX_PARAGRAPH_CHARACTERS = 256 * 1024
_MAX_CENTRAL_DIRECTORY = 2 * 1024 * 1024
_MAX_TABLE_COLUMNS = 128
_MAX_TABLE_CELLS = 4096
_ZIP_END = struct.Struct("<4s4H2IH")
_ZIP_END_SIGNATURE = b"PK\x05\x06"
_DOCUMENT = "word/document.xml"
_WORD_NAMESPACES = (
    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}",
    "{http://purl.oclc.org/ooxml/wordprocessingml/main}",
)
_CONTAINERS = ("sdt", "sdtContent", "customXml", "ins", "moveTo")
_INLINE_CONTAINERS = (*_CONTAINERS, "r", "hyperlink", "smartTag", "dir", "bdo")


@dataclass(frozen=True)
class DocxBlock:
    text: str
    kind: Literal["text", "table"] = "text"
    rows: list[list[str]] | None = None
    page: None = None


@dataclass(frozen=True)
class ParsedDocx:
    content_sha256: str
    blocks: list[DocxBlock]
    parser_version: str = PARSER_VERSION
    page_count: int = 0

    def artifact_bytes(self) -> bytes:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _check_directory_bounds(content: bytes) -> int:
    # ZipFile creates an object per central-directory record before callers can
    # inspect the count. Bound both the declared count and directory bytes first.
    end = content.rfind(_ZIP_END_SIGNATURE, max(0, len(content) - 65535 - _ZIP_END.size))
    if end < 0 or len(content) - end < _ZIP_END.size:
        raise TerminalExtractionError("INVALID_DOCX")
    _, disk, cd_disk, disk_members, members, cd_size, cd_offset, comment = _ZIP_END.unpack_from(
        content, end
    )
    if disk or cd_disk or disk_members != members or end + _ZIP_END.size + comment != len(content):
        raise TerminalExtractionError("INVALID_DOCX")
    if members > MAX_MEMBERS or cd_size > _MAX_CENTRAL_DIRECTORY:
        raise TerminalExtractionError("DOCX_PACKAGE_LIMIT_EXCEEDED")
    # OOXML here uses a single ZIP without a prepend, split volumes, or ZIP64
    # directory metadata. Local ZIP64 fields inside members do not affect this.
    if (
        not content.startswith(b"PK\x03\x04")
        or cd_offset + cd_size != end
        or content[end - 20 : end - 16] == b"PK\x06\x07"
    ):
        raise TerminalExtractionError("INVALID_DOCX")
    return members


def _read_document(content: bytes) -> bytes:
    declared_members = _check_directory_bounds(content)
    with ZipFile(BytesIO(content)) as archive:
        members = archive.infolist()
        if (
            len(members) > MAX_MEMBERS
            or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES
        ):
            raise TerminalExtractionError("DOCX_PACKAGE_LIMIT_EXCEEDED")
        if len(members) != declared_members:
            raise TerminalExtractionError("INVALID_DOCX")
        seen = set()
        for item in members:
            name = item.orig_filename
            key = name.casefold()
            if (
                key in seen
                or any(part in {"", ".", ".."} for part in name.removesuffix("/").split("/"))
                or any(char in "\\:" or ord(char) < 32 or ord(char) == 127 for char in name)
                or item.flag_bits & 0x41
                or stat.S_ISLNK(item.external_attr >> 16)
                or item.compress_type not in (ZIP_STORED, ZIP_DEFLATED)
            ):
                raise TerminalExtractionError("INVALID_DOCX")
            seen.add(key)
        try:
            document = archive.getinfo(_DOCUMENT)
        except KeyError:
            raise TerminalExtractionError("INVALID_DOCX") from None
        if document.file_size > MAX_XML_BYTES:
            raise TerminalExtractionError("DOCX_PACKAGE_LIMIT_EXCEEDED")
        # No relationship, inclusion, image, or embedded object is opened.
        with archive.open(document) as stream:
            xml = stream.read(MAX_XML_BYTES + 1)
        if len(xml) > MAX_XML_BYTES:
            raise TerminalExtractionError("DOCX_PACKAGE_LIMIT_EXCEEDED")
        if len(xml) != document.file_size:
            raise TerminalExtractionError("INVALID_DOCX")
        return xml


def _parse_xml(content: bytes) -> Element:
    parser = expat.ParserCreate(namespace_separator="}")
    tree = TreeBuilder()
    depth = nodes = 0

    def qualified(name):
        return "{" + name if "}" in name else name

    def start(name, attrs):
        nonlocal depth, nodes
        depth += 1
        nodes += 1
        if depth > MAX_XML_DEPTH or nodes > MAX_XML_ELEMENTS:
            raise TerminalExtractionError("DOCX_XML_LIMIT_EXCEEDED")
        tree.start(qualified(name), {qualified(key): value for key, value in attrs.items()})

    def end(name):
        nonlocal depth
        tree.end(qualified(name))
        depth -= 1

    def reject_declaration(*_args):
        raise TerminalExtractionError("INVALID_DOCX")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = tree.data
    parser.StartDoctypeDeclHandler = reject_declaration
    parser.EntityDeclHandler = reject_declaration
    parser.ExternalEntityRefHandler = reject_declaration
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.Parse(content, True)
    return tree.close()


def _children(node: Element, namespace: str, tags: set[str]) -> Iterator[Element]:
    containers = {namespace + tag for tag in _CONTAINERS}
    for child in node:
        if child.tag in tags:
            yield child
        elif child.tag in containers:
            yield from _children(child, namespace, tags)


def _paragraph(node: Element, namespace: str) -> str:
    inline = {namespace + tag for tag in _INLINE_CONTAINERS}
    text_tag = namespace + "t"
    separators = {
        namespace + "tab": "\t",
        namespace + "br": "\n",
        namespace + "cr": "\n",
        namespace + "noBreakHyphen": "\u2011",
    }

    def parts(parent):
        for child in parent:
            if child.tag == text_tag:
                yield child.text or ""
            elif child.tag in separators:
                yield separators[child.tag]
            elif child.tag in inline:
                yield from parts(child)

    text = "".join(parts(node)).strip()
    if len(text) > MAX_PARAGRAPH_CHARACTERS:
        raise TerminalExtractionError("DOCX_TEXT_LIMIT_EXCEEDED")
    return text


def _grid_value(element: Element | None, namespace: str, default: int) -> int:
    if element is None:
        return default
    value = element.get(namespace + "val", "")
    if not value.isascii() or not value.isdecimal() or len(value) > 3:
        raise TerminalExtractionError("INVALID_DOCX")
    columns = int(value)
    if not default <= columns <= _MAX_TABLE_COLUMNS:
        raise TerminalExtractionError("TABLE_LIMIT_EXCEEDED")
    return columns


def _table(table: Element, namespace: str) -> DocxBlock | None:
    rows: list[list[str]] = []
    cells = 0
    for row in _children(table, namespace, {namespace + "tr"}):
        before = _grid_value(row.find(f"{namespace}trPr/{namespace}gridBefore"), namespace, 0)
        after = _grid_value(row.find(f"{namespace}trPr/{namespace}gridAfter"), namespace, 0)
        cells += before + after
        if before + after > _MAX_TABLE_COLUMNS or cells > _MAX_TABLE_CELLS:
            raise TerminalExtractionError("TABLE_LIMIT_EXCEEDED")
        values = [""] * before
        for cell in _children(row, namespace, {namespace + "tc"}):
            span = _grid_value(cell.find(f"{namespace}tcPr/{namespace}gridSpan"), namespace, 1)
            cells += span
            if len(values) + span + after > _MAX_TABLE_COLUMNS or cells > _MAX_TABLE_CELLS:
                raise TerminalExtractionError("TABLE_LIMIT_EXCEEDED")
            paragraphs = []
            for child in _children(cell, namespace, {namespace + "p", namespace + "tbl"}):
                if child.tag == namespace + "tbl":
                    raise TerminalExtractionError("UNSUPPORTED_DOCX_STRUCTURE")
                text = _paragraph(child, namespace)
                if text:
                    paragraphs.append(text)
            values.append("\n".join(paragraphs))
            values.extend([""] * (span - 1))
        values.extend([""] * after)
        if values:
            rows.append(values)
    if not any(cell for row in rows for cell in row):
        return None
    lines = [
        " | ".join(
            cell.replace("\\", "\\\\").replace("|", "\\|").replace("\t", " ").replace("\n", " ")
            for cell in row
        )
        for row in rows
    ]
    return DocxBlock("\n".join(lines), kind="table", rows=rows)


def parse_docx(content: bytes) -> ParsedDocx:
    if len(content) > MAX_SOURCE_BYTES:
        raise TerminalExtractionError("SOURCE_LIMIT_EXCEEDED")
    try:
        root = _parse_xml(_read_document(content))
    except (BadZipFile, EOFError, OSError, RuntimeError, ValueError, expat.ExpatError, zlib.error):
        raise TerminalExtractionError("INVALID_DOCX") from None
    namespace = next((ns for ns in _WORD_NAMESPACES if root.tag == ns + "document"), None)
    if namespace is None:
        raise TerminalExtractionError("INVALID_DOCX")
    bodies = root.findall(namespace + "body")
    if len(bodies) != 1:
        raise TerminalExtractionError("INVALID_DOCX")
    blocks = []
    paragraph_tag = namespace + "p"
    for child in _children(bodies[0], namespace, {paragraph_tag, namespace + "tbl"}):
        if child.tag == paragraph_tag:
            text = _paragraph(child, namespace)
            if text:
                blocks.append(DocxBlock(text))
        else:
            table = _table(child, namespace)
            if table is not None:
                blocks.append(table)
    if not blocks:
        raise TerminalExtractionError("DOCX_NO_TEXT")
    return ParsedDocx(sha256(content).hexdigest(), blocks)
