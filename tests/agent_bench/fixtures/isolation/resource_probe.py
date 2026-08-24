"""Deterministic evaluator resource-limit adversary; never contacts a provider or network."""

from __future__ import annotations

import errno
import os
import signal
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


def _parallel_writers(workers: int, chunk_bytes: int) -> None:
    """Fill the private tmpfs from concurrent writers and leave it physically full.

    Nothing is deleted: the retained parent FD must still prove exhaustion after this exits.
    """

    # RLIMIT_FSIZE is a per-file backstop, never the aggregate boundary.  Ignoring SIGXFSZ turns a
    # single racing writer's overshoot into a clean EFBIG stop so the other writers fill the tail.
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    chunk = b"w" * chunk_bytes
    children: list[int] = []
    for index in range(workers):
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                with Path(f"writer-{index}.bin").open("wb", buffering=0) as handle:
                    while True:
                        handle.write(chunk)
            except OSError as exc:
                if exc.errno not in {errno.ENOSPC, errno.EDQUOT, errno.EFBIG}:
                    code = 2
            os._exit(code)
        children.append(pid)
    stopped = 0
    for pid in children:
        _, status = os.waitpid(pid, 0)
        if os.waitstatus_to_exitcode(status) == 0:
            stopped += 1
    usage = os.statvfs(".")
    print(f"parallel-writers:{stopped}:{usage.f_bavail}", flush=True)


def _inodes(files: int, size: int, max_dirs: int) -> None:
    """Exhaust the private tmpfs inode budget with a maximally structured *legal* tree.

    ``max_dirs`` directories plus ``files`` files plus the tmpfs root inode is exactly the
    ``EVALUATOR_DIRECTORY_ALLOWANCE`` budget, and both the per-directory entry bound and the
    whole-tree directory bound stay satisfied, so the terminal walker must not see a policy
    violation.
    """

    for index in range(files):
        Path(f"small-{index}.bin").write_bytes(b"x" * size)
    created = 0
    branch = Path(".")
    while created < max_dirs and os.statvfs(".").f_favail > 0:
        target = Path(f"d{created // 100}") if created % 100 == 0 else branch / f"s{created}"
        try:
            target.mkdir()
        except OSError as exc:
            if exc.errno != errno.ENOSPC:
                raise
            break
        if created % 100 == 0:
            branch = target
        created += 1
    usage = os.statvfs(".")
    print(f"inodes:{files}:{created}:{usage.f_favail}", flush=True)


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
    elif operation == "parallel-writers":
        _parallel_writers(int(sys.argv[2]), int(sys.argv[3]))
    elif operation == "inodes":
        _inodes(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
    else:
        raise SystemExit(f"unknown resource probe {operation!r}")


if __name__ == "__main__":
    main()
