"""Durable directory boundaries for the web layer (plan §4.6).

The frozen ``id_detector.io.fsync_directory`` is a no-op on Windows: its callers there rely on
``MoveFileExW`` with ``MOVEFILE_WRITE_THROUGH`` for file replaces. A backup publishes a *directory*
and a recovery *deletes* files, and neither is covered by that. NTFS honours ``FlushFileBuffers`` on
a directory handle opened with ``FILE_FLAG_BACKUP_SEMANTICS`` and write access, which commits the
directory's entries. This module provides that flush, a write-through directory move, and a durable
removal. POSIX gets the ``fsync`` of a directory descriptor.
"""

from __future__ import annotations

import os
from pathlib import Path

from id_detector.io import native_path

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_ALL = 0x1 | 0x2 | 0x4  # READ | WRITE | DELETE
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_MOVEFILE_WRITE_THROUGH = 0x8


def _kernel32():  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.FlushFileBuffers.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.MoveFileExW.restype = wintypes.BOOL
    return ctypes, kernel32


def flush_directory(path: Path) -> None:
    """Commit one directory's entries to the device, on Windows as well as POSIX."""

    if os.name == "nt":  # pragma: no cover - exercised on Windows
        ctypes, kernel32 = _kernel32()
        handle = kernel32.CreateFileW(
            native_path(Path(path)),
            _GENERIC_READ | _GENERIC_WRITE,
            _FILE_SHARE_ALL,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if handle is None or handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.FlushFileBuffers(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(handle)
        return
    descriptor = os.open(native_path(Path(path)), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def move_directory(source: Path, destination: Path) -> None:
    """Move a directory to a name that does not exist yet, returning once the move is durable.

    Windows: ``MoveFileExW(..., MOVEFILE_WRITE_THROUGH)``, and never ``REPLACE_EXISTING``, so an
    existing destination is an error rather than something overwritten. Both parents are then
    flushed, because the entry left one directory and arrived in another.
    """

    source, destination = Path(source), Path(destination)
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        ctypes, kernel32 = _kernel32()
        if not kernel32.MoveFileExW(
            native_path(source), native_path(destination), _MOVEFILE_WRITE_THROUGH
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        if os.path.lexists(native_path(destination)):
            raise FileExistsError(destination)
        os.rename(native_path(source), native_path(destination))
    flush_directory(destination.parent)
    if Path(native_path(source.parent)) != Path(native_path(destination.parent)):
        flush_directory(source.parent)


def remove_file(path: Path) -> None:
    """Delete one file and make the deletion itself durable."""

    os.remove(native_path(Path(path)))
    flush_directory(Path(path).parent)


def make_directories(directory: Path) -> None:
    """``mkdir(parents=True)`` where every newly created entry is flushed into its parent."""

    directory = Path(directory)
    missing: list[Path] = []
    probe = directory
    while not Path(native_path(probe)).is_dir():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    if not missing:
        return
    Path(native_path(directory)).mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        flush_directory(created.parent)
