"""Run inside WSL as the k3s operator (stdlib only). Opt-in: latest Gateway metrics image in the trial namespace.

Stops the Docker GPU vLLM, scales the trial vLLM + Gateway up, checks Service-DNS chat/SSE and the
reason-labelled readiness metrics, runs one bounded (20 s, independently resumed) EngineCore SIGSTOP, then
scales everything back to 0 and restores the Docker vLLM in `finally`. Only the kubellm-gpu-validation
namespace is touched. Default is a dry run.
"""
import argparse
import json
import subprocess
import time
from pathlib import Path

NS = "kubellm-gpu-validation"
BACKEND = "kubellm-gpu-secure-vllm-1"
METRIC_PREFIXES = (
    "gateway_backend_ready ", "gateway_backend_readiness_enabled", "gateway_backend_readiness_checks_total",
    "gateway_backend_readiness_failures_total", "gateway_backend_readiness_cache_hits_total",
    "gateway_backend_readiness_check_seconds_count",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print("DRY RUN: stop Docker GPU vLLM; trial vLLM + metrics-image Gateway; "
              "20 s independent-resume SIGSTOP; scale 0; restore")
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
                "$ " + " ".join(cmd) + "\n" + r.stdout + r.stderr + f"\nEXIT CODE: {r.returncode}\n", encoding="utf-8")
        if check and r.returncode:
            raise RuntimeError(f"command {sequence} failed: {' '.join(cmd[:4])}")
        return r

    def kub(*words, **kwargs):
        return run("k3s", "kubectl", "--request-timeout=20s", "-n", NS, *words, **kwargs)

    def get(kind, *words):
        return json.loads(kub("get", kind, *words, "-o", "json", save=False).stdout)

    def gateway_pod():
        return get("pods", "-l", "app=kubellm-gateway-trial")["items"][0]["metadata"]["name"]

    def endpoint_ready():
        slices = get("endpointslices", "-l", "kubernetes.io/service-name=kubellm-gateway-trial")
        return [e.get("conditions", {}).get("ready") for item in slices["items"] for e in item.get("endpoints", [])]

    def gw_python(code, timeout=40):
        return kub("exec", gateway_pod(), "--", "python", "-c", code, timeout=timeout, save=False)

    def metrics():
        code = ("import urllib.request;"
                "print(urllib.request.urlopen('http://localhost:8000/metrics',timeout=5).read().decode())")
        r = gw_python(code)
        return [line for line in r.stdout.splitlines() if line.startswith(METRIC_PREFIXES)]

    def chat(stream=False):
        body = "{'model':'Qwen/Qwen2.5-0.5B-Instruct','messages':[{'role':'user','content':'Reply OK'}],'max_tokens':8"
        body += ",'stream':True}" if stream else "}"
        code = ("import urllib.request,json; "
                f"data=json.dumps({body}).encode(); "
                "r=urllib.request.urlopen(urllib.request.Request('http://kubellm-gateway-trial:8000/v1/chat/completions',"
                "data=data,headers={'Content-Type':'application/json'}),timeout=25); "
                "t=r.read().decode(); print(r.status, '[DONE]' in t, len(t))")
        return gw_python(code).stdout.strip()

    original = get("deployments")
    if any(d["spec"].get("replicas", 1) != 0 for d in original["items"]):
        raise RuntimeError("Trial must start at 0 replicas; refusing to overwrite another active experiment")
    engine_pod, engine_pid, engine_start = None, None, None
    try:
        run("docker", "stop", BACKEND, timeout=90)
        kub("apply", "-f", str(args.repo / "k8s/validation/wsl-vllm-trial.yaml"))
        kub("apply", "-f", str(args.repo / "k8s/validation/wsl-gateway-trial-metrics.yaml"))
        for name in ["kubellm-vllm-trial", "kubellm-gateway-trial"]:
            run("k3s", "kubectl", "-n", NS, "rollout", "status", "deployment/" + name, "--timeout=600s", timeout=630)
        deployment = get("deployments", "kubellm-gateway-trial")
        results["gateway_image"] = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        pod = get("pods", "-l", "app=kubellm-gateway-trial")["items"][0]
        results["gateway_pod_image_id"] = pod["status"]["containerStatuses"][0]["imageID"]
        results["initial_endpoint_ready"] = endpoint_ready()
        if True not in results["initial_endpoint_ready"]:
            raise RuntimeError("No initial Ready gateway endpoint")
        results["chat_before"] = chat()
        results["sse_before"] = chat(stream=True)
        results["metrics_before"] = metrics()
        engine_pod = get("pods", "-l", "app=kubellm-vllm-trial")["items"][0]["metadata"]["name"]
        processes = kub("exec", engine_pod, "--", "ps", "-eo", "pid,comm").stdout
        matches = [int(row.split()[0]) for row in processes.splitlines() if "EngineCor" in row]
        if len(matches) != 1:
            raise RuntimeError("Ambiguous EngineCore identity")
        engine_pid = matches[0]
        engine_start = kub("exec", engine_pod, "--", "python3", "-c",
                           f"print(open('/proc/{engine_pid}/stat').read().split()[21])").stdout.strip()
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
        while time.monotonic() - started < 70:
            ready = endpoint_ready()
            try:
                m = metrics()
            except Exception:  # noqa: BLE001 - the pod may be briefly unreachable; keep sampling
                m = ["metrics unavailable"]
            timeline.append({"t": round(time.monotonic() - started, 1), "endpoint_ready": ready, "metrics": m})
            if ready and True not in ready:
                excluded = True
            if excluded and True in ready:
                returned = True
                break
            time.sleep(1)
        results.update(excluded=excluded, returned=returned, timeline=timeline, independent_resume_delay_seconds=20)
        results["metrics_after"] = metrics()
        results["chat_after"] = chat()
        results["match"] = excluded and returned and results["chat_after"].startswith("200")
    finally:
        if engine_pod and engine_pid:
            resume = ("import os,signal; "
                      f"p={engine_pid}; expected={engine_start!r}; "
                      "current=open(f'/proc/{p}/stat').read().split()[21]; "
                      "os.kill(p,signal.SIGCONT) if current==expected else None")
            kub("exec", engine_pod, "--", "python3", "-c", resume, check=False)
        kub("scale", "deployment/kubellm-vllm-trial", "deployment/kubellm-gateway-trial", "--replicas=0", check=False)
        deletion = run("k3s", "kubectl", "-n", NS, "wait", "--for=delete", "pod", "-l", "app=kubellm-vllm-trial",
                       "--timeout=120s", timeout=140, check=False)
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
        print(json.dumps({k: v for k, v in results.items() if k not in {"timeline"}}))
    if not results.get("match") or not results["docker_backend_restored"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
