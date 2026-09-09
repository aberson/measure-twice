"""Strict coding-agent suite bundles, path containment, globs, and instrument identity.

An agent suite is a directory, not a single JSON document.  ``suite.json`` points to strict task
manifests; each task manifest in turn points only within its own bundle.  Structural loading walks
every referenced component without following symlinks or Windows reparse points and verifies the
fixed visible/hidden pytest layout without executing task code.  A task's seed and oracle assets
are *every* regular file in those trees, so a tree holding generated Python bytecode--a
``__pycache__`` directory or a ``.pyc``/``.pyo`` file--is rejected at load with the offending path
named, never silently skipped: the hashed file set and any later materialization of the same tree
must answer which-files-count identically.

The v1 instrument-hash preimage is canonical JSON of this shape::

    {"schema_version": 1, "suite": <canonical suite object>, "tasks": [
      {"manifest_path": <suite-relative path>, "task": <canonical task object>,
       "assets": [{"path": <suite-relative path>, "sha256": <file digest>,
                   "size_bytes": <byte length>}, ...]}
    ]}

Tasks stay in suite-declared order.  Each task's descriptors are sorted by the UTF-8 bytes of its
relative path.  Only path and file bytes feed asset identity--never host permission/mode metadata--
so a Windows structure-only validation and a Linux run compute the same digest.  Model registry
content is deliberately absent: changing the roster never changes the benchmark instrument.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Literal, Self, cast

from measure_twice.agent_bench._wire import AgentInputError, WireCodec

TASK_FAMILIES = frozenset(
    {"bug-repair", "bounded-feature", "behavioral-refactor", "cli-data-boundary"}
)
DIFFICULTIES = frozenset({"easy", "medium", "hard"})
AGENT_RUN_CLASSES = frozenset({"smoke", "pilot", "observation"})

EVALUATOR_VERSION = "python-pytest-v1"
EVALUATOR_ARGV: tuple[str, ...] = (
    "/opt/measure-twice/runtime/python3.12",
    "-B",
    "-s",
    "-P",
    "-m",
    "pytest",
    "-q",
    "--disable-warnings",
    "--maxfail=1",
    "--basetemp=/tmp/pytest",
    "/workspace/tests",
    "/opt/measure-twice/oracle/tests",
)

ALWAYS_PROTECTED_PATHS: tuple[str, ...] = (
    ".git",
    ".gitignore",
    ".gitattributes",
    ".gitmodules",
    "tests",
)
_SEED_FORBIDDEN_NAMES = frozenset({".git", ".gitignore", ".gitattributes", ".gitmodules"})
# Leavings of a Python run against a bundle tree.  Rejected, never skipped: filtering would
# stabilize the hash while hiding that the bundle was compiled in place, and would give the
# loader and any later tree materializer two different answers to which-files-count.
_GENERATED_BYTECODE_NAMES = frozenset({"__pycache__"})
_GENERATED_BYTECODE_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo")


class AgentSuiteError(AgentInputError):
    """A suite bundle violated its schema, containment, or asset contract."""


_WIRE = WireCodec(AgentSuiteError)


def validate_relative_path(value: object, *, label: str) -> str:
    """Validate a normalized, portable, contained POSIX relative path."""

    if not isinstance(value, str) or not value:
        raise AgentSuiteError(f"{label} must be a non-empty POSIX relative path, got {value!r}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AgentSuiteError(f"{label} must be UTF-8 encodable") from exc
    if "\x00" in value:
        raise AgentSuiteError(f"{label} contains a NUL byte")
    if "\\" in value:
        raise AgentSuiteError(f"{label} must use '/' separators; backslashes are forbidden")
    if ":" in value:
        raise AgentSuiteError(f"{label} may not contain ':' or a Windows drive prefix")
    if value.startswith("/") or value.startswith("//"):
        raise AgentSuiteError(f"{label} must be relative, got {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise AgentSuiteError(
            f"{label} must be normalized and may not contain empty, '.' or '..' segments: {value!r}"
        )
    normalized = PurePosixPath(*parts).as_posix()
    if normalized != value:
        raise AgentSuiteError(f"{label} is not a normalized POSIX relative path: {value!r}")
    return value


def validate_allowed_change_glob(value: object, *, label: str) -> str:
    """Validate the closed, segment-aware ``*``/``?``/whole-segment ``**`` grammar."""

    pattern = validate_relative_path(value, label=label)
    for segment in pattern.split("/"):
        if "[" in segment or "]" in segment:
            raise AgentSuiteError(f"{label} may not use character classes: {pattern!r}")
        if "**" in segment and segment != "**":
            raise AgentSuiteError(
                f"{label} may use '**' only as a complete path segment: {pattern!r}"
            )
    return pattern


def _validate_protected_path(value: object, *, label: str) -> str:
    path = validate_relative_path(value, label=label)
    if any(character in path for character in "*?[]"):
        raise AgentSuiteError(
            f"{label} is an exact path, not a glob; wildcard characters are forbidden"
        )
    return path


@dataclass(frozen=True, slots=True)
class _HandleInfo:
    identity: tuple[int, int]
    is_directory: bool
    is_regular: bool
    is_reparse: bool
    size: int
    mtime_token: int


@dataclass(frozen=True, slots=True)
class _DirectoryEntry:
    name: str
    identity: tuple[int, int]
    is_reparse: bool
    is_directory: bool
    is_regular: bool


@dataclass(frozen=True, slots=True)
class _AssetDescriptor:
    path: str
    sha256: str
    size_bytes: int

    def to_mapping(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "size_bytes": self.size_bytes}


@dataclass(frozen=True, slots=True)
class _CapturedAsset:
    descriptor: _AssetDescriptor
    raw: bytes


@dataclass(frozen=True, slots=True)
class _TreeSnapshot:
    directories: frozenset[str]
    assets: tuple[_AssetDescriptor, ...]


def _require_posix_handle_support() -> None:
    required_flags = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
    if (
        any(not hasattr(os, name) for name in required_flags)
        or os.open not in os.supports_dir_fd
        or os.scandir not in os.supports_fd
    ):
        raise AgentSuiteError(
            "this POSIX host lacks required dir_fd/O_NOFOLLOW containment support"
        )


def _posix_flags(*, directory: bool) -> int:
    _require_posix_handle_support()
    flags = os.O_RDONLY | cast("int", vars(os)["O_CLOEXEC"])
    flags |= cast("int", vars(os)["O_NOFOLLOW"])
    if directory:
        flags |= cast("int", vars(os)["O_DIRECTORY"])
    return flags


def _native_open_root(path: Path) -> int:
    if os.name == "nt":
        from measure_twice.agent_bench import _win32_contained

        return _win32_contained.open_root(path)
    return os.open(path, _posix_flags(directory=True))


def _native_close_handle(handle: int) -> None:
    if os.name == "nt":
        from measure_twice.agent_bench import _win32_contained

        _win32_contained.close_handle(handle)
        return
    os.close(handle)


def _native_handle_info(handle: int) -> _HandleInfo:
    if os.name == "nt":
        from measure_twice.agent_bench import _win32_contained

        windows_info = _win32_contained.handle_info(handle)
        return _HandleInfo(
            identity=windows_info.identity,
            is_directory=windows_info.is_directory,
            is_regular=windows_info.is_regular,
            is_reparse=bool(
                windows_info.attributes & _win32_contained.FILE_ATTRIBUTE_REPARSE_POINT
            ),
            size=windows_info.size,
            mtime_token=windows_info.mtime_token,
        )
    metadata = os.fstat(handle)
    return _HandleInfo(
        identity=(metadata.st_dev, metadata.st_ino),
        is_directory=stat.S_ISDIR(metadata.st_mode),
        is_regular=stat.S_ISREG(metadata.st_mode),
        is_reparse=stat.S_ISLNK(metadata.st_mode),
        size=metadata.st_size,
        mtime_token=metadata.st_mtime_ns,
    )


def _native_list_directory(handle: int, *, target: str | None = None) -> list[_DirectoryEntry]:
    if os.name == "nt":
        from measure_twice.agent_bench import _win32_contained

        return [
            _DirectoryEntry(
                name=entry.name,
                identity=entry.identity,
                is_reparse=bool(entry.attributes & _win32_contained.FILE_ATTRIBUTE_REPARSE_POINT),
                is_directory=entry.is_directory,
                is_regular=not entry.is_directory,
            )
            for entry in _win32_contained.list_directory(handle)
            if target is None or entry.name == target or entry.name.casefold() == target.casefold()
        ]

    scan_handle = os.open(".", _posix_flags(directory=True), dir_fd=handle)
    try:
        parent_device = os.fstat(scan_handle).st_dev
        with os.scandir(scan_handle) as iterator:
            entries = []
            for entry in iterator:
                if (
                    target is not None
                    and entry.name != target
                    and entry.name.casefold() != target.casefold()
                ):
                    continue
                listed_inode = entry.inode()
                metadata = entry.stat(follow_symlinks=False)
                if metadata.st_dev == parent_device and metadata.st_ino != listed_inode:
                    raise OSError(
                        f"directory entry {entry.name!r} changed identity while it was inspected"
                    )
                entries.append(
                    _DirectoryEntry(
                        name=entry.name,
                        identity=(metadata.st_dev, metadata.st_ino),
                        is_reparse=stat.S_ISLNK(metadata.st_mode),
                        is_directory=stat.S_ISDIR(metadata.st_mode),
                        is_regular=stat.S_ISREG(metadata.st_mode),
                    )
                )
            return entries
    finally:
        os.close(scan_handle)


def _native_open_relative(parent_handle: int, component: str, *, directory: bool) -> int:
    if os.name == "nt":
        from measure_twice.agent_bench import _win32_contained

        return _win32_contained.open_relative(parent_handle, component, directory=directory)
    return os.open(component, _posix_flags(directory=directory), dir_fd=parent_handle)


def _native_read_file(handle: int) -> bytes:
    if os.name == "nt":
        from measure_twice.agent_bench import _win32_contained

        return _win32_contained.read_file(handle)
    os.lseek(handle, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(handle, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


class _ContainedNode:
    """Owned filesystem handle plus a display-only path that is never used for access."""

    def __init__(self, handle: int, display_path: Path, info: _HandleInfo) -> None:
        self._handle: int | None = handle
        self.display_path = display_path
        self.info = info

    @property
    def handle(self) -> int:
        if self._handle is None:
            raise AgentSuiteError("contained handle is already closed")
        return self._handle

    def close(self) -> None:
        handle = self._handle
        if handle is not None:
            _native_close_handle(handle)
            self._handle = None

    def take_handle(self) -> int:
        """Transfer this node's owned handle to a new explicit owner."""

        handle = self.handle
        self._handle = None
        return handle

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


class _ContainedRoot(_ContainedNode):
    """Capability pinning the suite root object for an entire load-and-hash snapshot."""

    def __init__(self, display_path: Path) -> None:
        absolute = Path(os.path.abspath(display_path))
        if not absolute.anchor:
            raise AgentSuiteError(f"could not determine filesystem anchor for {display_path}")
        current: _ContainedNode | None = None
        anchor_handle: int | None = None
        try:
            anchor_path = Path(absolute.anchor)
            anchor_handle = _native_open_root(anchor_path)
            anchor_info = _native_handle_info(anchor_handle)
            current = _ContainedNode(anchor_handle, anchor_path, anchor_info)
            anchor_handle = None
            if anchor_info.is_reparse:
                raise AgentSuiteError(
                    f"filesystem anchor may not be a symlink, junction, or reparse point: "
                    f"{anchor_path}"
                )
            if not anchor_info.is_directory:
                raise AgentSuiteError(f"filesystem anchor must be a directory: {anchor_path}")
            for component in absolute.parts[1:]:
                child = _open_contained_handle(
                    current,
                    component,
                    directory=True,
                    label="agent suite path",
                )
                try:
                    current.close()
                except BaseException:
                    child.close()
                    raise
                current = child
            info = current.info
            handle = current.take_handle()
        except OSError as exc:
            if current is not None:
                current.close()
            elif anchor_handle is not None:
                _native_close_handle(anchor_handle)
            raise AgentSuiteError(f"could not pin suite bundle root {absolute}: {exc}") from exc
        except BaseException:
            if current is not None:
                current.close()
            elif anchor_handle is not None:
                _native_close_handle(anchor_handle)
            raise
        super().__init__(handle, absolute, info)


def _exact_entry(parent: _ContainedNode, component: str, *, label: str) -> _DirectoryEntry:
    try:
        entries = _native_list_directory(parent.handle, target=component)
    except (OSError, UnicodeError) as exc:
        raise AgentSuiteError(
            f"could not inspect {label} below {parent.display_path}: {exc}"
        ) from exc
    exact = next((entry for entry in entries if entry.name == component), None)
    if exact is None:
        folded = next(
            (entry.name for entry in entries if entry.name.casefold() == component.casefold()), None
        )
        if folded is not None:
            raise AgentSuiteError(
                f"{label} component {component!r} has wrong case; on disk it is {folded!r}"
            )
        raise AgentSuiteError(
            f"missing {label} component {component!r} below {parent.display_path}"
        )
    if exact.is_reparse:
        raise AgentSuiteError(
            f"{label} may not be a symlink, junction, or reparse point: "
            f"{parent.display_path / component}"
        )
    return exact


def _open_contained_handle(
    parent: _ContainedNode,
    component: str,
    *,
    directory: bool,
    label: str,
    expected_entry: _DirectoryEntry | None = None,
) -> _ContainedNode:
    """Open one exact component relative to a pinned parent; retained as the race-test seam."""

    expected = (
        _exact_entry(parent, component, label=label) if expected_entry is None else expected_entry
    )
    if expected.name != component:
        raise AgentSuiteError(
            f"{label} listed component {expected.name!r} does not match {component!r}"
        )
    if expected.is_reparse:
        raise AgentSuiteError(
            f"{label} may not be a symlink, junction, or reparse point: "
            f"{parent.display_path / component}"
        )
    if directory and not expected.is_directory:
        raise AgentSuiteError(f"{label} changed from a directory before it was opened")
    if not directory and not expected.is_regular:
        raise AgentSuiteError(f"{label} changed from a regular file before it was opened")
    handle: int | None = None
    try:
        handle = _native_open_relative(parent.handle, component, directory=directory)
        info = _native_handle_info(handle)
        if info.identity != expected.identity:
            raise AgentSuiteError(f"{label} changed identity while its component was opened")
        if info.is_reparse:
            raise AgentSuiteError(
                f"{label} may not be a symlink, junction, or reparse point: "
                f"{parent.display_path / component}"
            )
        if directory and not info.is_directory:
            raise AgentSuiteError(f"{label} must be a directory: {component!r}")
        if not directory and not info.is_regular:
            raise AgentSuiteError(f"{label} must be a regular file: {component!r}")
        node = _ContainedNode(handle, parent.display_path / component, info)
        handle = None
        return node
    except OSError as exc:
        raise AgentSuiteError(
            f"could not open contained {label} component {component!r}: {exc}"
        ) from exc
    finally:
        if handle is not None:
            _native_close_handle(handle)


def _resolve_contained(
    root: _ContainedRoot,
    relative: str,
    *,
    label: str,
    expected: Literal["file", "directory"],
    final_identity: tuple[int, int] | None = None,
) -> _ContainedNode:
    """Open a validated path through pinned handles, never through descendant pathnames."""

    validate_relative_path(relative, label=label)
    current: _ContainedNode | None = None
    try:
        components = relative.split("/")
        for index, component in enumerate(components):
            parent = root if current is None else current
            child = _open_contained_handle(
                parent,
                component,
                directory=index < len(components) - 1 or expected == "directory",
                label=label,
            )
            if current is not None:
                try:
                    current.close()
                except BaseException:
                    child.close()
                    raise
            current = child
        if current is None:
            raise AgentSuiteError(f"{label} must not resolve to the bundle root")
        if final_identity is not None and current.info.identity != final_identity:
            raise AgentSuiteError(f"{label} changed identity before its queued traversal")
        return current
    except BaseException:
        if current is not None:
            current.close()
        raise


def _capture_opened_file(node: _ContainedNode, relative: str, *, label: str) -> _CapturedAsset:
    before = node.info
    try:
        raw = _native_read_file(node.handle)
        after = _native_handle_info(node.handle)
    except OSError as exc:
        raise AgentSuiteError(f"could not read contained {label} {relative!r}: {exc}") from exc
    if (
        after.identity != before.identity
        or after.size != before.size
        or after.mtime_token != before.mtime_token
    ):
        raise AgentSuiteError(f"{label} changed while its stable handle was read")
    return _CapturedAsset(
        descriptor=_AssetDescriptor(
            path=relative,
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
        ),
        raw=raw,
    )


def _capture_contained_file(root: _ContainedRoot, relative: str, *, label: str) -> _CapturedAsset:
    with _resolve_contained(root, relative, label=label, expected="file") as node:
        return _capture_opened_file(node, relative, label=label)


def _read_stable_contained_file(root: _ContainedRoot, relative: str, *, label: str) -> bytes:
    """Read a regular file while every ancestor and the final file are pinned by handle."""

    return _capture_contained_file(root, relative, label=label).raw


def _join_relative(parent: str, child: str) -> str:
    return f"{parent}/{child}" if parent else child


def _reject_generated_bytecode(name: str, relative: str, *, label: str) -> None:
    """Refuse compiled-Python leavings, so a bundle compiled in place can never be hashed."""

    if name in _GENERATED_BYTECODE_NAMES or name.endswith(_GENERATED_BYTECODE_SUFFIXES):
        raise AgentSuiteError(
            f"{label} may not contain generated Python bytecode: {relative}; remove every "
            "__pycache__ directory and .pyc/.pyo file from the bundle, then re-validate"
        )


def _walk_regular_files(root: _ContainedRoot, relative: str, *, label: str) -> _TreeSnapshot:
    """Capture one identity-bound snapshot, rejecting races, links, special files, and bytecode."""

    assets: list[_AssetDescriptor] = []
    directories = {relative}
    pending: list[tuple[str, tuple[int, int] | None]] = [(relative, None)]
    while pending:
        directory_relative, expected_identity = pending.pop()
        with _resolve_contained(
            root,
            directory_relative,
            label=label,
            expected="directory",
            final_identity=expected_identity,
        ) as directory:
            try:
                entries = sorted(
                    _native_list_directory(directory.handle),
                    key=lambda entry: entry.name.encode("utf-8"),
                )
            except (OSError, UnicodeError) as exc:
                raise AgentSuiteError(
                    f"could not enumerate {label} {directory.display_path}: {exc}"
                ) from exc
            for entry in entries:
                child_relative = _join_relative(directory_relative, entry.name)
                if entry.is_reparse:
                    raise AgentSuiteError(
                        f"{label} may not contain a symlink, junction, or reparse point: "
                        f"{child_relative}"
                    )
                _reject_generated_bytecode(entry.name, child_relative, label=label)
                if entry.is_directory:
                    with _open_contained_handle(
                        directory,
                        entry.name,
                        directory=True,
                        label=f"{label} directory",
                        expected_entry=entry,
                    ) as child:
                        directories.add(child_relative)
                        pending.append((child_relative, child.info.identity))
                elif entry.is_regular:
                    with _open_contained_handle(
                        directory,
                        entry.name,
                        directory=False,
                        label=f"{label} asset",
                        expected_entry=entry,
                    ) as child:
                        assets.append(
                            _capture_opened_file(
                                child, child_relative, label=f"{label} asset"
                            ).descriptor
                        )
                else:
                    raise AgentSuiteError(
                        f"{label} contains a non-regular special file: {child_relative}"
                    )
    return _TreeSnapshot(
        directories=frozenset(directories),
        assets=tuple(sorted(assets, key=lambda asset: asset.path.encode("utf-8"))),
    )


def _require_nonempty_utf8(raw: bytes, *, label: str) -> None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentSuiteError(f"{label} must be a readable UTF-8 file: {exc}") from exc
    if not text.strip():
        raise AgentSuiteError(f"{label} must not be empty")


def _paths_overlap(left: str, right: str) -> bool:
    left_parts = left.split("/")
    right_parts = right.split("/")
    shorter = min(len(left_parts), len(right_parts))
    return left_parts[:shorter] == right_parts[:shorter]


@dataclass(frozen=True, slots=True)
class RuntimeSpec:
    language: str
    version: str

    def __post_init__(self) -> None:
        if self.language != "python":
            raise AgentSuiteError("suite.runtime.language must equal 'python'")
        if self.version != "3.12":
            raise AgentSuiteError("suite.runtime.version must equal '3.12'")

    @classmethod
    def from_mapping(cls, value: object) -> RuntimeSpec:
        clean = _WIRE.require_exact_keys(
            value, frozenset({"language", "version"}), label="suite.runtime"
        )
        return cls(
            language=cast("str", clean["language"]),
            version=cast("str", clean["version"]),
        )

    def to_mapping(self) -> dict[str, str]:
        return {"language": self.language, "version": self.version}


@dataclass(frozen=True, slots=True)
class TaskProvenance:
    source: str
    license: str | None
    authoring_identity: str
    authoring_assistance: list[str]
    independent_reviewers: list[str]

    def __post_init__(self) -> None:
        _WIRE.validate_nonempty_string(self.source, label="task.provenance.source")
        if self.license is not None:
            _WIRE.validate_nonempty_string(self.license, label="task.provenance.license")
        _WIRE.validate_nonempty_string(
            self.authoring_identity, label="task.provenance.authoring_identity"
        )
        for field_name in ("authoring_assistance", "independent_reviewers"):
            value = getattr(self, field_name)
            if not isinstance(value, list):
                raise AgentSuiteError(
                    f"task.provenance.{field_name} must be a list, got {type(value).__name__}"
                )
            for index, item in enumerate(value):
                _WIRE.validate_nonempty_string(item, label=f"task.provenance.{field_name}[{index}]")
            if len(value) != len(set(value)):
                raise AgentSuiteError(f"task.provenance.{field_name} contains duplicates")

    @classmethod
    def from_mapping(cls, value: object) -> TaskProvenance:
        expected = frozenset(
            {
                "source",
                "license",
                "authoring_identity",
                "authoring_assistance",
                "independent_reviewers",
            }
        )
        clean = _WIRE.require_exact_keys(value, expected, label="task.provenance")
        return cls(
            source=cast("str", clean["source"]),
            license=cast("str | None", clean["license"]),
            authoring_identity=cast("str", clean["authoring_identity"]),
            authoring_assistance=cast("list[str]", clean["authoring_assistance"]),
            independent_reviewers=cast("list[str]", clean["independent_reviewers"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "source": self.source,
            "license": self.license,
            "authoring_identity": self.authoring_identity,
            "authoring_assistance": list(self.authoring_assistance),
            "independent_reviewers": list(self.independent_reviewers),
        }


@dataclass(frozen=True, slots=True)
class AgentTask:
    schema_version: int
    task_id: str
    family: str
    tags: list[str]
    cluster_id: str
    difficulty: str
    prompt: str
    seed: str
    oracle: str
    reference_patch: str
    allowed_changes: list[str]
    protected_paths: list[str]
    provenance: TaskProvenance

    def __post_init__(self) -> None:
        _WIRE.validate_schema_version(self.schema_version, label="task")
        _WIRE.validate_safe_id(self.task_id, label="task.task_id")
        if not isinstance(self.family, str) or self.family not in TASK_FAMILIES:
            raise AgentSuiteError(
                f"task.family must be one of {sorted(TASK_FAMILIES)}, got {self.family!r}"
            )
        if not isinstance(self.tags, list):
            raise AgentSuiteError(f"task.tags must be a list, got {type(self.tags).__name__}")
        for index, tag in enumerate(self.tags):
            _WIRE.validate_safe_id(tag, label=f"task.tags[{index}]")
        if len(self.tags) != len(set(self.tags)):
            raise AgentSuiteError("task.tags contains duplicate ids")
        _WIRE.validate_safe_id(self.cluster_id, label="task.cluster_id")
        if not isinstance(self.difficulty, str) or self.difficulty not in DIFFICULTIES:
            raise AgentSuiteError(
                f"task.difficulty must be one of {sorted(DIFFICULTIES)}, got {self.difficulty!r}"
            )
        for name in ("prompt", "seed", "oracle", "reference_patch"):
            validate_relative_path(getattr(self, name), label=f"task.{name}")
        paths = (self.prompt, self.seed, self.oracle, self.reference_patch)
        for index, left in enumerate(paths):
            for right in paths[index + 1 :]:
                if _paths_overlap(left, right):
                    raise AgentSuiteError(
                        f"task asset paths must be disjoint, got {left!r} and {right!r}"
                    )
        if not isinstance(self.allowed_changes, list) or not self.allowed_changes:
            raise AgentSuiteError("task.allowed_changes must be a non-empty list")
        for index, pattern in enumerate(self.allowed_changes):
            validate_allowed_change_glob(pattern, label=f"task.allowed_changes[{index}]")
        if len(self.allowed_changes) != len(set(self.allowed_changes)):
            raise AgentSuiteError("task.allowed_changes contains duplicate patterns")
        if not isinstance(self.protected_paths, list):
            raise AgentSuiteError(
                f"task.protected_paths must be a list, got {type(self.protected_paths).__name__}"
            )
        for index, protected in enumerate(self.protected_paths):
            _validate_protected_path(protected, label=f"task.protected_paths[{index}]")
        if len(self.protected_paths) != len(set(self.protected_paths)):
            raise AgentSuiteError("task.protected_paths contains duplicate paths")
        if not isinstance(self.provenance, TaskProvenance):
            raise AgentSuiteError("task.provenance must be a TaskProvenance")

    @classmethod
    def from_mapping(cls, value: object) -> AgentTask:
        expected = frozenset(
            {
                "schema_version",
                "task_id",
                "family",
                "tags",
                "cluster_id",
                "difficulty",
                "prompt",
                "seed",
                "oracle",
                "reference_patch",
                "allowed_changes",
                "protected_paths",
                "provenance",
            }
        )
        clean = _WIRE.require_exact_keys(value, expected, label="task")
        schema_version = _WIRE.validate_schema_version(clean["schema_version"], label="task")
        return cls(
            schema_version=schema_version,
            task_id=cast("str", clean["task_id"]),
            family=cast("str", clean["family"]),
            tags=cast("list[str]", clean["tags"]),
            cluster_id=cast("str", clean["cluster_id"]),
            difficulty=cast("str", clean["difficulty"]),
            prompt=cast("str", clean["prompt"]),
            seed=cast("str", clean["seed"]),
            oracle=cast("str", clean["oracle"]),
            reference_patch=cast("str", clean["reference_patch"]),
            allowed_changes=cast("list[str]", clean["allowed_changes"]),
            protected_paths=cast("list[str]", clean["protected_paths"]),
            provenance=TaskProvenance.from_mapping(clean["provenance"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "family": self.family,
            "tags": list(self.tags),
            "cluster_id": self.cluster_id,
            "difficulty": self.difficulty,
            "prompt": self.prompt,
            "seed": self.seed,
            "oracle": self.oracle,
            "reference_patch": self.reference_patch,
            "allowed_changes": list(self.allowed_changes),
            "protected_paths": list(self.protected_paths),
            "provenance": self.provenance.to_mapping(),
        }

    def allows_change(self, path: str) -> bool:
        return is_change_allowed(self, path)


@dataclass(frozen=True, slots=True)
class AgentSuite:
    schema_version: int
    suite_id: str
    version: str
    description: str
    runtime: RuntimeSpec
    evaluator_version: str
    execution_profile_id: str
    scoring_policy: str
    run_class: str
    tasks: list[str]
    task_specs: list[AgentTask] = field(default_factory=list, repr=False, compare=False)
    bundle_root: Path | None = field(default=None, repr=False, compare=False)
    asset_snapshots: tuple[tuple[_AssetDescriptor, ...], ...] = field(
        default=(), repr=False, compare=False
    )

    def __post_init__(self) -> None:
        _WIRE.validate_schema_version(self.schema_version, label="suite")
        _WIRE.validate_safe_id(self.suite_id, label="suite.suite_id")
        _WIRE.validate_nonempty_string(self.version, label="suite.version")
        _WIRE.validate_nonempty_string(self.description, label="suite.description")
        if not isinstance(self.runtime, RuntimeSpec):
            raise AgentSuiteError("suite.runtime must be a RuntimeSpec")
        if self.evaluator_version != EVALUATOR_VERSION:
            raise AgentSuiteError(
                f"suite.evaluator_version must equal {EVALUATOR_VERSION!r}, "
                f"got {self.evaluator_version!r}"
            )
        _WIRE.validate_safe_id(self.execution_profile_id, label="suite.execution_profile_id")
        if self.scoring_policy != "binary-heldout-v1":
            raise AgentSuiteError("suite.scoring_policy must equal 'binary-heldout-v1'")
        if not isinstance(self.run_class, str) or self.run_class not in AGENT_RUN_CLASSES:
            raise AgentSuiteError(
                f"suite.run_class must be one of {sorted(AGENT_RUN_CLASSES)}, "
                f"got {self.run_class!r}"
            )
        if not isinstance(self.tasks, list) or not self.tasks:
            raise AgentSuiteError("suite.tasks must be a non-empty list")
        for index, task_path in enumerate(self.tasks):
            validate_relative_path(task_path, label=f"suite.tasks[{index}]")
        if len(self.tasks) != len(set(self.tasks)):
            raise AgentSuiteError("suite.tasks contains duplicate manifest paths")
        if not isinstance(self.task_specs, list):
            raise AgentSuiteError("suite.task_specs must be a list")
        if self.task_specs:
            if len(self.task_specs) != len(self.tasks):
                raise AgentSuiteError("loaded task count does not match suite.tasks")
            ids = [task.task_id for task in self.task_specs]
            if len(ids) != len(set(ids)):
                raise AgentSuiteError(f"duplicate task id(s): {sorted(_duplicates(ids))}")
        if self.asset_snapshots and len(self.asset_snapshots) != len(self.tasks):
            raise AgentSuiteError("loaded asset snapshot count does not match suite.tasks")

    @classmethod
    def from_mapping(cls, value: object) -> AgentSuite:
        expected = frozenset(
            {
                "schema_version",
                "suite_id",
                "version",
                "description",
                "runtime",
                "evaluator_version",
                "execution_profile_id",
                "scoring_policy",
                "run_class",
                "tasks",
            }
        )
        clean = _WIRE.require_exact_keys(value, expected, label="suite")
        schema_version = _WIRE.validate_schema_version(clean["schema_version"], label="suite")
        return cls(
            schema_version=schema_version,
            suite_id=cast("str", clean["suite_id"]),
            version=cast("str", clean["version"]),
            description=cast("str", clean["description"]),
            runtime=RuntimeSpec.from_mapping(clean["runtime"]),
            evaluator_version=cast("str", clean["evaluator_version"]),
            execution_profile_id=cast("str", clean["execution_profile_id"]),
            scoring_policy=cast("str", clean["scoring_policy"]),
            run_class=cast("str", clean["run_class"]),
            tasks=cast("list[str]", clean["tasks"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "suite_id": self.suite_id,
            "version": self.version,
            "description": self.description,
            "runtime": self.runtime.to_mapping(),
            "evaluator_version": self.evaluator_version,
            "execution_profile_id": self.execution_profile_id,
            "scoring_policy": self.scoring_policy,
            "run_class": self.run_class,
            "tasks": list(self.tasks),
        }

    @property
    def loaded_tasks(self) -> list[AgentTask]:
        return list(self.task_specs)

    @property
    def instrument_hash(self) -> str:
        return instrument_hash(self)

    @property
    def task_hashes(self) -> dict[str, str]:
        return {task.task_id: task_hash(self, index) for index, task in enumerate(self.task_specs)}

    @property
    def families(self) -> frozenset[str]:
        return frozenset(task.family for task in self.task_specs)

    @property
    def tags(self) -> frozenset[str]:
        return frozenset(tag for task in self.task_specs for tag in task.tags)


def _duplicates(values: list[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _is_pytest_file(path: Path) -> bool:
    return path.suffix == ".py" and (
        path.name.startswith("test_") or path.name.endswith("_test.py")
    )


def load_agent_suite(path: str | Path) -> AgentSuite:
    """Load and structurally validate a complete agent-suite directory without executing it."""

    supplied = Path(os.path.abspath(Path(path)))
    root_path = supplied.parent if supplied.name == "suite.json" else supplied

    with _ContainedRoot(root_path) as root:
        suite_file = root.display_path / "suite.json"
        suite = AgentSuite.from_mapping(
            _WIRE.decode_json_object(
                _read_stable_contained_file(root, "suite.json", label="suite manifest"),
                source=suite_file,
                label="agent suite",
            )
        )

        task_specs: list[AgentTask] = []
        asset_snapshots: list[tuple[_AssetDescriptor, ...]] = []
        for index, manifest_relative in enumerate(suite.tasks):
            manifest = root.display_path.joinpath(*manifest_relative.split("/"))
            task = AgentTask.from_mapping(
                _WIRE.decode_json_object(
                    _read_stable_contained_file(
                        root,
                        manifest_relative,
                        label=f"suite.tasks[{index}] manifest",
                    ),
                    source=manifest,
                    label="agent task",
                )
            )
            manifest_parent = PurePosixPath(manifest_relative).parent
            if manifest_parent.name != task.task_id:
                raise AgentSuiteError(
                    f"task manifest directory {manifest_parent.name!r} must equal task_id "
                    f"{task.task_id!r}"
                )
            task_root = manifest_parent.as_posix()
            task_specs.append(task)
            asset_snapshots.append(_snapshot_task_assets(root, task, task_root))

        loaded = replace(
            suite,
            task_specs=task_specs,
            bundle_root=root.display_path,
            asset_snapshots=tuple(asset_snapshots),
        )
        # Hash the immutable snapshot while the root capability is still held.
        instrument_hash(loaded)
        return loaded


def _snapshot_task_assets(
    suite_root: _ContainedRoot, task: AgentTask, task_root: str
) -> tuple[_AssetDescriptor, ...]:
    prompt = _join_relative(task_root, task.prompt)
    reference = _join_relative(task_root, task.reference_patch)
    seed = _join_relative(task_root, task.seed)
    oracle = _join_relative(task_root, task.oracle)
    prompt_capture = _capture_contained_file(
        suite_root, prompt, label=f"task {task.task_id} prompt"
    )
    reference_capture = _capture_contained_file(
        suite_root, reference, label=f"task {task.task_id} reference patch"
    )
    _require_nonempty_utf8(prompt_capture.raw, label=f"task {task.task_id} prompt")
    _require_nonempty_utf8(reference_capture.raw, label=f"task {task.task_id} reference patch")

    seed_snapshot = _walk_regular_files(suite_root, seed, label=f"task {task.task_id} seed")
    oracle_snapshot = _walk_regular_files(suite_root, oracle, label=f"task {task.task_id} oracle")
    seed_paths = [asset.path for asset in seed_snapshot.assets]
    oracle_paths = [asset.path for asset in oracle_snapshot.assets]

    seed_entries = sorted(
        (*seed_snapshot.directories, *seed_paths), key=lambda path: path.encode("utf-8")
    )
    for path in seed_entries:
        if path == seed:
            continue
        relative = PurePosixPath(path).relative_to(seed).as_posix()
        if any(part in _SEED_FORBIDDEN_NAMES for part in relative.split("/")):
            raise AgentSuiteError(
                f"task {task.task_id!r} seed contains forbidden Git control path {relative!r}"
            )

    seed_tests = _join_relative(seed, "tests")
    oracle_tests = _join_relative(oracle, "tests")
    if seed_tests not in seed_snapshot.directories:
        raise AgentSuiteError(f"missing task {task.task_id} visible tests directory")
    if oracle_tests not in oracle_snapshot.directories:
        raise AgentSuiteError(f"missing task {task.task_id} hidden tests directory")
    if not seed_paths:
        raise AgentSuiteError(f"task {task.task_id!r} seed directory is empty")
    if not oracle_paths:
        raise AgentSuiteError(f"task {task.task_id!r} oracle directory is empty")
    visible = [path for path in seed_paths if path.startswith(f"{seed_tests}/")]
    hidden = [path for path in oracle_paths if path.startswith(f"{oracle_tests}/")]
    if not any(_is_pytest_file(Path(path)) for path in visible):
        raise AgentSuiteError(
            f"task {task.task_id!r} visible tests layout contains no pytest test file"
        )
    if not any(_is_pytest_file(Path(path)) for path in hidden):
        raise AgentSuiteError(
            f"task {task.task_id!r} hidden tests layout contains no pytest test file"
        )

    descriptors = [
        prompt_capture.descriptor,
        reference_capture.descriptor,
        *seed_snapshot.assets,
        *oracle_snapshot.assets,
    ]
    descriptor_paths = [descriptor.path for descriptor in descriptors]
    duplicates = _duplicates(descriptor_paths)
    if duplicates:
        raise AgentSuiteError(
            f"task {task.task_id!r} references duplicate assets: {sorted(duplicates)}"
        )
    return tuple(sorted(descriptors, key=lambda entry: entry.path.encode("utf-8")))


def _task_assets(suite: AgentSuite, index: int) -> list[dict[str, object]]:
    if (
        suite.bundle_root is None
        or not suite.task_specs
        or len(suite.asset_snapshots) != len(suite.task_specs)
    ):
        raise AgentSuiteError("instrument hashing requires a suite loaded from a bundle")
    return [descriptor.to_mapping() for descriptor in suite.asset_snapshots[index]]


def instrument_preimage(suite: AgentSuite) -> dict[str, object]:
    """Build the documented canonical v1 preimage for ``instrument_hash``."""

    if not isinstance(suite, AgentSuite):
        raise AgentSuiteError(f"suite must be an AgentSuite, got {type(suite).__name__}")
    if (
        suite.bundle_root is None
        or len(suite.task_specs) != len(suite.tasks)
        or len(suite.asset_snapshots) != len(suite.tasks)
    ):
        raise AgentSuiteError("instrument hashing requires a fully loaded suite bundle")
    task_entries: list[dict[str, object]] = []
    for index, task in enumerate(suite.task_specs):
        task_entries.append(
            {
                "manifest_path": suite.tasks[index],
                "task": task.to_mapping(),
                "assets": _task_assets(suite, index),
            }
        )
    return {
        "schema_version": 1,
        "suite": suite.to_mapping(),
        "tasks": task_entries,
    }


def instrument_hash(suite: AgentSuite) -> str:
    return _WIRE.canonical_sha256(instrument_preimage(suite))


def task_hash(suite: AgentSuite, index: int) -> str:
    if index < 0 or index >= len(suite.task_specs):
        raise AgentSuiteError(f"task index out of range: {index}")
    task = suite.task_specs[index]
    payload = {
        "schema_version": 1,
        "task": task.to_mapping(),
        "assets": _task_assets(suite, index),
    }
    return _WIRE.canonical_sha256(payload)


def _segment_matches(pattern: str, value: str) -> bool:
    pieces: list[str] = ["^"]
    for character in pattern:
        if character == "*":
            pieces.append("[^/]*")
        elif character == "?":
            pieces.append("[^/]")
        else:
            pieces.append(re.escape(character))
    pieces.append("$")
    return re.fullmatch("".join(pieces), value) is not None


def glob_matches(pattern: str, path: str) -> bool:
    """Match a complete normalized path using the closed v1 glob grammar."""

    valid_pattern = validate_allowed_change_glob(pattern, label="allowed-change glob")
    valid_path = validate_relative_path(path, label="changed path")
    pattern_parts = valid_pattern.split("/")
    path_parts = valid_path.split("/")
    # One dynamic-programming row per pattern segment keeps stack use constant even for hostile,
    # but valid, inputs with thousands of ``**`` or path components.
    previous = [False] * (len(path_parts) + 1)
    previous[0] = True
    for pattern_part in pattern_parts:
        current = [False] * (len(path_parts) + 1)
        if pattern_part == "**":
            current[0] = previous[0]
            for path_index in range(1, len(path_parts) + 1):
                current[path_index] = previous[path_index] or current[path_index - 1]
        else:
            for path_index, path_part in enumerate(path_parts, start=1):
                current[path_index] = previous[path_index - 1] and _segment_matches(
                    pattern_part, path_part
                )
        previous = current
    return previous[-1]


def is_protected_path(task: AgentTask, path: str) -> bool:
    valid_path = validate_relative_path(path, label="changed path")
    path_parts = valid_path.split("/")
    for protected in (*ALWAYS_PROTECTED_PATHS, *task.protected_paths):
        protected_parts = protected.split("/")
        if path_parts[: len(protected_parts)] == protected_parts:
            return True
    return False


def is_change_allowed(task: AgentTask, path: str) -> bool:
    """Apply protected exact/descendant override before any allowed-glob match."""

    if not isinstance(task, AgentTask):
        raise AgentSuiteError(f"task must be an AgentTask, got {type(task).__name__}")
    if is_protected_path(task, path):
        return False
    return any(glob_matches(pattern, path) for pattern in task.allowed_changes)


__all__ = [
    "AGENT_RUN_CLASSES",
    "ALWAYS_PROTECTED_PATHS",
    "DIFFICULTIES",
    "EVALUATOR_ARGV",
    "EVALUATOR_VERSION",
    "TASK_FAMILIES",
    "AgentSuite",
    "AgentSuiteError",
    "AgentTask",
    "RuntimeSpec",
    "TaskProvenance",
    "glob_matches",
    "instrument_hash",
    "instrument_preimage",
    "is_change_allowed",
    "is_protected_path",
    "load_agent_suite",
    "task_hash",
    "validate_allowed_change_glob",
    "validate_relative_path",
]
