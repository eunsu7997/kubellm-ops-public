import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/load-test/supervised-soak.py"


@pytest.mark.parametrize("output,code,expected", [
    ("60, 3000, 90", 0, None), ("80, 3000, 90", 0, "GPU safety threshold"),
    ("60, 511, 90", 0, "GPU safety threshold"), ("nan, 3000, 90", 0, "GPU query invalid"),
    ("60, -1, 90", 0, "GPU query invalid"), ("", 0, "GPU query unavailable"),
    ("60, 3000, 90", 1, "GPU query failure"),
])
def test_gpu_guard_fails_closed(output, code, expected):
    spec = importlib.util.spec_from_file_location("supervised_soak", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.gpu_guard(output, code) == expected
