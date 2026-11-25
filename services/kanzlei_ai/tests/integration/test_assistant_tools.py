import json

import pytest
from test_assistant_chat import ask, turn_count
from test_history_permissions import _PREFIX, _QUESTION, history
from test_history_permissions import chat as chat

from kanzlei_ai.chat.assistant import ERP_TOOL, READ_TOOL, SEARCH_TOOL, Reply, ToolCall
from kanzlei_ai.connectors.base import ConnectorError
from kanzlei_ai.connectors.contracts import Scope, WorkContext, WorkItem, WorkQuery

pytestmark = pytest.mark.integration
_INVOICE = {"source_id": "file:invoice", "client_id": "A"}


class Scripted:
    """Answers after a fixed list of tool calls and records every request."""

    external = False

    def __init__(self, *calls, answer="Antwort [1]."):
        self.calls = list(calls)
        self.answer = answer
        self.requests = []

    async def respond(self, messages, tools, on_delta):
        self.requests.append(([dict(message) for message in messages], tools))
        if tools and self.calls:
            name, arguments = self.calls.pop(0)
            call = ToolCall(f"call{len(self.requests):05d}", name, json.dumps(arguments))
            return Reply("", (call,))
        await on_delta(self.answer)
        return Reply(self.answer)


def tool_results(messages):
    return [json.loads(m["content"]) for m in messages if m["role"] == "tool"]


def test_attached_document_is_read_before_the_first_request(chat, database):
    client, erp, runtime, conversation, _ = chat
    runtime.assistant = provider = Scripted()
    events = ask(client, conversation, "Fasse den Anhang zusammen.", attachments=[_INVOICE])
    assert ("status", {"phase": "reading"}) in events
    name, answer = events[-1]
    assert name == "answer"
    assert answer["answer"] == "Antwort [1]."
    assert [item["title"] for item in answer["citations"]] == ["Testrechnung.pdf"]
    messages, tools = provider.requests[0]
    assert tools, "the model may still search or read more"
    assert messages[-2]["tool_calls"][0]["function"]["name"] == READ_TOOL
    (attached,) = tool_results(messages)
    assert attached["title"] == "Testrechnung.pdf"
    assert [result["number"] for result in attached["results"]] == [1, 2]
    assert "RE-2026-0042" in attached["results"][0]["text"]
    assert "next_offset" not in attached
    assert "attached documents" in messages[0]["content"]

    # A revoked attachment fails the turn before the model sees anything.
    provider.requests.clear()
    erp.denied.add("file:invoice")
    events = ask(client, conversation, "Und jetzt?", attachments=[_INVOICE])
    assert events[-1] == ("error", {"code": "ATTACHMENT_UNAVAILABLE"})
    assert provider.requests == []
    erp.denied.clear()


@pytest.mark.parametrize(
    "body",
    [
        {"attachments": [_INVOICE], "strict": True},
        {"attachments": [_INVOICE, _INVOICE]},
        {"attachments": [_INVOICE] * 4},
        {"attachments": [{"source_id": "invoice", "client_id": "A"}]},
        {"attachments": [{**_INVOICE, "revision": "a" * 64}]},
    ],
    ids=["strict", "duplicate", "too-many", "invalid-id", "extra-field"],
)
def test_invalid_attachments_are_rejected(chat, database, body):
    client, _, _, conversation, _ = chat
    response = client.post(f"{_PREFIX}/{conversation}/turns", json={"question": "Frage", **body})
    assert response.status_code == 422


def test_attachments_need_a_document_scope_in_the_same_mandant(chat, database):
    client, _, runtime, conversation, _ = chat
    runtime.assistant = Scripted()
    other = {"source_id": "file:invoice", "client_id": "B"}
    assert ask(client, conversation, "Frage", attachments=[other])[-1] == (
        "error",
        {"code": "ATTACHMENT_UNAVAILABLE"},
    )
    general = client.post(_PREFIX, json={"scope": {"kind": "assistant"}}).json()["id"]
    response = client.post(
        f"{_PREFIX}/{general}/turns", json={"question": "Frage", "attachments": [_INVOICE]}
    )
    assert response.status_code == 422
    assert turn_count(database) == 0


def test_read_document_continues_a_source_found_by_search(chat, database):
    client, _, runtime, conversation, _ = chat
    runtime.assistant = provider = Scripted(
        (SEARCH_TOOL, {"query": "RE-2026-0042"}),
        (READ_TOOL, {"document": 1, "offset": 1}),
        (READ_TOOL, {"document": 99}),
    )
    name, answer = ask(client, conversation, "Was steht in der ganzen Rechnung?")[-1]
    assert name == "answer"
    found, page, unknown = tool_results(provider.requests[-1][0])
    by_number = {item["number"]: item for item in found["results"]}
    assert page["title"] == "Testrechnung.pdf"
    # The table on page 2 was already numbered by the search; its number is reused.
    assert [(item["number"], item.get("page")) for item in page["results"]] == [
        (by_number[page["results"][0]["number"]]["number"], 2)
    ]
    assert "Beratung" in page["results"][0]["text"]
    assert unknown == {"error": "unknown_document"}


def test_memory_passes_recent_available_turns_and_inherits_their_sources(chat, database):
    client, erp, runtime, conversation, _ = chat
    first = ask(client, conversation, _QUESTION)[-1][1]
    assert first["citations"], "the synthetic provider cites the invoice"
    runtime.assistant = provider = Scripted(answer="Ja, genau.")
    second = ask(client, conversation, "Und was bedeutet das?")[-1][1]
    messages, _ = provider.requests[0]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == first["question"]
    assert "[" not in messages[2]["content"], "earlier citation numbers are not passed on"
    assert messages[2]["content"] == first["answer"].replace(" [1]", "")
    assert second["citations"] == []

    # The follow-up was derived from the invoice through memory, so it disappears too.
    erp.denied.add("file:invoice")
    items = history(client, conversation).json()["items"]
    assert [item["status"] for item in items] == ["unavailable", "unavailable"]
    provider.requests.clear()
    ask(client, conversation, "Noch etwas?")
    messages, _ = provider.requests[0]
    assert [m["role"] for m in messages] == ["system", "user"]


def test_memory_of_an_assistant_conversation_without_documents(chat, database):
    client, _, runtime, _, _ = chat
    conversation = client.post(_PREFIX, json={"scope": {"kind": "assistant"}}).json()["id"]
    ask(client, conversation, "Was ist der Vorsteuerabzug?")
    runtime.assistant = provider = Scripted(answer="Gern.")
    ask(client, conversation, "Kannst du das kürzer sagen?")
    messages, tools = provider.requests[0]
    assert tools == ()
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]


def work(*items, truncated=False):
    return WorkContext(
        version=1,
        today="2026-11-25",
        items=[
            WorkItem(
                type="task",
                id=item_id,
                client_id=client_id,
                client_label=f"Mandant {client_id}",
                title=title,
                status="Open",
                due_date="2026-12-01",
                due_kind="task",
                note=None,
            )
            for item_id, client_id, title in items
        ],
        truncated=truncated,
    )


def test_erp_query_returns_bounded_records_and_marks_the_answer(chat, database):
    client, erp, runtime, conversation, _ = chat
    queries = []

    async def read_work_context(access_token, scope, query, *, external):
        queries.append((scope, query, external))
        return work(("T-1", "A", "USt-Voranmeldung"))

    erp.read_work_context = read_work_context
    events = ask(client, conversation, "Welche Fristen stehen an?")
    name, answer = events[-1]
    assert name == "answer"
    assert "USt-Voranmeldung" in answer["answer"]
    assert answer["erp_data"] is True and answer["citations"] == []
    assert queries == [(Scope(kind="client", client_id="A"), WorkQuery(kind="deadlines"), False)]
    (stored,) = history(client, conversation).json()["items"]
    assert stored["erp_data"] is True

    # A record of another Mandant in a client conversation is a contract violation.
    async def leaky(access_token, scope, query, *, external):
        return work(("T-9", "B", "Fremd"))

    erp.read_work_context = leaky
    before = turn_count(database)
    assert ask(client, conversation, "Welche Aufgaben sind offen?")[-1][0] == "error"
    assert turn_count(database) == before


def test_erp_denial_is_a_tool_result_and_invalid_queries_never_reach_the_erp(chat, database):
    client, erp, runtime, conversation, _ = chat
    queries = []

    async def read_work_context(access_token, scope, query, *, external):
        queries.append(query)
        raise ConnectorError("FORBIDDEN", 403)

    erp.read_work_context = read_work_context
    runtime.assistant = provider = Scripted(
        (ERP_TOOL, {"kind": "sql"}),
        (ERP_TOOL, {"kind": "tasks", "days": 500}),
        (ERP_TOOL, {"kind": "tasks", "client_id": "B"}),
        answer="Keine Daten.",
    )
    name, answer = ask(client, conversation, "Aufgaben?")[-1]
    assert name == "answer" and "erp_data" not in answer
    assert tool_results(provider.requests[-1][0]) == [
        {"error": "invalid_tool_call"},
        {"error": "invalid_tool_call"},
        {"error": "not_available"},
    ]
    assert queries == [WorkQuery(kind="tasks", client_id="B")]


def test_external_provider_rechecks_consent_for_sources_in_memory(chat, database):
    client, erp, runtime, conversation, _ = chat
    ask(client, conversation, _QUESTION)
    calls = []

    async def policy(operation, scope, sources, *, access_token=None):
        calls.append((operation, [source.source_id for source in sources]))
        if operation == "generate":
            raise ConnectorError("FORBIDDEN", 403)

    erp.check_processing_policy = policy
    runtime.assistant = provider = Scripted(answer="Ja.")
    provider.external = True
    assert ask(client, conversation, "Und was bedeutet das?")[-1] == (
        "error",
        {"code": "FORBIDDEN"},
    )
    assert [operation for operation, _ in calls] == ["assist", "generate"]
    assert "file:invoice" in calls[1][1]
    assert provider.requests == []
