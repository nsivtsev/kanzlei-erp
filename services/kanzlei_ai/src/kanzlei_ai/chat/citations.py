"""Accept only structured, supported claims backed by exact SQL evidence."""

import re
from dataclasses import dataclass, field
from typing import Protocol

from kanzlei_ai.chat.context import Evidence
from kanzlei_ai.chat.language import abstention, email_answer, invoice_answer
from kanzlei_ai.connectors.base import ConnectorError
from kanzlei_ai.search.index import exact_identifiers

_QUESTION_AMOUNT = re.compile(
    r"\b(?:\w*betrag|\w*summe|rechnungswert|wie\s+(?:hoch|viel)|"
    r"total|amount|how\s+much|сумм[ауы]|итого?|сколько)\b",
    re.IGNORECASE,
)
_INVOICE_LABEL = (
    r"(?:Rechnung(?:snummer)?|Invoice(?:\s+(?:Number|No\.?))?|"
    r"Сч[её]т(?:-фактура)?)"
)
_AMOUNT_NUMBER = (
    r"(?:[0-9]{1,15}[,.]|[0-9]{1,3}(?:\.[0-9]{3}){1,4},|"
    r"[0-9]{1,3}(?:,[0-9]{3}){1,4}\.|"
    r"[0-9]{1,3}(?P<separator>[ \u00a0\u202f])[0-9]{3}"
    r"(?:(?P=separator)[0-9]{3}){0,3}[,.])[0-9]{2}"
)
_AMOUNT = re.compile(_AMOUNT_NUMBER)
_PREFIX_CURRENCY = re.compile(r"(?:EUR[ \u00a0\u202f]+|€[ \u00a0\u202f]*)")
_SUFFIX_CURRENCY = re.compile(r"[ \u00a0\u202f]*(?:EUR|€)$")
_AMOUNT_HEADERS = {
    "betrag",
    "gesamtbetrag",
    "summe",
    "amount",
    "total",
    "total amount",
    "invoice total",
    "сумма",
    "сумма счёта",
    "сумма счета",
    "итого",
    "всего",
}
_MARKDOWN_DIVIDER = re.compile(r":?-{3,}:?")


@dataclass(frozen=True)
class AnswerPacket:
    evidence_id: str
    quote: str = field(repr=False)


@dataclass(frozen=True)
class VerifiedInvoice:
    amount: str = field(repr=False)
    evidence: Evidence


@dataclass(frozen=True)
class VerifiedExcerpt:
    excerpt: str = field(repr=False)
    evidence: Evidence


VerifiedAnswer = VerifiedInvoice | VerifiedExcerpt


def render_answer(question: str, answer: VerifiedAnswer | None) -> str:
    if answer is None:
        return abstention(question)
    if isinstance(answer, VerifiedInvoice):
        return invoice_answer(question, answer.amount)
    return email_answer(question, answer.excerpt)


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
    prefix = _PREFIX_CURRENCY.match(value)
    suffix = _SUFFIX_CURRENCY.search(value)
    if bool(prefix) == bool(suffix):
        return None
    number = value[prefix.end() :] if prefix else value[: suffix.start()]
    return value if _AMOUNT.fullmatch(number) else None


def is_invoice_amount_question(question: str) -> bool:
    return bool(_QUESTION_AMOUNT.search(question) and exact_identifiers(question))


def supported_amount(question: str, context: tuple[Evidence, ...]) -> VerifiedInvoice | None:
    identifiers = exact_identifiers(question)
    if len(identifiers) != 1 or not _QUESTION_AMOUNT.search(question):
        return None
    identifier = next(iter(identifiers))
    # Amount tables can be separate from the invoice heading. Resolve through
    # the same source and generation, never by mixing two documents.
    invoice_heading = re.compile(
        rf"\s*{_INVOICE_LABEL}\s*(?:[:#№-]\s*|\s+){re.escape(identifier)}(?=\s|$)",
        re.IGNORECASE,
    )
    invoices = {
        evidence.dependency.version_key()
        for evidence in context
        if evidence.source_kind == "pdf"
        and evidence.kind != "table"
        and any(invoice_heading.match(line) for line in evidence.text.splitlines())
    }
    if len(invoices) != 1:
        return None
    eligible = [
        VerifiedInvoice(amount, evidence)
        for evidence in context
        if evidence.source_kind == "pdf"
        and evidence.dependency.version_key() in invoices
        and evidence.kind == "table"
        and (amount := _amount_from_table(evidence.text))
    ]
    if len({answer.amount for answer in eligible}) != 1:
        return None
    return eligible[0]


def validate_answer(question, context, packet) -> VerifiedAnswer | None:
    if packet is None:
        return None
    if (
        type(packet) is not AnswerPacket
        or type(packet.evidence_id) is not str
        or type(packet.quote) is not str
        or not packet.quote.strip()
    ):
        raise ConnectorError("INVALID_EVIDENCE", 503)
    if is_invoice_amount_question(question):
        supported = supported_amount(question, context)
        if (
            supported is not None
            and packet.evidence_id == supported.evidence.dependency.chunk_id
            and packet.quote == supported.amount
            and packet.quote in supported.evidence.text
        ):
            return supported
    else:
        for evidence in context:
            if (
                evidence.source_kind == "email"
                and packet.evidence_id == evidence.dependency.chunk_id
                and packet.quote in evidence.text
            ):
                return VerifiedExcerpt(packet.quote, evidence)
    raise ConnectorError("INVALID_EVIDENCE", 503)
