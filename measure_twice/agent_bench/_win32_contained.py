"""Small Win32/NT handle layer for race-resistant contained bundle reads.

This module is imported only on Windows.  A bundle root is pinned with ``CreateFileW`` and every
descendant is then opened one component at a time with ``NtCreateFile`` relative to the already
opened parent handle.  Callers own every returned integer handle and must close it explicitly.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

_FILE_LIST_DIRECTORY = 0x0001
_FILE_READ_DATA = 0x0001
_FILE_TRAVERSE = 0x0020
_FILE_READ_ATTRIBUTES = 0x0080
_SYNCHRONIZE = 0x00100000

_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_SHARE_ALL = _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE

_OPEN_EXISTING = 3
_FILE_OPEN = 1
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_TYPE_DISK = 0x0001

_FILE_ID_INFO = 18
_FILE_ID_EXTD_DIRECTORY_INFO = 19
_FILE_ID_EXTD_DIRECTORY_RESTART_INFO = 20
_ERROR_NO_MORE_FILES = 18
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


@dataclass(frozen=True, slots=True)
class HandleInfo:
    """Identity and stable metadata obtained from an opened Windows handle."""

    identity: tuple[int, int]
    attributes: int
    is_directory: bool
    is_regular: bool
    size: int
    mtime_token: int


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    """One exact entry enumerated from a pinned parent directory handle."""

    name: str
    identity: tuple[int, int]
    attributes: int
    is_directory: bool


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", wintypes.LPVOID),
        ("SecurityQualityOfService", wintypes.LPVOID),
    ]


class _IO_STATUS_UNION(ctypes.Union):
    _fields_ = (("Status", wintypes.LONG), ("Pointer", wintypes.LPVOID))


class _IO_STATUS_BLOCK(ctypes.Structure):
    _anonymous_ = ("status_or_pointer",)
    _fields_ = [
        ("status_or_pointer", _IO_STATUS_UNION),
        ("Information", ctypes.c_size_t),
    ]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


class _FILE_ID_128(ctypes.Structure):
    _fields_ = [("Identifier", ctypes.c_ubyte * 16)]


class _FILE_ID_INFO_DATA(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_ulonglong),
        ("FileId", _FILE_ID_128),
    ]


class _FILE_ID_EXTD_DIR_INFO(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", wintypes.DWORD),
        ("FileIndex", wintypes.DWORD),
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("AllocationSize", ctypes.c_longlong),
        ("FileAttributes", wintypes.DWORD),
        ("FileNameLength", wintypes.DWORD),
        ("EaSize", wintypes.DWORD),
        ("ReparsePointTag", wintypes.DWORD),
        ("FileId", _FILE_ID_128),
        ("FileName", wintypes.WCHAR * 1),
    ]


_windows_ctypes = cast("Any", ctypes)
_kernel32 = _windows_ctypes.WinDLL("kernel32", use_last_error=True)
_ntdll = _windows_ctypes.WinDLL("ntdll", use_last_error=True)

_CreateFileW = _kernel32.CreateFileW
_CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_CreateFileW.restype = wintypes.HANDLE

_CloseHandle = _kernel32.CloseHandle
_CloseHandle.argtypes = [wintypes.HANDLE]
_CloseHandle.restype = wintypes.BOOL

_GetFileInformationByHandle = _kernel32.GetFileInformationByHandle
_GetFileInformationByHandle.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
]
_GetFileInformationByHandle.restype = wintypes.BOOL

_GetFileInformationByHandleEx = _kernel32.GetFileInformationByHandleEx
_GetFileInformationByHandleEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
]
_GetFileInformationByHandleEx.restype = wintypes.BOOL

_GetFileType = _kernel32.GetFileType
_GetFileType.argtypes = [wintypes.HANDLE]
_GetFileType.restype = wintypes.DWORD

_ReadFile = _kernel32.ReadFile
_ReadFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
_ReadFile.restype = wintypes.BOOL

_NtCreateFile = _ntdll.NtCreateFile
_NtCreateFile.argtypes = [
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.DWORD,
    ctypes.POINTER(_OBJECT_ATTRIBUTES),
    ctypes.POINTER(_IO_STATUS_BLOCK),
    wintypes.LPVOID,
    wintypes.ULONG,
    wintypes.ULONG,
    wintypes.ULONG,
    wintypes.ULONG,
    wintypes.LPVOID,
    wintypes.ULONG,
]
_NtCreateFile.restype = wintypes.LONG

_RtlNtStatusToDosError = _ntdll.RtlNtStatusToDosError
_RtlNtStatusToDosError.argtypes = [wintypes.LONG]
_RtlNtStatusToDosError.restype = wintypes.ULONG


def _as_handle(handle: int) -> wintypes.HANDLE:
    return wintypes.HANDLE(handle)


def _file_id_value(file_id: _FILE_ID_128) -> int:
    value = int.from_bytes(bytes(file_id.Identifier), "little")
    if value == 0:
        raise OSError("filesystem returned an unavailable zero 128-bit file identity")
    return value


def _win_error(prefix: str, code: int | None = None) -> OSError:
    error_code = _windows_ctypes.get_last_error() if code is None else code
    error = _windows_ctypes.WinError(error_code)
    return OSError(error.errno, f"{prefix}: {error.strerror}")


def close_handle(handle: int) -> None:
    """Close a caller-owned handle."""

    if not _CloseHandle(_as_handle(handle)):
        raise _win_error("could not close Windows handle")


def open_root(path: Path) -> int:
    """Open a bundle root itself, without traversing a final reparse point."""

    desired_access = _FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
    raw = _CreateFileW(
        str(path),
        desired_access,
        _SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    value = ctypes.cast(raw, ctypes.c_void_p).value
    if value is None or value == _INVALID_HANDLE_VALUE:
        raise _win_error(f"could not pin bundle root {str(path)!r}")
    return value


def open_relative(parent_handle: int, component: str, *, directory: bool) -> int:
    """Open exactly one child relative to an already pinned parent directory handle."""

    component_buffer = ctypes.create_unicode_buffer(component)
    component_bytes = component.encode("utf-16-le")
    unicode_name = _UNICODE_STRING(
        Length=len(component_bytes),
        MaximumLength=len(component_bytes) + 2,
        Buffer=ctypes.cast(component_buffer, wintypes.LPWSTR),
    )
    attributes = _OBJECT_ATTRIBUTES(
        Length=ctypes.sizeof(_OBJECT_ATTRIBUTES),
        RootDirectory=_as_handle(parent_handle),
        ObjectName=ctypes.pointer(unicode_name),
        Attributes=0,
        SecurityDescriptor=None,
        SecurityQualityOfService=None,
    )
    io_status = _IO_STATUS_BLOCK()
    result = wintypes.HANDLE()
    desired_access = _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
    options = _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT
    if directory:
        desired_access |= _FILE_LIST_DIRECTORY | _FILE_TRAVERSE
        options |= _FILE_DIRECTORY_FILE
    else:
        desired_access |= _FILE_READ_DATA
        options |= _FILE_NON_DIRECTORY_FILE
    status = int(
        _NtCreateFile(
            ctypes.byref(result),
            desired_access,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            0,
            _SHARE_ALL,
            _FILE_OPEN,
            options,
            None,
            0,
        )
    )
    if status < 0:
        code = int(_RtlNtStatusToDosError(status))
        raise _win_error(f"could not open contained component {component!r}", code)
    value = ctypes.cast(result, ctypes.c_void_p).value
    if value is None or value == _INVALID_HANDLE_VALUE:
        raise OSError(f"NtCreateFile returned an invalid handle for {component!r}")
    return value


def handle_info(handle: int) -> HandleInfo:
    """Read stable identity, kind, size, and mtime data from an opened handle."""

    value = _BY_HANDLE_FILE_INFORMATION()
    if not _GetFileInformationByHandle(_as_handle(handle), ctypes.byref(value)):
        raise _win_error("could not inspect Windows handle")
    identity = _FILE_ID_INFO_DATA()
    if not _GetFileInformationByHandleEx(
        _as_handle(handle), _FILE_ID_INFO, ctypes.byref(identity), ctypes.sizeof(identity)
    ):
        raise _win_error("could not read 128-bit Windows file identity")
    attributes = int(value.dwFileAttributes)
    file_id = _file_id_value(identity.FileId)
    volume = int(identity.VolumeSerialNumber)
    size = (int(value.nFileSizeHigh) << 32) | int(value.nFileSizeLow)
    mtime = (int(value.ftLastWriteTime.dwHighDateTime) << 32) | int(
        value.ftLastWriteTime.dwLowDateTime
    )
    is_directory = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)
    is_regular = not is_directory and int(_GetFileType(_as_handle(handle))) == _FILE_TYPE_DISK
    return HandleInfo(
        identity=(volume, file_id),
        attributes=attributes,
        is_directory=is_directory,
        is_regular=is_regular,
        size=size,
        mtime_token=mtime,
    )


def list_directory(handle: int) -> list[DirectoryEntry]:
    """Enumerate exact names and identities through a pinned directory handle."""

    buffer_size = 64 * 1024
    buffer = (ctypes.c_ulonglong * (buffer_size // ctypes.sizeof(ctypes.c_ulonglong)))()
    entries: list[DirectoryEntry] = []
    info_class = _FILE_ID_EXTD_DIRECTORY_RESTART_INFO
    volume = handle_info(handle).identity[0]
    while True:
        if not _GetFileInformationByHandleEx(_as_handle(handle), info_class, buffer, buffer_size):
            code = _windows_ctypes.get_last_error()
            if code == _ERROR_NO_MORE_FILES:
                break
            raise _win_error("could not enumerate pinned directory handle", code)
        offset = 0
        while True:
            if offset + _FILE_ID_EXTD_DIR_INFO.FileName.offset > buffer_size:
                raise OSError("invalid directory entry offset")
            address = ctypes.addressof(buffer) + offset
            value = ctypes.cast(address, ctypes.POINTER(_FILE_ID_EXTD_DIR_INFO)).contents
            name_end = offset + _FILE_ID_EXTD_DIR_INFO.FileName.offset + int(value.FileNameLength)
            if int(value.FileNameLength) % 2 or name_end > buffer_size:
                raise OSError("invalid directory entry name length")
            name_bytes = ctypes.string_at(
                address + _FILE_ID_EXTD_DIR_INFO.FileName.offset,
                int(value.FileNameLength),
            )
            name = name_bytes.decode("utf-16-le")
            if name not in {".", ".."}:
                file_id = _file_id_value(value.FileId)
                entries.append(
                    DirectoryEntry(
                        name=name,
                        identity=(volume, file_id),
                        attributes=int(value.FileAttributes),
                        is_directory=bool(int(value.FileAttributes) & _FILE_ATTRIBUTE_DIRECTORY),
                    )
                )
            next_offset = int(value.NextEntryOffset)
            if next_offset == 0:
                break
            if next_offset % 8 or offset + next_offset < name_end:
                raise OSError("invalid directory enumeration offset")
            offset += next_offset
            if offset >= buffer_size:
                raise OSError("invalid directory enumeration offset")
        info_class = _FILE_ID_EXTD_DIRECTORY_INFO
    return entries


def read_file(handle: int) -> bytes:
    """Read a synchronous file handle from its initial offset."""

    buffer_size = 1024 * 1024
    buffer = ctypes.create_string_buffer(buffer_size)
    chunks: list[bytes] = []
    while True:
        count = wintypes.DWORD()
        if not _ReadFile(_as_handle(handle), buffer, buffer_size, ctypes.byref(count), None):
            raise _win_error("could not read contained Windows file handle")
        length = int(count.value)
        if length == 0:
            break
        chunks.append(buffer.raw[:length])
    return b"".join(chunks)


__all__ = [
    "FILE_ATTRIBUTE_REPARSE_POINT",
    "DirectoryEntry",
    "HandleInfo",
    "close_handle",
    "handle_info",
    "list_directory",
    "open_relative",
    "open_root",
    "read_file",
]
