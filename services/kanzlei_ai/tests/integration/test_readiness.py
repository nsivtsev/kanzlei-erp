"""Opt-in check of actual driver connectivity, including configured credentials."""

import asyncio

import httpx
import pytest

from kanzlei_ai.app import create_app


@pytest.mark.integration
def test_real_dependency_readiness():
    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            return await client.get("/health/ready")

    response = asyncio.run(request())
    assert response.status_code == 200, response.json()
    assert response.json() == {
        "status": "ready",
        "checks": {"postgres": "ok", "qdrant": "ok", "redis": "ok"},
    }
