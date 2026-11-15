"""Dependency-independent liveness and bounded, concurrent readiness checks."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from kanzlei_ai.auth import router
from kanzlei_ai.connectors.base import ConnectorError, ERPConnector
from kanzlei_ai.connectors.frappe import FrappeConnector
from kanzlei_ai.health import dependency_probes
from kanzlei_ai.settings import Settings

Probe = Callable[[], Awaitable[None]]


def create_app(
    *,
    settings: Settings | None = None,
    probes: Mapping[str, Probe] | None = None,
    timeout_seconds: float | None = None,
    connector: ERPConnector | None = None,
) -> FastAPI:
    settings = settings or Settings()
    checks = dependency_probes(settings) if probes is None else probes
    timeout = settings.health_timeout_seconds if timeout_seconds is None else timeout_seconds
    app = FastAPI(title="Kanzlei AI", version="0.1.0")
    if connector is None and settings.erp_base_url and settings.erp_service_key:
        connector = FrappeConnector(
            str(settings.erp_base_url),
            settings.erp_service_key,
            allow_insecure_http=settings.erp_allow_insecure_http,
            site_name=settings.erp_site_name,
            timeout_seconds=settings.erp_timeout_seconds,
        )
    app.state.erp_connector = connector
    app.include_router(router)

    @app.exception_handler(ConnectorError)
    async def connector_error(request: Request, error: ConnectorError) -> JSONResponse:
        return JSONResponse(
            {"code": error.code},
            status_code=error.status,
            headers={"Cache-Control": "private, no-store"},
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # Pydantic's default details include rejected input, potentially a token.
        return JSONResponse(
            {"code": "INVALID_REQUEST"},
            status_code=422,
            headers={"Cache-Control": "private, no-store"},
        )

    @app.middleware("http")
    async def private_connector_responses(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

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
