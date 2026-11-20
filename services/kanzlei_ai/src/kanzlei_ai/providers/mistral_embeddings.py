"""Fixed-width Mistral text embeddings in caller input order."""

import math
from collections.abc import Callable, Iterator, Sequence

import httpx

from kanzlei_ai.providers.mistral import MistralError, MistralRequestGate, _MistralEndpoint

EMBEDDING_MODEL = "mistral-embed"
EMBEDDING_DIMENSIONS = 1024
# A conservative UTF-8 byte budget for the embedding model's batch token limit.
_BATCH_BUDGET = 15000
_INPUT_OVERHEAD = 16
MAX_BATCH_ITEMS = 32
_MAX_EMBEDDING_RESPONSE_BYTES = 1024 * 1024


def embedding_batches(texts: Sequence[str]) -> Iterator[list[str]]:
    batch: list[str] = []
    size = 0
    for text in texts:
        try:
            cost = len(text.encode("utf-8")) + _INPUT_OVERHEAD
        except UnicodeError:
            raise MistralError("MISTRAL_INVALID_INPUT", retryable=False) from None
        if cost > _BATCH_BUDGET:
            raise MistralError("MISTRAL_INPUT_LIMIT_EXCEEDED", retryable=False)
        if size + cost > _BATCH_BUDGET or len(batch) == MAX_BATCH_ITEMS:
            yield batch
            batch = []
            size = 0
        batch.append(text)
        size += cost
    if batch:
        yield batch


class MistralEmbeddingProvider:
    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        timeout_seconds: float = 10,
        transport: httpx.BaseTransport | None = None,
        gate: MistralRequestGate | None = None,
    ):
        self._http = _MistralEndpoint(endpoint, api_key, timeout_seconds, gate)
        self._transport = transport

    def embed(
        self, texts: Sequence[str], *, before_batch: Callable[[], None] | None = None
    ) -> list[list[float]]:
        batches = list(embedding_batches(texts))
        if not batches:
            return []

        vectors: list[list[float]] = []
        with self._http.client(self._transport) as client:
            for batch in batches:
                if before_batch is not None:
                    before_batch()
                result = self._http.post(
                    client,
                    "embeddings",
                    {"model": EMBEDDING_MODEL, "input": batch, "encoding_format": "float"},
                    limit=_MAX_EMBEDDING_RESPONSE_BYTES,
                )
                vectors.extend(_ordered_vectors(result, len(batch)))
        return vectors


def _ordered_vectors(result: dict, count: int) -> list[list[float]]:
    data = result.get("data")
    if not isinstance(data, list) or len(data) != count:
        raise MistralError("MISTRAL_INVALID_RESPONSE")
    by_index: dict[int, list[float]] = {}
    for item in data:
        if not isinstance(item, dict):
            raise MistralError("MISTRAL_INVALID_RESPONSE")
        index, vector = item.get("index"), item.get("embedding")
        if (
            type(index) is not int
            or not 0 <= index < count
            or index in by_index
            or not isinstance(vector, list)
            or len(vector) != EMBEDDING_DIMENSIONS
            or any(type(value) not in {float, int} for value in vector)
        ):
            raise MistralError("MISTRAL_INVALID_RESPONSE")
        try:
            values = [float(value) for value in vector]
        except OverflowError:
            raise MistralError("MISTRAL_INVALID_RESPONSE") from None
        if not all(map(math.isfinite, values)):
            raise MistralError("MISTRAL_INVALID_RESPONSE")
        by_index[index] = values
    return [by_index[index] for index in range(count)]
