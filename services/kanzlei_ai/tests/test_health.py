"""Health endpoints must distinguish a live process from usable dependencies."""

import asyncio
from time import monotonic

import httpx
import pytest

from kanzlei_ai.app import create_app

DEPENDENCIES = ("postgres", "qdrant", "redis")


async def request(app, path):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path)


async def available():
    return None


def test_liveness_does_not_contact_dependencies():
    contacted = []

    async def unavailable():
        contacted.append(True)
        raise RuntimeError("not running")

    response = asyncio.run(
        request(create_app(probes=dict.fromkeys(DEPENDENCIES, unavailable)), "/health/live")
    )

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
    assert response.headers["cache-control"] == "no-store"
    assert contacted == []


def test_readiness_checks_every_dependency():
    contacted = []

    def probe(name):
        async def check():
            contacted.append(name)

        return check

    response = asyncio.run(
        request(create_app(probes={name: probe(name) for name in DEPENDENCIES}), "/health/ready")
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": dict.fromkeys(DEPENDENCIES, "ok"),
    }
    assert response.headers["cache-control"] == "no-store"
    assert sorted(contacted) == sorted(DEPENDENCIES)


@pytest.mark.parametrize("failed_dependency", DEPENDENCIES)
def test_readiness_reports_unavailable_dependencies_without_private_errors(failed_dependency):
    async def unavailable():
        raise RuntimeError("postgresql://private-user:private-password@internal.example/private")

    probes = dict.fromkeys(DEPENDENCIES, available)
    probes[failed_dependency] = unavailable
    response = asyncio.run(request(create_app(probes=probes), "/health/ready"))

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {
            name: "unavailable" if name == failed_dependency else "ok" for name in DEPENDENCIES
        },
    }
    assert "private" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_readiness_cancels_a_hung_probe_within_its_deadline():
    cancelled = []

    async def hangs():
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)

    probes = dict.fromkeys(DEPENDENCIES, available)
    probes["qdrant"] = hangs
    started = monotonic()
    response = asyncio.run(
        request(create_app(probes=probes, timeout_seconds=0.03), "/health/ready")
    )
    elapsed = monotonic() - started

    assert response.status_code == 503
    assert response.json()["checks"] == {"postgres": "ok", "qdrant": "unavailable", "redis": "ok"}
    assert elapsed < 1
    assert cancelled == [True]


def test_readiness_runs_probes_concurrently():
    started = set()
    everyone_started = None

    def probe(name):
        async def check():
            nonlocal everyone_started
            if everyone_started is None:
                everyone_started = asyncio.Event()
            started.add(name)
            if len(started) == len(DEPENDENCIES):
                everyone_started.set()
            await everyone_started.wait()

        return check

    probes = {name: probe(name) for name in DEPENDENCIES}
    response = asyncio.run(request(create_app(probes=probes, timeout_seconds=0.1), "/health/ready"))

    assert response.status_code == 200
