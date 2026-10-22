"""Exp 2: try to reproduce the first-window readiness 503 with the reason metrics available.

Runs: R1 fresh-vLLM first load, R2 warm control, R3 gateway-only restart with warm vLLM, R4 fresh-vLLM again.
Same load shape as the baseline phase 0: 1 req/s Poisson SSE, 256-word prompt, 128 max tokens, max in flight 4, 60 s.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
import oplib as L  # noqa: E402

COMPOSE = ["docker", "compose", "-f", "docker-compose.vllm-secure.yml", "-f", "docker-compose.operations.yml"]
ENV = {**os.environ, "KUBELLM_SECURE_GRAFANA_PASSWORD": "interpolation-only-not-applied"}
KEEP = re.compile(r"^(gateway_backend_ready |gateway_backend_readiness_checks_total|gateway_backend_readiness_failures_total|"
                  r"gateway_backend_readiness_cache_hits_total|gateway_backend_readiness_check_seconds_(sum|count))")


def vllm_metrics():
    p = subprocess.run(["docker", "exec", "kubellm-gpu-secure-vllm-1", "python3", "-c",
                        "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/metrics',timeout=3).read().decode())"],
                       capture_output=True, text=True, timeout=15)
    out = {}
    for line in p.stdout.splitlines():
        for key in ("vllm:num_requests_running", "vllm:num_requests_waiting", "vllm:kv_cache_usage_perc"):
            if line.startswith(key + "{") or line.startswith(key + " "):
                out[key] = line.rsplit(" ", 1)[-1]
    return out


def scraper(path, stop):
    with open(path, "x", encoding="utf-8") as f:
        while not stop.is_set():
            t0 = time.monotonic()
            row = {"ts": L.now()}
            try:
                row["gw"] = [l for l in L.metrics_full().splitlines() if KEEP.match(l)]
            except Exception as exc:  # noqa: BLE001
                row["gw_error"] = type(exc).__name__
            try:
                row["vllm"] = vllm_metrics()
            except Exception as exc:  # noqa: BLE001
                row["vllm_error"] = type(exc).__name__
            f.write(json.dumps(row) + "\n")
            f.flush()
            stop.wait(max(0, 2 - (time.monotonic() - t0)))


def run_phase(x, tag):
    stop = threading.Event()
    th = threading.Thread(target=scraper, args=(x.dir / f"{tag}-scrape.jsonl", stop))
    th.start()
    gpu_before = L.gpu()
    r = x.run([sys.executable, "scripts/load-test/supervised-soak.py", "--output-dir", str(x.dir / tag), "--rates", "1",
               "--phase-seconds", "60", "--max-in-flight", "4", "--max-tokens", "128", "--execute"],
              label=f"{tag}-soak", timeout=400)
    stop.set()
    th.join()
    x.event(f"{tag}-done", exit=r["exit"], gpu_before=gpu_before, gpu_after=L.gpu())
    return r["exit"]


def wait_ready(x, tag, timeout=420):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if L.http("GET", "/readyz", timeout=6)["status"] == 200 and L.chat()["ok"] and L.chat()["ok"]:
            x.event(f"{tag}-ready", seconds=round(time.monotonic() - t0, 1))
            return True
        time.sleep(3)
    x.event(f"{tag}-ready-timeout")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--skip-fresh-first", action="store_true", help="vLLM is already fresh; run R1 without restarting")
    args = ap.parse_args()
    if not args.execute:
        print("dry-run")
        return 0
    x = L.Experiment("02-readiness-503")
    x.event("start", gpu=L.gpu(), vllm_started=x.run(
        ["docker", "inspect", "kubellm-gpu-secure-vllm-1", "--format", "{{.State.StartedAt}}"], label="vllm-started")["stdout"].strip())
    if not args.skip_fresh_first:
        x.run(COMPOSE + ["up", "-d", "--no-deps", "--force-recreate", "vllm"], env=ENV, label="R1-recreate-vllm", timeout=300, check=True)
        if not wait_ready(x, "R1"):
            return 2
    run_phase(x, "R1-fresh-vllm-first-load")
    run_phase(x, "R2-warm-control")
    x.run(COMPOSE + ["up", "-d", "--no-deps", "--force-recreate", "gateway"], env=ENV, label="R3-recreate-gateway", timeout=300, check=True)
    if not wait_ready(x, "R3"):
        return 2
    run_phase(x, "R3-gateway-restart-warm-vllm")
    x.run(COMPOSE + ["up", "-d", "--no-deps", "--force-recreate", "vllm"], env=ENV, label="R4-recreate-vllm", timeout=300, check=True)
    if not wait_ready(x, "R4"):
        return 2
    run_phase(x, "R4-fresh-vllm-second-time")
    x.event("final", chat=L.chat()["ok"], gpu=L.gpu())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
