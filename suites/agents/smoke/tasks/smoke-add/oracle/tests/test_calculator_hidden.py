# ruff: noqa: S101 - assertions are the hidden executable smoke-task oracle

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _calculator() -> ModuleType:
    module_path = Path("/workspace/calculator.py")
    spec = importlib.util.spec_from_file_location("calculator_hidden", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_add_handles_zero_and_negative_integers() -> None:
    calculator = _calculator()
    assert calculator.add(7, 0) == 7
    assert calculator.add(-4, 9) == 5
