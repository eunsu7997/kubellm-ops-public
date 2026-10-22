"""Offline analysis of evidence/local-ops-experiments-20261021/02-readiness-503 (read-only on raw files)."""
import collections
import csv
import json
import re
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "evidence" / "local-ops-experiments-20261021" / "02-readiness-503"
LINE = re.compile(r"^(\S+?)(\{(.*)\})? (\S+)$")


def parse(lines):
    out = {}
    for line in lines:
        m = LINE.match(line)
        if m:
            out[m.group(1) + (m.group(2) or "")] = float(m.group(4))
    return out


def delta(first, last):
    return {k: last[k] - first.get(k, 0.0) for k in last if last[k] - first.get(k, 0.0)}


def main():
    destination = ROOT / "analysis.json"
    if destination.exists():
        raise FileExistsError("historical evidence already exists; do not overwrite analysis")
    result = {}
    for scrape in sorted(ROOT.glob("*-scrape.jsonl")):
        tag = scrape.name.removesuffix("-scrape.jsonl")
        rows = [json.loads(line) for line in scrape.read_text(encoding="utf-8").splitlines()]
        gw = [parse(r.get("gw", [])) for r in rows if r.get("gw")]
        d = delta(gw[0], gw[-1]) if gw else {}
        ready = [json.loads(line) for line in (ROOT / tag / "gpu-and-ready.jsonl").read_text(encoding="utf-8").splitlines()]
        status = collections.Counter(r.get("ready_status") for r in ready)
        reqs = list(csv.DictReader((ROOT / tag / "phase-00" / "requests.csv").open(encoding="utf-8")))
        ok = [r for r in reqs if r["category"] == "success"]
        ttft = sorted(float(r["ttft_s"]) for r in ok)
        first10 = [float(r["ttft_s"]) for r in ok[:10]]
        vl = [r.get("vllm", {}) for r in rows]
        running = [float(v["vllm:num_requests_running"]) for v in vl if "vllm:num_requests_running" in v]
        waiting = [float(v["vllm:num_requests_waiting"]) for v in vl if "vllm:num_requests_waiting" in v]
        checks = d.get("gateway_backend_readiness_check_seconds_count", 0)
        result[tag] = {
            "readyz_status_counts": dict(status),
            "readiness_deltas_during_run": d,
            "mean_probe_seconds_during_run": (d.get("gateway_backend_readiness_check_seconds_sum", 0) / checks) if checks else None,
            "requests": dict(collections.Counter(r["category"] for r in reqs)),
            "ttft_s_p50": st.median(ttft) if ttft else None,
            "ttft_s_p95": ttft[int(0.95 * (len(ttft) - 1))] if ttft else None,
            "ttft_s_first10_median": st.median(first10) if first10 else None,
            "ttft_s_after10_median": st.median([float(r["ttft_s"]) for r in ok[10:]]) if len(ok) > 10 else None,
            "vllm_running_max": max(running) if running else None,
            "vllm_waiting_max": max(waiting) if waiting else None,
        }
    with destination.open("x", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    json.dump(result, sys.stdout, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
