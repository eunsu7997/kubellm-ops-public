"""Shared helpers for the 2026-10-21 supervised local operations experiments.

Every command is recorded with exit code and output; evidence directories are created exclusively.
"""
import json
import math
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

GATEWAY = "http://127.0.0.1:18003"
MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evidence" / "local-ops-experiments-20261021"
TRIAL_LOCK = ROOT / ".runtime" / "gpu-diagnostic.lock"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Experiment:
    def __init__(self, name, root=EVIDENCE):
        self.dir = root / name
        self.dir.mkdir(parents=True, exist_ok=False)
        self.log = (self.dir / "commands.jsonl").open("x", encoding="utf-8")
        self.events = (self.dir / "events.jsonl").open("x", encoding="utf-8")

    def event(self, kind, **data):
        row = {"ts": now(), "kind": kind, **data}
        self.events.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.events.flush()
        return row

    def run(self, cmd, timeout=300, check=False, label=None, cwd=ROOT, env=None):
        started = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd, env=env)
            code, out, err = p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
        except subprocess.TimeoutExpired as exc:
            code, out, err = -9, (exc.stdout or b"").decode("utf-8", "replace"), "TIMEOUT"
        row = {"ts": now(), "label": label, "cmd": cmd if isinstance(cmd, list) else [cmd], "exit": code,
               "seconds": round(time.monotonic() - started, 2), "stdout": out[-6000:], "stderr": err[-3000:]}
        self.log.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.log.flush()
        if check and code:
            raise RuntimeError(f"{label or cmd} exit {code}: {err[-500:]}")
        return row

    def save(self, name, obj):
        path = self.dir / name
        path.write_text(obj if isinstance(obj, str) else json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
        return path


def gpu():
    try:
        p = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu,memory.used,memory.total,utilization.gpu",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True,
                           encoding="utf-8", timeout=3)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": type(exc).__name__}
    if p.returncode:
        return {"error": "sensor_nonzero_exit", "exit_code": p.returncode}
    try:
        t, used, total, util = [float(x) for x in p.stdout.strip().split(",")]
    except (TypeError, ValueError):
        return {"error": "sensor_invalid_format"}
    if (not all(math.isfinite(v) for v in (t, used, total, util)) or not 0 <= t <= 120
            or not 0 <= used <= total or total <= 0 or not 0 <= util <= 100):
        return {"error": "sensor_invalid_values"}
    return {"temp_c": t, "used_mib": used, "free_mib": total - used, "util": util}


def gpu_unsafe(g, min_free=512, max_temp=80):
    try:
        values = [g[k] for k in ("temp_c", "free_mib", "util", "used_mib")]
        return ("error" in g or not all(math.isfinite(v) for v in values)
                or not 0 <= g["temp_c"] <= 120 or g["free_mib"] < min_free
                or g["used_mib"] < 0 or not 0 <= g["util"] <= 100 or g["temp_c"] >= max_temp)
    except (KeyError, TypeError, ValueError):
        return True


def http(method, path, **kw):
    with httpx.Client(timeout=kw.pop("timeout", 10), trust_env=False) as c:
        started = time.monotonic()
        try:
            r = c.request(method, GATEWAY + path, **kw)
            return {"status": r.status_code, "seconds": round(time.monotonic() - started, 3), "text": r.text[:1500]}
        except Exception as exc:  # noqa: BLE001 - evidence records class only
            return {"status": None, "seconds": round(time.monotonic() - started, 3), "error": type(exc).__name__}


def chat(max_tokens=8, prompt="Say OK"):
    r = http("POST", "/v1/chat/completions", timeout=40,
             json={"model": MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens})
    r["ok"] = r["status"] == 200 and '"choices"' in r.get("text", "")
    return r


def sse(max_tokens=16):
    started = time.monotonic()
    first = None
    done = False
    chunks = 0
    try:
        with httpx.Client(timeout=40, trust_env=False) as c:
            with c.stream("POST", GATEWAY + "/v1/chat/completions", json={
                    "model": MODEL, "stream": True, "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": "Count to three"}]}) as r:
                status = r.status_code
                for line in r.iter_lines():
                    if line.startswith("data:"):
                        chunks += 1
                        if first is None:
                            first = time.monotonic() - started
                        if line.strip() == "data: [DONE]":
                            done = True
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}
    return {"ok": status == 200 and done, "status": status, "chunks": chunks, "ttft_s": round(first or 0, 3),
            "seconds": round(time.monotonic() - started, 3), "done": done}


def readiness_metrics():
    r = http("GET", "/metrics")
    keep = [l for l in r.get("text", "").splitlines() if l.startswith("gateway_backend_") and not l.startswith("#")]
    return keep


def metrics_full():
    with httpx.Client(timeout=10, trust_env=False) as c:
        return c.get(GATEWAY + "/metrics").text


def wait_for(fn, timeout, interval=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    return None
