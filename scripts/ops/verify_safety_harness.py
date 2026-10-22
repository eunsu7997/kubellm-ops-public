"""Opt-in isolated process verification. Synthetic sensors; no Docker, GPU load or service faults."""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from safety_harness import supervise


def verify(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    cases = []
    def safe(g):
        return g.get("invalid", False)

    def normal():
        return {"synthetic": True, "invalid": False}
    sleeping = [sys.executable, "-c", "import time; time.sleep(60)"]
    for name, command, sample, bounds, expected in [
        ("normal", [sys.executable, "-c", "print('isolated child completed')"], normal, {}, None),
        ("nonzero", [sys.executable, "-c", "raise SystemExit(7)"], normal, {}, None),
        ("deadline", sleeping, normal, {"wall_seconds": 0.5}, "hard wall deadline"),
        ("invalid-sensor", sleeping, lambda: {"synthetic": True, "invalid": True}, {},
         "GPU unsafe or sensor invalid"),
    ]:
        policy = {"wall_seconds": 4, "heartbeat_seconds": 1, "sensor_interval": 0.05}
        policy.update(bounds)
        r = supervise(command, output / name, sample, safe, **policy)
        matched = (r["success"] is (name == "normal") and r["abort"] == expected
                   and (not r["child_started"] or r["cleanup"]["reaped"]))
        cases.append({"case": name, "matched": matched, "result": r})

    calls = 0

    def stale():
        nonlocal calls
        calls += 1
        if calls > 1:
            time.sleep(0.8)
        return normal()

    r = supervise(sleeping, output / "heartbeat", stale, safe, wall_seconds=4,
                  heartbeat_seconds=0.2, sensor_interval=0.05, poll_seconds=0.02)
    cases.append({"case": "heartbeat", "matched": r["abort"] == "sensor heartbeat stale"
                  and not r["success"] and r["cleanup"]["reaped"], "result": r})
    journal = output / "failure-outcomes.jsonl"
    code = ("import json,time;from pathlib import Path;"
            f"p=Path({str(journal)!r});"
            "p.write_text(''.join(json.dumps({'request_id':i,'category':'timeout'})+'\\n' "
            "for i in range(3)),encoding='utf-8');time.sleep(60)")
    r = supervise([sys.executable, "-c", code], output / "consecutive-failures", normal, safe,
                  journal=journal, wall_seconds=4, heartbeat_seconds=1, sensor_interval=0.05)
    cases.append({"case": "consecutive-failures", "matched": r["abort"] ==
                  "3 consecutive sent-request failures" and not r["success"] and r["cleanup"]["reaped"],
                  "result": r})
    if os.name == "nt":
        armed, orphan = output / "worker-armed.txt", output / "orphan-must-not-write.txt"
        worker = (f"import time;from pathlib import Path;Path({str(armed)!r}).write_text('armed');"
                  f"time.sleep(2);Path({str(orphan)!r}).write_text('orphan')")
        code = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{worker!r}]);time.sleep(60)"
        r = supervise([sys.executable, "-c", code], output / "windows-worker-tree", normal, safe,
                      wall_seconds=1, heartbeat_seconds=2, sensor_interval=0.05)
        time.sleep(2.1)
        cases.append({"case": "windows-worker-tree", "matched": armed.exists() and not orphan.exists()
                      and r.get("cleanup", {}).get("tree") and r["cleanup"]["reaped"], "result": r,
                      "worker_armed": armed.exists(), "orphan_wrote": orphan.exists()})
    result = {"scope": "SYNTHETIC sensor + actual isolated Python subprocesses; no serving process fault",
              "finished_at_utc": datetime.now(UTC).isoformat(), "python_version": sys.version,
              "source_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                for name in ["safety_harness.py", "verify_safety_harness.py"]},
              "passed": all(c["matched"] for c in cases), "cases": cases}
    with (output / "verification.json").open("x", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--output-dir", type=Path, required=True)
    a = p.parse_args()
    if not a.execute:
        print("DRY RUN: six isolated child cases plus Windows tree case, synthetic sensor only; no services changed")
        return 0
    return verify(a.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
