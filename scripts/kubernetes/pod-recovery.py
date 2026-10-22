"""Opt-in recovery trials restricted to the dedicated WSL k3s vLLM workload."""
import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["pod-delete", "engine-freeze", "api-freeze"], required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    cmd = ["k3s", "kubectl", "--request-timeout=15s", "-n", "kubellm-gpu-validation"]
    args.output_dir.mkdir(parents=True, exist_ok=False)

    def call(*words, check=True, timeout=40):
        result = subprocess.run([*cmd, *words], capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(result.stderr)
        return result

    def save(name, result):
        (args.output_dir / name).write_text(
            result.stdout + result.stderr + f"\nEXIT CODE: {result.returncode}\n", encoding="utf-8",
        )

    def pods():
        return json.loads(call("get", "pods", "-l", "app=kubellm-vllm-trial", "-o", "json").stdout)["items"]

    def ready(pod):
        return any(c["type"] == "Ready" and c["status"] == "True" for c in pod["status"].get("conditions", []))

    original = next(p for p in pods() if ready(p) and not p["metadata"].get("deletionTimestamp"))
    name = original["metadata"]["name"]
    initial = original["status"]["containerStatuses"][0]
    plan = {"mode": args.mode, "pod": name, "execute": args.execute,
            "initial_container_id": initial["containerID"], "initial_restarts": initial["restartCount"]}
    (args.output_dir / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    if not args.execute:
        print("DRY RUN: no mutation")
        return 0
    started = time.monotonic()
    engine_pid = None
    host_pid = None
    success = False
    observations = []
    final_name = name
    try:
        if args.mode == "pod-delete":
            save("01-delete.txt", call("delete", "pod", name, "--wait=false"))
        elif args.mode == "engine-freeze":
            result = call("exec", name, "--", "ps", "-eo", "pid,comm")
            save("01-processes.txt", result)
            engine_pid = next(int(line.split()[0]) for line in result.stdout.splitlines() if "EngineCor" in line)
            save("02-freeze.txt", call("exec", name, "--", "python3", "-c",
                                      f"import os,signal; os.kill({engine_pid},signal.SIGSTOP)"))
        else:
            container_id = initial["containerID"].removeprefix("containerd://")
            inspection = subprocess.run(
                ["k3s", "crictl", "inspect", container_id], capture_output=True,
                text=True, check=True, timeout=30,
            )
            host_pid = int(json.loads(inspection.stdout)["info"]["pid"])
            if container_id not in Path(f"/proc/{host_pid}/cgroup").read_text():
                raise RuntimeError("Container identity mismatch; refusing to signal host process")
            (args.output_dir / "01-api-process.json").write_text(json.dumps({
                "container_id": container_id, "verified_host_pid": host_pid,
                "action": "SIGSTOP from ancestor PID namespace",
            }, indent=2), encoding="utf-8")
            os.kill(host_pid, signal.SIGSTOP)
        deadline = started + 420
        while time.monotonic() < deadline:
            current = pods()
            for pod in current:
                status = pod["status"].get("containerStatuses", [{}])[0]
                observations.append({"elapsed_seconds": time.monotonic() - started,
                                     "pod": pod["metadata"]["name"], "phase": pod["status"].get("phase"),
                                     "ready": ready(pod), "restarts": status.get("restartCount", 0)})
                changed = (pod["metadata"]["name"] != name if args.mode == "pod-delete"
                           else status.get("restartCount", 0) > initial["restartCount"])
                if changed and ready(pod) and not pod["metadata"].get("deletionTimestamp"):
                    success = True
                    final_name = pod["metadata"]["name"]
                    break
            if success:
                break
            # If freezing did not cause a restart in 150 seconds, resume the engine.
            if (engine_pid is not None or host_pid is not None) and time.monotonic() - started > 150:
                restarted = any(p["status"].get("containerStatuses", [{}])[0].get("restartCount", 0)
                                > initial["restartCount"] for p in current)
                if not restarted:
                    break
            time.sleep(3)
    finally:
        if engine_pid is not None or host_pid is not None:
            current = pods()
            for pod in current:
                statuses = pod["status"].get("containerStatuses", [])
                same_container = statuses and statuses[0].get("containerID") == initial["containerID"]
                if pod["metadata"]["name"] == name and same_container:
                    if host_pid is not None:
                        try:
                            os.kill(host_pid, signal.SIGCONT)
                        except ProcessLookupError:
                            pass
                    else:
                        save("03-resume-safety.txt", call("exec", name, "--", "python3", "-c",
                                                         f"import os,signal; os.kill({engine_pid},signal.SIGCONT)",
                                                         check=False))
        save("04-events.txt", call("get", "events", "--field-selector", f"involvedObject.name={name}", check=False))
        save("05-final-pod.txt", call("describe", "pod", final_name, check=False))
        save("06-final-logs.txt", call("logs", final_name, "--tail=120", check=False))
        (args.output_dir / "timeline.json").write_text(json.dumps(observations, indent=2), encoding="utf-8")
        result = {"mode": args.mode, "ready_recovered": success, "final_pod": final_name,
                  "elapsed_seconds": time.monotonic() - started}
        (args.output_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
