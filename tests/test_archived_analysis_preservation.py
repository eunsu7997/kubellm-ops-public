"""Archived one-off analysis must refuse to overwrite preserved reports before doing other work."""
import importlib.util
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[1] / "scripts/ops"


def load(name):
    spec = importlib.util.spec_from_file_location("archived_" + name, OPS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_old_ramp_analyzer_preserves_existing_report_before_network_or_reads(tmp_path, monkeypatch):
    module = load("analyze_exp07_ramp")
    target = tmp_path / "step-table.json"
    target.write_bytes(b"preserve these original bytes\r\n")
    monkeypatch.setattr(module.glob, "glob", lambda *a: pytest.fail("must refuse before reading CSV/network"))
    with pytest.raises(FileExistsError):
        module.main(str(tmp_path))
    assert target.read_bytes() == b"preserve these original bytes\r\n"


def test_old_readiness_analyzer_preserves_existing_report(tmp_path, monkeypatch):
    module = load("analyze_exp02")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    target = tmp_path / "analysis.json"
    target.write_bytes(b"original\r\n")
    with pytest.raises(FileExistsError):
        module.main()
    assert target.read_bytes() == b"original\r\n"
