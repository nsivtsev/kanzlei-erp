"""HTTP request and sanitized error handling shared by the Mistral adapters."""

import asyncio
import json
import math
import time
from contextlib import asynccontextmanager, contextmanager, nullcontext
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError


class MistralError(RuntimeError):
    """Safe to log. Retry scheduling belongs to the calling service."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = True,
        status: int | None = None,
        retry_after: int | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.status = status
        self.retry_after = retry_after


class MistralRequestGate:
    """One in-flight request across the processes sharing the AI broker."""

    _LOCK = "kanzlei-ai:mistral:request"
    _COOLDOWN = "kanzlei-ai:mistral:cooldown"

    def __init__(self, redis_url: str):
        self._redis_url = redis_url

    @contextmanager
    def request(self):
        try:
            with (
                Redis.from_url(
                    self._redis_url, socket_connect_timeout=2, socket_timeout=2
                ) as broker,
                broker.lock(self._LOCK, timeout=120, blocking=False),
            ):
                remaining = broker.ttl(self._COOLDOWN)
                if remaining >= 0:
                    raise MistralError("MISTRAL_RATE_LIMITED", status=429, retry_after=remaining)
                try:
                    yield
                except MistralError as error:
                    if error.status == 429:
                        broker.set(self._COOLDOWN, "1", ex=max(1, error.retry_after or 1))
                    raise
        except RedisError:
            raise MistralError("MISTRAL_REQUEST_GATE_UNAVAILABLE") from None

    @asynccontextmanager
    async def request_async(self):
        try:
            async with (
                AsyncRedis.from_url(
                    self._redis_url, socket_connect_timeout=2, socket_timeout=2
                ) as broker,
                broker.lock(self._LOCK, timeout=120, blocking=False),
            ):
                remaining = await broker.ttl(self._COOLDOWN)
                if remaining >= 0:
                    raise MistralError("MISTRAL_RATE_LIMITED", status=429, retry_after=remaining)
                try:
                    yield
                except MistralError as error:
                    if error.status == 429:
                        await broker.set(self._COOLDOWN, "1", ex=max(1, error.retry_after or 1))
                    raise
        except RedisError:
            raise MistralError("MISTRAL_REQUEST_GATE_UNAVAILABLE") from None


def _retry_after(value: str) -> int | None:
    value = value.strip()
    # Limit accepted cooldown hints to one day.
    if value.isascii() and value.isdecimal():
        return min(int(value), 86400) if len(value) <= 10 else None
    try:
        date = parsedate_to_datetime(value)
        if date is not None and date.tzinfo is not None:
            seconds = math.ceil((date - datetime.now(UTC)).total_seconds())
            return min(max(seconds, 0), 86400)
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _check_response(response: httpx.Response) -> None:
    status = response.status_code
    if status == 429:
        raise MistralError(
            "MISTRAL_RATE_LIMITED",
            status=status,
            retry_after=_retry_after(response.headers.get("Retry-After", "")),
        )
    if not response.is_success:
        raise MistralError(
            "MISTRAL_HTTP_ERROR",
            status=status,
            retryable=status >= 500 or status == 408,
        )
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise MistralError("MISTRAL_INVALID_RESPONSE")


def _extend_body(body: bytearray, chunk: bytes, limit: int) -> None:
    if len(body) + len(chunk) > limit:
        raise MistralError("MISTRAL_RESPONSE_LIMIT_EXCEEDED")
    body.extend(chunk)


def _json_object(body: str | bytearray) -> dict:
    try:
        value = json.loads(body)
    except (ValueError, RecursionError):
        raise MistralError("MISTRAL_INVALID_RESPONSE") from None
    if not isinstance(value, dict):
        raise MistralError("MISTRAL_INVALID_RESPONSE")
    return value


class _MistralEndpoint:
    def __init__(
        self, endpoint: str, api_key: str, timeout_seconds: float, gate: MistralRequestGate | None
    ):
        try:
            url = httpx.URL(endpoint)
        except httpx.InvalidURL:
            raise ValueError("Invalid Mistral endpoint") from None
        if (
            url.scheme != "https"
            or not url.host
            or url.userinfo
            or "?" in endpoint
            or "#" in endpoint
        ):
            raise ValueError("Invalid Mistral endpoint")
        if not api_key or any(not 33 <= ord(char) <= 126 for char in api_key):
            raise ValueError("Invalid Mistral API key")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Invalid Mistral timeout")
        base = str(url).rstrip("/")
        self.base = base if base.endswith("/v1") else f"{base}/v1"
        self.timeout = timeout_seconds
        self.gate = gate
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }

    def client(self, transport: httpx.BaseTransport | None) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout, transport=transport, trust_env=False, follow_redirects=False
        )

    def post(self, client: httpx.Client, path: str, payload: dict, *, limit: int) -> dict:
        body = bytearray()
        deadline = time.monotonic() + self.timeout
        try:
            with (
                self.gate.request() if self.gate else nullcontext(),
                client.stream(
                    "POST", f"{self.base}/{path}", headers=self._headers, json=payload
                ) as response,
            ):
                _check_response(response)
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        raise MistralError("MISTRAL_TIMEOUT")
                    _extend_body(body, chunk, limit)
        except httpx.TimeoutException:
            raise MistralError("MISTRAL_TIMEOUT") from None
        except httpx.RequestError:
            raise MistralError("MISTRAL_UNAVAILABLE") from None
        if time.monotonic() >= deadline:
            raise MistralError("MISTRAL_TIMEOUT")
        return _json_object(body)

    async def post_async(
        self, transport: httpx.AsyncBaseTransport | None, path: str, payload: dict, *, limit: int
    ) -> dict:
        body = bytearray()
        try:
            async with (
                asyncio.timeout(self.timeout),
                self.gate.request_async() if self.gate else nullcontext(),
                httpx.AsyncClient(
                    timeout=self.timeout,
                    transport=transport,
                    trust_env=False,
                    follow_redirects=False,
                ) as client,
                client.stream(
                    "POST", f"{self.base}/{path}", headers=self._headers, json=payload
                ) as response,
            ):
                _check_response(response)
                async for chunk in response.aiter_bytes():
                    _extend_body(body, chunk, limit)
        except (TimeoutError, httpx.TimeoutException):
            raise MistralError("MISTRAL_TIMEOUT") from None
        except httpx.RequestError:
            raise MistralError("MISTRAL_UNAVAILABLE") from None
        return _json_object(body)
