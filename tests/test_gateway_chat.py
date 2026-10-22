"""정상 요청(normal request) 테스트.

Gateway가 백엔드(vLLM/mock)로부터 정상 응답을 받았을 때 그대로 잘 돌려주는지 확인한다.
실제 네트워크 호출은 하지 않고, Gateway의 내부 HTTP 클라이언트(_http_client)를
monkeypatch해서 "백엔드가 이런 응답을 줬다고 가정하면"을 재현한다.
"""
from unittest.mock import AsyncMock

import httpx


def _fake_backend_response(status_code=200, json_body=None, content=b""):
    request = httpx.Request("POST", "http://backend.invalid/v1/chat/completions")
    if json_body is not None:
        return httpx.Response(status_code, json=json_body, request=request)
    return httpx.Response(status_code, content=content, request=request)


def test_normal_chat_completion_request(client, gateway_module, monkeypatch):
    fake_body = {
        "id": "mock-abcd1234",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
    }
    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=_fake_backend_response(200, fake_body))
    monkeypatch.setattr(gateway_module, "_http_client", fake_client)

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "mock-model", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "hello"
    fake_client.post.assert_awaited_once()


def test_invalid_json_body_returns_400(client):
    resp = client.post(
        "/v1/chat/completions",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
