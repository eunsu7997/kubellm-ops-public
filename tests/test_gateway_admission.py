"""Opt-in Gateway admission (GATEWAY_MAX_INFLIGHT) and request-body validation."""
import dataclasses
from unittest.mock import AsyncMock

import httpx
import pytest
from prometheus_client import REGISTRY

from gateway.app.config import Settings

OK_BODY = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}]}
REQUEST = {"messages": [{"role": "user", "content": "hi"}]}


def _metric(name, labels=None):
    value = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if value is None else value


def _ok_client(status=200, body=None):
    request = httpx.Request("POST", "http://backend.invalid/v1/chat/completions")
    fake = AsyncMock()
    fake.post = AsyncMock(return_value=httpx.Response(status, json=body or OK_BODY, request=request))
    return fake


@pytest.fixture
def limit(gateway_module, monkeypatch):
    def apply(max_inflight, retry_after=3):
        new = dataclasses.replace(gateway_module.settings, max_inflight=max_inflight,
                                  retry_after_seconds=retry_after)
        monkeypatch.setattr(gateway_module, "settings", new)
    return apply


@pytest.mark.parametrize("payload", [[1, 2], "text", 3])
def test_non_object_json_body_returns_400(client, payload):
    resp = client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "request body must be a JSON object"


def test_limit_disabled_by_default_and_slot_released(client, gateway_module, monkeypatch):
    monkeypatch.setattr(gateway_module, "_http_client", _ok_client())
    assert gateway_module.settings.max_inflight == 0
    resp = client.post("/v1/chat/completions", json=REQUEST)
    assert resp.status_code == 200
    assert gateway_module._inflight == 0


def test_rejects_with_retry_after_when_full(client, gateway_module, monkeypatch, limit):
    fake = _ok_client()
    monkeypatch.setattr(gateway_module, "_http_client", fake)
    limit(1, retry_after=3)
    monkeypatch.setattr(gateway_module, "_inflight", 1)  # another request is already running
    rejected_before = _metric("gateway_admission_rejected_total")

    resp = client.post("/v1/chat/completions", json=REQUEST)

    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "3"
    assert _metric("gateway_admission_rejected_total") == rejected_before + 1
    fake.post.assert_not_awaited()
    assert gateway_module._inflight == 1  # rejection never takes or frees a slot


def test_admits_below_limit_and_releases_after_backend_error(client, gateway_module, monkeypatch, limit):
    monkeypatch.setattr(gateway_module, "_http_client", _ok_client(status=500, body={"error": "x"}))
    limit(2)

    resp = client.post("/v1/chat/completions", json=REQUEST)

    assert resp.status_code == 502
    assert gateway_module._inflight == 0


def test_duration_histogram_labels_outcome(client, gateway_module, monkeypatch):
    monkeypatch.setattr(gateway_module, "_http_client", _ok_client())
    labels = {"outcome": "success", "stream": "false"}
    before = _metric("gateway_request_duration_seconds_count", labels)

    assert client.post("/v1/chat/completions", json=REQUEST).status_code == 200

    assert _metric("gateway_request_duration_seconds_count", labels) == before + 1


@pytest.mark.parametrize("kwargs", [{"max_inflight": -1}, {"retry_after_seconds": 0},
                                    {"retry_after_seconds": 61}])
def test_admission_settings_validation(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)
