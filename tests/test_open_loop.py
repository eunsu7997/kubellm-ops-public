"""Offline open-loop generator checks. MockTransport never opens a socket; fake clocks never wait."""
import asyncio
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/load-test/open_loop.py"
spec = importlib.util.spec_from_file_location("open_loop_harness", SCRIPT)
open_loop = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = open_loop
spec.loader.exec_module(open_loop)

URL = "http://127.0.0.1:9999/v1/chat/completions"
CSV_HEADER = ["request_id", "priority", "phase", "scheduled_offset_s", "send_offset_s", "send_lag_s",
              "status_code", "category", "ttft_s", "e2e_s", "chunks", "completion_tokens", "tpot_s"]
SSE_OK = [
    b'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n',
    b'data: {"choices":[{"index":0,"delta":{"content":"PRIVATE "}}]}\n\n',
    b'data: {"choices":[{"index":0,"delta":{"content":"RESPONSE"}}]}\n\n',
    b'data: {"choices":[{"index":0,"delta":{"content":"!"}}]}\n\n',
    b'data: {"choices":[],"usage":{"prompt_tokens":8,"completion_tokens":3}}\n\n',
    b"data: [DONE]\n\n",
]


class FakeClock:
    """Manual time: `sleep` advances the clock without waiting or yielding."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


def counting_clock():
    ticks = iter(range(1000))
    return lambda: float(next(ticks))


def sse_response(chunks, status=200, error=None):
    async def body():
        for chunk in chunks:
            yield chunk
        if error is not None:
            raise error

    return httpx.Response(status, headers={"content-type": "text/event-stream"}, content=body())


def make_plan(**overrides):
    plan = {"endpoint": URL, "model": "fake", "prompt_words": 8, "max_tokens": 16, "timeout": 1,
            "max_in_flight": 4, "duration_s": 10.0, "warmup_s": 0.0, "slo_ttft_s": None}
    plan.update(overrides)
    return plan


def schedule_of(count, step=0.1):
    return [{"request_id": i, "scheduled_offset_s": i * step, "priority": "low", "phase": "measure"}
            for i in range(1, count + 1)]


async def send_with(handler, clock=None):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body = open_loop.request_body("fake", 8, 16)
        return await open_loop.send_one(client, URL, body, "high", 1, clock or counting_clock())


# (1) deterministic schedule
def test_same_seed_same_schedule():
    first = open_loop.build_schedule(5, 30, seed=7, high_ratio=0.5)
    assert first == open_loop.build_schedule(5, 30, seed=7, high_ratio=0.5)
    assert first != open_loop.build_schedule(5, 30, seed=8, high_ratio=0.5)
    offsets = [e["scheduled_offset_s"] for e in first]
    assert offsets == sorted(offsets) and all(0 <= t < 30 for t in offsets)
    assert [e["request_id"] for e in first] == list(range(1, len(first) + 1))


# (2) burst window
def test_burst_window_has_more_arrivals():
    schedule = open_loop.build_schedule(5, 60, seed=1, burst=(20, 10, 4))

    def count(lo, hi):
        return sum(1 for e in schedule if lo <= e["scheduled_offset_s"] < hi)

    burst = count(20, 30)
    assert burst > 2 * count(0, 10) and burst > 2 * count(40, 50)
    assert open_loop.peak_arrival_rate(5, 60, (20, 10, 4)) == 20
    assert open_loop.peak_arrival_rate(5, 60, (70, 10, 4)) == 5
    assert open_loop.rate_segments(5, 60, (20, 10, 4)) == [(0.0, 20, 5), (20, 30, 20), (30, 60, 5)]


# (3) priorities and header
def test_high_ratio_splits_priorities():
    schedule = open_loop.build_schedule(20, 50, seed=3, high_ratio=0.3)
    share = sum(e["priority"] == "high" for e in schedule) / len(schedule)
    assert 0.25 < share < 0.35
    assert {e["priority"] for e in open_loop.build_schedule(5, 10, seed=3, high_ratio=0)} == {"low"}
    assert {e["priority"] for e in open_loop.build_schedule(5, 10, seed=3, high_ratio=1)} == {"high"}


def test_priority_header_sent():
    schedule = open_loop.build_schedule(10, 5, seed=2, high_ratio=0.5)
    seen = []

    def handler(request):
        seen.append(request.headers["x-priority"])
        payload = json.loads(request.content)
        assert payload["stream"] is True and payload["stream_options"] == {"include_usage": True}
        assert payload["messages"] == [{"role": "user", "content": " ".join(["lorem"] * 8)}]
        assert payload["max_tokens"] == 16 and payload["model"] == "fake"
        return sse_response(SSE_OK)

    clock = FakeClock()
    plan = make_plan(max_in_flight=len(schedule))
    records = asyncio.run(open_loop.run(plan, schedule, httpx.MockTransport(handler), clock, clock.sleep))
    assert Counter(seen) == Counter(e["priority"] for e in schedule)
    assert [r["priority"] for r in records] == [e["priority"] for e in schedule]
    assert {r["category"] for r in records} == {"success"}


# (4) streaming timings
def test_streaming_ttft_and_tpot():
    result = asyncio.run(send_with(lambda request: sse_response(SSE_OK)))
    assert result["category"] == "success" and result["status_code"] == 200
    assert result["chunks"] == 3 and result["completion_tokens"] == 3
    assert result["ttft_s"] < result["e2e_s"]
    # counting clock: start=0, first content=1, end=2 -> tpot = (2 - 1) / (3 - 1)
    assert (result["ttft_s"], result["e2e_s"]) == (1.0, 2.0)
    assert result["tpot_s"] == pytest.approx(0.5)


def test_streaming_without_usage_has_no_tpot():
    chunks = [c for c in SSE_OK if b"usage" not in c]
    result = asyncio.run(send_with(lambda request: sse_response(chunks)))
    assert result["category"] == "success" and result["ttft_s"] < result["e2e_s"]
    assert result["completion_tokens"] is None and result["tpot_s"] is None


# (5) categories
@pytest.mark.parametrize("status,expected", [(429, "rate_limited"), (502, "http_502"), (504, "http_504"),
                                             (404, "http_4xx"), (500, "http_5xx"), (503, "http_5xx")])
def test_status_categories(status, expected):
    result = asyncio.run(send_with(lambda request: httpx.Response(status, json={"error": "PRIVATE"})))
    assert (result["category"], result["status_code"]) == (expected, status)
    assert result["ttft_s"] is None and result["tpot_s"] is None


@pytest.mark.parametrize("error,expected", [(httpx.ReadTimeout, "timeout"), (httpx.ConnectTimeout, "timeout"),
                                            (httpx.ConnectError, "connection_error"),
                                            (httpx.ReadError, "transport_error")])
def test_transport_categories(error, expected):
    def fail(request):
        raise error("PRIVATE ERROR", request=request)

    result = asyncio.run(send_with(fail))
    assert result["category"] == expected and result["status_code"] is None
    assert "PRIVATE" not in json.dumps(result)


def test_stream_interrupted_after_headers():
    result = asyncio.run(send_with(lambda request: sse_response(SSE_OK[:2], error=httpx.ReadError("reset"))))
    assert result["category"] == "stream_interrupted" and result["status_code"] == 200
    assert result["tpot_s"] is None


@pytest.mark.parametrize("chunks", [[b"data: [DONE]\n\n"], [], [SSE_OK[0], SSE_OK[4], SSE_OK[5]],
                                    [b"data: not json\n\n", b": keep-alive\n\n"]])
def test_empty_200_is_invalid_response(chunks):
    result = asyncio.run(send_with(lambda request: sse_response(chunks)))
    assert result["category"] == "invalid_response" and result["status_code"] == 200
    assert result["ttft_s"] is None and result["chunks"] == 0


def test_content_without_done_marker_is_not_success():
    result = asyncio.run(send_with(lambda request: sse_response(SSE_OK[:-1])))
    assert result["category"] == "stream_incomplete" and result["chunks"] > 0


def test_progress_callback_records_completion_outcomes():
    observed = []
    clock = FakeClock()
    result = asyncio.run(open_loop.run_schedule(schedule_of(3), instant_success, 4, clock, clock.sleep,
                                                on_result=observed.append))
    assert {r["request_id"] for r in observed} == {r["request_id"] for r in result}
    assert all(r["category"] == "success" for r in observed)


# (6) open-loop sending and client cap
def run_held(schedule, max_in_flight, oversleep=0.0, on_result=None):
    """Run against a transport that holds every response until the whole schedule has been walked.

    Returns the records, the fake-clock time at which each request reached the transport, and the
    responses that had finished before release (expected: none).
    """
    clock = FakeClock()
    arrivals, finished = [], []

    async def sleep(seconds):
        clock.now += seconds + oversleep

    async def scenario():
        release = asyncio.Event()

        async def handler(request):
            arrivals.append(clock())
            await release.wait()
            finished.append(clock())
            return sse_response(SSE_OK)

        task = asyncio.create_task(open_loop.run(make_plan(max_in_flight=max_in_flight), schedule,
                                                 httpx.MockTransport(handler), clock, sleep, on_result))
        for _ in range(100):
            await asyncio.sleep(0)
        assert not task.done()
        finished_before_release = list(finished)
        release.set()
        return await task, finished_before_release

    records, finished_before_release = asyncio.run(scenario())
    return records, arrivals, finished_before_release


def test_later_requests_start_before_earlier_responses_finish():
    records, arrivals, finished_before_release = run_held(schedule_of(3), max_in_flight=8)
    assert finished_before_release == []
    assert arrivals == pytest.approx([0.1, 0.2, 0.3])
    assert [r["send_offset_s"] for r in records] == pytest.approx(arrivals)
    assert all(r["send_lag_s"] == pytest.approx(0) for r in records)
    assert [r["category"] for r in records] == ["success"] * 3


def test_late_start_is_recorded_as_send_lag():
    records, arrivals, finished_before_release = run_held(schedule_of(3), max_in_flight=8, oversleep=0.05)
    assert finished_before_release == []
    assert arrivals == pytest.approx([0.15, 0.25, 0.35])
    assert [r["send_offset_s"] for r in records] == pytest.approx(arrivals)
    assert [r["send_lag_s"] for r in records] == pytest.approx([0.05] * 3)


def test_requests_over_max_in_flight_are_dropped_unsent():
    records, arrivals, finished_before_release = run_held(schedule_of(6), max_in_flight=2)
    assert finished_before_release == []
    assert arrivals == pytest.approx([0.1, 0.2])
    assert [r["category"] for r in records] == ["success"] * 2 + ["dropped_client_cap"] * 4
    for dropped in records[2:]:
        assert dropped["status_code"] is None and dropped["e2e_s"] is None and dropped["chunks"] is None
    assert [r["send_offset_s"] for r in records] == pytest.approx([0.1, 0.2, 0.3, 0.4, 0.5, 0.6])


def test_progress_callback_contains_unsent_arrivals_and_sent_completions():
    observed = []
    records, _, _ = run_held(schedule_of(6), max_in_flight=2, on_result=observed.append)
    assert len(observed) == len(records) == 6
    assert [r["category"] for r in observed].count("dropped_client_cap") == 4
    assert len({r["request_id"] for r in observed}) == 6


def instant_success(entry):
    async def send():
        return {"status_code": 200, "category": "success", "ttft_s": 0.0, "e2e_s": 0.0, "chunks": 1,
                "completion_tokens": None, "tpot_s": None}
    return send()


def test_completed_requests_free_capacity():
    clock = FakeClock()
    records = asyncio.run(open_loop.run_schedule(schedule_of(5), instant_success, 1, clock, clock.sleep))
    assert [r["category"] for r in records] == ["success"] * 5


def test_unexpected_send_exception_is_recorded_not_fatal():
    async def send(entry):
        if entry["request_id"] == 2:
            raise RuntimeError("PRIVATE")
        return await instant_success(entry)

    clock = FakeClock()
    records = asyncio.run(open_loop.run_schedule(schedule_of(3), send, 4, clock, clock.sleep))
    assert [r["category"] for r in records] == ["success", "transport_error", "success"]
    assert records[1]["status_code"] is None and records[1]["send_offset_s"] == pytest.approx(0.2)
    assert "PRIVATE" not in json.dumps(records)


# (7) dry run
def test_dry_run_builds_no_client_and_writes_nothing(monkeypatch, capsys, artifact_dir):
    def forbidden(*args, **kwargs):
        pytest.fail("network client constructed in dry run")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    target = artifact_dir / "absent"
    argv = ["--rate", "2", "--duration-s", "10", "--seed", "5", "--high-ratio", "0.4",
            "--base-url", "http://127.0.0.1:8000", "--output-dir", str(target)]
    assert open_loop.main(argv) == 0
    plan = json.loads(capsys.readouterr().out)
    schedule = open_loop.build_schedule(2, 10, 5, high_ratio=0.4)
    assert plan["scheduled_requests"] == len(schedule)
    assert plan["scheduled_by_priority"] == {p: sum(e["priority"] == p for e in schedule) for p in ("high", "low")}
    assert plan["peak_arrival_rate_rps"] == 2 and plan["seed"] == 5
    assert plan["endpoint"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert plan["execution"].startswith("DRY RUN")
    assert not target.exists()
    assert open_loop.main(["--burst", "2", "3", "4"]) == 0
    assert json.loads(capsys.readouterr().out)["peak_arrival_rate_rps"] == 4
    assert list(artifact_dir.iterdir()) == []


# (8) unsafe URLs and large plans
@pytest.mark.parametrize("url", ["http://example.com", "http://localhost:8000", "http://0.0.0.0",
                                 "http://user:secret@127.0.0.1", "http://127.0.0.1?key=secret",
                                 "http://127.0.0.1#secret", "ftp://127.0.0.1", "http://127.0.0.1/private"])
def test_reject_unsafe_url(url, monkeypatch):
    with pytest.raises(ValueError):
        open_loop.endpoint(url)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: pytest.fail("client constructed"))
    with pytest.raises(SystemExit):
        open_loop.main(["--execute", "--base-url", url])


def test_endpoint():
    assert open_loop.endpoint("http://127.0.0.1:8000/v1") == URL.replace("9999", "8000")
    assert open_loop.endpoint("http://[::1]:8000") == "http://[::1]:8000/v1/chat/completions"
    assert open_loop.endpoint("https://example.com", True).startswith("https://example.com/")
    with pytest.raises(ValueError):
        open_loop.endpoint("http://user:secret@example.com", True)


@pytest.mark.parametrize("args", [
    ["--execute"], ["--rate", "0"], ["--rate", "nan"], ["--duration-s", "-1"], ["--high-ratio", "1.5"],
    ["--max-in-flight", "0"], ["--warmup-s", "10"], ["--slo-ttft-s", "0"], ["--burst", "1", "0", "2"],
    ["--timeout", "inf"],
])
def test_invalid_options(args):
    with pytest.raises(SystemExit) as exc:
        open_loop.main(args)
    assert exc.value.code != 0


@pytest.mark.parametrize("limits", [["--rate", "10", "--duration-s", "40"], ["--rate", "31", "--duration-s", "1"],
                                    ["--rate", "10", "--duration-s", "10", "--burst", "0", "1", "4"],
                                    ["--max-in-flight", "65"]])
def test_large_plan_gate(limits, monkeypatch, capsys):
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: pytest.fail("client constructed"))
    for execution in [[], ["--execute", "--base-url", "http://127.0.0.1:9999"]]:
        with pytest.raises(SystemExit):
            open_loop.main(limits + execution)
    assert open_loop.main(limits + ["--allow-large-plan"]) == 0
    assert json.loads(capsys.readouterr().out)["allow_large_plan"] is True


def test_plan_at_caps(capsys):
    assert open_loop.main(["--rate", "30", "--duration-s", "5", "--max-in-flight", "64"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["scheduled_requests"] <= 300 and plan["allow_large_plan"] is False


# (9) outputs
def test_execute_writes_outputs_without_prompt(artifact_dir, capsys):
    def handler(request):
        if request.headers["x-priority"] == "high":
            return sse_response(SSE_OK)
        return httpx.Response(503, text="PRIVATE ERROR BODY")

    directory = artifact_dir / "run"
    argv = ["--execute", "--base-url", "http://127.0.0.1:9999", "--output-dir", str(directory),
            "--rate", "5", "--duration-s", "4", "--seed", "1", "--max-in-flight", "64",
            "--warmup-s", "1", "--slo-ttft-s", "1"]
    clock = FakeClock()
    # Failed requests are experiment results, not a tool failure.
    assert open_loop.main(argv, httpx.MockTransport(handler), clock, clock.sleep) == 0
    capsys.readouterr()
    csv_text = (directory / "requests.csv").read_text(encoding="utf-8")
    summary_text = (directory / "summary.json").read_text(encoding="utf-8")
    assert csv_text.splitlines()[0].split(",") == CSV_HEADER
    for text in (csv_text, summary_text):
        assert "lorem" not in text and "PRIVATE" not in text and "RESPONSE" not in text
    summary = json.loads(summary_text)
    assert summary["plan"]["seed"] == 1 and summary["plan"]["burst"] is None
    assert set(summary["categories"]) <= {"success", "http_5xx"}
    with pytest.raises(SystemExit) as exc:
        open_loop.main(argv, httpx.MockTransport(handler), clock, clock.sleep)
    assert exc.value.code != 0
    with pytest.raises(FileExistsError):
        open_loop.write_results(directory, [], summary)


# (10) summary statistics
def record(priority, category, ttft, e2e, phase="measure", lag=0.0):
    return {"priority": priority, "phase": phase, "category": category, "ttft_s": ttft, "e2e_s": e2e,
            "send_lag_s": lag}


def test_summary_excludes_warmup_and_matches_hand_values():
    records = [record("high", "success", 9.0, 99.0, phase="warmup", lag=7.0)]
    records += [record("high", "success", t, e, lag=0.01) for t, e in
                [(0.5, 5.0), (0.1, 1.0), (0.4, 4.0), (0.2, 2.0), (0.3, 3.0)]]
    records += [record("low", "success", 0.2, 1.0), record("low", "rate_limited", None, 0.01, lag=0.3)]
    summary = open_loop.summarize(make_plan(duration_s=12.0, warmup_s=2.0, slo_ttft_s=0.35), records)
    assert summary["warmup_requests_excluded"] == 1
    assert summary["measurement_window_s"] == 10.0
    assert summary["categories"] == {"success": 6, "rate_limited": 1}
    assert summary["priorities"] == {"high": 5, "low": 2}
    assert summary["categories_by_priority"] == {"high": {"success": 5}, "low": {"success": 1, "rate_limited": 1}}
    assert "linear" in summary["percentile_method"]
    high = summary["latency"]["high"]
    assert high["ttft_s"]["count"] == 5
    assert [high["ttft_s"][k] for k in ("p50", "p95", "p99")] == pytest.approx([0.3, 0.48, 0.496])
    assert [high["e2e_s"][k] for k in ("p50", "p95", "p99")] == pytest.approx([3.0, 4.8, 4.96])
    low = summary["latency"]["low"]
    assert [low["ttft_s"][k] for k in ("p50", "p95", "p99")] == pytest.approx([0.2] * 3)
    assert [low["e2e_s"][k] for k in ("p50", "p95", "p99")] == pytest.approx([1.0] * 3)
    every = summary["latency"]["all"]["ttft_s"]
    assert [every["p50"], every["p95"]] == pytest.approx([0.25, 0.475])
    assert summary["goodput_rps"] == pytest.approx({"all": 0.4, "high": 0.3, "low": 0.1})
    assert summary["max_send_lag_s"] == pytest.approx(0.3)


def test_summary_without_slo_or_successes():
    summary = open_loop.summarize(make_plan(), [record("low", "timeout", None, 1.0)])
    assert summary["goodput_rps"] is None
    assert summary["latency"]["low"]["ttft_s"] == {"count": 0, "p50": None, "p95": None, "p99": None}
    assert open_loop.percentile([], 50) is None
    assert open_loop.percentile([1.0, 2.0, 3.0, 4.0], 50) == pytest.approx(2.5)


@pytest.fixture
def artifact_dir():
    # Avoid pytest's mode-0700 temporary directories in restricted Windows sessions.
    from uuid import uuid4
    # Prefix matches the existing .gitignore entry for offline harness artifacts.
    directory = SCRIPT.parents[2] / "results" / ("offline-harness-test-open-loop-" + uuid4().hex)
    directory.mkdir(parents=True)
    return directory
