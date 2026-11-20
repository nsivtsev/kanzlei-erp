import asyncio
import base64
import json

import httpx
import pytest

from kanzlei_ai.chat.citations import AnswerPacket, supported_amount, validate_answer
from kanzlei_ai.chat.context import Dependency, Evidence
from kanzlei_ai.connectors.contracts import SourceRef
from kanzlei_ai.documents.parse import (
    MAX_SOURCE_BYTES,
    Block,
    ExtractionError,
    TerminalExtractionError,
)
from kanzlei_ai.providers.mistral import MistralError
from kanzlei_ai.providers.mistral_chat import MistralChatProvider
from kanzlei_ai.providers.mistral_embeddings import MistralEmbeddingProvider
from kanzlei_ai.providers.mistral_ocr import MistralOcrProvider

ENDPOINT = "https://mistral.invalid/v1/"
SECRET = "synthetic-key"
PDF = b"%PDF-1.7 synthetic"
TABLE = "| Artikel | Betrag |\n| --- | --- |\n| Gesamt | 42,00 EUR |"
QUESTION = "Wie hoch ist der Betrag für Rechnung RE-2026-123?"
SOURCE = SourceRef(source_id="file:private-source", revision="1" * 64, client_id="private-id")
CONTEXT = (
    Evidence(Dependency(SOURCE, 1, "private-heading"), 1, "text", "Rechnung RE-2026-123"),
    Evidence(Dependency(SOURCE, 1, "private-table"), 3, "table", TABLE, "private-title"),
)


def provider(provider_type, handler, **kwargs):
    return provider_type(
        endpoint=ENDPOINT, api_key=SECRET, transport=httpx.MockTransport(handler), **kwargs
    )


def test_ocr_posts_original_inline_and_keeps_page_and_table_order():
    def handler(request):
        assert str(request.url) == "https://mistral.invalid/v1/ocr"
        assert request.method == "POST"
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        body = json.loads(request.content)
        assert body["document"] == {
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{base64.b64encode(PDF).decode()}",
        }
        assert body["model"] == "mistral-ocr-4-1"
        assert body["pages"] == [2]
        assert body["table_format"] == "markdown"
        assert body["include_image_base64"] is False
        return httpx.Response(
            200,
            json={
                "pages": [
                    {
                        "index": 2,
                        "markdown": (
                            "# Rechnung\n\n[tbl-0.md](tbl-0.md)\n\n"
                            "![img-0.jpeg](img-0.jpeg)\n\nZahlbar sofort."
                        ),
                        "tables": [{"id": "tbl-0.md", "content": TABLE, "format": "markdown"}],
                    }
                ],
                "model": "mistral-ocr-4-1",
            },
        )

    ocr = provider(MistralOcrProvider, handler)
    assert ocr.profile == "mistral-ocr-4-1"
    assert ocr.recognize_page(PDF, 3) == [
        Block(3, "text", "Rechnung"),
        Block(3, "table", TABLE),
        Block(3, "text", "Zahlbar sofort."),
    ]


@pytest.mark.parametrize("page", [0, -1, True, 201])
def test_ocr_rejects_invalid_page_locally(page):
    ocr = provider(MistralOcrProvider, lambda _: pytest.fail("unexpected request"))
    with pytest.raises(TerminalExtractionError):
        ocr.recognize_page(PDF, page)


@pytest.mark.parametrize("pages", [[], [{"index": 0, "markdown": "Wrong page"}]])
def test_ocr_rejects_missing_or_wrong_page(pages):
    ocr = provider(MistralOcrProvider, lambda _: httpx.Response(200, json={"pages": pages}))
    with pytest.raises(ExtractionError, match="OCR_INVALID_RESPONSE") as error:
        ocr.recognize_page(PDF, 3)
    assert error.value.retryable


def test_ocr_does_not_treat_blank_page_as_provider_failure():
    response = {"pages": [{"index": 0, "markdown": "", "tables": []}]}
    assert (
        provider(MistralOcrProvider, lambda _: httpx.Response(200, json=response)).recognize_page(
            PDF, 1
        )
        == []
    )


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "OCR_UNAVAILABLE", True),
        (413, "SOURCE_LIMIT_EXCEEDED", False),
        (422, "OCR_REJECTED", False),
        (500, "OCR_UNAVAILABLE", True),
    ],
)
def test_ocr_maps_http_errors_without_provider_payload(status, code, retryable):
    ocr = provider(MistralOcrProvider, lambda _: httpx.Response(status, text="private failure"))
    with pytest.raises(ExtractionError, match=f"^{code}$") as error:
        ocr.recognize_page(PDF, 1)
    assert error.value.retryable is retryable


def test_ocr_rate_limit_carries_retry_after_without_http_retry():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "17"}, text=SECRET)

    with pytest.raises(ExtractionError, match="^OCR_RATE_LIMITED$") as error:
        provider(MistralOcrProvider, handler).recognize_page(PDF, 1)
    assert error.value.retry_after == 17
    assert len(requests) == 1


def test_embeddings_batch_split_and_restore_input_order():
    batches = []

    def handler(request):
        assert str(request.url) == "https://mistral.invalid/v1/embeddings"
        body = json.loads(request.content)
        assert body["model"] == "mistral-embed"
        assert body["encoding_format"] == "float"
        inputs = body["input"]
        batches.append(inputs)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [float(item[0])] * 1024}
                    for index, item in reversed(list(enumerate(inputs)))
                ],
                "model": "mistral-embed",
            },
        )

    items = [str(index) * 6000 for index in range(4)]
    embeddings = provider(MistralEmbeddingProvider, handler)
    assert embeddings.embed([]) == []
    assert embeddings.embed(items) == [[float(i)] * 1024 for i in range(4)]
    assert batches == [items[:2], items[2:]]


@pytest.mark.parametrize(
    "data",
    [
        [],
        [{"index": 1, "embedding": [0.5] * 1024}],
        [{"index": True, "embedding": [0.5] * 1024}],
        [{"index": 0, "embedding": [0.5] * 1023}],
        [{"index": 0, "embedding": ["0.5"] * 1024}],
        [{"index": 0, "embedding": [True] * 1024}],
        [{"index": 0, "embedding": [0] * 1023 + [10**400]}],
    ],
)
def test_embeddings_reject_invalid_vectors(data):
    embeddings = provider(
        MistralEmbeddingProvider, lambda _: httpx.Response(200, json={"data": data})
    )
    with pytest.raises(MistralError, match="^MISTRAL_INVALID_RESPONSE$"):
        embeddings.embed(["text"])


def test_embeddings_reject_nonfinite_numbers():
    body = json.dumps({"data": [{"index": 0, "embedding": [float("inf")] * 1024}]})
    embeddings = provider(MistralEmbeddingProvider, lambda _: httpx.Response(200, content=body))
    with pytest.raises(MistralError, match="^MISTRAL_INVALID_RESPONSE$"):
        embeddings.embed(["text"])


def test_embeddings_reject_input_that_cannot_fit_one_request():
    embeddings = provider(MistralEmbeddingProvider, lambda _: pytest.fail("unexpected request"))
    with pytest.raises(MistralError, match="^MISTRAL_INPUT_LIMIT_EXCEEDED$"):
        embeddings.embed(["valid text", "ä" * 7501])


def test_http_timeout_is_safe(caplog):
    def handler(request):
        assert request.extensions["timeout"]["read"] == 0.2
        raise httpx.ReadTimeout(f"private body with {SECRET}", request=request)

    with pytest.raises(MistralError, match="^MISTRAL_TIMEOUT$") as error:
        provider(MistralEmbeddingProvider, handler, timeout_seconds=0.2).embed(["private text"])
    assert error.value.retryable
    assert error.value.__suppress_context__
    assert SECRET not in caplog.text
    assert "private text" not in caplog.text


def test_explicit_endpoint_never_follows_redirect():
    requests = []

    def handler(request):
        requests.append(request.url.host)
        return httpx.Response(307, headers={"Location": "https://other.invalid/embeddings"})

    with pytest.raises(MistralError, match="^MISTRAL_HTTP_ERROR$"):
        provider(MistralEmbeddingProvider, handler).embed(["text"])
    assert requests == ["mistral.invalid"]


@pytest.mark.parametrize("endpoint", ["", "/v1", "http://mistral.invalid", "https://a:b@host/v1"])
def test_requires_valid_explicit_https_endpoint(endpoint):
    with pytest.raises(ValueError, match="endpoint"):
        MistralEmbeddingProvider(endpoint=endpoint, api_key=SECRET)


def test_chat_returns_a_packet_accepted_by_the_citation_validator():
    async def handler(request):
        assert str(request.url) == "https://mistral.invalid/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "mistral-small-2603"
        assert body["stream"] is False
        assert body["reasoning_effort"] == "none"
        assert body["response_format"] == {"type": "json_object"}
        assert "private-" not in request.content.decode()
        prompt = json.loads(body["messages"][1]["content"])
        assert prompt["question"] == QUESTION
        assert prompt["evidence"][1]["text"] == TABLE
        return chat_reply(
            {"evidence_id": prompt["evidence"][1]["evidence_id"], "quote": "42,00 EUR"}
        )

    packet = asyncio.run(provider(MistralChatProvider, handler).generate(QUESTION, CONTEXT))
    assert packet == AnswerPacket("private-table", "42,00 EUR")
    assert validate_answer(QUESTION, CONTEXT, packet).evidence == CONTEXT[1]


def chat_reply(answer, *, finish_reason="stop"):
    return httpx.Response(
        200,
        json={
            "model": "mistral-small-2603",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(answer)},
                    "finish_reason": finish_reason,
                }
            ],
        },
    )


@pytest.mark.parametrize(
    "answer",
    [
        {"evidence_id": "5", "quote": "42,00 EUR"},
        {"evidence_id": "2", "quote": "999,00 EUR"},
        {"evidence_id": None, "quote": "42,00 EUR"},
        {"evidence_id": 2, "quote": "42,00 EUR"},
        {"evidence_id": "2", "quote": "42,00 EUR", "extra": "untrusted"},
    ],
)
def test_chat_rejects_invalid_packets(answer):
    with pytest.raises(MistralError, match="^MISTRAL_INVALID_RESPONSE$"):
        asyncio.run(
            provider(MistralChatProvider, lambda _: chat_reply(answer)).generate(QUESTION, CONTEXT)
        )


def test_chat_accepts_structured_abstention():
    answer = {"evidence_id": None, "quote": None}
    result = asyncio.run(
        provider(MistralChatProvider, lambda _: chat_reply(answer)).generate(QUESTION, CONTEXT)
    )
    assert result is None


def test_chat_rejects_truncated_output():
    answer = {"evidence_id": "2", "quote": "42,00 EUR"}
    chat = provider(MistralChatProvider, lambda _: chat_reply(answer, finish_reason="length"))
    with pytest.raises(MistralError, match="^MISTRAL_INVALID_RESPONSE$"):
        asyncio.run(chat.generate(QUESTION, CONTEXT))


def test_chat_deadline_bounds_async_transport():
    async def handler(_):
        await asyncio.sleep(10)
        pytest.fail("transport was not cancelled")

    chat = provider(MistralChatProvider, handler, timeout_seconds=0.05)
    with pytest.raises(MistralError, match="^MISTRAL_TIMEOUT$"):
        asyncio.run(chat.generate(QUESTION, CONTEXT))


def test_ocr_markdown_heading_is_visible_to_amount_verifier():
    response = {
        "pages": [
            {
                "index": 0,
                "markdown": "## Rechnung RE-2026-123 ##\n\n[tbl-0.md](tbl-0.md)",
                "tables": [{"id": "tbl-0.md", "content": TABLE}],
            }
        ]
    }
    ocr = provider(MistralOcrProvider, lambda _: httpx.Response(200, json=response))
    evidence = tuple(
        Evidence(Dependency(SOURCE, 1, str(i)), block.page, block.kind, block.text)
        for i, block in enumerate(ocr.recognize_page(PDF, 1))
    )
    answer = supported_amount(QUESTION, evidence)
    assert answer is not None
    assert answer.amount == "42,00 EUR"


def test_ocr_rejects_oversized_source_without_contacting_provider():
    ocr = provider(MistralOcrProvider, lambda _: pytest.fail("unexpected request"))
    with pytest.raises(TerminalExtractionError, match="^SOURCE_LIMIT_EXCEEDED$"):
        ocr.recognize_page(PDF.ljust(MAX_SOURCE_BYTES + 1, b" "), 1)


def test_ocr_rejects_oversized_provider_response():
    oversized = b"x" * (2 * 1024 * 1024 + 1)
    ocr = provider(MistralOcrProvider, lambda _: httpx.Response(200, content=oversized))
    with pytest.raises(ExtractionError, match="^OCR_RESPONSE_LIMIT_EXCEEDED$"):
        ocr.recognize_page(PDF, 1)


def test_embedding_rate_limit_performs_no_additional_batch_requests():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "23"}, text="private failure")

    embeddings = provider(MistralEmbeddingProvider, handler)
    with pytest.raises(MistralError, match="^MISTRAL_RATE_LIMITED$") as error:
        embeddings.embed(["1" * 7600, "2" * 7600])
    assert error.value.status == 429
    assert error.value.retryable
    assert error.value.retry_after == 23
    assert len(requests) == 1


def test_chat_propagates_caller_cancellation():
    async def generate():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def handler(_):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        chat = provider(MistralChatProvider, handler)
        task = asyncio.create_task(chat.generate(QUESTION, CONTEXT))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()

    asyncio.run(generate())
