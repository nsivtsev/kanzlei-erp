"""Rebuildable synthetic search projection; SQL remains the authority.

Sparse document weights use BM25 k1=1.2, b=0.75 and avgdl=100; Qdrant
applies IDF. SHA-256 term hashes are truncated to unsigned 32-bit indices;
collisions aggregate term frequencies before weighting, in sorted index order.
"""

import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from qdrant_client import QdrantClient, models

from kanzlei_ai.providers.base import EmbeddingProvider
from kanzlei_ai.providers.fake import SyntheticEmbedding

INDEX_PROFILE = "synthetic-v1"
MISTRAL_INDEX_PROFILE = "mistral-embed-v1"
_DIMENSIONS = 64
_PAYLOAD_INDEXES = (
    ("connector_id", models.PayloadSchemaType.KEYWORD),
    ("client_ids", models.PayloadSchemaType.KEYWORD),
    ("profile", models.PayloadSchemaType.KEYWORD),
    ("identifiers", models.PayloadSchemaType.KEYWORD),
    ("publication_id", models.PayloadSchemaType.KEYWORD),
    ("source_pk", models.PayloadSchemaType.INTEGER),
)
_IDENTIFIER = re.compile(
    r"(?<![\w/.-])(?=[\w/.-]*\d)[^\W_]+(?:[-/.][^\W_]+)+(?![\w]|[-/.][^\W_])", re.UNICODE
)


def exact_identifiers(text: str) -> frozenset[str]:
    return frozenset(_IDENTIFIER.findall(unicodedata.normalize("NFKC", text).casefold()))


def point_id(publication_id: str, chunk_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{len(publication_id)}:{publication_id}{chunk_id}"))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sparse(text: str, *, query: bool = False) -> models.SparseVector:
    terms = re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold())
    counts = Counter(int.from_bytes(hashlib.sha256(t.encode()).digest()[:4], "big") for t in terms)
    indices = sorted(counts)
    weights = [
        1.0 if query else counts[i] * 2.2 / (counts[i] + 1.2 * (0.25 + 0.75 * len(terms) / 100))
        for i in indices
    ]
    return models.SparseVector(indices=indices, values=weights)


def _match(key: str, value: str | int) -> models.FieldCondition:
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


@dataclass(frozen=True)
class Candidate:
    point_id: str
    publication_id: str
    source_pk: int
    revision: str
    generation: int
    chunk_id: str


class SearchIndex:
    def __init__(
        self,
        *,
        client: QdrantClient,
        collection: str,
        embedding: EmbeddingProvider | None = None,
        profile: str = INDEX_PROFILE,
        dimensions: int = _DIMENSIONS,
    ):
        self.client = client
        self.collection = collection
        self.embedding = embedding or SyntheticEmbedding()
        self.profile = profile
        self.dimensions = dimensions

    @classmethod
    def from_settings(
        cls, settings, *, check_compatibility: bool = True, interactive: bool = False
    ):
        key = settings.qdrant_api_key
        kwargs = {"collection": settings.search_collection}
        if settings.model_profile == "mistral":
            from kanzlei_ai.providers.mistral import MistralRequestGate
            from kanzlei_ai.providers.mistral_embeddings import (
                EMBEDDING_DIMENSIONS,
                MistralEmbeddingProvider,
            )

            kwargs = {
                "collection": settings.mistral_search_collection,
                "embedding": MistralEmbeddingProvider(
                    endpoint=settings.mistral_endpoint,
                    api_key=settings.mistral_api_key.get_secret_value(),
                    gate=MistralRequestGate.from_settings(settings, interactive=interactive),
                ),
                "profile": MISTRAL_INDEX_PROFILE,
                "dimensions": EMBEDDING_DIMENSIONS,
            }
        return cls(
            client=QdrantClient(
                url=str(settings.qdrant_url),
                api_key=key.get_secret_value() if key else None,
                timeout=2,
                check_compatibility=check_compatibility,
            ),
            **kwargs,
        )

    def _ensure_schema(self) -> None:
        if not self.client.collection_exists(self.collection):
            try:
                self.client.create_collection(
                    self.collection,
                    vectors_config={
                        "dense": models.VectorParams(
                            size=self.dimensions, distance=models.Distance.COSINE
                        )
                    },
                    sparse_vectors_config={
                        "sparse": models.SparseVectorParams(modifier=models.Modifier.IDF)
                    },
                )
            except Exception:
                if not self.client.collection_exists(self.collection):
                    raise
        info = self.client.get_collection(self.collection)
        self._validate_schema(info)
        for field, kind in _PAYLOAD_INDEXES:
            existing = info.payload_schema.get(field)
            if existing is not None:
                if existing.data_type != kind:
                    raise ValueError("incompatible search payload index schema")
                continue
            try:
                self.client.create_payload_index(self.collection, field, kind, wait=True)
            except Exception:
                refreshed = self.client.get_collection(self.collection).payload_schema.get(field)
                if refreshed is None or refreshed.data_type != kind:
                    raise

    def _validate_schema(self, info) -> None:
        params = info.config.params
        vectors = params.vectors
        sparse = params.sparse_vectors or {}
        if (
            not isinstance(vectors, dict)
            or set(vectors) != {"dense"}
            or vectors["dense"].size != self.dimensions
            or vectors["dense"].distance != models.Distance.COSINE
            or set(sparse) != {"sparse"}
            or sparse["sparse"].modifier != models.Modifier.IDF
        ):
            raise ValueError("incompatible search collection schema")
        for field, kind in _PAYLOAD_INDEXES:
            existing = info.payload_schema.get(field)
            if existing is not None and existing.data_type != kind:
                raise ValueError("incompatible search payload index schema")

    def stage(
        self,
        *,
        publication_id: str,
        connector_id: str,
        source_pk: int,
        revision: str,
        generation: int,
        client_ids: Sequence[str],
        chunks: Sequence[Mapping],
        before_embed: Callable[[], None] | None = None,
    ) -> None:
        UUID(publication_id)
        ids = [chunk["id"] for chunk in chunks]
        if len(set(ids)) != len(ids) or any(not isinstance(i, str) or not i for i in ids):
            raise ValueError("chunk IDs must be unique nonempty strings")
        texts = [chunk["text"] for chunk in chunks]
        if any(not isinstance(t, str) for t in texts):
            raise ValueError("chunk text must be a string")
        self._ensure_schema()
        if self._point_count(publication_id):
            raise ValueError("publication already staged")
        if self.profile == MISTRAL_INDEX_PROFILE:
            if before_embed is None:
                raise ValueError("External index publication requires a processing policy check")
            vectors = self.embedding.embed(texts, before_batch=before_embed)
        else:
            vectors = self.embedding.embed(texts)
        self._write_vectors(
            publication_id,
            connector_id,
            source_pk,
            revision,
            generation,
            client_ids,
            chunks,
            vectors,
        )
        if self._point_count(publication_id) != len(chunks):
            raise RuntimeError("search index publication incomplete")

    def stage_vectors(
        self,
        *,
        publication_id,
        connector_id,
        source_pk,
        revision,
        generation,
        client_ids,
        chunks,
        vectors,
    ):
        """Stage a confirmed batch under the durable build ID."""
        UUID(publication_id)
        self._ensure_schema()
        self._write_vectors(
            publication_id,
            connector_id,
            source_pk,
            revision,
            generation,
            client_ids,
            chunks,
            vectors,
        )

    def verify_staged(self, publication_id, expected_count):
        self._ensure_schema()
        return self._point_count(publication_id) == expected_count

    def _point_count(self, publication_id):
        return self.client.count(
            self.collection,
            count_filter=models.Filter(must=[_match("publication_id", publication_id)]),
            exact=True,
        ).count

    def _write_vectors(
        self,
        publication_id,
        connector_id,
        source_pk,
        revision,
        generation,
        client_ids,
        chunks,
        vectors,
    ):
        if len(vectors) != len(chunks) or any(len(v) != self.dimensions for v in vectors):
            raise ValueError("invalid embedding dimensions")
        points = [
            models.PointStruct(
                id=point_id(publication_id, chunk["id"]),
                vector={"dense": vector, "sparse": _sparse(chunk["text"])},
                payload={
                    "publication_id": publication_id,
                    "connector_id": connector_id,
                    "source_pk": source_pk,
                    "revision": revision,
                    "generation": generation,
                    "client_ids": list(client_ids),
                    "profile": self.profile,
                    "chunk_id": chunk["id"],
                    "identifiers": sorted(_hash(i) for i in exact_identifiers(chunk["text"])),
                },
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        for start in range(0, len(points), 100):
            result = self.client.upsert(
                self.collection,
                points=points[start : start + 100],
                wait=True,
                update_mode=models.UpdateMode.INSERT_ONLY,
            )
            if result.status != models.UpdateStatus.COMPLETED:
                raise RuntimeError("search index write not confirmed")

    def candidates(
        self,
        *,
        query: str,
        connector_id: str,
        client_ids: Sequence[str],
        limit: int,
        before_query_embed: Callable[[], None] | None = None,
        lexical_only: bool = False,
    ) -> dict[str, list[Candidate]]:
        result: dict[str, list[Candidate]] = {"exact": [], "dense": [], "sparse": []}
        if limit <= 0 or not client_ids:
            return result
        if not self.client.collection_exists(self.collection):
            return result
        self._validate_schema(self.client.get_collection(self.collection))
        base = [
            _match("connector_id", connector_id),
            _match("profile", self.profile),
            models.FieldCondition(key="client_ids", match=models.MatchAny(any=list(client_ids))),
        ]
        identifiers = sorted(_hash(i) for i in exact_identifiers(query))

        def convert(points):
            candidates = []
            for point in points:
                payload = point.payload
                if not isinstance(payload, Mapping):
                    continue
                publication = payload.get("publication_id")
                revision = payload.get("revision")
                chunk = payload.get("chunk_id")
                source = payload.get("source_pk")
                generation = payload.get("generation")
                if (
                    not isinstance(publication, str)
                    or len(publication) != 36
                    or not isinstance(revision, str)
                    or not 1 <= len(revision) <= 64
                    or not isinstance(chunk, str)
                    or not 1 <= len(chunk) <= 64
                    or type(source) is not int
                    or not 0 < source <= 2**31 - 1
                    or type(generation) is not int
                    or not 0 < generation <= 2**31 - 1
                ):
                    continue
                try:
                    if str(UUID(publication)) != publication:
                        continue
                except ValueError:
                    continue
                if str(point.id) != point_id(publication, chunk):
                    continue
                candidates.append(
                    Candidate(
                        point_id=str(point.id),
                        publication_id=publication,
                        source_pk=source,
                        revision=revision,
                        generation=generation,
                        chunk_id=chunk,
                    )
                )
            return candidates

        if identifiers:
            points, _ = self.client.scroll(
                self.collection,
                scroll_filter=models.Filter(
                    must=[
                        *base,
                        models.FieldCondition(
                            key="identifiers", match=models.MatchAny(any=identifiers)
                        ),
                    ]
                ),
                limit=limit,
                with_payload=True,
            )
            result["exact"] = convert(points)
        if not lexical_only:
            if self.profile == MISTRAL_INDEX_PROFILE:
                if before_query_embed is None:
                    raise ValueError("External query embedding requires a processing policy check")
                before_query_embed()
            dense = self.embedding.embed([query])
            if len(dense) != 1 or len(dense[0]) != self.dimensions:
                raise ValueError("invalid embedding dimensions")
            result["dense"] = convert(
                self.client.query_points(
                    self.collection,
                    query=dense[0],
                    using="dense",
                    query_filter=models.Filter(must=base),
                    limit=limit,
                ).points
            )
        sparse = _sparse(query, query=True)
        if sparse.indices:
            result["sparse"] = convert(
                self.client.query_points(
                    self.collection,
                    query=sparse,
                    using="sparse",
                    query_filter=models.Filter(must=base),
                    limit=limit,
                ).points
            )
        return result
