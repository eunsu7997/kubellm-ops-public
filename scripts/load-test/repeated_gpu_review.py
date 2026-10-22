"""Opt-in, bounded repeated Docker benchmark; never claims saturation or SLO capacity."""
import argparse
import asyncio
import json
import math
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


async def main_async(output):
    output.mkdir(parents=True, exist_ok=False)
    samples, runs = [], []
    stop = asyncio.Event()

    async def sample():
        while not stop.is_set():
            result = await asyncio.to_thread(subprocess.run, [
                "nvidia-smi", "--query-gpu=temperature.gpu,memory.free,utilization.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ], capture_output=True, text=True, timeout=10)
            row = {"utc": datetime.now(UTC).isoformat(), "output": result.stdout.strip(),
                   "exit_code": result.returncode}
            samples.append(row)
            if result.returncode:
                stop.set()
                return
            values = result.stdout.strip().split(",")
            if float(values[0]) >= 85 or float(values[1]) < 512:
                stop.set()
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                pass

    async def phase(client, concurrency, count, label):
        semaphore = asyncio.Semaphore(concurrency)

        async def one(index):
            async with semaphore:
                if stop.is_set():
                    return {"index": index, "category": "not_sent_guard"}
                start = time.monotonic()
                row = {"index": index}
                try:
                    async with asyncio.timeout(15):
                        response = await client.post("http://127.0.0.1:18003/v1/chat/completions", json={
                            "model": "Qwen/Qwen2.5-0.5B-Instruct", "stream": False, "temperature": 0,
                            "messages": [{"role": "user", "content": "Count from 1 to 100, separated by commas."}],
                            "max_tokens": 128,
                        })
                    body = response.json()
                    content = body.get("choices", [{}])[0].get("message", {}).get("content")
                    row.update(status=response.status_code, category=(
                        "success" if response.status_code == 200 and isinstance(content, str) and content
                        else "invalid_or_http_error"), completion_tokens=body.get("usage", {}).get("completion_tokens"))
                except (httpx.HTTPError, TimeoutError, ValueError, IndexError, AttributeError) as exc:
                    row["category"] = type(exc).__name__
                row["latency_seconds"] = time.monotonic() - start
                return row

        started = time.monotonic()
        async with asyncio.timeout(90):
            rows = await asyncio.gather(*(one(i) for i in range(count)))
        wall = time.monotonic() - started
        success = [r for r in rows if r["category"] == "success"]
        latencies = sorted(r["latency_seconds"] for r in success)
        def percentile(p):
            return latencies[max(0, math.ceil(len(latencies) * p) - 1)] if latencies else None
        run = {"label": label, "concurrency": concurrency, "requests": rows, "success": len(success),
               "wall_seconds": wall, "success_rps": len(success) / wall,
               "p50_seconds": percentile(.5), "p95_seconds": percentile(.95),
               "completion_tokens": sum(r.get("completion_tokens") or 0 for r in success)}
        runs.append(run)
        (output / f"{label}.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in run.items() if k != "requests"}), flush=True)
        if len(success) != count:
            stop.set()

    sampler = asyncio.create_task(sample())
    try:
        await asyncio.sleep(1)
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            await phase(client, 1, 4, "warmup")
            for repeat, order in enumerate([(1, 2, 4), (2, 4, 1), (4, 1, 2)], start=1):
                for concurrency in order:
                    if stop.is_set():
                        break
                    await phase(client, concurrency, 24, f"repeat-{repeat}-c{concurrency}")
                    await asyncio.sleep(2)
    finally:
        stop.set()
        await sampler
        result = {"utc": datetime.now(UTC).isoformat(), "measurement": "closed-loop full-response latency",
                  "percentiles": "nearest rank over successful requests per run; warmup separate",
                  "ttft": "NOT MEASURED", "tpot": "NOT MEASURED", "saturation": "NOT VERIFIED",
                  "max_tokens": 128, "timeout_seconds": 15, "runs": runs, "gpu_samples": samples}
        (output / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if len(runs) == 10 and all(r["success"] == len(r["requests"]) for r in runs) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.execute:
        raise SystemExit(asyncio.run(main_async(args.output_dir)))
    print("DRY RUN: 4 warmup + 216 measured requests, c1/2/4, 128 tokens, thermal/VRAM guard")
