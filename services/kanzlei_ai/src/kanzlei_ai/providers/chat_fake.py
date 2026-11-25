"""Deterministic extractive provider for synthetic chat fixtures."""

import json
import re

from kanzlei_ai.chat.assistant import ERP_TOOL, SEARCH_TOOL, Reply, ToolCall
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


_ERP = re.compile(r"aufgabe|frist|termin|task|deadline|задач|срок")


class SyntheticAssistantProvider:
    """Deterministic stand-in: asks the ERP or searches when useful, cites one excerpt."""

    external = False

    async def respond(self, messages, tools, on_delta) -> Reply:
        last = max(index for index, message in enumerate(messages) if message["role"] == "user")
        question = messages[last]["content"]
        current = messages[last + 1 :]
        payloads = [json.loads(m["content"]) for m in current if m["role"] == "tool"]
        results = [result for payload in payloads for result in payload.get("results", [])]
        records = [record for payload in payloads for record in payload.get("records", [])]
        attached = any(m.get("tool_call_id", "").startswith("attach") for m in current)
        terms = set(_TERM.findall(question.casefold())) - _GENERIC
        if tools and not payloads:
            erp = _ERP.search(question.casefold())
            if erp:
                due = erp.group(0) in ("frist", "termin", "deadline", "срок")
                arguments = json.dumps({"kind": "deadlines" if due else "tasks"})
                return Reply("", (ToolCall("call00002", ERP_TOOL, arguments),))
            if terms:
                arguments = json.dumps({"query": question}, ensure_ascii=False)
                return Reply("", (ToolCall("call00001", SEARCH_TOOL, arguments),))
        text = GENERAL_ANSWER
        if records:
            text = "Offene Punkte aus dem ERP:\n" + "\n".join(
                f"- **{record['title']}** ({record['client_label']}, "
                f"fällig {record.get('due_date', 'ohne Datum')})"
                for record in records
            )
        elif payloads and not results and any("records" in payload for payload in payloads):
            text = "Im ERP sind dazu keine offenen Punkte erfasst."
        elif payloads:
            text = "Die Dokumente enthalten dazu keine Angaben."
            best: tuple[int, str, int] | None = None
            for result in results:
                for sentence in re.split(r"(?<=[.!?])\s+|\n+", result["text"]):
                    excerpt = sentence.strip(" |")
                    overlap = len(terms.intersection(_TERM.findall(excerpt.casefold())))
                    if overlap and len(excerpt) <= 400 and (best is None or overlap > best[0]):
                        best = (overlap, excerpt, result["number"])
            if best is None and attached and results:
                first = re.split(r"(?<=[.!?])\s+|\n+", results[0]["text"].strip())[0][:400]
                best = (0, "Zusammenfassung: " + first.strip(" |"), results[0]["number"])
            if best is not None:
                text = f"{best[1]} [{best[2]}]"
        if on_delta is not None:
            for piece in re.findall(r"\S+\s*", text):
                await on_delta(piece)
        return Reply(text)
