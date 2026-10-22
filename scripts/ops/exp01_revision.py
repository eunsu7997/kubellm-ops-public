"""Exp 1: pin the Qwen revision in Docker Compose; recreate only vLLM; E2E; real rollback to previous args and re-apply.

Run from repo root: .venv/Scripts/python.exe scripts/ops/exp01_revision.py --execute
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import oplib as L  # noqa: E402

COMPOSE = ["docker", "compose", "-f", "docker-compose.vllm-secure.yml", "-f", "docker-compose.operations.yml"]
ENV = {**os.environ, "KUBELLM_SECURE_GRAFANA_PASSWORD": "interpolation-only-not-applied"}
FILE = L.ROOT / "docker-compose.vllm-secure.yml"
REV = "7ae557604adf67be50417f59c2c2f167def9a775"
NAMES = ["gateway", "prometheus", "grafana", "alertmanager", "alert-receiver"]


def snapshot(x, tag):
    ids = x.run(["docker", "ps", "--no-trunc", "--format", "{{.Names}} {{.ID}} {{.Status}}"], label=f"{tag}-ps")
    insp = x.run(["docker", "inspect", "kubellm-gpu-secure-vllm-1", "--format",
                  "{{.Id}} started={{.State.StartedAt}} restarts={{.RestartCount}} args={{json .Args}} ports={{json .NetworkSettings.Ports}}"],
                 label=f"{tag}-vllm-inspect")
    g = L.gpu()
    x.event(tag, gpu=g, vllm=insp["stdout"].strip(), ps=ids["stdout"])
    return ids["stdout"]


def e2e(x, tag):
    r = {"chat": L.chat(), "sse": L.sse(), "readyz": L.http("GET", "/readyz"), "healthz": L.http("GET", "/healthz"),
         "readiness_metrics": L.readiness_metrics(), "gpu": L.gpu()}
    x.event(tag, **{k: (v if k != "chat" else {kk: vv for kk, vv in v.items() if kk != "text"}) for k, v in r.items()})
    return r["chat"]["ok"] and r["sse"]["ok"] and r["readyz"]["status"] == 200


def recreate(x, tag, timeout=420):
    x.run(COMPOSE + ["config", "--quiet"], label=f"{tag}-config-valid", env=ENV, check=True)
    t0 = time.monotonic()
    x.run(COMPOSE + ["up", "-d", "--no-deps", "--force-recreate", "vllm"], label=f"{tag}-up-vllm", env=ENV, timeout=300, check=True)
    samples = []
    ready_at = None
    while time.monotonic() - t0 < timeout:
        g = L.gpu()
        if L.gpu_unsafe(g, min_free=256):
            x.event(f"{tag}-gpu-stop", gpu=g)
        r = L.http("GET", "/readyz", timeout=6)
        samples.append((round(time.monotonic() - t0, 1), r["status"]))
        x.event(f"{tag}-poll", t=round(time.monotonic() - t0, 1), readyz=r["status"], gpu=g)
        # readyz alone is not enough: the Gateway caches readiness for 5 s (found in the first run).
        if r["status"] == 200 and L.chat()["ok"] and L.chat()["ok"]:
            ready_at = round(time.monotonic() - t0, 1)
            break
        time.sleep(3)
    x.event(f"{tag}-ready", ready_at=ready_at, samples=len(samples))
    return ready_at


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--rollback-retest", action="store_true", help="skip A: start from the pinned state, run B then C")
    args = ap.parse_args()
    if not args.execute:
        print("dry-run: would recreate only the vllm service three times (new, old, new)")
        return 0
    x = L.Experiment("01b-rollback-retest" if args.rollback_retest else "01-compose-revision")
    backup = x.dir / "docker-compose.vllm-secure.new.yml"
    shutil.copy(FILE, backup)
    x.run(["git", "diff", "--", "docker-compose.vllm-secure.yml"], label="diff-new")
    before = snapshot(x, "pre")
    x.event("pre-e2e-ok", ok=e2e(x, "pre-e2e"))
    other_before = {l.split()[0]: l.split()[1] for l in before.splitlines() if "vllm-1" not in l and "kubellm-gpu-secure" in l}
    ok = False
    try:
        # A: apply pinned revision
        if args.rollback_retest:
            ok = True
        else:
            ra = recreate(x, "A-new")
            ok = bool(ra) and e2e(x, "A-new-e2e")
            x.run(["docker", "logs", "--tail", "400", "kubellm-gpu-secure-vllm-1"], label="A-vllm-logs")
            snapshot(x, "A-new-post")
        if not ok:
            raise RuntimeError("new revision config did not become ready")
        # B: real rollback to the previous args
        x.run(["git", "show", "HEAD:docker-compose.vllm-secure.yml"], label="old-file")
        old = subprocess.run(["git", "show", "HEAD:docker-compose.vllm-secure.yml"], cwd=L.ROOT, capture_output=True).stdout
        FILE.write_bytes(old)
        rb = recreate(x, "B-rollback")
        okb = bool(rb) and e2e(x, "B-rollback-e2e")
        snapshot(x, "B-rollback-post")
        x.run(["docker", "logs", "--tail", "200", "kubellm-gpu-secure-vllm-1"], label="B-vllm-logs")
        x.event("rollback-result", ok=okb)
    except Exception as exc:  # noqa: BLE001
        x.event("failure", error=str(exc)[:300])
        FILE.write_bytes(subprocess.run(["git", "show", "HEAD:docker-compose.vllm-secure.yml"], cwd=L.ROOT, capture_output=True).stdout)
        rr = recreate(x, "FAIL-restore-old")
        x.event("failure-restore", ready_at=rr, e2e=e2e(x, "FAIL-restore-e2e"))
        return 2
    # C: re-apply pinned revision (final intended state)
    shutil.copy(backup, FILE)
    rc = recreate(x, "C-reapply")
    okc = bool(rc) and e2e(x, "C-reapply-e2e")
    after = snapshot(x, "final")
    other_after = {l.split()[0]: l.split()[1] for l in after.splitlines() if "vllm-1" not in l and "kubellm-gpu-secure" in l}
    x.event("others-unchanged", unchanged=other_before == other_after, before=other_before, after=other_after)
    x.event("done", final_ok=okc)
    print("final_ok", okc)
    return 0 if okc else 1


if __name__ == "__main__":
    raise SystemExit(main())
