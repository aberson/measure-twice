# ruff: noqa: S101 - assertions are the executable smoke-task oracle

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _calculator() -> ModuleType:
    module_path = Path(__file__).parents[1] / "calculator.py"
    spec = importlib.util.spec_from_file_location("calculator", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_adds_two_positive_integers() -> None:
    assert _calculator().add(2, 3) == 5
