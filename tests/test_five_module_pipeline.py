import json

import pytest

from scripts.ops.five_module_pipeline import Pipeline, action_for, load_slo


def test_only_pre_admission_429_allows_local_retry_intervention():
    case = {"failed": 2, "final_429": 2, "accepted_failures": 0}
    decision = action_for({"verdict": "FAIL"}, case)
    assert decision["action"] == "EXPERIMENT_ENABLE_BOUNDED_429_RETRY"
    assert decision["production_mutation"] is False
    assert action_for({"verdict": "UNVERIFIED"}, case)["action"] == "OBSERVE_ONLY"
    assert action_for({"verdict": "FAIL"}, {**case, "accepted_failures": 1})["action"] == "OBSERVE_ONLY"
    assert action_for({"verdict": "FAIL"}, {**case, "final_429": 1})["action"] == "OBSERVE_ONLY"
    assert action_for({"verdict": "PASS"}, case)["action"] == "NO_ACTION"


def test_stage_failure_keeps_durable_receipt_and_correlation_id(tmp_path):
    pipeline = Pipeline(tmp_path, "test-correlation")

    def fail():
        raise ValueError("synthetic gate failure")

    with pytest.raises(ValueError):
        pipeline.stage("guard", fail)
    receipt = json.loads((tmp_path / "guard.json").read_text())
    assert receipt["run_id"] == "test-correlation"
    assert receipt["status"] == "FAILED"
    assert receipt["elapsed_s"] >= 0


def test_bad_upstream_source_is_rejected_before_execution(tmp_path):
    source = tmp_path / "metrics.py"
    marker = tmp_path / "should-not-exist"
    source.write_text(f"open({str(marker)!r}, 'w').write('bad')")
    with pytest.raises(ValueError, match="differs"):
        load_slo(source)
    assert not marker.exists()


def test_blocked_stage_is_not_labelled_completed(tmp_path):
    pipeline = Pipeline(tmp_path, "test")
    pipeline.stage("queue", lambda: {"allowed": False})
    assert json.loads((tmp_path / "queue.json").read_text())["status"] == "BLOCKED"


@pytest.mark.parametrize("blocked", ["guard", "queue"])
def test_failed_preflight_never_reaches_gpu_commands(tmp_path, monkeypatch, blocked):
    from types import SimpleNamespace

    from scripts.ops import five_module_pipeline as module

    guard_repo = tmp_path / "guard"
    (guard_repo / "src/gpu_guard").mkdir(parents=True)
    (guard_repo / "src/gpu_guard/cli.py").write_text("# synthetic source fingerprint only")
    commands = []

    def command(self, name, args, timeout):
        commands.append(name)
        assert name == "guard", "GPU/serving commands must never run after a failed preflight"
        checks = [{"check": "synthetic", "status": "FAIL" if blocked == "guard" else "PASS"}]
        return SimpleNamespace(stdout=json.dumps(checks))

    monkeypatch.setattr(module.Pipeline, "command", command)
    monkeypatch.setattr(module, "load_slo", lambda path: None)
    monkeypatch.setattr(module, "batch_snapshot", lambda path: {"experiment_allowed": False})
    monkeypatch.setattr(module.J.R, "gpu_pods_active", lambda: False)
    args = SimpleNamespace(output_dir=tmp_path / "out", rounds=5, slo_source=tmp_path / "unused.py",
                           guard_repo=guard_repo, queue_repo=tmp_path / "queue", copilot_repo=tmp_path / "copilot")
    assert module.execute(args) == 2
    summary = json.loads((tmp_path / "out/SUMMARY.json").read_text())
    assert summary["status"] == "BLOCKED"
    assert commands == ["guard"]
    assert not (tmp_path / "out/baseline").exists()


def test_command_timeout_cleans_owned_child_and_preserves_partial_log(tmp_path, monkeypatch):
    import subprocess

    from scripts.ops import five_module_pipeline as module

    class Child:
        returncode = None
        calls = 0

        def communicate(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["synthetic"], timeout)
            return "partial stage output", None

    child = Child()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: child)
    cleaned = []

    def cleanup(process):
        cleaned.append(process)
        process.returncode = -1

    monkeypatch.setattr(module, "stop_child", cleanup)
    with pytest.raises(subprocess.TimeoutExpired):
        module.Pipeline(tmp_path, "test").command("child", ["synthetic"], .01)
    assert cleaned == [child]
    assert (tmp_path / "child.log").read_text() == "partial stage output"
    assert json.loads((tmp_path / "child-command.json").read_text())["exit"] == -1
