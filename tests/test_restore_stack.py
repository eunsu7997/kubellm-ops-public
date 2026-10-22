"""Offline restore gates; no real Docker/WSL changes."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[1] / "scripts/ops/restore_stack.py"
SPEC = importlib.util.spec_from_file_location("restore_safety_tests", PATH)
restore = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore)


@pytest.mark.parametrize("phase,gpus,expected", [("Running", 1, True), ("Pending", 1, True),
                                                ("Succeeded", 1, False), ("Running", 0, False)])
def test_gpu_pod_resources_gate(phase, gpus, expected, monkeypatch):
    body = {"items": [{"status": {"phase": phase}, "spec": {"containers": [
        {"resources": {"limits": {"nvidia.com/gpu": str(gpus)}}}]}}]}
    monkeypatch.setattr(restore.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps(body).encode()))
    assert restore.gpu_pods_active() is expected


def test_unknown_cluster_state_is_not_no_conflict(monkeypatch):
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("fixture", 30)
    monkeypatch.setattr(restore.subprocess, "run", timeout)
    assert restore.gpu_pods_active() is None


@pytest.mark.parametrize("conflict", [True, None])
def test_stopped_model_not_started_on_conflict_or_unknown(monkeypatch, conflict):
    monkeypatch.setattr(sys, "argv", ["restore", "--execute"])
    monkeypatch.setattr(restore, "state", lambda n: "exited" if n.endswith("vllm-1") else "running")
    monkeypatch.setattr(restore, "ports_private", lambda p: True)
    monkeypatch.setattr(restore, "gpu_pods_active", lambda: conflict)
    monkeypatch.setattr(restore, "docker", lambda *a, **k: pytest.fail("no start allowed"))
    assert restore.main() == 2


def test_paused_container_requires_manual_investigation(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["restore", "--execute"])
    monkeypatch.setattr(restore, "state", lambda n: "paused")
    monkeypatch.setattr(restore, "docker", lambda *a, **k: pytest.fail("no mutation allowed"))
    assert restore.main() == 2


def test_public_binding_refused(monkeypatch):
    monkeypatch.setattr(restore, "docker", lambda *a, **k: SimpleNamespace(returncode=0,
        stdout=json.dumps({"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8002"}]})))
    assert not restore.ports_private(["kubellm-gpu-secure-gateway-1"])


def test_host_network_or_invalid_inspect_cannot_bypass_private_binding_gate(monkeypatch):
    monkeypatch.setattr(restore, "docker", lambda *a, **k: SimpleNamespace(returncode=0, stdout="host"))
    assert not restore.ports_private(["kubellm-gpu-secure-gateway-1"])
    monkeypatch.setattr(restore, "docker", lambda *a, **k: SimpleNamespace(returncode=0, stdout="PRIVATE invalid JSON"))
    assert not restore.ports_private(["kubellm-gpu-secure-gateway-1"])


def test_vllm_host_publication_refused_even_on_loopback(monkeypatch):
    def fake(*args, **kw):
        return SimpleNamespace(returncode=0, stdout="bridge" if "NetworkMode" in args[-1] else
                               json.dumps({"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8002"}]}))
    monkeypatch.setattr(restore, "docker", fake)
    assert not restore.ports_private(["kubellm-gpu-secure-vllm-1"])


def test_legacy_model_and_bad_gpu_block_model_start(monkeypatch):
    fake = SimpleNamespace(gpu=lambda: {}, gpu_unsafe=lambda g: False)
    monkeypatch.setitem(sys.modules, "oplib", fake)
    monkeypatch.setattr(restore, "docker", lambda *a, **k: SimpleNamespace(returncode=0, stdout="kubellm-vllm"))
    assert not restore.host_gpu_safe()
    monkeypatch.setattr(restore, "docker", lambda *a, **k: SimpleNamespace(returncode=0, stdout=""))
    assert restore.host_gpu_safe()
    fake.gpu_unsafe = lambda g: True
    assert not restore.host_gpu_safe()


def test_running_services_are_only_verified_without_start(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["restore", "--execute"])
    monkeypatch.setattr(restore, "state", lambda n: "running")
    monkeypatch.setattr(restore, "ports_private", lambda p: True)
    monkeypatch.setattr(restore, "docker", lambda *a, **k: SimpleNamespace(returncode=0, stdout="200"))
    monkeypatch.setattr(restore, "verify_stack", lambda: False)
    assert restore.main() == 5
