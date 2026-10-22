"""Exp 3c: does a *graceful* Alertmanager stop preserve the notification log (no duplicate firing after restart)?

Complements exp03b T6 (hard kill => duplicate). Isolated containers/ports/volumes as in exp03b.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import exp03b_receiver as B  # noqa: E402
import oplib as L  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    if not ap.parse_args().execute:
        print("dry-run")
        return 0
    x = L.Experiment("03c-alertmanager-graceful-restart")
    cfg = x.save("alertmanager-rehearsal.yml", B.CONFIG)
    x.run(["docker", "network", "create", B.NET], label="net", check=True)
    for v in (B.AMV, B.RECV):
        x.run(["docker", "volume", "create", v], label="vol-" + v, check=True)
    B.start_receiver(x, "rec-up", f"{B.RECV}:/data")
    B.start_am(x, "am-up", str(cfg))
    if not (B.wait_http(B.RECU, "/healthz") and B.wait_http(B.AMU, "/-/ready")):
        return 2
    B.post_alert("G1Graceful")
    time.sleep(8)
    before = B.count(B.events(), "firing", "G1Graceful")
    x.run(["docker", "stop", "--time", "30", B.AM], label="am-graceful-stop")
    x.run(["docker", "start", B.AM], label="am-start")
    B.wait_http(B.AMU, "/-/ready")
    for _ in range(6):
        B.post_alert("G1Graceful")
        time.sleep(3)
    time.sleep(8)
    after = B.count(B.events(), "firing", "G1Graceful")
    x.event("result", firing_before_restart=before, firing_after_graceful_restart=after,
            duplicate_after_graceful_restart=after > before)
    x.save("final-events.json", B.events() or {})
    for c in (B.AM, B.REC):
        x.run(["docker", "rm", "-f", c], label="cleanup-" + c)
    x.run(["docker", "network", "rm", B.NET], label="cleanup-net")
    for v in (B.AMV, B.RECV):
        x.run(["docker", "volume", "rm", v], label="cleanup-vol-" + v)
    print(before, after)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
