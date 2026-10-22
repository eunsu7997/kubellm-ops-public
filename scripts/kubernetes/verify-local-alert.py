"""Explicit opt-in <=25s EngineCore pause; verify actual local alert firing and resolution."""

import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx

BACKEND = "kubellm-gpu-secure-vllm-1"
ALERT = "GatewayInferenceNotReady"


def new_events(before, after):
    """Use event identities, not total count: GET /events returns only its last 100 records."""
    known = {json.dumps(event, sort_keys=True) for event in before.get("events", [])}
    return [event for event in after.get("events", []) if json.dumps(event, sort_keys=True) not in known]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--execute", action="store_true")
    a = p.parse_args()
    if not a.execute:
        print("DRY RUN: verify independent resume helper armed; pause EngineCore; local alert firing/resolution")
        return 0
    a.output_dir.mkdir(parents=True, exist_ok=False)

    def docker(*args, check=True):
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, encoding="utf-8", check=check, timeout=10
        )

    identity = docker("inspect", BACKEND, "--format", "{{.Id}}").stdout.strip()
    rows = docker("exec", BACKEND, "ps", "-eo", "pid,comm").stdout.splitlines()
    pids = [int(r.split()[0]) for r in rows if "EngineCor" in r]
    if len(pids) != 1:
        raise RuntimeError("Expected one EngineCore")
    pid = pids[0]
    ticks = docker(
        "exec", BACKEND, "python3", "-c", f"print(open('/proc/{pid}/stat').read().split()[21])"
    ).stdout.strip()
    marker = "/tmp/kubellm-alert-resume-" + uuid.uuid4().hex + ".json"
    resume = (
        f"import os,signal; p={pid}; expected={ticks!r}; "
        "actual=open(f'/proc/{p}/stat').read().split()[21]; "
        "os.kill(p,signal.SIGCONT) if actual==expected else None"
    )
    helper = (
        "import json,time,pathlib; " + f"m=pathlib.Path({marker!r}); "
        "m.write_text(json.dumps({'state':'armed','delay_seconds':25})); time.sleep(25); "
        + resume
        + "; m.write_text(json.dumps({'state':'resume_attempted','identity_match':actual==expected}))"
    )
    report = {
        "backend": BACKEND,
        "container_id": identity,
        "engine_pid": pid,
        "maximum_helper_delay_seconds": 25,
        "automatic_restart": False,
        "observations": [],
    }
    with httpx.Client(timeout=4, trust_env=False) as c:
        if c.get("http://127.0.0.1:18003/readyz").status_code != 200:
            raise RuntimeError("Gateway not ready before fault")
        before = c.get("http://127.0.0.1:19094/events").json()
        docker("exec", "-d", BACKEND, "python3", "-c", helper)
        for _ in range(20):
            arm = docker("exec", BACKEND, "cat", marker, check=False)
            if arm.returncode == 0 and json.loads(arm.stdout)["state"] == "armed":
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Independent helper did not arm; refusing fault")
        report["helper_armed_before_fault"] = True
        started = time.monotonic()

        def observe():
            row = {"elapsed_seconds": time.monotonic() - started}
            for key, url in [
                ("ready", "http://127.0.0.1:18003/readyz"),
                ("liveness", "http://127.0.0.1:18003/healthz"),
                ("prometheus_alerts", "http://127.0.0.1:19091/api/v1/alerts"),
                ("received_events", "http://127.0.0.1:19094/events"),
            ]:
                try:
                    r = c.get(url)
                    row[key] = {"status": r.status_code, "body": r.json()}
                except (httpx.HTTPError, ValueError) as exc:
                    row[key] = {"error_type": type(exc).__name__}
            report["observations"].append(row)

        try:
            docker("exec", BACKEND, "python3", "-c", f"import os,signal; os.kill({pid},signal.SIGSTOP)")
            while time.monotonic() - started < 21:
                observe()
                time.sleep(0.5)
        finally:
            if docker("inspect", BACKEND, "--format", "{{.Id}}").stdout.strip() != identity:
                raise RuntimeError("Container replaced; do not signal another workload")
            docker("exec", BACKEND, "python3", "-c", resume)
            report["explicit_resume_span_seconds"] = time.monotonic() - started
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                observe()
                events = report["observations"][-1].get("received_events", {}).get("body", {})
                received = new_events(before, events)
                statuses = {
                    event["status"]
                    for event in received
                    if any(alert["labels"].get("alertname") == ALERT for alert in event["alerts"])
                }
                if {"firing", "resolved"}.issubset(statuses):
                    break
                time.sleep(2)
            r = c.post(
                "http://127.0.0.1:18003/v1/chat/completions",
                timeout=20,
                json={
                    "model": "Qwen/Qwen2.5-0.5B-Instruct",
                    "messages": [{"role": "user", "content": "Reply OK"}],
                    "max_tokens": 8,
                },
            )
            report["restored_chat"] = {"status": r.status_code, "body": r.json()}
            report["new_alert_statuses"] = sorted(statuses)
            report["success"] = {"firing", "resolved"}.issubset(statuses) and r.status_code == 200
            report["helper_final"] = json.loads(docker("exec", BACKEND, "cat", marker).stdout)
            (a.output_dir / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "observations"}, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
