"""WSL root only. Explicit opt-in shared k3s permission change with restart and rollback."""
import argparse
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

UNIT = Path("/etc/systemd/system/k3s.service")
DROPIN = Path("/etc/systemd/system/k3s.service.d/30-kubellm-kubeconfig-group.conf")
KUBECONFIG = Path("/etc/rancher/k3s/k3s.yaml")
BACKUP_ROOT = Path("/var/lib/kubellm-validation")


def patched_unit(source):
    # k3s installs separate quoted arguments with systemd line continuations.
    pattern = r"(--write-kubeconfig-mode(?:['\"]?\s*=\s*|['\"]?\s*\\\n\s*['\"]?))(0?644)(?=[^0-9]|$)"
    updated, count = re.subn(pattern, lambda m: m[1] + "640", source)
    if count != 1:
        raise ValueError("Expected exactly one explicit 644 flag; refusing other service configurations")
    if "--write-kubeconfig-group" in source:
        raise ValueError("Existing CLI group override needs manual review")
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print("DRY RUN: root:eunsu 0640; replace unit's explicit 644 with 640; group drop-in; k3s restart")
        return 0
    import grp
    if os.geteuid() != 0:
        raise RuntimeError("WSL root required")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    if DROPIN.exists():
        raise RuntimeError("Existing drop-in; refusing overwrite")
    original = UNIT.read_bytes()
    replacement = patched_unit(original.decode("utf-8"))
    previous = KUBECONFIG.stat()
    gid = grp.getgrnam("eunsu").gr_gid
    backup = BACKUP_ROOT / ("security-backup-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    backup.mkdir(mode=0o700, parents=False, exist_ok=False)
    saved_unit = backup / "k3s.service"
    saved_unit.write_bytes(original)
    saved_unit.chmod(0o600)
    result = {"backup_contains_service_unit_only": True, "kubeconfig_content_read_or_copied": False,
              "before_mode": oct(previous.st_mode & 0o777), "target_mode": "0o640",
              "target_group": "eunsu", "restart_performed": False}

    def call(*cmd, check=True, timeout=180):
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        if check and r.returncode:
            raise RuntimeError("Command failed: " + " ".join(cmd[:3]))
        return r

    def ready():
        for _ in range(60):
            r = call("k3s", "kubectl", "--request-timeout=3s", "get", "nodes", "-o", "json", check=False, timeout=10)
            if r.returncode == 0:
                nodes = json.loads(r.stdout)["items"]
                if nodes and all(any(c["type"] == "Ready" and c["status"] == "True"
                                     for c in n["status"].get("conditions", [])) for n in nodes):
                    return True
            time.sleep(2)
        return False

    success = False
    try:
        UNIT.write_text(replacement, encoding="utf-8")
        DROPIN.parent.mkdir(parents=True, exist_ok=True)
        DROPIN.write_text("[Service]\nEnvironment=K3S_KUBECONFIG_GROUP=eunsu\n", encoding="utf-8")
        DROPIN.chmod(0o600)
        os.chown(KUBECONFIG, 0, gid)
        KUBECONFIG.chmod(0o640)
        call("systemctl", "daemon-reload")
        started = time.monotonic()
        call("systemctl", "restart", "k3s")
        result["restart_performed"] = True
        result["node_ready"] = ready()
        result["ready_observation_seconds"] = time.monotonic() - started
        current = KUBECONFIG.stat()
        result["after_mode"] = oct(current.st_mode & 0o777)
        result["after_gid_matches"] = current.st_gid == gid
        result["trusted_user_read_exit"] = call("runuser", "-u", "eunsu", "--", "test", "-r", str(KUBECONFIG),
                                                check=False).returncode
        result["unprivileged_read_exit"] = call("runuser", "-u", "nobody", "--", "test", "-r", str(KUBECONFIG),
                                                check=False).returncode
        access = call("runuser", "-u", "eunsu", "--", "k3s", "kubectl", "--request-timeout=10s",
                      "get", "nodes", "-o", "name", check=False)
        result["trusted_user_kubectl_exit"] = access.returncode
        success = (result["node_ready"] and result["after_mode"] == "0o640" and result["after_gid_matches"]
                   and result["trusted_user_read_exit"] == 0 and result["unprivileged_read_exit"] != 0
                   and access.returncode == 0)
    finally:
        if not success:
            UNIT.write_bytes(original)
            DROPIN.unlink(missing_ok=True)
            os.chown(KUBECONFIG, previous.st_uid, previous.st_gid)
            KUBECONFIG.chmod(previous.st_mode & 0o777)
            call("systemctl", "daemon-reload", check=False)
            call("systemctl", "restart", "k3s", check=False)
            result["rollback_node_ready"] = ready()
        result["success"] = success
        (args.output_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
