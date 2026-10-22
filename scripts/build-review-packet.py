"""Build a local, self-contained review packet from allowlisted derived evidence; never uploads."""

import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence/extended-operations-20261021"


def main():
    output = ROOT / "dist/kubellm-ops-review-20261021"
    output.mkdir(parents=True, exist_ok=False)
    provenance = []

    def source(relative):
        path = EVIDENCE / relative
        raw = path.read_bytes()
        provenance.append(
            {"repository_path": path.relative_to(ROOT).as_posix(), "source_sha256": hashlib.sha256(raw).hexdigest()}
        )
        return json.loads(raw)

    soak = source("soak-baseline/analysis.json")
    readiness = source("soak-baseline/readiness-by-phase.json")
    assert soak["complete"], "Do not package an incomplete run as complete"
    hardening = source("kubeconfig-hardening-applied/result.json")
    migration = source("prometheus-migration/result.json")
    grafana = source("grafana-config-restore/result.json")
    alert = source("local-alert/result.json")
    assert all(r["success"] for r in [hardening, migration, grafana, alert])
    cleaned = {
        "soak.json": soak,
        "soak-readiness.json": readiness,
        "permissions.json": {
            k: hardening[k]
            for k in [
                "after_mode",
                "after_gid_matches",
                "node_ready",
                "trusted_user_kubectl_exit",
                "unprivileged_read_exit",
                "success",
            ]
        },
        "restores.json": {
            "prometheus_migration_history_matches": migration["migration_history_matches"],
            "prometheus_isolated_restore_history_matches": migration["isolated_restore_history_matches"],
            "grafana": grafana,
        },
        "alert.json": {
            k: alert[k]
            for k in [
                "helper_armed_before_fault",
                "maximum_helper_delay_seconds",
                "explicit_resume_span_seconds",
                "new_alert_statuses",
                "automatic_restart",
                "success",
            ]
        },
    }
    for name, data in cleaned.items():
        (output / name).write_text(json.dumps(data, indent=2), encoding="utf-8")
    readme = """# KubeLLM-Ops — Local Review Packet

FastAPI Gateway → private Docker bridge → Qwen vLLM → RTX 4060.
Prometheus → Alertmanager → local evidence receiver; Grafana queries Prometheus.

This packet contains derived, allowlisted evidence summaries. It is not the full repository or raw log archive.
Original evidence paths and SHA256 values are in [provenance.json](provenance.json).

- [Phased SSE observation](soak.json): request outcomes, unsent client-cap drops, sampled readiness and latency.
- [Readiness by phase](soak-readiness.json): all nine 503 samples occurred in the designated first warm-up phase.
- [Permissions](permissions.json): shared kubeconfig restricted; trusted kubectl works; unprivileged read denied.
- [Restores](restores.json): identical historical Prometheus queries and separate Grafana config restoration.
- [Local alert](alert.json): bounded EngineCore pause, actual local firing and resolved webhook delivery.

Readiness 503 observations and unsent requests are retained; this is not an all-requests-success/SLO claim.
The baseline used 30 phases of 60 seconds with process gaps and drain time, not a long-term production soak.
Background work and cache behavior were not controlled. Docker measurements are not Kubernetes benchmarks.

Previous repository evidence separately verified WSL k3s GPU serving and fault recovery. One physical GPU
advertised four time-slicing slots; these are not four GPUs or VRAM isolation.
Power, saturation/max stable capacity, forced OOM, fresh independent GPU-host setup, multi-node operation,
off-host disaster recovery and production readiness remain NOT VERIFIED. SLO-Guard extension is deferred.

No kubeconfig, tokens, passwords, private keys, container IDs or absolute user paths are included.
No upload, public-sharing change or reviewer invitation was performed by this packet build.
"""
    (output / "README.md").write_text(readme, encoding="utf-8")
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()}
    (output / "manifest.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
    archive = output.with_suffix(".zip")
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as z:
        for file in sorted(output.iterdir()):
            z.write(file, output.name + "/" + file.name)
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        for name, expected in hashes.items():
            assert hashlib.sha256(z.read(output.name + "/" + name)).hexdigest() == expected
    report = {
        "archive": archive.relative_to(ROOT).as_posix(),
        "files": len(hashes) + 1,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "readback_verified": True,
        "uploaded": False,
        "raw_evidence_modified": False,
    }
    (EVIDENCE / "review-packet.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
