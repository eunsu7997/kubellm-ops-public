"""Offline source/evidence checks; no PromQL execution or runtime validation."""
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def read(relative):
    return (ROOT / relative).read_text(encoding="utf-8-sig")


def load_yaml(relative):
    return yaml.safe_load(read(relative))


def test_dashboard_metrics_match_saved_vllm_evidence():
    dashboard = json.loads(read("monitoring/grafana/dashboards/kubellm-ops.json"))
    raw = read("evidence/gpu-vllm-local/retry-20261011/06_vllm-metrics.txt")
    observed = set(re.findall(r"^(vllm:[\w]+)(?:\{|\s)", raw, re.MULTILINE))
    expressions = " ".join(
        target["expr"] for panel in dashboard["panels"]
        for target in panel["targets"]
    )
    referenced = set(re.findall(r"vllm:[\w]+", expressions))
    assert referenced <= observed
    assert {
        "vllm:time_to_first_token_seconds_bucket",
        "vllm:request_time_per_output_token_seconds_bucket",
        "vllm:num_requests_waiting", "vllm:num_requests_running",
        "vllm:kv_cache_usage_perc", "vllm:generation_tokens_total",
    } <= referenced
    assert "1 means 100 percent" in raw
    assert "vllm:kv_cache_usage_perc * 100" in expressions
    for panel in dashboard["panels"]:
        if any(re.search(r"\b(?:DCGM_|kube_)", t["expr"]) for t in panel["targets"]):
            assert "NOT VERIFIED" in panel["title"]
        assert panel["datasource"]["uid"] == "prometheus"


@pytest.mark.parametrize("file", [
    "docker-compose.vllm-example.yml", "docker-compose.vllm-secure.yml",
])
def test_gpu_compose_monitoring_wiring(file):
    services = load_yaml(file)["services"]
    assert services["vllm"]["image"].startswith("vllm/vllm-openai@sha256:")
    assert services["prometheus"]["image"] == "prom/prometheus:v2.55.1"
    assert services["grafana"]["image"] == "grafana/grafana:11.2.0"
    assert services["gateway"]["environment"]["VLLM_BASE_URL"] == "http://vllm:8000"
    environment = services["grafana"]["environment"]
    assert environment["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert re.fullmatch(r"\$\{KUBELLM_\w+:\?.+\}", environment["GF_SECURITY_ADMIN_PASSWORD"])
    ports = [port for service in services.values() for port in service.get("ports", [])]
    assert all(port.startswith("127.0.0.1:") for port in ports)
    assert len({port.split(":")[1] for port in ports}) == len(ports)
    scrape_mount = "./monitoring/vllm-example/prometheus.yml:/etc/prometheus/prometheus.yml:ro"
    assert scrape_mount in services["prometheus"]["volumes"]
    for service in ("prometheus", "grafana"):
        for mount in services[service]["volumes"]:
            if mount.startswith("./"):
                assert (ROOT / mount.split(":")[0]).exists()
    if file.endswith("secure.yml"):
        assert "ports" not in services["vllm"]


def test_scrapes_and_grafana_provisioning():
    config = load_yaml("monitoring/vllm-example/prometheus.yml")
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}
    assert set(jobs) == {"gateway", "vllm"}
    for name, job in jobs.items():
        assert job["static_configs"] == [{"targets": [f"{name}:8000"]}]
    datasource = load_yaml("monitoring/vllm-example/provisioning/datasources/prometheus.yml")
    assert datasource["datasources"][0]["uid"] == "prometheus"
    assert datasource["datasources"][0]["url"] == "http://prometheus:9090"
    provider = load_yaml("monitoring/vllm-example/provisioning/dashboards/dashboard.yml")
    assert provider["providers"][0]["options"]["path"] == "/var/lib/grafana/dashboards"
    for path in (ROOT / "monitoring").rglob("*.yml"):
        assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8-sig")), dict)
