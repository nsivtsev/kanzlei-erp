"""Read the two table formats emitted by PDF extraction."""

import re
from dataclasses import dataclass, replace

_MARKDOWN_DIVIDER = re.compile(r":?-+:?")
_MARKDOWN_PIPE = re.compile(r"(?<!\\)((?:\\\\)*)\|")
_MARKDOWN_END_PIPE = re.compile(r"(?<!\\)(?:\\\\)*\|$")
_BLANK_LINES = re.compile(r"\n(?:[ \t\r]*\n)+")


@dataclass(frozen=True)
class PdfTable:
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    header_lines: tuple[str, ...]
    row_lines: tuple[str, ...]
    before: str = ""
    after: str = ""

    def compact_markdown(self) -> "PdfTable":
        if len(self.header_lines) != 2:
            return self
        heading, separator = self.header_lines
        heading = _compact_markdown_row(heading, self.columns)
        divider = tuple(
            re.sub(r"-{4,}", "---", cell) for cell in _markdown_cells(separator.strip())
        )
        if heading.lstrip().startswith("|"):
            separator = _compact_markdown_row(separator, divider)
        else:
            # An unframed divider with spaced pipes can be mistaken for native data.
            separator = min(separator, "|".join(divider), key=len)
        return replace(
            self,
            header_lines=(heading, separator),
            row_lines=tuple(
                _compact_markdown_row(line, cells)
                for line, cells in zip(self.row_lines, self.rows, strict=True)
            ),
        )


def _compact_markdown_row(line: str, cells: tuple[str, ...]) -> str:
    # A space before each delimiter keeps a trailing backslash inside its cell.
    return min(line, "| " + " | ".join(cells) + " |", key=len)


def _markdown_cells(line: str) -> tuple[str, ...]:
    line = line.removeprefix("|")
    # An even number of backslashes leaves the pipe unescaped.
    parts = _MARKDOWN_PIPE.split(line)
    cells = tuple(
        (cell + suffix).strip() for cell, suffix in zip(parts[::2], [*parts[1::2], ""], strict=True)
    )
    return cells[:-1] if len(parts) > 1 and not parts[-1] else cells


def _plain_cells(line: str) -> tuple[str, ...]:
    return tuple(cell.strip() for cell in f" {line} ".split(" | "))


def _parse_table(text: str) -> PdfTable | None:
    lines = text.split("\n")
    if len(lines) < 2 or any(not line.strip() for line in lines):
        return None
    stripped = [line.strip() for line in lines]
    framed = stripped[0].startswith("|") and stripped[0].endswith("|")
    divider = _markdown_cells(stripped[1])
    # Compact/unframed Markdown can have no edge pipes. A spaced, unframed
    # row containing only hyphens can also be native data, so keep it as data.
    unframed_markdown = " | " not in stripped[1] or ":" in stripped[1]
    has_divider = (framed or (len(divider) > 1 and unframed_markdown)) and all(
        _MARKDOWN_DIVIDER.fullmatch(cell) for cell in divider
    )
    if (
        framed
        and not has_divider
        and any(not (line.startswith("|") and line.endswith("|")) for line in stripped)
    ):
        return None
    cells = _markdown_cells if framed or has_divider else _plain_cells
    columns, *rows = (cells(line) for line in stripped)
    if cells is _plain_cells and len(columns) < 2:
        return None
    if not any(columns) and (
        not has_divider
        or any(
            not line.startswith("|") or not _MARKDOWN_END_PIPE.search(line, 1) for line in stripped
        )
    ):
        return None
    if any(len(row) != len(columns) for row in rows):
        return None
    prefix_size = 2 if has_divider else 1
    return PdfTable(
        columns,
        tuple(rows[prefix_size - 1 :]),
        tuple(lines[:prefix_size]),
        tuple(lines[prefix_size:]),
    )


def parse_pdf_table(text: str) -> PdfTable | None:
    paragraphs = _BLANK_LINES.split(text)
    if len(paragraphs) == 1:
        return _parse_table(text)

    # Docling includes associated captions and footnotes as separate paragraphs.
    tables = [
        (index, table)
        for index, paragraph in enumerate(paragraphs)
        if (table := _parse_table(paragraph)) is not None
    ]
    if len(tables) != 1:
        return None
    index, table = tables[0]
    heading = table.header_lines[0].strip()
    if not (heading.startswith("|") and heading.endswith("|")):
        return None
    before, after = paragraphs[:index], paragraphs[index + 1 :]
    if any(
        line.lstrip().startswith("|")
        or (len(table.columns) > 1 and len(_plain_cells(line.strip())) == len(table.columns))
        for paragraph in before + after
        for line in paragraph.splitlines()
    ):
        return None
    return replace(table, before="\n\n".join(before), after="\n\n".join(after))
