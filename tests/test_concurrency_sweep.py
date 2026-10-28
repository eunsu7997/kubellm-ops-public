import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "scripts/load-test/concurrency_sweep.py"
spec = importlib.util.spec_from_file_location("concurrency_sweep", PATH)
sweep = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sweep  # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(sweep)


def _level(c, rps, ttft, err=0.0):
    return {"concurrency": c, "throughput_rps": rps, "ttft_p99_s": ttft, "error_rate": err}


def test_percentile_nearest_rank():
    assert sweep.percentile([], 99) is None
    assert sweep.percentile([3, 1, 2], 50) == 2
    assert sweep.percentile(list(range(1, 101)), 99) == 99


def test_summarize_keeps_failures_in_denominator():
    results = [sweep.Result(4, 0, "success", 0.1, 1.0, 10),
               sweep.Result(4, 1, "http_429", None, 0.01, 0)]
    s = sweep.summarize(4, results, wall_s=2.0)
    assert s["scheduled"] == 2 and s["success"] == 1
    assert s["error_rate"] == 0.5
    assert s["throughput_rps"] == 0.5
    assert s["chunks_per_s"] == 5.0


def test_knee_where_throughput_flattens_and_latency_doubles():
    levels = [_level(1, 2.0, 0.1), _level(2, 3.8, 0.12), _level(4, 6.5, 0.15),
              _level(8, 6.9, 0.35), _level(16, 7.0, 0.9)]
    assert sweep.find_knee(levels) == 4


def test_knee_stops_before_errors():
    levels = [_level(1, 2.0, 0.1), _level(2, 3.9, 0.11), _level(4, 7.0, 0.13, err=0.1)]
    assert sweep.find_knee(levels) == 2


def test_knee_is_highest_level_when_still_scaling():
    assert sweep.find_knee([_level(1, 1, 0.1), _level(2, 2, 0.1)]) == 2
    assert sweep.find_knee([]) is None


def test_level_bounds_and_dry_run(capsys):
    with pytest.raises(Exception):
        sweep.parse_levels("0,2")
    with pytest.raises(Exception):
        sweep.parse_levels("1,128")
    assert sweep.main(["--base-url", "http://127.0.0.1:9", "--model", "m"]) == 0
    assert "dry run" in capsys.readouterr().out


@pytest.mark.parametrize("levels,expected", [
    ([_level(1, 0, None, err=1)], None),
    ([_level(1, 1, 0.1, err=0.1)], None),
    ([_level(1, 1, 0.1), _level(2, 0, None, err=1), _level(4, 4, 0.1)], 1),
    ([_level(1, 1, 0.1), _level(2, 2, None)], 1),
    ([_level(1, 1, 0.1, err=None)], None),
])
def test_knee_does_not_recommend_failed_or_unmeasured_levels(levels, expected):
    assert sweep.find_knee(levels) == expected


@pytest.mark.parametrize("events,status,chunks", [
    ([{"choices": [{"delta": {"role": "assistant"}}]},
      {"choices": [{"delta": {"content": "hello"}}]},
      {"choices": [{"delta": {}, "finish_reason": "stop"}]}], "success", 1),
    ([{"choices": [{"delta": {"role": "assistant"}}]}], "incomplete", 0),
    ([{"choices": []}], "incomplete", 0),
    ([{"choices": [{"delta": {"content": ""}}]}], "incomplete", 0),
    ([{"error": {"message": "backend failed"}}], "invalid_sse", 0),
    ([{"choices": None}], "invalid_sse", 0),
    ([[]], "invalid_sse", 0),
])
def test_stream_counts_generated_text_only(events, status, chunks):
    import json

    import httpx

    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"

    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
        async with httpx.AsyncClient(transport=transport) as client:
            return await sweep._one(client, "http://mock/v1/chat/completions", {}, 1, 0, 1)

    result = asyncio.run(run())
    assert (result.status, result.chunks) == (status, chunks)
    assert (result.ttft_s is not None) == (chunks > 0)


def test_malformed_sse_is_not_a_success():
    import httpx

    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text="data: not-json\n\ndata: [DONE]\n\n"))
        async with httpx.AsyncClient(transport=transport) as client:
            return await sweep._one(client, "http://mock/v1/chat/completions", {}, 1, 0, 1)

    assert asyncio.run(run()).status == "invalid_sse"


def test_existing_output_is_rejected_before_sending_requests(tmp_path, monkeypatch):
    async def unexpected_request(*args, **kwargs):
        pytest.fail("load must not start when output already exists")

    monkeypatch.setattr(sweep, "run_level", unexpected_request)
    with pytest.raises(SystemExit) as exc:
        sweep.main(["--base-url", "http://127.0.0.1:9", "--model", "m", "--execute", "--out", str(tmp_path)])
    assert exc.value.code == 2
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("flag,value", [
    ("--requests-per-level", "0"), ("--max-tokens", "-1"),
    ("--timeout", "0"), ("--timeout", "nan"), ("--cooldown", "-1"), ("--cooldown", "inf"),
])
def test_invalid_limits_are_rejected(flag, value):
    with pytest.raises(SystemExit) as exc:
        sweep.main(["--base-url", "http://127.0.0.1:9", "--model", "m", flag, value])
    assert exc.value.code == 2
