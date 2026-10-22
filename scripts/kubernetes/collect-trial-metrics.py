"""Read-only localhost API snapshots; keep Grafana credentials in memory only."""
import argparse
import base64
import json
import subprocess
import time
from pathlib import Path

import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    for name, url in {
        "gateway-metrics.txt": "http://127.0.0.1:18013/metrics",
        "vllm-metrics.txt": "http://127.0.0.1:18005/metrics",
        "targets.json": "http://127.0.0.1:19092/api/v1/targets",
    }.items():
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        (args.output_dir / name).write_text(response.text, encoding="utf-8")
    result = subprocess.run([
        "wsl", "-d", "Ubuntu-24.04", "-u", "root", "--", "k3s", "kubectl",
        "-n", "kubellm-gpu-validation", "get", "secret", "kubellm-grafana-trial-auth",
        "-o", "jsonpath={.data.admin-password}",
    ], check=True, capture_output=True)
    auth = ("admin", base64.b64decode(result.stdout).decode())
    response = requests.get(
        "http://127.0.0.1:13002/api/dashboards/uid/kubellm-wsl-gpu-observed", auth=auth, timeout=15,
    )
    response.raise_for_status()
    dashboard = response.json()
    (args.output_dir / "grafana-dashboard.json").write_text(json.dumps(dashboard, indent=2), encoding="utf-8")
    queries = []
    end = int(time.time() * 1000)
    for panel in dashboard["dashboard"]["panels"]:
        for target in panel.get("targets", []):
            expr = target["expr"]
            prom = requests.get("http://127.0.0.1:19092/api/v1/query", params={"query": expr}, timeout=15)
            prom.raise_for_status()
            body = {
                "from": str(end - 300000), "to": str(end),
                "queries": [{"refId": "A", "expr": expr, "format": "time_series",
                             "datasource": {"type": "prometheus", "uid": "prometheus"},
                             "intervalMs": 1000, "maxDataPoints": 300}],
            }
            grafana = requests.post("http://127.0.0.1:13002/api/ds/query", auth=auth, json=body, timeout=20)
            queries.append({"panel": panel["title"], "expr": expr, "prometheus": prom.json(),
                            "grafana_http_status": grafana.status_code, "grafana": grafana.json()})
    (args.output_dir / "panel-queries.json").write_text(json.dumps(queries, indent=2), encoding="utf-8")
    print(f"Saved actual raw metrics and {len(queries)} panel queries; empty series are not verified metrics")


if __name__ == "__main__":
    main()
