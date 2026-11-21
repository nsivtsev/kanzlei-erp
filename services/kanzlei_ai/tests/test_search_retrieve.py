import asyncio

import pytest

from kanzlei_ai.connectors.base import ConnectorError
from kanzlei_ai.connectors.contracts import ClientPage, ClientRecord, Identity, Scope


class EmptyIndex:
    def __init__(self):
        self.calls = []

    def candidates(self, **kwargs):
        self.calls.append(kwargs)
        return {}


class ScopeConnector:
    def __init__(self, *, general_access=True, pages=()):
        self.general_access = general_access
        self.pages = iter(pages)
        self.cursors = []
        self.probes = []

    async def identity(self, access_token):
        assert access_token == "synthetic"
        return Identity(
            subject="person",
            connector_id="origin",
            expires_at=1,
            general_access=self.general_access,
        )

    async def list_clients(self, access_token, limit=50, cursor=None):
        assert access_token == "synthetic"
        self.cursors.append(cursor)
        return next(self.pages)

    async def check_scope(self, access_token, scope):
        assert access_token == "synthetic"
        self.probes.append(scope.client_id)
        if scope.kind == "client" and scope.client_id != "A":
            raise ConnectorError("FORBIDDEN", 403)


def search(query, scope, connector, index, limit=10):
    from kanzlei_ai.search.retrieve import retrieve

    return asyncio.run(
        retrieve(query, scope, "synthetic", limit, engine=None, index=index, connector=connector)
    )


def page(ids, cursor):
    return ClientPage(
        items=[ClientRecord(id=client_id, label=client_id) for client_id in ids],
        next_cursor=cursor,
    )


def test_client_permissions_are_checked_before_search_without_listing_documents():
    connector, index = ScopeConnector(), EmptyIndex()
    assert search("synthetic", Scope(kind="client", client_id="A"), connector, index) == []
    assert connector.probes == ["A"]
    assert index.calls[0]["connector_id"] == "origin"
    assert tuple(index.calls[0]["client_ids"]) == ("A",)


def test_external_question_embedding_checks_policy_before_search():
    from kanzlei_ai.search.index import MISTRAL_INDEX_PROFILE

    class DeniedConnector(ScopeConnector):
        async def check_processing_policy(self, operation, scope, sources, *, access_token):
            assert (operation, scope.client_id, sources, access_token) == (
                "embed",
                "A",
                [],
                "synthetic",
            )
            raise ConnectorError("FORBIDDEN", 403)

    class ExternalIndex(EmptyIndex):
        profile = MISTRAL_INDEX_PROFILE

        def candidates(self, *, before_query_embed, **kwargs):
            before_query_embed()
            return super().candidates(**kwargs)

    index = ExternalIndex()
    with pytest.raises(ConnectorError, match="FORBIDDEN"):
        search("synthetic", Scope(kind="client", client_id="A"), DeniedConnector(), index)
    assert index.calls == []


def test_query_policy_is_refreshed_after_identifier_lookup(monkeypatch):
    from qdrant_client import QdrantClient

    from kanzlei_ai.search.index import MISTRAL_INDEX_PROFILE, SearchIndex

    revoked = False

    def revoke_during_scroll(*args, **kwargs):
        nonlocal revoked
        revoked = True
        return [], None

    class Connector(ScopeConnector):
        async def check_processing_policy(self, operation, scope, sources, *, access_token):
            if revoked:
                raise ConnectorError("FORBIDDEN", 403)

    class Embedding:
        def embed(self, texts):
            pytest.fail("Revoked question reached the external model")

    client = QdrantClient(":memory:")
    index = SearchIndex(
        client=client, collection="external", embedding=Embedding(), profile=MISTRAL_INDEX_PROFILE
    )
    index._ensure_schema()
    monkeypatch.setattr(client, "scroll", revoke_during_scroll)
    with pytest.raises(ConnectorError, match="FORBIDDEN"):
        search("RE-2026-0042", Scope(kind="client", client_id="A"), Connector(), index)


@pytest.mark.parametrize("kind", ["general", "client"])
def test_denied_scope_does_not_search(kind):
    connector, index = ScopeConnector(general_access=False), EmptyIndex()
    scope = Scope(kind=kind, client_id="B" if kind == "client" else None)
    with pytest.raises(ConnectorError, match="FORBIDDEN"):
        search("synthetic", scope, connector, index)
    assert index.calls == []


def test_general_search_uses_deduplicated_complete_client_list():
    connector = ScopeConnector(pages=[page(["B", "A"], "more"), page(["C", "B"], None)])
    index = EmptyIndex()
    assert search("synthetic", Scope(kind="general"), connector, index) == []
    assert connector.probes == [None]
    assert connector.cursors == [None, "more"]
    assert tuple(index.calls[0]["client_ids"]) == ("A", "B", "C")


def test_live_general_check_runs_before_client_discovery():
    class RevokedGeneralScope(ScopeConnector):
        async def check_scope(self, access_token, scope):
            assert scope == Scope(kind="general")
            raise ConnectorError("FORBIDDEN", 403)

    connector, index = RevokedGeneralScope(pages=[page(["A"], None)]), EmptyIndex()
    with pytest.raises(ConnectorError, match="FORBIDDEN"):
        search("synthetic", Scope(kind="general"), connector, index)
    assert connector.cursors == []
    assert index.calls == []


def test_authorized_client_labels_come_from_the_complete_erp_list():
    from kanzlei_ai.search.retrieve import list_authorized_clients

    connector = ScopeConnector(
        pages=[
            ClientPage(items=[ClientRecord(id="B", label="Synthetic Beta")], next_cursor="more"),
            ClientPage(items=[ClientRecord(id="A", label="Synthetic Alpha")], next_cursor=None),
        ]
    )
    assert asyncio.run(list_authorized_clients(connector, "synthetic")) == {
        "A": "Synthetic Alpha",
        "B": "Synthetic Beta",
    }


def test_general_search_fails_closed_on_inconsistent_client_labels():
    connector = ScopeConnector(
        pages=[
            page(["A"], "more"),
            ClientPage(items=[ClientRecord(id="A", label="Changed label")], next_cursor=None),
        ]
    )
    index = EmptyIndex()
    with pytest.raises(ConnectorError, match="SEARCH_SCOPE_INCOMPLETE"):
        search("synthetic", Scope(kind="general"), connector, index)
    assert index.calls == []


@pytest.mark.parametrize(
    "pages",
    [
        [page(["A"], "again"), page(["B"], "again")],
        [page([str(i)], str(i)) for i in range(101)],
    ],
)
def test_incomplete_general_enumeration_fails_before_search(pages):
    connector, index = ScopeConnector(pages=pages), EmptyIndex()
    with pytest.raises(ConnectorError):
        search("synthetic", Scope(kind="general"), connector, index)
    assert index.calls == []
    assert len(connector.cursors) <= 100


def test_entire_search_has_a_deadline(monkeypatch):
    from kanzlei_ai.search import retrieve as retrieval

    class SlowConnector(ScopeConnector):
        async def identity(self, access_token):
            await asyncio.sleep(60)

    monkeypatch.setattr(retrieval, "_SEARCH_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(ConnectorError):
        search("synthetic", Scope(kind="general"), SlowConnector(), EmptyIndex())


@pytest.mark.parametrize(("query", "limit"), [(" ", 10), ("x" * 10_000, 10), ("ok", 0), ("ok", 21)])
def test_invalid_search_bounds_fail_before_search(query, limit):
    connector, index = ScopeConnector(), EmptyIndex()
    with pytest.raises(ValueError):
        search(query, Scope(kind="client", client_id="A"), connector, index, limit)
    assert connector.probes == []
    assert index.calls == []


def test_scope_check_outage_fails_before_search():
    class UnavailableScope(ScopeConnector):
        async def check_scope(self, access_token, scope):
            raise ConnectorError()

    index = EmptyIndex()
    with pytest.raises(ConnectorError):
        search("synthetic", Scope(kind="client", client_id="A"), UnavailableScope(), index)
    assert index.calls == []
