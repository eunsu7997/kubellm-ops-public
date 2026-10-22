"""Read completed phase artifacts; distinguish unsent requests and sampled readiness from request outcomes."""

import argparse
import csv
import importlib.util
import json
from collections import Counter
from pathlib import Path

spec = importlib.util.spec_from_file_location("open_loop_analysis", Path(__file__).with_name("open_loop.py"))
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    args = p.parse_args()
    root = args.directory
    supervision = json.loads((root / "supervision.json").read_text(encoding="utf-8"))
    phases = supervision["completed"]
    rows = []
    all_counts = Counter()
    for phase in phases:
        all_counts.update(phase["summary"]["categories"])
        if phase["phase"] != 0:
            with (root / f"phase-{phase['phase']:02d}" / "requests.csv").open(newline="", encoding="utf-8") as f:
                rows.extend(csv.DictReader(f))
    counts = Counter(r["category"] for r in rows)
    successes = [r for r in rows if r["category"] == "success"]
    telemetry = [json.loads(line) for line in (root / "gpu-and-ready.jsonl").read_text().splitlines()]
    gpu = [list(map(float, r["gpu_output"].split(","))) for r in telemetry if r["gpu_exit"] == 0]
    latency = {}
    for name in ("ttft_s", "e2e_s", "tpot_s"):
        values = [float(r[name]) for r in successes if r[name]]
        latency[name] = {"count": len(values), **{f"p{pct}": harness.percentile(values, pct) for pct in (50, 95, 99)}}
    result = {
        "complete": supervision["complete"],
        "stop_reason": supervision["stop_reason"],
        "phase_count": len(phases),
        "elapsed_seconds": supervision["elapsed_seconds"],
        "warmup_excluded_phase": 0,
        "all_phase_categories": dict(all_counts),
        "measurement_categories": dict(counts),
        "successful_sent_requests": len(successes),
        "unsent_client_cap": counts["dropped_client_cap"],
        "sent_failures": sum(v for k, v in counts.items() if k not in {"success", "dropped_client_cap"}),
        "latency_success_population_seconds": latency,
        "readiness_sample_codes_all_phases": dict(Counter(str(r.get("ready_status", "error")) for r in telemetry)),
        "gpu_samples": len(gpu),
        "peak_observed_temperature_c": max(r[0] for r in gpu) if gpu else None,
        "minimum_observed_free_vram_mib": min(r[1] for r in gpu) if gpu else None,
        "limitations": [
            f"{len(phases)}x{supervision['plan']['phase_seconds']}s phased observation "
            "includes gaps/drain, not production soak",
            "readiness sample fraction is not time-weighted availability",
            "first whole phase excluded; original per-phase files retain measure labels",
            "client cap drops are unsent, not server rejections",
            "client TPOT is an estimate from usage tokens, not direct per-token timing",
            "background configuration work/builds and prefix cache not controlled",
            "SLO policy and capacity expansion not enabled",
        ],
    }
    output = root / "analysis.json"
    with output.open("x", encoding="utf-8") as f:
        json.dump(result, f, indent=2, allow_nan=False)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
