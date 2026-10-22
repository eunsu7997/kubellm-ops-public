"""Bounded opt-in Docker EngineCore freeze; original container is always resumed."""
import argparse
import json
import subprocess
import time
from pathlib import Path

import httpx

BACKEND = "kubellm-gpu-secure-vllm-1"
CANDIDATE = "kubellm-readiness-review-20261021"


def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=40, check=check)


def observe():
    result = {}
    for name, url in [("candidate_ready", "http://127.0.0.1:18004/readyz"),
                      ("candidate_live", "http://127.0.0.1:18004/healthz"),
                      ("original_ready", "http://127.0.0.1:18003/readyz")]:
        started = time.monotonic()
        try:
            response = httpx.get(url, timeout=6, trust_env=False)
            result[name] = {"status": response.status_code, "body": response.json(),
                            "seconds": time.monotonic() - started}
        except httpx.HTTPError as exc:
            result[name] = {"error_type": type(exc).__name__}
    health = docker("exec", BACKEND, "python3", "-c",
                    "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health',timeout=3).status)")
    result["vllm_health"] = {"output": health.stdout.strip(), "exit_code": health.returncode}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print("DRY RUN: freeze only the named Docker backend EngineCore, resume within 25s")
        return 0
    args.output_dir.mkdir(parents=True, exist_ok=False)
    process_list = docker("exec", BACKEND, "ps", "-eo", "pid,comm").stdout
    pids = [int(row.split()[0]) for row in process_list.splitlines() if "EngineCor" in row]
    if len(pids) != 1:
        raise RuntimeError("Expected exactly one EngineCore; refusing to signal")
    pid = pids[0]
    before_id = docker("inspect", BACKEND, "--format", "{{.Id}}").stdout.strip()
    start_ticks = docker("exec", BACKEND, "python3", "-c",
                         f"print(open('/proc/{pid}/stat').read().split()[21])").stdout.strip()
    report = {"backend": BACKEND, "container_id": before_id, "engine_pid": pid,
              "before": observe(), "automatic_restart": False}
    if report["before"]["candidate_ready"].get("status") != 200:
        raise RuntimeError("Candidate not ready; refusing fault")
    # Independent in-container safety supervisor survives a killed host-side Python process.
    resume = ("import os,signal; "
              f"p={pid}; expected={start_ticks!r}; "
              "current=open(f'/proc/{p}/stat').read().split()[21]; "
              "os.kill(p,signal.SIGCONT) if current==expected else None")
    safety = "import time; time.sleep(25); " + resume
    docker("exec", "-d", BACKEND, "python3", "-c", safety)
    started = time.monotonic()
    try:
        docker("exec", BACKEND, "python3", "-c", f"import os,signal; os.kill({pid},signal.SIGSTOP)")
        time.sleep(6)  # Expire the candidate's 5s positive readiness cache.
        report["frozen"] = observe()
    finally:
        current_id = docker("inspect", BACKEND, "--format", "{{.Id}}").stdout.strip()
        if current_id != before_id:
            raise RuntimeError("Container identity changed; refusing to signal another container")
        report["resume"] = {"exit_code": docker("exec", BACKEND, "python3", "-c", resume).returncode}
        report["fault_seconds"] = time.monotonic() - started
        time.sleep(6)
        report["after"] = observe()
        response = httpx.post("http://127.0.0.1:18003/v1/chat/completions", json={
            "model": "Qwen/Qwen2.5-0.5B-Instruct", "messages": [{"role": "user", "content": "Reply OK"}],
            "max_tokens": 8, "stream": False,
        }, timeout=20, trust_env=False)
        report["restored_chat"] = {"status": response.status_code, "body": response.json()}
        report["match"] = (report.get("frozen", {}).get("candidate_ready", {}).get("status") == 503
                           and report["after"]["candidate_ready"].get("status") == 200
                           and response.status_code == 200)
        (args.output_dir / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
