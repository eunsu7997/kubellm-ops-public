"""Opt-in phased streaming run with hard deadlines, durable per-phase results and GPU guards."""
import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

import httpx


def gpu_guard(output, returncode):
    if returncode:
        return "GPU query failure"
    try:
        temp, free, utilization = map(float, output.strip().split(","))
        if (not all(math.isfinite(v) for v in (temp, free, utilization))
                or not 0 <= temp <= 120 or free < 0 or not 0 <= utilization <= 100):
            return "GPU query invalid"
        if temp >= 80 or free < 512:
            return "GPU safety threshold"
    except ValueError:
        return "GPU query unavailable"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--rates", type=float, nargs="+", default=[1])
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--phase-seconds", type=int, default=60)
    parser.add_argument("--max-in-flight", type=int, default=4)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if (not 1 <= args.repeats <= 60 or not 10 <= args.phase_seconds <= 120
            or any(not 0 < r <= 16 for r in args.rates) or not 1 <= args.max_in_flight <= 32
            or not 1 <= args.max_tokens <= 256 or len(args.rates) * args.repeats > 60):
        parser.error("plan exceeds bounded trial limits")
    rates = args.rates * args.repeats
    plan = {"rates": rates, "phase_seconds": args.phase_seconds, "max_tokens": args.max_tokens,
            "max_in_flight": args.max_in_flight, "temperature_stop_c": 80, "free_vram_stop_mib": 512,
            "measurement": "existing open-loop SSE harness; no SLO policy enabled",
            "warmup": "first phase of whole run excluded in aggregate analysis"}
    if not args.execute:
        print(json.dumps(plan, indent=2))
        return 0
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    completed, guard, samples = [], None, 0
    started = time.monotonic()
    with (args.output_dir / "gpu-and-ready.jsonl").open("x", encoding="utf-8") as telemetry:
        with httpx.Client(timeout=5, trust_env=False) as client:
            for index, rate in enumerate(rates):
                directory = args.output_dir / f"phase-{index:02d}"
                cmd = [sys.executable, str(Path(__file__).with_name("open_loop.py")),
                       "--base-url", "http://127.0.0.1:18003", "--model", "Qwen/Qwen2.5-0.5B-Instruct",
                       "--rate", str(rate), "--duration-s", str(args.phase_seconds), "--seed", str(4300 + index),
                       "--high-ratio", "0", "--max-in-flight", str(args.max_in_flight),
                       "--max-tokens", str(args.max_tokens), "--prompt-words", "256", "--timeout", "15",
                       "--output-dir", str(directory), "--allow-large-plan", "--execute"]
                phase_start = time.monotonic()
                with (args.output_dir / f"phase-{index:02d}-command.txt").open("x", encoding="utf-8") as log:
                    log.write("$ " + " ".join(cmd) + "\n")
                    log.flush()
                    child = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
                    try:
                        while child.poll() is None:
                            try:
                                sensor = subprocess.run([
                                    "nvidia-smi", "--query-gpu=temperature.gpu,memory.free,utilization.gpu",
                                    "--format=csv,noheader,nounits",
                                ], capture_output=True, text=True, encoding="utf-8", timeout=10)
                            except (OSError, subprocess.TimeoutExpired) as exc:
                                sensor = subprocess.CompletedProcess([], 1, type(exc).__name__, "")
                            row = {"phase": index, "elapsed": time.monotonic() - started,
                                   "gpu_output": sensor.stdout.strip(), "gpu_exit": sensor.returncode}
                            guard = gpu_guard(sensor.stdout, sensor.returncode)
                            try:
                                response = client.get("http://127.0.0.1:18003/readyz")
                                row["ready_status"] = response.status_code
                                row["ready_body"] = response.json()
                            except httpx.HTTPError as exc:
                                row["ready_error"] = type(exc).__name__
                            telemetry.write(json.dumps(row) + "\n")
                            telemetry.flush()
                            samples += 1
                            if time.monotonic() - phase_start > args.phase_seconds + 40:
                                guard = "phase wall deadline"
                            if guard:
                                break
                            time.sleep(2)
                    finally:
                        if child.poll() is None:
                            child.terminate()
                            child.wait(timeout=15)
                        log.write(f"\nEXIT CODE: {child.returncode}\n")
                if directory.joinpath("summary.json").exists():
                    summary = json.loads(directory.joinpath("summary.json").read_text(encoding="utf-8"))
                    completed.append({"phase": index, "rate": rate, "summary": summary})
                    categories = summary["categories"]
                    bad = sum(value for key, value in categories.items()
                              if key not in {"success", "dropped_client_cap"})
                    if bad:
                        guard = guard or "request failure; do not increase load"
                elif not guard:
                    guard = "harness failed without complete phase artifact"
                if child.returncode:
                    guard = guard or "harness nonzero exit"
                print(json.dumps({"phase": index, "rate": rate, "completed_phases": len(completed),
                                  "guard": guard}), flush=True)
                if guard:
                    break
    report = {"plan": plan, "elapsed_seconds": time.monotonic() - started, "gpu_ready_samples": samples,
              "completed": completed, "stop_reason": guard, "complete": len(completed) == len(rates)}
    (args.output_dir / "supervision.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["complete"] and guard is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
