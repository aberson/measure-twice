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


def _memory_fan_out(children: int, chunk: int) -> None:
    """Hold ``chunk`` bytes in each of ``children`` descendants, none near the cgroup bound.

    Only aggregate cgroup accounting can see this crossing: every descendant's own footprint is a
    small fraction of ``memory.max``, so a per-process ceiling stays silent.  Allocation is
    serialized through ``ready`` so the charge grows monotonically, and children park on ``hold``
    so earlier allocations stay resident while later siblings are still starting.
    """

    ready_read, ready_write = os.pipe()
    hold_read, hold_write = os.pipe()
    print(f"fan-out:{children}x{chunk}", flush=True)
    for index in range(children):
        pid = os.fork()
        if pid == 0:
            os.close(ready_read)
            os.close(hold_write)
            held = bytearray(chunk)
            for offset in range(0, chunk, 4096):
                held[offset] = 1
            os.write(ready_write, b"x")
            os.close(ready_write)
            os.read(hold_read, 1)
            os._exit(0)
        if os.read(ready_read, 1) != b"x":
            os._exit(3)
        print(f"allocated:{index + 1}", flush=True)
    os.close(hold_write)
    for _ in range(children):
        os.wait()
    print(f"fan-out-complete:{children}", flush=True)


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
    elif operation == "memory-fan-out":
        _memory_fan_out(int(sys.argv[2]), int(sys.argv[3]))
    elif operation == "processes":
        _processes()
    elif operation == "files":
        _files(int(sys.argv[2]), int(sys.argv[3]))
    else:
        raise SystemExit(f"unknown resource probe {operation!r}")


if __name__ == "__main__":
    main()
