"""Offline failure-branch tests for scripts/kubernetes/harden-shared-kubeconfig.py.

All filesystem paths are redirected to tmp_path; systemctl/runuser/k3s are faked. This verifies the script's
rollback logic only. It is not a real systemd/k3s failure and does not touch the shared cluster.
"""
import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

UNIT_TEXT = "ExecStart=/usr/local/bin/k3s \\\n    server \\\n    '--write-kubeconfig-mode' \\\n    '644' \\\n"


def load():
    file = Path(__file__).parents[1] / "scripts" / "kubernetes" / "harden-shared-kubeconfig.py"
    spec = importlib.util.spec_from_file_location("harden_rollback", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Fake:
    def __init__(self, node_ready=True, nobody_can_read=False, trusted_kubectl_ok=True):
        self.calls = []
        self.node_ready = node_ready
        self.nobody_can_read = nobody_can_read
        self.trusted_kubectl_ok = trusted_kubectl_ok
        self.restarted_after_failure = 0

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        self.calls.append(cmd)
        out = ""
        code = 0
        if cmd[:2] == ["k3s", "kubectl"] and "-o" in cmd and "json" in cmd:
            status = "True" if self.node_ready else "False"
            out = json.dumps({"items": [{"status": {"conditions": [{"type": "Ready", "status": status}]}}]})
            code = 0 if self.node_ready else 1
        elif cmd[0] == "runuser" and "nobody" in cmd:
            code = 0 if self.nobody_can_read else 1
        elif cmd[0] == "runuser" and "kubectl" in cmd:
            code = 0 if self.trusted_kubectl_ok else 1
        return subprocess.CompletedProcess(cmd, code, out, "")


@pytest.fixture
def env(tmp_path, monkeypatch):
    module = load()
    unit = tmp_path / "k3s.service"
    unit.write_text(UNIT_TEXT, encoding="utf-8", newline="")
    kubeconfig = tmp_path / "k3s.yaml"
    kubeconfig.write_text("", encoding="utf-8")  # never any real credential material
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(module, "UNIT", unit)
    monkeypatch.setattr(module, "DROPIN", tmp_path / "dropin" / "30.conf")
    monkeypatch.setattr(module, "KUBECONFIG", kubeconfig)
    monkeypatch.setattr(module, "BACKUP_ROOT", backups)
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    chowns, chmods = [], []
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: chowns.append((Path(path).name, uid, gid)), raising=False)
    real_chmod = Path.chmod

    def recording_chmod(self, mode):
        chmods.append((self.name, oct(mode)))
        real_chmod(self, mode)

    monkeypatch.setattr(Path, "chmod", recording_chmod)
    gid = os.stat(kubeconfig).st_gid  # chown is faked, so the "trusted group" must be the file's real group
    fake_grp = types.SimpleNamespace(getgrnam=lambda name: types.SimpleNamespace(gr_gid=gid))
    monkeypatch.setitem(sys.modules, "grp", fake_grp)
    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    return module, unit, chowns, chmods, tmp_path


def run(module, monkeypatch, fake, tmp_path):
    monkeypatch.setattr(module.subprocess, "run", fake)
    out = tmp_path / "result"
    monkeypatch.setattr(sys, "argv", ["harden", "--output-dir", str(out), "--execute"])
    code = module.main()
    return code, json.loads((out / "result.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("fake", [
    Fake(node_ready=False),
    Fake(nobody_can_read=True),
    Fake(trusted_kubectl_ok=False),
], ids=["node-not-ready", "unprivileged-user-can-still-read", "trusted-kubectl-fails"])
def test_failed_verification_rolls_back_unit_dropin_ownership_and_restarts(env, monkeypatch, fake):
    module, unit, chowns, chmods, tmp_path = env
    original = unit.read_bytes()
    code, result = run(module, monkeypatch, fake, tmp_path)
    assert code == 1 and result["success"] is False
    assert unit.read_bytes() == original  # the exact original unit is restored
    assert not module.DROPIN.exists()
    assert chowns[-1][1:] == (os.stat(module.KUBECONFIG).st_uid, os.stat(module.KUBECONFIG).st_gid)
    systemctl = [c for c in fake.calls if c[0] == "systemctl"]
    assert systemctl.count(["systemctl", "restart", "k3s"]) == 2  # once to apply, once to roll back
    assert "rollback_node_ready" in result
    backups = list(module.BACKUP_ROOT.iterdir())
    assert len(backups) == 1 and (backups[0] / "k3s.service").read_bytes() == original


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes are required to observe 0640")
def test_success_keeps_the_hardened_unit_and_does_not_roll_back(env, monkeypatch):
    module, unit, chowns, chmods, tmp_path = env
    fake = Fake()
    code, result = run(module, monkeypatch, fake, tmp_path)
    assert code == 0 and result["success"] is True
    assert "'640'" in unit.read_text(encoding="utf-8") and module.DROPIN.exists()
    assert [c for c in fake.calls if c[0] == "systemctl"].count(["systemctl", "restart", "k3s"]) == 1
    assert "rollback_node_ready" not in result
