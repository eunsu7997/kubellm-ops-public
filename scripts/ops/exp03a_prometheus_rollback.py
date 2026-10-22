"""Exp 3a: isolated rehearsal of the Prometheus migration *failure* branch.

The live Prometheus and the preserved original TSDB are never started or written. The original is read-only copied into
rehearsal volumes; two injected faults (truncated chunk file, unreadable permissions) are detected and rolled back to the
rehearsal source volume through a compose override that mirrors persist-monitoring.py's rollback.yml approach.
This does NOT execute the failure branch of persist-monitoring.py itself against the live service.
"""
import argparse
import json
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(__file__))
import oplib as L  # noqa: E402

ORIG = "7cacdfb50a50e7a35ff9baf6e07b2b294aae090feb5b6d5f24479d26b70f9b75"
SRC, DST = "kubellm-rehearsal-prom-src", "kubellm-rehearsal-prom-dst"
IMG = "prom/prometheus:v2.55.1"
FILE = "scripts/ops/rehearsal/prometheus-rehearsal.yml"
URL = "http://127.0.0.1:19096"


def sh(x, label, script, *mounts, user="0"):
    cmd = ["docker", "run", "--rm", "--user", user, "--entrypoint", "/bin/sh"]
    for m in mounts:
        cmd += ["-v", m]
    return x.run(cmd + [IMG, "-ec", script], label=label, timeout=240)


def tree_hash(x, label, volume):
    r = sh(x, label, "cd /v && find . -path ./lock -prune -o -path ./queries.active -prune -o -path ./wal -prune "
           "-o -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum && find . -path ./wal -prune -o -type f | wc -l",
           f"{volume}:/v:ro")
    return r["stdout"].strip().replace("\n", " | ")


def compose(x, label, volume, down=False, extra=None):
    env = {**os.environ, "REHEARSAL_DATA_VOLUME": volume}
    cmd = ["docker", "compose", "-f", FILE] + (extra or [])
    cmd += ["down", "--timeout", "30"] if down else ["up", "-d", "--no-deps", "--force-recreate", "prometheus"]
    return x.run(cmd, label=label, env=env, timeout=120)


def ready(timeout=45):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if httpx.get(URL + "/-/ready", timeout=3, trust_env=False).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False


def history(ts):
    r = httpx.get(URL + "/api/v1/query", params={"query": "sum(gateway_requests_total)", "time": ts}, timeout=10, trust_env=False)
    data = r.json()
    return data.get("data", {}).get("result")


def find_ts():
    r = httpx.get(URL + "/api/v1/query_range", params={"query": "sum(gateway_requests_total)", "start": time.time() - 6 * 3600,
                                                      "end": time.time(), "step": 300}, timeout=20, trust_env=False)
    res = r.json()["data"]["result"]
    return float(res[0]["values"][len(res[0]["values"]) // 2][0]) if res and res[0]["values"] else None


def trial(x, name, fault_script, before, ts):
    x.event(f"{name}-start")
    x.run(["docker", "volume", "rm", DST], label=f"{name}-rm-dst-if-any")
    x.run(["docker", "volume", "create", DST], label=f"{name}-create-dst", check=True)
    sh(x, f"{name}-copy", "cp -a /s/. /d/ && chown -R 65534:65534 /d", f"{SRC}:/s:ro", f"{DST}:/d")
    sh(x, f"{name}-inject", fault_script, f"{DST}:/d")
    src_hash_before = tree_hash(x, f"{name}-src-hash-before", SRC)
    compose(x, f"{name}-up-on-dst", DST)
    ok = ready(30)
    seen = history(ts) if ok else None
    detected = (not ok) or seen != before
    x.event(f"{name}-detect", ready=ok, history_matches=(seen == before), detected_failure=detected)
    x.run(["docker", "logs", "--tail", "40", "kubellm-rehearsal-prometheus-1"], label=f"{name}-dst-logs")
    # rollback: same compose, original(rehearsal-src) volume
    compose(x, f"{name}-rollback-up-on-src", SRC)
    rok = ready(60)
    rseen = history(ts) if rok else None
    src_hash_after = tree_hash(x, f"{name}-src-hash-after", SRC)
    x.event(f"{name}-rollback", ready=rok, history_matches=(rseen == before), src_blocks_unchanged=(src_hash_before.split()[0] == src_hash_after.split()[0]),
            src_hash_before=src_hash_before, src_hash_after=src_hash_after)
    compose(x, f"{name}-down", SRC, down=True)
    x.run(["docker", "volume", "rm", DST], label=f"{name}-rm-dst")
    return detected and rok and rseen == before and src_hash_before.split()[0] == src_hash_after.split()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    if not ap.parse_args().execute:
        print("dry-run: isolated Prometheus rollback rehearsal on copies of the preserved TSDB")
        return 0
    x = L.Experiment("03a-prometheus-rollback-rehearsal" + os.environ.get("EXP_SUFFIX", ""))
    live_id = x.run(["docker", "inspect", "kubellm-gpu-secure-prometheus-1", "--format", "{{.Id}} {{.State.Status}}"], label="live-before")["stdout"].strip()
    orig_before = tree_hash(x, "orig-hash-before", ORIG)
    x.event("orig", hash=orig_before, live=live_id)
    x.run(["docker", "volume", "create", SRC], label="create-src", check=True)
    sh(x, "copy-orig-to-src", "cp -a /s/. /d/ && chown -R 65534:65534 /d", f"{ORIG}:/s:ro", f"{SRC}:/d")
    compose(x, "baseline-up-on-src", SRC)
    if not ready(60):
        x.event("baseline-not-ready")
        return 2
    ts = find_ts()
    before = history(ts) if ts else None
    x.event("baseline", ts=ts, value=before)
    compose(x, "baseline-down", SRC, down=True)
    if not before:
        return 3
    results = {
        "F1-truncated-chunks": trial(x, "F1-truncated-chunks", "for f in $(find /d -path '*/chunks/*' -type f | head -3); do : > $f; done", before, ts),
        "F2-unreadable-permissions": trial(x, "F2-unreadable-permissions", "chown -R 0:0 /d && chmod -R 000 /d", before, ts),
    }
    x.run(["docker", "volume", "rm", SRC], label="rm-src")
    orig_after = tree_hash(x, "orig-hash-after", ORIG)
    live_after = x.run(["docker", "inspect", "kubellm-gpu-secure-prometheus-1", "--format", "{{.Id}} {{.State.Status}}"], label="live-after")["stdout"].strip()
    x.event("final", results=results, original_unchanged=(orig_before.split()[0] == orig_after.split()[0]), live_unchanged=(live_id == live_after))
    print(json.dumps(results))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
