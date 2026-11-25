import asyncio
import json

import httpx
import pytest

from kanzlei_ai.chat.assistant import SEARCH_TOOL, TOOLS, cite, strip_markers
from kanzlei_ai.chat.context import Dependency, Evidence
from kanzlei_ai.connectors.contracts import SourceRef
from kanzlei_ai.providers.mistral import MistralError
from kanzlei_ai.providers.mistral_chat import MistralAssistantProvider

ENDPOINT = "https://api.eu.mistral.ai"
SECRET = "synthetic-key"


def evidence(number):
    ref = SourceRef(source_id=f"file:{number}", client_id="A", revision="a" * 64)
    return Evidence(Dependency(ref, 1, str(number) * 64), 1, "text", f"Text {number}")


def test_cite_renumbers_by_first_use_and_drops_unknown_markers():
    context = (evidence(1), evidence(2), evidence(3))
    text, cited = cite("B [3], A [1] und C [3]. Erfunden [4] [0].", context)
    assert text == "B [1], A [2] und C [1]. Erfunden."
    assert cited == (context[2], context[0])
    assert cite("Ohne Belege [1].", ()) == ("Ohne Belege.", ())


def sse(*events, done=True):
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return body + ("data: [DONE]\n\n" if done else "")


def chunk(delta, finish=None):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def provider(handler):
    return MistralAssistantProvider(
        endpoint=ENDPOINT, api_key=SECRET, transport=httpx.MockTransport(handler), model="m-1"
    )


def test_streams_content_and_sends_tools_only_when_offered():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        assert request.url == f"{ENDPOINT}/v1/chat/completions"
        body = sse(
            chunk({"role": "assistant", "content": "Hallo"}), chunk({"content": " Welt"}, "stop")
        )
        return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})

    deltas = []

    async def on_delta(text):
        deltas.append(text)

    messages = [{"role": "user", "content": "Frage"}]
    reply = asyncio.run(provider(handler).respond(messages, (), on_delta))
    assert (reply.content, reply.tool_calls, deltas) == ("Hallo Welt", (), ["Hallo", " Welt"])
    assert requests[0]["stream"] is True and requests[0]["model"] == "m-1"
    assert "tools" not in requests[0]
    asyncio.run(provider(handler).respond(messages, TOOLS, None))
    assert requests[1]["tools"] == list(TOOLS) and requests[1]["tool_choice"] == "auto"


def test_collects_streamed_tool_call_fragments():
    def handler(request):
        call = {"id": "abcdefghi", "function": {"name": SEARCH_TOOL, "arguments": '{"query": '}}
        rest = {"index": 0, "function": {"arguments": '"RE-1"}'}}
        body = sse(
            chunk({"tool_calls": [{**call, "index": 0}]}),
            chunk({"tool_calls": [rest]}, "tool_calls"),
        )
        return httpx.Response(200, text=body)

    reply = asyncio.run(provider(handler).respond([], TOOLS, None))
    (call,) = reply.tool_calls
    assert (call.id, call.name, json.loads(call.arguments)) == (
        "abcdefghi",
        SEARCH_TOOL,
        {"query": "RE-1"},
    )


@pytest.mark.parametrize(
    "body",
    [
        sse(chunk({"content": "abgeschnitten"}, "length")),
        sse(chunk({"content": "ohne Ende"}), done=True),
        sse(chunk({"content": "ohne Abschluss"}), done=False),
        sse(
            chunk(
                {"tool_calls": [{"function": {"name": SEARCH_TOOL, "arguments": "{}"}}]},
                "tool_calls",
            )
        ),
        sse({"choices": []}),
        "data: {not json}\n\n",
    ],
    ids=["length", "no-finish", "no-done", "tool-without-id", "no-choice", "invalid-json"],
)
def test_incomplete_or_malformed_streams_are_rejected(body):
    adapter = provider(lambda request: httpx.Response(200, text=body))
    with pytest.raises(MistralError):
        asyncio.run(adapter.respond([], TOOLS, None))


def test_oversized_stream_and_http_errors_are_sanitized():
    huge = sse(*(chunk({"content": "x" * 4096}) for _ in range(600)))
    with pytest.raises(MistralError, match="LIMIT"):
        asyncio.run(provider(lambda request: httpx.Response(200, text=huge)).respond([], (), None))
    with pytest.raises(MistralError) as error:
        asyncio.run(
            provider(lambda request: httpx.Response(429, headers={"Retry-After": "7"})).respond(
                [], (), None
            )
        )
    assert error.value.retry_after == 7 and SECRET not in str(error.value)


def test_memory_text_drops_earlier_citation_numbers():
    assert strip_markers("Betrag 1.249,50 EUR [1]. Siehe auch [2] und [3] .") == (
        "Betrag 1.249,50 EUR. Siehe auch und."
    )
