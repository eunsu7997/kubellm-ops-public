"""One bounded supervisor for local operational load jobs, with fail-closed GPU and progress guards.

It stops the load generator, never the serving process. Sensor observations are independent of HTTP telemetry.
No model/provider API is used; the only subprocess is the supplied, explicitly opted-in local command.
"""
import json
import math
import os
import subprocess
import threading
import time
from pathlib import Path


class SensorWatch(threading.Thread):
    def __init__(self, path, sample, unsafe, interval=2):
        super().__init__(daemon=True)
        self.path, self.sample, self.unsafe, self.interval = path, sample, unsafe, interval
        self.stop_event = threading.Event()
        self.armed = threading.Event()
        self.last_sample_at = None
        self.abort = None
        self.rows = []

    def run(self):
        try:
            with Path(self.path).open("x", encoding="utf-8") as f:
                while not self.stop_event.is_set():
                    g = self.sample()
                    self.last_sample_at = time.monotonic()
                    row = {"observed_monotonic": self.last_sample_at, "gpu": g}
                    self.rows.append(row)
                    f.write(json.dumps(row, allow_nan=False) + "\n")
                    f.flush()
                    if self.unsafe(g):
                        self.abort = "GPU unsafe or sensor invalid"
                        break
                    self.armed.set()
                    self.stop_event.wait(self.interval)
        except Exception as exc:
            # Never serialize sensor output/exception text, which may include local/private details.
            self.abort = "sensor exception: " + type(exc).__name__
        finally:
            self.armed.set()  # wake a preflight waiter even when the sensor fails


class OutcomeJournal:
    """Tail only complete metadata lines, retaining unfinished writes until the next poll."""
    def __init__(self, path, consecutive_limit=3):
        self.path = Path(path) if path else None
        self.offset = 0
        self.partial = b""
        self.consecutive_limit = consecutive_limit
        self.consecutive_failures = 0
        self.sent_failures = 0
        self.successes = 0
        self.unsent = 0
        self.records = 0
        self.seen = set()
        self.abort = None

    def poll(self):
        if self.path is None or not self.path.exists():
            return
        with self.path.open("rb") as f:
            if f.seek(0, 2) < self.offset:
                raise ValueError("progress journal truncated")
            f.seek(self.offset)
            chunk = f.read(1024 * 1024)
            self.offset = f.tell()
        lines = (self.partial + chunk).split(b"\n")
        self.partial = lines.pop()
        if len(self.partial) > 16384:
            raise ValueError("progress line too large")
        for line in lines:
            if not line:
                raise ValueError("empty progress line")
            row = json.loads(line)
            if not isinstance(row, dict) or type(row.get("request_id")) is not int:
                raise ValueError("invalid progress identity")
            if row["request_id"] in self.seen:
                raise ValueError("duplicate outcome")
            self.seen.add(row["request_id"])
            category = row.get("category")
            if not isinstance(category, str) or not category:
                raise ValueError("invalid outcome category")
            self.records += 1
            if category == "dropped_client_cap":
                self.unsent += 1
                continue  # unsent arrivals neither fail nor reset the sent-outcome sequence
            if category == "success":
                self.successes += 1
                self.consecutive_failures = 0
            else:
                self.sent_failures += 1
                self.consecutive_failures += 1
                if self.consecutive_failures >= self.consecutive_limit:
                    self.abort = f"{self.consecutive_limit} consecutive sent-request failures"

    def drain(self):
        while self.path is not None and self.path.exists() and self.path.stat().st_size > self.offset:
            self.poll()


def stop_child(child, grace=3):
    if child.poll() is not None:
        return {"terminate": False, "kill": False, "reaped": True}
    if os.name == "nt" and hasattr(child, "pid"):
        # Windows venv python can be a launcher with a worker child. Kill the generator tree
        # before its launcher disappears; killing just the launcher can orphan the load.
        stopped = subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                 capture_output=True, timeout=max(3, grace))
        child.wait(timeout=max(3, grace))
        if stopped.returncode:
            raise RuntimeError("load generator process tree could not be stopped")
        return {"terminate": True, "kill": True, "tree": True, "reaped": child.poll() is not None,
                "taskkill_exit": stopped.returncode}
    child.terminate()
    killed = False
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        child.kill()
        killed = True
        child.wait(timeout=grace)
    return {"terminate": True, "kill": killed, "reaped": child.poll() is not None}


def supervise(command, output, sample, unsafe, *, cwd=None, journal=None, wall_seconds,
              heartbeat_seconds=8, poll_seconds=0.1, consecutive_limit=3, grace_seconds=3, sensor_interval=2,
              external_abort=None):
    """Return an explicit result on command, guard or cleanup failure; never signal a GPU backend."""
    output = Path(output)
    if (not all(math.isfinite(v) and v > 0 for v in
                (wall_seconds, heartbeat_seconds, poll_seconds, grace_seconds, sensor_interval)) or consecutive_limit < 1):
        raise ValueError("positive supervision bounds required")
    output.mkdir(parents=True, exist_ok=False)
    sensor = SensorWatch(output / "sensor.jsonl", sample, unsafe, sensor_interval)
    outcomes = OutcomeJournal(journal, consecutive_limit)
    result = {"command": list(command), "wall_limit_seconds": wall_seconds,
              "heartbeat_limit_seconds": heartbeat_seconds, "consecutive_failure_limit": consecutive_limit,
              "abort": None, "exit": None, "success": False, "child_started": False}
    child = None
    interrupted = False
    started = time.monotonic()
    try:
        sensor.start()
        if not sensor.armed.wait(timeout=heartbeat_seconds) or sensor.abort or not sensor.is_alive():
            result["abort"] = sensor.abort or "sensor did not arm"
        else:
            with (output / "child.log").open("x", encoding="utf-8") as log:
                child = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
                result["child_started"] = True
                while True:
                    # Check guards before accepting even a just-completed child as healthy.
                    now = time.monotonic()
                    outcomes.poll()
                    external_reason = external_abort() if external_abort else None
                    if sensor.abort:
                        result["abort"] = sensor.abort
                    elif not sensor.is_alive():
                        result["abort"] = "sensor thread stopped"
                    elif sensor.last_sample_at is None or now - sensor.last_sample_at > heartbeat_seconds:
                        result["abort"] = "sensor heartbeat stale"
                    elif now - started > wall_seconds:
                        result["abort"] = "hard wall deadline"
                    elif outcomes.abort:
                        result["abort"] = outcomes.abort
                    elif external_reason:
                        result["abort"] = external_reason
                    if result["abort"] or child.poll() is not None:
                        break
                    time.sleep(poll_seconds)
                result["cleanup"] = stop_child(child, grace_seconds)
                result["exit"] = child.returncode
                outcomes.drain()  # consume to EOF after child is reaped
                if outcomes.partial:
                    result["abort"] = result["abort"] or "incomplete progress line"
                result["abort"] = result["abort"] or outcomes.abort
                if journal is not None and not outcomes.records:
                    result["abort"] = result["abort"] or "no durable request outcomes"
    except KeyboardInterrupt:
        result["abort"] = result["abort"] or "operator interrupt"
        interrupted = True
    except Exception as exc:
        result["abort"] = result["abort"] or "supervisor exception: " + type(exc).__name__
    finally:
        if child is not None:
            try:
                result["cleanup"] = stop_child(child, grace_seconds) if child.poll() is None else result.get(
                    "cleanup", {"terminate": False, "kill": False, "reaped": True})
                result["exit"] = child.returncode
            except Exception as exc:
                result["cleanup_error"] = type(exc).__name__
                result["abort"] = result["abort"] or "child could not be reaped"
        sensor.stop_event.set()
        sensor.join(timeout=4)
        if sensor.is_alive():
            result["abort"] = result["abort"] or "sensor did not stop"
        result["abort"] = result["abort"] or sensor.abort
        result["elapsed_seconds"] = time.monotonic() - started
        result["outcomes"] = {"records": outcomes.records, "success": outcomes.successes,
                              "sent_failures": outcomes.sent_failures, "unsent": outcomes.unsent}
        result["sensor_samples"] = len(sensor.rows)
        result["success"] = (result["child_started"] and result["exit"] == 0 and result["abort"] is None
                             and result.get("cleanup", {}).get("reaped", False))
        (output / "result.json").write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    if interrupted:
        raise KeyboardInterrupt
    return result
