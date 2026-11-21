from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from test_history_permissions import _PREFIX, _QUESTION, history, pdf_url, run_turn
from test_history_permissions import chat as chat
from test_scoped_search import RankedIndex, add_source

from kanzlei_ai import db
from kanzlei_ai.chat import api
from kanzlei_ai.chat.citations import AnswerPacket
from kanzlei_ai.chat.context import DOCX_MIME, Dependency, database_call, read_current
from kanzlei_ai.connectors.base import ConnectorError
from kanzlei_ai.connectors.contracts import ClientPage, ClientRecord, Scope, SourceRef
from kanzlei_ai.models import chat_turns, index_publications, sources
from kanzlei_ai.providers.chat_fake import ExtractiveChatProvider
from kanzlei_ai.search.index import MISTRAL_INDEX_PROFILE

pytestmark = pytest.mark.integration
_GENERAL_BODY = {"scope": {"kind": "general"}}
_GENERAL = Scope(kind="general")
_TOKEN = "x" * 43


def _create_general(client):
    response = client.post(_PREFIX, json=_GENERAL_BODY)
    assert response.status_code == 201
    return response.json()


def _dependency(candidate, name, client_id, *, chunk_id=None):
    return Dependency(
        SourceRef(source_id=f"file:{name}", revision=candidate.revision, client_id=client_id),
        candidate.generation,
        chunk_id or candidate.chunk_id,
    )


@pytest.fixture
def general_erp(chat, monkeypatch):
    _, erp, *_ = chat
    erp.general_allowed = True
    erp.clients = {"A": "Synthetic A", "B": "Synthetic B"}
    authorize_sources = erp.authorize_sources

    def require_scope(scope):
        if scope.kind == "general" and not erp.general_allowed:
            raise ConnectorError("FORBIDDEN", 403)

    async def check_scope(token, scope):
        assert token == _TOKEN
        require_scope(scope)

    async def authorize(token, scope, refs):
        assert token == _TOKEN
        require_scope(scope)
        return await authorize_sources(token, scope, refs)

    async def list_clients(token, limit=50, cursor=None):
        assert token == _TOKEN and cursor is None and limit >= len(erp.clients)
        return ClientPage(
            items=[ClientRecord(id=key, label=value) for key, value in erp.clients.items()],
            next_cursor=None,
        )

    monkeypatch.setattr(erp, "check_scope", check_scope)
    monkeypatch.setattr(erp, "authorize_sources", authorize)
    monkeypatch.setattr(erp, "list_clients", list_clients, raising=False)
    return erp


@pytest.fixture
def two_client_sources(chat, database):
    _, erp, _, _, invoice = chat
    letter = add_source(
        database, "letter", ["B"], "Le séminaire commence lundi.", connector_id=erp.connector_id
    )
    with database.begin() as connection:
        connection.execute(
            sources.update()
            .where(sources.c.id == letter.source_pk)
            .values(manifest={"title": "Letter.docx", "mime_type": DOCX_MIME})
        )
    return [invoice, letter]


@pytest.fixture
def retrieved(two_client_sources, monkeypatch):
    """Stub ranking while reading both exact evidence fixtures from PostgreSQL."""
    invoice, letter = two_client_sources
    refs = [
        _dependency(invoice, "invoice", "A"),
        _dependency(invoice, "invoice", "A", chunk_id="d" * 64),
        _dependency(letter, "letter", "B"),
    ]

    async def retrieve_context(engine, index, session, question):
        assert session.scope == _GENERAL
        result = []
        for dep in refs:
            scoped = replace(session, scope=Scope(kind="client", client_id=dep.source.client_id))
            current = await database_call(read_current, engine, scoped, [dep])
            result.append(current[dep])
        return tuple(result)

    monkeypatch.setattr(api, "retrieve_context", retrieve_context)
    return refs


def test_general_and_legacy_conversation_collections_are_disjoint(chat, general_erp):
    client, erp, _, legacy_id, _ = chat
    general = _create_general(client)
    assert general == {
        "id": general["id"],
        "client_id": None,
        "scope": {"kind": "general"},
        "created_at": general["created_at"],
    }
    assert client.get(_PREFIX, params={"scope": "general"}).json() == {"items": [general]}
    (legacy,) = client.get(_PREFIX, params={"client_id": "A"}).json()["items"]
    assert legacy == {"id": legacy_id, "client_id": "A", "created_at": legacy["created_at"]}
    assert client.get(f"{_PREFIX}/{general['id']}/sources").status_code == 404
    assert erp.document_requests == []


def test_general_can_answer_from_a_second_clients_document(chat, general_erp, two_client_sources):
    client, _, runtime, legacy, _ = chat
    _, letter = two_client_sources
    runtime.index = RankedIndex(sparse=[letter])
    general = _create_general(client)["id"]
    question = "Когда начинается séminaire?"

    event, answer = run_turn(client, general, question)[-1]
    assert event == "answer"
    assert answer["answer"] == "В документе сказано:\n\nLe séminaire commence lundi."
    (citation,) = answer["citations"]
    assert citation["client"] == {"id": "B", "label": "Synthetic B"}

    event, answer = run_turn(client, legacy, question)[-1]
    assert event == "answer" and answer["status"] == "no_evidence" and answer["citations"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"client_id": "A", **_GENERAL_BODY},
        {"scope": {"kind": "client", "client_id": "A"}},
        {"scope": {"kind": "general", "client_id": "A"}},
        {"client_id": None, **_GENERAL_BODY},
    ],
)
def test_create_requires_exactly_one_supported_scope(chat, payload):
    client, *_ = chat
    assert client.post(_PREFIX, json=payload).status_code == 422


@pytest.mark.parametrize(
    "params", [{}, {"scope": "client"}, {"client_id": ""}, {"scope": "general", "client_id": "A"}]
)
def test_list_requires_exactly_one_supported_scope(chat, params):
    client, *_ = chat
    assert client.get(_PREFIX, params=params).status_code == 422


@pytest.mark.parametrize("identity_field", ["subject", "connector_id"])
def test_general_conversation_is_private_to_its_connector_and_owner(
    chat, general_erp, retrieved, identity_field
):
    client, erp, _, _, _ = chat
    general = _create_general(client)["id"]
    event, answer = run_turn(client, general)[-1]
    assert event == "answer"
    setattr(erp, identity_field, "different")
    assert client.get(_PREFIX, params={"scope": "general"}).json() == {"items": []}
    assert history(client, general).status_code == 404
    assert client.get(pdf_url(general, answer)).status_code == 404
    assert (
        client.post(f"{_PREFIX}/{general}/turns", json={"question": _QUESTION}).status_code == 404
    )


def test_general_uses_live_role_even_with_an_affirmative_identity_hint(
    chat, general_erp, retrieved
):
    client, erp, _, legacy, _ = chat
    general = _create_general(client)["id"]
    event, answer = run_turn(client, general)[-1]
    assert event == "answer"
    erp.general_allowed = False
    assert client.post(_PREFIX, json=_GENERAL_BODY).status_code == 403
    assert client.get(_PREFIX, params={"scope": "general"}).status_code == 403
    assert history(client, general).status_code == 403
    assert (
        client.post(f"{_PREFIX}/{general}/turns", json={"question": _QUESTION}).status_code == 403
    )
    assert client.get(pdf_url(general, answer)).status_code == 403
    assert history(client, legacy).status_code == 200


def test_history_cites_fresh_client_name_and_requires_every_dependency(
    chat, general_erp, retrieved, database
):
    client, erp, _, _, _ = chat
    general = _create_general(client)["id"]
    event, answer = run_turn(client, general)[-1]
    assert event == "answer"
    (citation,) = answer["citations"]
    assert citation == {
        "id": citation["id"],
        "title": "Testrechnung.pdf",
        "page": 2,
        "client": {"id": "A", "label": "Synthetic A"},
        "source_kind": "pdf",
    }
    with database.connect() as connection:
        stored = connection.scalar(
            select(chat_turns.c.citations).where(chat_turns.c.id == answer["id"])
        )
    assert "client" not in stored[0]
    erp.clients["A"] = "Renamed A"
    assert history(client, general).json()["items"][0]["citations"] == [
        {**citation, "client": {"id": "A", "label": "Renamed A"}}
    ]
    del erp.clients["A"]
    assert history(client, general).json()["items"][0]["status"] == "unavailable"
    assert client.get(pdf_url(general, answer)).status_code == 404
    erp.clients["A"] = "Renamed A"
    erp.denied.add("file:letter")
    (unavailable,) = history(client, general).json()["items"]
    assert unavailable == {
        **answer,
        "status": "unavailable",
        "question": None,
        "answer": None,
        "citations": [],
    }
    assert client.get(pdf_url(general, answer)).status_code == 404
    assert erp.reads == 0


def test_history_requires_general_role_in_final_source_authorization(
    chat, general_erp, retrieved, monkeypatch
):
    client, erp, _, _, _ = chat
    general = _create_general(client)["id"]
    assert run_turn(client, general)[-1][0] == "answer"
    check_scope = erp.check_scope
    list_clients = erp.list_clients
    checked_clients = False

    async def list_and_mark(*args, **kwargs):
        nonlocal checked_clients
        result = await list_clients(*args, **kwargs)
        checked_clients = True
        return result

    async def revoke_after_scope_check(*args):
        await check_scope(*args)
        if checked_clients:
            erp.general_allowed = False

    monkeypatch.setattr(erp, "list_clients", list_and_mark)
    monkeypatch.setattr(erp, "check_scope", revoke_after_scope_check)
    assert history(client, general).status_code == 403


@pytest.mark.parametrize("disclosure", ["history", "answer", "pdf", "original"])
def test_general_disclosure_catches_source_revoked_during_late_scope_check(
    chat, general_erp, retrieved, monkeypatch, disclosure
):
    client, erp, runtime, _, _ = chat
    general = _create_general(client)["id"]
    question = _QUESTION
    denied_source = "file:letter"
    locator = None
    if disclosure == "original":
        question = "When does the seminar start?"
        denied_source = "file:invoice"
        locator = (
            api._DRIVE_DOWNLOAD_PATH
            + "?"
            + urlencode(
                {"customer": "B", "file_id": "letter", "revision": retrieved[2].source.revision}
            )
        )

        class Provider:
            async def generate(self, question, context):
                return AnswerPacket(retrieved[2].chunk_id, context[-1].text)

        runtime.provider = Provider()

    event, answer = run_turn(client, general, question)[-1]
    assert event == "answer" and answer["status"] == "answered"
    disclosure_ready = disclosure == "history"
    checked_clients = False
    check_scope = erp.check_scope
    list_clients = erp.list_clients
    store_turn = api.history.store_turn

    def ready():
        nonlocal disclosure_ready
        disclosure_ready = True

    async def list_and_mark(*args, **kwargs):
        nonlocal checked_clients
        result = await list_clients(*args, **kwargs)
        checked_clients = disclosure_ready
        return result

    async def revoke_during_scope_check(*args):
        await check_scope(*args)
        if checked_clients:
            erp.denied.add(denied_source)

    def store_and_mark(*args):
        item = store_turn(*args)
        ready()
        return item

    async def resolve_and_mark(token, scope, ref):
        assert (token, scope, ref) == (_TOKEN, _GENERAL, retrieved[2].source)
        ready()
        return locator

    monkeypatch.setattr(erp, "list_clients", list_and_mark)
    monkeypatch.setattr(erp, "check_scope", revoke_during_scope_check)
    monkeypatch.setattr(api.history, "store_turn", store_and_mark)
    monkeypatch.setattr(erp, "on_read", ready)
    monkeypatch.setattr(erp, "resolve_source_locator", resolve_and_mark, raising=False)

    if disclosure == "answer":
        assert run_turn(client, general)[-1] == ("error", {"code": "STALE_SOURCE"})
    elif disclosure == "history":
        (item,) = history(client, general).json()["items"]
        assert item == {
            **answer,
            "status": "unavailable",
            "question": None,
            "answer": None,
            "citations": [],
        }
    else:
        response = client.get(pdf_url(general, answer).removesuffix("/pdf") + f"/{disclosure}")
        assert response.status_code == 404
        assert response.json() == {"code": "NOT_FOUND"}
    assert denied_source in erp.denied


@pytest.mark.parametrize("has_evidence", [True, False])
def test_general_turn_rechecks_role_before_emitting_saved_answer(
    chat, general_erp, retrieved, monkeypatch, has_evidence
):
    client, erp, _, _, _ = chat
    general = _create_general(client)["id"]
    store_turn = api.history.store_turn
    if not has_evidence:
        retrieved.clear()

    def revoke_after_store(*args):
        item = store_turn(*args)
        erp.general_allowed = False
        return item

    monkeypatch.setattr(api.history, "store_turn", revoke_after_store)
    assert run_turn(client, general)[-1] == ("error", {"code": "FORBIDDEN"})
    assert history(client, general).status_code == 403


def test_general_pdf_uses_exact_ref_and_rechecks_all_dependencies_after_read(
    chat, general_erp, retrieved, monkeypatch
):
    client, erp, _, _, _ = chat
    general = _create_general(client)["id"]
    _, answer = run_turn(client, general)[-1]
    read_source = erp.read_source

    async def read(token, scope, ref):
        assert (token, scope, ref) == (_TOKEN, _GENERAL, retrieved[1].source)
        return await read_source(token, scope, ref)

    monkeypatch.setattr(erp, "read_source", read)
    assert client.get(pdf_url(general, answer)).content == erp.pdf
    erp.on_read = lambda: erp.denied.add("file:letter")
    assert client.get(pdf_url(general, answer)).status_code == 404
    assert erp.reads == 2


def test_general_original_uses_cited_client_and_rechecks_role_after_open(
    chat, general_erp, retrieved, monkeypatch
):
    client, erp, runtime, _, _ = chat
    general = _create_general(client)["id"]
    locator = (
        api._DRIVE_DOWNLOAD_PATH
        + "?"
        + urlencode(
            {"customer": "B", "file_id": "letter", "revision": retrieved[2].source.revision}
        )
    )

    class Provider:
        async def generate(self, question, context):
            return AnswerPacket(retrieved[2].chunk_id, context[-1].text)

    runtime.provider = Provider()
    event, answer = run_turn(client, general, "When does the seminar start?")[-1]
    assert event == "answer"
    assert answer["answer"] == "The document says:\n\nLe séminaire commence lundi."
    assert answer["citations"][0]["client"] == {"id": "B", "label": "Synthetic B"}
    original = pdf_url(general, answer).removesuffix("/pdf") + "/original"

    async def resolve(token, scope, ref):
        assert (token, scope, ref) == (_TOKEN, _GENERAL, retrieved[2].source)
        return locator

    monkeypatch.setattr(erp, "resolve_source_locator", resolve, raising=False)
    assert client.get(original).json() == {"source_kind": "docx", "locator": locator}

    async def revoke_during_open(*args):
        erp.general_allowed = False
        return await resolve(*args)

    monkeypatch.setattr(erp, "resolve_source_locator", revoke_during_open)
    denied = client.get(original)
    assert denied.status_code == 403
    assert locator not in denied.text


@pytest.mark.parametrize("case", ["both_consented", "b_denied", "empty"])
def test_mistral_generation_requires_exact_online_source_consent(
    chat, general_erp, retrieved, monkeypatch, database, case
):
    client, erp, runtime, _, _ = chat
    general = _create_general(client)["id"]
    monkeypatch.setattr(runtime.index, "profile", MISTRAL_INDEX_PROFILE, raising=False)
    with database.begin() as connection:
        connection.execute(index_publications.update().values(profile=MISTRAL_INDEX_PROFILE))
    calls = []

    async def policy(operation, scope, refs, *, access_token):
        assert (operation, scope, access_token) == ("generate", _GENERAL, _TOKEN)
        assert refs == [retrieved[0].source, retrieved[2].source]
        calls.append("policy")
        if case == "b_denied":
            raise ConnectorError("FORBIDDEN", 403)

    class Provider:
        async def generate(self, question, context):
            assert calls == ["policy"]
            assert [item.dependency for item in context] == retrieved
            calls.append("provider")
            return await ExtractiveChatProvider().generate(question, context)

    monkeypatch.setattr(erp, "check_processing_policy", policy, raising=False)
    runtime.provider = Provider()
    if case == "empty":
        retrieved.clear()
    event, answer = run_turn(client, general)[-1]
    if case == "b_denied":
        assert (event, answer, calls) == ("error", {"code": "FORBIDDEN"}, ["policy"])
        assert history(client, general).json()["items"] == []
    elif case == "empty":
        assert event == "answer" and answer["status"] == "no_evidence" and calls == []
    else:
        assert event == "answer" and answer["status"] == "answered"
        assert calls == ["policy", "provider"]


def test_migration_preserves_legacy_and_refuses_downgrade_with_general_rows(database):
    config = Config()
    config.set_main_option("script_location", str(Path(db.__file__).parent / "migrations"))

    def migrate(operation, target):
        with database.begin() as connection:
            config.attributes["connection"] = connection
            operation(config, target)

    previous = "0008_optional_chunk_page"
    migrate(command.upgrade, previous)
    with database.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO chat_conversations (id, connector_id, subject, client_id) "
                "VALUES ('legacy', 'origin', 'person', 'A')"
            )
        )
    migrate(command.upgrade, "head")
    with database.begin() as connection:
        assert connection.execute(
            text("SELECT scope_kind, client_id FROM chat_conversations WHERE id = 'legacy'")
        ).one() == ("client", "A")
        connection.execute(
            text(
                "INSERT INTO chat_conversations (id, connector_id, subject, scope_kind) "
                "VALUES ('general', 'origin', 'person', 'general')"
            )
        )
    for kind, client_id in [("client", None), ("general", "A"), ("other", None)]:
        with pytest.raises(IntegrityError), database.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO chat_conversations "
                    "(id, connector_id, subject, scope_kind, client_id) "
                    "VALUES ('invalid', 'origin', 'person', :kind, :client_id)"
                ),
                {"kind": kind, "client_id": client_id},
            )
    with pytest.raises(IntegrityError):
        migrate(command.downgrade, previous)
    with database.begin() as connection:
        assert connection.scalar(text("SELECT count(*) FROM chat_conversations")) == 2
        connection.execute(text("DELETE FROM chat_conversations WHERE id = 'general'"))
    migrate(command.downgrade, previous)
    with database.begin() as connection:
        assert connection.execute(text("SELECT id, client_id FROM chat_conversations")).one() == (
            "legacy",
            "A",
        )
