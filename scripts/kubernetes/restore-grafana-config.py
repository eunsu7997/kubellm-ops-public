"""Export non-secret Grafana configuration and restore it into a separate local validation instance."""

import argparse
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

import httpx

TRIAL = "kubellm-grafana-config-restore-20261021"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--execute", action="store_true")
    a = p.parse_args()
    if not a.execute:
        print("DRY RUN: export dashboards/non-secret datasources; restore separate Grafana; restart and query")
        return 0
    a.output_dir.mkdir(parents=True, exist_ok=False)

    def docker(*args, check=True, env=None):
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, encoding="utf-8", check=check, timeout=120, env=env
        )

    # Credential exists only in process memory; never save or print the inspect result.
    inspection = json.loads(docker("inspect", "kubellm-gpu-secure-grafana-1").stdout)[0]
    settings = dict(row.split("=", 1) for row in inspection["Config"]["Env"] if "=" in row)
    original_password = settings.get("GF_SECURITY_ADMIN_PASSWORD")
    if not original_password:
        raise RuntimeError("Existing Grafana admin credential unavailable; do not reset it")
    password = secrets.token_urlsafe(32)
    environment = {**os.environ, "GF_SECURITY_ADMIN_PASSWORD": password}
    report = {
        "scope": "dashboard and non-secret datasource configuration, not full Grafana DB/users/secrets",
        "original_grafana_changed": False,
        "success": False,
    }
    with httpx.Client(
        base_url="http://127.0.0.1:13001", auth=("admin", original_password), timeout=15, trust_env=False
    ) as original:
        response = original.get("/api/datasources")
        response.raise_for_status()
        sources = []
        for source in response.json():
            if source["type"] != "prometheus" or source["url"] != "http://prometheus:9090":
                raise RuntimeError("Unexpected datasource; needs separate review")
            sources.append({key: source[key] for key in ("uid", "name", "type", "access", "url", "isDefault")})
        search = original.get("/api/search?type=dash-db")
        search.raise_for_status()
        dashboards = []
        for item in search.json():
            r = original.get("/api/dashboards/uid/" + item["uid"])
            r.raise_for_status()
            dashboard = r.json()["dashboard"]
            dashboard["id"] = None
            dashboards.append(dashboard)
        if not dashboards or not sources:
            raise RuntimeError("Expected non-empty configuration export")
    (a.output_dir / "datasources.json").write_text(json.dumps(sources, indent=2), encoding="utf-8")
    (a.output_dir / "dashboards.json").write_text(json.dumps(dashboards, indent=2), encoding="utf-8")
    volume = TRIAL + "-data"
    if docker("volume", "inspect", volume, check=False).returncode == 0:
        raise RuntimeError("Trial volume exists; refusing overwrite")
    docker("volume", "create", volume)
    created = False
    try:
        docker(
            "run",
            "-d",
            "--name",
            TRIAL,
            "--network",
            "kubellm-gpu-secure_backend",
            "-p",
            "127.0.0.1:13003:3000",
            "-e",
            "GF_SECURITY_ADMIN_PASSWORD",
            "-e",
            "GF_AUTH_ANONYMOUS_ENABLED=false",
            "-v",
            volume + ":/var/lib/grafana",
            "grafana/grafana:11.2.0",
            env=environment,
        )
        created = True
        with httpx.Client(
            base_url="http://127.0.0.1:13003", auth=("admin", password), timeout=15, trust_env=False
        ) as restored:

            def wait():
                for _ in range(60):
                    try:
                        if restored.get("/api/health").status_code == 200:
                            return
                    except httpx.HTTPError:
                        pass
                    time.sleep(1)
                raise RuntimeError("Trial Grafana readiness deadline")

            wait()
            for source in sources:
                restored.post("/api/datasources", json=source).raise_for_status()
            for dashboard in dashboards:
                restored.post(
                    "/api/dashboards/db", json={"dashboard": dashboard, "overwrite": False}
                ).raise_for_status()
            docker("restart", TRIAL)
            wait()
            checks = []
            for dashboard in dashboards:
                r = restored.get("/api/dashboards/uid/" + dashboard["uid"])
                r.raise_for_status()
                actual = r.json()["dashboard"]
                checks.append({"uid": dashboard["uid"], "panels_equal": actual["panels"] == dashboard["panels"]})
            r = restored.get(
                "/api/datasources/proxy/uid/" + sources[0]["uid"] + "/api/v1/query", params={"query": "up"}
            )
            r.raise_for_status()
            query = r.json()
            (a.output_dir / "restored-datasource-query.json").write_text(json.dumps(query, indent=2), encoding="utf-8")
            report.update(
                dashboard_checks=checks,
                datasource_count=len(sources),
                restart_performed=True,
                success=all(c["panels_equal"] for c in checks) and bool(query["data"]["result"]),
            )
    finally:
        if created:
            docker("rm", "-f", TRIAL, check=False)
        (a.output_dir / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
