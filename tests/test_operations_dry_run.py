"""The operations entry points must not mutate services without --execute."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("name", [
    "harden-shared-kubeconfig", "persist-monitoring", "restore-grafana-config", "verify-local-alert",
])
def test_operations_default_does_not_start_processes(name, monkeypatch, tmp_path):
    file = Path(__file__).parents[1] / "scripts" / "kubernetes" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def forbidden(*args, **kwargs):
        pytest.fail("dry run must not invoke Docker/systemctl or any subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    output = tmp_path / "must-not-exist"
    monkeypatch.setattr(sys, "argv", [name, "--output-dir", str(output)])
    assert module.main() == 0
    assert not output.exists()


def test_alert_observation_handles_bounded_history_rollover():
    file = Path(__file__).parents[1] / "scripts/kubernetes/verify-local-alert.py"
    spec = importlib.util.spec_from_file_location("alert_rollover", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    old = [{"received_at": str(i)} for i in range(100)]
    new = {"received_at": "new", "status": "resolved"}
    before = {"retained_count": 1000, "events": old}
    after = {"retained_count": 1000, "events": old[1:] + [new]}
    assert module.new_events(before, after) == [new]
