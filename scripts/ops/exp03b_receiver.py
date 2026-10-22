"""Exp 3b: isolated Alertmanager + receiver failure/retry/duplicate/persistence trials (synthetic alerts via the AM API).

Uses its own network, containers, volumes and ports (19097 AM, 19098 receiver). The live stack is never touched.
These are synthetic alerts: they prove delivery semantics of this Alertmanager config, not a Prometheus rule firing.
"""
import argparse
import os
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx

sys.path.insert(0, os.path.dirname(__file__))
import oplib as L  # noqa: E402

AM_IMG = "prom/alertmanager@sha256:e9733bafb1bdef9b00e25a21f8f99dc26a22224bf16641ad754d1649f4c3357a"
REC_IMG = "kubellm-alert-receiver:operations-20261021"
NET, AM, REC = "kubellm-rehearsal-am", "kubellm-rehearsal-am-1", "kubellm-rehearsal-receiver-1"
AMV, RECV = "kubellm-rehearsal-am-data", "kubellm-rehearsal-receiver-data"
AMU, RECU = "http://127.0.0.1:19097", "http://127.0.0.1:19098"
CONFIG = """global:
  resolve_timeout: 1m
route:
  receiver: local-evidence
  group_by: [alertname, job]
  group_wait: 1s
  group_interval: 5s
  repeat_interval: 1h
receivers:
  - name: local-evidence
    webhook_configs:
      - url: http://kubellm-rehearsal-receiver-1:8000/alerts
        send_resolved: true
"""
COUNT_PREFIXES = (
    'alertmanager_notifications_total{integration="webhook"',
    'alertmanager_notifications_failed_total{integration="webhook"',
)


def get(url, path):
    return httpx.get(url + path, timeout=10, trust_env=False)


def events():
    try:
        return get(RECU, "/events").json()
    except Exception:  # noqa: BLE001
        return None


def am_counts():
    out = {}
    try:
        for line in get(AMU, "/metrics").text.splitlines():
            if line.startswith(COUNT_PREFIXES):
                out[line.split(" ")[0]] = float(line.split(" ")[-1])
    except Exception as exc:  # noqa: BLE001
        out["error"] = type(exc).__name__
    return out


def post_alert(name, resolved=False):
    now = datetime.now(UTC)
    body = [{"labels": {"alertname": name, "job": "rehearsal", "severity": "warning"},
             "startsAt": (now - timedelta(seconds=30)).isoformat(),
             "endsAt": ((now - timedelta(seconds=1)) if resolved else (now + timedelta(minutes=2))).isoformat()}]
    return httpx.post(AMU + "/api/v2/alerts", json=body, timeout=10, trust_env=False).status_code


def start_receiver(x, label, data_mount):
    x.run(["docker", "rm", "-f", REC], label=label + "-rm")
    return x.run(["docker", "run", "-d", "--name", REC, "--network", NET, "-p", "127.0.0.1:19098:8000", "--read-only",
                  "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "-v", data_mount, REC_IMG],
                 label=label, check=True)


def start_am(x, label, cfg):
    x.run(["docker", "rm", "-f", AM], label=label + "-rm")
    return x.run(["docker", "run", "-d", "--name", AM, "--network", NET, "-p", "127.0.0.1:19097:9093", "-v",
                  f"{cfg}:/etc/alertmanager/alertmanager.yml:ro", "-v", f"{AMV}:/alertmanager", AM_IMG,
                  "--config.file=/etc/alertmanager/alertmanager.yml", "--storage.path=/alertmanager",
                  "--cluster.listen-address="], label=label, check=True)


def wait_http(url, path, timeout=40):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if get(url, path).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False


def count(evts, status, name):
    if not evts:
        return 0
    return sum(1 for e in evts["events"]
               if e["status"] == status and any(a["labels"].get("alertname") == name for a in e["alerts"]))


def resend_until(name, tries=12):
    for _ in range(tries):
        post_alert(name)  # Prometheus keeps re-sending firing alerts; AM retries on the group interval
        time.sleep(3)
        if count(events(), "firing", name):
            break


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    if not ap.parse_args().execute:
        print("dry-run: isolated receiver/Alertmanager failure trials")
        return 0
    x = L.Experiment("03b-receiver-failure-trials")
    cfg = x.save("alertmanager-rehearsal.yml", CONFIG)
    x.run(["docker", "network", "create", NET], label="net", check=True)
    for v in (AMV, RECV):
        x.run(["docker", "volume", "create", v], label="vol-" + v, check=True)
    start_receiver(x, "rec-up", f"{RECV}:/data")
    start_am(x, "am-up", str(cfg))
    if not (wait_http(RECU, "/healthz") and wait_http(AMU, "/-/ready")):
        x.event("not-ready")
        return 2
    r = {}
    # T1 baseline delivery
    post_alert("T1Baseline")
    time.sleep(8)
    r["T1_firing_delivered"] = count(events(), "firing", "T1Baseline")
    x.event("T1", firing=r["T1_firing_delivered"], am=am_counts())
    # T2 duplicates: re-send the same firing alert repeatedly
    for _ in range(6):
        post_alert("T1Baseline")
        time.sleep(2)
    time.sleep(6)
    r["T2_firing_after_resends"] = count(events(), "firing", "T1Baseline")
    x.event("T2-duplicates", firing_events=r["T2_firing_after_resends"], am=am_counts())
    # T3 receiver down -> alert -> retry after recovery
    x.run(["docker", "stop", REC], label="T3-stop-receiver")
    post_alert("T3ReceiverDown")
    time.sleep(20)
    x.event("T3-while-down", am=am_counts())
    start_receiver(x, "T3-receiver-up", f"{RECV}:/data")
    wait_http(RECU, "/healthz")
    resend_until("T3ReceiverDown")
    r["T3_delivered_after_recovery"] = count(events(), "firing", "T3ReceiverDown")
    x.event("T3-after-up", delivered=r["T3_delivered_after_recovery"], am=am_counts())
    # T4 receiver store failure (read-only data volume => HTTP 503) then recovery
    start_receiver(x, "T4-receiver-ro", f"{RECV}:/data:ro")
    wait_http(RECU, "/healthz")
    post_alert("T4StoreReadOnly")
    time.sleep(15)
    ro = httpx.post(RECU + "/alerts", json={"status": "firing", "alerts": [{"labels": {"alertname": "probe"}}]},
                    timeout=5, trust_env=False)
    x.event("T4-ro", direct_post_status=ro.status_code, am=am_counts())
    start_receiver(x, "T4-receiver-rw", f"{RECV}:/data")
    wait_http(RECU, "/healthz")
    resend_until("T4StoreReadOnly")
    r["T4_delivered_after_rw"] = count(events(), "firing", "T4StoreReadOnly")
    x.event("T4-after", delivered=r["T4_delivered_after_rw"], am=am_counts())
    # T5 resolved delivery + receiver recreate persistence
    post_alert("T1Baseline", resolved=True)
    time.sleep(10)
    r["T5_resolved_delivered"] = count(events(), "resolved", "T1Baseline")
    before_restart = (events() or {}).get("retained_count")
    start_receiver(x, "T5-receiver-recreate", f"{RECV}:/data")
    wait_http(RECU, "/healthz")
    r["T5_events_retained_after_recreate"] = (before_restart, (events() or {}).get("retained_count"))
    # T6 Alertmanager restart: persisted notification log should suppress a duplicate firing notification
    n_before = count(events(), "firing", "T3ReceiverDown")
    start_am(x, "T6-am-restart", str(cfg))
    wait_http(AMU, "/-/ready")
    for _ in range(5):
        post_alert("T3ReceiverDown")
        time.sleep(3)
    time.sleep(8)
    n_after = count(events(), "firing", "T3ReceiverDown")
    r["T6_firing_count_before_after_am_restart"] = (n_before, n_after)
    x.event("T6", before=n_before, after=n_after, am=am_counts())
    x.save("final-events.json", events() or {})
    x.event("results", **{k: str(v) for k, v in r.items()})
    for c in (AM, REC):
        x.run(["docker", "rm", "-f", c], label="cleanup-" + c)
    x.run(["docker", "network", "rm", NET], label="cleanup-net")
    for v in (AMV, RECV):
        x.run(["docker", "volume", "rm", v], label="cleanup-vol-" + v)
    print(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
