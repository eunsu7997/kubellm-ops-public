"""Local rehearsal children must not leave orphan processes or temporary logs."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "scripts/failure-tests/local_stack.py"
spec = importlib.util.spec_from_file_location("local_stack_lifecycle", PATH)
stack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stack)


def test_stop_reaps_child_and_preserves_output_without_temp_log(tmp_path):
    child = stack.ManagedProcess("test", [sys.executable, "-u", "-c",
                                         "print('child-output', flush=True)"], tmp_path, os.environ.copy())
    child.start()
    log = Path(child._log_file.name)
    try:
        child.process.wait(timeout=10)
    finally:
        child.stop()
    assert child.process.poll() is not None
    assert "child-output" in child.read_output()
    assert not log.exists()
    child.stop()
    assert "child-output" in child.read_output()


def test_double_start_cannot_orphan_the_first_child(tmp_path):
    child = stack.ManagedProcess("test", [sys.executable, "-c", "import time; time.sleep(60)"],
                                 tmp_path, os.environ.copy())
    child.start()
    process = child.process
    try:
        with pytest.raises(RuntimeError, match="already running"):
            child.start()
        assert child.process is process
    finally:
        child.stop()
    assert process.poll() is not None


def test_failed_launch_removes_its_log(tmp_path, monkeypatch):
    original = stack.tempfile.NamedTemporaryFile
    monkeypatch.setattr(stack.tempfile, "NamedTemporaryFile", lambda **kw: original(dir=tmp_path, **kw))

    def failed_launch(*args, **kwargs):
        raise OSError("launch failed")

    monkeypatch.setattr(stack.subprocess, "Popen", failed_launch)
    child = stack.ManagedProcess("test", ["missing-program"], tmp_path, {})
    with pytest.raises(OSError, match="launch failed"):
        child.start()
    assert list(tmp_path.iterdir()) == []
    assert child._log_file is None
    child.stop()
