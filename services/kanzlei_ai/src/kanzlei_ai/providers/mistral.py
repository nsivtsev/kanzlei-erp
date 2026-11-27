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
from redis.exceptions import LockError, RedisError


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
    """Bounded in-flight requests across the processes sharing the AI broker.

    Each request holds one of ``total`` numbered Redis locks. Background work
    (OCR and indexing) may use only the first ``background`` slots, so chat
    always keeps ``total - background`` slots of its own. Interactive callers
    try their reserved slots first and briefly wait for a free one; background
    callers fail at once and rely on durable job retries. A provider 429 sets a
    shared cooldown that every caller obeys.
    """

    _SLOT = "kanzlei-ai:mistral:request:{}"
    _COOLDOWN = "kanzlei-ai:mistral:cooldown"
    # Longer than every provider timeout, so a crashed holder frees its slot.
    _LOCK_SECONDS = 120
    _POLL_SECONDS = 0.2
    _BUSY_RETRY_AFTER = 2

    def __init__(
        self,
        redis_url: str,
        *,
        total: int = 1,
        background: int | None = None,
        interactive: bool = False,
        wait_seconds: float = 0.0,
    ):
        background = total if background is None else background
        if total < 1 or not 1 <= background <= total:
            raise ValueError("Invalid Mistral request slots")
        if not math.isfinite(wait_seconds) or wait_seconds < 0:
            raise ValueError("Invalid Mistral request wait")
        self._redis_url = redis_url
        # Interactive callers start with the slots background work cannot take.
        self._slots = tuple(reversed(range(total))) if interactive else tuple(range(background))
        self._wait_seconds = wait_seconds

    @classmethod
    def from_settings(cls, settings, *, interactive: bool = False) -> "MistralRequestGate":
        return cls(
            settings.redis_url.get_secret_value(),
            total=settings.mistral_max_concurrent_requests,
            background=settings.mistral_background_concurrent_requests,
            interactive=interactive,
            wait_seconds=settings.mistral_request_wait_seconds if interactive else 0.0,
        )

    def _locks(self, broker):
        return [
            broker.lock(self._SLOT.format(slot), timeout=self._LOCK_SECONDS) for slot in self._slots
        ]

    @classmethod
    def _busy(cls) -> MistralError:
        return MistralError("MISTRAL_RATE_LIMITED", status=429, retry_after=cls._BUSY_RETRY_AFTER)

    @staticmethod
    def _cooldown(remaining: int) -> MistralError:
        return MistralError("MISTRAL_RATE_LIMITED", status=429, retry_after=remaining)

    @contextmanager
    def request(self):
        try:
            with Redis.from_url(
                self._redis_url, socket_connect_timeout=2, socket_timeout=2
            ) as broker:
                held = self._acquire(broker)
                try:
                    try:
                        yield
                    except MistralError as error:
                        if error.status == 429:
                            broker.set(self._COOLDOWN, "1", ex=max(1, error.retry_after or 1))
                        raise
                finally:
                    try:
                        held.release()
                    except LockError:
                        # The slot expired after the request finished; nothing to free.
                        pass
        except RedisError:
            raise MistralError("MISTRAL_REQUEST_GATE_UNAVAILABLE") from None

    def _acquire(self, broker):
        deadline = time.monotonic() + self._wait_seconds
        locks = self._locks(broker)
        while True:
            remaining = broker.ttl(self._COOLDOWN)
            if remaining >= 0:
                raise self._cooldown(remaining)
            for lock in locks:
                if lock.acquire(blocking=False):
                    return lock
            if time.monotonic() + self._POLL_SECONDS > deadline:
                raise self._busy()
            time.sleep(self._POLL_SECONDS)

    @asynccontextmanager
    async def request_async(self):
        try:
            async with AsyncRedis.from_url(
                self._redis_url, socket_connect_timeout=2, socket_timeout=2
            ) as broker:
                held = await self._acquire_async(broker)
                try:
                    try:
                        yield
                    except MistralError as error:
                        if error.status == 429:
                            await broker.set(self._COOLDOWN, "1", ex=max(1, error.retry_after or 1))
                        raise
                finally:
                    try:
                        await held.release()
                    except LockError:
                        pass
        except RedisError:
            raise MistralError("MISTRAL_REQUEST_GATE_UNAVAILABLE") from None

    async def _acquire_async(self, broker):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._wait_seconds
        locks = self._locks(broker)
        while True:
            remaining = await broker.ttl(self._COOLDOWN)
            if remaining >= 0:
                raise self._cooldown(remaining)
            for lock in locks:
                if await lock.acquire(blocking=False):
                    return lock
            if loop.time() + self._POLL_SECONDS > deadline:
                raise self._busy()
            await asyncio.sleep(self._POLL_SECONDS)


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
        try:
            # Waiting for a free request slot does not count against the timeout.
            with self.gate.request() if self.gate else nullcontext():
                deadline = time.monotonic() + self.timeout
                with client.stream(
                    "POST", f"{self.base}/{path}", headers=self._headers, json=payload
                ) as response:
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
                # Waiting for a free request slot does not count against the timeout.
                self.gate.request_async() if self.gate else nullcontext(),
                asyncio.timeout(self.timeout),
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

    async def stream_async(
        self, transport: httpx.AsyncBaseTransport | None, path: str, payload: dict, *, limit: int
    ):
        """Yield each server-sent JSON event; the whole stream shares one deadline."""
        total = 0
        buffer = bytearray()
        headers = {**self._headers, "Accept": "text/event-stream"}
        try:
            async with (
                # Waiting for a free request slot does not count against the timeout.
                self.gate.request_async() if self.gate else nullcontext(),
                asyncio.timeout(self.timeout),
                httpx.AsyncClient(
                    timeout=self.timeout,
                    transport=transport,
                    trust_env=False,
                    follow_redirects=False,
                ) as client,
                client.stream(
                    "POST", f"{self.base}/{path}", headers=headers, json=payload
                ) as response,
            ):
                _check_response(response)
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise MistralError("MISTRAL_RESPONSE_LIMIT_EXCEEDED")
                    buffer.extend(chunk.replace(b"\r\n", b"\n"))
                    while (end := buffer.find(b"\n\n")) >= 0:
                        frame = bytes(buffer[:end]).decode("utf-8", "strict")
                        del buffer[: end + 2]
                        data = "\n".join(
                            line[5:].lstrip(" ")
                            for line in frame.split("\n")
                            if line.startswith("data:")
                        )
                        if not data:
                            continue
                        if data == "[DONE]":
                            return
                        yield _json_object(data)
        except (TimeoutError, httpx.TimeoutException):
            raise MistralError("MISTRAL_TIMEOUT") from None
        except httpx.RequestError:
            raise MistralError("MISTRAL_UNAVAILABLE") from None
        except UnicodeDecodeError:
            raise MistralError("MISTRAL_INVALID_RESPONSE") from None
        raise MistralError("MISTRAL_INVALID_RESPONSE")
