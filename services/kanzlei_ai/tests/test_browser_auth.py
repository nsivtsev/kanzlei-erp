"""Verify the fixed AI browser handoff boundary without contacting a live ERP."""

import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from kanzlei_ai.app import create_app
from kanzlei_ai.browser_auth import router
from kanzlei_ai.connectors.frappe import FrappeConnector

ERP_ORIGIN = "https://erp.example.invalid"
CHAT_ORIGIN = "https://chat.example.invalid"
PAYLOAD = {"version": 1, "ticket": "t" * 43, "state": "s" * 43, "verifier": "v" * 43}


def browser_app(handler, *, origin=ERP_ORIGIN):
    adapter = FrappeConnector(
        ERP_ORIGIN, SecretStr("k" * 43), transport=httpx.MockTransport(handler)
    )
    app = create_app(connector=adapter)
    app.state.chat_browser_erp_origin = origin
    app.include_router(router)
    return app


def test_unconfigured_application_does_not_mount_browser_routes():
    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url=CHAT_ORIGIN
        ) as client:
            assert (await client.get("/v1/auth/browser/config")).status_code == 404
            assert (await client.post("/v1/auth/browser/exchange", json=PAYLOAD)).status_code == 404

    asyncio.run(check())


def test_browser_config_and_binding_use_the_browser_only_rpc():
    def handler(request):
        assert request.url.path.endswith(".exchange_browser_ticket")
        assert json.loads(request.content) == {**PAYLOAD, "chat_origin": CHAT_ORIGIN}, (
            "Incorrect ERP exchange fields"
        )
        assert "Cookie" not in request.headers
        return httpx.Response(
            200,
            json={
                "message": {
                    "version": 1,
                    "access_token": "g" * 43,
                    "token_type": "bearer",
                    "identity": {
                        "version": 1,
                        "subject": "staff@example.invalid",
                        "connector_id": "fixture",
                        "expires_at": 1,
                        "general_access": False,
                    },
                }
            },
        )

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=browser_app(handler)), base_url=CHAT_ORIGIN
        ) as client:
            config = await client.get("/v1/auth/browser/config")
            assert config.status_code == 200
            assert config.json() == {"version": 1, "erp_origin": ERP_ORIGIN}
            assert "no-store" in config.headers["cache-control"]
            exchange = await client.post(
                "/v1/auth/browser/exchange", json=PAYLOAD, headers={"Origin": CHAT_ORIGIN}
            )
            assert exchange.status_code == 200
            assert "no-store" in exchange.headers["cache-control"]
            assert exchange.json()["token_type"] == "bearer"
            assert len(exchange.json()["access_token"]) == 43

    asyncio.run(check())


@pytest.mark.parametrize(
    ("payload", "origin", "settings_origin", "status", "code"),
    [
        ({"version": 1}, CHAT_ORIGIN, ERP_ORIGIN, 422, "INVALID_REQUEST"),
        ({**PAYLOAD, "state": "!"}, CHAT_ORIGIN, ERP_ORIGIN, 422, "INVALID_REQUEST"),
        ({**PAYLOAD, "chat_origin": CHAT_ORIGIN}, CHAT_ORIGIN, ERP_ORIGIN, 422, "INVALID_REQUEST"),
        (
            {k: v for k, v in PAYLOAD.items() if k != "version"},
            CHAT_ORIGIN,
            ERP_ORIGIN,
            422,
            "INVALID_REQUEST",
        ),
        (PAYLOAD, None, ERP_ORIGIN, 401, "UNAUTHENTICATED"),
        (PAYLOAD, "null", ERP_ORIGIN, 401, "UNAUTHENTICATED"),
        (PAYLOAD, CHAT_ORIGIN, None, 503, "CONNECTOR_UNAVAILABLE"),
    ],
    ids=["missing", "malformed", "extra", "missing-version", "no-origin", "opaque", "disabled"],
)
def test_browser_exchange_fails_without_echoing_input(
    payload, origin, settings_origin, status, code
):
    def handler(_request):
        pytest.fail("Invalid browser request reached ERP")

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=browser_app(handler, origin=settings_origin)),
            base_url=CHAT_ORIGIN,
        ) as client:
            response = await client.post(
                "/v1/auth/browser/exchange",
                json=payload,
                headers={"Origin": origin} if origin else {},
            )
            assert response.status_code == status
            assert response.json() == {"code": code}
            assert "no-store" in response.headers["cache-control"]

    asyncio.run(check())
