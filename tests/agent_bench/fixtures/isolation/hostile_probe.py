"""Hostile namespace canary used only by the marked Linux isolation tests."""

from __future__ import annotations

import json
import os
import socket
import sys
from collections.abc import Callable
from pathlib import Path


def _succeeds(operation: Callable[[], object]) -> bool:
    try:
        operation()
    except (OSError, UnicodeError):
        return False
    return True


def _read(path: str) -> bytes:
    return Path(path).read_bytes()


def _write(path: str) -> None:
    Path(path).write_text("escaped", encoding="utf-8")


def _tcp(port: int) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=0.25) as connection:
        connection.sendall(b"tcp-canary")


def _udp(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.settimeout(0.25)
        connection.connect(("127.0.0.1", port))
        connection.send(b"udp-canary")


def _dns() -> None:
    socket.getaddrinfo("measure-twice-canary.invalid", 443)


def main() -> None:
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    workspace_file = Path("/workspace/canary-write.txt")
    workspace_file.write_text("workspace", encoding="utf-8")
    oracle_file = Path("/opt/measure-twice/oracle/oracle-sentinel.txt")
    oracle_before = oracle_file.read_text(encoding="utf-8")
    oracle_mutated = _succeeds(lambda: oracle_file.write_text("mutated", encoding="utf-8"))
    proc_environment = Path("/proc/1/environ").read_bytes()
    result = {
        "credential_read": _succeeds(lambda: _read(payload["credential_path"])),
        "dns_reached": _succeeds(_dns),
        "host_read": _succeeds(lambda: _read(payload["host_path"])),
        "oracle_before": oracle_before,
        "oracle_mutated": oracle_mutated,
        "outside_write": _succeeds(lambda: _write(payload["outside_path"])),
        "parent_credential_environment": payload["credential_value"].encode("utf-8")
        in proc_environment,
        "process_environment_credential": os.environ.get("CREDENTIAL_SENTINEL") is not None,
        "run_store_visible": Path("/run-store").exists(),
        "suite_visible": Path("/suite").exists(),
        "tcp_reached": _succeeds(lambda: _tcp(payload["tcp_port"])),
        "udp_reached": _succeeds(lambda: _udp(payload["udp_port"])),
        "workspace_write": workspace_file.read_text(encoding="utf-8") == "workspace",
    }
    sys.stdout.write(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
