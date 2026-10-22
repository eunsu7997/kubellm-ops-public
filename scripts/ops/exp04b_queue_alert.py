"""Exp 4b: real VllmQueuePersisting firing/resolved.

The previous ramp did not observe a vLLM waiting queue (the bottleneck was not established), so the rule is exercised by
*temporarily* recreating vLLM with --max-num-seqs 2 through a compose override stored in the evidence directory, offering a
load above that service rate for 120 s, then restoring the repository configuration. A detached helper restores the stock
configuration after a hard cap even if this process dies.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(__file__))
import exp07_load as E7  # noqa: E402
import oplib as L  # noqa: E402
from exp04_alert_rules import count_status, receiver_events, stable  # noqa: E402

REV = "7ae557604adf67be50417f59c2c2f167def9a775"
BASE = ["docker", "compose", "-f", "docker-compose.vllm-secure.yml", "-f", "docker-compose.operations.yml"]
ENV = {**os.environ, "KUBELLM_SECURE_GRAFANA_PASSWORD": "interpolation-only-not-applied"}
OVERRIDE = """services:
  vllm:
    command: ["--model", "Qwen/Qwen2.5-0.5B-Instruct", "--gpu-memory-utilization", "0.4", "--max-model-len", "2048",
              "--revision", "REVISION", "--max-num-seqs", "2"]
""".replace("REVISION", REV)


def restore_helper_code(command, root, armed, cancel, result, delay):
    """Handshake proves the helper reached its wait loop; cancellation is acknowledged on disk."""
    return f'''import json,os,subprocess,time
from datetime import datetime,timedelta,timezone
from pathlib import Path
armed,cancel,result=map(Path,{[str(armed), str(cancel), str(result)]!r})
armed_time=datetime.now(timezone.utc)
with armed.open("x",encoding="utf-8") as f: json.dump({{"pid":os.getpid(),"delay_s":{delay!r},"armed_at_utc":armed_time.isoformat(),"expected_fire_at_utc":(armed_time+timedelta(seconds={delay!r})).isoformat()}},f)
deadline=time.monotonic()+{delay!r}
while time.monotonic()<deadline and not cancel.exists(): time.sleep(0.1)
if cancel.exists():
    outcome={{"outcome":"cancelled","exit":0}}
else:
    try:
        p=subprocess.run({command!r},cwd={str(root)!r},env=dict(os.environ,KUBELLM_SECURE_GRAFANA_PASSWORD="interpolation-only-not-applied"),timeout=300,capture_output=True)
        outcome={{"outcome":"restore-attempted","exit":p.returncode}}
    except Exception as exc: outcome={{"outcome":"restore-exception","error":type(exc).__name__}}
with result.open("x",encoding="utf-8") as f: json.dump(outcome,f)
'''


def recreate(x, tag, extra):
    x.run(BASE + extra + ["config", "--quiet"], label=f"{tag}-config", env=ENV, check=True)
    x.run(BASE + extra + ["up", "-d", "--no-deps", "--force-recreate", "vllm"], label=f"{tag}-up", env=ENV, timeout=300, check=True)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 420:
        if L.http("GET", "/readyz", timeout=6)["status"] == 200 and L.chat()["ok"] and L.chat()["ok"]:
            x.event(f"{tag}-ready", seconds=round(time.monotonic() - t0, 1))
            return True
        time.sleep(3)
    x.event(f"{tag}-ready-timeout")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    if not ap.parse_args().execute:
        print("dry-run: temporary vLLM recreate with --max-num-seqs 2, 120 s overload, restore")
        return 0
    x = L.Experiment("safety-fixed-queue-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
    override = x.save("override-max-num-seqs-2.yml", OVERRIDE)
    if not stable(x, "pre"):
        return 2
    armed, cancel, helper_result = (x.dir / n for n in ["helper-armed.json", "helper-cancel.txt", "helper-result.json"])
    helper_code = restore_helper_code(BASE + ["up", "-d", "--no-deps", "--force-recreate", "vllm"],
                                      L.ROOT, armed, cancel, helper_result, 1800)
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen([sys.executable, "-c", helper_code], creationflags=flags, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    deadline = time.monotonic() + 10
    handshake = None
    while p.poll() is None and time.monotonic() < deadline:
        try:
            candidate = json.loads(armed.read_text(encoding="utf-8"))
            if type(candidate.get("pid")) is int and candidate.get("delay_s") == 1800:
                handshake = candidate
                break
        except (OSError, ValueError, AttributeError):
            pass
        time.sleep(0.05)
    if handshake is None:
        cancel.write_text("cancel before service changes", encoding="utf-8")
        x.event("restore-helper-NOT-armed", launcher_pid=p.pid)
        return 3
    x.event("restore-helper-armed", launcher_pid=p.pid, handshake=handshake, after_s=1800)
    since = L.now()
    ok = False
    try:
        if not recreate(x, "temp-config", ["-f", str(override)]):
            raise RuntimeError("temporary config did not become ready")
        x.event("temp-args", args=x.run(["docker", "inspect", "kubellm-gpu-secure-vllm-1", "--format", "{{json .Args}}"],
                                         label="temp-args")["stdout"].strip())
        info = E7.run_load(x, "overload", 4, 120, 64, seed=7300)
        x.event("overload-done", waiting_positive_samples=info.get("waiting_positive_samples"), abort=info["abort"])
        for _ in range(40):
            time.sleep(5)
            ev = receiver_events()
            if count_status(ev, "VllmQueuePersisting", "resolved", since):
                break
        ok = E7.phase_ok(info)
    except Exception as exc:
        x.event("experiment-exception", error=type(exc).__name__)
        ok = False
    finally:
        try:
            restored = recreate(x, "restore-stock-config", [])
        except Exception as exc:
            restored = False
            x.event("restore-exception-helper-still-armed", error=type(exc).__name__)
        x.event("restored", ready=restored,
                args=x.run(["docker", "inspect", "kubellm-gpu-secure-vllm-1", "--format", "{{json .Args}}"],
                           label="restored-args")["stdout"].strip())
    stable_ok = stable(x, "final")
    if restored and stable_ok:
        cancel.write_text("stock restored and E2E stable", encoding="utf-8")
        try:
            p.wait(timeout=10)
            acknowledged = json.loads(helper_result.read_text())
        except (subprocess.TimeoutExpired, OSError, ValueError):
            acknowledged = {"outcome": "cancel-not-acknowledged"}
        x.event("restore-helper-cancellation", launcher_pid=p.pid, acknowledgement=acknowledged)
        if acknowledged.get("outcome") != "cancelled":
            stable_ok = False
    else:
        x.event("restore-helper-still-armed", pid=p.pid, after_s=1800)
    ev = receiver_events()
    firing = count_status(ev, "VllmQueuePersisting", "firing", since)
    resolved = count_status(ev, "VllmQueuePersisting", "resolved", since)
    success = ok and restored and stable_ok and firing > 0 and resolved > 0
    x.event("result", queue_alert_firing=firing, queue_alert_resolved=resolved,
            restored=restored, stable=stable_ok, success=success, load_phase_ok=ok,
            alert_delivery_verified=firing > 0 and resolved > 0)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
