"""Bounded, environment-explicit subprocess execution for coding-agent cells.

The response-only adapter predates workspace mutation and intentionally has a much smaller
subprocess seam.  Agent cells need a stricter contract: requests are immutable, stdin is encoded
as UTF-8 explicitly, no ambient environment is inherited, captured streams have hard byte
ceilings, and every terminal path reaps the whole process tree.

This module knows nothing about provider event formats.  It returns bytes so an adapter can parse
its own protocol without locale decoding or replacement changing the evidence.
"""

from __future__ import annotations

import array
import ctypes
import errno
import math
import os
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any, Final, Literal, Self, cast

from measure_twice.agent_bench._linux_capabilities import (
    LinuxCapabilityError,
    LinuxPathCapability,
    LinuxTreeDriftError,
    LinuxTreeLimitError,
    LinuxTreePolicyError,
    copy_tree,
    walk_tree,
)
from measure_twice.agent_bench.models import (
    EVALUATOR_DIRECTORY_ALLOWANCE,
    evaluator_tmpfs_minimum_bytes,
)

_READ_CHUNK_BYTES: Final[int] = 64 * 1024
_POLL_INTERVAL_S: Final[float] = 0.01
_TREE_POLL_INTERVAL_S: Final[float] = 0.05
_REAP_TIMEOUT_S: Final[float] = 5.0
_TASKKILL_TIMEOUT_S: Final[float] = 10.0
_REDACTION: Final[bytes] = b"<redacted>"
_MAX_SECRET_BYTES: Final[int] = 4096
_WINDOWS_CREATE_SUSPENDED: Final[int] = 0x00000004
_LINUX_RUN_LOCK = threading.Lock()
_LINUX_NAMESPACE_EXECUTABLE: Final[str] = "/usr/bin/unshare"
_LINUX_SUPERVISOR_EXECUTABLE: Final[str] = "/usr/bin/python3"
_LINUX_SYSTEMD_RUN_EXECUTABLE: Final[str] = "/usr/bin/systemd-run"
# magic, wait_status, exec_error, cpu_microseconds(q), hard_limit, hard_observed(q),
# setup_error.  hard_observed carries cgroup memory.peak and tmpfs byte capacity, both
# sourced from unbounded Ceilings values -- a signed 32-bit field overflowed at a >2 GiB
# ceiling, and the terminal _write_status runs OUTSIDE the supervisor's guarded try, so
# struct.error escaped and a real hard cgroup event surfaced as "invalid status record".
_SUPERVISOR_STATUS_FORMAT: Final[str] = "!4siiqiqi"
_SUPERVISOR_STATUS = struct.Struct(_SUPERVISOR_STATUS_FORMAT)
_SUPERVISOR_MAGIC: Final[bytes] = b"MT26"
EVALUATOR_WORKSPACE_FD_TOKEN: Final[str] = "__measure_twice_evaluator_workspace_fd__"  # noqa: S105 - argv placeholder, not a secret.
_TMPFS_SUPER_MAGIC: Final[int] = 0x01021994
_CGROUP2_SUPER_MAGIC: Final[int] = 0x63677270
# Bounds the pre-release bring-up ONLY: systemd-run scope creation, cgroup delegation and
# readback, the bounded private tmpfs, supervisor binding, and the SCM_RIGHTS handshake.
# It is deliberately independent of ProcessRequest.timeout_s -- no submitted byte executes
# before the release barrier, so the target's wall clock cannot be the right budget here.
SANDBOX_SETUP_TIMEOUT_S: Final[float] = 10.0
_SUPERVISOR_CODE_TEMPLATE: Final[str] = """
import errno
import ctypes
import os
import resource
import signal
import socket
import struct
import sys

status_fd = int(sys.argv[1])
target_fd = int(sys.argv[2])
control_fd = int(sys.argv[3])
guard_enabled = bool(int(sys.argv[4]))
environment_count = int(sys.argv[5])
file_size_limit = int(sys.argv[6])
open_files_limit = int(sys.argv[7])
memory_limit = int(sys.argv[8])
process_limit = int(sys.argv[9])
cpu_percent = int(sys.argv[10])
tmpfs_bytes = int(sys.argv[11])
tmpfs_inodes = int(sys.argv[12])
expected_cgroup_relative = sys.argv[13]
environment_items = sys.argv[14:14 + environment_count * 2]
target_environment = dict(zip(environment_items[::2], environment_items[1::2], strict=True))
target_argv = sys.argv[14 + environment_count * 2:]
os.set_inheritable(status_fd, False)
if control_fd >= 0:
    os.set_inheritable(control_fd, False)


def _write_status(
    wait_status, exec_error, cpu_microseconds, hard_limit, hard_observed, setup_error
):
    payload = struct.pack(
        "__MT_STATUS_FORMAT__",
        b"__MT_STATUS_MAGIC__",
        wait_status,
        exec_error,
        cpu_microseconds,
        hard_limit,
        hard_observed,
        setup_error,
    )
    offset = 0
    while offset < len(payload):
        offset += os.write(status_fd, payload[offset:])


def _read_at(directory_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        chunks = []
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                return b"".join(chunks).decode("ascii")
            chunks.append(chunk)
    finally:
        os.close(fd)


def _counter_map(directory_fd, name):
    values = {}
    for line in _read_at(directory_fd, name).splitlines():
        key, value = line.split(maxsplit=1)
        values[key] = int(value)
    return values


def _required_counter(values, name, key):
    value = values.get(key)
    if value is None or value < 0:
        raise OSError(errno.EPERM, f"{name} lacks nonnegative {key}")
    return value


def _nonnegative_value(directory_fd, name):
    try:
        value = int(_read_at(directory_fd, name).strip())
    except ValueError as exc:
        raise OSError(errno.EPERM, f"{name} is malformed") from exc
    if value < 0:
        raise OSError(errno.EPERM, f"{name} is negative")
    return value


def _verify_cgroup2(directory_fd):
    # fstatfs starts with ``long f_type`` on the supported LP64 Linux ABI.  A fixed
    # buffer avoids depending on the remaining ABI-private statfs layout here.
    buffer = ctypes.create_string_buffer(256)
    libc = ctypes.CDLL(None, use_errno=True)
    fstatfs = libc.fstatfs
    fstatfs.argtypes = [ctypes.c_int, ctypes.c_void_p]
    fstatfs.restype = ctypes.c_int
    if fstatfs(directory_fd, buffer) != 0:
        raise OSError(ctypes.get_errno(), "could not inspect resource scope filesystem")
    if ctypes.c_long.from_buffer(buffer).value != 0x63677270:
        raise OSError(errno.EPERM, "resource scope is not cgroup v2")


def _cgroup_directory(expected_relative):
    relative = None
    with open("/proc/self/cgroup", encoding="ascii") as source:
        for line in source:
            if line.startswith("0::"):
                relative = line.rstrip("\\n").split("::", 1)[1]
                break
    if (
        relative is None
        or not relative.startswith("/")
        or relative != expected_relative
        or any(part in {"", ".", ".."} for part in relative.split("/")[1:])
    ):
        raise OSError(errno.EINVAL, "unified cgroup v2 membership is unavailable")
    directory_fd = os.open(
        os.path.join("/sys/fs/cgroup", relative.lstrip("/")),
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        _verify_cgroup2(directory_fd)
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _verify_controls(directory_fd):
    if _read_at(directory_fd, "memory.max").strip() != str(memory_limit):
        raise OSError(errno.EPERM, "memory.max readback mismatch")
    if _read_at(directory_fd, "memory.swap.max").strip() != "0":
        raise OSError(errno.EPERM, "memory.swap.max readback mismatch")
    if _read_at(directory_fd, "pids.max").strip() != str(process_limit):
        raise OSError(errno.EPERM, "pids.max readback mismatch")
    pieces = _read_at(directory_fd, "cpu.max").split()
    if len(pieces) != 2 or pieces[0] == "max":
        raise OSError(errno.EPERM, "cpu.max readback is unavailable")
    quota, period = (int(piece) for piece in pieces)
    if quota <= 0 or period <= 0 or quota * 100 != cpu_percent * period:
        raise OSError(errno.EPERM, "cpu.max readback mismatch")


def _mount_tmpfs(path):
    libc = ctypes.CDLL(None, use_errno=True)
    mount = libc.mount
    mount.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_char_p,
    ]
    mount.restype = ctypes.c_int
    options = f"size={tmpfs_bytes},nr_inodes={tmpfs_inodes},mode=700".encode("ascii")
    result = mount(
        b"tmpfs",
        os.fsencode(path),
        b"tmpfs",
        ctypes.c_ulong(2 | 4),  # MS_NOSUID | MS_NODEV
        options,
    )
    if result != 0:
        raise OSError(ctypes.get_errno(), "could not mount evaluator tmpfs")


def _detach_tmpfs(path):
    libc = ctypes.CDLL(None, use_errno=True)
    unmount = libc.umount2
    unmount.argtypes = [ctypes.c_char_p, ctypes.c_int]
    unmount.restype = ctypes.c_int
    # The outer mount namespace owns this private /var/tmp mount.  A parent-held root FD pins the
    # terminal tree for validation/Step 27 while lazy detach releases the namespace use.
    if unmount(os.fsencode(path), 2) != 0:  # MNT_DETACH
        raise OSError(ctypes.get_errno(), "could not detach evaluator tmpfs")


cgroup_fd = -1
cgroup_kill_fd = -1
scratch_fd = -1
scratch_mounted = False
memory_events_before = {}
pids_events_before = {}
cpu_usage_before = 0
if guard_enabled:
    try:
        if (
            control_fd < 0
            or memory_limit <= 0
            or process_limit <= 0
            or cpu_percent <= 0
            or tmpfs_bytes <= 0
            or tmpfs_inodes <= 0
            or not expected_cgroup_relative
        ):
            raise OSError(errno.EINVAL, "invalid resource-guard arguments")
        cgroup_fd = _cgroup_directory(expected_cgroup_relative)
        _verify_controls(cgroup_fd)
        memory_events_before = _counter_map(cgroup_fd, "memory.events")
        pids_events_before = _counter_map(cgroup_fd, "pids.events")
        _required_counter(memory_events_before, "memory.events", "max")
        _required_counter(memory_events_before, "memory.events", "oom_kill")
        _required_counter(pids_events_before, "pids.events", "max")
        cpu_usage_before = _required_counter(
            _counter_map(cgroup_fd, "cpu.stat"), "cpu.stat", "usage_usec"
        )
        _nonnegative_value(cgroup_fd, "memory.peak")
        _nonnegative_value(cgroup_fd, "pids.peak")
        cgroup_kill_fd = os.open(
            "cgroup.kill",
            os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=cgroup_fd,
        )
        _mount_tmpfs("/var/tmp")
        scratch_mounted = True
        scratch_fd = os.open(
            "/var/tmp",
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        control = socket.socket(fileno=control_fd)
        control_fd = -1
        control.sendmsg(
            [b"MT26R"],
            [
                (
                    socket.SOL_SOCKET,
                    socket.SCM_RIGHTS,
                    struct.pack("iii", cgroup_fd, scratch_fd, cgroup_kill_fd),
                )
            ],
        )
        release = control.recv(1)
        control.close()
        if release != b"R":
            raise OSError(errno.ECANCELED, "parent declined evaluator release")
    except BaseException as exc:
        setup_error = getattr(exc, "errno", None) or errno.EIO
        try:
            if scratch_mounted:
                _detach_tmpfs("/var/tmp")
        except OSError as cleanup_exc:
            setup_error = getattr(cleanup_exc, "errno", None) or errno.EIO
        if scratch_fd >= 0:
            os.close(scratch_fd)
        if cgroup_kill_fd >= 0:
            os.close(cgroup_kill_fd)
        if cgroup_fd >= 0:
            os.close(cgroup_fd)
        if control_fd >= 0:
            os.close(control_fd)
        _write_status(0, 0, 0, 0, 0, setup_error)
        os.close(status_fd)
        os._exit(125)

error_read, error_write = os.pipe2(os.O_CLOEXEC)
target_pid = os.fork()
if target_pid == 0:
    os.close(error_read)
    try:
        if file_size_limit >= 0:
            resource.setrlimit(resource.RLIMIT_FSIZE, (file_size_limit, file_size_limit))
        if open_files_limit >= 0:
            resource.setrlimit(resource.RLIMIT_NOFILE, (open_files_limit, open_files_limit))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if scratch_fd >= 0:
            os.set_inheritable(scratch_fd, True)
            target_argv = [
                str(scratch_fd) if value == "__measure_twice_evaluator_workspace_fd__" else value
                for value in target_argv
            ]
        os.execve(f"/proc/self/fd/{target_fd}", target_argv, target_environment)
    except (OSError, ValueError) as exc:
        os.write(error_write, struct.pack("!i", getattr(exc, "errno", None) or errno.EINVAL))
        os._exit(127)
os.close(error_write)
exec_error = os.read(error_read, 4)
os.close(error_read)
while True:
    try:
        waited_pid, wait_status, usage = os.wait4(target_pid, 0)
        break
    except InterruptedError:
        continue
if waited_pid != target_pid:
    wait_status = 0
    setup_error = errno.ECHILD
else:
    setup_error = 0
error_number = struct.unpack("!i", exec_error)[0] if len(exec_error) == 4 else 0
cpu_seconds = usage.ru_utime + usage.ru_stime
try:
    os.kill(-1, signal.SIGKILL)
except OSError:
    pass
while True:
    try:
        _, _, child_usage = os.wait4(-1, 0)
    except ChildProcessError:
        break
    cpu_seconds += child_usage.ru_utime + child_usage.ru_stime
hard_limit = 0
hard_observed = 0
cpu_microseconds = int(cpu_seconds * 1_000_000)
if guard_enabled:
    try:
        memory_events_after = _counter_map(cgroup_fd, "memory.events")
        pids_events_after = _counter_map(cgroup_fd, "pids.events")
        _required_counter(memory_events_after, "memory.events", "max")
        _required_counter(memory_events_after, "memory.events", "oom_kill")
        _required_counter(pids_events_after, "pids.events", "max")
        memory_delta = max(
            _required_counter(memory_events_after, "memory.events", "max")
            - _required_counter(memory_events_before, "memory.events", "max"),
            _required_counter(memory_events_after, "memory.events", "oom_kill")
            - _required_counter(memory_events_before, "memory.events", "oom_kill"),
        )
        pids_delta = (
            _required_counter(pids_events_after, "pids.events", "max")
            - _required_counter(pids_events_before, "pids.events", "max")
        )
        if memory_delta > 0:
            hard_limit, hard_observed = 1, _nonnegative_value(cgroup_fd, "memory.peak")
        elif pids_delta > 0:
            hard_limit, hard_observed = 2, _nonnegative_value(cgroup_fd, "pids.peak")
        elif scratch_fd >= 0:
            scratch_usage = os.fstatvfs(scratch_fd)
            if scratch_usage.f_bavail <= 0:
                hard_limit = 3
                hard_observed = scratch_usage.f_blocks * scratch_usage.f_frsize
            elif scratch_usage.f_ffree <= 0:
                hard_limit = 4
                hard_observed = scratch_usage.f_files
        cpu_usage_after = _required_counter(
            _counter_map(cgroup_fd, "cpu.stat"), "cpu.stat", "usage_usec"
        )
        cpu_microseconds = max(0, cpu_usage_after - cpu_usage_before)
    except BaseException as exc:
        setup_error = getattr(exc, "errno", None) or errno.EIO
    if scratch_fd >= 0:
        os.close(scratch_fd)
    if cgroup_kill_fd >= 0:
        os.close(cgroup_kill_fd)
    if cgroup_fd >= 0:
        os.close(cgroup_fd)
_write_status(wait_status, error_number, cpu_microseconds, hard_limit, hard_observed, setup_error)
os.close(status_fd)
exit_code = os.waitstatus_to_exitcode(wait_status)
os._exit(exit_code if exit_code >= 0 else 128 - exit_code)
"""

# Substituted, not duplicated: the supervisor and the parent read the same wire shape from one
# owner, so widening or reordering a field cannot silently desync the two ends of the pipe.
_SUPERVISOR_CODE: Final[str] = _SUPERVISOR_CODE_TEMPLATE.replace(
    "__MT_STATUS_FORMAT__", _SUPERVISOR_STATUS_FORMAT
).replace("__MT_STATUS_MAGIC__", _SUPERVISOR_MAGIC.decode("ascii"))

Termination = Literal["exited", "signalled", "timeout", "stream-limit", "resource-limit"]
ResourceLimitName = Literal["cpu", "memory", "processes", "file-count", "file-bytes"]
ResourceLimitProvenance = Literal["hard-guard", "sampled-threshold"]


class ProcessContractError(ValueError):
    """An immutable process request violates the harness contract."""


class ProcessExecutionError(RuntimeError):
    """The harness could not reliably launch or collect a requested process."""


class ModelTreeViolationError(RuntimeError):
    """A quiescent evaluator result tree violates submitted-output policy.

    Step 26 has no ProcessResult terminal category for non-regular tree content.  Preserve this
    typed distinction for the Step-27 scorer instead of reporting a model-created symlink or
    special file as harness infrastructure failure.
    """


def _positive_int(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ProcessContractError(f"{label} must be a positive integer, got {value!r}")
    return value


def _utf8(value: object, *, label: str, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        qualifier = "non-empty " if not allow_empty else ""
        raise ProcessContractError(f"{label} must be a {qualifier}string")
    if "\0" in value:
        raise ProcessContractError(f"{label} may not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProcessContractError(f"{label} must be UTF-8 encodable") from exc
    return value


@dataclass(frozen=True, slots=True)
class LinuxResourceGuard:
    """The mandatory cgroup envelope for one evaluator target.

    This is configuration, not an observed result: the supervisor opens the cgroup directory
    after systemd places it in the fresh scope and sends the held descriptor to the owner before
    it releases any target byte.
    """

    memory_bytes: int
    processes: int
    cpu_bandwidth_percent: int

    def __post_init__(self) -> None:
        _positive_int(self.memory_bytes, label="Linux resource guard memory_bytes")
        _positive_int(self.processes, label="Linux resource guard processes")
        _positive_int(
            self.cpu_bandwidth_percent,
            label="Linux resource guard cpu_bandwidth_percent",
        )
        if self.cpu_bandwidth_percent > 100:
            raise ProcessContractError(
                "Linux resource guard cpu_bandwidth_percent may not exceed 100"
            )


@dataclass(slots=True)
class EvaluatorScratch:
    """A one-shot private tmpfs configuration plus its retained terminal root FD."""

    source: LinuxPathCapability = field(repr=False)
    file_limit: int
    byte_limit: int
    tmpfs_bytes: int
    tmpfs_inodes: int
    _terminal_tree: LinuxPathCapability | None = field(default=None, init=False, repr=False)
    _physical_tmpfs_bytes: int | None = field(default=None, init=False, repr=False)
    _physical_tmpfs_inodes: int | None = field(default=None, init=False, repr=False)
    _seed_released: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        for name in ("file_limit", "byte_limit", "tmpfs_bytes", "tmpfs_inodes"):
            _positive_int(getattr(self, name), label=f"evaluator scratch {name}")
        required_bytes = evaluator_tmpfs_minimum_bytes(
            file_bytes=self.byte_limit,
            files=self.file_limit,
        )
        if self.tmpfs_bytes < required_bytes:
            raise ProcessContractError(
                "evaluator scratch tmpfs_bytes must cover logical bytes and per-file pages"
            )
        required_inodes = self.file_limit + EVALUATOR_DIRECTORY_ALLOWANCE
        if self.tmpfs_inodes < required_inodes:
            raise ProcessContractError(
                "evaluator scratch tmpfs_inodes must cover files and directory structure"
            )

    def source_capability(self) -> LinuxPathCapability:
        """Borrow the pinned applied-tree source until the process claims completion."""

        with self._lock:
            if self._closed or self._seed_released:
                raise ProcessExecutionError("evaluator scratch source capability is unavailable")
            return self.source

    def adopt_terminal_tree(self, capability: LinuxPathCapability) -> None:
        """Transfer one retained tmpfs root used by terminal Step-26 validation."""

        with self._lock:
            if self._closed or self._terminal_tree is not None:
                capability.close()
                raise ProcessExecutionError("evaluator scratch terminal capability is unavailable")
            self._terminal_tree = capability

    def record_backing_bounds(self, *, bytes_capacity: int, inode_capacity: int) -> None:
        """Remember the supervisor-proved physical tmpfs ceilings for terminal evidence."""

        if bytes_capacity <= 0 or inode_capacity <= 0:
            raise ProcessExecutionError("evaluator tmpfs reported invalid physical bounds")
        with self._lock:
            if self._closed:
                raise ProcessExecutionError("evaluator scratch is closed")
            self._physical_tmpfs_bytes = bytes_capacity
            self._physical_tmpfs_inodes = inode_capacity

    def physical_limit(self, name: ResourceLimitName) -> int:
        with self._lock:
            if name == "file-bytes":
                value = self._physical_tmpfs_bytes
            elif name == "file-count":
                value = self._physical_tmpfs_inodes
            else:
                raise ProcessExecutionError("evaluator scratch has no requested physical limit")
            if value is None:
                raise ProcessExecutionError("evaluator tmpfs physical bounds are unavailable")
            return value

    def terminal_tree_capability(self) -> LinuxPathCapability:
        with self._lock:
            if self._closed or self._terminal_tree is None:
                raise ProcessExecutionError("evaluator terminal tree capability is unavailable")
            return self._terminal_tree

    def release_source(self) -> None:
        with self._lock:
            if self._seed_released:
                return
            self._seed_released = True
        self.source.close()

    def close(self) -> None:
        terminal: LinuxPathCapability | None
        with self._lock:
            if self._closed:
                return
            self._closed = True
            terminal = self._terminal_tree
            self._terminal_tree = None
            seed_released = self._seed_released
            self._seed_released = True
        if not seed_released:
            self.source.close()
        if terminal is not None:
            terminal.close()


def _validate_environment(
    environment: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    if not isinstance(environment, tuple):
        raise ProcessContractError("process environment must be an immutable tuple of pairs")
    clean: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(environment):
        if not isinstance(item, tuple) or len(item) != 2:
            raise ProcessContractError(f"process environment[{index}] must be a (name, value) pair")
        name = _utf8(item[0], label=f"process environment[{index}] name", allow_empty=False)
        value = _utf8(item[1], label=f"process environment[{index}] value")
        if "=" in name:
            raise ProcessContractError(f"process environment name {name!r} may not contain '='")
        folded = name.casefold()
        if folded in seen:
            raise ProcessContractError(f"duplicate process environment name {name!r}")
        seen.add(folded)
        clean.append((name, value))
    return tuple(clean)


@dataclass(frozen=True, slots=True)
class ProcessResourceLimits:
    """Sampled Linux scoring thresholds plus inherited per-process safety limits.

    Evaluator CPU seconds and logical writable-tree limits are sampled thresholds. A paired
    LinuxResourceGuard makes memory and task ceilings cgroup hard guards; the legacy unguarded
    process seam retains diagnostic sampling for direct non-evaluator callers. ``file_bytes``
    additionally sets ``RLIMIT_FSIZE`` and ``open_files`` sets ``RLIMIT_NOFILE``.
    """

    cpu_seconds: int | None = None
    memory_bytes: int | None = None
    processes: int | None = None
    file_bytes: int | None = None
    open_files: int | None = None
    tree_files: int | None = None
    tree_bytes: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "cpu_seconds",
            "memory_bytes",
            "processes",
            "file_bytes",
            "open_files",
            "tree_files",
            "tree_bytes",
        ):
            value = getattr(self, name)
            if value is not None:
                _positive_int(value, label=f"process resource limit {name}")
        tree_values = (self.tree_files, self.tree_bytes)
        if any(value is not None for value in tree_values):
            if not all(value is not None for value in tree_values):
                raise ProcessContractError("tree_files and tree_bytes must be supplied together")


@dataclass(slots=True)
class _RequestOwnership:
    lock: threading.Lock = field(default_factory=threading.Lock)
    consumed: bool = False
    closed: bool = False


@dataclass(frozen=True, slots=True)
class ProcessRequest:
    """One exact subprocess invocation.

    ``create`` is the ergonomic boundary for ordinary mappings/sequences.  The stored form uses
    only immutable tuples, so a caller cannot mutate argv or environment after validation.
    """

    argv: tuple[str, ...]
    stdin: str = field(repr=False)
    cwd: Path
    environment: tuple[tuple[str, str], ...] = field(repr=False)
    timeout_s: float
    stream_limit_bytes: int
    secret_values: tuple[str, ...] = field(default=(), repr=False)
    resource_limits: ProcessResourceLimits | None = None
    resource_guard: LinuxResourceGuard | None = field(default=None, repr=False, compare=False)
    _cwd_capability: LinuxPathCapability | None = field(default=None, repr=False, compare=False)
    _executable_capability: LinuxPathCapability | None = field(
        default=None, repr=False, compare=False
    )
    _namespace_capability: LinuxPathCapability | None = field(
        default=None, repr=False, compare=False
    )
    _supervisor_capability: LinuxPathCapability | None = field(
        default=None, repr=False, compare=False
    )
    _systemd_run_capability: LinuxPathCapability | None = field(
        default=None, repr=False, compare=False
    )
    _inherited_capabilities: tuple[LinuxPathCapability, ...] = field(
        default=(), repr=False, compare=False
    )
    _tree_capability: LinuxPathCapability | None = field(default=None, repr=False, compare=False)
    _tree_before_open: Callable[[str], None] | None = field(default=None, repr=False, compare=False)
    _evaluator_scratch: EvaluatorScratch | None = field(default=None, repr=False, compare=False)
    _ownership: _RequestOwnership = field(
        default_factory=_RequestOwnership, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.argv, tuple) or not self.argv:
            raise ProcessContractError("process argv must be a non-empty immutable tuple")
        for index, argument in enumerate(self.argv):
            _utf8(argument, label=f"process argv[{index}]", allow_empty=index != 0)
        executable_is_absolute = (
            PurePosixPath(self.argv[0]).is_absolute()
            if self._executable_capability is not None
            else Path(self.argv[0]).is_absolute()
        )
        if not executable_is_absolute:
            raise ProcessContractError("process argv[0] must be an absolute executable path")
        _utf8(self.stdin, label="process stdin")
        cwd_is_absolute = isinstance(self.cwd, Path) and (
            self.cwd.is_absolute()
            or (
                self._cwd_capability is not None
                and PurePosixPath(self.cwd.as_posix()).is_absolute()
            )
        )
        if not cwd_is_absolute:
            raise ProcessContractError("process cwd must be an absolute pathlib.Path")
        _validate_environment(self.environment)
        if (
            not isinstance(self.timeout_s, (int, float))
            or isinstance(self.timeout_s, bool)
            or not math.isfinite(self.timeout_s)
            or self.timeout_s <= 0
        ):
            raise ProcessContractError(
                f"process timeout_s must be finite and positive, got {self.timeout_s!r}"
            )
        _positive_int(self.stream_limit_bytes, label="process stream_limit_bytes")
        if not isinstance(self.secret_values, tuple):
            raise ProcessContractError("process secret_values must be an immutable tuple")
        seen_secrets: set[bytes] = set()
        for index, secret in enumerate(self.secret_values):
            clean = _utf8(secret, label=f"process secret_values[{index}]", allow_empty=False)
            encoded = clean.encode("utf-8")
            if len(encoded) > _MAX_SECRET_BYTES:
                raise ProcessContractError(
                    f"process secret_values[{index}] exceeds {_MAX_SECRET_BYTES} UTF-8 bytes"
                )
            if encoded in seen_secrets:
                raise ProcessContractError("process secret_values contains a duplicate")
            seen_secrets.add(encoded)
        if self.resource_limits is not None and not isinstance(
            self.resource_limits, ProcessResourceLimits
        ):
            raise ProcessContractError("process resource_limits must be ProcessResourceLimits")
        if self.resource_guard is not None and not isinstance(
            self.resource_guard, LinuxResourceGuard
        ):
            raise ProcessContractError("process resource_guard must be LinuxResourceGuard")
        if (self.resource_guard is None) != (self._evaluator_scratch is None):
            raise ProcessContractError(
                "Linux evaluator requests require both a resource guard and evaluator scratch"
            )
        if sys.platform == "linux":
            if (
                self._cwd_capability is None
                or self._executable_capability is None
                or self._namespace_capability is None
                or self._supervisor_capability is None
            ):
                raise ProcessContractError(
                    "Linux process requests require pinned cwd, target, and supervisor capabilities"
                )
            if self.resource_guard is not None and self._systemd_run_capability is None:
                raise ProcessContractError(
                    "Linux resource-guard requests require a pinned systemd-run capability"
                )
            if self.resource_limits is not None and self.resource_limits.tree_files is not None:
                if self._tree_capability is None and self._evaluator_scratch is None:
                    raise ProcessContractError(
                        "Linux tree resource limits require a pinned tree capability"
                    )
        if not isinstance(self._inherited_capabilities, tuple):
            raise ProcessContractError("inherited capabilities must be an immutable tuple")

    @classmethod
    def create(
        cls,
        *,
        argv: Sequence[str],
        stdin: str,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_s: float,
        stream_limit_bytes: int,
        secret_values: Sequence[str] = (),
        resource_limits: ProcessResourceLimits | None = None,
        tree_root: Path | None = None,
        _race_hook: Callable[[str], None] | None = None,
        _tree_before_open: Callable[[str], None] | None = None,
    ) -> Self:
        """Copy mutable caller inputs into the immutable request representation."""

        stored_argv = tuple(argv)
        stored_environment = tuple(environment.items())
        stored_secrets = tuple(secret_values)
        if sys.platform != "linux":
            return cls(
                argv=stored_argv,
                stdin=stdin,
                cwd=cwd,
                environment=stored_environment,
                timeout_s=timeout_s,
                stream_limit_bytes=stream_limit_bytes,
                secret_values=stored_secrets,
                resource_limits=resource_limits,
            )
        if not stored_argv:
            raise ProcessContractError("process argv must be a non-empty immutable tuple")
        if not isinstance(cwd, Path) or not cwd.is_absolute():
            raise ProcessContractError("process cwd must be an absolute pathlib.Path")
        capabilities: list[LinuxPathCapability] = []
        try:
            cwd_capability = LinuxPathCapability.acquire_absolute(
                cwd,
                expected="directory",
            )
            capabilities.append(cwd_capability)
            if _race_hook is not None:
                _race_hook("process-cwd")
            executable_capability = LinuxPathCapability.acquire_absolute(
                stored_argv[0],
                expected="regular",
                allow_symlinks=True,
                executable=True,
            )
            capabilities.append(executable_capability)
            namespace_capability = LinuxPathCapability.acquire_absolute(
                _LINUX_NAMESPACE_EXECUTABLE,
                expected="regular",
                allow_symlinks=True,
                executable=True,
            )
            capabilities.append(namespace_capability)
            supervisor_capability = LinuxPathCapability.acquire_absolute(
                _LINUX_SUPERVISOR_EXECUTABLE,
                expected="regular",
                allow_symlinks=True,
                executable=True,
            )
            capabilities.append(supervisor_capability)
            tree_capability = None
            if tree_root is not None:
                tree_capability = LinuxPathCapability.acquire_absolute(
                    tree_root,
                    expected="directory",
                )
                capabilities.append(tree_capability)
            request = cls(
                argv=stored_argv,
                stdin=stdin,
                cwd=cwd,
                environment=stored_environment,
                timeout_s=timeout_s,
                stream_limit_bytes=stream_limit_bytes,
                secret_values=stored_secrets,
                resource_limits=resource_limits,
                _cwd_capability=cwd_capability,
                _executable_capability=executable_capability,
                _namespace_capability=namespace_capability,
                _supervisor_capability=supervisor_capability,
                _tree_capability=tree_capability,
                _tree_before_open=_tree_before_open,
            )
        except BaseException:
            for capability in capabilities:
                capability.close()
            raise
        return request

    @classmethod
    def _from_owned_capabilities(
        cls,
        *,
        argv: tuple[str, ...],
        stdin: str,
        cwd: Path,
        environment: tuple[tuple[str, str], ...],
        timeout_s: float,
        stream_limit_bytes: int,
        secret_values: tuple[str, ...],
        resource_limits: ProcessResourceLimits | None,
        resource_guard: LinuxResourceGuard | None = None,
        evaluator_scratch: EvaluatorScratch | None = None,
        cwd_capability: LinuxPathCapability,
        executable_capability: LinuxPathCapability,
        inherited_capabilities: tuple[LinuxPathCapability, ...],
        tree_capability: LinuxPathCapability | None,
        tree_before_open: Callable[[str], None] | None = None,
    ) -> Self:
        """Internal transfer boundary used by ``SandboxLaunch``."""

        owned = (
            cwd_capability,
            executable_capability,
            *inherited_capabilities,
            *((tree_capability,) if tree_capability is not None else ()),
        )
        namespace_capability: LinuxPathCapability | None = None
        supervisor_capability: LinuxPathCapability | None = None
        systemd_run_capability: LinuxPathCapability | None = None
        try:
            if sys.platform == "linux":
                namespace_capability = LinuxPathCapability.acquire_absolute(
                    _LINUX_NAMESPACE_EXECUTABLE,
                    expected="regular",
                    allow_symlinks=True,
                    executable=True,
                )
                supervisor_capability = LinuxPathCapability.acquire_absolute(
                    _LINUX_SUPERVISOR_EXECUTABLE,
                    expected="regular",
                    allow_symlinks=True,
                    executable=True,
                )
                if resource_guard is not None:
                    systemd_run_capability = LinuxPathCapability.acquire_absolute(
                        _LINUX_SYSTEMD_RUN_EXECUTABLE,
                        expected="regular",
                        allow_symlinks=True,
                        executable=True,
                    )
            return cls(
                argv=argv,
                stdin=stdin,
                cwd=cwd,
                environment=environment,
                timeout_s=timeout_s,
                stream_limit_bytes=stream_limit_bytes,
                secret_values=secret_values,
                resource_limits=resource_limits,
                resource_guard=resource_guard,
                _cwd_capability=cwd_capability,
                _executable_capability=executable_capability,
                _namespace_capability=namespace_capability,
                _supervisor_capability=supervisor_capability,
                _systemd_run_capability=systemd_run_capability,
                _inherited_capabilities=inherited_capabilities,
                _tree_capability=tree_capability,
                _tree_before_open=tree_before_open,
                _evaluator_scratch=evaluator_scratch,
            )
        except BaseException:
            if namespace_capability is not None:
                namespace_capability.close()
            if supervisor_capability is not None:
                supervisor_capability.close()
            if systemd_run_capability is not None:
                systemd_run_capability.close()
            for capability in owned:
                capability.close()
            if evaluator_scratch is not None:
                evaluator_scratch.release_source()
            raise

    def _claim(self) -> None:
        with self._ownership.lock:
            if self._ownership.consumed or self._ownership.closed:
                raise ProcessContractError("process request is one-shot and already consumed")
            self._ownership.consumed = True

    def _all_capabilities(self) -> tuple[LinuxPathCapability, ...]:
        return (
            *((self._cwd_capability,) if self._cwd_capability is not None else ()),
            *((self._executable_capability,) if self._executable_capability is not None else ()),
            *((self._namespace_capability,) if self._namespace_capability is not None else ()),
            *((self._supervisor_capability,) if self._supervisor_capability is not None else ()),
            *((self._systemd_run_capability,) if self._systemd_run_capability is not None else ()),
            *self._inherited_capabilities,
            *((self._tree_capability,) if self._tree_capability is not None else ()),
        )

    def _release(self) -> None:
        for capability in self._all_capabilities():
            capability.close()
        if self._evaluator_scratch is not None:
            self._evaluator_scratch.release_source()
        with self._ownership.lock:
            self._ownership.closed = True

    def close(self) -> None:
        """Close an unconsumed request or idempotently finish a consumed request's ownership."""

        with self._ownership.lock:
            if self._ownership.closed:
                return
            if self._ownership.consumed:
                return
            self._ownership.closed = True
        for capability in self._all_capabilities():
            capability.close()
        if self._evaluator_scratch is not None:
            self._evaluator_scratch.release_source()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def stdin_bytes(self) -> bytes:
        """Return the exact bytes written to the child's stdin pipe."""

        return self.stdin.encode("utf-8")


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Bounded raw process evidence plus an explicit terminal reason."""

    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)
    exit_code: int | None
    signal: int | None
    elapsed_ms: int
    termination: Termination
    stdout_limit_exceeded: bool
    stderr_limit_exceeded: bool
    stdout_secret_detected: bool
    stderr_secret_detected: bool
    resource_limit: ResourceLimitName | None = None
    resource_limit_value: int | None = None
    resource_limit_observed: int | None = None
    resource_limit_provenance: ResourceLimitProvenance | None = None
    tree_sample_inconclusive_count: int = 0

    @property
    def timed_out(self) -> bool:
        return self.termination == "timeout"

    @property
    def stream_limit_exceeded(self) -> bool:
        return self.termination == "stream-limit"


@dataclass(slots=True)
class _BoundedCapture:
    limit: int
    reserve: int
    secrets: tuple[bytes, ...]
    data: bytearray
    total: int = 0
    exceeded: bool = False
    secret_detected: bool = False
    secret_tail: bytes = b""
    error: OSError | None = None

    def add(self, chunk: bytes) -> None:
        scan = self.secret_tail + chunk
        if any(secret in scan for secret in self.secrets):
            self.secret_detected = True
        if self.reserve > 1:
            self.secret_tail = scan[-(self.reserve - 1) :]
        self.total += len(chunk)
        keep = self.limit + self.reserve - len(self.data)
        if keep > 0:
            self.data.extend(chunk[:keep])
        if self.total > self.limit:
            self.exceeded = True

    def rendered(self) -> tuple[bytes, bool]:
        value = bytes(self.data)
        for secret in sorted(self.secrets, key=len, reverse=True):
            value = value.replace(secret, _REDACTION)
        return value[: self.limit], self.secret_detected


def _read_pipe(
    stream: IO[bytes],
    capture: _BoundedCapture,
    limit_reached: threading.Event,
    done: threading.Event,
) -> None:
    try:
        while True:
            chunk = stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            capture.add(chunk)
            if capture.exceeded:
                limit_reached.set()
                break
    except OSError as exc:
        capture.error = exc
    finally:
        done.set()


def _write_stdin(stream: IO[bytes], value: bytes) -> None:
    try:
        offset = 0
        while offset < len(value):
            written = stream.write(value[offset:])
            if written is None or written <= 0:
                raise BrokenPipeError("stdin pipe stopped accepting bytes")
            offset += written
        stream.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _posix_direct_children(parent_pid: int) -> set[int]:
    """Snapshot one process's direct children across all of its threads."""

    if sys.platform != "linux":
        return set()
    children: set[int] = set()
    try:
        child_files = tuple(Path(f"/proc/{parent_pid}/task").glob("*/children"))
    except OSError:
        return children
    for children_path in child_files:
        try:
            raw_children = children_path.read_text(encoding="ascii").split()
        except (FileNotFoundError, OSError, UnicodeError):
            continue
        for raw_pid in raw_children:
            try:
                children.add(int(raw_pid))
            except ValueError:
                continue
    return children


def _pid_stat_fields(pid: int) -> tuple[str, ...] | None:
    """Read the post-``comm`` fields in one Linux ``/proc/<pid>/stat`` record."""

    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    closing = raw.rfind(")")
    if closing < 0:
        return None
    return tuple(raw[closing + 2 :].split())


def _pid_starttime(pid: int) -> int | None:
    """Return Linux's immutable-per-process start token, guarding against numeric PID reuse."""

    fields = _pid_stat_fields(pid)
    if fields is None:
        return None
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def _pid_namespace_chain(pid: int) -> tuple[int, ...] | None:
    """Return host-visible PID-namespace IDs, preserving the process identity boundary."""

    try:
        lines = Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines()
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    for line in lines:
        if not line.startswith("NSpid:"):
            continue
        try:
            values = tuple(int(value) for value in line.split()[1:])
        except ValueError:
            return None
        if not values or any(value <= 0 for value in values):
            return None
        return values
    return None


def _pid_unified_cgroup(pid: int) -> str | None:
    """Read a host process's one unified cgroup-v2 membership record."""

    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii").splitlines()
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    matches = [line.removeprefix("0::") for line in lines if line.startswith("0::")]
    if len(matches) != 1 or not matches[0].startswith("/"):
        return None
    return matches[0]


def _pid_process_group(pid: int) -> int | None:
    fields = _pid_stat_fields(pid)
    if fields is None:
        return None
    try:
        return int(fields[2])
    except (IndexError, ValueError):
        return None


def _pid_cpu_ticks(pid: int) -> int | None:
    fields = _pid_stat_fields(pid)
    if fields is None:
        return None
    try:
        return sum(int(fields[index]) for index in (11, 12, 13, 14))
    except (IndexError, ValueError):
        return None


def _pid_resident_bytes(pid: int) -> int | None:
    try:
        fields = Path(f"/proc/{pid}/statm").read_text(encoding="ascii").split()
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    try:
        pages = int(fields[1])
        page_size = int(cast("Any", os).sysconf("SC_PAGE_SIZE"))
    except (IndexError, TypeError, ValueError, OSError):
        return None
    return pages * page_size


@dataclass(slots=True)
class _LinuxDescendantTracker:
    root_pid: int
    root_starttime: int
    resource_limits: ProcessResourceLimits | None = None
    tree_capability: LinuxPathCapability | None = None
    tree_before_open: Callable[[str], None] | None = None
    resource_guard: _LinuxResourceGuardState | None = None
    evaluator_scratch: EvaluatorScratch | None = None
    identities: dict[int, int] = field(default_factory=dict)
    cpu_high_water_ticks: int = 0
    resource_limit: ResourceLimitName | None = None
    resource_limit_value: int | None = None
    resource_limit_observed: int | None = None
    resource_limit_provenance: ResourceLimitProvenance | None = None
    tree_sample_inconclusive_count: int = 0
    monitor_error: BaseException | None = None
    next_tree_scan: float = 0.0
    stop: threading.Event = field(default_factory=threading.Event)
    limit_reached: threading.Event = field(default_factory=threading.Event)
    monitor_failed: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    # A monitor may be inside an FD-relative cgroup read while teardown starts.  This lock
    # makes detaching that borrowed guard atomic with every such read, so cleanup never closes
    # a descriptor the monitor can still use.
    guard_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    uses_resource_guard: bool = field(init=False)

    def __post_init__(self) -> None:
        self.uses_resource_guard = self.resource_guard is not None

    def detach_resource_guard(self) -> None:
        """Make future monitor passes avoid the runtime-owned cgroup descriptors."""

        with self.guard_lock:
            self.resource_guard = None

    def _guard_observations(
        self,
        limits: ProcessResourceLimits,
    ) -> tuple[tuple[ResourceLimitName, int] | None, tuple[int, bool] | None]:
        """Read guard attribution/accounting while teardown exclusion is held."""

        with self.guard_lock:
            guard = self.resource_guard
            if guard is None:
                return None, None
            if guard.collected:
                return None, None
            try:
                hard_event = guard.hard_event()
            except ProcessExecutionError as exc:
                if guard.control_missing_after_collection(exc):
                    return None, None
                raise
            if hard_event is not None:
                return hard_event, None
            if limits.cpu_seconds is None:
                return None, None
            try:
                cpu_microseconds = guard.cpu_usage_microseconds()
            except ProcessExecutionError as exc:
                if guard.control_missing_after_collection(exc):
                    return None, None
                raise
            return (
                None,
                (
                    cpu_microseconds,
                    cpu_microseconds >= limits.cpu_seconds * 1_000_000,
                ),
            )

    def _configured_limit(self, name: ResourceLimitName) -> int | None:
        limits = self.resource_limits
        if limits is None:
            return None
        return {
            "cpu": limits.cpu_seconds,
            "memory": limits.memory_bytes,
            "processes": limits.processes,
            "file-count": limits.tree_files,
            "file-bytes": limits.tree_bytes,
        }[name]

    def _set_resource_limit(
        self,
        name: ResourceLimitName,
        *,
        provenance: ResourceLimitProvenance = "sampled-threshold",
        observed: int | None = None,
        replace: bool = False,
        limit_value: int | None = None,
    ) -> None:
        with self.lock:
            if self.resource_limit is None or replace:
                self.resource_limit = name
                self.resource_limit_value = (
                    self._configured_limit(name) if limit_value is None else limit_value
                )
                self.resource_limit_observed = observed
                self.resource_limit_provenance = provenance
                self.limit_reached.set()

    def _record_tree_sample_inconclusive(self) -> None:
        """Retain audit evidence that a live writable-tree poll observed concurrent churn."""

        with self.lock:
            self.tree_sample_inconclusive_count += 1

    def _check_resources(self, candidates: Mapping[int, int], *, force_tree: bool) -> None:
        limits = self.resource_limits
        if limits is None or self.limit_reached.is_set():
            return
        owned = dict(candidates)
        if _pid_starttime(self.root_pid) == self.root_starttime:
            owned[self.root_pid] = self.root_starttime
        live = {pid: token for pid, token in owned.items() if _pid_starttime(pid) == token}
        hard_event: tuple[ResourceLimitName, int] | None = None
        guard_cpu: tuple[int, bool] | None = None
        if self.uses_resource_guard:
            # This observes an already-enforced cgroup event for result attribution; it is never
            # the host safety boundary. Once observed, terminate promptly rather than converting
            # an ENOMEM/EAGAIN-catching target into a wall-timeout result.
            hard_event, guard_cpu = self._guard_observations(limits)
            if hard_event is not None:
                name, observed = hard_event
                self._set_resource_limit(
                    name,
                    provenance="hard-guard",
                    observed=observed,
                    replace=True,
                )
                return
        if self.evaluator_scratch is not None and self.tree_capability is not None:
            exhausted = _tmpfs_hard_exhaustion(self.tree_capability)
            if exhausted is not None:
                name, observed = exhausted
                self._set_resource_limit(
                    name,
                    provenance="hard-guard",
                    observed=observed,
                    limit_value=self.evaluator_scratch.physical_limit(name),
                    replace=True,
                )
                return
        if (
            not self.uses_resource_guard
            and limits.processes is not None
            and len(live) > limits.processes
        ):
            self._set_resource_limit("processes", observed=len(live))
            return
        if limits.cpu_seconds is not None:
            if self.uses_resource_guard:
                if guard_cpu is None:
                    # Teardown detached the guard after the monitor was stopped.  Final target
                    # status still supplies CPU accounting; never fall back to unrelated /proc
                    # sampling for a guarded evaluator.
                    exceeded = False
                else:
                    cpu_microseconds, exceeded = guard_cpu
                    observed_cpu_seconds = cpu_microseconds // 1_000_000
            else:
                live_ticks = sum(
                    ticks for pid in live if (ticks := _pid_cpu_ticks(pid)) is not None
                )
                self.cpu_high_water_ticks = max(self.cpu_high_water_ticks, live_ticks)
                ticks_per_second = int(cast("Any", os).sysconf("SC_CLK_TCK"))
                observed_cpu_seconds = self.cpu_high_water_ticks // ticks_per_second
                exceeded = self.cpu_high_water_ticks >= limits.cpu_seconds * ticks_per_second
            if exceeded:
                self._set_resource_limit("cpu", observed=observed_cpu_seconds)
                return
        if not self.uses_resource_guard and limits.memory_bytes is not None:
            resident = sum(value for pid in live if (value := _pid_resident_bytes(pid)) is not None)
            if resident > limits.memory_bytes:
                self._set_resource_limit("memory", observed=resident)
                return
        now = time.monotonic()
        if (
            self.tree_capability is not None
            and limits.tree_files is not None
            and limits.tree_bytes is not None
            and (force_tree or now >= self.next_tree_scan)
        ):
            self.next_tree_scan = now + _TREE_POLL_INTERVAL_S
            try:
                walk_tree(
                    self.tree_capability,
                    file_limit=limits.tree_files,
                    byte_limit=limits.tree_bytes,
                    before_open=self.tree_before_open,
                )
            except LinuxTreeLimitError as exc:
                observed = exc.file_count if exc.limit_name == "file-count" else exc.size_bytes
                self._set_resource_limit(exc.limit_name, observed=observed)
            except (LinuxTreeDriftError, LinuxTreePolicyError):
                # A live evaluator can mutate its own tmpfs while the monitor walks it.  This
                # poll contributes no usage evidence; strict policy/identity validation happens
                # only after cleanup has made the same retained FD quiescent.
                self._record_tree_sample_inconclusive()
            except LinuxCapabilityError as exc:
                raise ProcessExecutionError("evaluator tree inspection failed closed") from exc

    def validate_terminal_tree(self) -> None:
        """Strictly score one retained evaluator tree only after process-tree quiescence."""

        limits = self.resource_limits
        capability = self.tree_capability
        if (
            limits is None
            or capability is None
            or limits.tree_files is None
            or limits.tree_bytes is None
        ):
            return
        if self.evaluator_scratch is not None:
            exhausted = _tmpfs_hard_exhaustion(capability)
            if exhausted is not None:
                name, observed = exhausted
                self._set_resource_limit(
                    name,
                    provenance="hard-guard",
                    observed=observed,
                    limit_value=self.evaluator_scratch.physical_limit(name),
                    replace=True,
                )
        try:
            walk_tree(
                capability,
                file_limit=limits.tree_files,
                byte_limit=limits.tree_bytes,
                before_open=self.tree_before_open,
            )
        except LinuxTreeLimitError as exc:
            observed = exc.file_count if exc.limit_name == "file-count" else exc.size_bytes
            self._set_resource_limit(exc.limit_name, observed=observed)
        except LinuxTreePolicyError as exc:
            raise ModelTreeViolationError(
                "evaluator terminal tree violates submitted-output policy"
            ) from exc
        except LinuxTreeDriftError as exc:
            raise ProcessExecutionError(
                "evaluator terminal tree changed during post-cleanup validation"
            ) from exc
        except LinuxCapabilityError as exc:
            raise ProcessExecutionError("evaluator terminal tree inspection failed closed") from exc

    def final_guard_observation(self) -> None:
        """Sample hard cgroup evidence once more before cleanup can kill the scope.

        This intentionally bypasses ``limit_reached``: a sampled logical-tree result must not
        hide a kernel memory/pids event that arrived between monitor ticks.  Hard attribution uses
        ``replace=True`` and therefore remains authoritative when both observations exist.
        """

        limits = self.resource_limits
        if limits is None or not self.uses_resource_guard:
            return
        hard_event, guard_cpu = self._guard_observations(limits)
        if hard_event is not None:
            name, observed = hard_event
            self._set_resource_limit(
                name,
                provenance="hard-guard",
                observed=observed,
                replace=True,
            )
            return
        if guard_cpu is None:
            return
        cpu_microseconds, exceeded = guard_cpu
        if exceeded:
            self._set_resource_limit(
                "cpu",
                observed=cpu_microseconds // 1_000_000,
            )

    def snapshot(self, *, force_tree: bool = False) -> dict[int, int]:
        with self.lock:
            candidates = dict(self.identities)
        if _pid_starttime(self.root_pid) == self.root_starttime:
            pending = [(self.root_pid, self.root_starttime), *candidates.items()]
            traversed: set[tuple[int, int]] = set()
            while pending:
                parent, parent_token = pending.pop()
                identity = (parent, parent_token)
                if identity in traversed or _pid_starttime(parent) != parent_token:
                    continue
                children = _posix_direct_children(parent)
                if _pid_starttime(parent) != parent_token:
                    continue
                traversed.add(identity)
                for child in children:
                    token = _pid_starttime(child)
                    if token is None:
                        continue
                    if candidates.get(child) != token:
                        candidates[child] = token
                        pending.append((child, token))
        candidates.pop(self.root_pid, None)
        with self.lock:
            self.identities.update(candidates)
            result = dict(self.identities)
        self._check_resources(result, force_tree=force_tree)
        return result

    def monitor(self) -> None:
        try:
            while not self.stop.is_set():
                self.snapshot()
                self.stop.wait(_POLL_INTERVAL_S)
        except BaseException as exc:
            with self.lock:
                self.monitor_error = exc
            self.monitor_failed.set()


def _kill_posix_tree(
    proc: subprocess.Popen[bytes],
    process_group_id: int,
    tracked: Mapping[int, int] | None = None,
) -> None:
    descendants = {} if tracked is None else dict(tracked)
    posix_os = cast("Any", os)
    sigkill = cast("int", cast("Any", signal).SIGKILL)
    group_owned = proc.poll() is None and _pid_process_group(proc.pid) == process_group_id
    if not group_owned:
        group_owned = any(
            _pid_starttime(pid) == token and _pid_process_group(pid) == process_group_id
            for pid, token in descendants.items()
        )
    if group_owned:
        try:
            posix_os.killpg(process_group_id, sigkill)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    for pid, token in descendants.items():
        if _pid_starttime(pid) != token:
            continue
        try:
            os.kill(pid, sigkill)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


class _WindowsJob:
    """Win32 kill-on-close job assigned before the suspended child is resumed."""

    def __init__(self, handle: int) -> None:
        self._handle = handle

    @classmethod
    def assign(cls, proc: subprocess.Popen[bytes]) -> _WindowsJob | None:
        if sys.platform != "win32":
            return None
        win = cast("Any", ctypes)
        kernel32 = win.WinDLL("kernel32", use_last_error=True)

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        create_job = kernel32.CreateJobObjectW
        create_job.restype = ctypes.c_void_p
        handle_value = create_job(None, None)
        if not handle_value:
            return None
        handle = int(handle_value)
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        set_info = kernel32.SetInformationJobObject
        assigned = kernel32.AssignProcessToJobObject
        process_handle = int(cast("Any", proc)._handle)
        if not set_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not assigned(
            handle, process_handle
        ):
            kernel32.CloseHandle(handle)
            return None
        ntdll = win.WinDLL("ntdll", use_last_error=True)
        resume = ntdll.NtResumeProcess
        resume.restype = ctypes.c_long
        if int(resume(process_handle)) < 0:
            kernel32.CloseHandle(handle)
            return None
        return cls(handle)

    def terminate(self) -> None:
        if self._handle:
            win = cast("Any", ctypes)
            kernel32 = win.WinDLL("kernel32", use_last_error=True)
            kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if self._handle:
            win = cast("Any", ctypes)
            kernel32 = win.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle(self._handle)
            self._handle = 0


def _taskkill(proc: subprocess.Popen[bytes]) -> None:
    executable = shutil.which("taskkill")
    if executable is not None:
        try:
            subprocess.run(  # noqa: S603 - resolved Windows system utility and literal flags.
                [executable, "/T", "/F", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=_TASKKILL_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def _kill_tree(
    proc: subprocess.Popen[bytes],
    process_group_id: int | None,
    job: _WindowsJob | None,
    tracked: Mapping[int, int] | None = None,
) -> None:
    if sys.platform == "win32":
        if job is not None:
            job.terminate()
        _taskkill(proc)
    elif process_group_id is not None:
        _kill_posix_tree(proc, process_group_id, tracked)
    elif proc.poll() is None:
        proc.kill()


def _limit_preexec(limits: ProcessResourceLimits) -> Callable[[], None]:
    import resource

    resource_api = cast("Any", resource)
    pairs = tuple(
        (cast("int", getattr(resource_api, resource_name)), value)
        for resource_name, value in (
            ("RLIMIT_FSIZE", limits.file_bytes),
            ("RLIMIT_NOFILE", limits.open_files),
        )
        if value is not None
    )

    def apply_limits() -> None:
        for identifier, value in pairs:
            resource_api.setrlimit(identifier, (value, value))
        resource_api.setrlimit(resource_api.RLIMIT_CORE, (0, 0))

    return apply_limits


def _close_pipe(stream: IO[bytes] | None) -> None:
    if stream is not None:
        try:
            stream.close()
        except OSError:
            pass


@dataclass(frozen=True, slots=True)
class _LinuxTargetStatus:
    wait_status: int
    exec_error: int
    cpu_microseconds: int
    hard_limit: int
    hard_observed: int
    setup_error: int


@dataclass(slots=True)
class _RunningProcess:
    proc: subprocess.Popen[bytes]
    # When the TARGET began executing, not when the harness began standing the sandbox up:
    # assigned after the release barrier on the evaluator seam and after the Win32 resume.
    started: float
    process_group_id: int | None
    status_read_fd: int | None
    # When the target stopped, captured before teardown so elapsed_ms excludes the reap chain.
    finished: float | None = None
    job: _WindowsJob | None = None
    tracker: _LinuxDescendantTracker | None = None
    tracker_thread: threading.Thread | None = None
    pipe_threads: list[threading.Thread] = field(default_factory=list)
    stdout_capture: _BoundedCapture | None = None
    stderr_capture: _BoundedCapture | None = None
    stream_limit_reached: threading.Event = field(default_factory=threading.Event)
    stdout_done: threading.Event = field(default_factory=threading.Event)
    stderr_done: threading.Event = field(default_factory=threading.Event)
    target_status: _LinuxTargetStatus | None = None
    resource_guard: _LinuxResourceGuardState | None = None
    scratch_tree: LinuxPathCapability | None = None


def _close_fd(fd: int | None) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _read_directory_text(capability: LinuxPathCapability, name: str) -> str:
    """Read one fixed cgroup control through its held directory descriptor."""

    try:
        posix_os = cast("Any", os)
        fd = posix_os.open(
            name,
            posix_os.O_RDONLY | posix_os.O_CLOEXEC | posix_os.O_NOFOLLOW,
            dir_fd=capability.fd,
        )
    except OSError as exc:
        raise ProcessExecutionError(f"could not read Linux resource guard {name}") from exc
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as exc:
        raise ProcessExecutionError(f"could not read Linux resource guard {name}") from exc
    finally:
        _close_fd(fd)
    try:
        return b"".join(chunks).decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProcessExecutionError(
            f"Linux resource guard {name} did not return ASCII control data"
        ) from exc


def _cgroup_counters(capability: LinuxPathCapability, name: str) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in _read_directory_text(capability, name).splitlines():
            key, raw_value = line.split(maxsplit=1)
            values[key] = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ProcessExecutionError(f"Linux resource guard {name} is malformed") from exc
    return values


def _required_cgroup_counter(
    values: Mapping[str, int],
    key: str,
    *,
    control: str,
) -> int:
    value = values.get(key)
    if value is None or value < 0:
        raise ProcessExecutionError(f"Linux resource guard {control} lacks nonnegative {key}")
    return value


def _scope_collection_path(scope_relative_path: str) -> str:
    """Validate the supervisor-proved cgroup v2 path used after FD release."""

    if not scope_relative_path.startswith("/") or any(
        component in {"", ".", ".."} for component in scope_relative_path.split("/")[1:]
    ):
        raise ProcessExecutionError("Linux resource guard scope path is invalid")
    return f"/sys/fs/cgroup{scope_relative_path}"


def _exact_scope_is_absent(
    scope_path: str,
    expected_identity: tuple[int, int],
) -> bool:
    """Return whether one verified scope disappeared, rejecting path replacement."""

    posix_os = cast("Any", os)
    try:
        fd = posix_os.open(
            scope_path,
            posix_os.O_RDONLY | posix_os.O_DIRECTORY | posix_os.O_CLOEXEC | posix_os.O_NOFOLLOW,
        )
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise ProcessExecutionError("could not inspect Linux resource guard collection") from exc
    try:
        metadata = os.fstat(fd)
    except OSError as exc:
        raise ProcessExecutionError(
            "could not inspect Linux resource guard collection identity"
        ) from exc
    finally:
        _close_fd(fd)
    if (metadata.st_dev, metadata.st_ino) != expected_identity:
        raise ProcessExecutionError(
            "Linux resource guard scope path was replaced before collection"
        )
    return False


def _wait_for_scope_collection(
    scope_path: str,
    expected_identity: tuple[int, int] | None,
) -> None:
    """After owner exit, prove the fresh scope path disappears without path fallback."""

    deadline = time.monotonic() + _REAP_TIMEOUT_S
    stable_absence_deadline: float | None = None
    while True:
        if expected_identity is not None:
            absent = _exact_scope_is_absent(scope_path, expected_identity)
        else:
            # No received identity crossed a failed handshake boundary.  The caller only uses
            # this after reaping its systemd-run owner, so a path object is unexpected but can
            # still be watched until it disappears.
            try:
                posix_os = cast("Any", os)
                fd = posix_os.open(
                    scope_path,
                    posix_os.O_RDONLY
                    | posix_os.O_DIRECTORY
                    | posix_os.O_CLOEXEC
                    | posix_os.O_NOFOLLOW,
                )
            except FileNotFoundError:
                absent = True
            except OSError as exc:
                raise ProcessExecutionError(
                    "could not inspect Linux resource guard collection"
                ) from exc
            else:
                _close_fd(fd)
                absent = False
        if absent:
            if expected_identity is not None:
                return
            # A failed handshake has no trusted received identity.  The systemd-run owner was
            # reaped first, but require a complete bounded absence interval so an asynchronously
            # appearing fresh scope cannot turn one point-in-time ENOENT into a false proof.
            if stable_absence_deadline is None:
                stable_absence_deadline = time.monotonic() + _REAP_TIMEOUT_S
            if time.monotonic() >= stable_absence_deadline:
                return
            time.sleep(_POLL_INTERVAL_S)
            continue
        stable_absence_deadline = None
        if time.monotonic() >= deadline:
            raise ProcessExecutionError("Linux resource guard scope was not collected")
        time.sleep(_POLL_INTERVAL_S)


def _nonnegative_cgroup_value(capability: LinuxPathCapability, name: str) -> int:
    try:
        value = int(_read_directory_text(capability, name).strip())
    except ValueError as exc:
        raise ProcessExecutionError(f"Linux resource guard {name} is malformed") from exc
    if value < 0:
        raise ProcessExecutionError(f"Linux resource guard {name} is negative")
    return value


def _is_collected_cgroup_error(cause: BaseException | None) -> bool:
    """Does this control-read failure mean the cgroup itself is gone?

    A retired cgroup v2 scope fails in two distinct shapes, and the caller must recognise both:
    ``open()`` raises ``ENOENT`` when the control file is already unlinked, but when the open
    wins the race the subsequent ``read()`` raises ``ENODEV`` on the now-removed cgroup.  Only
    those two errnos qualify -- every other cgroup read failure stays fail-closed, because the
    caller goes on to prove owner exit and exact-path absence before treating this as terminal.
    """

    if isinstance(cause, FileNotFoundError):
        return True
    return isinstance(cause, OSError) and cause.errno == errno.ENODEV


@dataclass(slots=True)
class _LinuxResourceGuardState:
    """Owner-side held cgroup controls received before target release."""

    capability: LinuxPathCapability = field(repr=False)
    kill_fd: int = field(repr=False)
    configuration: LinuxResourceGuard
    scope_path: str
    scope_identity: tuple[int, int]
    memory_events_before: dict[str, int]
    pids_events_before: dict[str, int]
    cpu_usage_before: int = 0
    outer_owner_identity: tuple[int, int] | None = None
    trusted_for_cleanup: bool = False
    validated_for_release: bool = False
    collected: bool = False

    @classmethod
    def adopt(
        cls,
        capability: LinuxPathCapability,
        kill_fd: int,
        configuration: LinuxResourceGuard,
        *,
        scope_relative_path: str,
    ) -> Self:
        try:
            os.set_inheritable(kill_fd, False)
            scope_path = _scope_collection_path(scope_relative_path)
        except BaseException:
            _close_fd(kill_fd)
            capability.close()
            raise
        return cls(
            capability,
            kill_fd,
            configuration,
            scope_path,
            capability.identity,
            {},
            {},
        )

    def validate_before_release(self) -> None:
        """Verify this is the named cgroup, then read every required control before release."""

        if self.capability.filesystem_magic != _CGROUP2_SUPER_MAGIC:
            raise ProcessExecutionError("Linux resource guard is not backed by cgroup v2")
        self._verify_expected_scope_identity()
        self._verify_kill_fd()
        # From this point a later control/counter mismatch may fail startup, but this held FD is
        # proven to address only the fresh generated scope and is safe for common cleanup.
        self.trusted_for_cleanup = True
        self._verify_readback()
        self.memory_events_before = _cgroup_counters(self.capability, "memory.events")
        self.pids_events_before = _cgroup_counters(self.capability, "pids.events")
        self._validate_attribution_counters(
            self.memory_events_before,
            self.pids_events_before,
        )
        self.cpu_usage_before = _required_cgroup_counter(
            _cgroup_counters(self.capability, "cpu.stat"),
            "usage_usec",
            control="cpu.stat",
        )
        _nonnegative_cgroup_value(self.capability, "memory.peak")
        _nonnegative_cgroup_value(self.capability, "pids.peak")
        self.outer_owner_identity = self._capture_outer_owner_identity()
        self.validated_for_release = True

    def _capture_outer_owner_identity(self) -> tuple[int, int]:
        """Validate every scope member and identify the one PID-namespace outer owner.

        The transient ``systemd-run`` client can briefly appear in ``cgroup.procs`` while it
        starts ``unshare --fork`` and then migrates away.  Its continued host lifetime cannot
        prove the scope remains live.  The host-visible ``NSpid`` chain identifies the unique
        cgroup member that is PID 1 in the new namespace; that member is the exact outer owner
        whose immutable start token defines a collected terminal state.
        """

        try:
            raw_members = _read_directory_text(self.capability, "cgroup.procs").splitlines()
            members = [int(value) for value in raw_members]
        except ValueError as exc:
            raise ProcessExecutionError("Linux resource guard cgroup.procs is malformed") from exc
        if not members or len(members) > self.configuration.processes:
            raise ProcessExecutionError("Linux resource guard cgroup.procs is out of bounds")
        namespace_owner: tuple[int, int] | None = None
        for pid in members:
            if pid <= 0:
                raise ProcessExecutionError("Linux resource guard cgroup.procs is malformed")
            starttime_before = _pid_starttime(pid)
            if starttime_before is None:
                raise ProcessExecutionError(
                    "Linux resource guard outer owner disappeared before release"
                )
            namespace_chain = _pid_namespace_chain(pid)
            unified_cgroup = _pid_unified_cgroup(pid)
            starttime_after = _pid_starttime(pid)
            if starttime_after is None or starttime_after != starttime_before:
                raise ProcessExecutionError(
                    "Linux resource guard outer owner identity changed before release"
                )
            starttime = starttime_after
            is_namespace_owner = (
                namespace_chain is not None
                and len(namespace_chain) >= 2
                and namespace_chain[0] == pid
                and namespace_chain[-1] == 1
            )
            if is_namespace_owner:
                expected_relative = self.scope_path.removeprefix("/sys/fs/cgroup")
                if unified_cgroup != expected_relative:
                    raise ProcessExecutionError(
                        "Linux resource guard outer owner cgroup membership drifted before release"
                    )
                if namespace_owner is not None:
                    raise ProcessExecutionError(
                        "Linux resource guard had multiple PID-namespace outer owners"
                    )
                namespace_owner = (pid, starttime)
        if namespace_owner is None:
            raise ProcessExecutionError(
                "Linux resource guard did not identify its PID-namespace outer owner"
            )
        return namespace_owner

    def outer_owner_exited(self) -> bool:
        identity = self.outer_owner_identity
        if identity is None:
            raise ProcessExecutionError("Linux resource guard outer owner was not captured")
        pid, starttime = identity
        return _pid_starttime(pid) != starttime

    def _verify_expected_scope_identity(self) -> None:
        try:
            expected = LinuxPathCapability.acquire_absolute(
                self.scope_path,
                expected="directory",
            )
        except LinuxCapabilityError as exc:
            raise ProcessExecutionError(
                "could not open the expected Linux resource guard scope"
            ) from exc
        try:
            if expected.filesystem_magic != _CGROUP2_SUPER_MAGIC:
                raise ProcessExecutionError("expected Linux resource guard is not cgroup v2")
            if expected.identity != self.scope_identity:
                raise ProcessExecutionError(
                    "received Linux resource guard did not match the expected scope path"
                )
        finally:
            expected.close()

    def _verify_kill_fd(self) -> None:
        if self.kill_fd < 0 or os.get_inheritable(self.kill_fd):
            raise ProcessExecutionError("Linux resource guard cgroup.kill is invalid")
        posix_os = cast("Any", os)
        try:
            expected = posix_os.stat(
                "cgroup.kill",
                dir_fd=self.capability.fd,
                follow_symlinks=False,
            )
            actual = os.fstat(self.kill_fd)
            fcntl_api = cast("Any", __import__("fcntl"))
            access_mode = fcntl_api.fcntl(self.kill_fd, fcntl_api.F_GETFL) & posix_os.O_ACCMODE
        except OSError as exc:
            raise ProcessExecutionError(
                "could not inspect Linux resource guard cgroup.kill"
            ) from exc
        if (
            not stat.S_ISREG(expected.st_mode)
            or not stat.S_ISREG(actual.st_mode)
            or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
            or access_mode not in {posix_os.O_WRONLY, posix_os.O_RDWR}
        ):
            raise ProcessExecutionError("Linux resource guard cgroup.kill did not match scope")

    @staticmethod
    def _validate_attribution_counters(
        memory_events: Mapping[str, int], pids_events: Mapping[str, int]
    ) -> None:
        _required_cgroup_counter(memory_events, "max", control="memory.events")
        _required_cgroup_counter(memory_events, "oom_kill", control="memory.events")
        _required_cgroup_counter(pids_events, "max", control="pids.events")

    def _verify_readback(self) -> None:
        if _read_directory_text(self.capability, "memory.max").strip() != str(
            self.configuration.memory_bytes
        ):
            raise ProcessExecutionError("Linux resource guard memory.max readback mismatch")
        if _read_directory_text(self.capability, "memory.swap.max").strip() != "0":
            raise ProcessExecutionError("Linux resource guard memory.swap.max readback mismatch")
        if _read_directory_text(self.capability, "pids.max").strip() != str(
            self.configuration.processes
        ):
            raise ProcessExecutionError("Linux resource guard pids.max readback mismatch")
        cpu_parts = _read_directory_text(self.capability, "cpu.max").split()
        if len(cpu_parts) != 2 or cpu_parts[0] == "max":
            raise ProcessExecutionError("Linux resource guard cpu.max readback is unavailable")
        try:
            quota, period = (int(value) for value in cpu_parts)
        except ValueError as exc:
            raise ProcessExecutionError("Linux resource guard cpu.max is malformed") from exc
        if (
            quota <= 0
            or period <= 0
            or quota * 100 != self.configuration.cpu_bandwidth_percent * period
        ):
            raise ProcessExecutionError("Linux resource guard cpu.max readback mismatch")

    def cpu_usage_microseconds(self) -> int:
        if not self.validated_for_release:
            raise ProcessExecutionError("Linux resource guard was not validated before use")
        values = _cgroup_counters(self.capability, "cpu.stat")
        value = _required_cgroup_counter(values, "usage_usec", control="cpu.stat")
        return max(0, value - self.cpu_usage_before)

    def hard_event(self) -> tuple[ResourceLimitName, int] | None:
        if not self.validated_for_release:
            raise ProcessExecutionError("Linux resource guard was not validated before use")
        memory_after = _cgroup_counters(self.capability, "memory.events")
        pids_after = _cgroup_counters(self.capability, "pids.events")
        self._validate_attribution_counters(memory_after, pids_after)
        memory_delta = max(
            _required_cgroup_counter(memory_after, "max", control="memory.events")
            - _required_cgroup_counter(self.memory_events_before, "max", control="memory.events"),
            _required_cgroup_counter(memory_after, "oom_kill", control="memory.events")
            - _required_cgroup_counter(
                self.memory_events_before, "oom_kill", control="memory.events"
            ),
        )
        if memory_delta > 0:
            return ("memory", _nonnegative_cgroup_value(self.capability, "memory.peak"))
        pids_delta = _required_cgroup_counter(
            pids_after, "max", control="pids.events"
        ) - _required_cgroup_counter(self.pids_events_before, "max", control="pids.events")
        if pids_delta > 0:
            return ("processes", _nonnegative_cgroup_value(self.capability, "pids.peak"))
        return None

    def control_missing_after_collection(
        self,
        error: ProcessExecutionError,
        *,
        wait_for_owner: bool = False,
    ) -> bool:
        """Accept control ENOENT only after proving this exact owner and scope are gone.

        systemd may collect a fast-empty transient scope between two monitor samples even while a
        now-stale directory descriptor remains open.  This is a terminal teardown state, not an
        attribution failure, but only once the outer owner is definitely gone and the exact path
        is absent.  Every other cgroup read failure remains fail-closed.
        """

        cause = error.__cause__
        if not _is_collected_cgroup_error(cause):
            return False
        # systemd retires the controls and reaps the transient scope's owner independently, and
        # either order is legal, so a cleanup caller waits the same bounded interval the collection
        # proof already uses.  That keeps the plan's "the exact namespace-supervisor (pid,
        # starttime) is gone" requirement intact while removing a false infrastructure failure on a
        # healthy but slow teardown; a genuinely surviving owner still fails closed at expiry.
        #
        # `wait_for_owner` defaults to FALSE because two of this method's callers are on the
        # MONITOR thread (_guard_observations) and hold `guard_lock`.  Blocking there would make
        # cleanup's detach_resource_guard() wait on that lock for longer than the tracker join
        # budget, turning a slow teardown into "Linux descendant monitor did not stop" -- the same
        # class of false failure this wait exists to remove.  Only the cleanup-thread callers,
        # which hold no lock, opt in.
        if wait_for_owner:
            owner_deadline = time.monotonic() + _REAP_TIMEOUT_S
            while not self.outer_owner_exited():
                if time.monotonic() >= owner_deadline:
                    return False
                time.sleep(_POLL_INTERVAL_S)
        elif not self.outer_owner_exited():
            return False
        # systemd can tear down control files just before it unlinks the cgroup directory.  The
        # fresh identity remains authoritative while we boundedly wait for that same object to
        # disappear; replacement or a surviving path is still a fail-closed infrastructure error.
        _wait_for_scope_collection(self.scope_path, self.scope_identity)
        self.collected = True
        return True

    def control_missing_during_unreleased_startup(
        self,
        error: ProcessExecutionError,
    ) -> bool:
        """Accept ENOENT only for a scope proven absent before the target release barrier."""

        if self.validated_for_release or not isinstance(error.__cause__, FileNotFoundError):
            return False
        if not _exact_scope_is_absent(self.scope_path, self.scope_identity):
            return False
        self.collected = True
        return True

    def kill(self) -> None:
        """Kill the whole delegated scope through the pre-opened pinned control FD."""

        if not self.trusted_for_cleanup or self.kill_fd < 0:
            raise ProcessExecutionError("Linux resource guard cgroup.kill is unavailable")
        try:
            if os.write(self.kill_fd, b"1") != 1:
                raise ProcessExecutionError("could not write Linux resource guard cgroup.kill")
        except OSError as exc:
            raise ProcessExecutionError("could not write Linux resource guard cgroup.kill") from exc

    def populated(self) -> bool:
        """Read the pinned scope's live-process bit before closing its descriptors."""

        if not self.trusted_for_cleanup:
            raise ProcessExecutionError("Linux resource guard is not trusted for cleanup")
        value = _required_cgroup_counter(
            _cgroup_counters(self.capability, "cgroup.events"),
            "populated",
            control="cgroup.events",
        )
        if value not in {0, 1}:
            raise ProcessExecutionError("Linux resource guard cgroup.events populated is malformed")
        return value == 1

    def kill_if_populated(self) -> bool:
        if not self.populated():
            return False
        self.kill()
        return True

    def verify_empty(self) -> None:
        """Prove the exact held scope is empty before releasing its descriptors."""

        if self.populated():
            raise ProcessExecutionError("Linux resource guard scope remained populated")

    def wait_until_empty(self) -> None:
        """Bound the asynchronous cgroup.kill transition before collection proof."""

        deadline = time.monotonic() + _REAP_TIMEOUT_S
        while self.populated():
            if time.monotonic() >= deadline:
                raise ProcessExecutionError("Linux resource guard scope remained populated")
            time.sleep(_POLL_INTERVAL_S)

    def verify_collected(self) -> None:
        """After handle release, prove systemd removed the same fresh scope path."""

        _wait_for_scope_collection(self.scope_path, self.scope_identity)

    def close(self) -> None:
        _close_fd(self.kill_fd)
        self.kill_fd = -1
        self.capability.close()


def _validate_evaluator_tmpfs(
    capability: LinuxPathCapability,
    scratch: EvaluatorScratch,
) -> None:
    """Validate the received private mount by FD before allowing target execution."""

    if capability.filesystem_magic != _TMPFS_SUPER_MAGIC:
        raise ProcessExecutionError("evaluator scratch is not backed by tmpfs")
    try:
        values = cast("Any", os).fstatvfs(capability.fd)
        page_size = int(cast("Any", os).sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError) as exc:
        raise ProcessExecutionError("could not inspect evaluator tmpfs bounds") from exc
    capacity_bytes = int(values.f_blocks) * int(values.f_frsize)
    configured_bytes = ((scratch.tmpfs_bytes + page_size - 1) // page_size) * page_size
    required_bytes = evaluator_tmpfs_minimum_bytes(
        file_bytes=scratch.byte_limit,
        files=scratch.file_limit,
    )
    if capacity_bytes < required_bytes or capacity_bytes > configured_bytes:
        raise ProcessExecutionError("evaluator tmpfs byte bound readback mismatch")
    required_inodes = scratch.file_limit + EVALUATOR_DIRECTORY_ALLOWANCE
    if int(values.f_files) < required_inodes or int(values.f_files) > scratch.tmpfs_inodes:
        raise ProcessExecutionError("evaluator tmpfs inode bound readback mismatch")
    scratch.record_backing_bounds(
        bytes_capacity=capacity_bytes,
        inode_capacity=int(values.f_files),
    )
    # This fd is a fresh tmpfs root in the supervisor's private mount namespace and no evaluator
    # target byte has crossed the release barrier.  Mark that structural exclusivity explicitly so
    # the shared copier can reject ordinary caller-controlled destination directories.
    capability._mark_exclusive_copy_destination()


def _tmpfs_hard_exhaustion(
    capability: LinuxPathCapability,
) -> tuple[ResourceLimitName, int] | None:
    """Observe a full held tmpfs for hard-guard attribution, never as its safety boundary."""

    try:
        values = cast("Any", os).fstatvfs(capability.fd)
    except OSError as exc:
        raise ProcessExecutionError("could not inspect evaluator tmpfs exhaustion") from exc
    # `<= 0`, not `== 0`: tmpfs block accounting overshoots on a full filesystem and statvfs
    # reports the remainder as a NEGATIVE count (measured: f_bfree = f_bavail = -1 on a genuinely
    # exhausted private tmpfs).  An equality test silently misses that, so a hard guard that had
    # actually fired would record no exhaustion and no `hard-guard` provenance at all.
    if int(values.f_bavail) <= 0:
        return ("file-bytes", int(values.f_blocks) * int(values.f_frsize))
    if int(values.f_ffree) <= 0:
        return ("file-count", int(values.f_files))
    return None


def _launch_capabilities(request: ProcessRequest) -> tuple[LinuxPathCapability, ...]:
    return (
        *((request._cwd_capability,) if request._cwd_capability is not None else ()),
        *((request._executable_capability,) if request._executable_capability is not None else ()),
        *((request._namespace_capability,) if request._namespace_capability is not None else ()),
        *((request._supervisor_capability,) if request._supervisor_capability is not None else ()),
        *(
            (request._systemd_run_capability,)
            if request._systemd_run_capability is not None
            else ()
        ),
        *request._inherited_capabilities,
    )


def _linux_launch_argv(
    request: ProcessRequest,
    status_write_fd: int,
    control_fd: int,
    scope_relative_path: str,
) -> tuple[str, ...]:
    namespace = request._namespace_capability
    supervisor = request._supervisor_capability
    target = request._executable_capability
    if namespace is None or supervisor is None or target is None:
        raise ProcessContractError("Linux process supervisor capabilities are missing")
    limits = request.resource_limits
    file_size_limit = limits.file_bytes if limits is not None else None
    open_files_limit = limits.open_files if limits is not None else None
    guard = request.resource_guard
    scratch = request._evaluator_scratch
    if (guard is None) != (scratch is None):
        raise ProcessContractError("Linux evaluator launch has incomplete guard state")
    if guard is None and EVALUATOR_WORKSPACE_FD_TOKEN in request.argv:
        raise ProcessContractError("evaluator workspace FD token requires a resource guard")
    return (
        f"/proc/self/fd/{namespace.fd}",
        "--user",
        "--map-root-user",
        "--mount",
        "--pid",
        "--mount-proc=/proc",
        "--fork",
        "--kill-child=KILL",
        "--",
        f"/proc/self/fd/{supervisor.fd}",
        "-I",
        "-c",
        _SUPERVISOR_CODE,
        str(status_write_fd),
        str(target.fd),
        str(control_fd),
        "1" if guard is not None else "0",
        str(len(request.environment)),
        str(file_size_limit if file_size_limit is not None else -1),
        str(open_files_limit if open_files_limit is not None else -1),
        str(guard.memory_bytes if guard is not None else -1),
        str(guard.processes if guard is not None else -1),
        str(guard.cpu_bandwidth_percent if guard is not None else -1),
        str(scratch.tmpfs_bytes if scratch is not None else -1),
        str(scratch.tmpfs_inodes if scratch is not None else -1),
        scope_relative_path,
        *(item for pair in request.environment for item in pair),
        *request.argv,
    )


def _systemd_scope_argv(
    request: ProcessRequest,
    unshare_argv: tuple[str, ...],
    scope_name: str,
) -> tuple[str, ...]:
    """Wrap the exact unshare owner in a fresh pinned delegated user scope."""

    systemd_run = request._systemd_run_capability
    guard = request.resource_guard
    if systemd_run is None or guard is None:
        raise ProcessContractError("Linux resource guard systemd-run capability is missing")
    return (
        f"/proc/self/fd/{systemd_run.fd}",
        "--user",
        "--scope",
        "--collect",
        "--quiet",
        f"--unit={scope_name}",
        "--slice=app.slice",
        "--property=Delegate=yes",
        f"--property=MemoryMax={guard.memory_bytes}",
        "--property=MemorySwapMax=0",
        f"--property=TasksMax={guard.processes}",
        f"--property=CPUQuota={guard.cpu_bandwidth_percent}%",
        "--",
        *unshare_argv,
    )


def _systemd_bus_environment() -> dict[str, str]:
    if not hasattr(os, "getuid"):
        raise ProcessExecutionError("Linux resource guard requires a POSIX user identity")
    uid = os.getuid()
    runtime_directory = f"/run/user/{uid}"
    return {
        "XDG_RUNTIME_DIR": runtime_directory,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_directory}/bus",
    }


def _expected_scope_relative_path(scope_name: str) -> str:
    """Return the explicit user-manager cgroup path for a fresh transient scope."""

    if not hasattr(os, "getuid"):
        raise ProcessExecutionError("Linux resource guard requires a POSIX user identity")
    uid = os.getuid()
    return f"/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/{scope_name}.scope"


def _reap_failed_start(proc: subprocess.Popen[bytes]) -> BaseException | None:
    """Bound a failed outer owner reap without hiding a surviving direct child."""

    error: BaseException | None = None
    process_group_id = proc.pid if os.name == "posix" else None
    try:
        _kill_tree(proc, process_group_id, None)
    except BaseException as exc:
        error = exc
    try:
        proc.wait(timeout=_REAP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        error = ProcessExecutionError("direct child survived failed-start termination")
    except OSError as exc:
        error = exc
    _close_pipe(proc.stdin)
    _close_pipe(proc.stdout)
    _close_pipe(proc.stderr)
    return error


def _cleanup_failed_linux_start(
    proc: subprocess.Popen[bytes],
    runtime: _RunningProcess | None,
    scope_relative_path: str,
) -> BaseException | None:
    """Fail closed after Popen: kill the verified scope, then prove its collection."""

    errors: list[BaseException] = []
    guard = runtime.resource_guard if runtime is not None else None
    trusted_guard = guard is not None and guard.trusted_for_cleanup
    if trusted_guard and guard is not None and not guard.collected:
        try:
            guard.kill_if_populated()
        except BaseException as exc:
            if not (
                isinstance(exc, ProcessExecutionError)
                and (
                    guard.control_missing_during_unreleased_startup(exc)
                    or guard.control_missing_after_collection(exc, wait_for_owner=True)
                )
            ):
                errors.append(exc)
    reap_error = _reap_failed_start(proc)
    if reap_error is not None:
        errors.append(reap_error)
    if runtime is not None:
        # The failed outer owner can retain the inherited writer.  Startup cleanup does not wait
        # for a status record after kill-first cleanup, so drop our reader rather than blocking.
        _close_fd(runtime.status_read_fd)
        runtime.status_read_fd = None
    if guard is not None:
        try:
            if trusted_guard and not guard.collected:
                guard.wait_until_empty()
        except BaseException as exc:
            if not (
                isinstance(exc, ProcessExecutionError)
                and (
                    guard.control_missing_during_unreleased_startup(exc)
                    or guard.control_missing_after_collection(exc, wait_for_owner=True)
                )
            ):
                errors.append(exc)
        finally:
            guard.close()
            if runtime is not None:
                runtime.resource_guard = None
        try:
            if trusted_guard:
                guard.verify_collected()
            else:
                _wait_for_scope_collection(guard.scope_path, None)
        except BaseException as exc:
            errors.append(exc)
    elif scope_relative_path:
        # No descriptor crossed the handshake boundary, so never issue a path-based kill.  The
        # fresh exact name plus a reaped systemd-run owner still lets us prove no scope remained.
        try:
            _wait_for_scope_collection(_scope_collection_path(scope_relative_path), None)
        except BaseException as exc:
            errors.append(exc)
    if runtime is not None and runtime.scratch_tree is not None:
        runtime.scratch_tree.close()
        runtime.scratch_tree = None
    return errors[0] if errors else None


def _received_rights(ancillary: list[tuple[int, int, bytes]]) -> list[int]:
    received: list[int] = []
    socket_api = cast("Any", socket)
    for level, kind, payload in ancillary:
        if level != socket.SOL_SOCKET or kind != socket_api.SCM_RIGHTS:
            continue
        values = array.array("i")
        usable = len(payload) - (len(payload) % values.itemsize)
        values.frombytes(payload[:usable])
        received.extend(values)
    return received


def _receive_evaluator_handshake(
    control: socket.socket,
    request: ProcessRequest,
    runtime: _RunningProcess,
    scope_relative_path: str,
) -> LinuxPathCapability:
    """Receive and prove the scope and scratch FDs before releasing the supervisor."""

    guard = request.resource_guard
    scratch = request._evaluator_scratch
    if guard is None or scratch is None:
        raise ProcessContractError("evaluator handshake requires resource guard and scratch")
    control.settimeout(SANDBOX_SETUP_TIMEOUT_S)
    try:
        socket_api = cast("Any", socket)
        payload, ancillary, message_flags, _address = cast("Any", control).recvmsg(
            16,
            socket_api.CMSG_SPACE(3 * array.array("i").itemsize),
            int(getattr(socket_api, "MSG_CMSG_CLOEXEC", 0)),
        )
    except (OSError, TimeoutError) as exc:
        raise ProcessExecutionError(
            "Linux evaluator supervisor did not reach its release barrier"
        ) from exc
    received = _received_rights(ancillary)
    if payload != b"MT26R" or message_flags & socket.MSG_CTRUNC or len(received) != 3:
        for fd in received:
            _close_fd(fd)
        raise ProcessExecutionError("Linux evaluator supervisor returned an invalid FD handshake")
    cgroup_fd, scratch_fd, cgroup_kill_fd = received
    cgroup_capability: LinuxPathCapability | None = None
    scratch_capability: LinuxPathCapability | None = None
    state: _LinuxResourceGuardState | None = None
    try:
        for fd in received:
            os.set_inheritable(fd, False)
        received_cgroup_fd = cgroup_fd
        cgroup_fd = -1
        try:
            cgroup_capability = LinuxPathCapability._from_open_fd(
                received_cgroup_fd,
                display_path="/sys/fs/cgroup/measure-twice-scope",
                expected="directory",
            )
        except LinuxCapabilityError as exc:
            raise ProcessExecutionError(
                "Linux evaluator supervisor returned an invalid cgroup capability"
            ) from exc
        if cgroup_capability is None:
            raise ProcessExecutionError("Linux evaluator handshake lost its cgroup capability")
        received_cgroup_capability = cgroup_capability
        cgroup_capability = None
        received_kill_fd = cgroup_kill_fd
        cgroup_kill_fd = -1
        state = _LinuxResourceGuardState.adopt(
            received_cgroup_capability,
            received_kill_fd,
            guard,
            scope_relative_path=scope_relative_path,
        )
        # From here on, every failure is a post-handshake startup failure.  Transfer the pinned
        # scope immediately so the common startup cleanup owns kill/empty/collection proof.
        runtime.resource_guard = state
        state = None
        runtime.resource_guard.validate_before_release()
        # Only after the cgroup and kill descriptor are verified and runtime-owned may an
        # untrusted scratch descriptor be interpreted.  A malformed second FD therefore still
        # routes through the same scope kill/empty/collection cleanup.
        received_scratch_fd = scratch_fd
        scratch_fd = -1
        try:
            scratch_capability = LinuxPathCapability._from_open_fd(
                received_scratch_fd,
                display_path="/var/tmp/measure-twice-evaluator-tmpfs",  # noqa: S108 - diagnostic only.
                expected="directory",
            )
        except LinuxCapabilityError as exc:
            raise ProcessExecutionError(
                "Linux evaluator supervisor returned an invalid scratch capability"
            ) from exc
        _validate_evaluator_tmpfs(scratch_capability, scratch)
        try:
            copy_tree(
                scratch.source_capability(),
                scratch_capability,
                file_limit=scratch.file_limit,
                byte_limit=scratch.byte_limit,
            )
        except LinuxTreeLimitError as exc:
            raise ProcessExecutionError(
                "evaluator applied tree cannot fit the private tmpfs logical limits"
            ) from exc
        except LinuxCapabilityError as exc:
            raise ProcessExecutionError(
                "could not materialize evaluator applied tree in the private tmpfs"
            ) from exc
        scratch.adopt_terminal_tree(scratch_capability.duplicate())
        return scratch_capability
    except BaseException:
        if state is not None:
            state.close()
        if cgroup_capability is not None:
            cgroup_capability.close()
        if scratch_capability is not None:
            scratch_capability.close()
        _close_fd(cgroup_fd)
        _close_fd(scratch_fd)
        _close_fd(cgroup_kill_fd)
        raise


def _start_process(request: ProcessRequest) -> _RunningProcess:
    """Spawn the outer owner and return immediately into cleanup-covered state."""

    capabilities = _launch_capabilities(request)
    status_read_fd: int | None = None
    status_write_fd: int | None = None
    control_parent: socket.socket | None = None
    control_child_fd: int | None = None
    proc: subprocess.Popen[bytes] | None = None
    runtime: _RunningProcess | None = None
    scope_relative_path = ""
    try:
        cwd: str | Path = (
            f"/proc/self/fd/{request._cwd_capability.fd}"
            if request._cwd_capability is not None
            else request.cwd
        )
        preexec_fn = (
            _limit_preexec(request.resource_limits)
            if request.resource_limits is not None and sys.platform != "linux"
            else None
        )
        if sys.platform == "linux":
            status_read_fd, status_write_fd = os.pipe2(os.O_CLOEXEC)
            scope_name: str | None = None
            if request.resource_guard is not None:
                control_parent, control_child = socket.socketpair(
                    socket.AF_UNIX, socket.SOCK_SEQPACKET
                )
                control_child_fd = control_child.detach()
                scope_name = f"measure-twice-{uuid.uuid4().hex}"
                scope_relative_path = _expected_scope_relative_path(scope_name)
            unshare_argv = _linux_launch_argv(
                request,
                status_write_fd,
                -1 if control_child_fd is None else control_child_fd,
                scope_relative_path,
            )
            if request.resource_guard is None:
                launch_argv = unshare_argv
                launch_environment = dict(request.environment)
            else:
                launch_argv = _systemd_scope_argv(
                    request,
                    unshare_argv,
                    cast("str", scope_name),
                )
                launch_environment = _systemd_bus_environment()
            pass_fds = (
                *tuple(capability.fd for capability in capabilities),
                status_write_fd,
                *((control_child_fd,) if control_child_fd is not None else ()),
            )
            proc = subprocess.Popen(  # noqa: S603 - every executable is pinned by descriptor.
                launch_argv,
                executable=launch_argv[0],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=launch_environment,
                text=False,
                bufsize=0,
                start_new_session=True,
                close_fds=True,
                pass_fds=pass_fds,
                preexec_fn=preexec_fn,
            )
            _close_fd(control_child_fd)
            control_child_fd = None
            runtime = _RunningProcess(
                proc=proc,
                # Provisional: correct for a guard-free Linux run, where the child is already
                # executing.  The evaluator seam re-anchors this at the release barrier below.
                started=time.monotonic(),
                process_group_id=proc.pid,
                status_read_fd=status_read_fd,
            )
            if control_parent is not None:
                try:
                    runtime.scratch_tree = _receive_evaluator_handshake(
                        control_parent,
                        request,
                        runtime,
                        scope_relative_path,
                    )
                except BaseException:
                    try:
                        control_parent.sendall(b"X")
                    except OSError:
                        pass
                    raise
                try:
                    control_parent.sendall(b"R")
                    # The barrier is open: this is the first instant any submitted byte can
                    # execute, so it -- not the start of bring-up -- is the target's clock
                    # origin.  Anchoring earlier charges systemd-run, cgroup delegation, the
                    # tmpfs mount and the handshake to the model's wall-clock budget.
                    runtime.started = time.monotonic()
                except BaseException:
                    # A failed release send is ambiguous: the supervisor could already have
                    # received it and forked the Bubblewrap target.  Do not take the graceful
                    # cancellation branch even if a subsequent best-effort ``X`` is delivered.
                    try:
                        control_parent.sendall(b"X")
                    except OSError:
                        pass
                    raise
            return runtime
        elif os.name == "posix":
            proc = subprocess.Popen(  # noqa: S603 - executable is absolute; shell is false.
                request.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=dict(request.environment),
                text=False,
                bufsize=0,
                start_new_session=True,
                close_fds=True,
                preexec_fn=preexec_fn,
            )
        else:
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | _WINDOWS_CREATE_SUSPENDED
            proc = subprocess.Popen(  # noqa: S603 - argv[0] is absolute and shell is false.
                request.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=dict(request.environment),
                text=False,
                bufsize=0,
                creationflags=creationflags,
            )
        return _RunningProcess(
            proc=proc,
            # POSIX children run from Popen; the Windows child is created suspended and has its
            # origin corrected in _initialize_process once the Win32 job resumes it.
            started=time.monotonic(),
            process_group_id=proc.pid if os.name == "posix" else None,
            status_read_fd=status_read_fd,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        cleanup_error: BaseException | None = None
        if proc is not None:
            cleanup_error = _cleanup_failed_linux_start(proc, runtime, scope_relative_path)
        if request._evaluator_scratch is not None:
            request._evaluator_scratch.close()
        if cleanup_error is not None:
            raise cleanup_error from exc
        detail = exc.strerror if isinstance(exc, OSError) else str(exc)
        raise ProcessExecutionError(
            f"could not start process executable {request.argv[0]!r}: {detail or exc}"
        ) from exc
    except BaseException:
        cleanup_error = None
        if proc is not None:
            cleanup_error = _cleanup_failed_linux_start(proc, runtime, scope_relative_path)
        if request._evaluator_scratch is not None:
            request._evaluator_scratch.close()
        if cleanup_error is not None:
            raise cleanup_error from None
        raise
    finally:
        _close_fd(status_write_fd)
        _close_fd(control_child_fd)
        if control_parent is not None:
            control_parent.close()
        if proc is None:
            _close_fd(status_read_fd)
        for capability in capabilities:
            capability.close()


def _start_owned_thread(runtime: _RunningProcess, thread: threading.Thread) -> None:
    runtime.pipe_threads.append(thread)
    thread.start()


def _initialize_process(request: ProcessRequest, runtime: _RunningProcess) -> None:
    """Establish identity, monitors, and pipe workers after ownership is cleanup-covered."""

    proc = runtime.proc
    runtime.job = _WindowsJob.assign(proc)
    if sys.platform == "win32" and runtime.job is None:
        raise ProcessExecutionError(
            "could not assign suspended process to a Win32 kill-on-close job"
        )
    if sys.platform == "win32":
        # The child was created suspended and NtResumeProcess ran inside assign(); until now no
        # target instruction had executed, so this is the Windows clock origin.
        runtime.started = time.monotonic()
    if sys.platform == "linux":
        root_starttime = _pid_starttime(proc.pid)
        if root_starttime is None:
            raise ProcessExecutionError("could not establish Linux process identity")
        runtime.tracker = _LinuxDescendantTracker(
            root_pid=proc.pid,
            root_starttime=root_starttime,
            resource_limits=request.resource_limits,
            tree_capability=runtime.scratch_tree or request._tree_capability,
            tree_before_open=request._tree_before_open,
            resource_guard=runtime.resource_guard,
            evaluator_scratch=request._evaluator_scratch,
        )
        runtime.tracker_thread = threading.Thread(
            target=runtime.tracker.monitor,
            name="agent-bench-descendants",
            daemon=True,
        )
        runtime.tracker_thread.start()

    secrets = tuple(value.encode("utf-8") for value in request.secret_values)
    reserve = max((len(value) for value in secrets), default=0)
    runtime.stdout_capture = _BoundedCapture(
        request.stream_limit_bytes, reserve, secrets, bytearray()
    )
    runtime.stderr_capture = _BoundedCapture(
        request.stream_limit_bytes, reserve, secrets, bytearray()
    )
    stdin_pipe = cast("IO[bytes]", proc.stdin)
    stdout_pipe = cast("IO[bytes]", proc.stdout)
    stderr_pipe = cast("IO[bytes]", proc.stderr)
    for thread in (
        threading.Thread(
            target=_read_pipe,
            args=(
                stdout_pipe,
                runtime.stdout_capture,
                runtime.stream_limit_reached,
                runtime.stdout_done,
            ),
            name="agent-bench-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=_read_pipe,
            args=(
                stderr_pipe,
                runtime.stderr_capture,
                runtime.stream_limit_reached,
                runtime.stderr_done,
            ),
            name="agent-bench-stderr",
            daemon=True,
        ),
        threading.Thread(
            target=_write_stdin,
            args=(stdin_pipe, request.stdin_bytes),
            name="agent-bench-stdin",
            daemon=True,
        ),
    ):
        _start_owned_thread(runtime, thread)


def _process_has_exited(proc: subprocess.Popen[bytes]) -> bool:
    if sys.platform != "linux":
        return proc.poll() is not None
    posix_os = cast("Any", os)
    try:
        status = posix_os.waitid(
            posix_os.P_PID,
            proc.pid,
            posix_os.WEXITED | posix_os.WNOHANG | posix_os.WNOWAIT,
        )
    except ChildProcessError:
        return True
    except OSError:
        return False
    return status is not None


def _raise_tracker_failure(tracker: _LinuxDescendantTracker) -> None:
    with tracker.lock:
        error = tracker.monitor_error
    if error is None:
        raise ProcessExecutionError("Linux descendant monitor failed without an error record")
    raise ProcessExecutionError(f"Linux descendant monitor failed: {error}") from error


def _wait_for_process(
    request: ProcessRequest, runtime: _RunningProcess
) -> tuple[Termination, bool]:
    deadline = runtime.started + float(request.timeout_s)
    while True:
        if runtime.stream_limit_reached.is_set():
            return "stream-limit", True
        tracker = runtime.tracker
        if tracker is not None and tracker.monitor_failed.is_set():
            _raise_tracker_failure(tracker)
        if tracker is not None and tracker.limit_reached.is_set():
            return "resource-limit", True
        if (
            _process_has_exited(runtime.proc)
            and runtime.stdout_done.is_set()
            and runtime.stderr_done.is_set()
        ):
            return "exited", False
        if time.monotonic() >= deadline:
            return "timeout", True
        time.sleep(_POLL_INTERVAL_S)


def _read_linux_target_status(runtime: _RunningProcess, *, required: bool) -> None:
    fd = runtime.status_read_fd
    runtime.status_read_fd = None
    if fd is None:
        if required:
            raise ProcessExecutionError("Linux process supervisor status pipe is missing")
        return
    payload = bytearray()
    deadline = time.monotonic() + _REAP_TIMEOUT_S
    try:
        os.set_blocking(fd, False)
        while len(payload) < _SUPERVISOR_STATUS.size:
            try:
                chunk = os.read(fd, _SUPERVISOR_STATUS.size - len(payload))
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProcessExecutionError(
                        "Linux process supervisor did not finish its status record"
                    ) from None
                try:
                    readable, _writable, _exceptional = select.select([fd], [], [], remaining)
                except OSError as exc:
                    raise ProcessExecutionError(
                        "could not wait for Linux process supervisor status"
                    ) from exc
                if not readable:
                    raise ProcessExecutionError(
                        "Linux process supervisor did not finish its status record"
                    ) from None
                continue
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) == _SUPERVISOR_STATUS.size:
            try:
                extra = os.read(fd, 1)
            except BlockingIOError:
                extra = b""
            if extra:
                payload.extend(extra)
    finally:
        _close_fd(fd)
    if not payload and not required:
        return
    if len(payload) != _SUPERVISOR_STATUS.size:
        raise ProcessExecutionError("Linux process supervisor returned an invalid status record")
    (
        magic,
        wait_status,
        exec_error,
        cpu_microseconds,
        hard_limit,
        hard_observed,
        setup_error,
    ) = _SUPERVISOR_STATUS.unpack(payload)
    if magic != _SUPERVISOR_MAGIC:
        raise ProcessExecutionError("Linux process supervisor returned an invalid status record")
    runtime.target_status = _LinuxTargetStatus(
        wait_status,
        exec_error,
        cpu_microseconds,
        hard_limit,
        hard_observed,
        setup_error,
    )


def _cleanup_process(runtime: _RunningProcess, *, abnormal: bool) -> None:
    """Stop and reap every owned object, even when one cleanup operation itself fails."""

    errors: list[BaseException] = []
    tracked: dict[int, int] = {}
    terminal_tree_quiescent = False
    tracker = runtime.tracker
    if tracker is not None:
        tracker.stop.set()
        if runtime.tracker_thread is not None:
            try:
                runtime.tracker_thread.join(timeout=_REAP_TIMEOUT_S)
            except RuntimeError as exc:
                errors.append(ProcessExecutionError("Linux descendant monitor was not started"))
                errors.append(exc)
            if runtime.tracker_thread.is_alive():
                errors.append(ProcessExecutionError("Linux descendant monitor did not stop"))
        # Take one final pinned cgroup sample before any abnormal cleanup can write cgroup.kill.
        # It may replace a prior sampled threshold with an intervening hard kernel event.
        try:
            tracker.final_guard_observation()
        except BaseException as exc:
            errors.append(exc)
        if tracker.monitor_failed.is_set():
            try:
                _raise_tracker_failure(tracker)
            except BaseException as exc:
                errors.append(exc)
        # This waits for an in-flight cgroup control read and prevents every later monitor pass
        # from borrowing the runtime-owned guard.  The following force scan remains logical-tree
        # only, so cleanup can safely close the guard even if a monitor thread missed its join.
        tracker.detach_resource_guard()
        try:
            # This is still a pre-kill liveness pass.  A mutating evaluator tree is an
            # inconclusive sample here; the authoritative scan happens after cgroup emptiness.
            tracked.update(tracker.snapshot(force_tree=False))
        except BaseException as exc:
            errors.append(exc)
    if abnormal or tracked:
        if runtime.resource_guard is not None:
            try:
                # Historical descendant identities remain in ``tracked`` after a normal exit.
                # The held cgroup may already be empty then; write cgroup.kill only when the
                # pinned cgroup.events control proves there is something left to terminate.
                runtime.resource_guard.kill_if_populated()
            except BaseException as exc:
                if not (
                    isinstance(exc, ProcessExecutionError)
                    and runtime.resource_guard.control_missing_after_collection(
                        exc, wait_for_owner=True
                    )
                ):
                    errors.append(exc)
        try:
            _kill_tree(runtime.proc, runtime.process_group_id, runtime.job, tracked)
        except BaseException as exc:
            errors.append(exc)
    try:
        runtime.proc.wait(timeout=_REAP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        try:
            _kill_tree(runtime.proc, runtime.process_group_id, runtime.job, tracked)
            runtime.proc.wait(timeout=_REAP_TIMEOUT_S)
        except BaseException as exc:
            errors.append(
                ProcessExecutionError("direct child survived whole-tree termination")
                if isinstance(exc, subprocess.TimeoutExpired)
                else exc
            )
    except BaseException as exc:
        errors.append(exc)
    _close_pipe(runtime.proc.stdin)
    for thread in runtime.pipe_threads:
        if thread.ident is not None:
            thread.join(timeout=_REAP_TIMEOUT_S)
    alive = [thread for thread in runtime.pipe_threads if thread.is_alive()]
    _close_pipe(runtime.proc.stdout)
    _close_pipe(runtime.proc.stderr)
    for thread in alive:
        thread.join(timeout=_REAP_TIMEOUT_S)
    if any(thread.is_alive() for thread in runtime.pipe_threads):
        errors.append(
            ProcessExecutionError("process pipe worker did not stop after whole-tree kill")
        )
    if runtime.job is not None:
        try:
            runtime.job.close()
        except BaseException as exc:
            errors.append(exc)
    if sys.platform == "linux" and runtime.proc.poll() is None:
        # ``unshare`` keeps the inherited write end open. If it survived both bounded waits,
        # waiting for EOF on this pipe would turn the cleanup failure into an unbounded hang.
        _close_fd(runtime.status_read_fd)
        runtime.status_read_fd = None
    else:
        try:
            _read_linux_target_status(runtime, required=sys.platform == "linux" and not abnormal)
        except BaseException as exc:
            errors.append(exc)
    _close_fd(runtime.status_read_fd)
    runtime.status_read_fd = None
    if runtime.resource_guard is not None:
        guard = runtime.resource_guard
        guard_empty = False
        try:
            # The outer systemd-run client can already have exited while a deliberately
            # reparented child remains in its delegated scope.  Test the cgroup itself on
            # every terminal path, not merely when the /proc descendant snapshot was nonempty.
            if not guard.collected:
                guard.kill_if_populated()
                guard.wait_until_empty()
            guard_empty = True
        except BaseException as exc:
            collected = isinstance(
                exc, ProcessExecutionError
            ) and guard.control_missing_after_collection(exc, wait_for_owner=True)
            if collected:
                # This exact branch proves the namespace-init owner and named scope disappeared.
                # Collection implies cgroup emptiness, so terminal retained-FD validation is safe.
                guard_empty = True
            else:
                errors.append(exc)
        finally:
            guard.close()
            runtime.resource_guard = None
        try:
            # Prove exact collection only after releasing the owned cgroup capabilities.
            guard.verify_collected()
        except BaseException as exc:
            errors.append(exc)
        terminal_tree_quiescent = guard_empty and runtime.proc.poll() is not None
    else:
        # Every Linux run has an outer PID namespace owner. Once that owner is reaped, its
        # namespace descendants are gone, so a legacy pinned tree root is safe to scan strictly.
        terminal_tree_quiescent = sys.platform == "linux" and runtime.proc.poll() is not None
    if tracker is not None and terminal_tree_quiescent:
        try:
            tracker.validate_terminal_tree()
        except BaseException as exc:
            errors.append(exc)
    if tracker is not None and runtime.tracker_thread is not None:
        if runtime.tracker_thread.is_alive() and tracker.tree_capability is not None:
            # The tracker's tree capability is `runtime.scratch_tree or request._tree_capability`
            # (see _initialize_process).  The request-owned half is closed later by
            # ProcessRequest._release(), which is outside this function's reach, so abandoning it
            # here is what makes the surviving-monitor hazard covered on BOTH descriptors rather
            # than only on the evaluator one.  A non-evaluator Linux run with tree_root set takes
            # exactly that path.
            tracker.tree_capability.abandon()
    if runtime.scratch_tree is not None:
        if runtime.tracker_thread is not None and runtime.tracker_thread.is_alive():
            # Deliberately leak this descriptor rather than close it.  `detach_resource_guard`
            # above makes teardown of the cgroup descriptors atomic against an in-flight monitor
            # read, but the tree capability has no such exclusion: `_tmpfs_hard_exhaustion` and
            # `walk_tree` take the raw `fd` int and release the capability's own lock before
            # issuing their syscalls, so closing here lets a surviving monitor's next fstatvfs or
            # scandir land on a REUSED descriptor number -- an unrelated object.  A monitor that
            # outlived its bounded join has already produced "Linux descendant monitor did not
            # stop" in `errors`, so this run is failing regardless; one leaked FD on a failing
            # run is strictly safer than a use-after-close on someone else's descriptor.
            # An exclusion held across a whole tree walk is NOT the fix: unlike the short cgroup
            # control reads `guard_lock` covers, a walk is long enough that waiting on it would
            # trade this hazard for a cleanup hang.
            runtime.scratch_tree = None
        else:
            runtime.scratch_tree.close()
            runtime.scratch_tree = None
    if errors:
        raise errors[0]


def _target_finished_at(runtime: _RunningProcess) -> float:
    """When the target stopped.

    ``is not None``, not truthiness: a monotonic clock can legitimately read ``0.0`` and an
    ``or`` would silently swap it for "now".  The fallback is unreachable today -- the only
    ``_render_result`` caller sets ``finished`` first -- but a future error path that forgets to
    stop the clock should degrade to a defined value here rather than inherit a wrong origin.
    """

    return runtime.finished if runtime.finished is not None else time.monotonic()


def _render_result(
    request: ProcessRequest,
    runtime: _RunningProcess,
    termination: Termination,
    abnormal: bool,
) -> ProcessResult:
    stdout_capture = runtime.stdout_capture
    stderr_capture = runtime.stderr_capture
    if stdout_capture is None or stderr_capture is None:
        raise ProcessExecutionError("process capture workers were not initialized")
    for label, capture in (("stdout", stdout_capture), ("stderr", stderr_capture)):
        if capture.error is not None and not abnormal:
            raise ProcessExecutionError(f"could not collect process {label}: {capture.error}")
    tracker = runtime.tracker
    if sys.platform == "linux":
        target_status = runtime.target_status
        if target_status is None:
            if not abnormal:
                raise ProcessExecutionError("Linux process supervisor did not report target status")
            returncode: int | None = None
        elif target_status.setup_error:
            detail = os.strerror(target_status.setup_error)
            raise ProcessExecutionError(
                f"Linux resource guard setup, attribution, or teardown failed: {detail}"
            )
        elif target_status.exec_error:
            detail = os.strerror(target_status.exec_error)
            raise ProcessExecutionError(
                f"could not start process executable {request.argv[0]!r}: {detail}"
            )
        else:
            returncode = os.waitstatus_to_exitcode(target_status.wait_status)
            limits = request.resource_limits
            if (
                tracker is not None
                and limits is not None
                and limits.cpu_seconds is not None
                and target_status.cpu_microseconds >= limits.cpu_seconds * 1_000_000
            ):
                tracker._set_resource_limit(
                    "cpu",
                    observed=target_status.cpu_microseconds // 1_000_000,
                )
    else:
        returncode = runtime.proc.returncode
    resource_limit = tracker.resource_limit if tracker is not None else None
    resource_limit_value = tracker.resource_limit_value if tracker is not None else None
    resource_limit_observed = tracker.resource_limit_observed if tracker is not None else None
    resource_limit_provenance = tracker.resource_limit_provenance if tracker is not None else None
    tree_sample_inconclusive_count = (
        tracker.tree_sample_inconclusive_count if tracker is not None else 0
    )
    if sys.platform == "linux" and runtime.target_status is not None:
        hard_code = runtime.target_status.hard_limit
        if hard_code:
            hard_name: ResourceLimitName
            if hard_code == 1:
                hard_name = "memory"
            elif hard_code == 2:
                hard_name = "processes"
            elif hard_code == 3:
                hard_name = "file-bytes"
            elif hard_code == 4:
                hard_name = "file-count"
            else:
                raise ProcessExecutionError("Linux resource guard reported an unknown hard event")
            if hard_name == "memory" and request.resource_guard is not None:
                hard_value = request.resource_guard.memory_bytes
            elif hard_name == "processes" and request.resource_guard is not None:
                hard_value = request.resource_guard.processes
            elif (
                hard_name in {"file-bytes", "file-count"} and request._evaluator_scratch is not None
            ):
                # These codes are only the supervisor's terminal fstatvfs-full observation;
                # they do not infer a transient full-and-delete event.
                hard_value = request._evaluator_scratch.physical_limit(hard_name)
            else:
                raise ProcessExecutionError(
                    "Linux resource guard reported an unconfigured hard event"
                )
            if tracker is not None:
                tracker._set_resource_limit(
                    hard_name,
                    provenance="hard-guard",
                    observed=runtime.target_status.hard_observed,
                    limit_value=hard_value,
                    replace=True,
                )
                resource_limit = tracker.resource_limit
                resource_limit_value = tracker.resource_limit_value
                resource_limit_observed = tracker.resource_limit_observed
                resource_limit_provenance = tracker.resource_limit_provenance
            else:
                resource_limit = hard_name
                resource_limit_value = hard_value
                resource_limit_observed = runtime.target_status.hard_observed
                resource_limit_provenance = "hard-guard"
    if resource_limit is not None:
        termination = "resource-limit"
    if termination == "exited" and returncode is not None and returncode < 0:
        termination = "signalled"
    signal_number = -returncode if returncode is not None and returncode < 0 else None
    exit_code = (
        returncode
        if termination == "exited" and returncode is not None and returncode >= 0
        else None
    )
    stdout, stdout_secret_detected = stdout_capture.rendered()
    stderr, stderr_secret_detected = stderr_capture.rendered()
    return ProcessResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        signal=signal_number,
        elapsed_ms=max(0, int((_target_finished_at(runtime) - runtime.started) * 1000)),
        termination=termination,
        stdout_limit_exceeded=stdout_capture.exceeded,
        stderr_limit_exceeded=stderr_capture.exceeded,
        stdout_secret_detected=stdout_secret_detected,
        stderr_secret_detected=stderr_secret_detected,
        resource_limit=resource_limit,
        resource_limit_value=resource_limit_value,
        resource_limit_observed=resource_limit_observed,
        resource_limit_provenance=resource_limit_provenance,
        tree_sample_inconclusive_count=tree_sample_inconclusive_count,
    )


def _run_process_owned(request: ProcessRequest) -> ProcessResult:
    """Execute one request with startup, waiting, and cleanup as explicit ownership phases."""

    if sys.platform != "linux" and not request.cwd.is_dir():
        raise ProcessContractError(f"process cwd is not a directory: {str(request.cwd)!r}")
    if request.resource_limits is not None and os.name != "posix":
        raise ProcessContractError("process resource limits require a POSIX host")
    runtime = _start_process(request)
    termination: Termination | None = None
    abnormal = True
    try:
        _initialize_process(request, runtime)
        termination, abnormal = _wait_for_process(request, runtime)
        # Stop the target's clock here: _cleanup_process below runs a bounded reap chain that
        # can legitimately spend tens of seconds, and none of it is the target's runtime.
        runtime.finished = time.monotonic()
    finally:
        _cleanup_process(runtime, abnormal=abnormal)
    if termination is None:
        raise ProcessExecutionError("process stopped before a terminal result was established")
    return _render_result(request, runtime, termination, abnormal)


def _run_process(request: ProcessRequest) -> ProcessResult:
    request._claim()
    try:
        return _run_process_owned(request)
    finally:
        request._release()


def run_process(request: ProcessRequest) -> ProcessResult:
    """Run one immutable process request under the platform's whole-tree owner."""

    if sys.platform == "linux":
        # Each invocation receives its own PID namespace. Serializing preserves the execution
        # profile's concurrency=1 without claiming process-wide ownership of other harness work.
        with _LINUX_RUN_LOCK:
            return _run_process(request)
    return _run_process(request)


__all__ = [
    "ModelTreeViolationError",
    "ProcessContractError",
    "ProcessExecutionError",
    "ProcessRequest",
    "ProcessResourceLimits",
    "ProcessResult",
    "Termination",
    "run_process",
]
