"""Bounded, opt-in open-loop streaming load generator; no network activity on import.

Arrivals follow a precomputed (piecewise-constant) Poisson schedule and each request is sent at its
scheduled time without waiting for earlier ones, so overload shows up as errors and latency instead
of a silently reduced arrival rate.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import ipaddress
import json
import math
import random
import time
from collections import Counter
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlsplit

import httpx

PROMPT_WORD = "lorem"
PRIORITIES = ("high", "low")
PERCENTILES = (50, 95, 99)
PERCENTILE_METHOD = "linear interpolation between closest ranks (numpy 'linear', Hyndman-Fan type 7)"
CATEGORY_NOTES = {
    "success": "HTTP 2xx, content observed, and SSE [DONE] received",
    "stream_incomplete": "content received but clean EOF occurred before SSE [DONE]",
    "timeout": "any httpx timeout, including one after response headers arrived",
    "stream_interrupted": "non-timeout transport error after response headers arrived",
    "redirect": "1xx/3xx status; redirects are not followed",
    "transport_error": "other transport error before headers, or an unexpected client-side exception",
    "dropped_client_cap": "not sent; send offsets taken when the drop was decided",
}
CSV_FIELDS = ["request_id", "priority", "phase", "scheduled_offset_s", "send_offset_s", "send_lag_s",
              "status_code", "category", "ttft_s", "e2e_s", "chunks", "completion_tokens", "tpot_s"]
# Plans above any of these need --allow-large-plan, even for a dry run.
MAX_REQUESTS = 300
MAX_PEAK_RATE = 30.0
MAX_IN_FLIGHT = 64


def endpoint(base_url: str, allow_non_loopback: bool = False) -> str:
    """Validate without DNS resolution. Reject URL fields that could leak secrets."""
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("invalid endpoint") from None
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in {"", "/", "/v1", "/v1/"}):
        raise ValueError("supply a base URL with no credentials, query, fragment or custom path")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    # Require numeric loopback: no DNS lookup or hostname rebinding.
    if not loopback and not allow_non_loopback:
        raise ValueError("numeric loopback endpoint required; remote targets require --allow-non-loopback")
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    return f"{parsed.scheme}://{authority}/v1/chat/completions"


def rate_segments(rate, duration_s, burst=None):
    """Arrival rate as (start, end, rate) segments covering [0, duration_s)."""
    if burst is None:
        return [(0.0, duration_s, rate)]
    start, length, multiplier = burst
    end = start + length
    edges = sorted({0.0, duration_s, min(start, duration_s), min(end, duration_s)})
    return [(lo, hi, rate * multiplier if start <= lo and hi <= end else rate) for lo, hi in pairwise(edges)]


def peak_arrival_rate(rate, duration_s, burst=None):
    return max(segment_rate for _, _, segment_rate in rate_segments(rate, duration_s, burst))


def build_schedule(rate, duration_s, seed, burst=None, high_ratio=0.0, warmup_s=0.0, limit=None):
    """Poisson arrival offsets plus priorities, all drawn from one seeded RNG.

    With `limit`, generation stops once more than `limit` arrivals exist (enough to reject a plan).
    """
    rng = random.Random(seed)
    offsets = []
    for lo, hi, segment_rate in rate_segments(rate, duration_s, burst):
        # Exponential gaps are memoryless, so restarting at each segment edge keeps the process Poisson.
        t = lo
        while segment_rate > 0 and (limit is None or len(offsets) <= limit):
            t += rng.expovariate(segment_rate)
            if t >= hi:
                break
            offsets.append(t)
    return [{"request_id": request_id, "scheduled_offset_s": offset,
             "priority": "high" if rng.random() < high_ratio else "low",
             "phase": "warmup" if offset < warmup_s else "measure"}
            for request_id, offset in enumerate(offsets, start=1)]


def request_body(model, prompt_words, max_tokens):
    return {"model": model, "messages": [{"role": "user", "content": " ".join([PROMPT_WORD] * prompt_words)}],
            "max_tokens": max_tokens, "stream": True, "stream_options": {"include_usage": True}}


def status_category(status):
    special = {429: "rate_limited", 502: "http_502", 504: "http_504"}
    if status in special:
        return special[status]
    return "http_5xx" if status >= 500 else "http_4xx" if status >= 400 else "redirect"


def _has_content(event):
    choices = event.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str) and delta["content"]:
            return True
        if isinstance(choice.get("text"), str) and choice["text"]:
            return True
    return False


async def _read_stream(response, result, start, clock):
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return True
        try:
            event = json.loads(data)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        usage = event.get("usage")
        tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
        if type(tokens) is int and tokens >= 0:
            result["completion_tokens"] = tokens
        if _has_content(event):
            if result["ttft_s"] is None:
                result["ttft_s"] = clock() - start
            result["chunks"] += 1
    return False


async def send_one(client, url, body, priority, timeout, clock=time.perf_counter):
    """Send one streaming request; return only timings, counts and a category (never body text).

    Any httpx timeout is `timeout`, even after headers arrived; other transport errors after headers
    are `stream_interrupted`. Redirects are not followed, so 1xx/3xx statuses are `redirect`.
    """
    result = {"status_code": None, "category": "transport_error", "ttft_s": None, "e2e_s": None,
              "chunks": 0, "completion_tokens": None, "tpot_s": None}
    headers_received = False
    start = clock()
    try:
        async with client.stream("POST", url, json=body, headers={"x-priority": priority},
                                 timeout=timeout) as response:
            headers_received = True
            status = result["status_code"] = response.status_code
            if 200 <= status < 300:
                done = await _read_stream(response, result, start, clock)
                result["category"] = ("success" if done and result["chunks"] else
                                      "stream_incomplete" if result["chunks"] else "invalid_response")
            else:
                result["category"] = status_category(status)
    except httpx.TimeoutException:
        result["category"] = "timeout"
    except httpx.ConnectError:
        result["category"] = "connection_error"
    except httpx.HTTPError:
        result["category"] = "stream_interrupted" if headers_received else "transport_error"
    result["e2e_s"] = clock() - start
    tokens = result["completion_tokens"]
    if result["category"] == "success" and tokens is not None and tokens >= 2:
        result["tpot_s"] = (result["e2e_s"] - result["ttft_s"]) / (tokens - 1)
    return result


NOT_SENT = {"status_code": None, "category": "dropped_client_cap", "ttft_s": None, "e2e_s": None,
            "chunks": None, "completion_tokens": None, "tpot_s": None}


async def run_schedule(schedule, send, max_in_flight, clock=time.perf_counter, sleep=asyncio.sleep, on_result=None):
    """Fire `send(entry)` as an independent task at each scheduled offset.

    send_offset_s/send_lag_s are taken inside the request task right before it calls `send`. Arrivals
    that find `max_in_flight` requests outstanding are recorded as dropped_client_cap and never sent;
    for those the offsets are taken when the drop is decided. An unexpected exception from `send` is
    recorded as transport_error for that request only. `clock` and `sleep` are injectable so tests
    need not wait in real time.
    """
    records, tasks = [], []
    in_flight = 0
    start = clock()

    def stamp(record):
        record["send_offset_s"] = clock() - start
        record["send_lag_s"] = record["send_offset_s"] - record["scheduled_offset_s"]

    async def launch(record, entry):
        nonlocal in_flight
        try:
            stamp(record)
            record.update(await send(entry))
        except Exception:
            # One failed request must not discard the rest of the run's measurements.
            record.update(NOT_SENT, category="transport_error")
        finally:
            in_flight -= 1
            if on_result is not None:
                on_result(dict(record))

    for entry in schedule:
        delay = entry["scheduled_offset_s"] - (clock() - start)
        if delay > 0:
            await sleep(delay)
        record = {"request_id": entry["request_id"], "priority": entry["priority"], "phase": entry["phase"],
                  "scheduled_offset_s": entry["scheduled_offset_s"], **NOT_SENT}
        records.append(record)
        if in_flight >= max_in_flight:
            stamp(record)
            if on_result is not None:
                on_result(dict(record))
            continue
        in_flight += 1
        tasks.append(asyncio.create_task(launch(record, entry)))
        # Let the new task reach `send` now rather than after the next arrival's sleep.
        await asyncio.sleep(0)
    await asyncio.gather(*tasks)
    return records


async def run(plan, schedule, transport=None, clock=time.perf_counter, sleep=asyncio.sleep, on_result=None):
    """Caller supplies a validated plan and its schedule."""
    body = request_body(plan["model"], plan["prompt_words"], plan["max_tokens"])
    limits = httpx.Limits(max_connections=plan["max_in_flight"], max_keepalive_connections=plan["max_in_flight"])
    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                 limits=limits) as client:
        async def send(entry):
            return await send_one(client, plan["endpoint"], body, entry["priority"], plan["timeout"], clock)
        return await run_schedule(schedule, send, plan["max_in_flight"], clock, sleep, on_result)


def percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100
    lower, upper = math.floor(rank), math.ceil(rank)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def summarize(plan, records):
    measured = [r for r in records if r["phase"] == "measure"]
    window_s = plan["duration_s"] - plan["warmup_s"]
    groups = {"all": measured, **{p: [r for r in measured if r["priority"] == p] for p in PRIORITIES}}
    latency = {}
    for name, group in groups.items():
        successes = [r for r in group if r["category"] == "success"]
        latency[name] = {}
        for metric in ("ttft_s", "e2e_s"):
            values = [r[metric] for r in successes if r[metric] is not None]
            latency[name][metric] = {"count": len(values),
                                     **{f"p{pct}": percentile(values, pct) for pct in PERCENTILES}}
    slo = plan["slo_ttft_s"]
    goodput = None if slo is None else {
        name: sum(1 for r in group if r["category"] == "success" and r["ttft_s"] is not None
                  and r["ttft_s"] <= slo) / window_s
        for name, group in groups.items()}
    return {
        "schema_version": 1, "plan": plan, "success_requires_sse_done": True,
        "measurement": "client-side streaming timings from send start; ttft_s = first content chunk",
        "category_notes": CATEGORY_NOTES,
        "measurement_window_s": window_s,
        "warmup_requests_excluded": len(records) - len(measured),
        "categories": dict(Counter(r["category"] for r in measured)),
        "priorities": {p: len(groups[p]) for p in PRIORITIES},
        "categories_by_priority": {p: dict(Counter(r["category"] for r in groups[p])) for p in PRIORITIES},
        "percentile_method": PERCENTILE_METHOD,
        "latency_population": "success requests in the measurement phase",
        "latency": latency,
        "slo_ttft_s": slo,
        "goodput_rps": goodput,
        "max_send_lag_s": max((r["send_lag_s"] for r in measured), default=None),
    }


def write_results(directory, records, summary):
    # Exclusive new directory: never overwrite an earlier run or evidence.
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    with (directory / "summary.json").open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)
    with (directory / "requests.csv").open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(records)


def main(argv=None, transport=None, clock=time.perf_counter, sleep=asyncio.sleep):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="explicit base URL, e.g. http://127.0.0.1:8000")
    parser.add_argument("--model", default="mock-model")
    parser.add_argument("--rate", type=float, default=1.0, help="mean arrivals per second")
    parser.add_argument("--duration-s", type=float, default=10.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--burst", type=float, nargs=3, metavar=("START_S", "DURATION_S", "MULTIPLIER"),
                        help="multiply the arrival rate inside one window")
    parser.add_argument("--high-ratio", type=float, default=0.5, help="fraction of x-priority: high requests")
    parser.add_argument("--max-in-flight", type=int, default=8)
    parser.add_argument("--max-tokens", type=int, default=16)
    parser.add_argument("--prompt-words", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=30.0, help="per HTTP phase, seconds")
    parser.add_argument("--warmup-s", type=float, default=0.0)
    parser.add_argument("--slo-ttft-s", type=float)
    parser.add_argument("--output-dir", help="new directory for JSON and CSV; must not already exist")
    parser.add_argument("--progress-jsonl",
                        help="exclusive append-only metadata journal for external safety supervision")
    parser.add_argument("--allow-non-loopback", action="store_true")
    parser.add_argument("--allow-large-plan", action="store_true",
                        help=f"explicit override for more than {MAX_REQUESTS} requests, {MAX_PEAK_RATE:g} req/s "
                             f"peak or {MAX_IN_FLIGHT} in flight; requires separate approval")
    parser.add_argument("--execute", action="store_true", help="send requests only after human approval")
    args = parser.parse_args(argv)
    floats = [args.rate, args.duration_s, args.high_ratio, args.timeout, args.warmup_s]
    floats += [args.slo_ttft_s] if args.slo_ttft_s is not None else []
    floats += args.burst or []
    if not all(math.isfinite(value) for value in floats):
        parser.error("numeric options must be finite")
    if any(value <= 0 for value in [args.rate, args.duration_s, args.max_in_flight, args.max_tokens,
                                    args.prompt_words, args.timeout]):
        parser.error("rate, duration, limits and timeout must be positive")
    if not 0 <= args.high_ratio <= 1:
        parser.error("--high-ratio must be between 0 and 1")
    if not 0 <= args.warmup_s < args.duration_s:
        parser.error("--warmup-s must be at least 0 and shorter than --duration-s")
    if args.slo_ttft_s is not None and args.slo_ttft_s <= 0:
        parser.error("--slo-ttft-s must be positive")
    burst = tuple(args.burst) if args.burst else None
    if burst and (burst[0] < 0 or burst[1] <= 0 or burst[2] <= 0):
        parser.error("--burst needs START_S >= 0, DURATION_S > 0 and MULTIPLIER > 0")
    peak = peak_arrival_rate(args.rate, args.duration_s, burst)
    large = f"plans above {MAX_REQUESTS} requests, {MAX_PEAK_RATE:g} req/s peak or {MAX_IN_FLIGHT} in flight"
    if (peak > MAX_PEAK_RATE or args.max_in_flight > MAX_IN_FLIGHT) and not args.allow_large_plan:
        parser.error(f"{large} require --allow-large-plan")
    schedule = build_schedule(args.rate, args.duration_s, args.seed, burst, args.high_ratio, args.warmup_s,
                              limit=None if args.allow_large_plan else MAX_REQUESTS)
    if len(schedule) > MAX_REQUESTS and not args.allow_large_plan:
        parser.error(f"{large} require --allow-large-plan")
    if args.execute and not args.base_url:
        parser.error("--execute requires an explicit --base-url")
    try:
        url = endpoint(args.base_url, args.allow_non_loopback) if args.base_url else None
    except ValueError as exc:
        parser.error(str(exc))
    plan = {
        "endpoint": url, "model": args.model, "rate": args.rate, "duration_s": args.duration_s,
        "seed": args.seed, "burst": list(burst) if burst else None, "high_ratio": args.high_ratio,
        "max_in_flight": args.max_in_flight, "max_tokens": args.max_tokens, "prompt_words": args.prompt_words,
        "timeout": args.timeout, "warmup_s": args.warmup_s, "slo_ttft_s": args.slo_ttft_s,
        "scheduled_requests": len(schedule),
        "scheduled_by_priority": {p: sum(1 for e in schedule if e["priority"] == p) for p in PRIORITIES},
        "peak_arrival_rate_rps": peak,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "allow_non_loopback": args.allow_non_loopback,
        "allow_large_plan": args.allow_large_plan,
        "execution": "EXECUTE" if args.execute else "DRY RUN; NOT VERIFIED",
        "request_timeout_note": "per HTTP phase; not a total experiment deadline",
    }
    if not args.execute:
        print(json.dumps(plan, indent=2))
        return 0
    directory = Path(args.output_dir) if args.output_dir else Path("results") / (
        "open_loop_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
    if directory.exists():
        parser.error("output directory already exists; choose a new directory")
    if args.progress_jsonl:
        with Path(args.progress_jsonl).open("x", encoding="utf-8") as progress:
            def observe(record):
                progress.write(json.dumps(record, allow_nan=False) + "\n")
                progress.flush()
            records = asyncio.run(run(plan, schedule, transport, clock, sleep, observe))
    else:
        records = asyncio.run(run(plan, schedule, transport, clock, sleep))
    summary = summarize(plan, records)
    write_results(directory, records, summary)
    print(json.dumps({"categories": summary["categories"], "output_dir": str(directory)}))
    # Request failures are expected results of an overload experiment, not a tool failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
