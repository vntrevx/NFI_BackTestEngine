"""Windows handle-based containment for explicit evidence files."""

from __future__ import annotations

import ctypes
import ntpath
import os
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import InputBoundaryError

_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_GENERIC_READ = 0x80000000
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_FILE_TYPE_DISK = 0x0001


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_wchar_p),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ulong),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", ctypes.c_ulong),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]


class _FileTime(ctypes.Structure):
    _fields_ = [("LowDateTime", ctypes.c_uint32), ("HighDateTime", ctypes.c_uint32)]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", ctypes.c_uint32),
        ("CreationTime", _FileTime),
        ("LastAccessTime", _FileTime),
        ("LastWriteTime", _FileTime),
        ("VolumeSerialNumber", ctypes.c_uint32),
        ("FileSizeHigh", ctypes.c_uint32),
        ("FileSizeLow", ctypes.c_uint32),
        ("NumberOfLinks", ctypes.c_uint32),
        ("FileIndexHigh", ctypes.c_uint32),
        ("FileIndexLow", ctypes.c_uint32),
    ]


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", ctypes.c_uint32),
        ("ReparseTag", ctypes.c_uint32),
    ]


def windows_path_is_contained(root_final_path: str, file_final_path: str) -> bool:
    """Compare canonical handle paths using Windows case and volume semantics."""
    root = _normalize_handle_path(root_final_path)
    candidate = _normalize_handle_path(file_final_path)
    try:
        return ntpath.commonpath([root, candidate]) == root and candidate != root
    except ValueError:
        return False


def validate_windows_file_attributes(attributes: int) -> None:
    """Reject final reparse points and non-regular Windows filesystem objects."""
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise InputBoundaryError("explicit evidence file is a Windows reparse point")
    if attributes & _FILE_ATTRIBUTE_DIRECTORY:
        raise InputBoundaryError("explicit evidence file is not a regular file")


def open_windows_locked_executable_descriptor(path: Path) -> int:
    """Retain a read handle that denies executable replacement through launch."""
    if os.name != "nt":
        raise InputBoundaryError("Windows executable locking is unavailable")
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    _configure_kernel32(kernel32)
    handle = kernel32.CreateFileW(
        str(path),
        _GENERIC_READ,
        _FILE_SHARE_READ,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise InputBoundaryError(
            f"Windows refused locked executable handle: {ctypes.get_last_error()}"
        )
    try:
        validate_windows_file_attributes(_file_attributes(kernel32, int(handle)))
        if kernel32.GetFileType(handle) != _FILE_TYPE_DISK:
            raise InputBoundaryError("trusted executable handle is not a disk file")
        import msvcrt

        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY)
        handle = None
        return descriptor
    finally:
        if handle is not None:
            kernel32.CloseHandle(handle)


def windows_root_identity(root: Path) -> tuple[str, tuple[int, int, int]]:
    """Capture canonical identity for later substitution checks."""
    if os.name != "nt":
        raise InputBoundaryError("Windows root identity is unavailable")
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    _configure_kernel32(kernel32)
    handle = _create_file(
        kernel32,
        str(root),
        access=0,
        flags=_FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
    )
    try:
        attributes = _file_attributes(kernel32, handle)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT or not attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise InputBoundaryError("trusted Windows root is not a regular directory")
        return _final_path(kernel32, handle), _file_identity(kernel32, handle)
    finally:
        kernel32.CloseHandle(handle)


def open_windows_contained_descriptor(
    root: Path,
    relative_name: str,
    *,
    expected_root_identity: tuple[str, tuple[int, int, int]] | None = None,
) -> int:
    """Open without following the final reparse point and prove handle containment."""
    if os.name != "nt":
        raise InputBoundaryError("Windows handle containment is unavailable on this platform")
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll: Any = ctypes.WinDLL("ntdll")
    _configure_kernel32(kernel32)
    _configure_ntdll(ntdll)
    root_handle = _create_file(
        kernel32,
        str(root),
        access=0,
        flags=_FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
    )
    file_handle: int | None = None
    try:
        root_attributes = _file_attributes(kernel32, root_handle)
        if root_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise InputBoundaryError("trusted evidence root is a Windows reparse point")
        if not root_attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise InputBoundaryError("trusted evidence root is not a directory")
        root_final = _final_path(kernel32, root_handle)
        if expected_root_identity is not None and (
            _normalize_handle_path(root_final)
            != _normalize_handle_path(expected_root_identity[0])
            or _file_identity(kernel32, root_handle) != expected_root_identity[1]
        ):
            raise InputBoundaryError("trusted Windows root identity changed")
        parts = PurePosixPath(relative_name).parts
        parent_handles: list[int] = []
        try:
            current_parent = root_handle
            walked: list[str] = []
            for component in parts[:-1]:
                parent_handle = _nt_open_relative(
                    ntdll, current_parent, component, directory=True
                )
                try:
                    walked.append(component)
                    _validate_windows_parent_handle(
                        kernel32,
                        parent_handle,
                        root_final=root_final,
                        expected_relative="/".join(walked),
                    )
                    parent_handles.append(parent_handle)
                    current_parent = parent_handle
                except BaseException:
                    kernel32.CloseHandle(parent_handle)
                    raise
            file_handle = _nt_open_relative(
                ntdll, current_parent, parts[-1], directory=False
            )
            validate_windows_file_attributes(_file_attributes(kernel32, file_handle))
            if kernel32.GetFileType(file_handle) != _FILE_TYPE_DISK:
                raise InputBoundaryError("explicit evidence handle is not a disk file")
            file_final = _final_path(kernel32, file_handle)
            if not windows_path_is_contained(root_final, file_final):
                raise InputBoundaryError(
                    "explicit evidence file handle resolves outside the trusted root"
                )
            if not _windows_handle_matches_relative(root_final, relative_name, file_final):
                raise InputBoundaryError(
                    "explicit evidence file identity or name changed during traversal"
                )
            import msvcrt

            descriptor = msvcrt.open_osfhandle(file_handle, os.O_RDONLY)
            file_handle = None  # ownership transferred to the CRT descriptor
            return descriptor
        finally:
            for parent_handle in reversed(parent_handles):
                kernel32.CloseHandle(parent_handle)
    finally:
        if file_handle is not None:
            kernel32.CloseHandle(file_handle)
        kernel32.CloseHandle(root_handle)


def _configure_kernel32(kernel32: Any) -> None:
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.GetFileInformationByHandleEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.GetFileInformationByHandleEx.restype = ctypes.c_int
    kernel32.GetFileInformationByHandle.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    kernel32.GetFileInformationByHandle.restype = ctypes.c_int
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    kernel32.GetFinalPathNameByHandleW.restype = ctypes.c_uint32
    kernel32.GetFileType.argtypes = [ctypes.c_void_p]
    kernel32.GetFileType.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int


def _configure_ntdll(ntdll: Any) -> None:
    ntdll.NtCreateFile.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_ulong,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    ntdll.NtCreateFile.restype = ctypes.c_long


def _nt_open_relative(
    ntdll: Any, parent_handle: int, component: str, *, directory: bool
) -> int:
    buffer = ctypes.create_unicode_buffer(component)
    name = _UnicodeString(
        len(component.encode("utf-16-le")),
        len(buffer) * 2,
        ctypes.cast(buffer, ctypes.c_wchar_p),
    )
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        ctypes.c_void_p(parent_handle),
        ctypes.pointer(name),
        0x40,
        None,
        None,
    )
    io_status = _IoStatusBlock()
    handle = ctypes.c_void_p()
    access = 0x00100080 | (_GENERIC_READ if not directory else 0)
    options = 0x00200020 | (0x00000001 if directory else 0x00000040)
    status = ntdll.NtCreateFile(
        ctypes.byref(handle),
        access,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        0,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        1,
        options,
        None,
        0,
    )
    if status < 0 or handle.value is None:
        raise InputBoundaryError(
            f"Windows refused parent-relative no-follow open: 0x{status & 0xFFFFFFFF:08x}"
        )
    return int(handle.value)


def _create_file(
    kernel32: Any,
    path: str,
    *,
    access: int,
    flags: int,
) -> int:
    handle = kernel32.CreateFileW(
        path,
        access,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        flags,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise InputBoundaryError(
            f"Windows refused no-follow evidence handle: {ctypes.get_last_error()}"
        )
    return int(handle)


def _file_attributes(kernel32: Any, handle: int) -> int:
    info = _FileAttributeTagInfo()
    if not kernel32.GetFileInformationByHandleEx(
        handle,
        _FILE_ATTRIBUTE_TAG_INFO_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        raise InputBoundaryError(
            f"cannot inspect Windows evidence handle: {ctypes.get_last_error()}"
        )
    return int(info.FileAttributes)


def _validate_windows_parent_handle(
    kernel32: Any,
    handle: int,
    *,
    root_final: str,
    expected_relative: str,
) -> None:
    attributes = _file_attributes(kernel32, handle)
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise InputBoundaryError("explicit evidence parent is a Windows reparse point")
    if not attributes & _FILE_ATTRIBUTE_DIRECTORY:
        raise InputBoundaryError("explicit evidence parent is not a directory")
    final_path = _final_path(kernel32, handle)
    if not windows_path_is_contained(root_final, final_path):
        raise InputBoundaryError("explicit evidence parent resolves outside the trusted root")
    if not _windows_handle_matches_relative(root_final, expected_relative, final_path):
        raise InputBoundaryError(
            "explicit evidence parent identity or name changed during traversal"
        )


def _windows_handle_matches_relative(
    root_final: str, relative_name: str, candidate_final: str
) -> bool:
    expected = ntpath.join(
        _normalize_handle_path(root_final),
        *PurePosixPath(relative_name).parts,
    )
    return ntpath.normcase(ntpath.normpath(expected)) == _normalize_handle_path(
        candidate_final
    )


def _file_identity(kernel32: Any, handle: int) -> tuple[int, int, int]:
    info = _ByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise InputBoundaryError(
            f"cannot identify Windows evidence handle: {ctypes.get_last_error()}"
        )
    return (
        int(info.VolumeSerialNumber),
        int(info.FileIndexHigh),
        int(info.FileIndexLow),
    )


def _final_path(kernel32: Any, handle: int) -> str:
    required = kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
    if not required:
        raise InputBoundaryError(
            f"cannot resolve Windows evidence handle: {ctypes.get_last_error()}"
        )
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise InputBoundaryError(
            f"cannot resolve Windows evidence handle: {ctypes.get_last_error()}"
        )
    return buffer.value


def _normalize_handle_path(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return ntpath.normcase(ntpath.normpath(value)).rstrip("\\")
