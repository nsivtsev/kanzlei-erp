"""Accept only structured, supported claims backed by exact SQL evidence."""

import re
from dataclasses import dataclass, field
from typing import Protocol

from kanzlei_ai.chat.context import Evidence
from kanzlei_ai.connectors.base import ConnectorError
from kanzlei_ai.search.index import exact_identifiers

_QUESTION_AMOUNT = re.compile(
    r"\b(?:\w*betrag|\w*summe|rechnungswert|wie\s+(?:hoch|viel))\b", re.IGNORECASE
)
_AMOUNT = re.compile(r"(?:[0-9]{1,3}(?:\.[0-9]{3}){1,4}|[0-9]{1,15}),[0-9]{2} (?:EUR|€)")
_AMOUNT_HEADERS = {"betrag", "gesamtbetrag", "summe"}
_MARKDOWN_DIVIDER = re.compile(r":?-{3,}:?")


@dataclass(frozen=True)
class AnswerPacket:
    evidence_id: str
    quote: str = field(repr=False)


@dataclass(frozen=True)
class VerifiedAnswer:
    amount: str = field(repr=False)
    evidence: Evidence


class ChatProvider(Protocol):
    """An async provider must cooperate with cancellation."""

    async def generate(
        self, question: str, context: tuple[Evidence, ...]
    ) -> AnswerPacket | None: ...


def _amount_from_table(text):
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("|") and line.endswith("|"):
            line = line[1:-1]
        rows.append(tuple(cell.strip() for cell in line.split("|")))
    if (
        len(rows) == 3
        and len(rows[1]) == len(rows[0])
        and all(_MARKDOWN_DIVIDER.fullmatch(cell) for cell in rows[1])
    ):
        del rows[1]
    if len(rows) != 2 or len(rows[0]) != len(rows[1]):
        return None
    columns = [i for i, cell in enumerate(rows[0]) if cell.casefold() in _AMOUNT_HEADERS]
    if len(columns) != 1:
        return None
    value = rows[1][columns[0]]
    return value if _AMOUNT.fullmatch(value) else None


def supported_amount(question: str, context: tuple[Evidence, ...]) -> VerifiedAnswer | None:
    identifiers = exact_identifiers(question)
    if len(identifiers) != 1 or not _QUESTION_AMOUNT.search(question):
        return None
    identifier = next(iter(identifiers))
    # Amount tables can be separate from the invoice heading. Resolve through
    # the same source and generation, never by mixing two documents.
    invoice_heading = re.compile(
        rf"\s*Rechnung(?:snummer)?\s*[:#-]?\s+{re.escape(identifier)}(?=\s|$)", re.IGNORECASE
    )
    invoices = {
        evidence.dependency.version_key()
        for evidence in context
        if evidence.kind != "table"
        and any(invoice_heading.match(line) for line in evidence.text.splitlines())
    }
    if len(invoices) != 1:
        return None
    eligible = [
        VerifiedAnswer(amount, evidence)
        for evidence in context
        if evidence.dependency.version_key() in invoices
        and evidence.kind == "table"
        and (amount := _amount_from_table(evidence.text))
    ]
    if len({answer.amount for answer in eligible}) != 1:
        return None
    return eligible[0]


def validate_answer(question, context, packet) -> VerifiedAnswer | None:
    if packet is None:
        return None
    supported = supported_amount(question, context)
    if (
        type(packet) is not AnswerPacket
        or type(packet.evidence_id) is not str
        or type(packet.quote) is not str
        or supported is None
        or packet.evidence_id != supported.evidence.dependency.chunk_id
        or packet.quote != supported.amount
        or packet.quote not in supported.evidence.text
    ):
        raise ConnectorError("INVALID_EVIDENCE", 503)
    return supported
