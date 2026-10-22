"""One bounded five-project control experiment. No production deployment or external messages.

Guard -> gpuq -> existing inference -> upstream SLO -> allowlisted experiment action -> Copilot.
Default is a dry run. Every stage has a durable receipt and the same correlation ID.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import exp08_jit_readiness as J
from compare_admission import direct_cache
from integration_bridge import batch_snapshot
from safety_harness import stop_child
from scale_preview import propose

ROOT = Path(__file__).resolve().parents[2]
SLO_BLOB = "3caad8ed9923cb58f23cb611f7b6e187d96087a7"


def now():
    return datetime.now(UTC).isoformat()


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)


def fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_slo(path):
    raw = Path(path).read_bytes()
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    if blob != SLO_BLOB:
        raise ValueError("llm-slo source differs from reviewed upstream blob")
    spec = importlib.util.spec_from_file_location("pipeline_llmslo", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def action_for(slo, accounting):
    """Only an observed pre-admission429 may activate the pretested local retry policy."""
    if (slo["verdict"] == "FAIL" and accounting["failed"] > 0 and
            accounting["failed"] == accounting["final_429"] and accounting["accepted_failures"] == 0):
        return {"action": "EXPERIMENT_ENABLE_BOUNDED_429_RETRY", "production_mutation": False,
                "reason": "Only explicit admission429 failures observed; never replay accepted streams"}
    return {"action": "NO_ACTION" if slo["verdict"] == "PASS" else "OBSERVE_ONLY",
            "production_mutation": False, "reason": "No allowlisted intervention for this evidence"}


def evaluate(run, module, objectives):
    plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    paths = sorted(run.glob("*-rows.json"))
    if len(paths) != len(plan["order"]):
        raise ValueError("partial run cannot drive control")
    results, raw_rows, offset = [], [], 0
    for path in paths:
        rows = json.loads(path.read_text(encoding="utf-8"))
        if sorted(r["request_id"] for r in rows) != list(range(len(plan["trace"]))):
            raise ValueError("missing/duplicate logical arrivals")
        meta = json.loads((run / path.name.replace("-rows", "-summary")).read_text(encoding="utf-8"))
        for row in rows:
            outcome = ("success" if row["category"] == "success" else "http_error" if row["status_code"] else
                       "timeout" if row["category"] == "timeout" else "transport_error")
            started = offset + row["offset"] + row["send_lag_s"]
            results.append(module.RequestResult(len(results), started, started + row["e2e_s"],
                                                row["status_code"], outcome))
            raw_rows.append(row)
        offset += meta["elapsed_including_drain_s"] + .3
    summary = module.summarize(results)
    verdict = module.evaluate_slo(summary, module.SLO(**objectives), len(plan["trace"]) * len(plan["order"]))
    signature = {k: plan[k] for k in ["trace", "prompts", "runtime"]}
    return {"upstream_slo_blob": SLO_BLOB,
            "latency_semantics": "logical request to full SSE completion E2E, including retry waits; not TTFT",
            "slo": verdict, "summary": summary,
            "accounting": {"planned": len(results), "failed": sum(r["category"] != "success" for r in raw_rows),
                           "final_429": sum(r["status_code"] == 429 for r in raw_rows),
                           "accepted_failures": sum(r["status_code"] == 200 and r["category"] != "success" for r in raw_rows)},
            "trace_runtime_sha256": hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()}


class Pipeline:
    def __init__(self, output, run_id):
        self.output, self.run_id, self.stages = Path(output), run_id, []

    def stage(self, name, function):
        entry = {"stage": name, "run_id": self.run_id, "started_at": now()}
        started = time.perf_counter()
        try:
            value = function()
            entry["result"] = value
            entry["status"] = "BLOCKED" if value.get("allowed") is False else "COMPLETED"
            return value
        except Exception as exc:
            entry.update(status="FAILED", exception=type(exc).__name__)
            raise
        finally:
            entry.update(finished_at=now(), elapsed_s=time.perf_counter() - started)
            self.stages.append(entry)
            save(self.output / (name + ".json"), entry)
            print(json.dumps({"stage": name, "status": entry["status"]}), flush=True)

    def command(self, name, args, timeout):
        child = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace",
                                 env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        output = ""
        try:
            output, _ = child.communicate(timeout=timeout)
        except BaseException:
            stop_child(child)
            output, _ = child.communicate(timeout=5)
            raise
        finally:
            (self.output / (name + ".log")).write_text(output, encoding="utf-8")
            save(self.output / (name + "-command.json"), {"args": args, "exit": child.returncode})
        if child.returncode:
            raise RuntimeError("stage command failed; inspect its preserved log")
        return subprocess.CompletedProcess(args, child.returncode, output, "")


def execute(a):
    output = a.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    pipe = Pipeline(output, uuid4().hex)
    objectives = {"p99_ms_max": 2500, "error_rate_pct_max": 0, "min_requests": a.rounds * 8,
                  "min_latency_samples": 20}
    save(output / "plan.json", {"run_id": pipe.run_id, "created": now(), "rounds": a.rounds,
         "objectives": objectives, "objective_semantics": "exploratory full-SSE E2E <=2.5s and logical errors0; not service SLA",
         "scope": "local experiment only; no Kubernetes writes, production mutation, paid API or external sends",
         "action_allowlist": ["EXPERIMENT_ENABLE_BOUNDED_429_RETRY"],
         "source_sha256": {str(p.relative_to(ROOT)): fingerprint(p) for p in (ROOT / "scripts/ops").glob("*.py")}})
    status, error, baseline_slo, candidate_slo, draft = "FAILED", None, None, None, None
    try:
        module = load_slo(a.slo_source)

        def guard():
            source = ROOT / "k8s/validation/wsl-vllm-trial.yaml"
            deployments = [d for d in yaml.safe_load_all(source.read_text(encoding="utf-8")) if isinstance(d, dict) and d.get("kind") == "Deployment"]
            if len(deployments) != 1:
                raise ValueError("ambiguous Deployment")
            manifest = output / "guard-deployment.yaml"
            manifest.write_text(yaml.safe_dump(deployments[0], sort_keys=False), encoding="utf-8")
            code = "import sys;sys.path.insert(0,sys.argv[1]);from gpu_guard.cli import main;raise SystemExit(main(sys.argv[2:]))"
            result = pipe.command("guard", [sys.executable, "-c", code, str(a.guard_repo / "src"), "check", str(manifest)], 30)
            checks = json.loads(result.stdout)
            return {"allowed": all(r["status"] != "FAIL" for r in checks), "checks": checks,
                    "source_sha256": fingerprint(a.guard_repo / "src/gpu_guard/cli.py"), "mode": "static, no kubectl/apply"}

        if not pipe.stage("01-deploy-guard", guard)["allowed"]:
            status = "BLOCKED"
            return 2
        def queue_state():
            state = batch_snapshot(a.queue_repo)
            state["gpu_pods_clear"] = J.R.gpu_pods_active() is False
            state["allowed"] = state["experiment_allowed"] and state["gpu_pods_clear"]
            return state

        queue = pipe.stage("02-gpuq-state", queue_state)
        if not queue["allowed"] or not queue["experiment_allowed"]:
            status = "BLOCKED"
            return 2

        def serving(policy, folder):
            pipe.command(folder, [sys.executable, str(ROOT / "scripts/ops/compare_admission.py"), "--execute",
                         "--followup", "--stream-retry", "--only-policy", policy, "--rounds", str(a.rounds),
                         "--run-id", pipe.run_id, "--output-dir", str(output / folder)], 330)
            return {"run": folder, "run_id": pipe.run_id, "policy": policy}

        pipe.stage("03-baseline-serving", lambda: serving("token-budget", "baseline"))
        baseline_slo = pipe.stage("04-llmslo-detect", lambda: evaluate(output / "baseline", module, objectives))
        decision = pipe.stage("05-control-decision", lambda: action_for(baseline_slo["slo"], baseline_slo["accounting"]))
        selected = "baseline"
        if decision["action"] == "EXPERIMENT_ENABLE_BOUNDED_429_RETRY":
            pipe.stage("06-candidate-serving", lambda: serving("token-retry", "candidate"))
            candidate_slo = pipe.stage("07-llmslo-verify", lambda: evaluate(output / "candidate", module, objectives))
            if candidate_slo["trace_runtime_sha256"] != baseline_slo["trace_runtime_sha256"]:
                raise ValueError("trace/runtime changed between baseline and intervention")
            selected = "candidate"

        def reports():
            for folder in ["baseline", *(["candidate"] if candidate_slo else [])]:
                pipe.command(folder + "-report", [sys.executable, str(ROOT / "scripts/ops/integration_report.py"),
                             str(output / folder), "--copilot-repo", str(a.copilot_repo),
                             "--output-dir", str(output / (folder + "-report"))], 60)
            return {"selected_evidence_report": selected + "-report/REPORT.md", "llm_generation": False}

        pipe.stage("08-copilot-evidence", reports)

        def llm():
            pipe.command("copilot", [sys.executable, str(ROOT / "scripts/ops/copilot_local_probe.py"), "--execute", "--rag",
                         "--report-dir", str(output / (selected + "-report")), "--copilot-repo", str(a.copilot_repo),
                         "--output-dir", str(output / "copilot")], 60)
            return json.loads((output / "copilot/acceptance.json").read_text(encoding="utf-8"))

        draft = pipe.stage("09-copilot-draft-gate", llm)

        def scaling():
            metric = direct_cache()
            return {"actual_metric": metric, "actual_preview": propose(backlog=0, kv_fraction=metric["kv_fraction"], age_s=0),
                    "capacity_branch": {"synthetic": True, "result": propose(backlog=4, kv_fraction=.9, age_s=0)}}

        pipe.stage("10-scale-capacity-preview", scaling)
        status = "COMPLETED_WITH_LIMITATIONS"
        return 0
    except Exception as exc:
        error = type(exc).__name__
        return 1
    finally:
        save(output / "SUMMARY.json", {"run_id": pipe.run_id, "status": status, "error": error, "stages": pipe.stages,
             "baseline_slo": baseline_slo["slo"] if baseline_slo else None,
             "candidate_slo": candidate_slo["slo"] if candidate_slo else None,
             "llm_draft_eligible": draft.get("eligible_draft") if draft else None,
             "production_changed": False, "scale_out_executed": False,
             "limitations": ["one GPU, bounded trace, no proven GPU-overload protection or production five-service deployment",
                             "SLO is full-SSE logical E2E, separate from1s TTFT scenario",
                             "LLM draft is advisory and gated; whole llm-slo-project QA not executed"]})
        print(json.dumps({"run_id": pipe.run_id, "status": status, "output": str(output)}), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--output-dir", type=Path)
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--guard-repo", type=Path, default=ROOT.parent / "gpu-deploy-guard")
    ap.add_argument("--queue-repo", type=Path, default=ROOT / ".runtime/vendor/gpu-queue-lab")
    ap.add_argument("--copilot-repo", type=Path, default=ROOT.parent / "incident-copilot")
    ap.add_argument("--slo-source", type=Path, default=ROOT / "evidence/integration-final-20261022/llm-slo-source/metrics.py")
    a = ap.parse_args()
    if not 3 <= a.rounds <= 10:
        ap.error("bounded rounds3..10 required")
    if not a.execute:
        print("dry-run: Guard -> gpuq -> inference -> llm-slo -> allowlisted local action -> Copilot; no production mutation")
        return 0
    if a.output_dir is None:
        ap.error("new --output-dir required")
    with J.DiagnosticLease(ROOT / ".runtime/five-module-pipeline.lock"):
        return execute(a)


if __name__ == "__main__":
    raise SystemExit(main())
