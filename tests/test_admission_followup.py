import asyncio
import time

import pytest

from scripts.ops.bounded_retry import request_with_budget, retry_delay, stream_with_budget
from scripts.ops.http_admission import CacheSignal, Gate, Rejected


def test_pressure_stale_and_recovery():
    async def trial():
        signal = CacheSignal()
        gate = Gate("token", wait=.02, signal=signal)
        with pytest.raises(Rejected, match="unknown_or_stale"):
            await gate.acquire(10, False, "high")
        signal.update(.9)
        with pytest.raises(Rejected, match="cache_pressure"):
            await gate.acquire(10, False, "high")
        signal.update(.1, time.perf_counter() - 10)
        with pytest.raises(Rejected, match="unknown_or_stale"):
            await gate.acquire(10, False, "high")
        signal.update(float("nan"))
        assert signal.reason() == "cache_unknown_or_stale"
        signal.update(.1)
        async with gate.lease(10):
            assert gate.snapshot()["active"] == 1
        assert gate.snapshot()["reserved_tokens"] == 0
    asyncio.run(trial())


@pytest.mark.parametrize("header", [None, "NaN", "-1", "infinity", "bad", "99"])
def test_invalid_or_unaffordable_retry_after_does_not_retry(header):
    assert retry_delay(429, header, 1, 0) is None


def test_retry_budget_no_stream_retry_and_backoff():
    assert retry_delay(429, "1", 1, 0, jitter=.05) == 1.05
    assert retry_delay(429, "1", 2, 0) is None
    assert retry_delay(200, "1", 1, 0) is None
    assert retry_delay(503, "1", 1, 0) is None
    assert retry_delay(429, "0", 1, 0) == .1


def test_token_budget_without_long_quota_causes_wait_then_progress():
    async def trial():
        gate = Gate("token", inflight=4, long_limit=4, budget=100)
        t = await gate.acquire(70, True, "low")
        waiter = asyncio.create_task(gate.acquire(70, True, "high"))
        await asyncio.sleep(.02)
        assert not waiter.done()
        assert gate.snapshot()["reserved_tokens"] == 70
        await gate.release(t)
        await gate.release(await waiter)
    asyncio.run(trial())


@pytest.mark.parametrize("statuses,expected", [([429, 200], 2), ([429, 429], 2), ([500], 1), ([200], 1)])
def test_retry_attempt_amplification_is_bounded(statuses, expected):
    import httpx

    async def trial():
        sent = []

        def handler(request):
            sent.append(request)
            return httpx.Response(statuses[len(sent) - 1], headers={"Retry-After": "0"}, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            _, attempts = await request_with_budget(client, "http://mock/test", {}, jitter=0)
            assert len(attempts) == len(sent) == expected
            with pytest.raises(ValueError, match="stream"):
                await request_with_budget(client, "http://mock/test", {"stream": True})
    asyncio.run(trial())


@pytest.mark.parametrize("first,done,attempts,category", [
    (429, True, 2, "success"), (200, False, 1, "stream_incomplete"), (500, True, 1, "http_5xx")])
def test_sse_retry_only_replays_explicit_429(first, done, attempts, category):
    import httpx

    from scripts.ops.compare_admission import OL

    async def trial():
        sent = []
        payload = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        if done:
            payload += "data: [DONE]\n\n"

        def handler(request):
            sent.append(request)
            status = first if len(sent) == 1 else 200
            return httpx.Response(status, headers={"Retry-After": "0", "Content-Type": "text/event-stream"},
                                  content=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            r = await stream_with_budget(OL.send_one, client, "http://mock/chat", {"stream": True}, "high", jitter=0)
        assert len(sent) == len(r["attempts"]) == attempts
        assert r["category"] == category
        if first == 429:
            assert r["ttft_s"] >= .1
            assert r["attempts"][1]["started_after_s"] >= r["attempts"][0]["sleep_s"]
    asyncio.run(trial())
