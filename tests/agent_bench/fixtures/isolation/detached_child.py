"""Try to outlive the Bubblewrap/process-tree owner and mutate the workspace later."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def _child(marker: str) -> None:
    if os.name == "posix":
        os.setsid()
    time.sleep(1.0)
    Path(marker).write_text("escaped", encoding="utf-8")


def main() -> None:
    if sys.argv[1] == "--child":
        _child(sys.argv[2])
        return
    marker = sys.argv[1]
    ready = sys.argv[2]
    subprocess.Popen(  # noqa: S603 - fixed self-exec canary argv.
        [sys.executable, "-I", __file__, "--child", marker]
    )
    Path(ready).write_text("ready", encoding="utf-8")
    time.sleep(30)


if __name__ == "__main__":
    main()
