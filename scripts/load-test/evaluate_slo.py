"""Offline, failure-inclusive SLO accounting for open_loop.py CSVs; never sends requests.

A valid analysis is not a successful experiment or evidence of admission-control benefit.
Historical classifiers without the SSE DONE contract require explicit exploratory opt-in.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import math
import re
from collections import Counter
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location("slo_schedule_source", Path(__file__).with_name("open_loop.py"))
generator = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(generator)

CATEGORIES = {"success", "stream_incomplete", "invalid_response", "timeout", "connection_error",
              "stream_interrupted", "transport_error", "dropped_client_cap", "rate_limited",
              "http_502", "http_504", "http_5xx", "http_4xx", "redirect"}


def number(value, field, *, positive=False, signed=False):
    if isinstance(value, bool):
        raise ValueError(f"{field}: expected a finite number")
    if isinstance(value, str) and not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value):
        raise ValueError(f"{field}: expected a decimal number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field}: expected a finite number") from None
    if not math.isfinite(result) or (not signed and result < 0) or (positive and result <= 0):
        raise ValueError(f"{field}: invalid range")
    return result


def integer(value, field, *, minimum=0):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)) or int(value) < minimum:
        raise ValueError(f"{field}: expected an integer >= {minimum}")
    return int(value)


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("summary contains duplicate JSON keys")
        result[key] = value
    return result


def load_run(directory):
    """Validate the full planned cohort, including warmup and unsent arrivals."""
    directory = Path(directory)
    csv_bytes = (directory / "requests.csv").read_bytes()
    summary_bytes = (directory / "summary.json").read_bytes()
    summary = json.loads(summary_bytes, object_pairs_hook=unique_json)
    if not isinstance(summary, dict) or not isinstance(summary.get("plan"), dict):
        raise ValueError("summary.plan must be an object")
    plan = summary["plan"]
    duration = number(plan.get("duration_s"), "duration_s", positive=True)
    warmup = number(plan.get("warmup_s"), "warmup_s")
    rate = number(plan.get("rate"), "rate", positive=True)
    ratio = number(plan.get("high_ratio"), "high_ratio")
    seed = plan.get("seed")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    total = integer(plan.get("scheduled_requests"), "scheduled_requests")
    if warmup >= duration or ratio > 1:
        raise ValueError("invalid warmup or high_ratio")
    burst = plan.get("burst")
    if burst is not None:
        if not isinstance(burst, list) or len(burst) != 3:
            raise ValueError("burst must be [start, length, multiplier]")
        burst = [number(v, "burst", positive=i > 0) for i, v in enumerate(burst)]
        if not math.isfinite(rate * burst[2]):
            raise ValueError("invalid burst")
    with io.StringIO(csv_bytes.decode("utf-8-sig"), newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != generator.CSV_FIELDS:
            raise ValueError("CSV header does not match the current request schema")
        raw_rows = list(reader)
    if len(raw_rows) != total:
        raise ValueError("CSV cohort count differs from scheduled_requests")
    schedule = generator.build_schedule(rate, duration, seed, burst, ratio, warmup, limit=total)
    if len(schedule) != total:
        raise ValueError("recorded plan does not reproduce the cohort count")
    expected = {r["request_id"]: r for r in schedule}
    rows, seen = [], set()
    for raw in raw_rows:
        if None in raw or any(v is None for v in raw.values()):
            raise ValueError("malformed CSV row")
        row = dict(raw)
        rid = integer(row["request_id"], "request_id", minimum=1)
        if rid in seen or rid not in expected:
            raise ValueError("duplicate or unexpected request_id")
        seen.add(rid)
        target = expected[rid]
        offset = number(row["scheduled_offset_s"], "scheduled_offset_s")
        if (row["priority"] != target["priority"] or row["phase"] != target["phase"]
                or not math.isclose(offset, target["scheduled_offset_s"], rel_tol=0, abs_tol=1e-9)):
            raise ValueError("CSV priority/phase/offset differs from the seeded plan")
        category = row["category"]
        if category not in CATEGORIES:
            raise ValueError("unknown request category")
        row["send_offset_s"] = number(row["send_offset_s"], "send_offset_s")
        row["send_lag_s"] = number(row["send_lag_s"], "send_lag_s", signed=True)
        if not math.isclose(row["send_offset_s"] - offset, row["send_lag_s"], abs_tol=1e-6):
            raise ValueError("inconsistent send lag")
        for field in ("ttft_s", "e2e_s", "tpot_s"):
            row[field] = None if row[field] == "" else number(row[field], field)
        for field in ("chunks", "completion_tokens", "status_code"):
            row[field] = None if row[field] == "" else integer(row[field], field)
        status = row["status_code"]
        if status is not None and not 100 <= status <= 599:
            raise ValueError("invalid HTTP status")
        if category in {"rate_limited", "http_502", "http_504", "http_5xx", "http_4xx", "redirect"}:
            if status is None or 200 <= status < 300 or generator.status_category(status) != category:
                raise ValueError("HTTP failure category differs from recorded status")
        if category == "success" and (status is None or not 200 <= status < 300
                or row["ttft_s"] is None or row["e2e_s"] is None or not row["chunks"]
                or row["ttft_s"] > row["e2e_s"]):
            raise ValueError("success row lacks a valid status/content/timing record")
        if category == "dropped_client_cap" and any(row[f] is not None for f in (
                "status_code", "ttft_s", "e2e_s", "chunks", "completion_tokens", "tpot_s")):
            raise ValueError("unsent row contains a backend result")
        row["request_id"] = rid
        rows.append(row)
    counts = dict(Counter(r["priority"] for r in rows))
    recorded = plan.get("scheduled_by_priority")
    if (not isinstance(recorded, dict) or set(recorded) != {"high", "low"}
            or any(integer(recorded[k], "scheduled_by_priority") != counts.get(k, 0) for k in recorded)):
        raise ValueError("plan priority counts differ from CSV")
    expected_summary = generator.summarize({**plan, "duration_s": duration, "warmup_s": warmup}, rows)
    for field in ("measurement_window_s", "warmup_requests_excluded", "categories", "priorities",
                  "categories_by_priority", "goodput_rps", "max_send_lag_s", "latency"):
        if field not in summary or not same_json_number_structure(summary[field], expected_summary[field]):
            raise ValueError(f"summary.{field} differs from CSV recomputation")
    if summary.get("success_requires_sse_done") is True:
        known_notes = {generator.CATEGORY_NOTES["success"], "HTTP 200, content observed, and SSE [DONE] received"}
        notes = summary.get("category_notes")
        if not isinstance(notes, dict) or notes.get("success") not in known_notes:
            raise ValueError("DONE declaration conflicts with known recorder contract notes")
    hashes = {"requests.csv": hashlib.sha256(csv_bytes).hexdigest(),
              "summary.json": hashlib.sha256(summary_bytes).hexdigest(),
              "analyzer": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "current_schedule_source": hashlib.sha256(Path(generator.__file__).read_bytes()).hexdigest()}
    summary["plan"] = {**plan, "duration_s": duration, "warmup_s": warmup}
    return rows, summary, hashes


def same_json_number_structure(actual, expected):
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and actual.keys() == expected.keys()
                and all(same_json_number_structure(actual[k], v) for k, v in expected.items()))
    if type(expected) in (int, float):
        return (type(actual) in (int, float) and math.isfinite(actual)
                and math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12))
    return actual == expected


def group_metrics(rows, threshold, duration, warmup, verified):
    """All measured arrivals form the denominator; missing/failed outcomes cannot disappear."""
    n = len(rows)
    successful = [r for r in rows if r["category"] == "success"]
    compliant = [r for r in successful if r["ttft_s"] <= threshold]
    times = sorted(r["ttft_s"] for r in successful)
    rank = (95 * n + 99) // 100 - 1
    bounded = n > 0 and rank < len(times)
    within = sum(warmup <= r["send_offset_s"] + r["e2e_s"] <= duration for r in compliant)
    quantile = {"kind": "no_samples" if not n else "finite" if bounded else "unbounded",
                "seconds": times[rank] if bounded else None,
                "method": "nearest rank ceil(0.95*N); all non-successes ordered at positive infinity"}
    rates = {"cohort_slo_successes_per_nominal_second": len(compliant) / (duration - warmup) if n else None,
             "slo_successes_completed_in_window_per_second": within / (duration - warmup) if n else None}
    return {
        "measurement_arrivals": n, "categories": dict(Counter(r["category"] for r in rows)),
        "successful": len(successful), "slo_compliant_successes": len(compliant),
        "attainment_fraction": len(compliant) / n if n and verified else None,
        "exploratory_classifier_attainment_fraction": len(compliant) / n if n else None,
        "evidence_status": "NO_SAMPLES" if not n else "OBSERVED" if verified else "LEGACY_UNVERIFIED",
        "max_send_lag_s": max((r["send_lag_s"] for r in rows), default=None),
        "min_send_lag_s": min((r["send_lag_s"] for r in rows), default=None),
        "early_send_count": sum(r["send_lag_s"] < 0 for r in rows),
        "success_only_ttft_p95_s": generator.percentile(times, 95) if verified else None,
        "failure_inclusive_ttft_p95": quantile if verified else {"kind": "legacy_unverified", "seconds": None},
        **{k: v if verified else None for k, v in rates.items()},
        "exploratory_classifier_metrics": None if verified else {
            "success_only_ttft_p95_s": generator.percentile(times, 95),
            "failure_inclusive_ttft_p95": quantile, **rates},
        "compliant_successes_completed_after_window": len(compliant) - within,
    }


def analyze(directory, slo_ttft_s=None, *, allow_legacy_success=False):
    rows, summary, hashes = load_run(directory)
    plan = summary["plan"]
    configured = plan.get("slo_ttft_s")
    if configured is not None:
        configured = number(configured, "plan.slo_ttft_s", positive=True)
    threshold = configured if slo_ttft_s is None else number(slo_ttft_s, "slo_ttft_s", positive=True)
    if threshold is None:
        raise ValueError("supply --slo-ttft-s; the original plan had no threshold")
    verified = summary.get("success_requires_sse_done") is True
    if not verified and not allow_legacy_success:
        raise ValueError("legacy success contract: explicit --allow-legacy-success required")
    measured = [r for r in rows if r["phase"] == "measure"]
    return {
        "schema_version": 1, "mode": "OFFLINE_REANALYSIS", "source_sha256": hashes,
        "slo_ttft_s": threshold,
        "threshold_basis": "recorded_plan" if configured == threshold else "retrospective_scenario",
        "success_requires_sse_done": verified,
        "completion_contract_basis": (
            "Recorder declaration/known notes plus row consistency; CSV has no independent SSE event proof."),
        "schedule_validation": "full CSV cohort matches current generator seed/priority/phase/offset schedule",
        "source_provenance_note": "Schedule reproduction is not proof of code loaded at the original run start.",
        "measurement_window_s": plan["duration_s"] - plan["warmup_s"],
        "warmup_requests_excluded": len(rows) - len(measured),
        "denominator": "ALL scheduled measurement arrivals, including 429/timeouts/unsent client-cap drops",
        "groups": {p: group_metrics([r for r in measured if p == "all" or r["priority"] == p],
                                    threshold, plan["duration_s"], plan["warmup_s"], verified)
                   for p in ("all", "high", "low")},
        "limitations": [
            "No admission control or priority scheduling is exercised by this analyzer.",
            "Priority headers are metadata until an actual policy is implemented and verified.",
            "A retrospective threshold is a scenario, not a preregistered or achieved business SLO.",
            "Legacy classifier counts do not establish content+DONE completion; verified attainment is null.",
            "Client first-content TTFT includes network/gateway time; cohort rates include drain completions.",
            "TTFT starts at actual send; scheduling lag is reported separately, not added to the SLO predicate.",
            "No maximum capacity, production readiness, or causal policy benefit is established.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("--slo-ttft-s", type=float)
    parser.add_argument("--allow-legacy-success", action="store_true")
    parser.add_argument("--output", type=Path, help="exclusive new JSON file; existing files are never overwritten")
    args = parser.parse_args(argv)
    try:
        result = analyze(args.run_directory, args.slo_ttft_s, allow_legacy_success=args.allow_legacy_success)
        text = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8", newline="\n") as f:
                f.write(text)
        else:
            print(text, end="")
    except (OSError, ValueError, KeyError, TypeError, csv.Error, RecursionError, OverflowError) as exc:
        parser.exit(2, f"analysis rejected: {type(exc).__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
