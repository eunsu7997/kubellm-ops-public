"""Opt-in bounded HTTP admission comparison against the existing private Gateway.

Dry-run by default. Uses no new GPU process. All outputs are new paths.
"""
import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent))
import exp08_jit_readiness as J
import oplib as L
from bounded_retry import stream_with_budget
from http_admission import CacheSignal, Gate, make_app
from safety_harness import supervise

ROOT = Path(__file__).resolve().parents[2]
OPEN_LOOP = ROOT / "scripts/load-test/open_loop.py"
spec = importlib.util.spec_from_file_location("existing_open_loop", OPEN_LOOP)
OL = importlib.util.module_from_spec(spec)
spec.loader.exec_module(OL)
MODEL = L.MODEL
PORT = 18023


def write(path, value):
    with Path(path).open("x", encoding="utf-8") as f:
        json.dump(value, f, indent=2, ensure_ascii=False, allow_nan=False)


def token_specs():
    prompts = {}
    for long, words, output in [(False, 8, 16), (True, 128, 64)]:
        prompt = " ".join(["Explain"] * words) + ". Reply briefly."
        body = {"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                "add_generation_prompt": True}
        # Only synthetic message JSON goes to stdin. No cache files or credentials are read.
        code = ("import sys,json,urllib.request; b=sys.stdin.buffer.read(); "
                "r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/tokenize',"
                "data=b,headers={'Content-Type':'application/json'}),timeout=10); print(r.read().decode())")
        r = subprocess.run(["docker", "exec", "-i", "kubellm-gpu-secure-vllm-1", "python3", "-c", code],
                           input=json.dumps(body), capture_output=True, text=True, timeout=20, check=True)
        tokens = json.loads(r.stdout)["count"]
        if type(tokens) is not int or not 0 < tokens < 1800:
            raise ValueError("invalid tokenizer count")
        prompts[prompt] = {"long": long, "input_tokens": tokens, "max_tokens": output}
    return prompts


def trace_for(prompts, pilot):
    keys = list(prompts)
    trace = []
    for burst in range(2 if pilot else 3):
        for j in range(3 if pilot else 4):
            long = j % 2 == 0
            prompt = keys[int(long)]
            trace.append({"request_id": len(trace), "offset": burst * 2.0 + j * 0.02,
                          "priority": "low" if long else "high", "prompt": prompt})
    return trace


def percentile(values, quantile):
    return sorted(values)[max(0, math.ceil(len(values) * quantile) - 1)] if values else None


def direct_cache():
    code = ("import urllib.request; t=urllib.request.urlopen('http://127.0.0.1:8000/metrics',timeout=3).read().decode();"
            "print('\\n'.join(l for l in t.splitlines() if l.startswith(('vllm:kv_cache_usage_perc{',"
            "'vllm:num_requests_running{','vllm:num_requests_waiting{','vllm:num_preemptions_total{'))))")
    r = subprocess.run(["docker", "exec", "kubellm-gpu-secure-vllm-1", "python3", "-c", code],
                       capture_output=True, text=True, timeout=5, check=True)
    rows = [line for line in r.stdout.splitlines() if line.startswith("vllm:kv_cache_usage_perc{")]
    if len(rows) != 1:
        raise ValueError("missing/ambiguous direct KV gauge")
    value = float(rows[0].split()[-1])
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("invalid direct KV fraction")
    return {"observed": time.perf_counter(), "kv_fraction": value, "raw": r.stdout}


async def phase(out, name, mode, prompts, trace):
    followup = mode in {"token-budget", "token-dynamic", "basic-cross", "off-cross", "token-retry"}
    signal = CacheSignal() if mode == "token-dynamic" else None
    policy = "token" if mode.startswith("token-") else mode.split("-")[0]
    gate = Gate(policy, inflight=2, queue=2, wait=0.6, budget=250 if followup else 400,
                long_limit=4 if followup else 1, aging=0.2, signal=signal)
    cache_rows, monitor = [], None
    if signal:
        initial = await asyncio.to_thread(direct_cache)
        signal.update(initial["kv_fraction"], initial["observed"])
        cache_rows.append(initial)

        async def poll_cache():
            while True:
                await asyncio.sleep(1)
                try:
                    row = await asyncio.to_thread(direct_cache)
                    signal.update(row["kv_fraction"], row["observed"])
                    cache_rows.append(row)
                except Exception as exc:
                    signal.update(None)
                    cache_rows.append({"error": type(exc).__name__, "observed": time.perf_counter()})
        monitor = asyncio.create_task(poll_cache())
    app = make_app(gate, prompts)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT,
                                         log_level="warning", access_log=False, lifespan="on",
                                         timeout_graceful_shutdown=3))
    serving = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            if serving.done():
                await serving
                raise RuntimeError("adapter failed to start")
            await asyncio.sleep(0.02)
        else:
            raise RuntimeError("adapter startup deadline")
        started = time.perf_counter()
        async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
            async def one(entry):
                await asyncio.sleep(max(0, started + entry["offset"] - time.perf_counter()))
                sent = time.perf_counter()
                body = {"model": MODEL, "stream": True, "temperature": 0,
                        "stream_options": {"include_usage": True},
                        "messages": [{"role": "user", "content": entry["prompt"]}],
                        "max_tokens": prompts[entry["prompt"]]["max_tokens"]}
                url = f"http://127.0.0.1:{PORT}/v1/chat/completions"
                if mode == "token-retry":
                    r = await stream_with_budget(OL.send_one, client, url, body, entry["priority"],
                                                 jitter=.01 * (entry["request_id"] % 6))
                else:
                    r = await OL.send_one(client, url, body, entry["priority"], 15)
                r.update(request_id=entry["request_id"], priority=entry["priority"],
                         send_lag_s=sent - started - entry["offset"], offset=entry["offset"])
                with (out / "outcomes.jsonl").open("a", encoding="utf-8") as f:
                    # A valid 429 is a planned rejection, not a sent transport failure for the safety guard.
                    journal = {"request_id": phase.sequence, "category":
                               "success" if r["category"] in {"success", "rate_limited"} else r["category"]}
                    phase.sequence += 1
                    f.write(json.dumps(journal) + "\n")
                    f.flush()
                return r
            rows = await asyncio.gather(*(one(e) for e in trace))
        await asyncio.sleep(0.05)
        elapsed = time.perf_counter() - started
        state = gate.snapshot()
        write(out / f"{name}-rows.json", rows)
        write(out / f"{name}-adapter.json", {"events": gate.events, "requests": app.state.requests,
                                           "final": state, "direct_cache": cache_rows})
        if state["active"] or state["queued"]:
            raise RuntimeError("lease leaked")
        if any(r["category"] not in {"success", "rate_limited"} for r in rows):
            raise RuntimeError("sent request failure")
        success = [r for r in rows if r["category"] == "success"]
        accepted = [r for r in rows if r["status_code"] == 200]
        actual = [r for r in app.state.requests if r["prompt_tokens_actual"] is not None]
        errors = [r["prompt_tokens_actual"] - (r["cost"] -
                  next(s["max_tokens"] for s in prompts.values() if s["input_tokens"] + s["max_tokens"] == r["cost"]))
                  for r in actual]
        summary = {"policy": mode, "scheduled": len(trace), "sent": len(rows), "unsent": len(trace) - len(rows),
                   "counts": dict(Counter(r["category"] for r in rows)), "success_rate": len(success) / len(trace),
                   "accepted_failure_rate": (len(accepted) - len(success)) / len(accepted) if accepted else None,
                   "client_ttft_p99_s": percentile([r["ttft_s"] for r in success], .99),
                   "adapter_arrival_ttft_p99_s": percentile([r["first_content"] for r in app.state.requests
                                                            if r["first_content"] is not None], .99),
                   "high_ttft_p99_s": percentile([r["ttft_s"] for r in success if r["priority"] == "high"], .99),
                   "low_ttft_p99_s": percentile([r["ttft_s"] for r in success if r["priority"] == "low"], .99),
                   "slo_1s_attainment_all_scheduled": sum(r["ttft_s"] <= 1 for r in success) / len(trace),
                   "elapsed_including_drain_s": elapsed,
                   "output_tokens": sum(r["completion_tokens"] or 0 for r in success),
                   "output_tokens_per_observed_second": sum(r["completion_tokens"] or 0 for r in success) / elapsed,
                   "token_count_error_samples": len(errors), "token_count_errors": errors,
                   "retries": sum(max(0, len(r.get("attempts", [])) - 1) for r in rows),
                   "http_attempts": sum(len(r.get("attempts", [None])) for r in rows),
                   "final_gate": state,
                   "max_active": max(e["active"] for e in gate.events),
                   "max_queued": max(e["queued"] for e in gate.events),
                   "max_reserved_tokens": max(e["reserved_tokens"] for e in gate.events)}
        write(out / f"{name}-summary.json", summary)
        return summary
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            if monitor:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)


phase.sequence = 0


async def worker(out):
    plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    summaries = []
    for index, mode in enumerate(plan["order"]):
        summaries.append(await phase(out, f"{index:02d}-{mode}", mode, plan["prompts"], plan["trace"]))
        await asyncio.sleep(0.3)
    write(out / "comparison.json", summaries)


def sample():
    g = L.gpu()
    try:
        with httpx.Client(timeout=3, trust_env=False) as c:
            r = c.get("http://127.0.0.1:19091/api/v1/query", params={"query":
                '{job="vllm",__name__=~"vllm:kv_cache_usage_perc|vllm:num_requests_running|vllm:num_requests_waiting|vllm:num_preemptions_total"}'})
            r.raise_for_status()
            data = r.json()
            if data.get("status") != "success" or not data.get("data", {}).get("result"):
                raise ValueError("missing telemetry")
            g["vllm_prometheus"] = data["data"]["result"]
    except Exception as exc:
        g["error"] = "telemetry_" + type(exc).__name__
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--pilot", action="store_true")
    ap.add_argument("--followup", action="store_true")
    ap.add_argument("--stream-retry", action="store_true")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--only-policy", choices=["token-budget", "token-retry"])
    ap.add_argument("--run-id")
    ap.add_argument("--output-dir", type=Path)
    ap.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if not 1 <= a.rounds <= 25 or (a.stream_retry and not a.followup) or (a.only_policy and not a.stream_retry):
        ap.error("rounds1..25, stream-retry requires --followup")
    if a.worker:
        asyncio.run(worker(a.worker), loop_factory=asyncio.SelectorEventLoop)
        return 0
    if not a.execute:
        print("dry-run: isolated localhost adapter -> existing Gateway; off/basic/token; no service mutation")
        return 0
    if a.output_dir is None:
        ap.error("--output-dir required")
    with J.DiagnosticLease(L.TRIAL_LOCK):
        if J.active_project_loads() != 0 or not J.runtime_stock() or L.gpu_unsafe(sample()):
            raise RuntimeError("overlap/stock/GPU/telemetry preflight blocked")
        if not (L.http("GET", "/readyz")["status"] == 200 and L.chat()["ok"] and L.sse()["ok"]):
            raise RuntimeError("serving preflight failed")
        a.output_dir.mkdir(parents=True, exist_ok=False)
        prompts = token_specs()
        order = ["off", "basic", "token"] if a.pilot else ["off", "basic", "token", "token", "basic", "off"]
        trace = trace_for(prompts, a.pilot)
        bridge = None
        if a.followup:
            from integration_bridge import batch_snapshot
            if J.R.gpu_pods_active() is not False:
                raise RuntimeError("active/unknown k3s GPU Pods; refusing overlap")
            bridge = batch_snapshot(ROOT / ".runtime/vendor/gpu-queue-lab")
            write(a.output_dir / "gpuq-preflight.json", bridge)
            if not bridge["experiment_allowed"]:
                raise RuntimeError("gpuq active GPU batch: do not overlap inference experiment")
            order = ["off-cross", "basic-cross", "token-budget", "token-dynamic"]
            order = order + list(reversed(order)) + order
            if a.stream_retry:
                order = [mode for i in range(a.rounds) for mode in
                         (["token-budget", "token-retry"] if i % 2 == 0 else ["token-retry", "token-budget"])]
                if a.only_policy:
                    order = [a.only_policy] * a.rounds
            trace = trace_for(prompts, False)[:8]
            for i, entry in enumerate(trace):
                # Both lengths appear under both priorities; same trace for every policy.
                entry["priority"] = "high" if (i // 2) % 2 == 0 else "low"
        plan = {"created": L.now(), "model": MODEL, "prompts": prompts,
                "run_id": a.run_id,
                "worker_event_loop": "SelectorEventLoop",
                "trace": trace, "order": order, "followup": a.followup,
                "gate": {"inflight": 2, "queue": 2, "wait_s": .6, "tokens": 400, "long_limit": 1, "aging_s": .2},
                "slo": {"exploratory_ttft_s": 1, "minimum_success": 1,
                        "recovery": "ready200 + actual chat success + SSE DONE + zero adapter leases"},
                "warmup": "preflight chat and SSE excluded; existing model warm, no restart",
                "retry": "disabled; 429 Retry-After=1, no automatic retries",
                "limitations": "small fixed synthetic trace; no saturation/OOM or policy superiority claim",
                "runtime": J.runtime_selected(),
                "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in [Path(__file__), Path(__file__).with_name("http_admission.py"), OPEN_LOOP,
                                            Path(__file__).with_name("safety_harness.py")]}}
        if a.followup:
            plan["gate"].update(tokens=250, long_limit=4)
            plan["cache"] = {"source": "direct vLLM /metrics via existing docker exec", "max_age_s": 4,
                             "pressure_fraction": .85, "failure_action": "hold admission then 429"}
            for filename in ["integration_bridge.py", "bounded_retry.py"]:
                p = Path(__file__).with_name(filename)
                plan["source_sha256"][str(p.relative_to(ROOT))] = hashlib.sha256(p.read_bytes()).hexdigest()
        if a.stream_retry:
            plan["retry"] = {"token-retry": "429-only,2 attempts,2.5s total, Retry-After+jitter0..50ms",
                             "token-budget": "none", "logical_arrivals": "identical fixed trace",
                             "rounds": a.rounds}
        write(a.output_dir / "plan.json", plan)
        result = supervise([sys.executable, str(Path(__file__).resolve()), "--worker", str(a.output_dir.resolve())],
                           a.output_dir / "supervisor", sample, L.gpu_unsafe, cwd=ROOT,
                           journal=a.output_dir / "outcomes.jsonl", wall_seconds=300 if a.stream_retry else 180 if a.followup else 65 if a.pilot else 110)
        recovery = {"ready": L.http("GET", "/readyz"), "chat": L.chat(), "sse": L.sse(),
                    "runtime_stock": J.runtime_stock(), "gpu": L.gpu()}
        write(a.output_dir / "recovery.json", recovery)
        ok = (result["success"] and recovery["ready"]["status"] == 200 and recovery["chat"]["ok"]
              and recovery["sse"]["ok"] and recovery["runtime_stock"])
        print(json.dumps({"output": str(a.output_dir), "ok": ok, "supervisor": result}))
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
