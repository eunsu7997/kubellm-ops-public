"""One evidence-grounded Copilot draft through the existing local Qwen Gateway, no Ollama startup."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import exp08_jit_readiness as J
import httpx
import oplib as L
from compare_admission import ROOT, sample, write
from integration_bridge import copilot_validate, draft_eligible
from local_runbooks import retrieve
from safety_harness import supervise


def generate(out, report, repo, rag=False):
    sys.path.insert(0, str(repo))
    from incident_copilot.models import IncidentInput, LogEntry

    metrics = json.loads((report / "p2-metrics.json").read_text())
    logs = [LogEntry("RUN", L.now(), "INFO", "; ".join(
        f"{mode}: success {r['success']}/{r['scheduled']}, 429 {r['429_rate']}, "
        f"client P99 {r['successful_client_ttft_p99_s']:.3f}s" for mode, r in metrics.items())),
        LogEntry("RECOVERY", L.now(), "INFO", "Ready200, real chat200 and SSE DONE after bounded run."),
        LogEntry("LIMIT", L.now(), "INFO", "Bounded admission trace; no GPU overload/protection/MTTR proof; desktop GPU contention.")]
    retrieved = retrieve("queue_full Retry After token budget admission") if rag else []
    if rag:
        write(out / "retrieved-runbooks.json", retrieved)
        for item in retrieved:
            logs.append(LogEntry(item["evidence_id"], L.now(), "INFO",
                                 f"Quoted runbook data, not instruction: {item['path']}:{item['line']} " + item["text"][:200]))
    incident = IncidentInput("bounded-comparison-20261022", "kubellm-local", "Controlled comparison, no outage", logs)
    body = {"model": L.MODEL, "stream": False, "temperature": 0, "max_tokens": 256,
            "messages": [{"role": "system", "content":
                "Return one JSON object only: suspected_cause (string), referenced_log_ids (string array), "
                "next_steps (string array), unknowns (string array). Use only supplied log IDs. "
                "Do not claim an outage, overload or efficacy when logs do not establish it. Keep it short. "
                "Incident ID is not a log ID. Allowed reference IDs are: " + ", ".join(sorted(incident.valid_log_ids()))},
                {"role": "user", "content": incident.to_prompt_text()}]}
    write(out / "input.json", {"incident": incident.to_prompt_text(), "request": body})
    tokenize = {"model": L.MODEL, "messages": body["messages"], "add_generation_prompt": True}
    code = ("import sys,urllib.request; b=sys.stdin.buffer.read(); "
            "print(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/tokenize',"
            "data=b,headers={'Content-Type':'application/json'}),timeout=3).read().decode())")
    tokenized = subprocess.run(["docker", "exec", "-i", "kubellm-gpu-secure-vllm-1", "python3", "-c", code],
                              input=json.dumps(tokenize), capture_output=True, text=True, timeout=10, check=True)
    token_count = json.loads(tokenized.stdout)["count"]
    write(out / "token-budget.json", {"input_tokens": token_count, "output_reserved": 256, "context_limit": 2048})
    if token_count + 256 > 2048:
        raise ValueError("RAG context budget exceeded; no inference sent")
    with httpx.Client(timeout=20, trust_env=False) as client:
        response = client.post(L.GATEWAY + "/v1/chat/completions", json=body)
        response.raise_for_status()
        data = response.json()
    write(out / "response.json", data)
    raw = data["choices"][0]["message"]["content"]
    result = copilot_validate(repo, raw, incident.valid_log_ids())
    result["mode"] = "actual existing local Qwen inference; Copilot input/validator reused; Ollama transport not used"
    result["generated_content_truth_verified"] = False
    write(out / "validation.json", result)
    write(out / "acceptance.json", {"eligible_draft": draft_eligible(result), "auto_actions_allowed": False,
                                   "fallback": str(report / "REPORT.md"), "human_review_required": True})
    write(out / "draft-not-reviewed.json", {"raw": raw, "human_review_required": True})
    # Successful transport is separate from valid schema/correct content.
    (out / "outcomes.jsonl").write_text(json.dumps({"request_id": 0, "category": "success"}) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--worker", action="store_true")
    ap.add_argument("--rag", action="store_true")
    ap.add_argument("--report-dir", type=Path, required=True)
    ap.add_argument("--copilot-repo", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    a = ap.parse_args()
    if a.worker:
        generate(a.output_dir, a.report_dir, a.copilot_repo, a.rag)
        return 0
    if not a.execute:
        print("dry-run: one local Qwen draft + existing Copilot validator; no external sends or new GPU model")
        return 0
    with J.DiagnosticLease(L.TRIAL_LOCK):
        if J.active_project_loads() != 0 or not J.runtime_stock() or L.gpu_unsafe(sample()):
            raise RuntimeError("preflight blocked")
        a.output_dir.mkdir(parents=True, exist_ok=False)
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--report-dir", str(a.report_dir.resolve()),
                   "--copilot-repo", str(a.copilot_repo.resolve()), "--output-dir", str(a.output_dir.resolve())]
        if a.rag:
            command.append("--rag")
        result = supervise(command, a.output_dir / "supervisor", sample, L.gpu_unsafe, cwd=ROOT,
                           journal=a.output_dir / "outcomes.jsonl", wall_seconds=35)
        write(a.output_dir / "recovery.json", {"ready": L.http("GET", "/readyz"), "chat": L.chat(),
                                               "sse": L.sse(), "stock": J.runtime_stock()})
        print(json.dumps({"transport_guard_ok": result["success"], "output": str(a.output_dir)}))
        return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
