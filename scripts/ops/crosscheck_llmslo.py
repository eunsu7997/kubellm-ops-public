"""Execute pinned llm-slo-lab pure math against real trace timings, not its CPU mock server."""
import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

EXPECTED_BLOB = "3caad8ed9923cb58f23cb611f7b6e187d96087a7"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--analysis", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    raw = a.source.read_bytes()
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    if blob != EXPECTED_BLOB:
        raise ValueError("source must match the reviewed upstream blob exactly")
    spec = importlib.util.spec_from_file_location("verified_llmslo_metrics", a.source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    expected = json.loads(a.analysis.read_text())
    groups = {}
    for path in sorted(a.run.glob("*-rows.json")):
        summary = json.loads((a.run / path.name.replace("-rows", "-summary")).read_text())
        groups.setdefault(summary["policy"], []).extend(json.loads(path.read_text()))
    checks = {}
    for policy, rows in groups.items():
        samples = [r["ttft_s"] for r in rows if r["category"] == "success"]
        value = module.percentile_nearest_rank(samples, 99)
        if value != expected[policy]["successful_client_ttft_p99_s"]:
            raise ValueError("percentile mismatch")
        checks[policy] = {"p99_s": value, "samples": len(samples), "matches": True,
                          "429_status_class": module.status_class(429)}
    result = {"source_repository": "eunsu7997/llm-slo-lab", "blob_sha": blob,
              "scope": "pure percentile/status code on actual Gateway trace, no CPU mock inference",
              "whole_project_QA": "UNVERIFIED: full project/verify.ps1 not checked out or executed",
              "checks": checks}
    with a.output.open("x", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
