from uuid import uuid4

from qdrant_client import QdrantClient

from kanzlei_ai.search.index import SearchIndex, exact_identifiers, point_id


def test_exact_identifiers_are_complete_and_normalized():
    assert exact_identifiers("Re-2026-0042 and RE-2026-00420") == frozenset(
        {"re-2026-0042", "re-2026-00420"}
    )
    assert point_id("publication", "chunk") == point_id("publication", "chunk")
    assert point_id("publication", "chunk") != point_id("other", "chunk")


def test_search_index_uses_configured_embedding_profile_and_dimensions():
    class Embedding1024:
        def embed(self, texts, *, before_batch=None):
            if before_batch:
                before_batch()
            return [[1.0] + [0.0] * 1023 for _ in texts]

    client = QdrantClient(":memory:")
    index = SearchIndex(
        client=client,
        collection="mistral_test",
        embedding=Embedding1024(),
        profile="mistral-embed-v1",
        dimensions=1024,
    )
    publication = str(uuid4())
    checked = []
    index.stage(
        publication_id=publication,
        connector_id="erp",
        source_pk=1,
        revision="r1",
        generation=1,
        client_ids=["a"],
        chunks=[{"id": "c1", "text": "Rechnung RE-2026-0042"}],
        before_embed=lambda: checked.append(True),
    )
    assert checked == [True]
    assert client.get_collection(index.collection).config.params.vectors["dense"].size == 1024
    points, _ = client.scroll(index.collection, with_payload=True)
    assert points[0].payload["profile"] == "mistral-embed-v1"
    result = index.candidates(query="Rechnung", connector_id="erp", client_ids=["a"], limit=1)
    assert result["dense"][0].publication_id == publication


def test_scoped_hybrid_index_stages_and_returns_only_routing_data():
    client = QdrantClient(":memory:")
    index = SearchIndex(client=client, collection="test_synthetic")
    publication = str(uuid4())
    index.stage(
        publication_id=publication,
        connector_id="erp",
        source_pk=7,
        revision="r1",
        generation=2,
        client_ids=["a"],
        chunks=[{"id": "c1", "text": "Rechnung RE-2026-0042 alpha"}],
    )
    result = index.candidates(query="RE-2026-0042", connector_id="erp", client_ids=["a"], limit=10)
    assert result["exact"][0].publication_id == publication
    assert result["exact"][0].chunk_id == "c1"
    assert not index.candidates(
        query="RE-2026-004", connector_id="erp", client_ids=["a"], limit=10
    )["exact"]
    assert all(
        not hits
        for hits in index.candidates(
            query="RE-2026-0042", connector_id="erp", client_ids=["b"], limit=10
        ).values()
    )
    points, _ = client.scroll("test_synthetic", with_payload=True, with_vectors=False)
    assert "text" not in points[0].payload
    assert "RE-2026-0042" not in str(points[0].payload)


def test_rejects_incompatible_collection_and_existing_publication():
    import pytest
    from qdrant_client import models

    client = QdrantClient(":memory:")
    client.create_collection(
        "wrong",
        vectors_config={"dense": models.VectorParams(size=32, distance=models.Distance.COSINE)},
    )
    with pytest.raises(ValueError, match="schema"):
        SearchIndex(client=client, collection="wrong").stage(
            publication_id=str(uuid4()),
            connector_id="erp",
            source_pk=1,
            revision="r",
            generation=1,
            client_ids=["a"],
            chunks=[],
        )
    index = SearchIndex(client=client, collection="correct")
    kwargs = dict(
        publication_id=str(uuid4()),
        connector_id="erp",
        source_pk=1,
        revision="r",
        generation=1,
        client_ids=["a"],
        chunks=[{"id": "c", "text": "synthetic"}],
    )
    index.stage(**kwargs)
    with pytest.raises(ValueError, match="already staged"):
        index.stage(**kwargs)


def test_stage_requires_insert_only_writes():
    from unittest.mock import patch

    from qdrant_client import models

    client = QdrantClient(":memory:")
    index = SearchIndex(client=client, collection="safe")
    with patch.object(client, "upsert", wraps=client.upsert) as upsert:
        index.stage(
            publication_id=str(uuid4()),
            connector_id="erp",
            source_pk=1,
            revision="r",
            generation=1,
            client_ids=["a"],
            chunks=[{"id": "c", "text": "synthetic"}],
        )
    assert upsert.call_args.kwargs["update_mode"] == models.UpdateMode.INSERT_ONLY


def test_pinned_server_synthetic_index_when_configured():
    import os
    from unittest.mock import patch

    import pytest

    url = os.environ.get("TASK6_QDRANT_TEST_URL")
    if not url:
        pytest.skip("disposable Qdrant server not configured")
    client = QdrantClient(url=url, timeout=2)
    collection = "task6_adapter_" + uuid4().hex
    index = SearchIndex(client=client, collection=collection)
    try:
        index.stage(
            publication_id=str(uuid4()),
            connector_id="erp",
            source_pk=1,
            revision="r",
            generation=1,
            client_ids=["a"],
            chunks=[{"id": "c", "text": "synthetic RE-2026-0042"}],
        )
        result = index.candidates(
            query="RE-2026-0042 synthetic", connector_id="erp", client_ids=["a"], limit=2
        )
        assert all(len(result[branch]) == 1 for branch in ("exact", "dense", "sparse"))
        assert all(
            not hits
            for hits in index.candidates(
                query="synthetic RE-2026-0042", connector_id="erp", client_ids=["b"], limit=2
            ).values()
        )
        with patch.object(
            client, "create_payload_index", wraps=client.create_payload_index
        ) as create_index:
            index.stage(
                publication_id=str(uuid4()),
                connector_id="erp",
                source_pk=2,
                revision="r",
                generation=1,
                client_ids=["a"],
                chunks=[{"id": "c", "text": "synthetic"}],
            )
            create_index.assert_not_called()
        client.delete_payload_index(collection, "profile", wait=True)
        with patch.object(
            client, "create_payload_index", wraps=client.create_payload_index
        ) as create_index:
            index.stage(
                publication_id=str(uuid4()),
                connector_id="erp",
                source_pk=3,
                revision="r",
                generation=1,
                client_ids=["a"],
                chunks=[{"id": "c", "text": "synthetic"}],
            )
            assert [call.args[1] for call in create_index.call_args_list] == ["profile"]
        info = client.get_collection(collection)
        assert {
            "connector_id",
            "client_ids",
            "profile",
            "identifiers",
            "publication_id",
            "source_pk",
        } <= set(info.payload_schema)
    finally:
        if client.collection_exists(collection):
            client.delete_collection(collection)


def test_exact_identifier_preserves_all_supported_separators():
    assert exact_identifiers("Re-2026-0042/5") == frozenset({"re-2026-0042/5"})


def test_candidates_missing_collection_does_not_create_schema():
    from unittest.mock import patch

    client = QdrantClient(":memory:")
    index = SearchIndex(client=client, collection="missing")
    with (
        patch.object(client, "create_collection", wraps=client.create_collection) as create,
        patch.object(
            client, "create_payload_index", wraps=client.create_payload_index
        ) as create_index,
    ):
        assert index.candidates(
            query="synthetic", connector_id="erp", client_ids=["a"], limit=5
        ) == {"exact": [], "dense": [], "sparse": []}
        create.assert_not_called()
        create_index.assert_not_called()


def test_candidates_validates_schema_without_ddl():
    from unittest.mock import patch

    import pytest
    from qdrant_client import models

    client = QdrantClient(":memory:")
    index = SearchIndex(client=client, collection="populated")
    index.stage(
        publication_id=str(uuid4()),
        connector_id="erp",
        source_pk=1,
        revision="r",
        generation=1,
        client_ids=["a"],
        chunks=[{"id": "c", "text": "synthetic"}],
    )
    with (
        patch.object(client, "create_collection", wraps=client.create_collection) as create,
        patch.object(
            client, "create_payload_index", wraps=client.create_payload_index
        ) as create_index,
    ):
        assert index.candidates(query="synthetic", connector_id="erp", client_ids=["a"], limit=5)[
            "dense"
        ]
        create.assert_not_called()
        create_index.assert_not_called()
    client.create_collection(
        "wrong_read",
        vectors_config={"dense": models.VectorParams(size=32, distance=models.Distance.COSINE)},
    )
    with pytest.raises(ValueError, match="schema"):
        SearchIndex(client=client, collection="wrong_read").candidates(
            query="synthetic", connector_id="erp", client_ids=["a"], limit=5
        )


def test_malformed_and_boolean_candidates_are_skipped_in_every_branch():
    from types import SimpleNamespace
    from unittest.mock import patch

    client = QdrantClient(":memory:")
    index = SearchIndex(client=client, collection="validation")
    index.stage(
        publication_id=str(uuid4()),
        connector_id="erp",
        source_pk=1,
        revision="r",
        generation=1,
        client_ids=["a"],
        chunks=[{"id": "seed", "text": "synthetic"}],
    )
    publication = str(uuid4())
    valid_payload = {
        "publication_id": publication,
        "source_pk": 1,
        "revision": "r1",
        "generation": 1,
        "chunk_id": "valid",
    }
    valid = SimpleNamespace(id=point_id(publication, "valid"), payload=valid_payload)
    invalids = [
        SimpleNamespace(
            id=point_id(publication, "bad"),
            payload={k: v for k, v in valid_payload.items() if k != "chunk_id"},
        ),
        *[
            SimpleNamespace(
                id=point_id(publication, "valid"), payload={**valid_payload, field: value}
            )
            for field, value in [
                ("source_pk", True),
                ("generation", True),
                ("source_pk", 0),
                ("generation", -1),
                ("publication_id", "not-a-uuid"),
                ("revision", ""),
                ("revision", "r" * 65),
                ("chunk_id", "c" * 65),
            ]
        ],
        SimpleNamespace(id=str(uuid4()), payload=valid_payload),
        SimpleNamespace(id=str(uuid4()), payload=None),
    ]
    points = [*invalids, valid]
    with (
        patch.object(client, "scroll", return_value=(points, None)),
        patch.object(client, "query_points", return_value=SimpleNamespace(points=points)),
    ):
        result = index.candidates(
            query="synthetic RE-2026-0042", connector_id="erp", client_ids=["a"], limit=20
        )
    assert all(
        len(result[branch]) == 1 and result[branch][0].chunk_id == "valid"
        for branch in ("exact", "dense", "sparse")
    )


def test_boolean_numeric_routing_is_not_admitted():
    from types import SimpleNamespace
    from unittest.mock import patch

    client = QdrantClient(":memory:")
    index = SearchIndex(client=client, collection="boolean")
    index.stage(
        publication_id=str(uuid4()),
        connector_id="erp",
        source_pk=1,
        revision="r",
        generation=1,
        client_ids=["a"],
        chunks=[{"id": "seed", "text": "synthetic"}],
    )
    publication = str(uuid4())
    points = [
        SimpleNamespace(
            id=point_id(publication, "valid"),
            payload={
                "publication_id": publication,
                "source_pk": 1,
                "revision": "r",
                "generation": True,
                "chunk_id": "valid",
            },
        )
    ]
    with (
        patch.object(client, "scroll", return_value=(points, None)),
        patch.object(client, "query_points", return_value=SimpleNamespace(points=points)),
    ):
        result = index.candidates(
            query="synthetic RE-2026-0042", connector_id="erp", client_ids=["a"], limit=20
        )
    assert all(not values for values in result.values())
