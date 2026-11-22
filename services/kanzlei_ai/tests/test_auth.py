"""The browser cannot choose an ERP user; all access uses the online connector."""

import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from kanzlei_ai.app import create_app
from kanzlei_ai.connectors.frappe import FrappeConnector


def test_unconfigured_connector_fails_closed_without_breaking_liveness():
    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
        ) as c:
            auth = await c.post("/v1/auth/exchange", json={"ticket": "x" * 43})
            live = await c.get("/health/live")
            assert auth.status_code == 503
            assert auth.json() == {"code": "CONNECTOR_UNAVAILABLE"}
            assert live.status_code == 200

    asyncio.run(request())


def test_empty_optional_erp_environment_keeps_the_connector_disabled(monkeypatch):
    monkeypatch.setenv("AI_ERP_BASE_URL", "")
    monkeypatch.setenv("AI_ERP_SERVICE_KEY", "")
    assert create_app().state.erp_connector is None


@pytest.mark.parametrize(
    "payload",
    [
        {"ticket": "x" * 43, "user": "Administrator"},
        {"ticket": "x" * 43, "role": "System Manager"},
    ],
)
def test_exchange_rejects_browser_identity_fields(payload):
    def forbidden_network(request):
        raise AssertionError("Invalid browser identity must never reach ERP")

    adapter = FrappeConnector(
        "https://erp.example.invalid",
        SecretStr("s" * 43),
        transport=httpx.MockTransport(forbidden_network),
    )

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(connector=adapter)), base_url="http://test"
        ) as c:
            response = await c.post("/v1/auth/exchange", json=payload)
            assert response.status_code == 422
            assert "Administrator" not in response.text

    asyncio.run(request())


def test_authenticated_http_flow_forwards_only_the_verified_grant():
    identity = {
        "version": 1,
        "subject": "staff@example.invalid",
        "connector_id": "fixture-erp",
        "expires_at": 9999999999,
        "general_access": False,
    }

    def handler(request):
        if request.url.path.endswith("exchange_ticket"):
            return httpx.Response(
                200,
                json={
                    "message": {
                        "version": 1,
                        "access_token": "g" * 43,
                        "token_type": "bearer",
                        "identity": identity,
                    }
                },
            )
        assert json.loads(request.content)["access_token"] == "g" * 43
        return httpx.Response(200, json={"message": identity})

    adapter = FrappeConnector(
        "https://erp.example.invalid", SecretStr("s" * 43), transport=httpx.MockTransport(handler)
    )

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(connector=adapter)), base_url="http://test"
        ) as c:
            exchange = await c.post("/v1/auth/exchange", json={"ticket": "t" * 43})
            assert exchange.status_code == 200
            assert exchange.json()["access_token"] == "g" * 43
            me = await c.get("/v1/auth/me", headers={"Authorization": "Bearer " + "g" * 43})
            assert me.json()["subject"] == identity["subject"]
            assert me.headers["cache-control"] == "private, no-store"
            assert (await c.get("/v1/auth/me")).status_code == 401

    asyncio.run(request())
