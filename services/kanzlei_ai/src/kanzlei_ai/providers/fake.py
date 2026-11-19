"""Deterministic 64-dimensional synthetic embeddings; not a semantic model."""

import hashlib
import math
import re
import unicodedata
from collections.abc import Sequence


class SyntheticEmbedding:
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        result = []
        for text in texts:
            vector = [0.0] * 64
            for term in re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold()):
                digest = hashlib.sha256(term.encode()).digest()
                vector[int.from_bytes(digest[:4], "big") % 64] += 1.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            result.append([value / norm for value in vector])
        return result
