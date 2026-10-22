"""Run inside WSL as k3s operator: opt-in endpoint/fallback trial; restore Docker in finally."""
import argparse
import json
import subprocess
import time
from pathlib import Path

NS = "kubellm-gpu-validation"
BACKEND = "kubellm-gpu-secure-vllm-1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print("DRY RUN: Docker GPU stop; dedicated k3s trial; 20s independent resume; scale 0; restore Docker")
        return
    args.output_dir.mkdir(parents=True, exist_ok=False)
    results = {}
    sequence = 0

    def run(*cmd, check=True, timeout=60, save=True):
        nonlocal sequence
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        if save:
            sequence += 1
            (args.output_dir / f"{sequence:03d}-command.txt").write_text(
                "$ " + " ".join(cmd) + "\n" + r.stdout + r.stderr + f"\nEXIT CODE: {r.returncode}\n",
                encoding="utf-8",
            )
        if check and r.returncode:
            raise RuntimeError(f"command {sequence} failed")
        return r

    def kub(*words, **kwargs):
        return run("k3s", "kubectl", "--request-timeout=20s", "-n", NS, *words, **kwargs)

    def get(kind, *words):
        return json.loads(kub("get", kind, *words, "-o", "json", save=False).stdout)

    def gateway_pod():
        return get("pods", "-l", "app=kubellm-gateway-trial")["items"][0]["metadata"]["name"]

    def endpoint_state():
        slices = get("endpointslices", "-l", "kubernetes.io/service-name=kubellm-gateway-trial")
        endpoints = [e for item in slices["items"] for e in item.get("endpoints", [])]
        return {"elapsed": time.monotonic() - started,
                "ready": [e.get("conditions", {}).get("ready") for e in endpoints], "raw": slices}

    original = get("deployments")
    if any(d["spec"].get("replicas", 1) != 0 for d in original["items"]):
        raise RuntimeError("Trial must start at 0 replicas; refusing to overwrite another active experiment")
    engine_pod, engine_pid, engine_start = None, None, None
    started = time.monotonic()
    try:
        run("docker", "stop", BACKEND, timeout=90)
        kub("apply", "-f", str(args.repo / "k8s/validation/wsl-vllm-trial.yaml"))
        kub("apply", "-f", str(args.repo / "k8s/validation/wsl-gateway-trial.yaml"))
        # Two consecutive failed canaries exclude traffic; liveness remains local.
        patch = {"spec": {"template": {"spec": {"containers": [{"name": "gateway", "readinessProbe": {
            "failureThreshold": 2, "timeoutSeconds": 4,
        }}]}}}}
        kub("patch", "deployment", "kubellm-gateway-trial", "--type=strategic", "-p", json.dumps(patch))
        # kubectl --request-timeout above is per API call; rollout watch has its own bound.
        for name in ["kubellm-vllm-trial", "kubellm-gateway-trial"]:
            run("k3s", "kubectl", "-n", NS, "rollout", "status", "deployment/" + name,
                "--timeout=600s", timeout=630)
        initial = endpoint_state()
        if True not in initial["ready"]:
            raise RuntimeError("No initial Ready gateway endpoint")
        results["initial"] = initial
        engine_pod = get("pods", "-l", "app=kubellm-vllm-trial")["items"][0]["metadata"]["name"]
        processes = kub("exec", engine_pod, "--", "ps", "-eo", "pid,comm").stdout
        matches = [int(row.split()[0]) for row in processes.splitlines() if "EngineCor" in row]
        if len(matches) != 1:
            raise RuntimeError("Ambiguous EngineCore identity")
        engine_pid = matches[0]
        engine_start = kub("exec", engine_pod, "--", "python3", "-c",
                           f"print(open('/proc/{engine_pid}/stat').read().split()[21])").stdout.strip()
        # Child survives the injection controller's os._exit: deliberately exercises independent fallback.
        safety = ("import time,os,signal; time.sleep(20); "
                  f"p={engine_pid}; expected={engine_start!r}; "
                  "current=open(f'/proc/{p}/stat').read().split()[21]; "
                  "os.kill(p,signal.SIGCONT) if current==expected else None")
        inject = ("import subprocess,os,signal; "
                  f"subprocess.Popen(['python3','-c',{safety!r}],start_new_session=True,"
                  "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                  f"os.kill({engine_pid},signal.SIGSTOP); os._exit(0)")
        started = time.monotonic()
        kub("exec", engine_pod, "--", "python3", "-c", inject)
        timeline, excluded, returned = [], False, False
        while time.monotonic() - started < 65:
            state = endpoint_state()
            timeline.append(state)
            if state["ready"] and True not in state["ready"]:
                excluded = True
            if excluded and True in state["ready"]:
                returned = True
                break
            time.sleep(1)
        results.update(excluded=excluded, returned=returned, timeline=timeline,
                       injection_controller_exited=True, independent_resume_delay_seconds=20)
        kub("describe", "pod", gateway_pod())
        kub("get", "events", "--sort-by=.metadata.creationTimestamp")
        request = ("import urllib.request,json; "
                   "data=json.dumps({'model':'Qwen/Qwen2.5-0.5B-Instruct',"
                   "'messages':[{'role':'user','content':'Reply OK'}],'max_tokens':8}).encode(); "
                   "r=urllib.request.urlopen(urllib.request.Request("
                   "'http://kubellm-gateway-trial:8000/v1/chat/completions',data=data,"
                   "headers={'Content-Type':'application/json'}),timeout=20); "
                   "print(r.status); print(r.read().decode())")
        chat = kub("exec", gateway_pod(), "--", "python", "-c", request)
        results["restored_service_chat"] = chat.stdout
        results["match"] = excluded and returned and chat.stdout.startswith("200\n")
    finally:
        if engine_pod and engine_pid:
            resume = ("import os,signal; "
                      f"p={engine_pid}; expected={engine_start!r}; "
                      "current=open(f'/proc/{p}/stat').read().split()[21]; "
                      "os.kill(p,signal.SIGCONT) if current==expected else None")
            kub("exec", engine_pod, "--", "python3", "-c", resume, check=False)
        kub("scale", "deployment/kubellm-vllm-trial", "deployment/kubellm-gateway-trial", "--replicas=0",
            check=False)
        # Wait for actual GPU pod deletion before restarting the Docker GPU backend.
        deletion = run("k3s", "kubectl", "-n", NS, "wait", "--for=delete", "pod",
                       "-l", "app=kubellm-vllm-trial", "--timeout=120s", timeout=140, check=False)
        if deletion.returncode:
            raise RuntimeError("GPU pod still present; refusing simultaneous Docker GPU startup")
        run("docker", "start", BACKEND)
        restored = False
        for _ in range(150):
            r = run("docker", "exec", BACKEND, "python3", "-c",
                    "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health',timeout=2).status)",
                    check=False, timeout=10, save=False)
            if r.returncode == 0 and r.stdout.strip() == "200":
                restored = True
                break
            time.sleep(2)
        results["docker_backend_restored"] = restored
        (args.output_dir / "result.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in results.items() if k not in {"timeline", "initial"}}))
    if not results.get("match") or not results["docker_backend_restored"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
