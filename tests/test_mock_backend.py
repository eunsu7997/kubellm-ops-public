"""mock-backend 자체에 대한 최소 테스트.

폴더 이름(`mock-backend`)에 하이픈이 있어 일반적인 `import mock-backend...`가 안 되므로
importlib로 파일 경로에서 직접 모듈을 로드한다.
"""
import importlib.util
import sys
from pathlib import Path

from fastapi.testclient import TestClient

_MOCK_MAIN_PATH = Path(__file__).resolve().parent.parent / "mock-backend" / "app" / "main.py"


def _load_mock_app():
    spec = importlib.util.spec_from_file_location("mock_backend_app_main", _MOCK_MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.app


def test_mock_backend_normal_mode_echoes_input():
    client = TestClient(_load_mock_app())
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "mock-model", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert resp.status_code == 200
    assert "ping" in resp.json()["choices"][0]["message"]["content"]


def test_mock_backend_error500_mode_returns_500():
    client = TestClient(_load_mock_app())
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "mock-model", "messages": [{"role": "user", "content": "x"}]},
        headers={"x-mock-mode": "error500"},
    )
    assert resp.status_code == 500


def _sse_lines(resp):
    return [line[6:] for line in resp.text.splitlines() if line.startswith("data: ")]


def test_mock_backend_streams_openai_sse_with_done():
    client = TestClient(_load_mock_app())
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "m", "stream": True, "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _sse_lines(resp)
    assert events[-1] == "[DONE]"
    import json
    deltas = [json.loads(e)["choices"][0]["delta"].get("content") for e in events[:-1]]
    assert len([d for d in deltas if d]) == 5


def test_mock_backend_capacity_model_is_off_by_default():
    import importlib.util as iu
    spec = iu.spec_from_file_location("mock_cap_check", _MOCK_MAIN_PATH)
    module = iu.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.TOKEN_DELAY_SECONDS == 0
    assert module.MAX_CONCURRENCY == 0
    assert module._slot() is None
