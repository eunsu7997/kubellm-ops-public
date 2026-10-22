"""Offline cleanup/stop gates for the optional JIT diagnostic. No Docker/GPU faults here."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

OPS = Path(__file__).resolve().parents[1] / "scripts/ops"
sys.path.insert(0, str(OPS))
SPEC = importlib.util.spec_from_file_location("jit_diagnostic_tests", OPS / "exp08_jit_readiness.py")
diag = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diag)


class Evidence:
    def event(self, *a, **kw):
        pass


def test_failed_temporary_recreate_still_restores_stock(monkeypatch):
    calls = []
    def recreate(x, tag, extra):
        calls.append((tag, extra))
        if extra:
            raise RuntimeError("fixture temp failure")
        return True
    monkeypatch.setattr(diag, "recreate", recreate)
    phases, restored, error = diag.trial(Evidence(), "fixture.yaml")
    assert restored and not phases and error == "RuntimeError"
    assert calls[-1] == ("restore-stock", [])


def test_aborted_cold_load_skips_warm_control_and_restores(monkeypatch):
    calls = []
    monkeypatch.setattr(diag, "recreate", lambda *a: True)
    def phase(x, tag):
        calls.append(tag)
        return {"abort": "sensor unsafe"}
    monkeypatch.setattr(diag, "phase", phase)
    phases, restored, error = diag.trial(Evidence(), "fixture.yaml")
    assert calls == ["cold"] and restored and len(phases) == 1 and error is None


def test_default_dry_run_starts_nothing(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["jit"])
    monkeypatch.setattr(diag, "assert_stock_config", lambda: (_ for _ in ()).throw(AssertionError("no preflight")))
    assert diag.main() == 0


def test_runtime_gate_requires_same_digest_args_and_gpu(monkeypatch):
    data = {"image": diag.IMAGE.split("@")[1], "args": ["serve", *diag.ARGS],
            "devices": [{"Count": -1, "Capabilities": [["gpu"]]}]}
    monkeypatch.setattr(diag.R, "docker", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(data)))
    assert diag.runtime_stock()
    data["args"] = ["serve", "--model", "wrong-model"]
    assert not diag.runtime_stock()


def test_diagnostic_lease_is_exclusive_and_retained_when_helper_may_fire(tmp_path):
    path = tmp_path / "diagnostic.lock"
    with diag.DiagnosticLease(path) as lease:
        with pytest.raises(FileExistsError), diag.DiagnosticLease(path):
            pass
        assert json.loads(path.read_text())["owner_id"] == lease.owner
    assert not path.exists()
    with diag.DiagnosticLease(path) as lease:
        lease.keep = True
    assert path.exists()


def test_wrong_or_live_after_run_refused_before_service_queries(tmp_path, monkeypatch):
    root = tmp_path / "evidence/ops-safety-closure-20261021"
    older, newest = (root / n for n in ["safety-fixed-soak-01", "safety-fixed-soak-02"])
    older.mkdir(parents=True)
    newest.mkdir()
    monkeypatch.setattr(diag.L, "ROOT", tmp_path)
    monkeypatch.setattr(diag, "assert_stock_config", lambda: pytest.fail("no service preflight after refusal"))
    with pytest.raises(ValueError, match="newest"):
        diag.execute(SimpleNamespace(after_run=older), SimpleNamespace(keep=False))
    monkeypatch.setattr(diag, "active_project_loads", lambda: 1)
    with pytest.raises(RuntimeError, match="live/unknown"):
        diag.execute(SimpleNamespace(after_run=newest), SimpleNamespace(keep=False))


def test_restored_readiness_without_exact_args_does_not_pass(monkeypatch):
    monkeypatch.setattr(diag, "ready_once", lambda: True)
    monkeypatch.setattr(diag, "runtime_stock", lambda extra=(): False)
    monkeypatch.setattr(diag, "runtime_selected", lambda: {"args": ["wrong"]})
    x = Evidence()
    x.run = lambda *a, **kw: None
    assert not diag.recreate(x, "restore-stock", [])


def test_newest_run_is_chosen_by_timestamp_not_mode_name(tmp_path, monkeypatch):
    root = tmp_path / "evidence/ops-safety-closure-20261021"
    warm = root / "safety-fixed-warmup-20261021T151000000000Z"
    soak = root / "safety-fixed-soak-20261021T152000000000Z"
    warm.mkdir(parents=True)
    soak.mkdir()
    monkeypatch.setattr(diag.L, "ROOT", tmp_path)
    monkeypatch.setattr(diag, "active_project_loads", lambda: 1)
    with pytest.raises(RuntimeError, match="live/unknown"):
        diag.execute(SimpleNamespace(after_run=soak), SimpleNamespace(keep=False))


def test_post_restore_identity_error_still_records_armed_fallback(tmp_path, monkeypatch, capsys):
    events = []
    x = SimpleNamespace(event=lambda kind, **kw: events.append({"kind": kind, **kw}))
    monkeypatch.setattr(diag, "trial", lambda *a: ([], True, None))
    def fail():
        raise ValueError("fixture changed config")
    monkeypatch.setattr(diag, "assert_stock_config", fail)
    lease = SimpleNamespace(keep=True)
    result = diag.finish_trial(x, "unused", lease, None, tmp_path / "cancel", tmp_path / "result",
                               {"expected_fire_at_utc": "fixture-deadline"})
    assert result == 1 and events[-1]["kind"] == "done" and events[-1]["helper_still_armed"]
    assert events[-1]["error"] == "ValueError" and "fixture-deadline" in capsys.readouterr().err
    assert not (tmp_path / "cancel").exists()


def test_exact_stock_identity_is_recorded_before_cancel(tmp_path, monkeypatch):
    events = []
    x = SimpleNamespace(event=lambda kind, **kw: events.append({"kind": kind, **kw}))
    phase = {"success": True, "exit": 0, "abort": None, "successes": 1, "sent_failures": 0,
             "unsent": 0, "samples": 1, "telemetry_error": None}
    monkeypatch.setattr(diag, "trial", lambda *a: ([phase, phase], True, None))
    monkeypatch.setattr(diag, "assert_stock_config", lambda: None)
    monkeypatch.setattr(diag, "runtime_stock", lambda: True)
    monkeypatch.setattr(diag, "ready_once", lambda: True)
    monkeypatch.setattr(diag.L, "sse", lambda **kw: {"ok": True})
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({"outcome": "cancelled", "exit": 0}))
    lease = SimpleNamespace(keep=True)
    code = diag.finish_trial(x, "unused", lease, SimpleNamespace(wait=lambda **kw: None),
                             tmp_path / "cancel", result_path, {})
    assert code == 0 and not lease.keep
    assert events[0]["kind"] == "stock-identity-before-helper-cancel" and events[0]["verified"]
    assert events[-1]["kind"] == "done" and events[-1]["collection_success"]


def test_log_query_failure_does_not_claim_collected_data(monkeypatch):
    monkeypatch.setattr(diag.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout="", stderr="fixture"))
    x = SimpleNamespace(save=lambda *a: None)
    with pytest.raises(RuntimeError, match="log query failed"):
        diag.collect_logs(x, "tag", "start", "end")
