"""Code-owned Claude launcher discovery and sealed environment policy.

Platform installation roots, executable provenance, dependency PATH, and temporary-directory
selection live here. The adapter owns subprocess execution, compatibility probes, and responses.
"""

from __future__ import annotations

import ctypes
import os
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import cast

from measure_twice.model_sweep_execution import CLAUDE_EXECUTABLE, ClaudeContextProfile


class ClaudeSetupError(ValueError):
    """Claude's executable or supported CLI contract could not be proven before a run."""


@dataclass(frozen=True, slots=True)
class _LauncherBoundary:
    """One code-owned launcher directory and the install roots its target may occupy.

    ``launcher_directory`` is the only directory that may contribute a PATH candidate. A native
    launcher may be a symlink, so ``target_roots`` separately names where its resolved target may
    live (for example ``~/.local/share/claude/versions``). ``dependency_directories`` is the
    minimal approved PATH tail for the selected launch chain; ambient PATH entries never flow into
    a child merely because they are absolute.
    """

    launcher_directory: Path
    target_roots: tuple[Path, ...]
    dependency_directories: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _ResolvedLauncher:
    executable: Path
    boundary: _LauncherBoundary


class _WindowsGuid(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]


@dataclass(frozen=True, slots=True)
class _WindowsRuntimePaths:
    profile: Path
    roaming_app_data: Path
    local_app_data: Path
    program_files: Path
    program_data: Path
    windows: Path


def _windows_known_folder(folder_id: str) -> Path:
    """Resolve a Windows known folder through the OS, never a mutable environment variable."""
    if sys.platform != "win32":
        raise ClaudeSetupError("Windows known folders are unavailable on this platform")
    guid = _WindowsGuid.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
    raw_path = ctypes.c_void_p()
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    get_known_folder = shell32.SHGetKnownFolderPath
    get_known_folder.argtypes = [
        ctypes.POINTER(_WindowsGuid),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_known_folder.restype = ctypes.c_long
    result = get_known_folder(ctypes.byref(guid), 0, None, ctypes.byref(raw_path))
    if result != 0 or raw_path.value is None:
        raise ClaudeSetupError(
            "Windows known-folder lookup failed for "
            f"{folder_id} (HRESULT 0x{result & 0xFFFFFFFF:08x})"
        )
    try:
        return Path(ctypes.wstring_at(raw_path.value)).resolve()
    finally:
        free = ole32.CoTaskMemFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = None
        free(raw_path)


@lru_cache(maxsize=1)
def _windows_runtime_paths() -> _WindowsRuntimePaths:
    """The OS-owned account/system roots used by the Windows launcher policy."""
    return _WindowsRuntimePaths(
        profile=_windows_known_folder("5E6C858F-0E22-4760-9AFE-EA3317B67173"),
        roaming_app_data=_windows_known_folder("3EB685DB-65F9-4CF6-A03A-E3EF65729F3D"),
        local_app_data=_windows_known_folder("F1B32785-6FBA-4FCF-9D55-7B8E7F157091"),
        program_files=_windows_known_folder("905E63B6-C1BF-494E-B29C-65B732D3D21A"),
        program_data=_windows_known_folder("62AB5D82-FDC1-4DC3-A9DD-070D1D495D97"),
        windows=_windows_known_folder("F38BF404-1D43-42F2-9305-67DE0B28FC23"),
    )


def _posix_account_home() -> Path:
    """Resolve the effective account home without trusting ``HOME`` from the parent process."""
    import importlib
    import operator

    pwd = importlib.import_module("pwd")
    geteuid = cast("Callable[[], int]", vars(os)["geteuid"])
    getpwuid = cast("Callable[[int], object]", vars(pwd)["getpwuid"])
    home = cast("str", operator.attrgetter("pw_dir")(getpwuid(geteuid())))
    return Path(home).resolve()


def _runtime_launcher_policy() -> tuple[_LauncherBoundary, ...]:
    """Return the small code-owned platform installation inventory.

    Threat contract: checkout/config/PATH-search hijacking is rejected. Selection trusts the
    chosen user/system installation boundary: same-user malware that already replaced files inside
    one of these roots is outside Step 56 and requires an external signature or trust store.
    """
    if sys.platform == "win32":
        paths = _windows_runtime_paths()
        system32 = paths.windows / "System32"
        node = paths.program_files / "nodejs"
        dependencies = (node, system32, paths.windows)
        native_bin = paths.profile / ".local" / "bin"
        native_versions = paths.profile / ".local" / "share" / "claude" / "versions"
        npm_bin = paths.roaming_app_data / "npm"
        chocolatey_bin = paths.program_data / "chocolatey" / "bin"
        return (
            _LauncherBoundary(native_bin, (native_bin, native_versions), dependencies),
            _LauncherBoundary(npm_bin, (npm_bin,), dependencies),
            _LauncherBoundary(node, (node,), dependencies),
            _LauncherBoundary(
                chocolatey_bin,
                (paths.program_data / "chocolatey",),
                dependencies,
            ),
        )

    home = _posix_account_home()
    system_dependencies = (
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
        Path("/opt/homebrew/bin"),
        Path("/home/linuxbrew/.linuxbrew/bin"),
    )
    return (
        _LauncherBoundary(
            home / ".local" / "bin",
            (
                home / ".local" / "bin",
                home / ".local" / "share" / "claude" / "versions",
                home / ".local" / "lib" / "node_modules",
            ),
            system_dependencies,
        ),
        _LauncherBoundary(
            Path("/usr/local/bin"),
            (
                Path("/usr/local/bin"),
                Path("/usr/local/share/claude/versions"),
                Path("/usr/local/lib/node_modules"),
            ),
            system_dependencies,
        ),
        _LauncherBoundary(
            Path("/usr/bin"),
            (Path("/usr/bin"), Path("/usr/share/claude/versions"), Path("/usr/lib/node_modules")),
            system_dependencies,
        ),
        _LauncherBoundary(
            Path("/opt/homebrew/bin"),
            (Path("/opt/homebrew"),),
            system_dependencies,
        ),
        _LauncherBoundary(
            Path("/home/linuxbrew/.linuxbrew/bin"),
            (Path("/home/linuxbrew/.linuxbrew"),),
            system_dependencies,
        ),
    )


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_path_key(path), _path_key(root))) == _path_key(root)
    except ValueError:
        return False


def _approved_path_boundaries(
    policy: Sequence[_LauncherBoundary],
) -> tuple[_LauncherBoundary, ...]:
    """Filter ambient PATH to exact code-owned launcher directories, preserving PATH order."""
    by_lexical = {
        _path_key(Path(os.path.abspath(boundary.launcher_directory))): boundary
        for boundary in policy
    }
    approved: list[_LauncherBoundary] = []
    seen: set[str] = set()
    for raw_entry in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_entry:
            continue
        clean_entry = os.path.expandvars(raw_entry.strip('"'))
        candidate = Path(clean_entry)
        if not candidate.is_absolute():
            continue
        lexical = Path(os.path.abspath(candidate))
        boundary = by_lexical.get(_path_key(lexical))
        if boundary is None:
            continue
        expected_resolved = boundary.launcher_directory.resolve()
        resolved = candidate.resolve()
        key = _path_key(resolved)
        if key != _path_key(expected_resolved) or key in seen:
            continue
        seen.add(key)
        approved.append(boundary)
    return tuple(approved)


def _launcher_names() -> tuple[str, ...]:
    if sys.platform == "win32":
        return ("claude.exe", "claude.cmd", "claude.bat", CLAUDE_EXECUTABLE)
    return (CLAUDE_EXECUTABLE,)


def _candidate_is_executable(path: Path) -> bool:
    return path.is_file() and (sys.platform == "win32" or os.access(path, os.X_OK))


def _resolve_executable(
    context: ClaudeContextProfile,
    policy: Sequence[_LauncherBoundary],
    *,
    allow_virtual: bool = False,
) -> _ResolvedLauncher:
    """Resolve the literal launcher only through the code-owned runtime policy."""
    if context.executable != CLAUDE_EXECUTABLE:  # defense in depth behind profile validation
        raise ClaudeSetupError(f"Claude executable contract requires literal {CLAUDE_EXECUTABLE!r}")
    approved = _approved_path_boundaries(policy)
    for boundary in approved:
        directory = boundary.launcher_directory.resolve()
        for name in _launcher_names():
            lexical = Path(os.path.abspath(directory / name))
            if not _candidate_is_executable(lexical):
                continue
            resolved = lexical.resolve()
            if not _is_within(lexical, boundary.launcher_directory):
                continue
            if not any(_is_within(resolved, root.resolve()) for root in boundary.target_roots):
                continue
            return _ResolvedLauncher(executable=resolved, boundary=boundary)

    if allow_virtual:
        # A DI runner executes Python test code, not argv[0]. Give it a deterministic virtual path
        # inside the production inventory so offline tests need no installed CLI; this does not
        # authorize a real subprocess or bless an arbitrary tmp_path. Real-Popen tests patch the
        # narrow policy seam and provide an executable candidate that passes the checks above.
        virtual_boundary = next(
            (item for item in policy if item.launcher_directory.is_absolute()), None
        )
        if virtual_boundary is not None:
            suffix = ".exe" if sys.platform == "win32" else ""
            return _ResolvedLauncher(
                executable=(
                    virtual_boundary.launcher_directory / f"{CLAUDE_EXECUTABLE}{suffix}"
                ).resolve(),
                boundary=virtual_boundary,
            )
    raise ClaudeSetupError(
        f"Claude executable {CLAUDE_EXECUTABLE!r} was not found in an approved runtime install root"
    )


def _unique_existing_directories(paths: Sequence[Path]) -> tuple[Path, ...]:
    selected: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved = path.resolve()
        key = _path_key(resolved)
        if key in seen or not resolved.is_dir():
            continue
        seen.add(key)
        selected.append(resolved)
    return tuple(selected)


def _windows_system_environment(paths: _WindowsRuntimePaths) -> Mapping[str, str]:
    system32 = (paths.windows / "System32").resolve()
    command_processor = (system32 / "cmd.exe").resolve()
    return MappingProxyType(
        {
            "COMSPEC": str(command_processor),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "PATH": str(system32),
            "SYSTEMROOT": str(paths.windows),
            "WINDIR": str(paths.windows),
        }
    )


def _trusted_temp_root() -> Path:
    """Return an OS-owned temp boundary, independent of ambient TEMP/TMPDIR and the checkout."""
    root = (
        _windows_runtime_paths().local_app_data / "Temp"
        if sys.platform == "win32"
        else Path("/tmp")  # noqa: S108  # fixed OS-owned boundary, never ambient TMPDIR
    ).resolve()
    if not root.is_dir():
        raise ClaudeSetupError(f"trusted temporary-directory root is unavailable: {root}")
    checkout = Path.cwd().resolve()
    if _is_within(root, checkout):
        raise ClaudeSetupError(
            f"trusted temporary-directory root resolves inside the current checkout: {root}"
        )
    return root


def _allowlisted_environment(
    context: ClaudeContextProfile, launcher: _ResolvedLauncher
) -> Mapping[str, str]:
    """Build the model environment from allowlisted values plus a minimal launch-chain PATH."""
    special = {"COMSPEC", "PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "WINDIR"}
    if sys.platform == "win32":
        ambient = {name.upper(): value for name, value in os.environ.items()}
        selected = {
            name: ambient[name.upper()]
            for name in context.environment_allowlist
            if name.upper() not in special and name.upper() in ambient
        }
    else:
        selected = {
            name: os.environ[name]
            for name in context.environment_allowlist
            if name not in special and name in os.environ
        }
    child_path = _unique_existing_directories(
        (launcher.boundary.launcher_directory, *launcher.boundary.dependency_directories)
    )
    if "PATH" in context.environment_allowlist:
        selected["PATH"] = os.pathsep.join(str(directory) for directory in child_path)
    if sys.platform == "win32":
        paths = _windows_runtime_paths()
        system_values = _windows_system_environment(paths)
        for name in special - {"PATH", "TEMP", "TMP", "TMPDIR"}:
            if name in context.environment_allowlist and name in system_values:
                selected[name] = system_values[name]
        if "USERPROFILE" in context.environment_allowlist:
            selected["USERPROFILE"] = str(paths.profile)
        if "HOME" in context.environment_allowlist:
            selected["HOME"] = str(paths.profile)
        if "APPDATA" in context.environment_allowlist:
            selected["APPDATA"] = str(paths.roaming_app_data)
        if "LOCALAPPDATA" in context.environment_allowlist:
            selected["LOCALAPPDATA"] = str(paths.local_app_data)
    elif "HOME" in context.environment_allowlist:
        selected["HOME"] = str(_posix_account_home())
    temp_root = str(_trusted_temp_root())
    for name in ("TEMP", "TMP", "TMPDIR"):
        if name in context.environment_allowlist:
            selected[name] = temp_root
    return MappingProxyType(selected)


def _credential_free_environment(environment: Mapping[str, str]) -> Mapping[str, str]:
    """Strip model credentials from compatibility probes; only model calls receive OAuth."""
    return MappingProxyType(
        {
            name: value
            for name, value in environment.items()
            if name.upper() != "CLAUDE_CODE_OAUTH_TOKEN"
        }
    )
