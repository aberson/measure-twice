"""Deterministic evaluator resource-limit adversary; never contacts a provider or network."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def _cpu() -> None:
    value = 1
    while True:
        value = (value * 3 + 1) % 1_000_000_007


def _memory() -> None:
    blocks: list[bytearray] = []
    while True:
        blocks.append(bytearray(8 * 1024 * 1024))


def _processes() -> None:
    children: list[int] = []
    try:
        while True:
            pid = os.fork()
            if pid == 0:
                time.sleep(30)
                os._exit(0)
            children.append(pid)
    except OSError:
        print(f"process-limit:{len(children)}", flush=True)


def _files(count: int, size: int) -> None:
    for index in range(count):
        Path(f"created-{index}.bin").write_bytes(b"x" * size)


def main() -> None:
    operation = sys.argv[1]
    if operation == "cpu":
        _cpu()
    elif operation == "memory":
        _memory()
    elif operation == "processes":
        _processes()
    elif operation == "files":
        _files(int(sys.argv[2]), int(sys.argv[3]))
    else:
        raise SystemExit(f"unknown resource probe {operation!r}")


if __name__ == "__main__":
    main()
