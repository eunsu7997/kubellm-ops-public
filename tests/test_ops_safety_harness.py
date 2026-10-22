"""No GPU faults: policy tests and tiny isolated Python children verify supervision contracts."""
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[1] / "scripts/ops"
sys.path.insert(0, str(OPS))
import exp07_load as experiment  # noqa: E402
import oplib  # noqa: E402
import safety_harness as harness  # noqa: E402

GOOD = {"temp_c": 40.0, "free_mib": 6000.0, "used_mib": 1000.0, "util": 10.0}


@pytest.mark.parametrize("change", [
    {"temp_c": 80}, {"free_mib": 511}, {"temp_c": float("nan")}, {"free_mib": float("inf")},
    {"util": -1}, {"used_mib": -1}, {"error": "unavailable"}, {"temp_c": "private-invalid-value"},
])
def test_gpu_guard_fails_closed(change):
    assert oplib.gpu_unsafe({**GOOD, **change})


def test_gpu_guard_accepts_valid_boundary_and_rejects_missing():
    assert not oplib.gpu_unsafe({**GOOD, "temp_c": 79, "free_mib": 512})
    assert oplib.gpu_unsafe({})


@pytest.mark.parametrize("failure", ["timeout", "missing", "nonzero", "bad-format", "nan", "range"])
def test_sensor_errors_are_bounded_and_do_not_expose_output(monkeypatch, failure):
    def run(cmd, **kwargs):
        assert kwargs["timeout"] == 3
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, 3)
        if failure == "missing":
            raise FileNotFoundError("PRIVATE")
        return subprocess.CompletedProcess(cmd, 1 if failure == "nonzero" else 0,
                                           {"bad-format": "PRIVATE", "nan": "nan, 1, 2, 3",
                                            "range": "40, 5, 2, 101"}.get(failure, "40, 1, 2, 3"), "PRIVATE")
    monkeypatch.setattr(oplib.subprocess, "run", run)
    result = oplib.gpu()
    assert "error" in result and oplib.gpu_unsafe(result)
    assert "PRIVATE" not in json.dumps(result)


def test_journal_partial_writes_and_unsent_do_not_reset_failures(tmp_path):
    p = tmp_path / "outcomes.jsonl"
    journal = harness.OutcomeJournal(p)
    p.write_bytes(b'{"request_id":1,"category":"time')
    journal.poll()
    assert journal.records == 0
    with p.open("ab") as f:
        f.write(b'out"}\n{"request_id":2,"category":"dropped_client_cap"}\n')
        f.write(b'{"request_id":3,"category":"timeout"}\n{"request_id":4,"category":"timeout"}\n')
    journal.poll()
    assert journal.abort == "3 consecutive sent-request failures"
    assert journal.sent_failures == 3 and journal.unsent == 1


def test_journal_success_resets_failure_streak_and_rejects_duplicate(tmp_path):
    p = tmp_path / "outcomes.jsonl"
    p.write_text(''.join(json.dumps({"request_id": i, "category": c}) + '\n'
                         for i, c in enumerate(["timeout", "success", "timeout", "timeout"])))
    journal = harness.OutcomeJournal(p)
    journal.poll()
    assert journal.abort is None and journal.consecutive_failures == 2
    with p.open("a") as f:
        f.write('{"request_id":0,"category":"success"}\n')
    with pytest.raises(ValueError, match="duplicate"):
        journal.poll()


def test_deadline_stops_and_reaps_real_child(tmp_path):
    result = harness.supervise([sys.executable, "-c", "import time;time.sleep(60)"], tmp_path / "deadline",
                               lambda: GOOD, oplib.gpu_unsafe, wall_seconds=0.5, heartbeat_seconds=2,
                               poll_seconds=0.02, grace_seconds=0.2)
    assert result["abort"] == "hard wall deadline" and not result["success"]
    assert result["cleanup"]["reaped"] and result["elapsed_seconds"] < 5
    assert json.loads((tmp_path / "deadline/result.json").read_text())["exit"] != 0


def test_sensor_failure_refuses_to_launch_child(tmp_path, monkeypatch):
    monkeypatch.setattr(harness.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    result = harness.supervise(["forbidden"], tmp_path / "invalid", lambda: {"error": "fixture"},
                               oplib.gpu_unsafe, wall_seconds=1)
    assert not result["child_started"] and result["abort"] and not result["success"]


def test_sensor_exception_cannot_silently_stop_monitoring(tmp_path):
    def fail():
        raise RuntimeError("PRIVATE")
    result = harness.supervise([sys.executable, "-c", "pass"], tmp_path / "exception", fail,
                               oplib.gpu_unsafe, wall_seconds=1)
    assert not result["success"] and "RuntimeError" in result["abort"]
    assert "PRIVATE" not in json.dumps(result)


def test_stale_heartbeat_stops_real_child(tmp_path):
    calls = 0
    def sample():
        nonlocal calls
        calls += 1
        if calls > 1:
            time.sleep(0.6)
        return GOOD
    result = harness.supervise([sys.executable, "-c", "import time;time.sleep(60)"], tmp_path / "heartbeat",
                               sample, oplib.gpu_unsafe, wall_seconds=3, heartbeat_seconds=0.2,
                               poll_seconds=0.02, sensor_interval=0.02)
    assert result["abort"] == "sensor heartbeat stale" and result["cleanup"]["reaped"]


def test_nonzero_child_is_not_success(tmp_path):
    result = harness.supervise([sys.executable, "-c", "raise SystemExit(7)"], tmp_path / "exit",
                               lambda: GOOD, oplib.gpu_unsafe, wall_seconds=5)
    assert result["exit"] == 7 and result["success"] is False


def test_termination_fallback_kills_and_waits():
    class Child:
        returncode = None
        def poll(self):
            return self.returncode
        def terminate(self):
            pass
        def wait(self, timeout):
            if self.returncode is None:
                raise subprocess.TimeoutExpired("fixture", timeout)
        def kill(self):
            self.returncode = -9
    assert harness.stop_child(Child(), grace=0.01) == {"terminate": True, "kill": True, "reaped": True}


class Evidence:
    def event(self, *args, **kwargs):
        pass
    def save(self, *args, **kwargs):
        pass


def test_ramp_without_recovery_or_any_completed_step_is_not_success(monkeypatch):
    monkeypatch.setattr(experiment, "recovery_ok", lambda *a: False)
    assert experiment.mode_ramp(Evidence()) is False


@pytest.mark.parametrize("changes,recovered", [({"exit": 1}, True), ({"abort": "fault"}, True),
                                               ({"sent_failures": 1}, True), ({}, False)])
def test_soak_failure_and_failed_recovery_propagate(monkeypatch, changes, recovered):
    info = {"exit": 0, "abort": None, "success": True, "sent_failures": 0, **changes}
    monkeypatch.setattr(experiment, "run_load", lambda *a, **k: info)
    monkeypatch.setattr(experiment, "recovery_ok", lambda *a: recovered)
    assert experiment.mode_soak(Evidence(), 1) is False


def test_missing_summary_or_phase_fields_not_accepted():
    assert not experiment.phase_ok({"abort": None})


def test_zero_successes_or_missing_telemetry_not_accepted():
    info = {"success": True, "exit": 0, "abort": None, "sent_failures": 0,
            "successes": 0, "samples": 1, "telemetry_error": None}
    assert not experiment.phase_ok(info)
    info["successes"] = 5
    assert experiment.phase_ok(info)
    info["unsent"] = 1
    assert not experiment.phase_ok(info)
    info["unsent"] = 0
    info["samples"] = 0
    assert not experiment.phase_ok(info)


def test_invalid_metrics_do_not_become_numbers():
    rows = [{"vllm": {"queue": v}} for v in ["NaN", "inf", "PRIVATE", None, "1"]]
    assert experiment.metric_values(rows, "queue") == [1.0]


def test_telemetry_failure_is_observable(tmp_path, monkeypatch):
    monkeypatch.setattr(oplib, "http", lambda *a, **k: {"status": 200})
    monkeypatch.setattr(experiment, "vllm_metrics", lambda: {})
    thread = experiment.Telemetry(tmp_path / "metrics.jsonl")
    thread.start()
    thread.join(timeout=2)
    assert thread.error == "telemetry exception: ValueError" and not thread.is_alive()


def test_external_monitor_failure_stops_and_reaps(tmp_path):
    result = harness.supervise([sys.executable, "-c", "import time; time.sleep(60)"],
                               tmp_path / "external", lambda: GOOD, oplib.gpu_unsafe,
                               wall_seconds=2, external_abort=lambda: "telemetry unavailable")
    assert result["abort"] == "telemetry unavailable" and result["cleanup"]["reaped"]


def test_operator_interrupt_persists_cleanup_then_propagates(tmp_path):
    def interrupt():
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        harness.supervise([sys.executable, "-c", "import time; time.sleep(60)"],
                          tmp_path / "interrupt", lambda: GOOD, oplib.gpu_unsafe,
                          wall_seconds=3, external_abort=interrupt)
    result = json.loads((tmp_path / "interrupt/result.json").read_text())
    assert result["abort"] == "operator interrupt" and result["cleanup"]["reaped"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows taskkill process-tree path; verified on Windows")
def test_supervisor_stops_worker_descendants(tmp_path):
    marker = tmp_path / "orphan-must-not-write.txt"
    armed = tmp_path / "armed.txt"
    grandchild = (f"import time;from pathlib import Path;Path({str(armed)!r}).write_text('armed');"
                  f"time.sleep(2);Path({str(marker)!r}).write_text('orphan')")
    code = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{grandchild!r}]);time.sleep(60)"
    result = harness.supervise([sys.executable, "-c", code], tmp_path / "tree",
                               lambda: GOOD, oplib.gpu_unsafe, wall_seconds=1, poll_seconds=0.05)
    assert result["cleanup"]["tree"] and result["cleanup"]["reaped"]
    time.sleep(2.1)
    assert armed.exists() and not marker.exists()


def test_ops_entrypoint_dry_run_no_sensors_or_processes(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["load", "soak"])
    monkeypatch.setattr(oplib, "gpu", lambda: pytest.fail("no sensor in dry run"))
    assert experiment.main() == 0


def test_diagnostic_lease_blocks_new_experiment_before_gpu_queries(tmp_path, monkeypatch):
    lock = tmp_path / "active.lock"
    lock.write_text("fixture owner")
    monkeypatch.setattr(oplib, "TRIAL_LOCK", lock)
    monkeypatch.setattr(sys, "argv", ["load", "soak", "--execute"])
    monkeypatch.setattr(oplib, "gpu", lambda: pytest.fail("must not overlap active diagnostic"))
    with pytest.raises(SystemExit) as exc:
        experiment.main()
    assert exc.value.code == 2


def test_harness_module_import_does_not_run_any_child(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("import must not run"))
    spec = importlib.util.spec_from_file_location("fresh_safety_import", OPS / "safety_harness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
