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

_O_CLOEXEC: Final[int] = int(getattr(os, "O_CLOEXEC", 0x80000))
_O_DIRECTORY: Final[int] = int(getattr(os, "O_DIRECTORY", 0x10000))
_O_NOFOLLOW: Final[int] = int(getattr(os, "O_NOFOLLOW", 0x20000))
_O_NONBLOCK: Final[int] = int(getattr(os, "O_NONBLOCK", 0x800))

CapabilityKind = Literal["directory", "regular", "any"]
TreeLimitName = Literal["file-count", "file-bytes"]


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
    raise LinuxCapabilityError(
        f"could not acquire FD capability with openat2: errno {error_number}"
    )


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
    return stat.S_ISDIR(mode) or stat.S_ISREG(mode)


class LinuxPathCapability:
    """Exclusive ownership of one already-opened Linux filesystem object."""

    __slots__ = (
        "_closed",
        "_fd",
        "_lock",
        "display_path",
        "filesystem_magic",
        "st_dev",
        "st_ino",
        "st_mode",
    )

    def __init__(self, fd: int, *, display_path: str) -> None:
        _require_linux()
        try:
            metadata = os.fstat(fd)
            filesystem_magic = _filesystem_magic(fd)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        self._closed = False
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
            capability = cls(fd, display_path=display)
            if not _kind_matches(capability.st_mode, expected):
                capability.close()
                raise LinuxCapabilityError(f"capability object has the wrong type for {display!r}")
            if executable and not capability.st_mode & 0o111:
                capability.close()
                raise LinuxCapabilityError(f"capability object is not executable: {display!r}")
            return capability
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
        capability = cls(fd, display_path=display)
        if not _kind_matches(capability.st_mode, expected):
            capability.close()
            raise LinuxCapabilityError(f"capability object has the wrong type for {display!r}")
        if executable and not capability.st_mode & 0o111:
            capability.close()
            raise LinuxCapabilityError(f"capability object is not executable: {display!r}")
        return capability

    @classmethod
    def _from_open_fd(cls, fd: int, *, display_path: str, expected: CapabilityKind) -> Self:
        capability = cls(fd, display_path=display_path)
        if not _kind_matches(capability.st_mode, expected):
            capability.close()
            raise LinuxCapabilityError(f"capability object has the wrong type for {display_path!r}")
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
        return type(self)._from_open_fd(fd, display_path=display_path, expected=expected)

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


def _enumerate_directory(capability: LinuxPathCapability) -> tuple[_EnumeratedEntry, ...]:
    entries: list[_EnumeratedEntry] = []
    scan_capability = capability.reopen_directory()
    try:
        with os.scandir(scan_capability.fd) as iterator:
            for entry in iterator:
                if len(entries) >= _MAX_DIRECTORY_ENTRIES:
                    raise LinuxCapabilityError(
                        "directory enumeration exceeded the structural entry bound"
                    )
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise LinuxCapabilityError(
                        f"could not inspect FD-relative entry {entry.name!r}"
                    ) from exc
                entries.append(
                    _EnumeratedEntry(
                        name=_component_text(entry.name, label="directory entry"),
                        st_dev=metadata.st_dev,
                        st_ino=metadata.st_ino,
                        st_mode=metadata.st_mode,
                    )
                )
    except OSError as exc:
        raise LinuxCapabilityError("could not enumerate pinned directory capability") from exc
    finally:
        scan_capability.close()
    return tuple(sorted(entries, key=lambda value: value.name.encode("utf-8")))


def open_verified_child(
    parent: LinuxPathCapability,
    entry: _EnumeratedEntry,
    *,
    display_path: str,
) -> LinuxPathCapability:
    """Open one enumerated component and bind its identity to the opened object."""

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
    if (
        child.st_dev != entry.st_dev
        or child.st_ino != entry.st_ino
        or stat.S_IFMT(child.st_mode) != stat.S_IFMT(entry.st_mode)
    ):
        child.close()
        raise LinuxCapabilityError(
            f"FD-relative entry identity changed before open: {display_path!r}"
        )
    return child


def open_verified_children(
    root: LinuxPathCapability,
    *,
    omit_names: frozenset[str] = frozenset(),
    before_open: Callable[[str], None] | None = None,
) -> tuple[tuple[str, LinuxPathCapability], ...]:
    """Acquire every direct regular/directory child with enumeration/open identity binding."""

    acquired: list[tuple[str, LinuxPathCapability]] = []
    try:
        for entry in _enumerate_directory(root):
            if entry.name in omit_names:
                continue
            display = f"{root.display_path.rstrip('/')}/{entry.name}"
            if before_open is not None:
                before_open(entry.name)
            acquired.append((entry.name, open_verified_child(root, entry, display_path=display)))
        return tuple(acquired)
    except BaseException:
        for _name, capability in acquired:
            capability.close()
        raise


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
            raise LinuxCapabilityError("tree walk exceeded the structural depth bound")
        for entry in _enumerate_directory(directory):
            relative = entry.name if not relative_parent else f"{relative_parent}/{entry.name}"
            if before_open is not None:
                before_open(relative)
            child = open_verified_child(directory, entry, display_path=relative)
            try:
                if stat.S_ISDIR(child.st_mode):
                    directory_count += 1
                    if directory_count > _MAX_TREE_DIRECTORIES:
                        raise LinuxCapabilityError(
                            "tree walk exceeded the structural directory bound"
                        )
                    visit(relative, child, depth + 1)
                else:
                    file_count += 1
                    if file_limit is not None and file_count > file_limit:
                        raise LinuxTreeLimitError("file-count", file_count, size_bytes)
                    size_bytes += os.fstat(child.fd).st_size
                    if byte_limit is not None and size_bytes > byte_limit:
                        raise LinuxTreeLimitError("file-bytes", file_count, size_bytes)
            finally:
                child.close()

    with root.reopen_directory() as scan_root:
        try:
            visit("", scan_root, 0)
        except RecursionError as exc:
            raise LinuxCapabilityError("tree walk exceeded the structural depth bound") from exc
    return TreeWalkUsage(
        file_count=file_count,
        size_bytes=size_bytes,
        directory_count=directory_count,
    )


__all__ = [
    "LinuxCapabilityError",
    "LinuxCapabilityUnavailableError",
    "LinuxPathCapability",
    "LinuxTreeLimitError",
    "TreeLimitName",
    "TreeWalkUsage",
    "capabilities_overlap",
    "is_ancestor",
    "open_verified_child",
    "open_verified_children",
    "walk_tree",
]
