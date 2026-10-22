"""Read archived CSVs without network access; write a new, exclusive interpretation file.

Historical 'success' retains the historical classifier (SSE DONE was not required).
Request errors are client observations, not proven server failures. Never edit raw evidence.
"""
import argparse
import csv
import hashlib
import json
import math
from collections import Counter
from pathlib import Path


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    rank = (len(values) - 1) * p / 100
    lo, hi = math.floor(rank), math.ceil(rank)
    return values[lo] + (values[hi] - values[lo]) * (rank - lo)


def analyze(path):
    path = Path(path)
    summary_path = path.with_name("summary.json")
    plan = json.loads(summary_path.read_text(encoding="utf-8"))["plan"]
    start, end = float(plan["warmup_s"]), float(plan["duration_s"])
    duration = end - start
    if duration <= 0:
        raise ValueError("positive measurement duration required")
    with path.open(encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["phase"] == "measure"]
    successes = [r for r in rows if r["category"] == "success"]

    def completed_at(r):
        return float(r["send_offset_s"]) + float(r["e2e_s"])

    within = [r for r in successes if start <= completed_at(r) <= end]
    sent = [r for r in rows if r["category"] != "dropped_client_cap"]
    last = max((completed_at(r) for r in sent if r["e2e_s"]), default=end)
    cohort_span = max(end, last) - start
    errors = [r for r in sent if r["category"] != "success"]
    latency = {k: {"p50": percentile([float(r[k]) for r in successes if r[k]], 50),
                   "p95": percentile([float(r[k]) for r in successes if r[k]], 95)}
               for k in ["ttft_s", "e2e_s"]}
    return {
        "source": path.as_posix(),
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "plan_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
        "rate_offered_rps": plan["rate"], "measurement_seconds": duration,
        "categories": dict(Counter(r["category"] for r in rows)),
        "client_observed_sent_failures": len(errors),
        "failure_http_statuses": dict(Counter(r["status_code"] or "none" for r in errors)),
        "success_cohort": len(successes), "success_completed_within_window": len(within),
        "success_completed_after_window": len(successes) - len(within),
        "last_sent_completion_offset_s": last, "cohort_observation_seconds": cohort_span,
        "completion_rate_within_window_rps": len(within) / duration,
        "cohort_completion_rate_including_drain_rps": len(successes) / cohort_span,
        "historical_cohort_divided_by_nominal_seconds_NOT_sustained_rps": len(successes) / duration,
        "tokens_of_requests_completed_within_window_per_nominal_second":
            sum(int(r["completion_tokens"] or 0) for r in within) / duration,
        "latency_success_cohort_seconds_linear_percentiles": latency,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source", type=Path)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    paths = sorted(a.source.rglob("requests.csv"))
    if not paths:
        p.error("no requests.csv found")
    result = {"scope": "OFFLINE reinterpretation; no new traffic or Prometheus queries",
              "notes": ["Historical success did not require SSE DONE; completion marker cannot be proven from CSV.",
                        "Client stream timeouts with HTTP 200 are not established server HTTP failures.",
                        "Within-window and cohort-with-drain rates have different denominators; neither proves capacity.",
                        "Token figure groups whole completed requests, not token emission timestamps.",
                        "Rates/latency are local observations, not production SLO or saturation proof."],
              "runs": [analyze(path) for path in paths]}
    with a.output.open("x", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"Analyzed {len(paths)} preserved CSVs -> {a.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
