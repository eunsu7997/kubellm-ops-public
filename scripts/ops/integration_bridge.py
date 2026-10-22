"""Read-only adapters for the existing gpuq reporter and Incident Copilot validator.

No Kubernetes writes, model/provider calls, Slack or automated remediation.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def batch_snapshot(queue_repo):
    queue_repo = Path(queue_repo).resolve()
    sys.path.insert(0, str(queue_repo))
    from gpuq.report import summarize, team_stats

    command = ["wsl", "-d", "Ubuntu-24.04", "--", "/usr/local/bin/k3s", "kubectl",
               "get", "workloads", "-A", "-o", "json"]
    r = subprocess.run(command, capture_output=True, timeout=20, check=True)
    rows = summarize(json.loads(r.stdout.decode("utf-8")))
    # This is batch-workload admission state, not proof of physical VRAM ownership.
    active = [row for row in rows if row["state"] == "Running" and row["gpus"] > 0]
    result = {"reporter_sha256": digest(queue_repo / "gpuq/report.py"), "command": command,
              "rows": rows, "teams": team_stats(rows), "active_gpu_batches": len(active),
              "experiment_allowed": not active}
    return json.loads(json.dumps(result, default=str))


def copilot_validate(copilot_repo, analysis, log_ids):
    copilot_repo = Path(copilot_repo).resolve()
    sys.path.insert(0, str(copilot_repo))
    from incident_copilot.validator import parse_ai_response, validate

    parsed = parse_ai_response(analysis if isinstance(analysis, str) else json.dumps(analysis, ensure_ascii=False))
    result = validate(parsed, set(log_ids))
    return {"mode": "deterministic offline evidence report; no LLM inference", "result": asdict(result),
            "validator_sha256": digest(copilot_repo / "incident_copilot/validator.py")}


def draft_eligible(validation):
    """Reference/schema checks gate a draft; eligibility is never truth certification or action approval."""
    r = validation["result"]
    analysis = r["parsed"].get("analysis")
    return bool(r["format_valid"] and not r["missing_ids"] and not r["warnings"] and
                analysis and analysis.get("unknowns"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue-repo", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    data = batch_snapshot(a.queue_repo)
    with a.output.open("x", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    print(json.dumps({"active_gpu_batches": data["active_gpu_batches"],
                      "experiment_allowed": data["experiment_allowed"]}))
    return 0 if data["experiment_allowed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
