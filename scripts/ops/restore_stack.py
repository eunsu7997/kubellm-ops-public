"""Ordered restore of the existing kubellm-gpu-secure containers after a Docker engine/Desktop restart.

Starts the *existing* containers (docker start, no recreate, no build). Default is a dry run that prints the plan.
Order: vllm (wait for its /health) -> prometheus -> alertmanager -> alert-receiver -> gateway -> grafana.
Never starts the legacy stopped containers (e.g. kubellm-vllm would republish 0.0.0.0:8002).
"""
import argparse
import json
import subprocess
import sys
import time

P = "kubellm-gpu-secure-"
ORDER = ["vllm", "prometheus", "alertmanager", "alert-receiver", "gateway", "grafana"]
HEALTH = "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/health',timeout=3).status)"


def docker(*args, timeout=120):
    return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", timeout=timeout)


def gpu_pods_active():
    """No kubeconfig/credentials or Pod bodies are logged. Unknown state blocks model startup."""
    try:
        p = subprocess.run(["wsl", "-d", "Ubuntu-24.04", "--", "/usr/local/bin/k3s", "kubectl",
                            "get", "pods", "-A", "-o", "json"], capture_output=True, timeout=30)
        if p.returncode:
            return None
        data = json.loads(p.stdout.decode("utf-8"))
        for pod in data["items"]:
            if pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
                continue
            spec = pod["spec"]
            for c in spec.get("containers", []) + spec.get("initContainers", []):
                for field in ("requests", "limits"):
                    if float(c.get("resources", {}).get(field, {}).get("nvidia.com/gpu", 0)) > 0:
                        return True
        return False
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return None


def ports_private(plan):
    try:
        for name in plan:
            mode = docker("container", "inspect", name, "--format", "{{.HostConfig.NetworkMode}}")
            if mode.returncode or mode.stdout.strip() in {"", "host", "none"}:
                return False
            p = docker("container", "inspect", name, "--format", "{{json .HostConfig.PortBindings}}")
            if p.returncode:
                return False
            bindings = json.loads(p.stdout or "null") or {}
            if name.endswith("vllm-1") and bindings:
                return False
            if any(b.get("HostIp") not in {"127.0.0.1", "::1"} for bs in bindings.values() for b in (bs or [])):
                return False
        return True
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError, TypeError):
        return False


def host_gpu_safe():
    import oplib as L

    legacy = docker("ps", "--filter", "name=^/kubellm-vllm$", "--format", "{{.Names}}")
    return legacy.returncode == 0 and not legacy.stdout.strip() and not L.gpu_unsafe(L.gpu())


def verify_stack():
    import httpx
    import oplib as L

    checks = {"gateway_ready": L.http("GET", "/readyz", timeout=3)["status"] == 200,
              "chat": L.chat()["ok"], "sse_done": L.sse()["ok"]}
    for name, url in {"grafana": "http://127.0.0.1:13001/api/health",
                      "receiver": "http://127.0.0.1:19094/healthz",
                      "alertmanager": "http://127.0.0.1:19093/-/ready"}.items():
        try:
            checks[name] = httpx.get(url, timeout=5, trust_env=False).status_code == 200
        except httpx.HTTPError:
            checks[name] = False
    try:
        r = httpx.get("http://127.0.0.1:19091/api/v1/targets", timeout=5, trust_env=False).json()
        targets = r["data"]["activeTargets"]
        jobs = {t["labels"].get("job") for t in targets if t["health"] == "up"}
        checks["prometheus_gateway_vllm_targets"] = {"gateway", "vllm"}.issubset(jobs)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        checks["prometheus_gateway_vllm_targets"] = False
    print(json.dumps({"verification": checks}))
    return all(checks.values())


def state(name):
    try:
        p = docker("container", "inspect", name, "--format", "{{.State.Status}}")
        return p.stdout.strip() if p.returncode == 0 else "missing"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--model-timeout", type=int, default=420)
    args = ap.parse_args()
    if not 1 <= args.model_timeout <= 900:
        ap.error("model-timeout must be within [1,900]")
    plan = [P + s + "-1" for s in ORDER]
    for name in plan:
        print(f"{name}: {state(name)}")
    if not args.execute:
        print("dry-run: pass --execute to start the stopped containers in order")
        return 0
    for name in plan:
        if state(name) == "missing":
            print(f"missing container {name}; refusing to recreate automatically", file=sys.stderr)
            return 2
        if state(name) not in {"running", "exited", "created"}:
            print("paused/restarting/unknown container state: manual investigation required", file=sys.stderr)
            return 2
    if not ports_private(plan):
        print("public or unexpected port bindings: refusing startup", file=sys.stderr)
        return 2
    if state(plan[0]) != "running":
        if gpu_pods_active() is not False:
            print("GPU Pod conflict or unknown k3s state: refusing model startup", file=sys.stderr)
            return 2
        try:
            safe = host_gpu_safe()
        except (OSError, subprocess.TimeoutExpired):
            safe = False
        if not safe:
            print("legacy model active, GPU unsafe, or unknown host GPU state: refusing startup", file=sys.stderr)
            return 2
    for name in plan:
        if state(name) != "running":
            r = docker("start", name)
            print(f"start {name}: exit {r.returncode}")
            if r.returncode:
                print(r.stderr, file=sys.stderr)
                return 3
        if name.endswith("vllm-1"):
            deadline = time.monotonic() + args.model_timeout
            while time.monotonic() < deadline:
                r = docker("exec", name, "python3", "-c", HEALTH)
                if r.returncode == 0 and r.stdout.strip() == "200":
                    print("vllm /health 200")
                    break
                time.sleep(5)
            else:
                print("vllm did not become healthy in time", file=sys.stderr)
                return 4
    return 0 if verify_stack() else 5


if __name__ == "__main__":
    raise SystemExit(main())
