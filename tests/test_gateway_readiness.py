"""Offline contract tests; these are not GPU runtime evidence."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from gateway.app.config import Settings


@pytest.mark.parametrize("response,expected", [
    (httpx.Response(200, json={"choices": [{"message": {"content": "Hi"}}]}), 200),
    (httpx.Response(200, json={"choices": []}), 503),
    (httpx.Response(200, json={"choices": [None]}), 503),
    (httpx.Response(200, text="invalid"), 503),
    (httpx.Response(500, text="private diagnostic"), 503),
])
def test_inference_contract_and_cache(client, gateway_module, monkeypatch, response, expected):
    monkeypatch.setattr(gateway_module, "settings", replace(gateway_module.settings, readiness_mode="inference"))
    backend = AsyncMock()
    backend.post.return_value = response
    monkeypatch.setattr(gateway_module, "_http_client", backend)
    for _ in range(3):
        result = client.get("/readyz")
        assert result.status_code == expected
        assert "private" not in result.text
    assert backend.post.await_count == 1
    assert backend.post.call_args.kwargs["json"]["max_tokens"] == 1
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("exc", [httpx.ConnectError("private"), httpx.ReadTimeout("private"), TimeoutError()])
def test_backend_fault_does_not_break_liveness(client, gateway_module, monkeypatch, exc):
    monkeypatch.setattr(gateway_module, "settings", replace(gateway_module.settings, readiness_mode="inference"))
    backend = AsyncMock()
    backend.post.side_effect = exc
    monkeypatch.setattr(gateway_module, "_http_client", backend)
    assert client.get("/readyz").status_code == 503
    assert client.get("/healthz").status_code == 200


def test_backend_mode_checks_health_only(client, gateway_module, monkeypatch):
    monkeypatch.setattr(gateway_module, "settings", replace(gateway_module.settings, readiness_mode="backend"))
    backend = AsyncMock()
    backend.get.return_value = httpx.Response(200)
    monkeypatch.setattr(gateway_module, "_http_client", backend)
    assert client.get("/readyz").status_code == 200
    backend.post.assert_not_awaited()


@pytest.mark.parametrize("kwargs", [
    {"readiness_mode": "unknown"}, {"readiness_timeout": float("nan")},
    {"readiness_timeout": 0}, {"readiness_cache_seconds": 0},
])
def test_bad_settings_fail_fast(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)


def test_negative_cache_recovers_after_expiry(client, gateway_module, monkeypatch):
    monkeypatch.setattr(gateway_module, "settings", replace(gateway_module.settings, readiness_mode="inference"))
    backend = AsyncMock()
    backend.post.return_value = httpx.Response(500)
    monkeypatch.setattr(gateway_module, "_http_client", backend)
    assert client.get("/readyz").status_code == 503
    backend.post.return_value = httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    assert client.get("/readyz").status_code == 503
    monkeypatch.setattr(gateway_module, "_readiness_until", 0)
    assert client.get("/readyz").status_code == 200
    assert backend.post.await_count == 2


def test_concurrent_checks_coalesce_and_total_deadline(gateway_module, monkeypatch):
    async def slow(*args, **kwargs):
        await asyncio.sleep(10)

    async def scenario():
        monkeypatch.setattr(gateway_module, "settings", replace(
            gateway_module.settings, readiness_mode="inference", readiness_timeout=.02,
        ))
        monkeypatch.setattr(gateway_module, "_readiness_lock", asyncio.Lock())
        monkeypatch.setattr(gateway_module, "_readiness_until", 0)
        backend = AsyncMock()
        backend.post.side_effect = slow
        monkeypatch.setattr(gateway_module, "_http_client", backend)
        async with asyncio.timeout(1):
            values = await asyncio.gather(*(gateway_module._check_backend_readiness() for _ in range(20)))
        assert values == [False] * 20
        assert backend.post.await_count == 1

    asyncio.run(scenario())
