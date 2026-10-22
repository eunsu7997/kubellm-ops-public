"""Bounded model-load fault in the isolated trial, with original-args recovery."""
import argparse
import json
import subprocess
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--k3s", action="store_true")
    parser.add_argument("--context")
    parser.add_argument("--namespace", default="kubellm-gpu-validation")
    parser.add_argument("--deployment", default="kubellm-vllm-trial")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.namespace != "kubellm-gpu-validation" or args.deployment != "kubellm-vllm-trial":
        parser.error("only the dedicated KubeLLM trial is supported")
    if not args.k3s and not args.context:
        parser.error("select --k3s or an explicit --context")
    kubectl = ["k3s", "kubectl"] if args.k3s else ["kubectl", "--context", args.context]
    command = [*kubectl, "--request-timeout=15s", "-n", args.namespace]
    args.output_dir.mkdir(parents=True, exist_ok=False)

    def run(name, *words, check=True, timeout=60):
        result = subprocess.run([*command, *words], capture_output=True, text=True, timeout=timeout)
        (args.output_dir / name).write_text(
            "COMMAND: " + json.dumps([*command, *words]) + "\n" + result.stdout
            + result.stderr + f"\nEXIT CODE: {result.returncode}\n", encoding="utf-8",
        )
        if check and result.returncode:
            raise RuntimeError(f"{name} failed with exit {result.returncode}")
        return result

    # Capture only necessary args/identity; do not save an entire Deployment's env.
    initial = subprocess.run(
        [*command, "get", "deployment", args.deployment, "-o", "json"],
        capture_output=True, text=True, check=True, timeout=30,
    )
    deployment = json.loads(initial.stdout)
    original = deployment["spec"]["template"]["spec"]["containers"][0]["args"]
    model_index = original.index("--model") + 1
    broken = list(original)
    broken[model_index] = "kubellm-validation/definitely-missing-model"
    selector = ",".join(f"{k}={v}" for k, v in deployment["spec"]["selector"]["matchLabels"].items())
    (args.output_dir / "plan.json").write_text(json.dumps({
        "execute": args.execute, "namespace": args.namespace, "deployment": args.deployment,
        "original_args": original, "broken_args": broken, "uid": deployment["metadata"]["uid"],
    }, indent=2), encoding="utf-8")
    if not args.execute:
        print("DRY RUN: no mutation; original model captured, restoration planned")
        return 0

    def patch(values):
        return json.dumps([{"op": "replace", "path": "/spec/template/spec/containers/0/args", "value": values}])

    mutation_attempted = False
    observed = False
    restored = False
    try:
        mutation_attempted = True
        run("01-patch.txt", "patch", "deployment", args.deployment, "--type=json", "-p", patch(broken))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            result = subprocess.run(
                [*command, "get", "pods", "-l", selector, "-o", "json"],
                capture_output=True, text=True, check=True, timeout=30,
            )
            for pod in json.loads(result.stdout)["items"]:
                if pod["spec"]["containers"][0].get("args") != broken:
                    continue
                name = pod["metadata"]["name"]
                log = subprocess.run([*command, "logs", name, "--tail=120"], capture_output=True, text=True)
                status = pod.get("status", {}).get("containerStatuses", [])
                failed = any(s.get("state", {}).get("waiting", {}).get("reason") == "CrashLoopBackOff"
                             or s.get("state", {}).get("terminated", {}).get("exitCode", 0) != 0 for s in status)
                if failed or "Traceback" in log.stdout or "OfflineModeIsEnabled" in log.stdout:
                    run("02-failed-pod.txt", "describe", "pod", name)
                    run("03-failed-logs.txt", "logs", name, "--tail=200", check=False)
                    run("04-events.txt", "get", "events", "--field-selector", f"involvedObject.name={name}")
                    observed = True
                    break
            if observed:
                break
            time.sleep(3)
    finally:
        if mutation_attempted:
            identity = subprocess.run(
                [*command, "get", "deployment", args.deployment, "-o", "jsonpath={.metadata.uid}"],
                capture_output=True, text=True, check=True, timeout=30,
            )
            if identity.stdout != deployment["metadata"]["uid"]:
                raise RuntimeError("Deployment identity changed; refusing to overwrite another object")
            run("05-restore-original-args.txt", "patch", "deployment", args.deployment,
                "--type=json", "-p", patch(original))
            result = run("06-restored-rollout.txt", "rollout", "status", f"deployment/{args.deployment}",
                         "--timeout=300s", check=False, timeout=330)
            restored = result.returncode == 0
            (args.output_dir / "result.json").write_text(json.dumps({
                "fault_observed": observed, "original_args_restored": True, "ready_rollout": restored,
            }, indent=2), encoding="utf-8")
    print(json.dumps({"fault_observed": observed, "restored_ready": restored}))
    return 0 if observed and restored else 1


if __name__ == "__main__":
    raise SystemExit(main())
