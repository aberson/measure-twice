"""Owned Linux descriptor capabilities and one race-resistant tree walker.

Path text is accepted only while acquiring a capability.  Every later security-sensitive use is
descriptor-relative: Linux ``openat2`` prevents symlink/magic-link traversal, identity comes from
``fstat``, ancestry walks ``..`` from held directory descriptors, and the shared tree walker queues
directory descriptors rather than names.  The module is stdlib-only and fails closed off Linux or
when the required kernel syscall is unavailable.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import platform
import stat
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Final, Literal, Self, SupportsIndex, cast

_OPENAT2_SYSCALLS: Final[dict[str, int]] = {
    "aarch64": 437,
    "arm64": 437,
    "riscv64": 437,
    "x86_64": 437,
    "amd64": 437,
}
_RESOLVE_NO_MAGICLINKS: Final[int] = 0x02
_RESOLVE_NO_SYMLINKS: Final[int] = 0x04
_RESOLVE_BENEATH: Final[int] = 0x08
_EXT4_SUPER_MAGIC: Final[int] = 0xEF53
_MAX_ANCESTRY_DEPTH: Final[int] = 4096
_MAX_TREE_DIRECTORIES: Final[int] = 10_000
_MAX_DIRECTORY_ENTRIES: Final[int] = 10_000
_COPY_CHUNK_BYTES: Final[int] = 1024 * 1024

_O_CLOEXEC: Final[int] = int(getattr(os, "O_CLOEXEC", 0x80000))
_O_DIRECTORY: Final[int] = int(getattr(os, "O_DIRECTORY", 0x10000))
_O_NOFOLLOW: Final[int] = int(getattr(os, "O_NOFOLLOW", 0x20000))
_O_NONBLOCK: Final[int] = int(getattr(os, "O_NONBLOCK", 0x800))

CapabilityKind = Literal["directory", "regular", "any"]
TreeLimitName = Literal["file-count", "file-bytes"]
TreePolicyViolation = Literal[
    "invalid-name", "special-file", "structural-shape", "symlink", "unreadable"
]


class LinuxCapabilityError(RuntimeError):
    """An FD-backed filesystem operation could not preserve the identity contract."""


class LinuxCapabilityUnavailableError(LinuxCapabilityError):
    """The host lacks the Linux kernel semantics required by the contract."""


class LinuxTreeLimitError(LinuxCapabilityError):
    """An FD-relative tree walk crossed a configured aggregate ceiling."""

    def __init__(self, limit_name: TreeLimitName, file_count: int, size_bytes: int) -> None:
        super().__init__(f"{limit_name} ceiling crossed during FD-relative tree walk")
        self.limit_name = limit_name
        self.file_count = file_count
        self.size_bytes = size_bytes


class LinuxTreeDriftError(LinuxCapabilityError):
    """A held source tree changed during one descriptor-relative traversal.

    A live evaluator sample can discard this inconclusive observation and retry later.  Copy and
    post-cleanup terminal validation deliberately remain strict callers of the same primitive.
    """


class LinuxTreePolicyError(LinuxCapabilityError):
    """A stable tree entry violates evaluator result-tree policy.

    This is distinct from an I/O/capability failure and from concurrent namespace drift: a
    terminal evaluator result containing a symlink, special/unreadable entry, or structural
    policy violation is attributable to the submitted model output.
    """

    def __init__(
        self,
        message: str,
        violation: TreePolicyViolation,
        *,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.violation = violation
        self.path = path


class _OpenAt2Error(LinuxCapabilityError):
    """Private errno-preserving openat2 failure used only for traversal classification."""

    def __init__(self, error_number: int) -> None:
        super().__init__(f"could not acquire FD capability with openat2: errno {error_number}")
        self.error_number = error_number


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


class _StatFs(ctypes.Structure):
    _fields_ = [
        ("f_type", ctypes.c_long),
        ("f_bsize", ctypes.c_long),
        ("f_blocks", ctypes.c_ulonglong),
        ("f_bfree", ctypes.c_ulonglong),
        ("f_bavail", ctypes.c_ulonglong),
        ("f_files", ctypes.c_ulonglong),
        ("f_ffree", ctypes.c_ulonglong),
        ("f_fsid", ctypes.c_int * 2),
        ("f_namelen", ctypes.c_long),
        ("f_frsize", ctypes.c_long),
        ("f_flags", ctypes.c_long),
        ("f_spare", ctypes.c_long * 4),
    ]


def _require_linux() -> None:
    if sys.platform != "linux":
        raise LinuxCapabilityUnavailableError("Linux FD capabilities require a Linux host")
    if platform.machine().lower() not in _OPENAT2_SYSCALLS:
        raise LinuxCapabilityUnavailableError("openat2 syscall number is unsupported on this CPU")


def _path_text(value: str | os.PathLike[str], *, label: str) -> str:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\0" in raw or "\\" in raw:
        raise LinuxCapabilityError(f"{label} must be an absolute normalized Linux path")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LinuxCapabilityError(f"{label} must be UTF-8 encodable") from exc
    path = PurePosixPath(raw)
    components = raw.split("/")[1:]
    if (
        not path.is_absolute()
        or path.as_posix() != raw
        or (raw != "/" and any(component in {"", ".", ".."} for component in components))
    ):
        raise LinuxCapabilityError(f"{label} must be an absolute normalized Linux path")
    return path.as_posix()


def _component_text(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise LinuxCapabilityError(f"{label} must be one non-empty path component")
    if "/" in value or "\\" in value or "\0" in value:
        raise LinuxCapabilityError(f"{label} must be one non-empty path component")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LinuxCapabilityError(f"{label} must be UTF-8 encodable") from exc
    return value


def _openat2(
    parent_fd: int,
    relative: str,
    *,
    flags: int,
    allow_symlinks: bool,
) -> int:
    _require_linux()
    how = _OpenHow(
        flags=flags,
        mode=0,
        resolve=(
            _RESOLVE_BENEATH
            | _RESOLVE_NO_MAGICLINKS
            | (0 if allow_symlinks else _RESOLVE_NO_SYMLINKS)
        ),
    )
    libc = cast("Any", ctypes.CDLL(None, use_errno=True))
    libc.syscall.restype = ctypes.c_long
    encoded = relative.encode("utf-8")
    syscall_number = _OPENAT2_SYSCALLS[platform.machine().lower()]
    result = int(
        libc.syscall(
            ctypes.c_long(syscall_number),
            ctypes.c_int(parent_fd),
            ctypes.c_char_p(encoded),
            ctypes.byref(how),
            ctypes.c_size_t(ctypes.sizeof(how)),
        )
    )
    if result >= 0:
        return result
    error_number = ctypes.get_errno()
    if error_number in {errno.ENOSYS, errno.E2BIG, errno.EINVAL}:
        raise LinuxCapabilityUnavailableError(
            "Linux openat2 beneath/no-symlink semantics are unavailable"
        )
    raise _OpenAt2Error(error_number)


def _filesystem_magic(fd: int) -> int:
    if ctypes.sizeof(ctypes.c_long) != 8 or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise LinuxCapabilityUnavailableError("fstatfs capability evidence requires Linux LP64")
    value = _StatFs()
    libc = cast("Any", ctypes.CDLL(None, use_errno=True))
    fstatfs = libc.fstatfs
    fstatfs.argtypes = [ctypes.c_int, ctypes.POINTER(_StatFs)]
    fstatfs.restype = ctypes.c_int
    if int(fstatfs(fd, ctypes.byref(value))) != 0:
        error_number = ctypes.get_errno()
        raise LinuxCapabilityError(f"could not inspect capability filesystem: errno {error_number}")
    return int(value.f_type) & 0xFFFFFFFFFFFFFFFF


def _kind_matches(mode: int, expected: CapabilityKind) -> bool:
    if expected == "directory":
        return stat.S_ISDIR(mode)
    if expected == "regular":
        return stat.S_ISREG(mode)
    return True


class LinuxPathCapability:
    """Exclusive ownership of one already-opened Linux filesystem object."""

    __slots__ = (
        "_closed",
        "_exclusive_copy_destination",
        "_fd",
        "_lock",
        "display_path",
        "filesystem_magic",
        "st_dev",
        "st_ino",
        "st_mode",
    )

    def __init__(
        self,
        fd: int,
        *,
        display_path: str,
        exclusive_copy_destination: bool = False,
    ) -> None:
        _require_linux()
        try:
            # SCM_RIGHTS intentionally transfers a fresh descriptor without preserving the
            # sender's FD_CLOEXEC bit.  Capabilities can arrive over that boundary, so make
            # every owned descriptor non-inheritable before inspecting or retaining it.
            os.set_inheritable(fd, False)
            metadata = os.fstat(fd)
            filesystem_magic = _filesystem_magic(fd)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        self._closed = False
        self._exclusive_copy_destination = exclusive_copy_destination
        self._lock = threading.Lock()
        self.display_path = display_path
        self.st_dev = metadata.st_dev
        self.st_ino = metadata.st_ino
        self.st_mode = metadata.st_mode
        self.filesystem_magic = filesystem_magic

    @classmethod
    def acquire_absolute(
        cls,
        path: str | os.PathLike[str],
        *,
        expected: CapabilityKind,
        allow_symlinks: bool = False,
        executable: bool = False,
    ) -> Self:
        """Acquire an absolute path beneath a pinned ``/`` descriptor with ``openat2``."""

        display = _path_text(path, label="capability path")
        if allow_symlinks and display != "/":
            parsed = PurePosixPath(display)
            parent = cls.acquire_absolute(parsed.parent.as_posix(), expected="directory")
            try:
                flags = os.O_RDONLY | _O_CLOEXEC | _O_NONBLOCK
                if expected == "directory":
                    flags |= _O_DIRECTORY
                fd = _openat2(
                    parent.fd,
                    parsed.name,
                    flags=flags,
                    allow_symlinks=True,
                )
            finally:
                parent.close()
            return cls._from_open_fd(
                fd,
                display_path=display,
                expected=expected,
                executable=executable,
            )
        root_fd = os.open("/", os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
        try:
            relative = display.removeprefix("/")
            if not relative:
                fd = os.dup(root_fd)
            else:
                flags = os.O_RDONLY | _O_CLOEXEC | _O_NONBLOCK
                if not allow_symlinks:
                    flags |= _O_NOFOLLOW
                if expected == "directory":
                    flags |= _O_DIRECTORY
                fd = _openat2(
                    root_fd,
                    relative,
                    flags=flags,
                    allow_symlinks=allow_symlinks,
                )
        finally:
            os.close(root_fd)
        return cls._from_open_fd(
            fd,
            display_path=display,
            expected=expected,
            executable=executable,
        )

    @classmethod
    def _from_open_fd(
        cls,
        fd: int,
        *,
        display_path: str,
        expected: CapabilityKind,
        executable: bool = False,
        exclusive_copy_destination: bool = False,
    ) -> Self:
        capability = cls(
            fd,
            display_path=display_path,
            exclusive_copy_destination=exclusive_copy_destination,
        )
        if not _kind_matches(capability.st_mode, expected):
            capability.close()
            raise LinuxCapabilityError(f"capability object has the wrong type for {display_path!r}")
        if executable and not capability.st_mode & 0o111:
            capability.close()
            raise LinuxCapabilityError(f"capability object is not executable: {display_path!r}")
        return capability

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def fd(self) -> int:
        with self._lock:
            if self._closed:
                raise LinuxCapabilityError("Linux path capability is closed")
            return self._fd

    @property
    def identity(self) -> tuple[int, int]:
        return (self.st_dev, self.st_ino)

    @property
    def filesystem_name(self) -> str:
        return "ext4" if self.filesystem_magic == _EXT4_SUPER_MAGIC else hex(self.filesystem_magic)

    def _mark_exclusive_copy_destination(self) -> None:
        """Mark a validated release-barrier-private directory as a copy destination.

        This internal capability bit is minted only by the evaluator tmpfs validation path.  It is
        propagated through duplicate/reopen/child operations so every directory created below the
        private root retains the same structural non-concurrency invariant.
        """

        if not stat.S_ISDIR(self.st_mode):
            raise LinuxCapabilityError("only a directory can be an exclusive copy destination")
        with self._lock:
            if self._closed:
                raise LinuxCapabilityError("Linux path capability is closed")
            self._exclusive_copy_destination = True

    def duplicate(self) -> Self:
        """Create an explicit independently-owned duplicate of the same kernel object."""

        with self._lock:
            if self._closed:
                raise LinuxCapabilityError("Linux path capability is closed")
            duplicated_fd = os.dup(self._fd)
        return type(self)._from_open_fd(
            duplicated_fd,
            display_path=self.display_path,
            expected=("directory" if stat.S_ISDIR(self.st_mode) else "regular"),
            exclusive_copy_destination=self._exclusive_copy_destination,
        )

    def reopen_directory(self) -> Self:
        """Open ``.`` as a fresh directory description without sharing a scan offset."""

        if not stat.S_ISDIR(self.st_mode):
            raise LinuxCapabilityError("only a directory capability can be reopened")
        flags = os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC | _O_NOFOLLOW | _O_NONBLOCK
        with self._lock:
            if self._closed:
                raise LinuxCapabilityError("Linux path capability is closed")
            fd = _openat2(self._fd, ".", flags=flags, allow_symlinks=False)
        reopened = type(self)._from_open_fd(
            fd,
            display_path=self.display_path,
            expected="directory",
            exclusive_copy_destination=self._exclusive_copy_destination,
        )
        if reopened.identity != self.identity:
            reopened.close()
            raise LinuxCapabilityError("reopened directory capability changed identity")
        return reopened

    def open_beneath(
        self,
        relative: str,
        *,
        expected: CapabilityKind,
        display_path: str,
    ) -> Self:
        """Open a relative descendant without following symlinks or magic links."""

        if relative in {"", "."}:
            if not _kind_matches(self.st_mode, expected):
                raise LinuxCapabilityError("capability root has the wrong requested type")
            return self.reopen_directory() if expected == "directory" else self.duplicate()
        relative_path = PurePosixPath(relative)
        if (
            relative_path.is_absolute()
            or relative_path.as_posix() != relative
            or any(component in {"", ".", ".."} for component in relative.split("/"))
        ):
            raise LinuxCapabilityError("relative capability path must remain beneath its root")
        flags = os.O_RDONLY | _O_CLOEXEC | _O_NOFOLLOW | _O_NONBLOCK
        if expected == "directory":
            flags |= _O_DIRECTORY
        with self._lock:
            if self._closed:
                raise LinuxCapabilityError("Linux path capability is closed")
            fd = _openat2(
                self._fd,
                relative_path.as_posix(),
                flags=flags,
                allow_symlinks=False,
            )
        return type(self)._from_open_fd(
            fd,
            display_path=display_path,
            expected=expected,
            exclusive_copy_destination=self._exclusive_copy_destination,
        )

    def open_parent(self) -> Self:
        """Open the held directory's current ``..`` object under the ownership lock."""

        if not stat.S_ISDIR(self.st_mode):
            raise LinuxCapabilityError("only a directory capability has a parent")
        with self._lock:
            if self._closed:
                raise LinuxCapabilityError("Linux path capability is closed")
            fd = os.open(
                "..",
                os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC | _O_NOFOLLOW,
                dir_fd=self._fd,
            )
        return type(self)._from_open_fd(
            fd,
            display_path=f"{self.display_path}/..",
            expected="directory",
        )

    def close(self) -> None:
        """Idempotently release the owned descriptor."""

        with self._lock:
            if self._closed:
                return
            fd = self._fd
            self._closed = True
            self._fd = -1
        os.close(fd)

    def __enter__(self) -> Self:
        _ = self.fd
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __copy__(self) -> Self:
        raise TypeError("LinuxPathCapability is non-copyable; use duplicate() explicitly")

    def __deepcopy__(self, _memo: dict[int, object]) -> Self:
        raise TypeError("LinuxPathCapability is non-copyable; use duplicate() explicitly")

    def __reduce__(self) -> tuple[Any, ...]:
        raise TypeError("LinuxPathCapability is non-serializable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> tuple[Any, ...]:
        raise TypeError("LinuxPathCapability is non-serializable")

    def __getstate__(self) -> dict[str, object]:
        raise TypeError("LinuxPathCapability is non-serializable")

    def __repr__(self) -> str:
        state = "closed" if self.closed else "open"
        return (
            f"LinuxPathCapability(display_path={self.display_path!r}, "
            f"identity={self.identity!r}, state={state!r})"
        )


def is_ancestor(ancestor: LinuxPathCapability, descendant: LinuxPathCapability) -> bool:
    """Compare held objects while walking descriptor-relative parents of ``descendant``."""

    if not stat.S_ISDIR(ancestor.st_mode) or not stat.S_ISDIR(descendant.st_mode):
        raise LinuxCapabilityError("capability ancestry requires directory objects")
    current = descendant.duplicate()
    try:
        for _ in range(_MAX_ANCESTRY_DEPTH):
            if current.identity == ancestor.identity:
                return True
            parent = current.open_parent()
            if parent.identity == current.identity:
                parent.close()
                return False
            current.close()
            current = parent
    finally:
        current.close()
    raise LinuxCapabilityError("capability ancestry exceeded the structural depth bound")


def capabilities_overlap(first: LinuxPathCapability, second: LinuxPathCapability) -> bool:
    """Return whether either held directory object is an ancestor of the other."""

    return is_ancestor(first, second) or is_ancestor(second, first)


@dataclass(frozen=True, slots=True)
class TreeWalkUsage:
    file_count: int
    size_bytes: int
    directory_count: int


@dataclass(frozen=True, slots=True)
class _EnumeratedEntry:
    name: str
    st_dev: int
    st_ino: int
    st_mode: int
    st_size: int
    st_mtime_ns: int
    st_ctime_ns: int
    st_nlink: int


@dataclass(frozen=True, slots=True)
class _CopiedEntry:
    """One destination identity retained until the complete copy has been verified."""

    entry: _EnumeratedEntry
    is_directory: bool
    children: tuple[Self, ...] = ()


def _entry_from_stat(name: str, metadata: os.stat_result) -> _EnumeratedEntry:
    return _EnumeratedEntry(
        name=_component_text(name, label="directory entry"),
        st_dev=metadata.st_dev,
        st_ino=metadata.st_ino,
        st_mode=metadata.st_mode,
        st_size=metadata.st_size,
        st_mtime_ns=metadata.st_mtime_ns,
        st_ctime_ns=metadata.st_ctime_ns,
        st_nlink=metadata.st_nlink,
    )


def _entry_matches_stat(entry: _EnumeratedEntry, metadata: os.stat_result) -> bool:
    return (
        entry.st_dev == metadata.st_dev
        and entry.st_ino == metadata.st_ino
        and entry.st_mode == metadata.st_mode
        and entry.st_size == metadata.st_size
        and entry.st_mtime_ns == metadata.st_mtime_ns
        and entry.st_ctime_ns == metadata.st_ctime_ns
        and entry.st_nlink == metadata.st_nlink
    )


def _is_tree_drift_oserror(error: OSError) -> bool:
    return error.errno in {errno.ENOENT, getattr(errno, "ESTALE", -1)}


def _is_tree_policy_oserror(error: OSError) -> bool:
    return error.errno in {errno.EACCES, errno.EPERM}


def _entry_policy_violation(mode: int) -> TreePolicyViolation:
    return "symlink" if stat.S_ISLNK(mode) else "special-file"


def _enumerate_directory(capability: LinuxPathCapability) -> tuple[_EnumeratedEntry, ...]:
    entries: list[_EnumeratedEntry] = []
    scan_capability = capability.reopen_directory()
    try:
        with os.scandir(scan_capability.fd) as iterator:
            for entry in iterator:
                if len(entries) >= _MAX_DIRECTORY_ENTRIES:
                    raise LinuxTreePolicyError(
                        "directory enumeration exceeded the structural entry bound",
                        "structural-shape",
                        path="",
                    )
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    if _is_tree_drift_oserror(exc):
                        raise LinuxTreeDriftError(
                            f"FD-relative entry changed while being enumerated: {entry.name!r}"
                        ) from exc
                    if _is_tree_policy_oserror(exc):
                        raise LinuxTreePolicyError(
                            f"FD-relative entry is unreadable: {entry.name!r}",
                            "unreadable",
                            path=entry.name,
                        ) from exc
                    raise LinuxCapabilityError(
                        f"could not inspect FD-relative entry {entry.name!r}"
                    ) from exc
                try:
                    entries.append(_entry_from_stat(entry.name, metadata))
                except LinuxCapabilityError as exc:
                    raise LinuxTreePolicyError(
                        f"FD-relative entry name violates tree policy: {entry.name!r}",
                        "invalid-name",
                        path=entry.name,
                    ) from exc
    except OSError as exc:
        if _is_tree_drift_oserror(exc):
            raise LinuxTreeDriftError(
                "FD-relative directory changed while being enumerated"
            ) from exc
        if _is_tree_policy_oserror(exc):
            raise LinuxTreePolicyError(
                "FD-relative directory is unreadable", "unreadable", path=""
            ) from exc
        raise LinuxCapabilityError("could not enumerate pinned directory capability") from exc
    finally:
        scan_capability.close()
    return tuple(sorted(entries, key=lambda value: value.name.encode("utf-8")))


def _open_verified_destination_child(
    parent: LinuxPathCapability,
    entry: _EnumeratedEntry,
    *,
    display_path: str,
) -> LinuxPathCapability:
    """Open one already-created child while validating an exclusive private destination tree."""

    if not parent._exclusive_copy_destination:
        raise LinuxCapabilityError(
            "destination child validation requires an exclusive private capability"
        )

    if stat.S_ISLNK(entry.st_mode) or not (
        stat.S_ISDIR(entry.st_mode) or stat.S_ISREG(entry.st_mode)
    ):
        raise LinuxCapabilityError(
            f"FD-relative entry is a symlink or special file: {display_path!r}"
        )
    expected: CapabilityKind = "directory" if stat.S_ISDIR(entry.st_mode) else "regular"
    try:
        child = parent.open_beneath(entry.name, expected=expected, display_path=display_path)
    except LinuxCapabilityUnavailableError:
        raise
    except LinuxCapabilityError as exc:
        raise LinuxCapabilityError(
            f"FD-relative entry changed or became unreadable: {display_path!r}"
        ) from exc
    try:
        held_metadata = os.fstat(child.fd)
    except OSError as exc:
        child.close()
        raise LinuxCapabilityError(
            f"could not inspect FD-relative entry after open: {display_path!r}"
        ) from exc
    if not _entry_matches_stat(entry, held_metadata):
        child.close()
        raise LinuxCapabilityError(
            f"FD-relative entry identity changed before open: {display_path!r}"
        )
    try:
        named_metadata = os.stat(entry.name, dir_fd=parent.fd, follow_symlinks=False)
    except OSError as exc:
        child.close()
        raise LinuxCapabilityError(
            f"FD-relative entry changed after open: {display_path!r}"
        ) from exc
    if not _entry_matches_stat(entry, named_metadata):
        child.close()
        raise LinuxCapabilityError(f"FD-relative entry changed after open: {display_path!r}")
    return child


def _verify_held_child_name(
    parent: LinuxPathCapability,
    entry: _EnumeratedEntry,
    child: LinuxPathCapability,
    *,
    display_path: str,
) -> None:
    """Prove a held child still has its original verified parent-directory binding."""

    try:
        held_metadata = os.fstat(child.fd)
        named_metadata = os.stat(entry.name, dir_fd=parent.fd, follow_symlinks=False)
    except OSError as exc:
        if _is_tree_drift_oserror(exc):
            raise LinuxTreeDriftError(
                f"FD-relative entry changed after acquisition: {display_path!r}"
            ) from exc
        if _is_tree_policy_oserror(exc):
            raise LinuxTreePolicyError(
                f"FD-relative entry is unreadable: {display_path!r}",
                "unreadable",
                path=display_path,
            ) from exc
        raise LinuxCapabilityError(
            f"could not inspect FD-relative entry after acquisition: {display_path!r}"
        ) from exc
    if not _entry_matches_stat(entry, held_metadata) or not _entry_matches_stat(
        entry,
        named_metadata,
    ):
        raise LinuxTreeDriftError(f"FD-relative entry changed after acquisition: {display_path!r}")


def _acquire_current_child(
    parent: LinuxPathCapability,
    name: str,
    *,
    display_path: str,
) -> tuple[_EnumeratedEntry, LinuxPathCapability]:
    """Open a child before snapshotting it, then bind its live name to that FD.

    A directory stream may retain a name after its original object has been unlinked.  On ext4
    the inode can be reused immediately, so a naked ``DirEntry.stat()`` tuple is not a durable
    authority.  Acquire the descriptor first; the held object's metadata is the only snapshot
    used for a later visit, and the no-follow parent lookup proves the name still denotes it.
    """

    component = _component_text(name, label="directory entry")
    try:
        child = parent.open_beneath(component, expected="any", display_path=display_path)
    except LinuxCapabilityUnavailableError:
        raise
    except _OpenAt2Error as exc:
        if exc.error_number in {errno.ENOENT, getattr(errno, "ESTALE", -1)}:
            raise LinuxTreeDriftError(
                f"FD-relative entry changed before acquisition: {display_path!r}"
            ) from exc
        if exc.error_number in {errno.EACCES, errno.EPERM}:
            raise LinuxTreePolicyError(
                f"FD-relative entry is unreadable: {display_path!r}",
                "unreadable",
                path=display_path,
            ) from exc
        try:
            failed_metadata = os.stat(
                component,
                dir_fd=parent.fd,
                follow_symlinks=False,
            )
        except OSError as inspect_error:
            if _is_tree_drift_oserror(inspect_error):
                raise LinuxTreeDriftError(
                    f"FD-relative entry changed before acquisition: {display_path!r}"
                ) from exc
            raise LinuxCapabilityError(
                f"could not inspect FD-relative entry before acquisition: {display_path!r}"
            ) from inspect_error
        if stat.S_ISLNK(failed_metadata.st_mode) or not (
            stat.S_ISDIR(failed_metadata.st_mode) or stat.S_ISREG(failed_metadata.st_mode)
        ):
            raise LinuxTreePolicyError(
                f"FD-relative entry is a symlink or special file: {display_path!r}",
                _entry_policy_violation(failed_metadata.st_mode),
                path=display_path,
            ) from exc
        raise LinuxCapabilityError(
            f"could not acquire FD-relative entry: {display_path!r}"
        ) from exc
    except LinuxCapabilityError as exc:
        # This lookup classifies a failed acquisition only; it never supplies authority for a
        # successful visit.  The latter always comes from the held descriptor below.
        try:
            failed_metadata = os.stat(
                component,
                dir_fd=parent.fd,
                follow_symlinks=False,
            )
        except OSError as inspect_error:
            if _is_tree_drift_oserror(inspect_error):
                raise LinuxTreeDriftError(
                    f"FD-relative entry changed before acquisition: {display_path!r}"
                ) from exc
            raise LinuxCapabilityError(
                f"could not inspect FD-relative entry before acquisition: {display_path!r}"
            ) from inspect_error
        if failed_metadata is not None and (
            stat.S_ISLNK(failed_metadata.st_mode)
            or not (stat.S_ISDIR(failed_metadata.st_mode) or stat.S_ISREG(failed_metadata.st_mode))
        ):
            raise LinuxTreePolicyError(
                f"FD-relative entry is a symlink or special file: {display_path!r}",
                _entry_policy_violation(failed_metadata.st_mode),
                path=display_path,
            ) from exc
        raise LinuxCapabilityError(
            f"FD-relative entry changed or became unreadable: {display_path!r}"
        ) from exc
    try:
        held_metadata = os.fstat(child.fd)
        entry = _entry_from_stat(component, held_metadata)
        if not (stat.S_ISDIR(held_metadata.st_mode) or stat.S_ISREG(held_metadata.st_mode)):
            raise LinuxTreePolicyError(
                f"FD-relative entry is a symlink or special file: {display_path!r}",
                _entry_policy_violation(held_metadata.st_mode),
                path=display_path,
            )
        named_metadata = os.stat(component, dir_fd=parent.fd, follow_symlinks=False)
    except OSError as exc:
        child.close()
        if _is_tree_drift_oserror(exc):
            raise LinuxTreeDriftError(
                f"FD-relative entry changed after acquisition: {display_path!r}"
            ) from exc
        if _is_tree_policy_oserror(exc):
            raise LinuxTreePolicyError(
                f"FD-relative entry is unreadable: {display_path!r}",
                "unreadable",
                path=display_path,
            ) from exc
        raise LinuxCapabilityError(
            f"could not inspect FD-relative entry after open: {display_path!r}"
        ) from exc
    except BaseException:
        child.close()
        raise
    if not _entry_matches_stat(entry, named_metadata):
        child.close()
        raise LinuxTreeDriftError(f"FD-relative entry changed after acquisition: {display_path!r}")
    return entry, child


def _metadata_fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _verify_directory_metadata(
    directory: LinuxPathCapability,
    *,
    initial_metadata: os.stat_result,
    display_path: str,
) -> None:
    """Require that no source-directory namespace or metadata mutation preceded a visit."""

    try:
        current_metadata = os.fstat(directory.fd)
    except OSError as exc:
        if _is_tree_drift_oserror(exc):
            raise LinuxTreeDriftError(
                f"FD-relative source directory changed while being visited: {display_path!r}"
            ) from exc
        raise LinuxCapabilityError(
            f"could not verify FD-relative source directory {display_path!r}"
        ) from exc
    if _metadata_fingerprint(current_metadata) != _metadata_fingerprint(initial_metadata):
        raise LinuxTreeDriftError(
            f"FD-relative source directory changed while being visited: {display_path!r}"
        )


def _verify_directory_snapshot(
    directory: LinuxPathCapability,
    expected: tuple[_EnumeratedEntry, ...],
    *,
    initial_metadata: os.stat_result,
    display_path: str,
) -> None:
    """Require a source directory's names and metadata to remain coherent through a visit."""

    _verify_directory_metadata(
        directory,
        initial_metadata=initial_metadata,
        display_path=display_path,
    )
    observed = _enumerate_directory(directory)
    if observed != expected:
        raise LinuxTreeDriftError(
            f"FD-relative source directory entries changed while being visited: {display_path!r}"
        )


def _stream_verified_children(
    directory: LinuxPathCapability,
    *,
    relative_parent: str,
    before_open: Callable[[str], None] | None,
    visit: Callable[[_EnumeratedEntry, LinuxPathCapability, str], None],
    omit_names: frozenset[str] = frozenset(),
) -> None:
    """Acquire each child before it can be freed, verify its name, then visit while held."""

    try:
        initial_metadata = os.fstat(directory.fd)
    except OSError as exc:
        if _is_tree_drift_oserror(exc):
            raise LinuxTreeDriftError(
                "FD-relative source directory changed while being visited: "
                f"{directory.display_path!r}"
            ) from exc
        raise LinuxCapabilityError(
            f"could not inspect FD-relative source directory {directory.display_path!r}"
        ) from exc
    entries: list[_EnumeratedEntry] = []
    scan_capability = directory.reopen_directory()
    try:
        with os.scandir(scan_capability.fd) as iterator:
            for raw_entry in iterator:
                if len(entries) >= _MAX_DIRECTORY_ENTRIES:
                    raise LinuxTreePolicyError(
                        "directory enumeration exceeded the structural entry bound",
                        "structural-shape",
                    )
                try:
                    name = _component_text(raw_entry.name, label="directory entry")
                except LinuxCapabilityError as exc:
                    raise LinuxTreePolicyError(
                        f"FD-relative entry name violates tree policy: {raw_entry.name!r}",
                        "invalid-name",
                    ) from exc
                relative = name if not relative_parent else f"{relative_parent}/{name}"
                entry, child = _acquire_current_child(
                    directory,
                    name,
                    display_path=relative,
                )
                entries.append(entry)
                try:
                    # The held no-follow FD is the acquisition authority: a replacement that
                    # wins before this open is simply the current child.  Recheck observable
                    # parent metadata before invoking any visitor, while post-acquisition name
                    # rebinding below rejects a replacement that races the actual visit.
                    _verify_directory_metadata(
                        directory,
                        initial_metadata=initial_metadata,
                        display_path=relative_parent or directory.display_path,
                    )
                    if entry.name in omit_names:
                        continue
                    # The hook deliberately runs only after the descriptor is held.  A test or
                    # hostile source can therefore rename/unlink/recreate the name, but it cannot
                    # make this visit read a replacement object; the post-hook binding check fails.
                    if before_open is not None:
                        before_open(relative)
                    _verify_held_child_name(
                        directory,
                        entry,
                        child,
                        display_path=relative,
                    )
                    visit(entry, child, relative)
                    _verify_held_child_name(
                        directory,
                        entry,
                        child,
                        display_path=relative,
                    )
                    _verify_directory_metadata(
                        directory,
                        initial_metadata=initial_metadata,
                        display_path=relative_parent or directory.display_path,
                    )
                finally:
                    child.close()
    except OSError as exc:
        if _is_tree_drift_oserror(exc):
            raise LinuxTreeDriftError(
                "FD-relative directory changed while being enumerated"
            ) from exc
        if _is_tree_policy_oserror(exc):
            raise LinuxTreePolicyError("FD-relative directory is unreadable", "unreadable") from exc
        raise LinuxCapabilityError("could not enumerate pinned directory capability") from exc
    finally:
        scan_capability.close()
    _verify_directory_snapshot(
        directory,
        tuple(sorted(entries, key=lambda value: value.name.encode("utf-8"))),
        initial_metadata=initial_metadata,
        display_path=relative_parent or directory.display_path,
    )


def open_verified_children(
    root: LinuxPathCapability,
    *,
    omit_names: frozenset[str] = frozenset(),
    before_open: Callable[[str], None] | None = None,
) -> tuple[tuple[str, LinuxPathCapability], ...]:
    """Acquire every direct regular/directory child with enumeration/open identity binding."""

    acquired: list[tuple[str, LinuxPathCapability]] = []
    try:

        def retain(
            entry: _EnumeratedEntry,
            child: LinuxPathCapability,
            _relative: str,
        ) -> None:
            acquired.append((entry.name, child.duplicate()))

        _stream_verified_children(
            root,
            relative_parent="",
            before_open=before_open,
            visit=retain,
            omit_names=omit_names,
        )
        return tuple(acquired)
    except BaseException:
        for _name, capability in acquired:
            capability.close()
        raise


def _display_child(parent: LinuxPathCapability, name: str) -> str:
    return f"{parent.display_path.rstrip('/')}/{name}"


def _entry_from_capability(name: str, capability: LinuxPathCapability) -> _EnumeratedEntry:
    """Capture the current descriptor identity for a destination entry."""

    try:
        metadata = os.fstat(capability.fd)
    except OSError as exc:
        raise LinuxCapabilityError(f"could not inspect copied FD-relative entry {name!r}") from exc
    return _entry_from_stat(name, metadata)


def _create_destination_directory(
    parent: LinuxPathCapability,
    name: str,
    *,
    display_path: str,
) -> LinuxPathCapability:
    """Create and identity-bind a single child directory below an owned parent FD."""

    try:
        os.mkdir(name, mode=0o700, dir_fd=parent.fd)
    except FileExistsError as exc:
        raise LinuxCapabilityError(f"copy destination already contains {display_path!r}") from exc
    except OSError as exc:
        raise LinuxCapabilityError(
            f"could not create FD-relative directory {display_path!r}"
        ) from exc
    try:
        metadata = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
    except OSError as exc:
        raise LinuxCapabilityError(
            f"could not inspect created FD-relative directory {display_path!r}"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise LinuxCapabilityError(
            f"created FD-relative directory changed before open: {display_path!r}"
        )
    created = _entry_from_stat(name, metadata)
    directory = _open_verified_destination_child(parent, created, display_path=display_path)
    try:
        # The source directory may be read-only.  Keep the destination traversable while its
        # descendants are copied, then restore only its ordinary permission bits at completion.
        os.chmod(directory.fd, 0o700)
    except OSError as exc:
        directory.close()
        raise LinuxCapabilityError(
            f"could not prepare FD-relative directory {display_path!r}"
        ) from exc
    return directory


def _create_destination_regular(
    parent: LinuxPathCapability,
    name: str,
    *,
    display_path: str,
) -> LinuxPathCapability:
    """Create a private regular destination file below an owned parent FD."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_CLOEXEC | _O_NOFOLLOW
    try:
        fd = os.open(name, flags, 0o600, dir_fd=parent.fd)
    except FileExistsError as exc:
        raise LinuxCapabilityError(f"copy destination already contains {display_path!r}") from exc
    except OSError as exc:
        raise LinuxCapabilityError(f"could not create FD-relative file {display_path!r}") from exc
    return LinuxPathCapability._from_open_fd(
        fd,
        display_path=display_path,
        expected="regular",
        exclusive_copy_destination=parent._exclusive_copy_destination,
    )


def _write_all(fd: int, value: bytes) -> None:
    """Write one buffered block completely, retaining the caller's descriptor ownership."""

    remaining = memoryview(value)
    while remaining:
        try:
            written = os.write(fd, remaining)
        except InterruptedError:
            continue
        except OSError as exc:
            raise LinuxCapabilityError("could not write copied regular file") from exc
        if written <= 0:
            raise LinuxCapabilityError("could not write copied regular file")
        remaining = remaining[written:]


def _copy_regular_contents(
    source: LinuxPathCapability,
    destination: LinuxPathCapability,
    *,
    source_entry: _EnumeratedEntry,
    display_path: str,
) -> None:
    """Copy and re-read one stable source file, rejecting same-size content mutation."""

    size_bytes = source_entry.st_size
    try:
        os.lseek(source.fd, 0, os.SEEK_SET)
    except OSError as exc:
        raise LinuxCapabilityError(
            f"could not seek FD-relative source file {display_path!r}"
        ) from exc
    copied_digest = hashlib.blake2b(digest_size=32)
    remaining = size_bytes
    while remaining:
        try:
            block = os.read(source.fd, min(_COPY_CHUNK_BYTES, remaining))
        except InterruptedError:
            continue
        except OSError as exc:
            raise LinuxCapabilityError(
                f"could not read FD-relative source file {display_path!r}"
            ) from exc
        if not block:
            raise LinuxTreeDriftError(
                f"FD-relative source file changed while being copied: {display_path!r}"
            )
        _write_all(destination.fd, block)
        copied_digest.update(block)
        remaining -= len(block)
    try:
        first_final_source = os.fstat(source.fd)
        final_destination = os.fstat(destination.fd)
    except OSError as exc:
        raise LinuxCapabilityError(
            f"could not verify copied FD-relative file {display_path!r}"
        ) from exc
    if not _entry_matches_stat(source_entry, first_final_source):
        raise LinuxTreeDriftError(
            f"FD-relative source file changed while being copied: {display_path!r}"
        )
    if not stat.S_ISREG(final_destination.st_mode) or final_destination.st_size != size_bytes:
        raise LinuxCapabilityError(f"copied FD-relative file changed: {display_path!r}")
    try:
        os.lseek(source.fd, 0, os.SEEK_SET)
    except OSError as exc:
        raise LinuxCapabilityError(
            f"could not seek FD-relative source file {display_path!r}"
        ) from exc
    verified_digest = hashlib.blake2b(digest_size=32)
    remaining = size_bytes
    while remaining:
        try:
            block = os.read(source.fd, min(_COPY_CHUNK_BYTES, remaining))
        except InterruptedError:
            continue
        except OSError as exc:
            raise LinuxCapabilityError(
                f"could not verify FD-relative source file {display_path!r}"
            ) from exc
        if not block:
            raise LinuxTreeDriftError(
                f"FD-relative source file changed while being copied: {display_path!r}"
            )
        verified_digest.update(block)
        remaining -= len(block)
    try:
        second_final_source = os.fstat(source.fd)
    except OSError as exc:
        raise LinuxCapabilityError(
            f"could not verify FD-relative source file {display_path!r}"
        ) from exc
    if (
        not _entry_matches_stat(source_entry, second_final_source)
        or copied_digest.digest() != verified_digest.digest()
    ):
        raise LinuxTreeDriftError(
            f"FD-relative source file changed while being copied: {display_path!r}"
        )


def _set_copied_mode(capability: LinuxPathCapability, mode: int, *, display_path: str) -> None:
    """Restore only ordinary rwx bits; ownership, timestamps, and special bits never transfer."""

    try:
        os.chmod(capability.fd, stat.S_IMODE(mode) & 0o777)
    except OSError as exc:
        raise LinuxCapabilityError(
            f"could not set copied FD-relative mode for {display_path!r}"
        ) from exc


def _verify_copied_entries(
    directory: LinuxPathCapability,
    expected: tuple[_CopiedEntry, ...],
    *,
    relative_parent: str,
    depth: int,
) -> None:
    """Ensure the destination still consists of the exact identities created by this copy."""

    if depth > _MAX_ANCESTRY_DEPTH:
        raise LinuxTreePolicyError(
            "tree copy exceeded the structural depth bound", "structural-shape"
        )
    observed = _enumerate_directory(directory)
    expected_by_name = {value.entry.name: value for value in expected}
    if len(observed) != len(expected) or any(
        entry.name not in expected_by_name for entry in observed
    ):
        raise LinuxCapabilityError("copy destination changed while the tree was being copied")
    for observed_entry in observed:
        copied = expected_by_name[observed_entry.name]
        expected_entry = copied.entry
        if observed_entry != expected_entry:
            raise LinuxCapabilityError("copy destination changed while the tree was being copied")
        relative = (
            observed_entry.name
            if not relative_parent
            else f"{relative_parent}/{observed_entry.name}"
        )
        child = _open_verified_destination_child(directory, expected_entry, display_path=relative)
        try:
            if copied.is_directory:
                _verify_copied_entries(
                    child,
                    copied.children,
                    relative_parent=relative,
                    depth=depth + 1,
                )
        finally:
            child.close()


def copy_tree(
    source: LinuxPathCapability,
    destination: LinuxPathCapability,
    *,
    file_limit: int | None = None,
    byte_limit: int | None = None,
    before_open: Callable[[str], None] | None = None,
) -> TreeWalkUsage:
    """Copy a held source directory's regular-file tree into an empty held destination directory.

    Both root capabilities remain owned by the caller.  Every source entry is enumerated and
    opened through descriptor-relative no-follow operations, while every destination name is a
    validated single component created under its held directory descriptor.  The result counts
    logical regular-file bytes and nested directories, matching :func:`walk_tree` semantics.
    """

    if not stat.S_ISDIR(source.st_mode):
        raise LinuxCapabilityError("tree copy source must be a directory capability")
    if not stat.S_ISDIR(destination.st_mode):
        raise LinuxCapabilityError("tree copy destination must be a directory capability")
    if not destination._exclusive_copy_destination:
        raise LinuxCapabilityError(
            "tree copy destination must be a validated exclusive private capability"
        )
    if capabilities_overlap(source, destination):
        raise LinuxCapabilityError("tree copy source and destination capabilities may not overlap")
    if _enumerate_directory(destination):
        raise LinuxCapabilityError("tree copy destination must be empty")

    file_count = 0
    size_bytes = 0
    directory_count = 0

    def copy_directory(
        relative_parent: str,
        source_directory: LinuxPathCapability,
        destination_directory: LinuxPathCapability,
        depth: int,
    ) -> tuple[_CopiedEntry, ...]:
        nonlocal directory_count, file_count, size_bytes
        if depth > _MAX_ANCESTRY_DEPTH:
            raise LinuxTreePolicyError(
                "tree copy exceeded the structural depth bound", "structural-shape"
            )
        copied_entries: list[_CopiedEntry] = []

        def copy_child(
            source_entry: _EnumeratedEntry,
            source_child: LinuxPathCapability,
            relative: str,
        ) -> None:
            nonlocal directory_count, file_count, size_bytes
            destination_display = _display_child(destination_directory, source_entry.name)
            if stat.S_ISDIR(source_child.st_mode):
                directory_count += 1
                if directory_count > _MAX_TREE_DIRECTORIES:
                    raise LinuxTreePolicyError(
                        "tree copy exceeded the structural directory bound",
                        "structural-shape",
                    )
                destination_child = _create_destination_directory(
                    destination_directory,
                    source_entry.name,
                    display_path=destination_display,
                )
                try:
                    children = copy_directory(
                        relative,
                        source_child,
                        destination_child,
                        depth + 1,
                    )
                    _set_copied_mode(
                        destination_child,
                        source_child.st_mode,
                        display_path=destination_display,
                    )
                    copied_entries.append(
                        _CopiedEntry(
                            entry=_entry_from_capability(source_entry.name, destination_child),
                            is_directory=True,
                            children=children,
                        )
                    )
                finally:
                    destination_child.close()
                return

            file_count += 1
            if file_limit is not None and file_count > file_limit:
                raise LinuxTreeLimitError("file-count", file_count, size_bytes)
            size_bytes += source_entry.st_size
            if byte_limit is not None and size_bytes > byte_limit:
                raise LinuxTreeLimitError("file-bytes", file_count, size_bytes)
            destination_child = _create_destination_regular(
                destination_directory,
                source_entry.name,
                display_path=destination_display,
            )
            try:
                _copy_regular_contents(
                    source_child,
                    destination_child,
                    source_entry=source_entry,
                    display_path=relative,
                )
                _set_copied_mode(
                    destination_child,
                    source_child.st_mode,
                    display_path=destination_display,
                )
                copied_entries.append(
                    _CopiedEntry(
                        entry=_entry_from_capability(source_entry.name, destination_child),
                        is_directory=False,
                    )
                )
            finally:
                destination_child.close()

        _stream_verified_children(
            source_directory,
            relative_parent=relative_parent,
            before_open=before_open,
            visit=copy_child,
        )
        return tuple(copied_entries)

    try:
        with (
            source.reopen_directory() as source_root,
            destination.reopen_directory() as destination_root,
        ):
            copied = copy_directory("", source_root, destination_root, 0)
            _verify_copied_entries(destination_root, copied, relative_parent="", depth=0)
    except RecursionError as exc:
        raise LinuxTreePolicyError(
            "tree copy exceeded the structural depth bound", "structural-shape"
        ) from exc
    return TreeWalkUsage(
        file_count=file_count,
        size_bytes=size_bytes,
        directory_count=directory_count,
    )


def walk_tree(
    root: LinuxPathCapability,
    *,
    file_limit: int | None = None,
    byte_limit: int | None = None,
    before_open: Callable[[str], None] | None = None,
) -> TreeWalkUsage:
    """Walk one held tree with FD queues and fail on any entry/open identity drift."""

    if not stat.S_ISDIR(root.st_mode):
        raise LinuxCapabilityError("tree walk root must be a directory capability")
    file_count = 0
    size_bytes = 0
    directory_count = 0

    def visit(relative_parent: str, directory: LinuxPathCapability, depth: int) -> None:
        nonlocal directory_count, file_count, size_bytes
        if depth > _MAX_ANCESTRY_DEPTH:
            raise LinuxTreePolicyError(
                "tree walk exceeded the structural depth bound", "structural-shape"
            )

        def visit_child(
            entry: _EnumeratedEntry,
            child: LinuxPathCapability,
            relative: str,
        ) -> None:
            nonlocal directory_count, file_count, size_bytes
            if stat.S_ISDIR(child.st_mode):
                directory_count += 1
                if directory_count > _MAX_TREE_DIRECTORIES:
                    raise LinuxTreePolicyError(
                        "tree walk exceeded the structural directory bound",
                        "structural-shape",
                    )
                visit(relative, child, depth + 1)
                return
            file_count += 1
            if file_limit is not None and file_count > file_limit:
                raise LinuxTreeLimitError("file-count", file_count, size_bytes)
            size_bytes += entry.st_size
            if byte_limit is not None and size_bytes > byte_limit:
                raise LinuxTreeLimitError("file-bytes", file_count, size_bytes)

        _stream_verified_children(
            directory,
            relative_parent=relative_parent,
            before_open=before_open,
            visit=visit_child,
        )

    with root.reopen_directory() as scan_root:
        try:
            visit("", scan_root, 0)
        except RecursionError as exc:
            raise LinuxTreePolicyError(
                "tree walk exceeded the structural depth bound", "structural-shape"
            ) from exc
    return TreeWalkUsage(
        file_count=file_count,
        size_bytes=size_bytes,
        directory_count=directory_count,
    )


__all__ = [
    "LinuxCapabilityError",
    "LinuxCapabilityUnavailableError",
    "LinuxPathCapability",
    "LinuxTreeDriftError",
    "LinuxTreeLimitError",
    "LinuxTreePolicyError",
    "TreeLimitName",
    "TreeWalkUsage",
    "capabilities_overlap",
    "copy_tree",
    "is_ancestor",
    "open_verified_children",
    "walk_tree",
]
