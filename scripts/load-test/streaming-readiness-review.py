"""Opt-in bounded varied-input SSE measurement and readiness observation on localhost 18003."""
import argparse
import asyncio
import json
import subprocess
import time
from pathlib import Path

import httpx

PROFILES = [("short", 1, 32, "Reply with one short sentence."),
            ("medium", 4, 128, "context " * 256 + "\nWrite a paragraph about Kubernetes."),
            ("long", 4, 256, "context " * 768 + "\nWrite a detailed paragraph about serving LLMs.")]


def parse_event(line):
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    return "DONE" if data == "[DONE]" else json.loads(data)


async def execute(output):
    output.mkdir(parents=True, exist_ok=False)
    records, observations, gpu = [], [], []
    stop, failed = asyncio.Event(), asyncio.Event()
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        async def watch():
            while not stop.is_set():
                try:
                    r = await client.get("http://127.0.0.1:18003/readyz", timeout=5)
                    observations.append({"monotonic": time.monotonic(), "status": r.status_code})
                except httpx.HTTPError as exc:
                    observations.append({"monotonic": time.monotonic(), "error": type(exc).__name__})
                p = await asyncio.to_thread(subprocess.run, [
                    "nvidia-smi", "--query-gpu=temperature.gpu,memory.free,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ], capture_output=True, text=True, encoding="utf-8", timeout=10)
                gpu.append({"monotonic": time.monotonic(), "output": p.stdout.strip(), "exit": p.returncode})
                try:
                    temp, free, _ = map(float, p.stdout.strip().split(","))
                    if p.returncode or temp >= 85 or free < 512:
                        failed.set()
                        return
                except ValueError:
                    failed.set()
                    return
                await asyncio.sleep(1)

        async def one(profile, cap, prompt, phase, semaphore):
            async with semaphore:
                if failed.is_set():
                    return
                start = time.monotonic()
                row = {"profile": profile, "phase": phase, "max_tokens": cap}
                first, last, tokens, done = None, None, None, False
                try:
                    async with asyncio.timeout(20):
                        async with client.stream("POST", "http://127.0.0.1:18003/v1/chat/completions", json={
                            "model": "Qwen/Qwen2.5-0.5B-Instruct", "messages": [{"role": "user", "content": prompt}],
                            "stream": True, "stream_options": {"include_usage": True},
                            "temperature": 0, "max_tokens": cap,
                        }) as response:
                            row["status"] = response.status_code
                            async for line in response.aiter_lines():
                                event = parse_event(line)
                                if event == "DONE":
                                    done = True
                                elif isinstance(event, dict):
                                    if event.get("usage"):
                                        tokens = event["usage"].get("completion_tokens")
                                    for choice in event.get("choices", []):
                                        if choice.get("delta", {}).get("content"):
                                            last = time.monotonic()
                                            if first is None:
                                                first = last
                    row.update(category="success" if done and first is not None and row["status"] == 200 else "invalid",
                               ttft_seconds=first - start if first else None, e2e_seconds=time.monotonic() - start,
                               completion_tokens=tokens, client_tpot_estimate_seconds=(
                                   (last - first) / (tokens - 1)
                                   if first and type(tokens) is int and tokens > 1 else None))
                except (httpx.HTTPError, TimeoutError, ValueError) as exc:
                    row["category"] = type(exc).__name__
                records.append(row)
                if row["category"] != "success":
                    failed.set()

        monitor = asyncio.create_task(watch())
        try:
            for name, concurrency, cap, prompt in PROFILES:
                await one(name, cap, prompt, "warmup", asyncio.Semaphore(1))
                await one(name, cap, prompt, "warmup", asyncio.Semaphore(1))
                for repeat in range(2):
                    if failed.is_set():
                        break
                    semaphore = asyncio.Semaphore(concurrency)
                    await asyncio.gather(*(one(name, cap, prompt, f"measure-{repeat+1}", semaphore) for _ in range(12)))
        finally:
            stop.set()
            await monitor
            summary = {"profiles": [{"name": p[0], "concurrency": p[1], "max_tokens": p[2]} for p in PROFILES],
                       "measurement": "client first content TTFT; TPOT estimate uses usage, not chunk count",
                       "warnings": ["closed loop excludes worker queue",
                                    "fixed prefixes/cache/background activity uncontrolled",
                                    "not saturation, maximum capacity, or SLO certification"],
                       "requests": records, "readiness": observations, "gpu": gpu}
            (output / "result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"requests": len(records), "success": sum(r["category"] == "success" for r in records),
                      "readiness_observations": len(observations), "readiness_non_200":
                      sum(r.get("status") != 200 for r in observations)}))
    return 1 if failed.is_set() else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.execute:
        raise SystemExit(asyncio.run(execute(args.output_dir)))
    print("DRY RUN: 6 warmup + 72 measured requests, c1/4, 32/128/256 caps, 20s deadline, GPU guard")
