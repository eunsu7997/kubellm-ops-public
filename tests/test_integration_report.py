import json

import pytest

from scripts.ops.integration_bridge import draft_eligible
from scripts.ops.integration_report import analyze


def dataset(path):
    files = {
        "plan.json": {"trace": [{}, {}], "order": ["token"]},
        "00-token-rows.json": [
            {"request_id": 0, "priority": "high", "status_code": 200, "category": "success", "ttft_s": .1},
            {"request_id": 1, "priority": "low", "status_code": 429, "category": "rate_limited", "ttft_s": None}],
        "00-token-summary.json": {"policy": "token", "output_tokens": 8, "elapsed_including_drain_s": 2,
                                  "client_ttft_p99_s": .1, "adapter_arrival_ttft_p99_s": .09,
                                  "max_active": 1, "max_reserved_tokens": 100}}
    for name, value in files.items():
        (path / name).write_text(json.dumps(value))


def test_all_planned_requests_include_429_and_unbounded_tail(tmp_path):
    dataset(tmp_path)
    result = analyze(tmp_path)["token"]
    assert result["success_rate"] == .5
    assert result["429_rate"] == .5
    assert result["slo_1s_attainment"] == .5
    assert result["accepted_failure_rate"] == 0
    assert result["failure_inclusive_p99"] == {"kind": "unbounded", "seconds": None}
    assert result["per_priority"]["low"]["scheduled"] == 1
    assert result["per_priority"]["low"]["successful_ttft_p99_s"] is None


def test_missing_phase_and_duplicate_request_fail_closed(tmp_path):
    dataset(tmp_path)
    rows = json.loads((tmp_path / "00-token-rows.json").read_text())
    rows[1]["request_id"] = 0
    (tmp_path / "00-token-rows.json").write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="duplicate"):
        analyze(tmp_path)
    (tmp_path / "00-token-rows.json").unlink()
    with pytest.raises(ValueError, match="missing phase"):
        analyze(tmp_path)


def test_hallucinated_reference_or_missing_uncertainty_blocks_draft():
    validation = {"result": {"format_valid": True, "missing_ids": ["invented"], "warnings": [],
                             "parsed": {"analysis": {"unknowns": ["uncertain"]}}}}
    assert not draft_eligible(validation)
    validation["result"]["missing_ids"] = []
    assert draft_eligible(validation)
    validation["result"]["parsed"]["analysis"]["unknowns"] = []
    assert not draft_eligible(validation)


def test_container_log_capture_is_time_bounded_and_whitelisted(monkeypatch):
    from types import SimpleNamespace

    from scripts.ops import integration_report as report

    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout='2026-10-22T01:00:00Z INFO "POST /v1/chat/completions HTTP/1.1" 200\n'
                                      '2026-10-22T01:00:00Z PRIVATE_PROMPT_SHOULD_NOT_BE_SAVED\n', stderr="")

    monkeypatch.setattr(report.subprocess, "run", run)
    result = report.safe_access_logs("start", "end")
    assert all(c[c.index("--since") + 1] == "start" and c[c.index("--until") + 1] == "end" for c in commands)
    assert "PRIVATE_PROMPT" not in json.dumps(result)
    assert result["kubellm-gpu-secure-gateway-1"][0]["status"] == 200
    assert result["capture_window"]["complete_raw_log_coverage_claimed"] is False
