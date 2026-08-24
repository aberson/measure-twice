"""Detach a descendant that deliberately outlives a cleanly-exiting evaluator target."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def _child(marker: str, ready: str, release: str) -> None:
    if os.name == "posix":
        os.setsid()
    Path(ready).write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 10
    release_path = Path(release)
    while not release_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if release_path.exists():
        Path(marker).write_text("escaped", encoding="utf-8")


def main() -> None:
    if sys.argv[1] == "--child":
        _child(sys.argv[2], sys.argv[3], sys.argv[4])
        return
    marker, ready, release = sys.argv[1], sys.argv[2], sys.argv[3]
    subprocess.Popen(  # noqa: S603 - fixed self-exec canary argv.
        [sys.executable, "-I", __file__, "--child", marker, ready, release]
    )
    # Exit 0 as the nominal target only once the detached descendant is demonstrably live.
    deadline = time.monotonic() + 10
    ready_path = Path(ready)
    while not ready_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)


if __name__ == "__main__":
    main()
