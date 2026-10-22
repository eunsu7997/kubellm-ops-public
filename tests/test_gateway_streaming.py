"""stream=true 요청을 Gateway가 SSE로 그대로 중계하는지 테스트한다.

실제 네트워크는 쓰지 않는다. httpx.MockTransport로 백엔드를 흉내낸 AsyncClient를
Gateway의 _http_client 자리에 끼워 넣는다.
"""
import asyncio
import json
import time

import httpx
import pytest
from prometheus_client import REGISTRY

BACKEND_URL = "http://backend.invalid/v1/chat/completions"

SSE_CHUNKS = [
    b'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n',
    b'data: {"choices":[{"index":0,"delta":{"content":"hel"}}]}\n\n',
    b'data: {"choices":[{"index":0,"delta":{"content":"lo"}}]}\n\n',
    b"data: [DONE]\n\n",
]


def _metric(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _sse_response(chunks):
    async def body():
        for chunk in chunks:
            yield chunk

    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())


def _post(client, stream):
    payload = {"model": "mock-model", "messages": [{"role": "user", "content": "hello"}]}
    if stream is not None:
        payload["stream"] = stream
    return client.post("/v1/chat/completions", json=payload)


def test_stream_relays_chunks_in_order(client, gateway_module, monkeypatch):
    seen_bodies = []

    def handler(request):
        seen_bodies.append(json.loads(request.content))
        return _sse_response(SSE_CHUNKS)

    monkeypatch.setattr(gateway_module, "_http_client", _mock_client(handler))
    success_before = _metric("gateway_requests_success_total")

    resp = _post(client, True)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.content == b"".join(SSE_CHUNKS)
    assert seen_bodies[0]["stream"] is True
    assert _metric("gateway_requests_success_total") == success_before + 1


def test_stream_records_time_to_first_chunk(client, gateway_module, monkeypatch):
    monkeypatch.setattr(gateway_module, "_http_client", _mock_client(lambda request: _sse_response(SSE_CHUNKS)))
    count_before = _metric("gateway_time_to_first_chunk_seconds_count")

    resp = _post(client, True)

    assert resp.status_code == 200
    assert _metric("gateway_time_to_first_chunk_seconds_count") == count_before + 1


def test_stream_backend_5xx_returns_502(client, gateway_module, monkeypatch):
    def handler(request):
        return httpx.Response(500, json={"error": "INTERNAL-ONLY-MARKER"})

    monkeypatch.setattr(gateway_module, "_http_client", _mock_client(handler))
    ttfc_before = _metric("gateway_time_to_first_chunk_seconds_count")
    success_before = _metric("gateway_requests_success_total")

    resp = _post(client, True)

    assert resp.status_code == 502
    assert resp.json()["detail"] == "backend returned an error"
    assert "INTERNAL-ONLY-MARKER" not in resp.text
    assert _metric("gateway_time_to_first_chunk_seconds_count") == ttfc_before
    assert _metric("gateway_requests_success_total") == success_before


def test_stream_backend_4xx_passes_status_through(client, gateway_module, monkeypatch):
    def handler(request):
        return httpx.Response(400, json={"error": "bad request"})

    monkeypatch.setattr(gateway_module, "_http_client", _mock_client(handler))

    resp = _post(client, True)

    assert resp.status_code == 400
    assert resp.json() == {"error": "bad request"}


def test_stream_connection_failure_returns_502(client, gateway_module, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(gateway_module, "_http_client", _mock_client(handler))

    resp = _post(client, True)

    assert resp.status_code == 502
    assert resp.json()["detail"] == "backend connection failed"


def test_stream_timeout_returns_504(client, gateway_module, monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    monkeypatch.setattr(gateway_module, "_http_client", _mock_client(handler))

    resp = _post(client, True)

    assert resp.status_code == 504
    assert resp.json()["detail"] == "backend request timed out"


def test_stream_interrupted_counts_stream_error(client, gateway_module, monkeypatch):
    async def broken_body():
        yield SSE_CHUNKS[0]
        raise httpx.ReadError("connection reset")

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=broken_body())

    monkeypatch.setattr(gateway_module, "_http_client", _mock_client(handler))
    labels = {"reason": "stream_error"}
    errors_before = _metric("gateway_backend_failures_total", labels)
    success_before = _metric("gateway_requests_success_total")
    disconnects_before = _metric("gateway_client_disconnects_total")

    resp = _post(client, True)

    assert resp.status_code == 200
    assert resp.content == SSE_CHUNKS[0]
    assert _metric("gateway_backend_failures_total", labels) == errors_before + 1
    assert _metric("gateway_requests_success_total") == success_before
    assert _metric("gateway_client_disconnects_total") == disconnects_before


def test_stream_cancelled_mid_way_counts_client_disconnect_and_closes_backend(gateway_module):
    """클라이언트 연결 종료로 스트림 작업이 취소된 상황을 흉내낸다."""
    labels = {"reason": "stream_error"}
    errors_before = _metric("gateway_backend_failures_total", labels)
    success_before = _metric("gateway_requests_success_total")
    disconnects_before = _metric("gateway_client_disconnects_total")
    latency_before = _metric("gateway_request_latency_seconds_count")
    received = []

    async def scenario():
        async def hanging_body():
            yield SSE_CHUNKS[0]
            await asyncio.Event().wait()  # 백엔드가 다음 청크를 아직 안 보낸 상태

        first_received = asyncio.Event()
        async with _mock_client(lambda request: httpx.Response(200, content=hanging_body())) as backend:
            resp = await backend.send(backend.build_request("POST", BACKEND_URL), stream=True)
            relay = gateway_module._relay_stream(resp, BACKEND_URL, time.perf_counter())

            async def consume():
                async for chunk in relay:
                    received.append(chunk)
                    first_received.set()

            task = asyncio.create_task(consume())
            await first_received.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return resp

    resp = asyncio.run(scenario())

    assert received == [SSE_CHUNKS[0]]
    assert resp.is_closed
    assert _metric("gateway_client_disconnects_total") == disconnects_before + 1
    assert _metric("gateway_backend_failures_total", labels) == errors_before
    assert _metric("gateway_requests_success_total") == success_before
    assert _metric("gateway_request_latency_seconds_count") == latency_before + 1


def test_stream_generator_closed_early_counts_client_disconnect(gateway_module):
    """응답 전송 쪽이 제너레이터를 중간에 닫는(aclose) 상황."""
    labels = {"reason": "stream_error"}
    errors_before = _metric("gateway_backend_failures_total", labels)
    success_before = _metric("gateway_requests_success_total")
    disconnects_before = _metric("gateway_client_disconnects_total")

    async def scenario():
        async with _mock_client(lambda request: _sse_response(SSE_CHUNKS)) as backend:
            resp = await backend.send(backend.build_request("POST", BACKEND_URL), stream=True)
            relay = gateway_module._relay_stream(resp, BACKEND_URL, time.perf_counter())
            first = await relay.__anext__()
            await relay.aclose()
            return first, resp

    first, resp = asyncio.run(scenario())

    assert first == SSE_CHUNKS[0]
    assert resp.is_closed
    assert _metric("gateway_client_disconnects_total") == disconnects_before + 1
    assert _metric("gateway_backend_failures_total", labels) == errors_before
    assert _metric("gateway_requests_success_total") == success_before


def test_stream_empty_body_counts_success_without_ttfc(client, gateway_module, monkeypatch):
    monkeypatch.setattr(gateway_module, "_http_client", _mock_client(lambda request: _sse_response([])))
    ttfc_before = _metric("gateway_time_to_first_chunk_seconds_count")
    success_before = _metric("gateway_requests_success_total")

    resp = _post(client, True)

    assert resp.status_code == 200
    assert resp.content == b""
    assert _metric("gateway_time_to_first_chunk_seconds_count") == ttfc_before
    assert _metric("gateway_requests_success_total") == success_before + 1


def test_stream_false_keeps_json_response(client, gateway_module, monkeypatch):
    backend_body = {
        "id": "mock-abcd1234",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
    }
    monkeypatch.setattr(
        gateway_module, "_http_client", _mock_client(lambda request: httpx.Response(200, json=backend_body))
    )
    ttfc_before = _metric("gateway_time_to_first_chunk_seconds_count")
    success_before = _metric("gateway_requests_success_total")

    for stream in (False, None):
        resp = _post(client, stream)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.json() == backend_body

    assert _metric("gateway_time_to_first_chunk_seconds_count") == ttfc_before
    assert _metric("gateway_requests_success_total") == success_before + 2


# --- 종료 경로별 지표와 백엔드 스트림 정리 횟수 ---

OUTCOME_METRICS = {
    "success": ("gateway_requests_success_total", {}),
    "stream_error": ("gateway_backend_failures_total", {"reason": "stream_error"}),
    "disconnect": ("gateway_client_disconnects_total", {}),
    "latency": ("gateway_request_latency_seconds_count", {}),
}


class TrackedStream(httpx.AsyncByteStream):
    """청크를 내보낸 뒤 오류를 내거나 멈추고, aclose() 호출 횟수를 기록하는 백엔드 스트림."""

    def __init__(self, chunks, error=None, hang=False):
        self.chunks = chunks
        self.error = error
        self.hang = hang
        self.close_count = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error
        if self.hang:
            await asyncio.Event().wait()

    async def aclose(self):
        self.close_count += 1


def _outcome_snapshot():
    return {key: _metric(name, labels) for key, (name, labels) in OUTCOME_METRICS.items()}


def _outcome_deltas(before):
    after = _outcome_snapshot()
    return {key: after[key] - before[key] for key in OUTCOME_METRICS}


def _tracked_backend(stream):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    return _mock_client(handler)


async def _open_relay(gateway_module, backend):
    resp = await backend.send(backend.build_request("POST", BACKEND_URL), stream=True)
    return resp, gateway_module._relay_stream(resp, BACKEND_URL, time.perf_counter())


def test_outcome_completed_closes_backend_once(gateway_module):
    stream = TrackedStream(SSE_CHUNKS)
    before = _outcome_snapshot()

    async def scenario():
        async with _tracked_backend(stream) as backend:
            resp, relay = await _open_relay(gateway_module, backend)
            return [chunk async for chunk in relay], resp

    received, resp = asyncio.run(scenario())

    assert received == SSE_CHUNKS
    assert resp.is_closed
    assert stream.close_count == 1
    assert _outcome_deltas(before) == {"success": 1, "stream_error": 0, "disconnect": 0, "latency": 1}


def test_outcome_backend_error_closes_backend_once(gateway_module):
    stream = TrackedStream(SSE_CHUNKS[:1], error=httpx.ReadError("connection reset"))
    before = _outcome_snapshot()

    async def scenario():
        async with _tracked_backend(stream) as backend:
            resp, relay = await _open_relay(gateway_module, backend)
            return [chunk async for chunk in relay], resp

    received, resp = asyncio.run(scenario())

    assert received == SSE_CHUNKS[:1]
    assert resp.is_closed
    assert stream.close_count == 1
    assert _outcome_deltas(before) == {"success": 0, "stream_error": 1, "disconnect": 0, "latency": 1}


def test_outcome_cancelled_closes_backend_once(gateway_module):
    stream = TrackedStream(SSE_CHUNKS[:1], hang=True)
    before = _outcome_snapshot()
    received = []

    async def scenario():
        first_received = asyncio.Event()
        async with _tracked_backend(stream) as backend:
            resp, relay = await _open_relay(gateway_module, backend)

            async def consume():
                async for chunk in relay:
                    received.append(chunk)
                    first_received.set()

            task = asyncio.create_task(consume())
            await first_received.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return resp

    resp = asyncio.run(scenario())

    assert received == SSE_CHUNKS[:1]
    assert resp.is_closed
    assert stream.close_count == 1
    assert _outcome_deltas(before) == {"success": 0, "stream_error": 0, "disconnect": 1, "latency": 1}


def test_outcome_generator_closed_early_closes_backend_once(gateway_module):
    stream = TrackedStream(SSE_CHUNKS)
    before = _outcome_snapshot()

    async def scenario():
        async with _tracked_backend(stream) as backend:
            resp, relay = await _open_relay(gateway_module, backend)
            first = await relay.__anext__()
            await relay.aclose()
            return first, resp

    first, resp = asyncio.run(scenario())

    assert first == SSE_CHUNKS[0]
    assert resp.is_closed
    assert stream.close_count == 1
    assert _outcome_deltas(before) == {"success": 0, "stream_error": 0, "disconnect": 1, "latency": 1}


def test_outcome_client_disconnect_via_streaming_response_closes_backend_once(gateway_module):
    """StreamingResponse가 첫 청크를 보낸 뒤 ASGI http.disconnect를 받는 상황을 흉내낸다."""
    stream = TrackedStream(SSE_CHUNKS[:1], hang=True)
    before = _outcome_snapshot()
    sent = []

    async def scenario():
        first_sent = asyncio.Event()

        async def receive():
            await first_sent.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                first_sent.set()

        async with _tracked_backend(stream) as backend:
            body = {"model": "mock-model", "messages": [], "stream": True}
            response = await gateway_module._stream_chat_completions(backend, body, BACKEND_URL, time.perf_counter())
            await asyncio.wait_for(response({"type": "http"}, receive, send), timeout=5)

    asyncio.run(scenario())

    bodies = [m.get("body", b"") for m in sent if m["type"] == "http.response.body"]
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 200
    assert b"".join(bodies) == SSE_CHUNKS[0]
    assert stream.close_count == 1
    assert _outcome_deltas(before) == {"success": 0, "stream_error": 0, "disconnect": 1, "latency": 1}
