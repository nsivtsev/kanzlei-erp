"""Page-level OCR contract shared by extraction providers."""

from typing import Protocol

from kanzlei_ai.documents.parse import Block


class OcrProvider(Protocol):
    @property
    def profile(self) -> str:
        """Stable identifier for the OCR model and processing profile."""
        ...

    def recognize_page(self, content: bytes, page: int) -> list[Block]:
        """Recognize one 1-based page of an original PDF and return page-provenanced blocks."""
        ...
