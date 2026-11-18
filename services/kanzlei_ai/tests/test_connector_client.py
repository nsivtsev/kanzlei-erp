"""Exercise the HTTP boundary with complete synthetic wire responses."""

import asyncio
import hashlib
import json

import httpx
import pytest
from pydantic import SecretStr

from kanzlei_ai.connectors import frappe


def client(handler, **kwargs):
    assert hasattr(frappe, "FrappeConnector"), "ERP HTTP connector not implemented"
    return frappe.FrappeConnector(
        "https://erp.example.invalid",
        SecretStr("synthetic-service-key-" + "s" * 32),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


IDENTITY = {
    "version": 1,
    "subject": "staff@example.invalid",
    "connector_id": "fixture-erp",
    "expires_at": 9999999999,
    "general_access": False,
}


def test_exchange_uses_fixed_endpoint_and_private_service_credential():
    def handler(request):
        assert request.url.path == "/api/method/kanzlei_erp.ai_connector.api.exchange_ticket"
        assert request.headers["X-Kanzlei-AI-Service-Key"].startswith("synthetic-service-key-")
        assert "cookie" not in request.headers
        assert json.loads(request.content) == {"version": 1, "ticket": "t" * 43}
        return httpx.Response(
            200,
            json={
                "message": {
                    "version": 1,
                    "access_token": "g" * 43,
                    "token_type": "bearer",
                    "identity": IDENTITY,
                }
            },
        )

    result = asyncio.run(client(handler).exchange_ticket("t" * 43))
    assert result.identity.subject == IDENTITY["subject"]
    assert "g" * 43 not in repr(result)


@pytest.mark.parametrize("status", [302, 401, 403, 409, 500])
def test_error_responses_are_closed_and_do_not_disclose_erp_body(status):
    adapter = client(lambda request: httpx.Response(status, text="private traceback and password"))
    with pytest.raises(Exception) as result:
        asyncio.run(adapter.identity("g" * 43))
    assert type(result.value).__name__ == "ConnectorError"
    assert "private" not in str(result.value)


def test_incompatible_identity_response_is_rejected():
    adapter = client(
        lambda request: httpx.Response(200, json={"message": {**IDENTITY, "version": 2}})
    )
    with pytest.raises(Exception) as result:
        asyncio.run(adapter.identity("g" * 43))
    assert type(result.value).__name__ == "ConnectorError"


def test_binary_read_checks_revision_and_checksum():
    from kanzlei_ai.connectors import contracts

    assert hasattr(contracts, "SourceRef"), "SourceRef missing"
    reference = contracts.SourceRef(source_id="file:abc", revision="a" * 64, client_id="A")
    scope = contracts.Scope(kind="client", client_id="A")
    content = b"synthetic private PDF"

    def handler(request):
        return httpx.Response(
            200,
            content=content,
            headers={
                "X-AI-Revision": "a" * 64,
                "X-AI-Content-SHA256": hashlib.sha256(content).hexdigest(),
            },
        )

    assert asyncio.run(client(handler).read_source("g" * 43, scope, reference)) == content
    bad = client(
        lambda request: httpx.Response(
            200,
            content=content,
            headers={
                "X-AI-Revision": "b" * 64,
                "X-AI-Content-SHA256": hashlib.sha256(content).hexdigest(),
            },
        )
    )
    with pytest.raises(Exception) as result:
        asyncio.run(bad.read_source("g" * 43, scope, reference))
    assert type(result.value).__name__ == "ConnectorError"


def test_background_binary_read_has_no_browser_grant_and_keeps_revision_check():
    from kanzlei_ai.connectors.contracts import SourceRef

    reference = SourceRef(source_id="file:abc", revision="a" * 64, client_id="A")
    content = b"synthetic ingestion PDF"

    def handler(request):
        assert request.url.path.endswith(".background_read_source")
        assert json.loads(request.content) == {"version": 1, "source": reference.model_dump()}
        assert b"access_token" not in request.content
        return httpx.Response(
            200,
            content=content,
            headers={
                "X-AI-Revision": reference.revision,
                "X-AI-Content-SHA256": hashlib.sha256(content).hexdigest(),
            },
        )

    assert asyncio.run(client(handler).read_background_source(reference)) == content


def test_json_responses_are_bounded():
    adapter = client(lambda request: httpx.Response(200, content=b"x" * 2048), max_json_bytes=1024)
    with pytest.raises(Exception) as result:
        asyncio.run(adapter.identity("g" * 43))
    assert type(result.value).__name__ == "ConnectorError"


def test_plain_http_requires_explicit_local_development_setting():
    assert hasattr(frappe, "FrappeConnector"), "ERP HTTP connector not implemented"
    with pytest.raises(ValueError):
        frappe.FrappeConnector("http://erp.example.invalid", SecretStr("s" * 43))


def test_slow_erp_request_has_a_whole_operation_deadline():
    async def handler(request):
        await asyncio.sleep(10)
        return httpx.Response(200, json={"message": IDENTITY})

    adapter = client(handler, timeout_seconds=0.02)
    with pytest.raises(Exception) as result:
        asyncio.run(adapter.identity("g" * 43))
    assert type(result.value).__name__ == "ConnectorError"


def test_binary_response_size_is_bounded_even_with_a_valid_checksum():
    from kanzlei_ai.connectors.contracts import Scope, SourceRef

    content = b"oversized"
    adapter = client(
        lambda request: httpx.Response(
            200,
            content=content,
            headers={
                "X-AI-Revision": "a" * 64,
                "X-AI-Content-SHA256": hashlib.sha256(content).hexdigest(),
            },
        ),
        max_source_bytes=2,
    )
    with pytest.raises(Exception) as result:
        asyncio.run(
            adapter.read_source(
                "g" * 43,
                Scope(kind="client", client_id="A"),
                SourceRef(source_id="file:abc", revision="a" * 64, client_id="A"),
            )
        )
    assert type(result.value).__name__ == "ConnectorError"
    assert result.value.status == 413
