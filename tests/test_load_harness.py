"""Offline harness checks. MockTransport never opens a socket."""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/load-test/load_test.py"
spec = importlib.util.spec_from_file_location("experiment_harness", SCRIPT)
harness = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = harness
spec.loader.exec_module(harness)


@pytest.mark.parametrize("url", ["http://example.com", "http://localhost:8000", "http://0.0.0.0",
                                     "http://user:secret@127.0.0.1", "http://127.0.0.1?key=secret",
                                     "http://127.0.0.1#secret", "ftp://127.0.0.1", "http://127.0.0.1/private"])
def test_reject_unsafe_url(url):
    with pytest.raises(ValueError):
        harness.endpoint(url)


def test_endpoint():
    assert harness.endpoint("http://127.0.0.1:8000/v1") == "http://127.0.0.1:8000/v1/chat/completions"
    assert harness.endpoint("http://[::1]:8000") == "http://[::1]:8000/v1/chat/completions"
    assert harness.endpoint("https://example.com", True).startswith("https://example.com/")
    with pytest.raises(ValueError):
        harness.endpoint("http://user:secret@example.com", True)


def test_dry_run_and_gates(monkeypatch, capsys, artifact_dir):
    def forbidden(*args, **kwargs):
        pytest.fail("network client constructed in dry run")
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    assert harness.main(["--output-dir", str(artifact_dir / "absent")]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["endpoint"] is None
    assert (plan["requests"], plan["concurrency"], plan["max_tokens"]) == (3, 1, 16)
    assert not (artifact_dir / "absent").exists()
    assert harness.main(["--base-url", "http://127.0.0.1:8000"]) == 0
    for args in [["--execute"], ["--requests", "0"], ["--timeout", "nan"], ["--concurrency", "-1"]]:
        with pytest.raises(SystemExit):
            harness.main(args)


@pytest.mark.parametrize("limits", [["--concurrency", "5"], ["--requests", "51"]])
def test_large_plan_gate(limits, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("network client constructed while validating plan")
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    for execution in [[], ["--execute", "--base-url", "http://127.0.0.1:9999"]]:
        with pytest.raises(SystemExit):
            harness.main(limits + execution)
    assert harness.main(limits + ["--allow-large-plan"]) == 0
    assert json.loads(capsys.readouterr().out)["allow_large_plan"] is True


def test_plan_at_caps(capsys):
    assert harness.main(["--concurrency", "4", "--requests", "50"]) == 0
    assert json.loads(capsys.readouterr().out)["allow_large_plan"] is False


def test_mock_run_and_outputs(artifact_dir):
    active = peak = 0
    async def respond(request):
        nonlocal active, peak
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 16 and payload["stream"] is False
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "PRIVATE RESPONSE"}}],
                                        "usage": {"completion_tokens": 2}})
    plan = {"endpoint": harness.endpoint("http://127.0.0.1:9999"), "model": "fake",
            "target_kind": "direct-vllm", "requests": 5, "concurrency": 2,
            "max_tokens": 16, "timeout": 1, "started_at_utc": "offline"}
    report = asyncio.run(harness.run(plan, httpx.MockTransport(respond)))
    assert peak == 2
    assert report["categories"] == {"success": 5}
    assert report["ttft"] == report["tpot"] == "NOT MEASURED"
    assert all(r["latency_seconds"] >= 0 for r in report["requests"])
    directory = artifact_dir / "new-run"
    harness.write_results(directory, report)
    text = (directory / "summary.json").read_text()
    assert "PRIVATE RESPONSE" not in text and "Reply with OK" not in text
    assert len((directory / "requests.csv").read_text().splitlines()) == 6
    with pytest.raises(FileExistsError):
        harness.write_results(directory, report)


@pytest.mark.parametrize("status,body,expected", [
    (502, {}, "http_502"), (504, {}, "http_504"), (429, {}, "rate_limited"),
    (404, {}, "http_4xx"), (500, {}, "http_5xx"), (302, {}, "redirect"),
    (200, [], "invalid_response"), (200, {}, "invalid_response"),
])
def test_response_categories(status, body, expected):
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(status, json=body))) as client:
            result = await harness.send_one(client, "http://127.0.0.1", "fake", 16, 1, 1)
            assert result.category == expected and result.status_code == status
    asyncio.run(check())


@pytest.mark.parametrize("error,category", [(httpx.ReadTimeout, "timeout"),
                                           (httpx.ConnectError, "connection_error"),
                                           (httpx.ReadError, "transport_error")])
def test_transport_categories(error, category):
    def fail(request):
        raise error("PRIVATE ERROR", request=request)
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            result = await harness.send_one(client, "http://127.0.0.1", "fake", 16, 1, 1)
            assert result.category == category
            assert "PRIVATE" not in json.dumps(harness.asdict(result))
    asyncio.run(check())

@pytest.fixture
def artifact_dir():
    # Avoid pytest's mode-0700 temporary directories in restricted Windows sessions.
    from uuid import uuid4
    directory = SCRIPT.parents[2] / "results" / ("offline-harness-test-" + uuid4().hex)
    directory.mkdir(parents=True)
    # Preserve artifacts; never clean up existing files in this source-only stage.
    return directory
