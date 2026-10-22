"""백엔드(vLLM/mock) 장애 상황에서 Gateway가 올바르게 반응하는지 테스트한다.

세 가지 실패를 재현한다:
1. connection failure (백엔드에 연결 자체가 안 됨) -> httpx.ConnectError
2. timeout (백엔드가 너무 오래 응답 안 함) -> httpx.TimeoutException
3. backend 500 (백엔드가 내부 오류를 반환함)

세 경우 모두 Gateway는 원본 예외/오류 메시지를 사용자에게 그대로 노출하지 않고,
일반화된 메시지로 감싸서 반환해야 한다(민감 정보 비노출 요구사항).
"""
from unittest.mock import AsyncMock

import httpx


def _post(client, mode):
    return client.post(
        "/v1/chat/completions",
        json={"model": "mock-model", "messages": [{"role": "user", "content": f"test-{mode}"}]},
    )


def test_backend_connection_failure_returns_502(client, gateway_module, monkeypatch):
    fake_client = AsyncMock()
    fake_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    monkeypatch.setattr(gateway_module, "_http_client", fake_client)

    resp = _post(client, "connfail")

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "connection refused" not in detail  # 원본 예외 메시지를 그대로 노출하지 않음
    assert detail == "backend connection failed"


def test_backend_timeout_returns_504(client, gateway_module, monkeypatch):
    fake_client = AsyncMock()
    fake_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    monkeypatch.setattr(gateway_module, "_http_client", fake_client)

    resp = _post(client, "timeout")

    assert resp.status_code == 504
    assert resp.json()["detail"] == "backend request timed out"


def test_backend_500_returns_502_without_leaking_backend_body(client, gateway_module, monkeypatch):
    request = httpx.Request("POST", "http://backend.invalid/v1/chat/completions")
    backend_error_body = {"error": "internal secret stack trace: db_password=hunter2"}
    fake_client = AsyncMock()
    fake_client.post = AsyncMock(
        return_value=httpx.Response(500, json=backend_error_body, request=request)
    )
    monkeypatch.setattr(gateway_module, "_http_client", fake_client)

    resp = _post(client, "500")

    assert resp.status_code == 502
    assert "hunter2" not in resp.text  # 백엔드가 내려준 민감한 오류 본문이 그대로 새 나가면 안 됨
    assert resp.json()["detail"] == "backend returned an error"
