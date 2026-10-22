"""Exp 4: real Prometheus -> Alertmanager -> local receiver firing/resolved for GatewayScrapeMissing and VllmScrapeMissing.

A: stop the Gateway container (scrape target disappears) then start it again.
B: pause the vLLM container (metrics scrape times out; engine state is preserved) then unpause it.
Each action has an independent detached helper that restores service after a hard cap even if this process dies.
VllmQueuePersisting is exercised in exp 7 (needs a sustained waiting queue).
"""
import argparse
import json
import os
import subprocess
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(__file__))
import oplib as L  # noqa: E402

PROM = "http://127.0.0.1:19091"
RECV = "http://127.0.0.1:19094"
GW, VLLM = "kubellm-gpu-secure-gateway-1", "kubellm-gpu-secure-vllm-1"
HELPER = "import time,subprocess;time.sleep({s});subprocess.run({cmd})"


def arm_helper(x, label, seconds, cmd):
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen([sys.executable, "-c", HELPER.format(s=seconds, cmd=cmd)], creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    x.event("helper-armed", label=label, pid=p.pid, after_s=seconds, cmd=cmd)
    return p


def alerts():
    try:
        r = httpx.get(PROM + "/api/v1/alerts", timeout=5, trust_env=False).json()["data"]["alerts"]
        return {f'{a["labels"]["alertname"]}': a["state"] for a in r}
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__}


def receiver_events():
    try:
        return httpx.get(RECV + "/events", timeout=5, trust_env=False).json()
    except Exception:  # noqa: BLE001
        return {"retained_count": None, "events": []}


def count_status(events, name, status, since):
    return sum(1 for e in events["events"] if e["received_at"] > since and e["status"] == status
               and any(a["labels"].get("alertname") == name for a in e["alerts"]))


def stable(x, tag, timeout=240):
    t0 = time.monotonic()
    ok_n = 0
    while time.monotonic() - t0 < timeout:
        al = alerts()
        ok = L.http("GET", "/readyz", timeout=6)["status"] == 200 and L.chat()["ok"] and not al
        ok_n = ok_n + 1 if ok else 0
        if ok_n >= 3:
            x.event(f"{tag}-stable", seconds=round(time.monotonic() - t0, 1), alerts=al)
            return True
        time.sleep(4)
    x.event(f"{tag}-not-stable", alerts=alerts())
    return False


def scenario(x, tag, name, down, up, helper_cmd, extra_names=()):
    since = L.now()
    x.event(f"{tag}-start", gpu=L.gpu(), alerts=alerts(), recv_before=receiver_events().get("retained_count"))
    arm_helper(x, tag, 120, helper_cmd)
    x.run(down, label=f"{tag}-inject")
    t0 = time.monotonic()
    fired_at = None
    timeline = []
    while time.monotonic() - t0 < 100:
        al = alerts()
        ev = receiver_events()
        got = count_status(ev, name, "firing", since)
        timeline.append((round(time.monotonic() - t0, 1), al, got))
        x.event(f"{tag}-poll", t=round(time.monotonic() - t0, 1), alerts=al, receiver_firing=got)
        if got and fired_at is None:
            fired_at = round(time.monotonic() - t0, 1)
            time.sleep(6)  # hold a little after delivery
            break
        time.sleep(2)
    x.run(up, label=f"{tag}-restore")
    t1 = time.monotonic()
    resolved_at = None
    while time.monotonic() - t1 < 150:
        al = alerts()
        got = count_status(receiver_events(), name, "resolved", since)
        x.event(f"{tag}-recovery-poll", t=round(time.monotonic() - t1, 1), alerts=al, receiver_resolved=got)
        if got and name not in al:
            resolved_at = round(time.monotonic() - t1, 1)
            break
        time.sleep(2)
    res = {"fired_delivered_after_s": fired_at, "resolved_delivered_after_restore_s": resolved_at,
           "firing_events": count_status(receiver_events(), name, "firing", since),
           "resolved_events": count_status(receiver_events(), name, "resolved", since)}
    for extra in extra_names:
        res[extra + "_firing_events"] = count_status(receiver_events(), extra, "firing", since)
    x.event(f"{tag}-result", **res)
    return res, stable(x, tag)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    if not ap.parse_args().execute:
        print("dry-run: stops the Gateway ~40-60 s, then pauses vLLM ~40-60 s")
        return 0
    x = L.Experiment("04-alert-rules")
    if not stable(x, "pre"):
        return 2
    ra, oka = scenario(x, "A-gateway-scrape-missing", "GatewayScrapeMissing", ["docker", "stop", "--time", "20", GW],
                       ["docker", "start", GW], f'["docker","start","{GW}"]')
    if not oka:
        x.event("abort-after-A")
        return 3
    rb, okb = scenario(x, "B-vllm-scrape-missing", "VllmScrapeMissing", ["docker", "pause", VLLM], ["docker", "unpause", VLLM],
                       f'["docker","unpause","{VLLM}"]', extra_names=("GatewayInferenceNotReady",))
    x.save("result.json", json.dumps({"A": ra, "B": rb, "A_stable": oka, "B_stable": okb}, indent=2))
    print(json.dumps({"A": ra, "B": rb}))
    return 0 if oka and okb else 1


if __name__ == "__main__":
    raise SystemExit(main())
