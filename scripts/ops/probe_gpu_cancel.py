"""One synthetic stream cancellation; observe adapter lease and backend idle, never kill serving."""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import exp08_jit_readiness as J
import httpx
import oplib as L
import uvicorn
from compare_admission import ROOT, direct_cache, sample, token_specs, write
from http_admission import Gate, make_app
from safety_harness import supervise


async def probe(out):
    prompts = json.loads((out / "prompts.json").read_text())
    prompt = next(p for p, s in prompts.items() if s["long"])
    gate = Gate("basic", inflight=1)
    app = make_app(gate, prompts)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=18023, log_level="warning", access_log=False))
    task = asyncio.create_task(server.serve())
    result = {"expected_outcome": "cancel_after_first_content", "full_stream_success": False}
    try:
        for _ in range(100):
            if server.started:
                break
            if task.done():
                await task
                raise RuntimeError("adapter startup failed")
            await asyncio.sleep(.02)
        if not server.started:
            raise RuntimeError("adapter startup timed out")
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            body = {"model": L.MODEL, "stream": True, "max_tokens": prompts[prompt]["max_tokens"],
                    "messages": [{"role": "user", "content": prompt}]}
            async with client.stream("POST", "http://127.0.0.1:18023/v1/chat/completions", json=body) as response:
                response.raise_for_status()
                lines = response.aiter_lines()
                async for line in lines:
                    if line.startswith("data:") and line[5:].strip() != "[DONE]":
                        event = json.loads(line[5:])
                        if any(c.get("delta", {}).get("content") for c in event.get("choices", [])):
                            result["active_before_close"] = gate.snapshot()
                            break
                else:
                    raise RuntimeError("no content before stream ended")
                cancelled_at = time.perf_counter()
            for _ in range(100):
                if not gate.snapshot()["active"]:
                    break
                await asyncio.sleep(.01)
            result["adapter_zero_observed_after_s"] = time.perf_counter() - cancelled_at
            result["gate_after_close"] = gate.snapshot()
            if gate.snapshot()["active"] or gate.snapshot()["queued"]:
                raise RuntimeError("adapter cancellation leaked")
            samples = []
            for _ in range(10):
                row = await asyncio.to_thread(direct_cache)
                samples.append(row)
                running = [l for l in row["raw"].splitlines() if l.startswith("vllm:num_requests_running{")]
                waiting = [l for l in row["raw"].splitlines() if l.startswith("vllm:num_requests_waiting{")]
                if (running and waiting and float(running[0].split()[-1]) == 0 and
                        float(waiting[0].split()[-1]) == 0 and row["kv_fraction"] == 0):
                    result["backend_idle_observed_after_s"] = time.perf_counter() - cancelled_at
                    break
                await asyncio.sleep(.1)
            else:
                raise RuntimeError("backend did not return to observed idle")
            result["backend_samples"] = samples
            result["limits"] = "sampled upper bound; does not prove engine cancellation timing or VRAM deallocation"
            write(out / "probe.json", result)
            (out / "outcomes.jsonl").write_text(json.dumps({"request_id": 0, "category": "success"}) + "\n")
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--output-dir", type=Path)
    ap.add_argument("--worker", type=Path)
    a = ap.parse_args()
    if a.worker:
        asyncio.run(probe(a.worker))
        return 0
    if not a.execute:
        print("dry-run: cancel one synthetic stream after content, observe lease and backend idle")
        return 0
    if a.output_dir is None:
        ap.error("new --output-dir required")
    with J.DiagnosticLease(L.TRIAL_LOCK):
        if J.active_project_loads() != 0 or not J.runtime_stock() or L.gpu_unsafe(sample()):
            raise RuntimeError("preflight blocked")
        a.output_dir.mkdir(parents=True, exist_ok=False)
        write(a.output_dir / "prompts.json", token_specs())
        result = supervise([sys.executable, str(Path(__file__).resolve()), "--worker", str(a.output_dir.resolve())],
                           a.output_dir / "supervisor", sample, L.gpu_unsafe, cwd=ROOT,
                           journal=a.output_dir / "outcomes.jsonl", wall_seconds=40)
        recovery = {"ready": L.http("GET", "/readyz"), "chat": L.chat(), "sse": L.sse(),
                    "stock": J.runtime_stock()}
        write(a.output_dir / "recovery.json", recovery)
        ok = (result["success"] and recovery["ready"]["status"] == 200 and recovery["chat"]["ok"]
              and recovery["sse"]["ok"] and recovery["stock"])
        print(json.dumps({"ok": ok, "output": str(a.output_dir)}))
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
