"""Exp 6c: restart Docker Desktop (engine), observe what returns on its own, restore with restore_stack.py, verify.

No Windows reboot. Containers of other projects are only observed; if one that was running does not return on its own it is
started again to restore its prior state (recorded in the events).
"""
import argparse
import os
import subprocess
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(__file__))
import oplib as L  # noqa: E402

PROM, RECV = "http://127.0.0.1:19091", "http://127.0.0.1:19094"
FIXED_TS = None


def ps():
    p = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}|{{.Status}}|{{.Ports}}"], capture_output=True, text=True, timeout=60)
    return p.stdout if p.returncode == 0 else None


def running_names():
    p = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=60)
    return set(p.stdout.split()) if p.returncode == 0 else None


def hist(ts):
    r = httpx.get(PROM + "/api/v1/query", params={"query": "sum(gateway_requests_total)", "time": ts}, timeout=10, trust_env=False).json()
    return r["data"]["result"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    if not ap.parse_args().execute:
        print("dry-run: docker desktop restart; then ordered restore; verification")
        return 0
    x = L.Experiment("06c-docker-desktop-restart")
    ts = time.time() - 1800
    before_running = running_names()
    h0 = hist(ts)
    recv0 = httpx.get(RECV + "/events", timeout=5, trust_env=False).json()["retained_count"]
    x.event("before", running=sorted(before_running), history=h0, receiver_events=recv0, gpu=L.gpu(), ps=ps())
    x.run(["docker", "inspect", "gpu-guard-lab-control-plane", "--format", "{{.HostConfig.RestartPolicy.Name}} {{.State.Status}}"], label="other-project-before")
    t0 = time.monotonic()
    x.run(["docker", "desktop", "restart"], label="desktop-restart", timeout=600)
    up_at = None
    while time.monotonic() - t0 < 600:
        p = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=60)
        if p.returncode == 0 and p.stdout.strip():
            up_at = round(time.monotonic() - t0, 1)
            break
        time.sleep(5)
    x.event("engine-up", seconds=up_at)
    if up_at is None:
        return 2
    time.sleep(20)  # give restart policies time to act
    auto = running_names()
    x.event("after-engine-restart", running_on_their_own=sorted(auto), ps=ps())
    # restore our stack in order
    t1 = time.monotonic()
    r = x.run([sys.executable, "scripts/ops/restore_stack.py", "--execute"], label="restore-stack", timeout=700)
    restored_at = round(time.monotonic() - t1, 1)
    recovered = None
    while time.monotonic() - t1 < 700:
        if L.http("GET", "/readyz", timeout=6)["status"] == 200 and L.chat()["ok"] and L.sse()["ok"]:
            recovered = round(time.monotonic() - t1, 1)
            break
        time.sleep(4)
    # other-project state restore (only if it was running before and is not now)
    other = "gpu-guard-lab-control-plane"
    other_restored = None
    if other in before_running and other not in (running_names() or set()):
        x.run(["docker", "start", other], label="other-project-restore-prior-state")
        other_restored = True
    legacy = x.run(["docker", "ps", "--format", "{{.Names}}|{{.Ports}}"], label="ports-after")["stdout"]
    x.event("result", restore_exit=r["exit"], restore_seconds=restored_at, e2e_recovered_after_s=recovered,
            history_unchanged=hist(ts) == h0,
            receiver_events_retained=httpx.get(RECV + "/events", timeout=5, trust_env=False).json()["retained_count"] >= recv0,
            other_project_restored_by_us=other_restored, gpu=L.gpu(), published_ports=legacy,
            legacy_8002_published="8002" in legacy)
    print(up_at, restored_at, recovered)
    return 0 if r["exit"] == 0 and recovered else 1


if __name__ == "__main__":
    raise SystemExit(main())
