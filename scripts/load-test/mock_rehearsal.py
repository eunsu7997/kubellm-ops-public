"""Rehearse the concurrency sweep and Gateway admission without a GPU.

Starts mock-backend (with its synthetic capacity model switched on) and the Gateway as local
processes, runs concurrency_sweep.py through the Gateway twice (no limit, then
GATEWAY_MAX_INFLIGHT), and writes both reports. The numbers describe the synthetic mock only;
they check that the tools, the 429 path and the report format work end to end.

    python scripts/load-test/mock_rehearsal.py --out evidence/mock-rehearsal-20261023
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "failure-tests"))
from local_stack import ManagedProcess, _base_env, python_executable, wait_until_healthy  # noqa: E402

_spec = importlib.util.spec_from_file_location("concurrency_sweep", Path(__file__).with_name("concurrency_sweep.py"))
sweep = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sweep
_spec.loader.exec_module(sweep)

MOCK_PORT, GATEWAY_PORT = 18101, 18102


def _mock(slots: int, token_delay: float) -> ManagedProcess:
    env = _base_env({"MOCK_MAX_CONCURRENCY": str(slots), "MOCK_TOKEN_DELAY_SECONDS": str(token_delay)})
    cmd = [python_executable(), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
           "--port", str(MOCK_PORT), "--log-level", "warning"]
    return ManagedProcess("mock-backend", cmd, ROOT / "mock-backend", env)


def _gateway(max_inflight: int) -> ManagedProcess:
    env = _base_env({"VLLM_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}", "MODEL_NAME": "mock-model",
                     "REQUEST_TIMEOUT": "60", "GATEWAY_MAX_INFLIGHT": str(max_inflight)})
    cmd = [python_executable(), "-m", "uvicorn", "gateway.app.main:app", "--host", "127.0.0.1",
           "--port", str(GATEWAY_PORT), "--log-level", "warning"]
    return ManagedProcess("gateway", cmd, ROOT, env)


def run_case(name: str, out: Path, max_inflight: int, args) -> dict:
    gateway = _gateway(max_inflight)
    gateway.start()
    try:
        if not wait_until_healthy(f"http://127.0.0.1:{GATEWAY_PORT}/readyz"):
            raise SystemExit(f"gateway did not start:\n{gateway.read_output()}")
        target = out / name
        sweep.main(["--base-url", f"http://127.0.0.1:{GATEWAY_PORT}", "--model", "mock-model",
                    "--levels", args.levels, "--requests-per-level", str(args.requests_per_level),
                    "--max-tokens", str(args.max_tokens), "--cooldown", "1", "--execute", "--out", str(target)])
        return json.loads((target / "summary.json").read_text(encoding="utf-8"))
    finally:
        gateway.stop()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--slots", type=int, default=4, help="synthetic mock capacity (concurrent generations)")
    ap.add_argument("--token-delay", type=float, default=0.02, help="synthetic seconds per output token")
    ap.add_argument("--limit", type=int, default=4, help="GATEWAY_MAX_INFLIGHT for the second run")
    ap.add_argument("--levels", default="1,2,4,8,16")
    ap.add_argument("--requests-per-level", type=int, default=32)
    ap.add_argument("--max-tokens", type=int, default=32)
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=False)
    mock = _mock(args.slots, args.token_delay)
    mock.start()
    try:
        if not wait_until_healthy(f"http://127.0.0.1:{MOCK_PORT}/healthz"):
            raise SystemExit(f"mock-backend did not start:\n{mock.read_output()}")
        no_limit = run_case("no-limit", args.out, 0, args)
        limited = run_case(f"limit-{args.limit}", args.out, args.limit, args)
    finally:
        mock.stop()

    rows = []
    for label, report in (("no limit", no_limit), (f"limit {args.limit}", limited)):
        for lv in report["levels"]:
            rows.append({"case": label, **lv})
    report = {
        "synthetic": True,
        "note": "mock-backend capacity model, not GPU measurements",
        "mock": {"slots": args.slots, "token_delay_s": args.token_delay},
        "knee_no_limit": no_limit["knee_concurrency"],
        "rows": rows,
    }
    (args.out / "REHEARSAL.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print()
    print(f"{'case':<10}{'conc':>5}{'ok/sched':>10}{'rps':>8}{'ttft p99':>10}{'e2e p99':>9}")
    for r in rows:
        fmt = lambda v: "-" if v is None else f"{v:.2f}"  # noqa: E731
        print(f"{r['case']:<10}{r['concurrency']:>5}{r['success']:>5}/{r['scheduled']:<4}"
              f"{fmt(r['throughput_rps']):>8}{fmt(r['ttft_p99_s']):>10}{fmt(r['e2e_p99_s']):>9}")
    print(f"\nknee without limit: {report['knee_no_limit']} (mock slots = {args.slots})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
