"""Dependency-independent liveness and bounded, concurrent readiness checks."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from kanzlei_ai.health import dependency_probes
from kanzlei_ai.settings import Settings

Probe = Callable[[], Awaitable[None]]


def create_app(
    *,
    settings: Settings | None = None,
    probes: Mapping[str, Probe] | None = None,
    timeout_seconds: float | None = None,
) -> FastAPI:
    settings = settings or Settings()
    checks = dependency_probes(settings) if probes is None else probes
    timeout = settings.health_timeout_seconds if timeout_seconds is None else timeout_seconds
    app = FastAPI(title="Kanzlei AI", version="0.1.0")

    @app.get("/health/live")
    async def live() -> JSONResponse:
        return JSONResponse({"status": "alive"}, headers={"Cache-Control": "no-store"})

    async def check(name: str, probe: Probe) -> tuple[str, str]:
        try:
            async with asyncio.timeout(timeout):
                await probe()
        except Exception:
            # Driver exceptions can include credentials, hostnames and private paths.
            return name, "unavailable"
        return name, "ok"

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        results = dict(
            await asyncio.gather(*(check(name, probe) for name, probe in checks.items()))
        )
        available = all(result == "ok" for result in results.values())
        return JSONResponse(
            {"status": "ready" if available else "not_ready", "checks": results},
            status_code=200 if available else 503,
            headers={"Cache-Control": "no-store"},
        )

    return app
