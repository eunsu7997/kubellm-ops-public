"""Create a runtime-only password for the isolated WSL k3s Grafana trial.

Run inside Ubuntu WSL as the k3s operator. Never prints or stores the password
in the repository; existing Secrets are preserved.
"""
import argparse
import base64
import json
import secrets
import subprocess

NAMESPACE = "kubellm-gpu-validation"
NAME = "kubellm-grafana-trial-auth"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rotate", action="store_true", help="Rotate only this isolated trial Secret")
    args = parser.parse_args()
    command = ["k3s", "kubectl", "-n", NAMESPACE]
    check = subprocess.run(
        [*command, "get", "secret", NAME, "--ignore-not-found", "-o", "name"],
        capture_output=True, text=True, check=True,
    )
    if check.stdout.strip() and not args.rotate:
        print("Existing trial Grafana Secret preserved")
        return
    manifest = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": NAME, "namespace": NAMESPACE},
        "type": "Opaque",
        "data": {"admin-password": base64.b64encode(secrets.token_urlsafe(32).encode()).decode()},
    }
    operation = ([*command, "patch", "secret", NAME, "--type=merge", "--patch-file=/dev/stdin"]
                 if check.stdout.strip() else [*command, "create", "-f", "-"])
    payload = {"data": manifest["data"]} if check.stdout.strip() else manifest
    subprocess.run(
        operation, input=json.dumps(payload),
        text=True, check=True,
    )
    print("Runtime password generated; credential value was not logged")


if __name__ == "__main__":
    main()
