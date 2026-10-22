"""Historical v1 cohort/nominal-window analysis. Use reanalyze_load.py for new reports; never overwrite evidence."""
import csv
import glob
import json
import os
import statistics as st
import sys
from datetime import UTC, datetime

import httpx

PROM = "http://127.0.0.1:19091"


def pct(values, q):
    values = sorted(values)
    return values[int(q * (len(values) - 1))] if values else None


def main(directory):
    destination = os.path.join(directory, "step-table.json")
    if os.path.exists(destination):
        raise FileExistsError("preserve existing analysis; use reanalyze_load.py --output NEW_PATH")
    table = []
    for path in sorted(glob.glob(os.path.join(directory, "step*", "requests.csv"))):
        rows = list(csv.DictReader(open(path, encoding="utf-8")))
        ok = [r for r in rows if r["category"] == "success"]
        cats = {}
        for r in rows:
            cats[r["category"]] = cats.get(r["category"], 0) + 1
        sent = [r for r in rows if r["category"] != "dropped_client_cap"]
        tokens = sum(int(r["completion_tokens"] or 0) for r in ok)
        e2e = [float(r["e2e_s"]) for r in ok]
        ttft = [float(r["ttft_s"]) for r in ok]
        table.append({
            "step": os.path.basename(os.path.dirname(path)), "scheduled": len(rows), "categories": cats,
            "server_side_failures": sum(v for k, v in cats.items() if k not in ("success", "dropped_client_cap")),
            "completed_per_s": round(len(ok) / 60, 1), "completion_tokens_per_s": round(tokens / 60),
            "send_lag_p95_s": round(pct([float(r["send_lag_s"]) for r in sent], 0.95), 3),
            "ttft_p50_s": round(st.median(ttft), 3), "ttft_p95_s": round(pct(ttft, 0.95), 2),
            "e2e_p50_s": round(st.median(e2e), 2), "e2e_p95_s": round(pct(e2e, 0.95), 2),
        })
    events = [json.loads(line) for line in open(os.path.join(directory, "events.jsonl"), encoding="utf-8")]
    start = datetime.fromisoformat(events[0]["ts"]).timestamp()
    end = datetime.fromisoformat(events[-1]["ts"]).timestamp()
    window = int(end - start) + 30
    prom = {}
    for name, query in {
        "readiness_failures": f"increase(gateway_backend_readiness_failures_total[{window}s])",
        "readiness_checks": f"increase(gateway_backend_readiness_checks_total[{window}s])",
        "vllm_running_max": f"max_over_time(vllm:num_requests_running[{window}s])",
        "vllm_waiting_max": f"max_over_time(vllm:num_requests_waiting[{window}s])",
        "vllm_kv_cache_max": f"max_over_time(vllm:kv_cache_usage_perc[{window}s])",
    }.items():
        r = httpx.get(PROM + "/api/v1/query", params={"query": query, "time": end}, timeout=10, trust_env=False).json()
        prom[name] = [{"labels": x["metric"], "value": round(float(x["value"][1]), 3)} for x in r["data"]["result"]]
    result = {"window_utc": [datetime.fromtimestamp(start, UTC).isoformat(), datetime.fromtimestamp(end, UTC).isoformat()],
              "steps": table, "prometheus_over_window": prom}
    with open(destination, "x", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    for row in table:
        print(json.dumps(row))
    print(json.dumps(prom))


if __name__ == "__main__":
    main(sys.argv[1])
