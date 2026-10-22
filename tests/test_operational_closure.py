"""No-GPU safety and SSE parsing contracts; live evidence is collected separately."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def load(relative):
    spec = importlib.util.spec_from_file_location("closure_test_module", ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("script,extra", [
    ("close-docker-gap.py", []), ("close-kubernetes-gap.py", ["--repo", str(ROOT)]),
])
def test_closure_dry_run_never_invokes_runtime(script, extra, monkeypatch, tmp_path):
    module = load("scripts/kubernetes/" + script)

    def refuse(*args, **kwargs):
        raise AssertionError("Dry run must not invoke Docker, Kubernetes or shell")

    monkeypatch.setattr(module.subprocess, "run", refuse)
    monkeypatch.setattr(sys, "argv", [script, "--output-dir", str(tmp_path / "new"), *extra])
    module.main()
    assert not (tmp_path / "new").exists()


def test_sse_done_and_non_data_lines():
    module = load("scripts/load-test/streaming-readiness-review.py")
    assert module.parse_event("data: [DONE]") == "DONE"
    assert module.parse_event(": heartbeat") is None


def test_sse_usage_is_not_content_chunk_count():
    module = load("scripts/load-test/streaming-readiness-review.py")
    event = {"choices": [], "usage": {"completion_tokens": 128}}
    assert module.parse_event("data: " + json.dumps(event))["usage"]["completion_tokens"] == 128


def test_malformed_sse_is_not_silent_success():
    module = load("scripts/load-test/streaming-readiness-review.py")
    with pytest.raises(ValueError):
        module.parse_event("data: invalid JSON")
