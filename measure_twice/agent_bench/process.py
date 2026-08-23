"""Bounded, environment-explicit subprocess execution for coding-agent cells.

The response-only adapter predates workspace mutation and intentionally has a much smaller
subprocess seam.  Agent cells need a stricter contract: requests are immutable, stdin is encoded
as UTF-8 explicitly, no ambient environment is inherited, captured streams have hard byte
ceilings, and every terminal path reaps the whole process tree.

This module knows nothing about provider event formats.  It returns bytes so an adapter can parse
its own protocol without locale decoding or replacement changing the evidence.
"""

from __future__ import annotations

import ctypes
import math
import os
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any, Final, Literal, Self, cast

from measure_twice.agent_bench._linux_capabilities import (
    LinuxCapabilityError,
    LinuxPathCapability,
    LinuxTreeLimitError,
    walk_tree,
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
_SUPERVISOR_STATUS = struct.Struct("!4siiq")
_SUPERVISOR_MAGIC: Final[bytes] = b"MT26"
_SUPERVISOR_CODE: Final[str] = """
import os
import errno
import resource
import signal
import struct
import sys

status_fd = int(sys.argv[1])
target_fd = int(sys.argv[2])
environment_count = int(sys.argv[3])
file_size_limit = int(sys.argv[4])
open_files_limit = int(sys.argv[5])
environment_items = sys.argv[6:6 + environment_count * 2]
target_environment = dict(zip(environment_items[::2], environment_items[1::2], strict=True))
target_argv = sys.argv[6 + environment_count * 2:]
os.set_inheritable(status_fd, False)
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
    os._exit(125)
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
cpu_microseconds = int(cpu_seconds * 1_000_000)
payload = struct.pack("!4siiq", b"MT26", wait_status, error_number, cpu_microseconds)
offset = 0
while offset < len(payload):
    offset += os.write(status_fd, payload[offset:])
os.close(status_fd)
exit_code = os.waitstatus_to_exitcode(wait_status)
os._exit(exit_code if exit_code >= 0 else 128 - exit_code)
"""

Termination = Literal["exited", "signalled", "timeout", "stream-limit", "resource-limit"]
ResourceLimitName = Literal[
    "cpu", "memory", "processes", "file-count", "file-bytes", "tree-inspection"
]


class ProcessContractError(ValueError):
    """An immutable process request violates the harness contract."""


class ProcessExecutionError(RuntimeError):
    """The harness could not reliably launch or collect a requested process."""


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
    """Linux aggregate-tree ceilings plus inherited per-process safety limits.

    The process owner samples aggregate CPU, resident memory, owned process count, and the writable
    tree while the command runs. ``file_bytes`` additionally sets ``RLIMIT_FSIZE`` so one hostile
    write is stopped between tree scans; ``open_files`` is inherited as ``RLIMIT_NOFILE``.
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
    _inherited_capabilities: tuple[LinuxPathCapability, ...] = field(
        default=(), repr=False, compare=False
    )
    _tree_capability: LinuxPathCapability | None = field(default=None, repr=False, compare=False)
    _tree_before_open: Callable[[str], None] | None = field(default=None, repr=False, compare=False)
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
            if self.resource_limits is not None and self.resource_limits.tree_files is not None:
                if self._tree_capability is None:
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
            return cls(
                argv=argv,
                stdin=stdin,
                cwd=cwd,
                environment=environment,
                timeout_s=timeout_s,
                stream_limit_bytes=stream_limit_bytes,
                secret_values=secret_values,
                resource_limits=resource_limits,
                _cwd_capability=cwd_capability,
                _executable_capability=executable_capability,
                _namespace_capability=namespace_capability,
                _supervisor_capability=supervisor_capability,
                _inherited_capabilities=inherited_capabilities,
                _tree_capability=tree_capability,
                _tree_before_open=tree_before_open,
            )
        except BaseException:
            if namespace_capability is not None:
                namespace_capability.close()
            if supervisor_capability is not None:
                supervisor_capability.close()
            for capability in owned:
                capability.close()
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
            *self._inherited_capabilities,
            *((self._tree_capability,) if self._tree_capability is not None else ()),
        )

    def _release(self) -> None:
        for capability in self._all_capabilities():
            capability.close()
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


def _pid_starttime(pid: int) -> int | None:
    """Return Linux's immutable-per-process start token, guarding against numeric PID reuse."""

    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    closing = raw.rfind(")")
    if closing < 0:
        return None
    fields = raw[closing + 2 :].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def _pid_process_group(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    closing = raw.rfind(")")
    if closing < 0:
        return None
    fields = raw[closing + 2 :].split()
    try:
        return int(fields[2])
    except (IndexError, ValueError):
        return None


def _pid_cpu_ticks(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    closing = raw.rfind(")")
    if closing < 0:
        return None
    fields = raw[closing + 2 :].split()
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
    identities: dict[int, int] = field(default_factory=dict)
    cpu_high_water_ticks: int = 0
    resource_limit: ResourceLimitName | None = None
    monitor_error: BaseException | None = None
    next_tree_scan: float = 0.0
    stop: threading.Event = field(default_factory=threading.Event)
    limit_reached: threading.Event = field(default_factory=threading.Event)
    monitor_failed: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def _set_resource_limit(self, name: ResourceLimitName) -> None:
        with self.lock:
            if self.resource_limit is None:
                self.resource_limit = name
                self.limit_reached.set()

    def _check_resources(self, candidates: Mapping[int, int], *, force_tree: bool) -> None:
        limits = self.resource_limits
        if limits is None or self.limit_reached.is_set():
            return
        owned = dict(candidates)
        if _pid_starttime(self.root_pid) == self.root_starttime:
            owned[self.root_pid] = self.root_starttime
        live = {pid: token for pid, token in owned.items() if _pid_starttime(pid) == token}
        if limits.processes is not None and len(live) > limits.processes:
            self._set_resource_limit("processes")
            return
        if limits.cpu_seconds is not None:
            live_ticks = sum(ticks for pid in live if (ticks := _pid_cpu_ticks(pid)) is not None)
            self.cpu_high_water_ticks = max(self.cpu_high_water_ticks, live_ticks)
            ticks_per_second = int(cast("Any", os).sysconf("SC_CLK_TCK"))
            if self.cpu_high_water_ticks >= limits.cpu_seconds * ticks_per_second:
                self._set_resource_limit("cpu")
                return
        if limits.memory_bytes is not None:
            resident = sum(value for pid in live if (value := _pid_resident_bytes(pid)) is not None)
            if resident > limits.memory_bytes:
                self._set_resource_limit("memory")
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
                self._set_resource_limit(exc.limit_name)
            except LinuxCapabilityError:
                self._set_resource_limit("tree-inspection")

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
            self.snapshot(force_tree=True)
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


@dataclass(slots=True)
class _RunningProcess:
    proc: subprocess.Popen[bytes]
    started: float
    process_group_id: int | None
    status_read_fd: int | None
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


def _close_fd(fd: int | None) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _launch_capabilities(request: ProcessRequest) -> tuple[LinuxPathCapability, ...]:
    return (
        *((request._cwd_capability,) if request._cwd_capability is not None else ()),
        *((request._executable_capability,) if request._executable_capability is not None else ()),
        *((request._namespace_capability,) if request._namespace_capability is not None else ()),
        *((request._supervisor_capability,) if request._supervisor_capability is not None else ()),
        *request._inherited_capabilities,
    )


def _linux_launch_argv(request: ProcessRequest, status_write_fd: int) -> tuple[str, ...]:
    namespace = request._namespace_capability
    supervisor = request._supervisor_capability
    target = request._executable_capability
    if namespace is None or supervisor is None or target is None:
        raise ProcessContractError("Linux process supervisor capabilities are missing")
    limits = request.resource_limits
    file_size_limit = limits.file_bytes if limits is not None else None
    open_files_limit = limits.open_files if limits is not None else None
    return (
        f"/proc/self/fd/{namespace.fd}",
        "--user",
        "--map-root-user",
        "--pid",
        "--fork",
        "--kill-child=KILL",
        "--",
        f"/proc/self/fd/{supervisor.fd}",
        "-I",
        "-c",
        _SUPERVISOR_CODE,
        str(status_write_fd),
        str(target.fd),
        str(len(request.environment)),
        str(file_size_limit if file_size_limit is not None else -1),
        str(open_files_limit if open_files_limit is not None else -1),
        *(item for pair in request.environment for item in pair),
        *request.argv,
    )


def _reap_failed_start(proc: subprocess.Popen[bytes]) -> None:
    process_group_id = proc.pid if os.name == "posix" else None
    _kill_tree(proc, process_group_id, None)
    try:
        proc.wait(timeout=_REAP_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        pass
    _close_pipe(proc.stdin)
    _close_pipe(proc.stdout)
    _close_pipe(proc.stderr)


def _start_process(request: ProcessRequest) -> _RunningProcess:
    """Spawn the outer owner and return immediately into cleanup-covered state."""

    started = time.monotonic()
    capabilities = _launch_capabilities(request)
    status_read_fd: int | None = None
    status_write_fd: int | None = None
    proc: subprocess.Popen[bytes] | None = None
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
            launch_argv = _linux_launch_argv(request, status_write_fd)
            pass_fds = (*tuple(capability.fd for capability in capabilities), status_write_fd)
            proc = subprocess.Popen(  # noqa: S603 - every executable is pinned by descriptor.
                launch_argv,
                executable=launch_argv[0],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=dict(request.environment),
                text=False,
                bufsize=0,
                start_new_session=True,
                close_fds=True,
                pass_fds=pass_fds,
                preexec_fn=preexec_fn,
            )
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
            started=started,
            process_group_id=proc.pid if os.name == "posix" else None,
            status_read_fd=status_read_fd,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if proc is not None:
            _reap_failed_start(proc)
        detail = exc.strerror if isinstance(exc, OSError) else str(exc)
        raise ProcessExecutionError(
            f"could not start process executable {request.argv[0]!r}: {detail or exc}"
        ) from exc
    except BaseException:
        if proc is not None:
            _reap_failed_start(proc)
        raise
    finally:
        _close_fd(status_write_fd)
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
    if sys.platform == "linux":
        root_starttime = _pid_starttime(proc.pid)
        if root_starttime is None:
            raise ProcessExecutionError("could not establish Linux process identity")
        runtime.tracker = _LinuxDescendantTracker(
            proc.pid,
            root_starttime,
            request.resource_limits,
            request._tree_capability,
            request._tree_before_open,
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
    try:
        while len(payload) <= _SUPERVISOR_STATUS.size:
            chunk = os.read(fd, _SUPERVISOR_STATUS.size + 1 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
    finally:
        _close_fd(fd)
    if not payload and not required:
        return
    if len(payload) != _SUPERVISOR_STATUS.size:
        raise ProcessExecutionError("Linux process supervisor returned an invalid status record")
    magic, wait_status, exec_error, cpu_microseconds = _SUPERVISOR_STATUS.unpack(payload)
    if magic != _SUPERVISOR_MAGIC:
        raise ProcessExecutionError("Linux process supervisor returned an invalid status record")
    runtime.target_status = _LinuxTargetStatus(wait_status, exec_error, cpu_microseconds)


def _cleanup_process(runtime: _RunningProcess, *, abnormal: bool) -> None:
    """Stop and reap every owned object, even when one cleanup operation itself fails."""

    errors: list[BaseException] = []
    tracked: dict[int, int] = {}
    tracker = runtime.tracker
    if tracker is not None:
        tracker.stop.set()
        if runtime.tracker_thread is not None and runtime.tracker_thread.ident is not None:
            runtime.tracker_thread.join(timeout=_REAP_TIMEOUT_S)
            if runtime.tracker_thread.is_alive():
                errors.append(ProcessExecutionError("Linux descendant monitor did not stop"))
        if tracker.monitor_failed.is_set():
            try:
                _raise_tracker_failure(tracker)
            except BaseException as exc:
                errors.append(exc)
        try:
            tracked.update(tracker.snapshot(force_tree=True))
        except BaseException as exc:
            errors.append(exc)
    if abnormal or tracked:
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
    try:
        _read_linux_target_status(runtime, required=sys.platform == "linux" and not abnormal)
    except BaseException as exc:
        errors.append(exc)
    _close_fd(runtime.status_read_fd)
    runtime.status_read_fd = None
    if errors:
        raise errors[0]


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
                tracker._set_resource_limit("cpu")
    else:
        returncode = runtime.proc.returncode
    resource_limit = tracker.resource_limit if tracker is not None else None
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
        elapsed_ms=max(0, int((time.monotonic() - runtime.started) * 1000)),
        termination=termination,
        stdout_limit_exceeded=stdout_capture.exceeded,
        stderr_limit_exceeded=stderr_capture.exceeded,
        stdout_secret_detected=stdout_secret_detected,
        stderr_secret_detected=stderr_secret_detected,
        resource_limit=resource_limit,
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
    "ProcessContractError",
    "ProcessExecutionError",
    "ProcessRequest",
    "ProcessResourceLimits",
    "ProcessResult",
    "Termination",
    "run_process",
]
