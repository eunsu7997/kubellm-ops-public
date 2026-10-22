"""Opt-in local load experiments using the common fail-closed safety harness.

No SLO policy is enabled. Three consecutive completed sent-request failures stop every mode, including queue trials.
Unsent client-cap records neither increment nor reset that streak. Guard failures and recovery failures are nonzero.
"""
import argparse
import hashlib
import json
import math
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, os.path.dirname(__file__))
import oplib as L  # noqa: E402
from exp02_readiness503 import vllm_metrics  # noqa: E402
from exp04_alert_rules import PROM, receiver_events  # noqa: E402
from safety_harness import supervise  # noqa: E402


def prom_alerts():
    try:
        r = httpx.get(PROM + "/api/v1/alerts", timeout=3, trust_env=False).json()["data"]["alerts"]
        return {a["labels"]["alertname"]: a["state"] for a in r}
    except Exception as exc:
        return {"error": type(exc).__name__}


class Telemetry(threading.Thread):
    """HTTP telemetry cannot block the independent GPU safety thread."""
    def __init__(self, path):
        super().__init__(daemon=True)
        self.path, self.stop_event, self.rows = path, threading.Event(), []
        self.error = None

    def run(self):
        try:
            with self.path.open("x", encoding="utf-8") as f:
                while not self.stop_event.is_set():
                    row = {"ts": L.now()}
                    row["readyz"] = L.http("GET", "/readyz", timeout=3)["status"]
                    row["vllm"] = vllm_metrics()
                    required = ("vllm:num_requests_waiting", "vllm:num_requests_running", "vllm:kv_cache_usage_perc")
                    if any(len(metric_values([row], k)) != 1 for k in required):
                        raise ValueError("required vLLM telemetry unavailable")
                    row["alerts"] = prom_alerts()
                    f.write(json.dumps(row, allow_nan=False) + "\n")
                    f.flush()
                    self.rows.append(row)
                    self.stop_event.wait(5)
        except Exception as exc:
            self.error = "telemetry exception: " + type(exc).__name__


def phase_ok(info):
    return (info.get("success") is True and info.get("exit") == 0 and info.get("abort") is None
            and info.get("sent_failures") == 0 and info.get("successes", 0) > 0
            and info.get("unsent", 0) == 0
            and info.get("telemetry_error") is None and info.get("samples", 0) > 0)


def metric_values(rows, name):
    values = []
    for row in rows:
        try:
            value = float(row.get("vllm", {})[name])
            if math.isfinite(value):
                values.append(value)
        except (KeyError, TypeError, ValueError):
            continue
    return values


def run_load(x, tag, rate, seconds, in_flight, tokens=128, words=256, seed=7000, soak=False, warmup_s=0.0):
    del soak  # every mode uses the same safety policy
    if (not math.isfinite(rate) or not 0 < rate <= 150 or not 1 <= seconds <= 7200
            or not 1 <= in_flight <= 256 or not 1 <= tokens <= 256 or not 1 <= words <= 2048):
        raise ValueError("load plan exceeds explicitly bounded local trial limits")
    gpu = L.gpu()
    if L.gpu_unsafe(gpu):
        return {"tag": tag, "success": False, "exit": None, "abort": "GPU preflight unsafe",
                "sent_failures": 0, "categories": None, "waiting_positive_samples": 0}
    out = x.dir / tag
    progress = x.dir / f"{tag}-outcomes.jsonl"
    cmd = [sys.executable, str(L.ROOT / "scripts/load-test/open_loop.py"),
           "--base-url", L.GATEWAY, "--model", L.MODEL, "--rate", str(rate),
           "--duration-s", str(seconds), "--seed", str(seed), "--high-ratio", "0",
           "--max-in-flight", str(in_flight), "--max-tokens", str(tokens), "--prompt-words", str(words),
           "--timeout", "30", "--warmup-s", str(warmup_s), "--output-dir", str(out),
           "--progress-jsonl", str(progress), "--allow-large-plan", "--execute"]
    telemetry = Telemetry(x.dir / f"{tag}-monitor.jsonl")
    started = L.now()
    source_paths = ["scripts/ops/exp07_load.py", "scripts/ops/safety_harness.py", "scripts/ops/oplib.py",
                    "scripts/ops/exp02_readiness503.py", "scripts/ops/exp04_alert_rules.py",
                    "scripts/load-test/open_loop.py"]
    x.event(f"{tag}-start", command=cmd, gpu=gpu, python_version=sys.version,
            source_sha256={p: hashlib.sha256((L.ROOT / p).read_bytes()).hexdigest() for p in source_paths})
    telemetry.start()
    try:
        guard = supervise(cmd, x.dir / f"{tag}-supervisor", L.gpu, L.gpu_unsafe,
                          cwd=L.ROOT, journal=progress, wall_seconds=seconds + 45,
                          external_abort=lambda: telemetry.error)
    finally:
        telemetry.stop_event.set()
        telemetry.join(timeout=25)
    sp = out / "summary.json"
    summary, sensors = None, []
    try:
        summary = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else None
        with (x.dir / f"{tag}-supervisor/sensor.jsonl").open(encoding="utf-8") as f:
            sensors = [json.loads(line)["gpu"] for line in f]
    except (OSError, ValueError, KeyError) as exc:
        guard["abort"] = guard["abort"] or "result read exception: " + type(exc).__name__
    valid = [g for g in sensors if not L.gpu_unsafe(g)]
    rows = telemetry.rows

    waiting, running, kv = [metric_values(rows, name) for name in
                            ("vllm:num_requests_waiting", "vllm:num_requests_running", "vllm:kv_cache_usage_perc")]
    categories = summary["categories"] if summary else None
    failed = sum(v for k, v in (categories or {}).items() if k not in {"success", "dropped_client_cap"})
    if summary is None:
        guard["abort"] = guard["abort"] or "missing completed summary; durable partial outcomes retained"
    elif guard["outcomes"]["records"] != summary["plan"]["scheduled_requests"]:
        guard["abort"] = guard["abort"] or "progress count differs from scheduled requests"
    if telemetry.error or not waiting:
        guard["abort"] = guard["abort"] or telemetry.error or "no valid vLLM telemetry"
    if telemetry.is_alive():
        guard["abort"] = guard["abort"] or "telemetry thread did not stop"
    info = {"tag": tag, "started": started, "exit": guard["exit"], "abort": guard["abort"],
            "success": guard["success"] and summary is not None and guard["abort"] is None,
            "categories": categories, "sent_failures": failed if summary else guard["outcomes"]["sent_failures"],
            "successes": (categories or {}).get("success", 0),
            "unsent": (categories or {}).get("dropped_client_cap", 0), "telemetry_error": telemetry.error,
            "latency": summary["latency"]["all"] if summary else None,
            "waiting_max": max(waiting, default=None), "waiting_positive_samples": sum(w > 0 for w in waiting),
            "samples": len(waiting), "running_max": max(running, default=None), "kv_cache_max": max(kv, default=None),
            "gpu_temp_max": max((g["temp_c"] for g in valid), default=None),
            "gpu_free_min_mib": min((g["free_mib"] for g in valid), default=None),
            "readyz_non200": sum(r.get("readyz") != 200 for r in rows),
            "alerts_seen": sorted({k for r in rows for k in r.get("alerts", {}) if k != "error"})}
    x.event(f"{tag}-result", **info)
    return info


def recovery_ok(x, tag):
    for _ in range(10):
        g = L.gpu()
        if L.gpu_unsafe(g):
            x.event(f"{tag}-recovery-FAILED", reason="GPU unsafe or unavailable", gpu=g)
            return False
        if L.http("GET", "/readyz", timeout=3)["status"] == 200 and L.chat()["ok"] and L.sse()["ok"]:
            x.event(f"{tag}-recovery-ok", gpu=g)
            return True
        time.sleep(3)
    x.event(f"{tag}-recovery-FAILED", reason="E2E recovery gate failed")
    return False


def mode_warmup(x):
    info = run_load(x, "warmup", 1, 90, 8, seed=6999)
    recovered = recovery_ok(x, "warmup")
    return phase_ok(info) and recovered


def mode_soak(x, minutes):
    info = run_load(x, "soak", 1, minutes * 60, 8, seed=7001)
    recovered = recovery_ok(x, "soak")
    return phase_ok(info) and recovered


def mode_ramp(x):
    results, ok = [], True
    for i, rate in enumerate([2, 4, 8, 16, 32, 64, 100, 150]):
        if not recovery_ok(x, f"pre-step{i}"):
            ok = False
            break
        info = run_load(x, f"step{i:02d}-rate{rate}", rate, 60, 256, seed=7100 + i)
        results.append(info)
        if not phase_ok(info):
            ok = False
            break
        if info["unsent"]:
            x.event("ramp-stopped-offered-load-not-achieved", rate=rate, unsent=info["unsent"])
            ok = False
            break
        if info["waiting_positive_samples"] >= max(3, info["samples"] // 2):
            x.event("ramp-stopped-queue-observed", rate=rate, reason="do not automatically hold/increase queue load")
            break
        time.sleep(15)
    recovered = recovery_ok(x, "ramp-final")
    x.save("ramp-summary.json", results)
    return bool(results) and ok and recovered


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["warmup", "soak", "ramp"])
    ap.add_argument("--minutes", type=int, default=60)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--evidence-root", type=Path, default=L.ROOT / "evidence/ops-safety-closure-20261021")
    a = ap.parse_args()
    if not 1 <= a.minutes <= 120:
        ap.error("minutes must be within [1,120]")
    if not a.execute:
        print("DRY RUN: bounded", a.mode, "with GPU sensor heartbeat and consecutive-failure guard")
        return 0
    if L.TRIAL_LOCK.exists():
        ap.error("service diagnostic lock exists; inspect its live owner/helper before starting another load")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    x = L.Experiment(f"safety-fixed-{a.mode}-{stamp}", root=a.evidence_root)
    if not recovery_ok(x, "pre"):
        return 2
    try:
        ok = {"warmup": mode_warmup, "soak": lambda e: mode_soak(e, a.minutes), "ramp": mode_ramp}[a.mode](x)
    except KeyboardInterrupt:
        x.event("operator-interrupt", result="not passed; load cleanup recorded by supervisor")
        return 130
    except Exception as exc:
        x.event("experiment-exception", error=type(exc).__name__)
        ok = False
    recovered = recovery_ok(x, "final")
    ok = ok and recovered
    x.event("done", ok=ok, recovered=recovered, receiver_events=receiver_events().get("retained_count"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
