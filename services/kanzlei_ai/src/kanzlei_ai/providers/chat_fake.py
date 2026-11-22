"""Deterministic extractive provider for synthetic chat fixtures."""

import re

from kanzlei_ai.chat.citations import AnswerPacket, is_invoice_amount_question, supported_amount
from kanzlei_ai.chat.context import Evidence

_TERM = re.compile(r"[^\W\d_]{4,}")
_GENERIC = frozenset(
    "what when where which does about email document word says tell please was steht wann welche "
    "über bitte mail dokument письме письмо документе когда сказано скажи".split()
)


class ExtractiveChatProvider:
    async def generate(self, question: str, context: tuple[Evidence, ...]) -> AnswerPacket | None:
        if is_invoice_amount_question(question):
            answer = supported_amount(question, context)
            return (
                AnswerPacket(answer.evidence.dependency.chunk_id, answer.amount) if answer else None
            )

        terms = set(_TERM.findall(question.casefold())) - _GENERIC
        matches = []
        for item in context:
            if item.source_kind not in ("email", "docx"):
                continue
            for sentence in re.split(r"(?<=[.!?])\s+|\n\n+", item.text):
                excerpt = sentence.strip()
                overlap = len(terms.intersection(_TERM.findall(excerpt.casefold())))
                if overlap and len(excerpt) <= 400:
                    matches.append((overlap, AnswerPacket(item.dependency.chunk_id, excerpt)))
        matches.sort(key=lambda pair: pair[0], reverse=True)
        if matches and (len(matches) == 1 or matches[0][0] > matches[1][0]):
            return matches[0][1]
        return None
