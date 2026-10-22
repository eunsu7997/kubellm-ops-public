"""Actual isolated Python helper handshake/cancellation; never executes Docker."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

OPS = Path(__file__).resolve().parents[1] / "scripts/ops"
sys.path.insert(0, str(OPS))
SPEC = importlib.util.spec_from_file_location("queue_helper_tests", OPS / "exp04b_queue_alert.py")
queue = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(queue)


def test_helper_cancellation_acknowledged_without_restoring(tmp_path):
    armed, cancel, result = (tmp_path / n for n in ["armed.json", "cancel.txt", "result.json"])
    forbidden = tmp_path / "must-not-run.txt"
    command = [sys.executable, "-c", f"from pathlib import Path;Path({str(forbidden)!r}).write_text('bad')"]
    cancel.write_text("fixture cancellation")
    p = subprocess.run([sys.executable, "-c", queue.restore_helper_code(
        command, tmp_path, armed, cancel, result, 0.5)], capture_output=True, timeout=5)
    assert p.returncode == 0
    assert json.loads(armed.read_text())["pid"] > 0
    assert json.loads(result.read_text()) == {"outcome": "cancelled", "exit": 0}
    assert not forbidden.exists()


def test_helper_performs_only_supplied_bounded_restore_command(tmp_path):
    armed, cancel, result = (tmp_path / n for n in ["armed.json", "cancel.txt", "result.json"])
    p = subprocess.run([sys.executable, "-c", queue.restore_helper_code(
        [sys.executable, "-c", "raise SystemExit(7)"], tmp_path, armed, cancel, result, 0.05)],
        capture_output=True, timeout=5)
    assert p.returncode == 0
    assert json.loads(result.read_text()) == {"outcome": "restore-attempted", "exit": 7}
