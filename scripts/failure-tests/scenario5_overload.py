"""Legacy scenario 5 entry point: delegate to the bounded, dry-run-first harness.

No process startup, shutdown, or automatic concurrency sweep.
"""
import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "load-test" / "load_test.py"), run_name="__main__")
