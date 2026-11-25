"""Deterministic extractive provider for synthetic chat fixtures."""

import json
import re

from kanzlei_ai.chat.assistant import SEARCH_TOOL, Reply, ToolCall
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


GENERAL_ANSWER = "Synthetische allgemeine Antwort ohne Kanzlei-Dokumente."


class SyntheticAssistantProvider:
    """Deterministic stand-in: searches when the question has terms, cites one excerpt."""

    external = False

    async def respond(self, messages, tools, on_delta) -> Reply:
        question = next(m["content"] for m in messages if m["role"] == "user")
        results = [
            result
            for message in messages
            if message["role"] == "tool"
            for result in json.loads(message["content"]).get("results", [])
        ]
        searched = any(message["role"] == "tool" for message in messages)
        terms = set(_TERM.findall(question.casefold())) - _GENERIC
        if tools and not searched and terms:
            arguments = json.dumps({"query": question}, ensure_ascii=False)
            return Reply("", (ToolCall("call00001", SEARCH_TOOL, arguments),))
        text = GENERAL_ANSWER
        if searched:
            text = "Die Dokumente enthalten dazu keine Angaben."
            best: tuple[int, str, int] | None = None
            for result in results:
                for sentence in re.split(r"(?<=[.!?])\s+|\n+", result["text"]):
                    excerpt = sentence.strip(" |")
                    overlap = len(terms.intersection(_TERM.findall(excerpt.casefold())))
                    if overlap and len(excerpt) <= 400 and (best is None or overlap > best[0]):
                        best = (overlap, excerpt, result["number"])
            if best is not None:
                text = f"{best[1]} [{best[2]}]"
        if on_delta is not None:
            for piece in re.findall(r"\S+\s*", text):
                await on_delta(piece)
        return Reply(text)
