from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from gateway.app import main


def samples(client):
    return {(s.name, tuple(sorted(s.labels.items()))): s.value
            for family in text_string_to_metric_families(client.get("/metrics").text) for s in family.samples}


def test_unknown_failure_cache_and_recovery_are_observable(monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, readiness_mode="inference"))
    with TestClient(main.app) as client:
        before = samples(client)
        assert before[("gateway_backend_readiness_enabled", ())] == 1
        assert before[("gateway_backend_ready", ())] == -1
        backend = AsyncMock()
        backend.post.side_effect = httpx.ConnectError("private detail")
        monkeypatch.setattr(main, "_http_client", backend)
        assert client.get("/readyz").status_code == 503
        failed = samples(client)
        assert failed[("gateway_backend_ready", ())] == 0
        assert client.get("/readyz").status_code == 503
        cached = samples(client)
        assert cached[("gateway_backend_readiness_cache_hits_total", ())] > failed[
            ("gateway_backend_readiness_cache_hits_total", ())]
        assert backend.post.await_count == 1
        monkeypatch.setattr(main, "_readiness_until", 0)
        backend.post.side_effect = None
        backend.post.return_value = httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
        assert client.get("/readyz").status_code == 200
        assert samples(client)[("gateway_backend_ready", ())] == 1
        assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("outcome,reason", [
    (httpx.ReadTimeout("private timeout"), "timeout"),
    (httpx.ConnectError("private transport"), "transport"),
    (httpx.Response(503), "http_status"),
    (httpx.Response(200, json=[]), "invalid_response"),
])
def test_readiness_failure_reason_has_no_private_exception_detail(monkeypatch, outcome, reason):
    monkeypatch.setattr(main, "settings", replace(main.settings, readiness_mode="inference"))
    with TestClient(main.app) as client:
        backend = AsyncMock()
        if isinstance(outcome, Exception):
            backend.post.side_effect = outcome
        else:
            backend.post.return_value = outcome
        monkeypatch.setattr(main, "_http_client", backend)
        key = ("gateway_backend_readiness_failures_total", (("reason", reason),))
        before = samples(client).get(key, 0)
        assert client.get("/readyz").status_code == 503
        assert samples(client)[key] == before + 1
        assert "private timeout" not in client.get("/metrics").text
