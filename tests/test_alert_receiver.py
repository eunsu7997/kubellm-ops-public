import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def sink(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("local_alert_sink", Path(__file__).parents[1]
                                                / "monitoring/alert-receiver/app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DATAFILE", tmp_path / "events.json")
    return module, TestClient(module.app)


def test_notifications_are_sanitized_and_survive_new_client(sink):
    module, client = sink
    data = {"status": "firing", "alerts": [{"status": "firing", "labels": {
        "alertname": "GatewayInferenceNotReady", "token": "private-detail"},
        "annotations": {"private": "private-detail"}}]}
    assert client.post("/alerts", json=data).status_code == 200
    assert "private-detail" not in module.DATAFILE.read_text()
    with TestClient(module.app) as second:
        assert second.get("/events").json()["retained_count"] == 1


@pytest.mark.parametrize("payload,status", [(b"broken", 400), (b"[]", 400), (b"x" * (128*1024+1), 413)],
                         ids=["malformed", "array", "too-large"])
def test_invalid_payloads_not_recorded(sink, payload, status):
    module, client = sink
    assert client.post("/alerts", content=payload).status_code == status
    assert not module.DATAFILE.exists()


def test_corrupt_store_is_not_overwritten(sink):
    module, client = sink
    module.DATAFILE.write_text("broken")
    assert client.post("/alerts", json={"status": "resolved", "alerts": []}).status_code == 503
    assert module.DATAFILE.read_text() == "broken"


def test_history_is_bounded(sink):
    module, client = sink
    module.DATAFILE.write_text(json.dumps([{"status": "resolved"}] * 1000))
    assert client.post("/alerts", json={"status": "firing", "alerts": []}).status_code == 200
    assert len(json.loads(module.DATAFILE.read_text())) == 1000
