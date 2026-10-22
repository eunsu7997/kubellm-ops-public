"""P2 measurements + P3 local evidence report, with the existing Copilot validator.

Deterministic evidence processing, no LLM generation and no external messages.
"""
import argparse
import hashlib
import json
import math
import re
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

try:
    from .integration_bridge import copilot_validate
except ImportError:
    from integration_bridge import copilot_validate


def p99(values):
    return sorted(values)[math.ceil(.99 * len(values)) - 1] if values else None


def analyze(folder):
    folder = Path(folder)
    plan = json.loads((folder / "plan.json").read_text())
    groups = {}
    paths = sorted(folder.glob("*-rows.json"))
    if len(paths) != len(plan["order"]):
        raise ValueError("missing phase results")
    for index, path in enumerate(paths):
        rows = json.loads(path.read_text())
        if (len(rows) != len(plan["trace"]) or
                sorted(r["request_id"] for r in rows) != list(range(len(plan["trace"])))):
            raise ValueError("incomplete/duplicate request accounting")
        name = path.name.removesuffix("-rows.json")
        meta = json.loads((folder / (name + "-summary.json")).read_text())
        if meta["policy"] != plan["order"][index]:
            raise ValueError("policy order mismatch")
        if "counts" in meta and meta["counts"] != dict(Counter(r["category"] for r in rows)):
            raise ValueError("summary request counts disagree")
        for row in rows:
            if row["category"] == "success" and (row["status_code"] != 200 or
                    not isinstance(row["ttft_s"], int | float) or not math.isfinite(row["ttft_s"]) or row["ttft_s"] < 0):
                raise ValueError("invalid successful timing/status")
        g = groups.setdefault(meta["policy"], {"rows": [], "phases": [], "tokens": 0, "seconds": 0})
        g["rows"].extend(rows)
        g["phases"].append(meta)
        g["tokens"] += meta["output_tokens"]
        g["seconds"] += meta["elapsed_including_drain_s"]
    result = {}
    for mode, g in groups.items():
        good = [r for r in g["rows"] if r["category"] == "success"]
        n = len(g["rows"])
        per_priority = {}
        for priority in ["high", "low"]:
            selected = [r for r in g["rows"] if r["priority"] == priority]
            per_priority[priority] = {"scheduled": len(selected),
                "success": sum(r["category"] == "success" for r in selected),
                "successful_ttft_p99_s": p99([r["ttft_s"] for r in selected if r["category"] == "success"])}
        accepted = [r for r in g["rows"] if r["status_code"] == 200]
        rank = math.ceil(.99 * n)
        inclusive = sorted(r["ttft_s"] for r in good)[rank - 1] if rank <= len(good) else None
        attempts = [attempt for row in g["rows"] for attempt in row.get("attempts", [{"status": row["status_code"]}])]
        result[mode] = {"scheduled": n, "success": len(good), "counts": dict(Counter(r["category"] for r in g["rows"])),
                        "success_rate": len(good) / n, "429_rate": sum(r["status_code"] == 429 for r in g["rows"]) / n,
                        "http_attempts": len(attempts), "attempt_amplification": len(attempts) / n,
                        "attempt_429_count": sum(t["status"] == 429 for t in attempts),
                        "max_attempts_per_logical": max(len(row.get("attempts", [None])) for row in g["rows"]),
                        "accepted_failure_rate": (len(accepted) - len(good)) / len(accepted) if accepted else None,
                        "successful_client_ttft_p99_s": p99([r["ttft_s"] for r in good]),
                        "failure_inclusive_p99": {"kind": "finite" if inclusive is not None else "unbounded",
                                                  "seconds": inclusive},
                        "slo_1s_attainment": sum(r["ttft_s"] <= 1 for r in good) / n,
                        "output_tokens_per_observed_second": g["tokens"] / g["seconds"],
                        "per_priority": per_priority,
                        "phase_client_p99_s": [p["client_ttft_p99_s"] for p in g["phases"]],
                        "adapter_arrival_p99_per_phase_s": [p["adapter_arrival_ttft_p99_s"] for p in g["phases"]],
                        "max_active": max(p["max_active"] for p in g["phases"]),
                        "max_reserved_tokens": max(p["max_reserved_tokens"] for p in g["phases"])}
    if len(groups) != len(set(plan["order"])):
        raise ValueError("missing policy phases")
    return result


def safe_access_logs(created, until):
    """Only timestamp/method/path/status; never copy raw container messages or prompt contents."""
    selected = {}
    for name in ["kubellm-gpu-secure-gateway-1", "kubellm-gpu-secure-vllm-1"]:
        r = subprocess.run(["docker", "logs", "--timestamps", "--since", created, "--until", until,
                            "--tail", "2000", name],
                           capture_output=True, text=True, timeout=10, check=True)
        rows = []
        for line in (r.stdout + r.stderr).splitlines():
            match = re.search(r'"(GET|POST) (/(?:readyz|health|metrics|v1/chat/completions)) HTTP/[^" ]+" (\d{3})', line)
            if match:
                rows.append({"timestamp": line.split()[0], "method": match[1], "path": match[2], "status": int(match[3])})
        selected[name] = rows
    selected["capture_window"] = {"since": created, "until": until,
                                   "until_basis": "recovery receipt filesystem write timestamp (upper bound)",
                                   "tail_limit_per_container": 2000, "complete_raw_log_coverage_claimed": False}
    return selected


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run", type=Path)
    ap.add_argument("--copilot-repo", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    a = ap.parse_args()
    result = analyze(a.run)
    plan = json.loads((a.run / "plan.json").read_text())
    recovery = json.loads((a.run / "recovery.json").read_text())
    until = datetime.fromtimestamp((a.run / "recovery.json").stat().st_mtime, UTC).isoformat()
    evidence = {"RUN": {"path": str(a.run / "comparison.json"), "metrics": result},
                "PLAN": {"path": str(a.run / "plan.json"), "model": plan["model"], "order": plan["order"]},
                "GPUQ": json.loads((a.run / "gpuq-preflight.json").read_text()),
                "RECOVERY": recovery, "ACCESS": safe_access_logs(plan["created"], until)}
    analysis = {"suspected_cause": "Small bounded trace completed; policy efficacy under overload is not established.",
                "referenced_log_ids": ["RUN", "PLAN", "GPUQ", "RECOVERY", "ACCESS"],
                "next_steps": ["Keep production serving unchanged; review policy-specific latency and rejection budgets."],
                "unknowns": ["Causal protection benefit, stable population P99 and physical KV reclamation latency are unverified."]}
    validated = copilot_validate(a.copilot_repo, analysis, evidence)
    # Negative control: the validator must reject a fabricated evidence reference.
    negative = copilot_validate(a.copilot_repo, {**analysis, "referenced_log_ids": ["NONEXISTENT"]}, evidence)
    if (not validated["result"]["format_valid"] or validated["result"]["missing_ids"] or
            negative["result"]["missing_ids"] != ["NONEXISTENT"]):
        raise ValueError("Copilot reference validation failed")
    a.output_dir.mkdir(parents=True, exist_ok=False)
    for name, value in {"p2-metrics.json": result, "evidence.json": evidence,
                        "analysis.json": analysis, "copilot-validation.json": validated,
                        "negative-control.json": negative}.items():
        (a.output_dir / name).write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = ["# Local evidence report — deterministic, no LLM generation", "",
             "This run is a bounded comparison, not a production incident. Recovery duration/MTTR is not estimable.", "",
             "| Policy | Success/planned | Success-only client P99 ms | Logical final429 | Output tokens/s |",
             "|---|---:|---:|---:|---:|"]
    for policy, r in result.items():
        ttft = r["successful_client_ttft_p99_s"]
        lines.append(f"| {policy} | {r['success']}/{r['scheduled']} | {ttft * 1000:.1f} | "
                     f"{r['429_rate']:.1%} | {r['output_tokens_per_observed_second']:.2f} |")
    lines += ["", "## Cause candidates", analysis["suspected_cause"], "",
              "## Evidence", "RUN/PLAN/GPUQ/RECOVERY/ACCESS refer to evidence.json. All IDs exist; content correctness still needs review.",
              "", "## Counterevidence", "Uncontrolled policy also succeeds; small samples and desktop GPU contention prevent causal conclusions.",
              "", "## Suggested actions", analysis["next_steps"][0], "", "## Unknowns", analysis["unknowns"][0], "",
              "No LLM, RAG, Slack or automatic remediation was executed. This reuses the actual Incident Copilot validator."]
    (a.output_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest = {str(p.relative_to(a.run)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(a.run.rglob("*")) if p.is_file()}
    (a.output_dir / "input-sha256.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(a.output_dir), "policies": list(result), "copilot_refs_valid": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
