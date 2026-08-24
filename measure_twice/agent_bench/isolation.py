"""Fail-closed FD-capability Bubblewrap profiles for coding-agent benchmarks.

Every host source is acquired once as a :class:`LinuxPathCapability`.  Launches render only
``--bind-fd``/``--ro-bind-fd`` sources, execute a pinned Bubblewrap descriptor, and retain an
independent evaluator-workspace descriptor for terminal validation.  Path strings after
acquisition are diagnostic locators or sandbox destinations, never filesystem authority.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final, Literal, Self

from measure_twice.agent_bench._linux_capabilities import (
    LinuxCapabilityError,
    LinuxCapabilityUnavailableError,
    LinuxPathCapability,
    LinuxTreeLimitError,
    TreeWalkUsage,
    capabilities_overlap,
    open_verified_children,
    walk_tree,
)
from measure_twice.agent_bench.models import SANDBOX_CONTRACT_VERSION, Ceilings
from measure_twice.agent_bench.process import (
    EVALUATOR_WORKSPACE_FD_TOKEN,
    SANDBOX_SETUP_TIMEOUT_S,
    EvaluatorScratch,
    LinuxResourceGuard,
    ProcessExecutionError,
    ProcessRequest,
    ProcessResourceLimits,
)

_BWRAP_UNAVAILABLE: Final[str] = (
    "compatible Bubblewrap with behavioral --bind-fd/--ro-bind-fd support is unavailable"
)
_BWRAP_CANDIDATES: Final[tuple[str, ...]] = (
    "/usr/local/bin/bwrap",
    "/usr/bin/bwrap",
    "/bin/bwrap",
)
_TRUSTED_NETWORK_SOURCES: Final[
    tuple[tuple[tuple[str, ...], str, Literal["directory", "regular"]], ...]
] = (
    (("/etc/ssl/certs",), "/etc/ssl/certs", "directory"),
    # Default WSL resolv.conf is an absolute symlink. openat2(RESOLVE_BENEATH) correctly rejects
    # following that link from /etc, so acquire its fixed WSL-owned target directly instead. The
    # regular-file fallback supports installations with generateResolvConf=false.
    (("/etc/resolv.conf", "/mnt/wsl/resolv.conf"), "/etc/resolv.conf", "regular"),
    (("/etc/hosts",), "/etc/hosts", "regular"),
)
_SECRET_NAME_MARKERS: Final[tuple[str, ...]] = (
    "ACCESS_KEY",
    "API_KEY",
    "APIKEY",
    "AUTH",
    "COOKIE",
    "CREDENTIAL",
    "OAUTH",
    "PASSWD",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "SESSION",
    "TOKEN",
)
_AGENT_PASSTHROUGH_NAMES: Final[frozenset[str]] = frozenset({"LANG", "LC_ALL", "TZ"})
_ENVIRONMENT_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SANDBOX_TMP: Final[str] = "/tmp"  # noqa: S108 - private namespace tmpfs.
_SANDBOX_HOME: Final[str] = "/tmp/home"  # noqa: S108 - inside private tmpfs.
_BASE_ENVIRONMENT: Final[Mapping[str, str]] = MappingProxyType(
    {
        "HOME": _SANDBOX_HOME,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PWD": "/workspace",
        "TMPDIR": _SANDBOX_TMP,
    }
)
CAPTURE_GIT_ENVIRONMENT: Final[Mapping[str, str]] = MappingProxyType(
    {
        **_BASE_ENVIRONMENT,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "XDG_CONFIG_HOME": f"{_SANDBOX_HOME}/.config",
    }
)
EVALUATOR_ENVIRONMENT: Final[Mapping[str, str]] = MappingProxyType(
    {
        **_BASE_ENVIRONMENT,
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
)

ProfileName = Literal["agent", "capture", "evaluator"]
RaceHook = Callable[[str], None]


class IsolationContractError(ValueError):
    """A sandbox request would weaken or ambiguously express the fixed contract."""


class IsolationUnavailableError(RuntimeError):
    """The host cannot enforce the accepted WSL2/ext4/Bubblewrap substrate."""


class ResourceCeilingError(RuntimeError):
    """An evaluator tree crossed a file-count or aggregate-byte ceiling."""


def _environment_text(value: object, *, label: str, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        qualifier = "non-empty " if not allow_empty else ""
        raise IsolationContractError(f"{label} must be a {qualifier}string")
    if "\0" in value:
        raise IsolationContractError(f"{label} may not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise IsolationContractError(f"{label} must be UTF-8 encodable") from exc
    return value


def is_secret_environment_name(name: str) -> bool:
    """Return whether an environment name is credential-bearing by fail-closed convention."""

    upper = name.upper()
    return any(marker in upper for marker in _SECRET_NAME_MARKERS)


def allowlisted_environment(
    source: Mapping[str, str],
    *,
    allowed_names: Collection[str] = (),
    fixed: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Copy only named non-secret variables and overlay harness-owned fixed values."""

    if isinstance(allowed_names, (str, bytes)):
        raise IsolationContractError("allowed environment names must be a collection of names")
    allowed: set[str] = set()
    for name in allowed_names:
        if not isinstance(name, str) or _ENVIRONMENT_NAME_RE.fullmatch(name) is None:
            raise IsolationContractError(f"invalid allowed environment name {name!r}")
        if is_secret_environment_name(name):
            raise IsolationContractError(f"secret environment name {name!r} may not be allowed")
        allowed.add(name)
    clean: dict[str, str] = {}
    for name in allowed:
        if name in source:
            clean[name] = _environment_text(source[name], label=f"environment variable {name!r}")
    for name, value in ({} if fixed is None else fixed).items():
        if not isinstance(name, str) or _ENVIRONMENT_NAME_RE.fullmatch(name) is None:
            raise IsolationContractError(f"invalid fixed environment name {name!r}")
        if is_secret_environment_name(name):
            raise IsolationContractError(f"secret environment name {name!r} may not be fixed")
        clean[name] = _environment_text(value, label=f"fixed environment variable {name!r}")
    return tuple(sorted(clean.items()))


def secret_environment_values(source: Mapping[str, str]) -> tuple[str, ...]:
    """Extract non-empty credential values for in-memory stream redaction only."""

    values: set[str] = set()
    for name, value in source.items():
        if not isinstance(name, str):
            raise IsolationContractError("environment variable names must be strings")
        if not is_secret_environment_name(name) or not value:
            continue
        values.add(_environment_text(value, label=f"secret environment variable {name!r}"))
    return tuple(sorted(values, key=lambda value: (-len(value.encode("utf-8")), value)))


def _linux_path(value: str | os.PathLike[str], *, label: str) -> str:
    raw = os.fspath(value)
    if not isinstance(raw, str):
        raise IsolationContractError(f"{label} must be an absolute normalized Linux path")
    _environment_text(raw, label=label, allow_empty=False)
    if "\\" in raw:
        raise IsolationContractError(f"{label} must be an absolute normalized Linux path")
    path = PurePosixPath(raw)
    normalized = path.as_posix()
    components = raw.split("/")[1:]
    if (
        not path.is_absolute()
        or normalized != raw
        or (raw != "/" and any(component in {"", ".", ".."} for component in components))
    ):
        raise IsolationContractError(f"{label} must be an absolute normalized Linux path")
    if len(path.parts) >= 2 and path.parts[1] == "mnt":
        raise IsolationContractError(f"{label} may not be under /mnt/*")
    return normalized


def _sandbox_command(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)) or not argv:
        raise IsolationContractError("sandbox command must be a non-empty argv sequence")
    result = tuple(
        _environment_text(argument, label=f"sandbox command argv[{index}]", allow_empty=index != 0)
        for index, argument in enumerate(argv)
    )
    _linux_path(result[0], label="sandbox command argv[0]")
    return result


def _close_capabilities(capabilities: Sequence[LinuxPathCapability]) -> None:
    for capability in capabilities:
        capability.close()


def _duplicate_entries(
    entries: Sequence[tuple[str, LinuxPathCapability]],
) -> tuple[tuple[str, LinuxPathCapability], ...]:
    duplicated: list[tuple[str, LinuxPathCapability]] = []
    try:
        for destination, capability in entries:
            duplicated.append((destination, capability.duplicate()))
    except BaseException:
        _close_capabilities(tuple(capability for _destination, capability in duplicated))
        raise
    return tuple(duplicated)


@dataclass(slots=True)
class _PreflightOwnership:
    lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False


@dataclass(slots=True)
class LinuxIsolationPreflight:
    """Owned verified host capabilities used to construct one or more sandbox launches."""

    bwrap_version: str
    distribution: str
    distribution_version: str
    kernel_version: str
    _bwrap: LinuxPathCapability = field(repr=False)
    _runtime: tuple[tuple[str, LinuxPathCapability], ...] = field(repr=False)
    _network: tuple[tuple[str, LinuxPathCapability], ...] = field(repr=False)
    _roots: tuple[LinuxPathCapability, ...] = field(repr=False)
    _ownership: _PreflightOwnership = field(default_factory=_PreflightOwnership, repr=False)

    @property
    def bwrap_executable(self) -> str:
        return self._bwrap.display_path

    @property
    def runtime_mounts(self) -> tuple[str, ...]:
        return tuple(destination for destination, _capability in self._runtime)

    @property
    def checked_filesystems(self) -> tuple[tuple[str, str], ...]:
        return tuple((root.display_path, root.filesystem_name) for root in self._roots)

    def _require_open(self) -> None:
        with self._ownership.lock:
            if self._ownership.closed:
                raise IsolationContractError("Linux isolation preflight is closed")

    def acquire(
        self,
        value: str | os.PathLike[str],
        *,
        label: str,
    ) -> LinuxPathCapability:
        """Acquire a caller root beneath the longest matching pinned ext4 root."""

        self._require_open()
        raw = _linux_path(value, label=label)
        path = PurePosixPath(raw)
        choices: list[tuple[int, LinuxPathCapability, PurePosixPath]] = []
        for root in self._roots:
            root_path = PurePosixPath(root.display_path)
            try:
                relative = path.relative_to(root_path)
            except ValueError:
                continue
            choices.append((len(root_path.parts), root, relative))
        if not choices:
            raise IsolationContractError(f"{label} is outside every preflighted ext4 root")
        _depth, root, relative = max(choices, key=lambda value: value[0])
        try:
            capability = root.open_beneath(
                relative.as_posix(),
                expected="directory",
                display_path=raw,
            )
        except LinuxCapabilityError as exc:
            raise IsolationContractError(
                f"could not acquire {label} beneath its pinned root"
            ) from exc
        if capability.filesystem_name != "ext4":
            capability.close()
            raise IsolationContractError(f"{label} must remain on ext4")
        return capability

    def duplicate_bwrap(self) -> LinuxPathCapability:
        self._require_open()
        return self._bwrap.duplicate()

    def duplicate_runtime(self) -> tuple[tuple[str, LinuxPathCapability], ...]:
        self._require_open()
        return _duplicate_entries(self._runtime)

    def duplicate_network(self) -> tuple[tuple[str, LinuxPathCapability], ...]:
        self._require_open()
        return _duplicate_entries(self._network)

    def close(self) -> None:
        with self._ownership.lock:
            if self._ownership.closed:
                return
            self._ownership.closed = True
        _close_capabilities(
            (
                self._bwrap,
                *(capability for _destination, capability in self._runtime),
                *(capability for _destination, capability in self._network),
                *self._roots,
            )
        )

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(slots=True)
class _FdMount:
    read_only: bool
    destination: str
    capability: LinuxPathCapability = field(repr=False)


@dataclass(slots=True)
class _LaunchOwnership:
    lock: threading.Lock = field(default_factory=threading.Lock)
    consumed: bool = False
    closed: bool = False


@dataclass(slots=True)
class SandboxLaunch:
    """One-shot owner of all FD-backed sources for one Bubblewrap invocation."""

    profile: ProfileName
    network_isolated: bool
    writable_mounts: tuple[str, ...]
    read_only_mounts: tuple[str, ...]
    command: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = field(repr=False)
    _bwrap: LinuxPathCapability = field(repr=False)
    _cwd: LinuxPathCapability = field(repr=False)
    _mounts: tuple[_FdMount, ...] = field(repr=False)
    _overlap_roots: tuple[LinuxPathCapability, ...] = field(default=(), repr=False)
    resource_limits: ProcessResourceLimits | None = None
    resource_guard: LinuxResourceGuard | None = field(default=None, repr=False)
    evaluator_file_limit: int | None = None
    evaluator_bytes_limit: int | None = None
    _terminal_tree: LinuxPathCapability | None = field(default=None, repr=False)
    _evaluator_scratch: EvaluatorScratch | None = field(default=None, repr=False)
    _tree_before_open: RaceHook | None = field(default=None, repr=False)
    _ownership: _LaunchOwnership = field(default_factory=_LaunchOwnership, repr=False)

    def __post_init__(self) -> None:
        if self.profile not in {"agent", "capture", "evaluator"}:
            raise IsolationContractError(f"unknown isolation profile {self.profile!r}")
        if self.profile in {"capture", "evaluator"} and not self.network_isolated:
            raise IsolationContractError(f"{self.profile} profile must isolate the network")
        if self.profile == "evaluator" and (
            self.resource_limits is None
            or self.resource_guard is None
            or self.evaluator_file_limit is None
            or self.evaluator_bytes_limit is None
            or self._evaluator_scratch is None
        ):
            raise IsolationContractError("evaluator launch requires every resource/tree ceiling")

    def _launch_capabilities(self) -> tuple[LinuxPathCapability, ...]:
        return (
            self._bwrap,
            self._cwd,
            *(mount.capability for mount in self._mounts),
            *self._overlap_roots,
        )

    def _validate_overlap_roots(self) -> None:
        for index, first in enumerate(self._overlap_roots):
            for second in self._overlap_roots[index + 1 :]:
                if capabilities_overlap(first, second):
                    raise IsolationContractError(
                        f"{self.profile} source roots overlap at launch consumption"
                    )

    def _render_argv(
        self,
        *,
        bwrap: LinuxPathCapability,
        mounts: tuple[_FdMount, ...],
    ) -> tuple[str, ...]:
        argv = [
            f"/proc/self/fd/{bwrap.fd}",
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
        ]
        if not self.network_isolated:
            argv.append("--share-net")
        argv.extend(
            (
                "--hostname",
                f"measure-twice-{self.profile}",
                "--cap-drop",
                "ALL",
                "--clearenv",
                "--dir",
                "/usr",
                "--dir",
                "/workspace",
                "--dir",
                "/submitted",
                "--dir",
                "/opt",
                "--dir",
                "/opt/measure-twice",
                "--dir",
                "/etc",
                "--dir",
                "/etc/ssl",
            )
        )
        for mount in mounts:
            argv.extend(
                (
                    "--ro-bind-fd" if mount.read_only else "--bind-fd",
                    str(mount.capability.fd),
                    mount.destination,
                )
            )
        if self._evaluator_scratch is not None:
            argv.extend(("--bind-fd", EVALUATOR_WORKSPACE_FD_TOKEN, "/workspace"))
        argv.extend(
            (
                "--symlink",
                "usr/bin",
                "/bin",
                "--symlink",
                "usr/lib",
                "/lib",
                "--symlink",
                "usr/lib64",
                "/lib64",
                "--symlink",
                "usr/sbin",
                "/sbin",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                _SANDBOX_TMP,
                "--dir",
                _SANDBOX_HOME,
            )
        )
        for name, value in self.environment:
            argv.extend(("--setenv", name, value))
        argv.extend(("--chdir", "/workspace", "--", *self.command))
        return tuple(argv)

    def process_request(
        self,
        *,
        stdin: str,
        timeout_s: float,
        stream_limit_bytes: int,
        secret_values: Sequence[str] = (),
    ) -> ProcessRequest:
        """Consume this launch into one immutable, FD-owning process request."""

        with self._ownership.lock:
            if self._ownership.consumed or self._ownership.closed:
                raise IsolationContractError("sandbox launch is one-shot and already consumed")
            self._ownership.consumed = True
            duplicates: list[LinuxPathCapability] = []
            try:
                self._validate_overlap_roots()
                bwrap = self._bwrap.duplicate()
                duplicates.append(bwrap)
                cwd = self._cwd.reopen_directory()
                duplicates.append(cwd)
                mount_duplicates: list[_FdMount] = []
                for mount in self._mounts:
                    duplicate = mount.capability.duplicate()
                    duplicates.append(duplicate)
                    mount_duplicates.append(
                        _FdMount(
                            read_only=mount.read_only,
                            destination=mount.destination,
                            capability=duplicate,
                        )
                    )
                mounts = tuple(mount_duplicates)
                tree = (
                    self._terminal_tree.reopen_directory()
                    if self._terminal_tree is not None
                    else None
                )
                if tree is not None:
                    duplicates.append(tree)
                argv = self._render_argv(bwrap=bwrap, mounts=mounts)
                request = ProcessRequest._from_owned_capabilities(
                    argv=argv,
                    stdin=stdin,
                    cwd=Path(self._cwd.display_path),
                    environment=(),
                    timeout_s=timeout_s,
                    stream_limit_bytes=stream_limit_bytes,
                    secret_values=tuple(secret_values),
                    resource_limits=self.resource_limits,
                    resource_guard=self.resource_guard,
                    evaluator_scratch=self._evaluator_scratch,
                    cwd_capability=cwd,
                    executable_capability=bwrap,
                    inherited_capabilities=tuple(mount.capability for mount in mounts),
                    tree_capability=tree,
                    tree_before_open=self._tree_before_open,
                )
            except BaseException:
                _close_capabilities(duplicates)
                _close_capabilities(self._launch_capabilities())
                if self._terminal_tree is not None:
                    self._terminal_tree.close()
                if self._evaluator_scratch is not None:
                    self._evaluator_scratch.close()
                self._ownership.closed = True
                raise
            _close_capabilities(self._launch_capabilities())
        return request

    def terminal_tree_capability(self) -> LinuxPathCapability:
        """Borrow the retained evaluator root for immediate terminal validation."""

        with self._ownership.lock:
            if self._ownership.closed:
                raise IsolationContractError("evaluator terminal tree capability is unavailable")
            scratch = self._evaluator_scratch
            terminal = self._terminal_tree
        if scratch is not None:
            try:
                return scratch.terminal_tree_capability()
            except ProcessExecutionError as exc:
                raise IsolationContractError(
                    "evaluator terminal tree capability is unavailable"
                ) from exc
        if terminal is None:
            raise IsolationContractError("evaluator terminal tree capability is unavailable")
        _ = terminal.fd
        return terminal

    def close(self) -> None:
        with self._ownership.lock:
            if self._ownership.closed:
                return
            self._ownership.closed = True
        _close_capabilities(self._launch_capabilities())
        if self._terminal_tree is not None:
            self._terminal_tree.close()
        if self._evaluator_scratch is not None:
            self._evaluator_scratch.close()

    def __enter__(self) -> Self:
        with self._ownership.lock:
            if self._ownership.closed:
                raise IsolationContractError("sandbox launch is closed")
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _read_text(path: Path, *, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise IsolationUnavailableError(f"could not read {label}: {exc}") from exc


def _os_release() -> dict[str, str]:
    result: dict[str, str] = {}
    for line in _read_text(Path("/etc/os-release"), label="/etc/os-release").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        result[name] = value.strip().strip('"')
    return result


def resolve_bubblewrap() -> LinuxPathCapability:
    """Pin Bubblewrap from fixed trusted executable locations without PATH lookup."""

    if sys.platform != "linux":
        raise IsolationUnavailableError(_BWRAP_UNAVAILABLE)
    for candidate in _BWRAP_CANDIDATES:
        try:
            return LinuxPathCapability.acquire_absolute(
                candidate,
                expected="regular",
                allow_symlinks=True,
                executable=True,
            )
        except LinuxCapabilityError:
            continue
    raise IsolationUnavailableError(_BWRAP_UNAVAILABLE)


def _pinned_subprocess(
    executable: LinuxPathCapability,
    argv: Sequence[str],
    *,
    pass_capabilities: Sequence[LinuxPathCapability] = (),
    cwd: LinuxPathCapability | None = None,
    timeout_s: float = SANDBOX_SETUP_TIMEOUT_S,
) -> subprocess.CompletedProcess[bytes]:
    descriptors = tuple(
        dict.fromkeys(
            (
                executable.fd,
                *(capability.fd for capability in pass_capabilities),
                *((cwd.fd,) if cwd is not None else ()),
            )
        )
    )
    return subprocess.run(  # noqa: S603 - executable and every host source are pinned FDs.
        tuple(argv),
        executable=f"/proc/self/fd/{executable.fd}",
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=timeout_s,
        env={},
        cwd=f"/proc/self/fd/{cwd.fd}" if cwd is not None else "/",
        close_fds=True,
        pass_fds=descriptors,
    )


def _write_all(fd: int, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        written = os.write(fd, value[offset:])
        if written <= 0:
            raise OSError("short write while constructing Bubblewrap probe")
        offset += written


def _bubblewrap_version(bwrap: LinuxPathCapability) -> str:
    try:
        version = _pinned_subprocess(
            bwrap,
            (f"/proc/self/fd/{bwrap.fd}", "--version"),
        )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
        raise IsolationUnavailableError(_BWRAP_UNAVAILABLE) from exc
    version_text = version.stdout.decode("utf-8", errors="replace").strip()
    if version.returncode != 0 or not version_text:
        raise IsolationUnavailableError(_BWRAP_UNAVAILABLE)
    return version_text


@dataclass(slots=True)
class _BubblewrapProbeWorkspace:
    """Own and deterministically tear down every partial Bubblewrap-probe artifact."""

    root: LinuxPathCapability
    name: str = field(default_factory=lambda: f".measure-twice-bwrap-probe-{uuid.uuid4().hex}")
    probe: LinuxPathCapability | None = field(default=None, init=False)
    writable: LinuxPathCapability | None = field(default=None, init=False)
    readonly: LinuxPathCapability | None = field(default=None, init=False)
    written: LinuxPathCapability | None = field(default=None, init=False)
    sentinel_fd: int | None = field(default=None, init=False)
    probe_created: bool = field(default=False, init=False)
    writable_created: bool = field(default=False, init=False)
    readonly_created: bool = field(default=False, init=False)

    def _setup(self) -> None:
        os.mkdir(self.name, mode=0o700, dir_fd=self.root.fd)
        self.probe_created = True
        self.probe = self.root.open_beneath(
            self.name,
            expected="directory",
            display_path=f"{self.root.display_path}/{self.name}",
        )
        os.mkdir("writable", mode=0o700, dir_fd=self.probe.fd)
        self.writable_created = True
        os.mkdir("readonly", mode=0o700, dir_fd=self.probe.fd)
        self.readonly_created = True
        self.writable = self.probe.open_beneath(
            "writable",
            expected="directory",
            display_path=f"{self.probe.display_path}/writable",
        )
        self.readonly = self.probe.open_beneath(
            "readonly",
            expected="directory",
            display_path=f"{self.probe.display_path}/readonly",
        )
        self.sentinel_fd = os.open(
            "sentinel",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_CLOEXEC", 0)),
            0o600,
            dir_fd=self.readonly.fd,
        )
        _write_all(self.sentinel_fd, b"pinned")
        os.close(self.sentinel_fd)
        self.sentinel_fd = None

    def __enter__(self) -> Self:
        try:
            self._setup()
        except BaseException:
            self.close()
            raise
        return self

    def mount_capabilities(self) -> tuple[LinuxPathCapability, LinuxPathCapability]:
        if self.writable is None or self.readonly is None:
            raise RuntimeError("Bubblewrap probe workspace is not initialized")
        return (self.writable, self.readonly)

    def verify_written(self) -> None:
        if self.writable is None:
            raise RuntimeError("Bubblewrap probe workspace is not initialized")
        self.written = self.writable.open_beneath(
            "written",
            expected="regular",
            display_path=f"{self.writable.display_path}/written",
        )
        if os.read(self.written.fd, 16) != b"written":
            raise IsolationUnavailableError(_BWRAP_UNAVAILABLE)

    def close(self) -> None:
        if self.sentinel_fd is not None:
            try:
                os.close(self.sentinel_fd)
            except OSError:
                pass
            self.sentinel_fd = None
        if self.written is not None:
            self.written.close()
            self.written = None
        if self.writable is not None:
            try:
                os.unlink("written", dir_fd=self.writable.fd)
            except OSError:
                pass
            self.writable.close()
            self.writable = None
        if self.readonly is not None:
            for name in ("forbidden", "sentinel"):
                try:
                    os.unlink(name, dir_fd=self.readonly.fd)
                except OSError:
                    pass
            self.readonly.close()
            self.readonly = None
        if self.probe is not None:
            if self.writable_created:
                try:
                    os.rmdir("writable", dir_fd=self.probe.fd)
                except OSError:
                    pass
            if self.readonly_created:
                try:
                    os.rmdir("readonly", dir_fd=self.probe.fd)
                except OSError:
                    pass
            self.probe.close()
            self.probe = None
        if self.probe_created:
            try:
                os.rmdir(self.name, dir_fd=self.root.fd)
            except OSError:
                pass
            self.probe_created = False

    def __exit__(self, *_args: object) -> None:
        self.close()


def _bubblewrap_probe_argv(
    bwrap: LinuxPathCapability,
    runtime: tuple[tuple[str, LinuxPathCapability], ...],
    workspace: _BubblewrapProbeWorkspace,
) -> tuple[str, ...]:
    writable, readonly = workspace.mount_capabilities()
    argv = [
        f"/proc/self/fd/{bwrap.fd}",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--dir",
        "/usr",
        "--dir",
        "/writable",
        "--dir",
        "/readonly",
    ]
    for destination, capability in runtime:
        argv.extend(("--ro-bind-fd", str(capability.fd), destination))
    argv.extend(
        (
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib64",
            "/lib64",
            "--bind-fd",
            str(writable.fd),
            "/writable",
            "--ro-bind-fd",
            str(readonly.fd),
            "/readonly",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--",
            "/usr/bin/python3",
            "-I",
            "-c",
            (
                "import pathlib; "
                "assert pathlib.Path('/readonly/sentinel').read_bytes()==b'pinned'; "
                "pathlib.Path('/writable/written').write_bytes(b'written'); "
                "ok=False; "
                "\ntry: pathlib.Path('/readonly/forbidden').write_bytes(b'x')\n"
                "except OSError: ok=True\n"
                "raise SystemExit(0 if ok else 9)"
            ),
        )
    )
    return tuple(argv)


def _probe_bubblewrap(
    bwrap: LinuxPathCapability,
    runtime: tuple[tuple[str, LinuxPathCapability], ...],
    probe_root: LinuxPathCapability,
) -> str:
    version_text = _bubblewrap_version(bwrap)

    with _BubblewrapProbeWorkspace(probe_root) as workspace:
        writable, readonly = workspace.mount_capabilities()
        mounts = (
            *(capability for _destination, capability in runtime),
            writable,
            readonly,
        )
        try:
            result = _pinned_subprocess(
                bwrap,
                _bubblewrap_probe_argv(bwrap, runtime, workspace),
                pass_capabilities=mounts,
                cwd=probe_root,
            )
            if result.returncode != 0:
                raise IsolationUnavailableError(_BWRAP_UNAVAILABLE)
            workspace.verify_written()
        except IsolationUnavailableError:
            raise
        except (LinuxCapabilityError, OSError, subprocess.SubprocessError) as exc:
            raise IsolationUnavailableError(_BWRAP_UNAVAILABLE) from exc
    return version_text


def _acquire_trusted_network_sources() -> tuple[tuple[str, LinuxPathCapability], ...]:
    """Pin fixed WSL-owned network inputs, including the absolute resolver-symlink target."""

    acquired: list[tuple[str, LinuxPathCapability]] = []
    try:
        for candidates, destination, expected in _TRUSTED_NETWORK_SOURCES:
            capability: LinuxPathCapability | None = None
            for source in candidates:
                try:
                    capability = LinuxPathCapability.acquire_absolute(
                        source,
                        expected=expected,
                        allow_symlinks=True,
                    )
                except LinuxCapabilityError:
                    continue
                break
            if capability is None:
                raise IsolationUnavailableError(
                    f"required trusted network source is unavailable: {destination!r}"
                )
            acquired.append((destination, capability))
    except BaseException:
        _close_capabilities(tuple(capability for _destination, capability in acquired))
        raise
    return tuple(acquired)


def preflight_linux_isolation(paths: Sequence[Path]) -> LinuxIsolationPreflight:
    """Pin WSL2 Ubuntu 24.04/ext4 roots and behavior-probe FD-capable Bubblewrap."""

    if isinstance(paths, (str, bytes)) or not paths:
        raise IsolationUnavailableError("preflight requires at least one untrusted path")
    for path in paths:
        try:
            raw = os.fspath(path).replace("\\", "/")
        except TypeError as exc:
            raise IsolationUnavailableError("untrusted paths must be pathlib paths") from exc
        if raw == "/mnt" or raw.startswith("/mnt/"):
            raise IsolationUnavailableError(f"untrusted path may not be under /mnt/*: {raw!r}")
    if sys.platform != "linux":
        raise IsolationUnavailableError("live agent isolation requires Linux under WSL2")
    kernel = _read_text(Path("/proc/sys/kernel/osrelease"), label="kernel release").strip()
    low_kernel = kernel.lower()
    if "microsoft" not in low_kernel or "wsl2" not in low_kernel:
        raise IsolationUnavailableError("live agent isolation requires WSL2")
    release = _os_release()
    if release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "24.04":
        raise IsolationUnavailableError("live agent isolation requires Ubuntu 24.04 under WSL2")

    owned: list[LinuxPathCapability] = []
    try:
        root_entries: list[LinuxPathCapability] = []
        for path in paths:
            root = LinuxPathCapability.acquire_absolute(path, expected="directory")
            owned.append(root)
            root_entries.append(root)
        roots = tuple(root_entries)
        if any(root.filesystem_name != "ext4" for root in roots):
            raise IsolationUnavailableError("every untrusted path must use ext4")
        usr = LinuxPathCapability.acquire_absolute(
            "/usr", expected="directory", allow_symlinks=True
        )
        owned.append(usr)
        runtime = (("/usr", usr),)
        network_entries = _acquire_trusted_network_sources()
        owned.extend(capability for _destination, capability in network_entries)
        bwrap = resolve_bubblewrap()
        owned.append(bwrap)
        version = _probe_bubblewrap(bwrap, runtime, roots[0])
        preflight = LinuxIsolationPreflight(
            bwrap_version=version,
            distribution=release["ID"],
            distribution_version=release["VERSION_ID"],
            kernel_version=kernel,
            _bwrap=bwrap,
            _runtime=runtime,
            _network=tuple(network_entries),
            _roots=roots,
        )
    except (LinuxCapabilityError, LinuxCapabilityUnavailableError) as exc:
        _close_capabilities(owned)
        raise IsolationUnavailableError(str(exc)) from exc
    except BaseException:
        _close_capabilities(owned)
        raise
    return preflight


def _base_mounts(
    preflight: LinuxIsolationPreflight,
    *,
    include_network: bool,
) -> list[_FdMount]:
    mounts: list[_FdMount] = []
    try:
        mounts.extend(
            _FdMount(read_only=True, destination=destination, capability=capability)
            for destination, capability in preflight.duplicate_runtime()
        )
        if include_network:
            mounts.extend(
                _FdMount(read_only=True, destination=destination, capability=capability)
                for destination, capability in preflight.duplicate_network()
            )
    except BaseException:
        _close_capabilities(tuple(mount.capability for mount in mounts))
        raise
    return mounts


def _call_hook(hook: RaceHook | None, label: str) -> None:
    if hook is not None:
        hook(label)


def _new_launch(
    *,
    profile: ProfileName,
    preflight: LinuxIsolationPreflight,
    network_isolated: bool,
    command: tuple[str, ...],
    environment: tuple[tuple[str, str], ...],
    mounts: list[_FdMount],
    cwd_source: LinuxPathCapability,
    terminal_tree: LinuxPathCapability | None = None,
    resource_limits: ProcessResourceLimits | None = None,
    resource_guard: LinuxResourceGuard | None = None,
    evaluator_file_limit: int | None = None,
    evaluator_bytes_limit: int | None = None,
    evaluator_scratch: EvaluatorScratch | None = None,
    writable_mounts: Sequence[str] | None = None,
    tree_before_open: RaceHook | None = None,
    overlap_sources: Sequence[LinuxPathCapability] = (),
) -> SandboxLaunch:
    bwrap: LinuxPathCapability | None = None
    cwd: LinuxPathCapability | None = None
    overlap_roots: list[LinuxPathCapability] = []
    try:
        bwrap = preflight.duplicate_bwrap()
        cwd = cwd_source.reopen_directory()
        for source in overlap_sources:
            overlap_roots.append(source.reopen_directory())
        return SandboxLaunch(
            profile=profile,
            network_isolated=network_isolated,
            writable_mounts=(
                tuple(mount.destination for mount in mounts if not mount.read_only)
                if writable_mounts is None
                else tuple(writable_mounts)
            ),
            read_only_mounts=tuple(mount.destination for mount in mounts if mount.read_only),
            command=command,
            environment=environment,
            resource_limits=resource_limits,
            resource_guard=resource_guard,
            evaluator_file_limit=evaluator_file_limit,
            evaluator_bytes_limit=evaluator_bytes_limit,
            _bwrap=bwrap,
            _cwd=cwd,
            _mounts=tuple(mounts),
            _overlap_roots=tuple(overlap_roots),
            _terminal_tree=terminal_tree,
            _evaluator_scratch=evaluator_scratch,
            _tree_before_open=tree_before_open,
        )
    except BaseException:
        if bwrap is not None:
            bwrap.close()
        if cwd is not None:
            cwd.close()
        _close_capabilities(overlap_roots)
        _close_capabilities(tuple(mount.capability for mount in mounts))
        if terminal_tree is not None:
            terminal_tree.close()
        if evaluator_scratch is not None:
            evaluator_scratch.close()
        raise


def build_agent_sandbox(
    preflight: LinuxIsolationPreflight,
    *,
    workspace: str | os.PathLike[str],
    command: Sequence[str],
    source_environment: Mapping[str, str] | None = None,
    allowed_environment_names: Collection[str] = ("LANG", "LC_ALL", "TZ"),
    _race_hook: RaceHook | None = None,
) -> SandboxLaunch:
    """Build the Step-26 provider-control-plane mechanics with one writable workspace.

    Step 28/29 production wiring owns provider-command qualification, provider-native child-network
    denial, and allocation of a workspace distinct from operator-home, project, and run-store roots.
    """

    requested_names = set(allowed_environment_names)
    if requested_names - _AGENT_PASSTHROUGH_NAMES:
        raise IsolationContractError(
            "agent environment passthrough is restricted to LANG, LC_ALL, and TZ"
        )
    sandbox_command = _sandbox_command(command)
    environment = allowlisted_environment(
        {} if source_environment is None else source_environment,
        allowed_names=requested_names,
        fixed=_BASE_ENVIRONMENT,
    )
    workspace_capability = preflight.acquire(workspace, label="agent workspace")
    try:
        _call_hook(_race_hook, "agent-workspace")
        mounts = _base_mounts(preflight, include_network=True)
        mounts.append(
            _FdMount(read_only=False, destination="/workspace", capability=workspace_capability)
        )
        return _new_launch(
            profile="agent",
            preflight=preflight,
            network_isolated=False,
            command=sandbox_command,
            environment=environment,
            mounts=mounts,
            cwd_source=workspace_capability,
        )
    except BaseException:
        workspace_capability.close()
        raise


def build_capture_sandbox(
    preflight: LinuxIsolationPreflight,
    *,
    submitted_tree: str | os.PathLike[str],
    capture_repository: str | os.PathLike[str],
    command: Sequence[str],
    _race_hook: RaceHook | None = None,
) -> SandboxLaunch:
    """Build no-network capture with verified submitted children and no root ``.git``."""

    sandbox_command = _sandbox_command(command)
    submitted = preflight.acquire(submitted_tree, label="submitted tree")
    repository: LinuxPathCapability | None = None
    children: tuple[tuple[str, LinuxPathCapability], ...] = ()
    try:
        _call_hook(_race_hook, "capture-submitted")
        repository = preflight.acquire(capture_repository, label="capture repository")
        _call_hook(_race_hook, "capture-repository")
        if capabilities_overlap(submitted, repository):
            raise IsolationContractError("capture repository and submitted tree may not overlap")
        children = open_verified_children(
            submitted,
            omit_names=frozenset({".git"}),
            before_open=(
                (lambda name: _race_hook(f"capture-child:{name}"))
                if _race_hook is not None
                else None
            ),
        )
        mounts = _base_mounts(preflight, include_network=False)
        mounts.extend(
            _FdMount(
                read_only=True,
                destination=(PurePosixPath("/submitted") / name).as_posix(),
                capability=capability,
            )
            for name, capability in children
        )
        mounts.append(_FdMount(read_only=False, destination="/workspace", capability=repository))
        return _new_launch(
            profile="capture",
            preflight=preflight,
            network_isolated=True,
            command=sandbox_command,
            environment=allowlisted_environment({}, fixed=CAPTURE_GIT_ENVIRONMENT),
            mounts=mounts,
            cwd_source=repository,
            overlap_sources=(submitted, repository),
        )
    except BaseException:
        if repository is not None:
            repository.close()
        _close_capabilities(tuple(capability for _name, capability in children))
        raise
    finally:
        submitted.close()


def build_evaluator_sandbox(
    preflight: LinuxIsolationPreflight,
    *,
    workspace: str | os.PathLike[str],
    oracle: str | os.PathLike[str],
    runtime: str | os.PathLike[str],
    command: Sequence[str],
    ceilings: Ceilings,
    _race_hook: RaceHook | None = None,
    _tree_before_open: RaceHook | None = None,
) -> SandboxLaunch:
    """Build no-network evaluator with aggregate owned-tree ceilings."""

    if not isinstance(ceilings, Ceilings):
        raise IsolationContractError("evaluator ceilings must be a Ceilings instance")
    sandbox_command = _sandbox_command(command)
    acquired: list[LinuxPathCapability] = []
    evaluator_scratch: EvaluatorScratch | None = None
    try:
        workspace_capability = preflight.acquire(workspace, label="evaluator workspace")
        acquired.append(workspace_capability)
        _call_hook(_race_hook, "evaluator-workspace")
        oracle_capability = preflight.acquire(oracle, label="evaluator oracle")
        acquired.append(oracle_capability)
        _call_hook(_race_hook, "evaluator-oracle")
        runtime_capability = preflight.acquire(runtime, label="evaluator runtime")
        acquired.append(runtime_capability)
        _call_hook(_race_hook, "evaluator-runtime")
        for index, first in enumerate(acquired):
            for second in acquired[index + 1 :]:
                if capabilities_overlap(first, second):
                    raise IsolationContractError(
                        "evaluator workspace, oracle, and runtime may not overlap"
                    )
        evaluator_scratch = EvaluatorScratch(
            source=workspace_capability.reopen_directory(),
            file_limit=ceilings.evaluator_files,
            byte_limit=ceilings.evaluator_file_bytes,
            tmpfs_bytes=ceilings.evaluator_tmpfs_bytes,
            tmpfs_inodes=ceilings.evaluator_tmpfs_inodes,
        )
        limits = ProcessResourceLimits(
            cpu_seconds=ceilings.evaluator_cpu_s,
            memory_bytes=ceilings.evaluator_memory_bytes,
            processes=ceilings.evaluator_processes,
            file_bytes=ceilings.evaluator_file_bytes,
            open_files=max(32, ceilings.evaluator_files),
            tree_files=ceilings.evaluator_files,
            tree_bytes=ceilings.evaluator_file_bytes,
        )
        guard = LinuxResourceGuard(
            memory_bytes=ceilings.evaluator_memory_bytes,
            processes=ceilings.evaluator_processes,
            cpu_bandwidth_percent=ceilings.evaluator_cpu_bandwidth_percent,
        )
        mounts = _base_mounts(preflight, include_network=False)
        mounts.extend(
            (
                _FdMount(
                    read_only=True,
                    destination="/opt/measure-twice/oracle",
                    capability=oracle_capability,
                ),
                _FdMount(
                    read_only=True,
                    destination="/opt/measure-twice/runtime",
                    capability=runtime_capability,
                ),
            )
        )
        launch = _new_launch(
            profile="evaluator",
            preflight=preflight,
            network_isolated=True,
            command=sandbox_command,
            environment=allowlisted_environment({}, fixed=EVALUATOR_ENVIRONMENT),
            mounts=mounts,
            cwd_source=workspace_capability,
            resource_limits=limits,
            resource_guard=guard,
            evaluator_file_limit=ceilings.evaluator_files,
            evaluator_bytes_limit=ceilings.evaluator_file_bytes,
            evaluator_scratch=evaluator_scratch,
            writable_mounts=("/workspace",),
            tree_before_open=_tree_before_open,
            overlap_sources=(workspace_capability, oracle_capability, runtime_capability),
        )
        # The launch owns reopened cwd/overlap roots and EvaluatorScratch owns its reopened seed;
        # unlike oracle/runtime, the original workspace capability is not itself a mount.
        workspace_capability.close()
        return launch
    except BaseException:
        _close_capabilities(acquired)
        if evaluator_scratch is not None:
            evaluator_scratch.close()
        raise


_CAPTURE_GIT_CONFIG: Final[tuple[str, ...]] = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.attributesFile=/dev/null",
    "-c",
    "core.fsmonitor=false",
)


def _trusted_git_executable(executable: str | os.PathLike[str]) -> str:
    value = _linux_path(executable, label="capture Git executable")
    if not (value.startswith("/usr/bin/") or value.startswith("/bin/")):
        raise IsolationContractError("capture Git executable must come from pinned /usr runtime")
    return value


def capture_git_add_argv(executable: str | os.PathLike[str] = "/usr/bin/git") -> tuple[str, ...]:
    """Return the locked force-add command used by authoritative capture."""

    return (
        _trusted_git_executable(executable),
        *_CAPTURE_GIT_CONFIG,
        "add",
        "-A",
        "-f",
        "--",
        ".",
    )


def capture_git_diff_argv(executable: str | os.PathLike[str] = "/usr/bin/git") -> tuple[str, ...]:
    """Return the locked full binary diff command used by authoritative capture."""

    return (
        _trusted_git_executable(executable),
        *_CAPTURE_GIT_CONFIG,
        "diff",
        "--cached",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
    )


@dataclass(frozen=True, slots=True)
class TreeUsage:
    file_count: int
    size_bytes: int


def measure_tree_usage(
    root: LinuxPathCapability,
    *,
    _before_open: RaceHook | None = None,
) -> TreeUsage:
    """Measure a held evaluator tree through the shared FD-relative walker."""

    try:
        usage = walk_tree(root, before_open=_before_open)
    except LinuxCapabilityError as exc:
        raise ResourceCeilingError(f"evaluator tree inspection failed closed: {exc}") from exc
    return TreeUsage(file_count=usage.file_count, size_bytes=usage.size_bytes)


def enforce_evaluator_tree_ceiling(launch: SandboxLaunch) -> TreeUsage:
    """Fail when a retained evaluator tree exceeds its pinned aggregate ceilings."""

    if launch.profile != "evaluator":
        raise IsolationContractError("tree ceilings may only be enforced for an evaluator profile")
    if launch.evaluator_file_limit is None or launch.evaluator_bytes_limit is None:
        raise IsolationContractError("evaluator profile is missing tree ceilings")
    capability = launch.terminal_tree_capability()
    try:
        usage: TreeWalkUsage = walk_tree(
            capability,
            file_limit=launch.evaluator_file_limit,
            byte_limit=launch.evaluator_bytes_limit,
            before_open=launch._tree_before_open,
        )
    except LinuxTreeLimitError as exc:
        label = "file" if exc.limit_name == "file-count" else "byte"
        raise ResourceCeilingError(f"evaluator {label} ceiling exceeded") from exc
    except LinuxCapabilityError as exc:
        raise ResourceCeilingError(f"evaluator tree inspection failed closed: {exc}") from exc
    return TreeUsage(file_count=usage.file_count, size_bytes=usage.size_bytes)


__all__ = [
    "CAPTURE_GIT_ENVIRONMENT",
    "EVALUATOR_ENVIRONMENT",
    "SANDBOX_CONTRACT_VERSION",
    "IsolationContractError",
    "IsolationUnavailableError",
    "LinuxIsolationPreflight",
    "ResourceCeilingError",
    "SandboxLaunch",
    "TreeUsage",
    "allowlisted_environment",
    "build_agent_sandbox",
    "build_capture_sandbox",
    "build_evaluator_sandbox",
    "capture_git_add_argv",
    "capture_git_diff_argv",
    "enforce_evaluator_tree_ceiling",
    "is_secret_environment_name",
    "measure_tree_usage",
    "preflight_linux_isolation",
    "resolve_bubblewrap",
    "secret_environment_values",
]
