"""Independent hand-counted SLO denominators and corrupted-cohort rejection; no GPU/network."""
import csv
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/load-test/evaluate_slo.py"
SPEC = importlib.util.spec_from_file_location("slo_evaluation_tests", SCRIPT)
slo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(slo)


def row(category="success", ttft=.1, send=1, e2e=1, lag=0):
    return {"category": category, "ttft_s": ttft, "send_offset_s": send, "e2e_s": e2e, "send_lag_s": lag}


def fixture_run(tmp_path, *, verified=True, high_ratio=.5, seed=42, burst=None):
    plan = {"rate": 2, "duration_s": 5, "warmup_s": 1, "seed": seed,
            "burst": burst, "high_ratio": high_ratio, "slo_ttft_s": None}
    schedule = slo.generator.build_schedule(2, 5, seed, burst, high_ratio=high_ratio, warmup_s=1)
    plan["scheduled_requests"] = len(schedule)
    plan["scheduled_by_priority"] = {p: sum(r["priority"] == p for r in schedule) for p in ("high", "low")}
    records = [{**r, "send_offset_s": r["scheduled_offset_s"] + .01, "send_lag_s": .01,
                "status_code": 200, "category": "success", "ttft_s": .1,
                "e2e_s": .2, "chunks": 3, "completion_tokens": 3, "tpot_s": .05} for r in schedule]
    summary = json.loads(json.dumps(slo.generator.summarize(plan, records)))
    summary["success_requires_sse_done"] = verified
    if not verified:
        summary["category_notes"]["success"] = "HTTP 2xx and content observed"
    rewrite(tmp_path, records, summary)
    return records, summary


def rewrite(directory, rows, summary):
    with (directory / "requests.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=slo.generator.CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")


def test_denominator_includes_429_timeouts_unsent_and_success_above_threshold():
    rows = [row(), row(ttft=2), row("rate_limited", None), row("timeout", None),
            row("dropped_client_cap", None, e2e=None)]
    result = slo.group_metrics(rows, 1, 10, 0, True)
    assert result["measurement_arrivals"] == 5
    assert result["slo_compliant_successes"] == 1
    assert result["attainment_fraction"] == .2  # not 1/2 from the success-only subset
    assert result["failure_inclusive_ttft_p95"]["kind"] == "unbounded"
    assert result["success_only_ttft_p95_s"] == pytest.approx(1.905)


def test_failure_inclusive_nearest_rank_at_exact_five_percent_boundary():
    finite = slo.group_metrics([row()] * 19 + [row("timeout", None)], 1, 10, 0, True)
    unbounded = slo.group_metrics([row()] * 18 + [row("timeout", None)], 1, 10, 0, True)
    assert finite["failure_inclusive_ttft_p95"]["seconds"] == .1
    assert unbounded["failure_inclusive_ttft_p95"]["seconds"] is None
    json.dumps(unbounded, allow_nan=False)


def test_drain_cohort_rate_differs_from_completions_within_window():
    result = slo.group_metrics([row(send=1), row(send=9, e2e=3)], 1, 10, 0, True)
    assert result["cohort_slo_successes_per_nominal_second"] == .2
    assert result["slo_successes_completed_in_window_per_second"] == .1
    assert result["compliant_successes_completed_after_window"] == 1


def test_empty_priority_has_no_attainment_or_latency_claim():
    result = slo.group_metrics([], 1, 10, 0, True)
    assert result["attainment_fraction"] is None
    assert result["evidence_status"] == "NO_SAMPLES"
    assert result["failure_inclusive_ttft_p95"]["kind"] == "no_samples"


def test_full_analysis_excludes_warmup_and_identifies_retrospective_threshold(tmp_path):
    records, _ = fixture_run(tmp_path)
    result = slo.analyze(tmp_path, 1)
    measured = [r for r in records if r["phase"] == "measure"]
    assert result["groups"]["all"]["measurement_arrivals"] == len(measured)
    assert result["warmup_requests_excluded"] == len(records) - len(measured)
    assert result["threshold_basis"] == "retrospective_scenario"
    assert result["groups"]["high"]["attainment_fraction"] == 1
    assert len(result["source_sha256"]["requests.csv"]) == 64


def test_recorded_threshold_is_used_without_cli_override(tmp_path):
    records, summary = fixture_run(tmp_path)
    summary["plan"]["slo_ttft_s"] = .05
    summary = slo.generator.summarize(summary["plan"], records)
    rewrite(tmp_path, records, summary)
    result = slo.analyze(tmp_path)
    assert result["threshold_basis"] == "recorded_plan"
    assert result["groups"]["all"]["attainment_fraction"] == 0
    assert slo.analyze(tmp_path, 1)["threshold_basis"] == "retrospective_scenario"


def test_legacy_classifier_requires_opt_in_and_never_reports_verified_attainment(tmp_path):
    fixture_run(tmp_path, verified=False)
    with pytest.raises(ValueError, match="legacy"):
        slo.analyze(tmp_path, 1)
    result = slo.analyze(tmp_path, 1, allow_legacy_success=True)
    assert result["groups"]["all"]["attainment_fraction"] is None
    assert result["groups"]["all"]["exploratory_classifier_attainment_fraction"] == 1
    assert result["groups"]["all"]["evidence_status"] == "LEGACY_UNVERIFIED"
    group = result["groups"]["all"]
    assert group["success_only_ttft_p95_s"] is None
    assert group["cohort_slo_successes_per_nominal_second"] is None
    assert group["failure_inclusive_ttft_p95"]["kind"] == "legacy_unverified"
    assert group["exploratory_classifier_metrics"]["success_only_ttft_p95_s"] == .1


@pytest.mark.parametrize("threshold", [0, -1, float("nan"), float("inf"), True, 10**400])
def test_invalid_threshold_is_rejected(tmp_path, threshold):
    fixture_run(tmp_path)
    with pytest.raises(ValueError):
        slo.analyze(tmp_path, threshold)


@pytest.mark.parametrize("change", ["missing", "duplicate", "priority", "phase", "offset", "category",
                                    "nan", "status", "ttft_after_e2e", "drop_with_response", "priority_count",
                                    "http_category"])
def test_corrupt_cohort_is_rejected_instead_of_silently_improving_denominator(tmp_path, change):
    records, summary = fixture_run(tmp_path)
    if change == "missing":
        records.pop()
    elif change == "duplicate":
        records[-1]["request_id"] = records[0]["request_id"]
    elif change == "priority":
        records[0]["priority"] = "low" if records[0]["priority"] == "high" else "high"
    elif change == "phase":
        records[0]["phase"] = "measure" if records[0]["phase"] == "warmup" else "warmup"
    elif change == "offset":
        records[0]["scheduled_offset_s"] += .01
    elif change == "category":
        records[0]["category"] = "silent_error"
    elif change == "nan":
        records[0]["ttft_s"] = float("nan")
    elif change == "status":
        records[0]["status_code"] = 500
    elif change == "ttft_after_e2e":
        records[0]["ttft_s"] = 99
    elif change == "drop_with_response":
        records[0]["category"] = "dropped_client_cap"
    elif change == "http_category":
        records[0]["category"] = "rate_limited"
    else:
        summary["plan"]["scheduled_by_priority"]["high"] += 1
    rewrite(tmp_path, records, summary)
    with pytest.raises(ValueError):
        slo.analyze(tmp_path, 1)


def test_cli_does_not_overwrite_and_has_no_network_effects(tmp_path, monkeypatch):
    import socket

    fixture_run(tmp_path)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("offline analyzer used network"))
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("offline analyzer opened a socket"))
    output = tmp_path / "analysis.json"
    args = [str(tmp_path), "--slo-ttft-s", "1", "--output", str(output)]
    assert slo.main(args) == 0
    original = output.read_bytes()
    with pytest.raises(SystemExit) as exc:
        slo.main(args)
    assert exc.value.code == 2
    assert output.read_bytes() == original


def test_absent_threshold_and_duplicate_json_keys_are_rejected(tmp_path):
    fixture_run(tmp_path)
    with pytest.raises(ValueError, match="threshold"):
        slo.analyze(tmp_path)
    (tmp_path / "summary.json").write_text('{"plan": {}, "plan": {}}')
    with pytest.raises(ValueError, match="duplicate"):
        slo.analyze(tmp_path, 1)


@pytest.mark.parametrize("field", ["categories", "priorities", "categories_by_priority", "measurement_window_s",
                                 "warmup_requests_excluded", "goodput_rps", "max_send_lag_s", "latency"])
def test_summary_aggregate_mismatch_is_rejected(tmp_path, field):
    rows, summary = fixture_run(tmp_path)
    summary[field] = "mismatched run"
    rewrite(tmp_path, rows, summary)
    with pytest.raises(ValueError, match="summary"):
        slo.analyze(tmp_path, 1)


def test_legacy_flag_alone_cannot_upgrade_completion_contract(tmp_path):
    rows, summary = fixture_run(tmp_path, verified=False)
    summary["success_requires_sse_done"] = True
    rewrite(tmp_path, rows, summary)
    with pytest.raises(ValueError, match="contract"):
        slo.analyze(tmp_path, 1)


def test_negative_seed_and_outside_window_burst_match_generator(tmp_path):
    fixture_run(tmp_path, seed=-42, burst=[30, 1, 2])
    assert slo.analyze(tmp_path, 1)["groups"]["all"]["measurement_arrivals"] > 0


@pytest.mark.parametrize("seed", [True, 1.5, "42"])
def test_non_integer_seed_is_rejected(tmp_path, seed):
    rows, summary = fixture_run(tmp_path)
    summary["plan"]["seed"] = seed
    rewrite(tmp_path, rows, summary)
    with pytest.raises(ValueError, match="seed"):
        slo.analyze(tmp_path, 1)


def test_utf8_bom_is_accepted_but_extra_header_column_is_not(tmp_path):
    fixture_run(tmp_path)
    p = tmp_path / "requests.csv"
    content = p.read_bytes()
    p.write_bytes(b"\xef\xbb\xbf" + content)
    assert slo.analyze(tmp_path, 1)["groups"]["all"]["measurement_arrivals"] > 0
    lines = content.decode().splitlines()
    lines[0] += ",unexpected"
    p.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(ValueError, match="header"):
        slo.analyze(tmp_path, 1)


def test_early_send_lag_is_retained_and_reported_not_silently_clamped(tmp_path):
    rows, summary = fixture_run(tmp_path)
    target = next(r for r in rows if r["phase"] == "measure")
    target["send_lag_s"] = -.001
    target["send_offset_s"] = target["scheduled_offset_s"] - .001
    summary = slo.generator.summarize(summary["plan"], rows)
    rewrite(tmp_path, rows, summary)
    result = slo.analyze(tmp_path, 1)["groups"]["all"]
    assert result["early_send_count"] == 1
    assert result["min_send_lag_s"] == -.001


def test_csv_parse_error_exits_two_without_overwriting_output(tmp_path):
    fixture_run(tmp_path)
    (tmp_path / "requests.csv").write_text("a" * 140_000)
    with pytest.raises(SystemExit) as exc:
        slo.main([str(tmp_path), "--slo-ttft-s", "1"])
    assert exc.value.code == 2
