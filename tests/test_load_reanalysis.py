"""Offline interpretation checks distinguish a measurement window from cohort drain."""
import csv
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ops/reanalyze_load.py"
SPEC = importlib.util.spec_from_file_location("load_reanalysis_tests", SCRIPT)
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)


def test_drain_and_client_timeout_are_not_sustained_rate_or_server_http_error(tmp_path):
    rows = [dict(phase="measure", category="success", send_offset_s=1, e2e_s=1, ttft_s=.1,
                 completion_tokens=10, status_code=200),
            dict(phase="measure", category="success", send_offset_s=9, e2e_s=3, ttft_s=.2,
                 completion_tokens=20, status_code=200),
            dict(phase="measure", category="timeout", send_offset_s=8, e2e_s=5, ttft_s="",
                 completion_tokens="", status_code=200)]
    path = tmp_path / "requests.csv"
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (tmp_path / "summary.json").write_text(json.dumps({"plan": {"warmup_s": 0, "duration_s": 10, "rate": 1}}))
    result = analysis.analyze(path)
    assert result["completion_rate_within_window_rps"] == .1
    assert result["cohort_completion_rate_including_drain_rps"] == pytest.approx(2 / 13)
    assert result["success_completed_after_window"] == 1
    assert result["failure_http_statuses"] == {"200": 1}
    assert result["tokens_of_requests_completed_within_window_per_nominal_second"] == 1


def test_percentile_uses_same_linear_interpolation_as_generator():
    assert analysis.percentile([1, 2, 3, 4], 95) == pytest.approx(3.85)
    assert analysis.percentile([], 95) is None
