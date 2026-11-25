"""Only an explicit and well-formed ERP grant permits external processing."""

import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from kanzlei_ai.connectors.base import ConnectorError
from kanzlei_ai.connectors.contracts import Scope, SourceRef
from kanzlei_ai.connectors.frappe import FrappeConnector

SCOPE = Scope(kind="client", client_id="fixture")
SOURCE = SourceRef(client_id="fixture", source_id="file:document", revision="a" * 64)


def connector(handler):
    return FrappeConnector(
        "https://erp.example.invalid",
        SecretStr("synthetic-policy-key-" + "s" * 32),
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize("delegated", [False, True])
def test_processing_check_sends_only_policy_metadata(delegated):
    operation = "embed" if delegated else "ocr"
    token = "g" * 43 if delegated else None
    sources = [] if delegated else [SOURCE]

    def handle(request):
        assert request.method == "POST"
        assert request.url.path == (
            "/api/method/kanzlei_erp.ai_connector.api.check_processing_policy"
        )
        expected = {
            "version": 1,
            "operation": operation,
            "scope": {"version": 1, "kind": "client", "client_id": "fixture"},
            "sources": [source.model_dump() for source in sources],
        }
        if delegated:
            expected["access_token"] = token
        assert json.loads(request.content) == expected
        return httpx.Response(200, json={"message": {"version": 1, "allowed": True}})

    assert (
        asyncio.run(
            connector(handle).check_processing_policy(operation, SCOPE, sources, access_token=token)
        )
        is None
    )


@pytest.mark.parametrize(
    "message",
    [
        {"version": 1, "allowed": False},
        {"version": 1, "allowed": 1},
        {"version": True, "allowed": True},
        {"allowed": True},
        {"version": 1, "allowed": True, "confirmation": "unexpected"},
    ],
)
def test_processing_check_rejects_missing_or_unexpected_permission(message):
    adapter = connector(lambda _: httpx.Response(200, json={"message": message}))
    with pytest.raises(ConnectorError):
        asyncio.run(adapter.check_processing_policy("embed", SCOPE, [SOURCE]))


def test_processing_check_preserves_an_explicit_erp_denial():
    adapter = connector(lambda _: httpx.Response(403, text="private ERP details"))
    with pytest.raises(ConnectorError, match="FORBIDDEN") as error:
        asyncio.run(
            adapter.check_processing_policy("generate", SCOPE, [SOURCE], access_token="g" * 43)
        )
    assert error.value.status == 403


def test_assistant_question_check_sends_no_scope_or_sources():
    def handle(request):
        assert json.loads(request.content) == {
            "version": 1,
            "operation": "assist",
            "sources": [],
            "access_token": "g" * 43,
        }
        return httpx.Response(200, json={"message": {"version": 1, "allowed": True}})

    asyncio.run(
        connector(handle).check_processing_policy("assist", None, [], access_token="g" * 43)
    )


@pytest.mark.parametrize(("operation", "scope"), [("assist", SCOPE), ("generate", None)])
def test_only_the_assistant_question_check_omits_scope(operation, scope):
    def handle(request):
        raise AssertionError("no request expected")

    with pytest.raises(ConnectorError, match="INVALID_REQUEST"):
        asyncio.run(connector(handle).check_processing_policy(operation, scope, []))
