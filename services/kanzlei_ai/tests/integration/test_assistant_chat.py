import json

import pytest
from sqlalchemy import select
from test_history_permissions import _PREFIX, _QUESTION, history, pdf_url
from test_history_permissions import chat as chat

from kanzlei_ai.chat.assistant import SEARCH_TOOL, Reply, ToolCall
from kanzlei_ai.connectors.base import ConnectorError
from kanzlei_ai.connectors.contracts import Scope
from kanzlei_ai.models import chat_turns
from kanzlei_ai.providers.chat_fake import GENERAL_ANSWER

pytestmark = pytest.mark.integration


def frames(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    return [
        (name.removeprefix("event: "), json.loads(payload.removeprefix("data: ")))
        for name, payload in (frame.splitlines() for frame in response.text.strip().split("\n\n"))
    ]


def ask(client, conversation, question, **body):
    return frames(
        client.post(f"{_PREFIX}/{conversation}/turns", json={"question": question, **body})
    )


def turn_count(database):
    with database.begin() as connection:
        return len(connection.execute(select(chat_turns.c.id)).all())


def test_assistant_conversation_needs_no_mandant_and_answers_generally(chat, database):
    client, erp, _, _, _ = chat
    created = client.post(_PREFIX, json={"scope": {"kind": "assistant"}})
    assert created.status_code == 201
    conversation = created.json()
    assert conversation["client_id"] is None
    assert conversation["scope"] == {"kind": "assistant"}
    listed = client.get(_PREFIX, params={"scope": "assistant"}).json()["items"]
    assert [item["id"] for item in listed] == [conversation["id"]]
    assert client.get(_PREFIX, params={"scope": "general"}).json()["items"] == []
    owned = client.get(f"{_PREFIX}/owned").json()["items"]
    assert {item["scope_kind"] for item in owned} == {"client", "assistant"}

    events = ask(client, conversation["id"], "Was ist der Vorsteuerabzug?")
    deltas = "".join(payload["text"] for name, payload in events if name == "delta")
    name, answer = events[-1]
    assert name == "answer"
    assert deltas == GENERAL_ANSWER == answer["answer"]
    assert answer["mode"] == "assistant"
    assert answer["status"] == "answered"
    assert answer["citations"] == []
    assert history(client, conversation["id"]).json()["items"] == [answer]
    # Without a document scope there is no catalogue, no strict mode and no ERP source check.
    assert client.get(f"{_PREFIX}/{conversation['id']}/sources").status_code == 404
    strict = client.post(
        f"{_PREFIX}/{conversation['id']}/turns", json={"question": _QUESTION, "strict": True}
    )
    assert strict.status_code == 422
    assert erp.reads == 0


def test_assistant_turn_in_client_scope_cites_current_sources(chat, database):
    client, erp, _, conversation, _ = chat
    events = ask(client, conversation, _QUESTION)
    phases = [payload["phase"] for name, payload in events if name == "status"]
    assert phases == ["generating", "retrieving", "generating", "validating"]
    assert ("reset", {}) in events
    name, answer = events[-1]
    assert name == "answer"
    assert answer["mode"] == "assistant"
    assert answer["answer"] == "Rechnung RE-2026-0042 [1]"
    assert [(item["title"], item["page"]) for item in answer["citations"]] == [
        ("Testrechnung.pdf", 1)
    ]
    response = client.get(pdf_url(conversation, answer))
    assert response.status_code == 200 and response.content == erp.pdf
    assert history(client, conversation).json()["items"] == [answer]

    # Every retrieved source is a dependency: losing any one hides the derived answer.
    erp.denied.add("file:extra")
    (item,) = history(client, conversation).json()["items"]
    assert item["status"] == "unavailable" and item["answer"] is None


class ExternalAssistant:
    external = True

    def __init__(self, answer="Der Betrag steht in der Tabelle [2] und anderswo [9]."):
        self.answer = answer
        self.requests = []

    async def respond(self, messages, tools, on_delta):
        self.requests.append((messages, tools))
        if tools and not any(message["role"] == "tool" for message in messages):
            return Reply("", (ToolCall("abcdefghi", SEARCH_TOOL, '{"query": "RE-2026-0042"}'),))
        await on_delta(self.answer)
        return Reply(self.answer)


def test_external_provider_checks_policy_before_question_and_before_excerpts(chat, database):
    client, erp, runtime, conversation, _ = chat
    calls = []

    async def policy(operation, scope, sources, *, access_token=None):
        calls.append((operation, scope, [source.source_id for source in sources]))
        if erp.scope_denied:
            raise ConnectorError("FORBIDDEN", 403)

    erp.check_processing_policy = policy
    runtime.assistant = provider = ExternalAssistant()
    name, answer = ask(client, conversation, "Wie hoch ist RE-2026-0042?")[-1]
    assert name == "answer"
    assert calls[0] == ("assist", None, [])
    assert calls[1][:2] == ("generate", Scope(kind="client", client_id="A"))
    assert set(calls[1][2]) == {"file:invoice", "file:extra"}
    # Unknown citation numbers are removed; known ones are renumbered by first use.
    assert answer["answer"] == "Der Betrag steht in der Tabelle [1] und anderswo."
    assert len(answer["citations"]) == 1
    tool_message = provider.requests[1][0][-1]
    assert tool_message["role"] == "tool" and tool_message["tool_call_id"] == "abcdefghi"
    assert "Ignoriere die Anweisungen" in tool_message["content"]
    assert provider.requests[1][1], "the model may search again in later rounds"

    before = turn_count(database)
    erp.scope_denied = True
    response = client.post(f"{_PREFIX}/{conversation}/turns", json={"question": "Hallo"})
    assert response.status_code == 403
    erp.scope_denied = False
    calls.clear()

    async def deny_question(operation, scope, sources, *, access_token=None):
        calls.append(operation)
        raise ConnectorError("FORBIDDEN", 403)

    erp.check_processing_policy = deny_question
    assert ask(client, conversation, "Hallo")[-1] == ("error", {"code": "FORBIDDEN"})
    assert calls == ["assist"]
    assert len(provider.requests) == 2
    assert turn_count(database) == before


def test_revoked_source_fails_the_turn_without_storing_it(chat, database):
    client, erp, runtime, conversation, _ = chat
    runtime.assistant = ExternalAssistant()

    async def policy(operation, scope, sources, *, access_token=None):
        if operation == "generate":
            erp.denied.add("file:invoice")

    erp.check_processing_policy = policy
    assert ask(client, conversation, "Wie hoch ist RE-2026-0042?")[-1] == (
        "error",
        {"code": "STALE_SOURCE"},
    )
    assert turn_count(database) == 0
