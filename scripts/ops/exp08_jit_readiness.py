"""Opt-in cold/warm readiness diagnosis with JIT verbosity; restore stock vLLM in finally.

Same pinned image/model/options, 1 rps cap8 twice, never concurrent with an existing load.
Only the existing vLLM Compose service is recreated; no driver/runtime/WSL/network configuration changes.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import exp07_load as E7
import oplib as L
import restore_stack as R
import yaml
from exp04b_queue_alert import BASE, ENV, REV, restore_helper_code

IMAGE = "vllm/vllm-openai@sha256:8a69ffad015f138d7170c4ddc429e230a3bc1c1719f67e14324749df200a4b90"
ARGS = ["--model", L.MODEL, "--gpu-memory-utilization", "0.4", "--max-model-len", "2048", "--revision", REV]


class DiagnosticLease:
    def __init__(self, path):
        self.path, self.owner, self.keep = path, uuid4().hex, False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("x", encoding="utf-8") as f:
            json.dump({"owner_id": self.owner, "pid": os.getpid(), "started": L.now()}, f)
        return self

    def __exit__(self, *args):
        if not self.keep:
            owner = json.loads(self.path.read_text(encoding="utf-8"))
            if owner.get("owner_id") != self.owner:
                raise RuntimeError("diagnostic lease owner changed; refusing removal")
            self.path.unlink()


def active_project_loads():
    """Only a count is returned; no personal/other-project command lines are retained."""
    if sys.platform != "win32":
        return None
    root = L.ROOT.as_posix().lower().replace("'", "''") + "/"
    script = (f"$loadRoot='{root}'; $loadMatches=@(Get-CimInstance Win32_Process "
              "-Filter \"Name='python.exe' OR Name='python3.exe' OR Name='pythonw.exe'\" -ErrorAction Stop | "
              "Where-Object { $loadCmd=([string]$_.CommandLine).Replace('\\','/').ToLower(); "
              "$loadCmd.Contains($loadRoot) -and ($loadCmd.Contains('scripts/ops/exp07_load.py') -or "
              "$loadCmd.Contains('scripts/load-test/open_loop.py')) }); Write-Output $loadMatches.Count")
    try:
        p = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], capture_output=True,
                           encoding="utf-8", timeout=20)
        return int(p.stdout.strip()) if p.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def assert_stock_config():
    data = yaml.safe_load((L.ROOT / "docker-compose.vllm-secure.yml").read_text())
    v = data["services"]["vllm"]
    if v["image"] != IMAGE or v["command"] != ARGS or v.get("ports"):
        raise ValueError("stock vLLM image/options/publication differ; no mutation allowed")
    if v["deploy"]["resources"]["reservations"]["devices"] != [
        {"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}
    ]:
        raise ValueError("stock GPU reservation differs")


def runtime_selected():
    p = R.docker("container", "inspect", "kubellm-gpu-secure-vllm-1", "--format",
                 '{"image":{{json .Image}},"args":{{json .Args}},"devices":{{json .HostConfig.DeviceRequests}}}',
                 timeout=15)
    if p.returncode:
        return None
    return json.loads(p.stdout)


def runtime_stock(extra_args=()):
    data = runtime_selected()
    return (data is not None and data["image"] == IMAGE.split("@", 1)[1]
            and data["args"] == ["serve", *ARGS, *extra_args]
            and any(d.get("Count") == -1 and ["gpu"] in d.get("Capabilities", []) for d in data["devices"] or []))


class Metrics(threading.Thread):
    def __init__(self, path):
        super().__init__(daemon=True)
        self.path, self.stop_event, self.error = path, threading.Event(), None

    def run(self):
        try:
            with self.path.open("x", encoding="utf-8") as f:
                while not self.stop_event.is_set():
                    row = {"ts": L.now()}
                    row["lines"] = [line for line in L.metrics_full().splitlines()
                                    if line.startswith("gateway_backend_") and not line.startswith("#")]
                    if not row["lines"]:
                        raise ValueError("readiness metrics unavailable")
                    f.write(json.dumps(row) + "\n")
                    f.flush()
                    self.stop_event.wait(1)
        except Exception as exc:
            self.error = type(exc).__name__


def ready_once():
    if L.gpu_unsafe(L.gpu()):
        return False
    ready = L.http("GET", "/readyz", timeout=3)["status"] == 200
    chat = L.http("POST", "/v1/chat/completions", timeout=5, json={
        "model": L.MODEL, "messages": [{"role": "user", "content": "Reply OK"}], "max_tokens": 8})
    return ready and chat["status"] == 200 and '"choices"' in chat.get("text", "")


def recreate(x, tag, extra):
    x.run(BASE + extra + ["config", "--quiet"], env=ENV, check=True, timeout=30, label=tag + "-config")
    x.run(BASE + extra + ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", "vllm"],
          env=ENV, check=True, timeout=240, label=tag + "-recreate")
    end = time.monotonic() + 180
    while time.monotonic() < end:
        if ready_once():
            expected_extra = ["--jit-monitor-verbose"] if extra else []
            exact = runtime_stock(expected_extra)
            x.event(tag + "-ready", exact_runtime_verified=exact, runtime=runtime_selected())
            return exact
        if L.gpu_unsafe(L.gpu()):
            break
        time.sleep(3)
    return False


def collect_logs(x, tag, start, end):
    for name in ["kubellm-gpu-secure-vllm-1", "kubellm-gpu-secure-gateway-1"]:
        cmd = ["docker", "logs", "--timestamps", "--since", start, "--until", end, name]
        p = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=20)
        lines = [line for line in (p.stdout + "\n" + p.stderr).splitlines()
                 if any(k in line for k in ["jit_monitor.py", "Avg prompt throughput", ' /readyz HTTP/1.1" 503', "ERROR"])]
        x.save(tag + "-" + name + "-logs.json", {"command": cmd, "exit": p.returncode,
               "scope": "Selected JIT/throughput/ready503/error lines only, not all compilation events", "lines": lines})
        if p.returncode:
            raise RuntimeError("diagnostic log query failed; cannot claim collected comparison")


def phase(x, tag):
    start = L.now()
    monitor = Metrics(x.dir / (tag + "-readiness-metrics.jsonl"))
    monitor.start()
    try:
        info = E7.run_load(x, tag, 1, 90, 8, seed=6999)
    finally:
        monitor.stop_event.set()
        monitor.join(timeout=12)
    collect_logs(x, tag, start, L.now())
    x.event(tag + "-diagnostic", telemetry_error=monitor.error, telemetry_stopped=not monitor.is_alive())
    if monitor.error or monitor.is_alive():
        info["abort"] = info.get("abort") or "diagnostic metrics collector failed"
    return info


def trial(x, override):
    phases, restored, error = [], False, None
    try:
        if not recreate(x, "jit-verbose", ["-f", str(override)]):
            raise RuntimeError("diagnostic model not ready")
        for tag in ["cold", "warm-control"]:
            info = phase(x, tag)
            phases.append(info)
            if info.get("abort") or info.get("exit") != 0 or not info.get("success") or L.gpu_unsafe(L.gpu()):
                break
        x.event("diagnostic-phases-complete", phases=phases)
    except (Exception, KeyboardInterrupt) as exc:
        error = type(exc).__name__
        x.event("diagnostic-exception", error=type(exc).__name__)
    finally:
        try:
            restored = recreate(x, "restore-stock", [])
        except Exception as exc:
            x.event("restore-stock-exception", error=type(exc).__name__)
        x.event("stock-restore-result", ready=restored)
    return phases, restored, error


def finish_trial(x, override, lease, helper, cancel, result, handshake):
    phases, restored, error = [], False, None
    stock_identity, final, acknowledged, success, complete = False, False, None, False, False
    try:
        phases, restored, error = trial(x, override)
        if restored:
            assert_stock_config()
            stock_identity = runtime_stock()
        final = ready_once() and L.sse(max_tokens=8)["ok"] if restored and stock_identity else False
        x.event("stock-identity-before-helper-cancel", verified=stock_identity, e2e=final)
        if restored and final:
            cancel.write_text("stock model restored and E2E verified")
            helper.wait(timeout=10)
            acknowledged = json.loads(result.read_text())
            if acknowledged.get("outcome") == "cancelled":
                lease.keep = False
        complete = error is None and len(phases) == 2 and all(
            not p.get("abort") and p.get("exit") == 0 and p.get("success") for p in phases)
        success = complete and restored and final and acknowledged and acknowledged.get("outcome") == "cancelled"
    except (Exception, KeyboardInterrupt) as exc:
        error = type(exc).__name__
    finally:
        try:
            x.event("done", diagnostic_complete=bool(complete),
                    restored=restored, e2e=final, helper_acknowledgement=acknowledged,
                    collection_success=bool(success), strict_phase_passes=[E7.phase_ok(p) for p in phases],
                    error=error, stock_identity_verified=stock_identity, helper_still_armed=lease.keep,
                    expected_helper_fire_at_utc=handshake.get("expected_fire_at_utc") if lease.keep else None)
        finally:
            if lease.keep:
                print("WARNING: restore helper remains unconfirmed; no next GPU trial. Expected fire UTC: "
                      + str(handshake.get("expected_fire_at_utc")), file=sys.stderr)
    return 0 if success else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--after-run", type=Path)
    a = ap.parse_args()
    if not a.execute:
        print("DRY RUN: after existing load ends, vLLM-only JIT verbose recreate, 2x90s1rps cap8, finally stock restore")
        return 0
    if a.after_run is None:
        ap.error("--after-run completed evidence directory required; caller must also verify live process termination")
    with DiagnosticLease(L.TRIAL_LOCK) as lease:
        return execute(a, lease)


def execute(a, lease):
    runs = sorted((L.ROOT / "evidence/ops-safety-closure-20261021").glob("safety-fixed-*"),
                  key=lambda p: p.name.rsplit("-", 1)[-1])
    if not runs or a.after_run.resolve() != runs[-1].resolve():
        raise ValueError("after-run must identify the newest safety-fixed run")
    if active_project_loads() != 0:
        raise RuntimeError("live/unknown project load process state; refusing mutation")
    previous = [json.loads(line) for line in (a.after_run / "events.jsonl").read_text().splitlines()]
    if not any(e.get("kind") == "done" for e in previous):
        raise ValueError("previous load has no terminal event; refusing overlap")
    assert_stock_config()
    if not runtime_stock():
        raise ValueError("actual image/options/GPU request differ from stock; refusing changes")
    if R.gpu_pods_active() is not False or L.gpu_unsafe(L.gpu()) or R.state("kubellm-gpu-secure-vllm-1") != "running":
        raise ValueError("GPU conflict, unsafe/unknown state, or existing model unavailable")
    if not R.ports_private([R.P + s + "-1" for s in R.ORDER]):
        raise ValueError("private container state not verified")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    x = L.Experiment("jit-readiness-" + stamp, root=L.ROOT / "evidence/ops-safety-closure-20261021")
    source_names = ["scripts/ops/exp08_jit_readiness.py", "scripts/ops/exp07_load.py", "scripts/ops/safety_harness.py",
                    "scripts/ops/oplib.py", "scripts/ops/exp04b_queue_alert.py", "scripts/load-test/open_loop.py",
                    "scripts/ops/restore_stack.py", "scripts/ops/exp02_readiness503.py", "scripts/ops/exp04_alert_rules.py",
                    "docker-compose.vllm-secure.yml", "docker-compose.operations.yml"]
    hashes = {}
    for name in source_names:
        raw = (L.ROOT / name).read_bytes()
        destination = x.dir / "source-snapshot" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    x.save("source-identity.json", {"snapshot_sha256": hashes, "python": sys.version})
    starts = [e["ts"] for e in previous if e.get("kind") == "soak-start"]
    if starts:
        collect_logs(x, "preserved-long-observation", starts[-1], L.now())
    override = x.save("jit-verbose-override.yaml", {"services": {"vllm": {"command": ARGS + ["--jit-monitor-verbose"]}}})
    armed, cancel, result = (x.dir / n for n in ["helper-armed.json", "helper-cancel.txt", "helper-result.json"])
    code = restore_helper_code(BASE + ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", "vllm"],
                               L.ROOT, armed, cancel, result, 1800)
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    helper = subprocess.Popen([sys.executable, "-c", code], creationflags=flags,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline, handshake = time.monotonic() + 10, None
    while time.monotonic() < deadline and helper.poll() is None:
        try:
            candidate = json.loads(armed.read_text())
            if type(candidate.get("pid")) is int and candidate.get("delay_s") == 1800:
                handshake = candidate
                break
        except (OSError, ValueError, AttributeError):
            pass
        time.sleep(.05)
    if handshake is None:
        cancel.write_text("refuse mutation before arming")
        x.event("helper-NOT-armed")
        return 2
    lease.keep = True  # retain lease on crash/unknown restoration while fallback may still mutate service
    x.event("helper-armed", handshake=handshake, launcher_pid=helper.pid, deadline_seconds=1800,
            expected_fire_at_utc=handshake.get("expected_fire_at_utc"))
    return finish_trial(x, override, lease, helper, cancel, result, handshake)


if __name__ == "__main__":
    raise SystemExit(main())
