import importlib.util
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "scripts/load-test/concurrency_sweep.py"
spec = importlib.util.spec_from_file_location("concurrency_sweep", PATH)
sweep = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sweep  # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(sweep)


def _level(c, rps, ttft, err=0.0):
    return {"concurrency": c, "throughput_rps": rps, "ttft_p99_s": ttft, "error_rate": err}


def test_percentile_nearest_rank():
    assert sweep.percentile([], 99) is None
    assert sweep.percentile([3, 1, 2], 50) == 2
    assert sweep.percentile(list(range(1, 101)), 99) == 99


def test_summarize_keeps_failures_in_denominator():
    results = [sweep.Result(4, 0, "success", 0.1, 1.0, 10),
               sweep.Result(4, 1, "http_429", None, 0.01, 0)]
    s = sweep.summarize(4, results, wall_s=2.0)
    assert s["scheduled"] == 2 and s["success"] == 1
    assert s["error_rate"] == 0.5
    assert s["throughput_rps"] == 0.5
    assert s["chunks_per_s"] == 5.0


def test_knee_where_throughput_flattens_and_latency_doubles():
    levels = [_level(1, 2.0, 0.1), _level(2, 3.8, 0.12), _level(4, 6.5, 0.15),
              _level(8, 6.9, 0.35), _level(16, 7.0, 0.9)]
    assert sweep.find_knee(levels) == 4


def test_knee_stops_before_errors():
    levels = [_level(1, 2.0, 0.1), _level(2, 3.9, 0.11), _level(4, 7.0, 0.13, err=0.1)]
    assert sweep.find_knee(levels) == 2


def test_knee_is_highest_level_when_still_scaling():
    assert sweep.find_knee([_level(1, 1, 0.1), _level(2, 2, 0.1)]) == 2
    assert sweep.find_knee([]) is None


def test_level_bounds_and_dry_run(capsys):
    with pytest.raises(Exception):
        sweep.parse_levels("0,2")
    with pytest.raises(Exception):
        sweep.parse_levels("1,128")
    assert sweep.main(["--base-url", "http://127.0.0.1:9", "--model", "m"]) == 0
    assert "dry run" in capsys.readouterr().out
