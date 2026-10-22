"""Isolated experimental admission adapter; never installed into the serving Gateway.

Only accepts preregistered synthetic prompts with server-side token counts.
Single process/event loop only. Not a production admission service.
"""
import asyncio
import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


class Rejected(Exception):
    pass


@dataclass
class CacheSignal:
    """Direct vLLM gauge, fraction of its allocated KV block pool, never VRAM fraction."""
    value: float | None = None
    observed: float = 0
    max_age: float = 4
    threshold: float = .85

    def update(self, value, observed=None):
        if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1:
            self.value = None
        else:
            self.value = float(value)
        self.observed = time.perf_counter() if observed is None else observed

    def reason(self):
        age = time.perf_counter() - self.observed
        if self.value is None or not 0 <= age <= self.max_age:
            return "cache_unknown_or_stale"
        return "cache_pressure" if self.value >= self.threshold else None


@dataclass(eq=False)
class Ticket:
    cost: int
    long: bool
    priority: str
    created: float
    admitted: float | None = None


class Gate:
    def __init__(self, mode, *, inflight=2, queue=2, wait=0.6, budget=256, long_limit=1, aging=0.2, signal=None):
        if mode not in {"off", "basic", "token"}:
            raise ValueError("unknown policy")
        if min(inflight, queue, wait, budget, long_limit, aging) <= 0:
            raise ValueError("positive bounds required")
        self.mode, self.inflight, self.queue, self.wait = mode, inflight, queue, wait
        self.budget, self.long_limit, self.aging = budget, long_limit, aging
        self.pending, self.active = [], []
        self.condition = asyncio.Condition()
        self.events = []
        self.signal = signal

    def snapshot(self):
        return {"active": len(self.active), "queued": len(self.pending),
                "reserved_tokens": sum(t.cost for t in self.active),
                "active_long": sum(t.long for t in self.active)}

    def event(self, kind, ticket):
        self.events.append({"kind": kind, "cost": ticket.cost, "priority": ticket.priority,
                            "observed": time.perf_counter(),
                            "wait_s": time.perf_counter() - ticket.created, **self.snapshot()})

    def fits(self, ticket):
        if self.mode == "token" and self.signal is not None and self.signal.reason():
            return False
        return (len(self.active) < self.inflight and
                (self.mode != "token" or
                 (sum(t.cost for t in self.active) + ticket.cost <= self.budget and
                  (not ticket.long or sum(t.long for t in self.active) < self.long_limit))))

    def next_ticket(self):
        now = time.perf_counter()
        if self.mode == "token":
            ordered = sorted(self.pending, key=lambda t: (
                0 if now - t.created >= self.aging else 1 if t.priority == "high" else 2,
                t.created if now - t.created >= self.aging else t.cost, t.created))
        else:
            ordered = self.pending
        # An aged request reserves the next available slot instead of being bypassed forever.
        if ordered and now - ordered[0].created >= self.aging:
            return ordered[0] if self.fits(ordered[0]) else None
        return next((t for t in ordered if self.fits(t)), None)

    async def acquire(self, cost, long, priority):
        t = Ticket(cost, long, priority, time.perf_counter())
        async with self.condition:
            if self.mode == "off":
                self.active.append(t)
                t.admitted = time.perf_counter()
                self.event("admit", t)
                return t
            if self.mode == "token" and cost > self.budget:
                self.event("over_budget", t)
                raise Rejected("over_budget")
            if len(self.pending) >= self.queue:
                self.event("queue_full", t)
                raise Rejected("queue_full")
            self.pending.append(t)
            self.event("enqueue", t)
            try:
                while True:
                    if time.perf_counter() - t.created >= self.wait:
                        self.event("wait_expired", t)
                        reason = self.signal.reason() if self.mode == "token" and self.signal else None
                        raise Rejected(reason or "wait_expired")
                    if self.next_ticket() is t:
                        self.pending.remove(t)
                        self.active.append(t)
                        t.admitted = time.perf_counter()
                        self.event("admit", t)
                        return t
                    remaining = self.wait - (time.perf_counter() - t.created)
                    try:
                        await asyncio.wait_for(self.condition.wait(), min(self.aging, max(remaining, 0.001)))
                    except TimeoutError:
                        pass
            finally:
                if t in self.pending:
                    self.pending.remove(t)
                    self.event("dequeue", t)
                self.condition.notify_all()

    async def release(self, t):
        async with self.condition:
            if t in self.active:
                self.active.remove(t)
                self.event("release", t)
            self.condition.notify_all()

    @asynccontextmanager
    async def lease(self, cost, long=False, priority="low"):
        t = await self.acquire(cost, long, priority)
        try:
            yield t
        finally:
            with anyio.CancelScope(shield=True):
                await self.release(t)


def make_app(gate, prompts, upstream="http://127.0.0.1:18003", model="Qwen/Qwen2.5-0.5B-Instruct"):
    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            app.state.client = client
            yield

    app = FastAPI(lifespan=lifespan)
    app.state.requests = []

    @app.get("/healthz")
    async def health():
        return gate.snapshot()

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        arrived = time.perf_counter()
        body = await request.json()
        messages = body.get("messages")
        if (body.get("model") != model or body.get("stream") is not True or
                not isinstance(messages, list) or len(messages) != 1 or
                messages[0].get("role") != "user"):
            return JSONResponse({"error": "synthetic experiment input only"}, status_code=422)
        spec = prompts.get(messages[0].get("content"))
        if spec is None or body.get("max_tokens") != spec["max_tokens"]:
            return JSONResponse({"error": "unregistered synthetic prompt"}, status_code=422)
        record = {"arrival": arrived, "priority": request.headers.get("x-priority", "low"),
                  "cost": spec["input_tokens"] + spec["max_tokens"], "first_content": None,
                  "prompt_tokens_actual": None}
        app.state.requests.append(record)
        acquire = asyncio.create_task(gate.acquire(record["cost"], spec["long"], record["priority"]))
        try:
            while not acquire.done():
                if await request.is_disconnected():
                    acquire.cancel()
                    record["status"] = 499
                    return JSONResponse({"error": "client disconnected"}, status_code=499)
                await asyncio.wait({acquire}, timeout=0.01)
            try:
                ticket = await acquire
            except Rejected as exc:
                record.update(status=429, reason=str(exc))
                return JSONResponse({"error": str(exc)}, status_code=429, headers={"Retry-After": "1"})
        except BaseException:
            acquire.cancel()
            await asyncio.gather(acquire, return_exceptions=True)
            if not acquire.cancelled() and acquire.exception() is None:
                await gate.release(acquire.result())
            raise
        finally:
            if not acquire.done():
                acquire.cancel()
                await asyncio.gather(acquire, return_exceptions=True)
            # A disconnect racing admission must also return the acquired lease.
            if record.get("status") == 499 and not acquire.cancelled() and acquire.exception() is None:
                await gate.release(acquire.result())

        record["queue_wait_s"] = ticket.admitted - ticket.created
        response = None
        try:
            response = await app.state.client.send(app.state.client.build_request(
                "POST", upstream + "/v1/chat/completions", json=body), stream=True)
            record["status"] = response.status_code
            if response.status_code != 200:
                await response.aclose()
                await gate.release(ticket)
                return JSONResponse({"error": "upstream failure"}, status_code=response.status_code)
        except BaseException:
            if response is not None:
                await response.aclose()
            await gate.release(ticket)
            raise

        async def relay():
            import json
            try:
                async for line in response.aiter_lines():
                    if line.startswith("data:") and line[5:].strip() != "[DONE]":
                        try:
                            event = json.loads(line[5:])
                            if any(c.get("delta", {}).get("content") for c in event.get("choices", [])):
                                if record["first_content"] is None:
                                    record["first_content"] = time.perf_counter() - arrived
                            if event.get("usage"):
                                record["prompt_tokens_actual"] = event["usage"].get("prompt_tokens")
                        except (ValueError, TypeError):
                            pass
                    yield line + "\n"
            finally:
                with anyio.CancelScope(shield=True):
                    await response.aclose()
                    await gate.release(ticket)
        return StreamingResponse(relay(), media_type="text/event-stream")

    return app
