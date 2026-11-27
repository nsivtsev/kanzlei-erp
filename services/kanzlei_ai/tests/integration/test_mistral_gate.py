"""Shared Mistral request slots against a real Redis, without provider traffic."""

import asyncio
import json
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
from redis import Redis

from kanzlei_ai.providers.mistral import MistralError, MistralRequestGate
from kanzlei_ai.providers.mistral_chat import MistralAssistantProvider
from kanzlei_ai.settings import Settings

pytestmark = pytest.mark.integration


@pytest.fixture
def redis_url():
    # A separate logical database keeps the shared gate keys of a running stack untouched.
    url = urlsplit(Settings().redis_url.get_secret_value())
    isolated = urlunsplit(url._replace(path="/15"))
    with Redis.from_url(isolated) as broker:
        broker.flushdb()
        yield isolated
        broker.flushdb()


def gate(redis_url, **kwargs):
    kwargs.setdefault("total", 4)
    kwargs.setdefault("background", 2)
    return MistralRequestGate(redis_url, **kwargs)


def test_interactive_requests_run_at_the_same_time(redis_url):
    chat = gate(redis_url, interactive=True)

    async def run():
        inside = 0
        both = asyncio.Event()

        async def request():
            nonlocal inside
            async with chat.request_async():
                inside += 1
                if inside == 2:
                    both.set()
                await asyncio.wait_for(both.wait(), 2)

        await asyncio.gather(request(), request())

    asyncio.run(run())


def test_background_work_cannot_take_the_slots_reserved_for_chat(redis_url):
    background = gate(redis_url)
    chat = gate(redis_url, interactive=True)

    with background.request(), background.request():
        with pytest.raises(MistralError, match="^MISTRAL_RATE_LIMITED$") as error:
            with background.request():
                pytest.fail("background exceeded its share")
        assert error.value.status == 429
        assert error.value.retry_after == 2
        with chat.request(), chat.request():
            pass
    with background.request():
        pass


def test_interactive_request_waits_for_a_free_slot(redis_url):
    holder = gate(redis_url, total=1, background=1)
    chat = gate(redis_url, total=1, background=1, interactive=True, wait_seconds=3)
    entered = threading.Event()
    release = threading.Event()

    def hold():
        with holder.request():
            entered.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert entered.wait(2)
        threading.Timer(0.4, release.set).start()
        started = time.monotonic()
        with chat.request():
            assert time.monotonic() - started >= 0.3
    finally:
        release.set()
        thread.join()


def test_full_gate_fails_after_the_bounded_wait(redis_url):
    holder = gate(redis_url, total=1, background=1)
    chat = gate(redis_url, total=1, background=1, interactive=True, wait_seconds=0.5)

    async def run():
        async with holder.request_async():
            started = asyncio.get_running_loop().time()
            with pytest.raises(MistralError, match="^MISTRAL_RATE_LIMITED$"):
                async with chat.request_async():
                    pytest.fail("slot was not held")
            assert asyncio.get_running_loop().time() - started < 2

    asyncio.run(run())


def test_provider_429_cools_down_every_caller(redis_url):
    chat = gate(redis_url, interactive=True)
    with pytest.raises(MistralError):
        with chat.request():
            raise MistralError("MISTRAL_RATE_LIMITED", status=429, retry_after=30)
    for caller in (gate(redis_url), chat):
        with pytest.raises(MistralError, match="^MISTRAL_RATE_LIMITED$") as error:
            with caller.request():
                pytest.fail("cooldown ignored")
        assert 0 < error.value.retry_after <= 30


def test_cancelled_request_frees_its_slot(redis_url):
    only = gate(redis_url, total=1, background=1, interactive=True)

    async def run():
        entered = asyncio.Event()

        async def hold():
            async with only.request_async():
                entered.set()
                await asyncio.sleep(30)

        task = asyncio.create_task(hold())
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        async with only.request_async():
            pass

    asyncio.run(run())


def test_two_assistant_answers_stream_concurrently(redis_url):
    """Two users' answers overlap instead of the second one being refused."""
    started = 0
    both = asyncio.Event()

    async def stream():
        yield f"data: {json.dumps(_chunk({'content': 'Teil '}))}\n\n".encode()
        await asyncio.wait_for(both.wait(), 2)
        yield f"data: {json.dumps(_chunk({'content': 'Ende'}, 'stop'))}\n\n".encode()
        yield b"data: [DONE]\n\n"

    async def handler(request):
        nonlocal started
        started += 1
        if started == 2:
            both.set()
        return httpx.Response(200, content=stream())

    provider = MistralAssistantProvider(
        endpoint="https://api.eu.mistral.ai",
        api_key="synthetic-key",
        transport=httpx.MockTransport(handler),
        gate=gate(redis_url, interactive=True),
    )

    async def run():
        return await asyncio.gather(
            provider.respond([{"role": "user", "content": "A"}], (), None),
            provider.respond([{"role": "user", "content": "B"}], (), None),
        )

    replies = asyncio.run(run())
    assert [reply.content for reply in replies] == ["Teil Ende", "Teil Ende"]


def _chunk(delta, finish=None):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
