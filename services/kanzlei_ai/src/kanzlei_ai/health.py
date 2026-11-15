"""Small real dependency probes; no dependency is contacted during app startup."""

import math
from collections.abc import Awaitable, Callable

import httpx
from psycopg import AsyncConnection
from redis.asyncio import Redis

from kanzlei_ai.settings import Settings


def dependency_probes(settings: Settings) -> dict[str, Callable[[], Awaitable[None]]]:
    async def postgres() -> None:
        async with await AsyncConnection.connect(
            settings.database_url.get_secret_value(),
            password=(
                settings.database_password.get_secret_value()
                if settings.database_password
                else None
            ),
            connect_timeout=max(1, math.ceil(settings.health_timeout_seconds)),
        ) as connection:
            await connection.execute("SELECT 1")

    async def qdrant() -> None:
        headers = {}
        if settings.qdrant_api_key:
            headers["api-key"] = settings.qdrant_api_key.get_secret_value()
        async with httpx.AsyncClient(
            timeout=settings.health_timeout_seconds, trust_env=False
        ) as client:
            response = await client.get(
                f"{str(settings.qdrant_url).rstrip('/')}/readyz", headers=headers
            )
            response.raise_for_status()

    async def redis() -> None:
        async with Redis.from_url(
            settings.redis_url.get_secret_value(),
            socket_connect_timeout=settings.health_timeout_seconds,
            socket_timeout=settings.health_timeout_seconds,
        ) as client:
            await client.ping()

    return {"postgres": postgres, "qdrant": qdrant, "redis": redis}
