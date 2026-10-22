"""Offline safety tests; these do not constitute live Kubernetes evidence."""
import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


def load(name):
    path = Path(__file__).parents[1] / "scripts" / "kubernetes" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_kubeconfig_hardening_preserves_other_service_arguments():
    module = load("harden-shared-kubeconfig")
    source = "ExecStart=/usr/local/bin/k3s server '--write-kubeconfig-mode=644' '--node-name=keepme'\n"
    assert module.patched_unit(source) == source.replace("mode=644", "mode=640")


def test_kubeconfig_hardening_handles_installed_systemd_continuation():
    source = "'--write-kubeconfig-mode' " + "\\\n\t'644' \\\n\t'--disable'"
    assert load("harden-shared-kubeconfig").patched_unit(source) == source.replace("'644'", "'640'")


@pytest.mark.parametrize("source", [
    "ExecStart=k3s server", "--write-kubeconfig-mode=640",
    "--write-kubeconfig-mode=644 --write-kubeconfig-mode=644",
    "--write-kubeconfig-mode=644 --write-kubeconfig-group=other",
])
def test_kubeconfig_hardening_refuses_unreviewed_units(source):
    with pytest.raises(ValueError):
        load("harden-shared-kubeconfig").patched_unit(source)


def deployment():
    return {"metadata": {"uid": "original-uid"}, "spec": {
        "selector": {"matchLabels": {"app": "kubellm-vllm-trial"}},
        "template": {"spec": {"containers": [{"args": ["--model", "Qwen/test", "--revision", "pinned"]}]}},
    }}


def test_model_fault_dry_run_never_patches(monkeypatch, tmp_path):
    module = load("model-load-failure")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(deployment()), "")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["fault", "--k3s", "--output-dir", str(tmp_path / "dry")])
    assert module.main() == 0
    assert all("patch" not in command for command in calls)


@pytest.mark.parametrize("identity_changed", [False, True])
def test_failed_fault_patch_restores_only_original_object(monkeypatch, tmp_path, identity_changed):
    module = load("model-load-failure")
    patches = []

    def run(command, **kwargs):
        if "patch" in command:
            values = json.loads(command[-1])[0]["value"]
            patches.append(values)
            return subprocess.CompletedProcess(command, 1 if len(patches) == 1 else 0, "", "failure")
        if command[-1] == "jsonpath={.metadata.uid}":
            return subprocess.CompletedProcess(command, 0, "other-uid" if identity_changed else "original-uid", "")
        if "rollout" in command:
            return subprocess.CompletedProcess(command, 0, "restored", "")
        return subprocess.CompletedProcess(command, 0, json.dumps(deployment()), "")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["fault", "--k3s", "--output-dir", str(tmp_path / "fault"), "--execute"])
    with pytest.raises(RuntimeError):
        module.main()
    if identity_changed:
        assert len(patches) == 1  # Never restore into a concurrently replaced Deployment.
    else:
        assert patches[1] == ["--model", "Qwen/test", "--revision", "pinned"]


def test_grafana_password_is_environment_safe_and_not_logged(monkeypatch, capsys):
    module = load("create-trial-grafana-secret")
    manifests = []

    def run(command, **kwargs):
        if "create" in command:
            manifests.append(json.loads(kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["secret"])
    module.main()
    password = base64.b64decode(manifests[0]["data"]["admin-password"])
    assert password.isascii() and b"\x00" not in password and len(password) >= 32
    assert password.decode() not in capsys.readouterr().out


def test_existing_grafana_secret_is_preserved(monkeypatch):
    module = load("create-trial-grafana-secret")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "secret/existing", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["secret"])
    module.main()
    assert len(calls) == 1 and "get" in calls[0]
