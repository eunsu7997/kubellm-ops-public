"""Admission invariants independent of a GPU or model server."""
import asyncio

import pytest

from scripts.ops.http_admission import Gate, Rejected


def test_queue_bounds_wait_and_lease_recovery():
    async def trial():
        gate = Gate("basic", inflight=1, queue=1, wait=0.03)
        t = await gate.acquire(10, False, "low")
        waiting = asyncio.create_task(gate.acquire(10, False, "high"))
        await asyncio.sleep(0)
        with pytest.raises(Rejected, match="queue_full"):
            await gate.acquire(10, False, "high")
        with pytest.raises(Rejected, match="wait_expired"):
            await waiting
        assert gate.snapshot()["queued"] == 0
        await gate.release(t)
        assert gate.snapshot()["active"] == 0
    asyncio.run(trial())


def test_cancel_waiter_and_active_lease():
    async def trial():
        gate = Gate("basic", inflight=1)
        started = asyncio.Event()

        async def owner():
            async with gate.lease(10):
                started.set()
                await asyncio.sleep(10)

        running = asyncio.create_task(owner())
        await started.wait()
        waiting = asyncio.create_task(gate.acquire(10, False, "low"))
        await asyncio.sleep(0)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        assert gate.snapshot()["queued"] == 0
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        assert gate.snapshot() == {"active": 0, "queued": 0, "reserved_tokens": 0, "active_long": 0}
        async with gate.lease(10):
            assert gate.snapshot()["active"] == 1
    asyncio.run(trial())


def test_token_budget_and_long_quota():
    async def trial():
        gate = Gate("token", inflight=3, budget=100, wait=0.03)
        with pytest.raises(Rejected, match="over_budget"):
            await gate.acquire(101, False, "high")
        t = await gate.acquire(60, True, "low")
        with pytest.raises(Rejected, match="wait_expired"):
            await gate.acquire(30, True, "high")
        short = await gate.acquire(30, False, "high")
        assert gate.snapshot()["reserved_tokens"] == 90
        await gate.release(short)
        await gate.release(t)
    asyncio.run(trial())


def test_aged_long_is_not_bypassed_by_new_short():
    async def trial():
        gate = Gate("token", inflight=1, budget=100, wait=0.2, aging=0.01)
        t = await gate.acquire(20, False, "high")
        old = asyncio.create_task(gate.acquire(80, True, "low"))
        await asyncio.sleep(0.02)
        new = asyncio.create_task(gate.acquire(20, False, "high"))
        await asyncio.sleep(0)
        await gate.release(t)
        old_ticket = await old
        assert not new.done()
        await gate.release(old_ticket)
        await gate.release(await new)
    asyncio.run(trial())


def test_http_over_budget_returns_429_and_retry_after_without_upstream():
    from fastapi.testclient import TestClient

    from scripts.ops.http_admission import make_app

    gate = Gate("token", budget=100)
    app = make_app(gate, {"synthetic": {"input_tokens": 200, "max_tokens": 16, "long": True}})
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions", json={
            "model": "Qwen/Qwen2.5-0.5B-Instruct", "stream": True, "max_tokens": 16,
            "messages": [{"role": "user", "content": "synthetic"}]})
        assert r.status_code == 429
        assert r.headers["Retry-After"] == "1"
        assert r.json()["error"] == "over_budget"
        assert client.get("/healthz").json()["active"] == 0


def test_tcp_stream_disconnect_releases_slot_and_queue_timeout_is_429():
    """Real localhost HTTP with a deliberately stalled CPU mock; no GPU performance claim."""
    import socket

    import httpx
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse

    from scripts.ops.http_admission import make_app

    async def trial():
        backend = FastAPI()

        @backend.post("/v1/chat/completions")
        async def stall():
            async def chunks():
                yield 'data: {"choices":[{"delta":{"content":"mock"}}]}\n\n'
                await asyncio.sleep(10)
                yield "data: [DONE]\n\n"
            return StreamingResponse(chunks(), media_type="text/event-stream")

        sockets = []
        servers = []
        tasks = []
        try:
            for _ in range(2):
                sock = socket.socket()
                sock.bind(("127.0.0.1", 0))
                sock.listen(16)
                sockets.append(sock)
            upstream = f"http://127.0.0.1:{sockets[0].getsockname()[1]}"
            gate = Gate("basic", inflight=1, queue=1, wait=0.1)
            adapter = make_app(gate, {"synthetic": {"input_tokens": 10, "max_tokens": 16, "long": False}},
                               upstream=upstream)
            for app, sock in zip([backend, adapter], sockets, strict=True):
                server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
                servers.append(server)
                tasks.append(asyncio.create_task(server.serve(sockets=[sock])))
            for _ in range(100):
                if all(s.started for s in servers):
                    break
                await asyncio.sleep(.01)
            url = f"http://127.0.0.1:{sockets[1].getsockname()[1]}"
            body = {"model": "Qwen/Qwen2.5-0.5B-Instruct", "stream": True, "max_tokens": 16,
                    "messages": [{"role": "user", "content": "synthetic"}]}
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                async with client.stream("POST", url + "/v1/chat/completions", json=body) as first:
                    first_lines = first.aiter_lines()
                    async for line in first_lines:
                        if "mock" in line:
                            break
                    pending = asyncio.create_task(client.post(url + "/v1/chat/completions", json=body))
                    for _ in range(50):
                        if gate.snapshot()["queued"] == 1:
                            break
                        await asyncio.sleep(.001)
                    overflow = await client.post(url + "/v1/chat/completions", json=body)
                    assert overflow.status_code == 429
                    assert overflow.headers["Retry-After"] == "1"
                    assert overflow.json()["error"] == "queue_full"
                    expired = await pending
                    assert expired.status_code == 429
                    assert expired.json()["error"] == "wait_expired"
                for _ in range(100):
                    if not gate.snapshot()["active"]:
                        break
                    await asyncio.sleep(.01)
                assert gate.snapshot()["active"] == 0
                assert gate.snapshot()["queued"] == 0
        finally:
            for server in servers:
                server.should_exit = True
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
            for sock in sockets:
                sock.close()
    asyncio.run(trial())
