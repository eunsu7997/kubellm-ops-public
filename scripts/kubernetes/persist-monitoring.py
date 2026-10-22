"""Opt-in same-host Prometheus volume migration and isolated historical-query restore rehearsal."""

import argparse
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

import httpx

PROM = "kubellm-gpu-secure-prometheus-1"
DEST = "kubellm-gpu-secure_prometheus-data"
RESTORE = "kubellm-prometheus-restore-20261021"
IMAGE = "prom/prometheus:v2.55.1"
BASE = ["docker", "compose", "-f", "docker-compose.vllm-secure.yml"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print("DRY RUN: preserve original TSDB; migrate named volume; isolated restore; selected services only")
        return 0
    baseline = Path("evidence/extended-operations-20261021/soak-baseline/supervision.json")
    if not baseline.exists() or not json.loads(baseline.read_text())["complete"]:
        raise RuntimeError("Baseline must finish before monitoring cutover")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    environment = dict(os.environ)
    environment["KUBELLM_SECURE_GRAFANA_PASSWORD"] = secrets.token_urlsafe(32)  # Parse only; never select Grafana.

    def call(label, *command, check=True, timeout=180):
        r = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", env=environment, timeout=timeout)
        (args.output_dir / (label + ".txt")).write_text(
            "$ " + " ".join(command) + "\n" + r.stdout + r.stderr + f"\nEXIT CODE: {r.returncode}\n", encoding="utf-8"
        )
        if check and r.returncode:
            raise RuntimeError(label + " failed")
        return r

    def wait(url):
        for _ in range(60):
            try:
                if httpx.get(url, timeout=3, trust_env=False).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise RuntimeError("HTTP readiness deadline: " + url)

    timestamp = time.time() - 60

    def historical(port):
        r = httpx.get(
            f"http://127.0.0.1:{port}/api/v1/query",
            params={
                "query": "sum(gateway_requests_total)",
                "time": timestamp,
            },
            timeout=10,
            trust_env=False,
        )
        r.raise_for_status()
        data = r.json()
        if data["status"] != "success" or not data["data"]["result"]:
            raise RuntimeError("Historical query must have data")
        return data

    mounts = json.loads(call("00-original-mounts", "docker", "inspect", PROM, "--format", "{{json .Mounts}}").stdout)
    source = next(m["Name"] for m in mounts if m["Destination"] == "/prometheus" and m["Type"] == "volume")
    if source == DEST:
        raise RuntimeError("Already migrated; refusing overwrite")
    if call("01-destination-existence", "docker", "volume", "inspect", DEST, check=False).returncode == 0:
        raise RuntimeError("Destination exists; refusing overwrite")
    before = historical(19091)
    (args.output_dir / "02-history-before.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
    rollback = args.output_dir / "rollback.yml"
    rollback.write_text(
        "services:\n  gateway:\n    image: kubellm-ops-gateway:readiness-20261021\n"
        "  prometheus:\n    volumes:\n      - original-prometheus:/prometheus\n"
        "volumes:\n  original-prometheus:\n    external: true\n    name: " + source + "\n",
        encoding="utf-8",
    )
    result = {
        "source_volume_preserved": source,
        "destination_volume": DEST,
        "query_timestamp": timestamp,
        "success": False,
        "grafana_recreated": False,
        "vllm_recreated": False,
    }
    stopped = False
    try:
        call("03-stop-prometheus", "docker", "stop", "--time", "30", PROM)
        stopped = True
        call("04-create-volume", "docker", "volume", "create", DEST)
        call(
            "05-copy-tsdb",
            "docker",
            "run",
            "--rm",
            "--user",
            "0",
            "--entrypoint",
            "/bin/sh",
            "-v",
            source + ":/source:ro",
            "-v",
            DEST + ":/destination",
            IMAGE,
            "-ec",
            "cp -a /source/. /destination/; chown -R 65534:65534 /destination",
        )
        call("06-compose-valid", *BASE, "-f", "docker-compose.operations.yml", "config", "--quiet")
        call(
            "07-selected-services",
            *BASE,
            "-f",
            "docker-compose.operations.yml",
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "gateway",
            "prometheus",
            "alertmanager",
            "alert-receiver",
        )
        wait("http://127.0.0.1:19091/-/ready")
        wait("http://127.0.0.1:18003/readyz")
        after = historical(19091)
        result["migration_history_matches"] = before["data"] == after["data"]
        (args.output_dir / "08-history-after.json").write_text(json.dumps(after, indent=2), encoding="utf-8")
        if not result["migration_history_matches"]:
            raise RuntimeError("Migrated historical value differs")
        restore_volume = RESTORE + "-data"
        if (
            call("09-restore-volume-existence", "docker", "volume", "inspect", restore_volume, check=False).returncode
            == 0
        ):
            raise RuntimeError("Restore destination exists; refusing overwrite")
        call("10-create-restore-volume", "docker", "volume", "create", restore_volume)
        call(
            "11-copy-restore",
            "docker",
            "run",
            "--rm",
            "--user",
            "0",
            "--entrypoint",
            "/bin/sh",
            "-v",
            source + ":/source:ro",
            "-v",
            restore_volume + ":/destination",
            IMAGE,
            "-ec",
            "cp -a /source/. /destination/; chown -R 65534:65534 /destination",
        )
        config = str(Path("monitoring/operations/restore-prometheus.yml").resolve())
        call(
            "12-isolated-restore",
            "docker",
            "run",
            "-d",
            "--name",
            RESTORE,
            "--network",
            "kubellm-gpu-secure_backend",
            "-p",
            "127.0.0.1:19095:9090",
            "-v",
            restore_volume + ":/prometheus",
            "-v",
            config + ":/etc/prometheus/prometheus.yml:ro",
            IMAGE,
        )
        wait("http://127.0.0.1:19095/-/ready")
        restored = historical(19095)
        (args.output_dir / "13-history-restored.json").write_text(json.dumps(restored, indent=2), encoding="utf-8")
        result["isolated_restore_history_matches"] = before["data"] == restored["data"]
        if not result["isolated_restore_history_matches"]:
            raise RuntimeError("Restored historical value differs")
        payload = {
            "model": "Qwen/Qwen2.5-0.5B-Instruct",
            "messages": [{"role": "user", "content": "Reply OK"}],
            "max_tokens": 8,
        }
        chat = httpx.post("http://127.0.0.1:18003/v1/chat/completions", json=payload, timeout=20, trust_env=False)
        (args.output_dir / "14-chat-after.json").write_text(
            json.dumps({"status": chat.status_code, "body": chat.json()}, indent=2)
        )
        chat.raise_for_status()
        stream = httpx.post(
            "http://127.0.0.1:18003/v1/chat/completions",
            json={**payload, "stream": True},
            timeout=20,
            trust_env=False,
        )
        stream_result = {"status": stream.status_code, "body": stream.text, "done": "[DONE]" in stream.text}
        (args.output_dir / "14-stream-after.json").write_text(json.dumps(stream_result, indent=2), encoding="utf-8")
        if stream.status_code != 200 or not stream_result["done"]:
            raise RuntimeError("Actual streaming smoke failed")
        result["success"] = True
    finally:
        call("15-remove-isolated-container", "docker", "rm", "-f", RESTORE, check=False)
        if stopped and not result["success"]:
            r = call(
                "failure-rollback",
                *BASE,
                "-f",
                str(rollback),
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "gateway",
                "prometheus",
                check=False,
            )
            result["rollback_exit"] = r.returncode
        (args.output_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
