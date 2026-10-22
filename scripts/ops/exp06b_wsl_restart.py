"""Exp 6b: terminate and restart only the Ubuntu-24.04 WSL distro (k3s host); verify k3s, kubeconfig mode and Docker stack.

Docker Desktop and its containers are not stopped. kubeconfig contents are never read (stat metadata only).
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import oplib as L  # noqa: E402

WSL = ["wsl", "-d", "Ubuntu-24.04", "--", "bash", "-c"]
K = "export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; "


def w(x, label, script, timeout=60):
    return x.run(WSL + [script], label=label, timeout=timeout)


def state(x, tag):
    out = {
        "kubeconfig_meta": w(x, f"{tag}-stat", "stat -c '%a %U:%G' /etc/rancher/k3s/k3s.yaml")["stdout"].strip(),
        "dropin_exists": w(x, f"{tag}-dropin", "test -f /etc/systemd/system/k3s.service.d/30-kubellm-kubeconfig-group.conf && echo yes")["stdout"].strip(),
        "k3s_active": w(x, f"{tag}-active", "systemctl is-active k3s")["stdout"].strip(),
        "uptime": w(x, f"{tag}-uptime", "uptime -s")["stdout"].strip(),
        "nodes": w(x, f"{tag}-nodes", K + "/usr/local/bin/k3s kubectl get nodes --no-headers")["stdout"].strip(),
        "trial_deploys": w(x, f"{tag}-deploys", K + "/usr/local/bin/k3s kubectl get deploy -n kubellm-gpu-validation --no-headers")["stdout"].strip(),
        "not_running_or_completed": w(x, f"{tag}-pods", K + "/usr/local/bin/k3s kubectl get pods -A --no-headers | grep -vE 'Running|Completed' | head -20")["stdout"].strip(),
    }
    x.event(tag, **out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    if not ap.parse_args().execute:
        print("dry-run: wsl --terminate Ubuntu-24.04, restart, verify k3s")
        return 0
    x = L.Experiment("06b-wsl-ubuntu-restart" + os.environ.get("EXP_SUFFIX", ""))
    before = state(x, "before")
    x.event("docker-before", chat=L.chat()["ok"], gpu=L.gpu())
    x.run(["wsl", "--terminate", "Ubuntu-24.04"], label="terminate")
    t0 = time.monotonic()
    recovered = None
    while time.monotonic() - t0 < 300:
        r = w(x, "poll-nodes", K + "/usr/local/bin/k3s kubectl get nodes --no-headers", timeout=30)
        if r["exit"] == 0 and " Ready" in r["stdout"]:
            recovered = round(time.monotonic() - t0, 1)
            break
        time.sleep(5)
    x.event("node-ready", seconds=recovered)
    time.sleep(30)  # allow system pods (Kueue controller etc.) to settle
    after = state(x, "after")
    same = {k: before[k] == after[k] for k in ("kubeconfig_meta", "dropin_exists", "trial_deploys")}
    x.event("result", node_ready_after_s=recovered, uptime_changed=before["uptime"] != after["uptime"], same=same,
            docker_chat_ok=L.chat()["ok"], gpu=L.gpu())
    print(recovered, same)
    return 0 if recovered and all(same.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
