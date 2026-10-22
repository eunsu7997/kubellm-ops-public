"""Opt-in migration and rollback rehearsal, gateway only; never recreates the GPU backend."""
import argparse
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

import httpx

OLD = "kubellm-gateway-streaming-candidate-20261021"
NEW = "kubellm-gpu-secure-gateway-1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print("DRY RUN: stop old gateway; compose up gateway only; rehearse rollback; retain new gateway")
        return
    args.output_dir.mkdir(parents=True, exist_ok=False)
    environment = dict(os.environ)
    # Parse-only value: Grafana is never selected or recreated by this command.
    environment["KUBELLM_SECURE_GRAFANA_PASSWORD"] = secrets.token_urlsafe(32)

    def call(label, *cmd, check=True):
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120, env=environment)
        (args.output_dir / f"{label}.txt").write_text(
            "$ " + " ".join(cmd) + "\n" + r.stdout + r.stderr + f"\nEXIT CODE: {r.returncode}\n", encoding="utf-8",
        )
        if check and r.returncode:
            raise RuntimeError(f"{label} failed (see evidence)")
        return r

    def smoke(label):
        with httpx.Client(timeout=20, trust_env=False) as c:
            deadline = time.monotonic() + 60
            while True:
                try:
                    ready = c.get("http://127.0.0.1:18003/readyz")
                    if ready.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError("Gateway readiness deadline")
                time.sleep(1)
            payload = {"model": "Qwen/Qwen2.5-0.5B-Instruct",
                       "messages": [{"role": "user", "content": "Reply with OK"}], "max_tokens": 8}
            chat = c.post("http://127.0.0.1:18003/v1/chat/completions", json=payload)
            sse = c.post("http://127.0.0.1:18003/v1/chat/completions", json={**payload, "stream": True})
            result = {"ready_status": ready.status_code, "chat_status": chat.status_code,
                      "chat": chat.json(), "sse_status": sse.status_code, "sse": sse.text,
                      "sse_done": "[DONE]" in sse.text}
            (args.output_dir / f"{label}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            if chat.status_code != 200 or sse.status_code != 200 or not result["sse_done"]:
                raise RuntimeError("Actual chat/SSE smoke failed")

    call("00-compose-valid", "docker", "compose", "-f", "docker-compose.vllm-secure.yml", "config", "--quiet")
    smoke("01-before")
    success = False
    try:
        call("02-stop-old", "docker", "stop", OLD)
        call("03-compose-gateway-only", "docker", "compose", "-f", "docker-compose.vllm-secure.yml",
             "up", "-d", "--no-deps", "--no-build", "gateway")
        smoke("04-new")
        call("05-rollback-stop-new", "docker", "stop", NEW)
        call("06-rollback-start-old", "docker", "start", OLD)
        smoke("07-rollback")
        call("08-stop-old-again", "docker", "stop", OLD)
        call("09-start-new-final", "docker", "start", NEW)
        smoke("10-final")
        call("11-final-image", "docker", "inspect", NEW, "--format", "{{.Image}} {{.State.Health.Status}}")
        success = True
    finally:
        if not success:
            call("failure-stop-new", "docker", "stop", NEW, check=False)
            call("failure-restore-old", "docker", "start", OLD, check=False)
    print(json.dumps({"new_gateway_active": success, "rollback_rehearsed": success,
                      "old_gateway_preserved": True, "gpu_backend_recreated": False}))


if __name__ == "__main__":
    main()
