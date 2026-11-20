"""Deterministic invoice-amount provider for German synthetic documents."""

from kanzlei_ai.chat.citations import AnswerPacket, supported_amount
from kanzlei_ai.chat.context import Evidence


class ExtractiveChatProvider:
    async def generate(self, question: str, context: tuple[Evidence, ...]) -> AnswerPacket | None:
        answer = supported_amount(question, context)
        if answer is None:
            return None
        return AnswerPacket(answer.evidence.dependency.chunk_id, answer.amount)
