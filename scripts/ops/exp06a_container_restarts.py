"""Exp 6a: restart each Compose container in place (docker restart) and verify recovery + data persistence."""
import argparse
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(__file__))
import oplib as L  # noqa: E402

P = "kubellm-gpu-secure-"
ORDER = ["alert-receiver", "alertmanager", "prometheus", "grafana", "gateway", "vllm"]
PROM, RECV, GRAFANA = "http://127.0.0.1:19091", "http://127.0.0.1:19094", "http://127.0.0.1:13001"


FIXED_TS = time.time() - 3600  # one fixed instant: a moving "now - 1h" would change with the data


def hist():
    r = httpx.get(PROM + "/api/v1/query", params={"query": "sum(gateway_requests_total)", "time": FIXED_TS},
                  timeout=10, trust_env=False).json()
    return r["data"]["result"]


def probes():
    out = {"chat": L.chat()["ok"], "readyz": L.http("GET", "/readyz", timeout=6)["status"]}
    for name, url in (("prom", PROM + "/-/ready"), ("recv", RECV + "/healthz"), ("grafana", GRAFANA + "/api/health"),
                      ("am", "http://127.0.0.1:19093/-/ready")):
        try:
            out[name] = httpx.get(url, timeout=5, trust_env=False).status_code
        except httpx.HTTPError as exc:
            out[name] = type(exc).__name__
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    if not ap.parse_args().execute:
        print("dry-run: restarts alert-receiver, alertmanager, prometheus, grafana, gateway, vllm one by one")
        return 0
    x = L.Experiment("06a-container-restarts")
    h0 = hist()
    r0 = httpx.get(RECV + "/events", timeout=5, trust_env=False).json()["retained_count"]
    x.event("baseline", history=h0, receiver_events=r0, probes=probes(), gpu=L.gpu())
    ok_all = True
    for svc in ORDER:
        name = P + svc + "-1"
        before = x.run(["docker", "inspect", name, "--format", "{{.Id}} {{.State.StartedAt}}"], label=f"{svc}-before")["stdout"].strip()
        t0 = time.monotonic()
        x.run(["docker", "restart", "--time", "30", name], label=f"{svc}-restart", timeout=200)
        recovered = None
        while time.monotonic() - t0 < 420:
            p = probes()
            if p["chat"] and p["readyz"] == 200 and p["prom"] == 200 and p["recv"] == 200 and p["grafana"] == 200 and p["am"] == 200:
                recovered = round(time.monotonic() - t0, 1)
                break
            time.sleep(3)
        after = x.run(["docker", "inspect", name, "--format", "{{.Id}} {{.State.StartedAt}}"], label=f"{svc}-after")["stdout"].strip()
        extras = {}
        if svc == "prometheus":
            extras["history_unchanged"] = hist() == h0
        if svc == "alert-receiver":
            extras["receiver_events_retained"] = httpx.get(RECV + "/events", timeout=5, trust_env=False).json()["retained_count"] >= r0
        ok = recovered is not None and before.split()[0] == after.split()[0] and before != after
        ok_all &= ok and all(extras.values())
        x.event(f"{svc}-result", recovered_after_s=recovered, same_container_id=before.split()[0] == after.split()[0],
                restarted=before != after, probes=probes(), gpu=L.gpu(), **extras)
        time.sleep(5)
    x.event("final", ok_all=ok_all, ports=x.run(["docker", "ps", "--format", "{{.Names}} {{.Ports}}"], label="ports")["stdout"])
    print("ok_all", ok_all)
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
