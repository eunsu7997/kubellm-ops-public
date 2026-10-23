"""Closed-loop concurrency sweep: find where a single-GPU vLLM stops scaling.

For each concurrency level C, C workers send streaming chat requests back to back for a fixed
number of requests per level. Per level it reports throughput, TTFT and end-to-end percentiles
and the error rate (every scheduled request is in the denominator). The knee is the first level
whose added throughput is small while latency grows, which is the evidence for choosing
GATEWAY_MAX_INFLIGHT.

Without --execute it only prints the plan. It never changes the server.

    python scripts/load-test/concurrency_sweep.py --base-url http://127.0.0.1:18003 \
        --model Qwen/Qwen2.5-0.5B-Instruct --levels 1,2,4,8,16,32 --requests-per-level 64 \
        --execute --out results/sweep-$(date +%Y%m%d)
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path

MAX_LEVEL = 64
PROMPT = "Explain in three sentences why GPU memory limits LLM batch size."


@dataclass
class Result:
    level: int
    index: int
    status: str  # success | http_<code> | transport_error | incomplete
    ttft_s: float | None
    e2e_s: float
    chunks: int


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100). None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize(level: int, results: list[Result], wall_s: float) -> dict:
    ok = [r for r in results if r.status == "success"]
    ttft = [r.ttft_s for r in ok if r.ttft_s is not None]
    e2e = [r.e2e_s for r in ok]
    scheduled = len(results)
    return {
        "concurrency": level,
        "scheduled": scheduled,
        "success": len(ok),
        "error_rate": (scheduled - len(ok)) / scheduled if scheduled else None,
        "throughput_rps": len(ok) / wall_s if wall_s > 0 else None,
        "chunks_per_s": sum(r.chunks for r in ok) / wall_s if wall_s > 0 else None,
        "ttft_p50_s": percentile(ttft, 50),
        "ttft_p95_s": percentile(ttft, 95),
        "ttft_p99_s": percentile(ttft, 99),
        "e2e_p50_s": percentile(e2e, 50),
        "e2e_p99_s": percentile(e2e, 99),
        "wall_s": wall_s,
    }


def find_knee(levels: list[dict], *, min_gain: float = 0.10, latency_factor: float = 2.0) -> int | None:
    """Last level worth using: before throughput gain drops under min_gain while p99 TTFT
    grows by latency_factor over the lowest level, or before errors appear."""
    usable = [s for s in levels if s["throughput_rps"] is not None and s["ttft_p99_s"] is not None]
    if not usable:
        return None
    base_ttft = usable[0]["ttft_p99_s"]
    best = usable[0]["concurrency"]
    for prev, cur in zip(usable, usable[1:]):
        if cur["error_rate"]:
            return prev["concurrency"]
        prev_rps = prev["throughput_rps"]
        gain = (cur["throughput_rps"] - prev_rps) / prev_rps if prev_rps else 0
        if gain < min_gain and cur["ttft_p99_s"] >= latency_factor * base_ttft:
            return prev["concurrency"]
        best = cur["concurrency"]
    return best


async def _one(client, url: str, body: dict, level: int, index: int, timeout: float) -> Result:
    import httpx

    start = time.perf_counter()
    ttft = None
    chunks = 0
    done = False
    try:
        async with client.stream("POST", url, json=body, timeout=timeout) as resp:
            if resp.status_code != 200:
                await resp.aread()
                return Result(level, index, f"http_{resp.status_code}", None, time.perf_counter() - start, 0)
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    done = True
                    break
                if ttft is None:
                    ttft = time.perf_counter() - start
                chunks += 1
    except httpx.HTTPError:
        return Result(level, index, "transport_error", ttft, time.perf_counter() - start, chunks)
    status = "success" if done and chunks else "incomplete"
    return Result(level, index, status, ttft, time.perf_counter() - start, chunks)


async def run_level(base_url: str, model: str, level: int, total: int, max_tokens: int, timeout: float):
    import httpx

    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    body = {"model": model, "stream": True, "max_tokens": max_tokens, "temperature": 0,
            "messages": [{"role": "user", "content": PROMPT}]}
    queue = list(range(total))
    results: list[Result] = []

    async with httpx.AsyncClient() as client:
        async def worker():
            while queue:
                index = queue.pop()
                results.append(await _one(client, url, body, level, index, timeout))

        start = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(level)))
        wall = time.perf_counter() - start
    return results, wall


def parse_levels(text: str) -> list[int]:
    levels = sorted({int(x) for x in text.split(",") if x.strip()})
    if not levels or levels[0] < 1 or levels[-1] > MAX_LEVEL:
        raise argparse.ArgumentTypeError(f"levels must be within 1..{MAX_LEVEL}")
    return levels


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--levels", type=parse_levels, default=parse_levels("1,2,4,8,16"))
    ap.add_argument("--requests-per-level", type=int, default=32)
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--cooldown", type=float, default=5, help="seconds between levels")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--execute", action="store_true", help="actually send requests")
    args = ap.parse_args(argv)

    plan = {"base_url": args.base_url, "model": args.model, "levels": args.levels,
            "requests_per_level": args.requests_per_level, "max_tokens": args.max_tokens}
    print(json.dumps({"plan": plan}, ensure_ascii=False))
    if not args.execute:
        print("dry run: add --execute to send requests")
        return 0

    summaries, rows = [], []
    for i, level in enumerate(args.levels):
        if i:
            time.sleep(args.cooldown)
        results, wall = asyncio.run(run_level(args.base_url, args.model, level,
                                              args.requests_per_level, args.max_tokens, args.timeout))
        summary = summarize(level, results, wall)
        summaries.append(summary)
        rows.extend(asdict(r) for r in results)
        print(json.dumps(summary))
        if summary["error_rate"] and summary["error_rate"] > 0.5:
            print("stopping: more than half of the requests failed at this level")
            break

    report = {"plan": plan, "levels": summaries, "knee_concurrency": find_knee(summaries)}
    print(json.dumps({"knee_concurrency": report["knee_concurrency"]}))
    if args.out:
        args.out.mkdir(parents=True, exist_ok=False)
        (args.out / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        with (args.out / "requests.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(Result.__dataclass_fields__))
            writer.writeheader()
            writer.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
