"""Namespace-canonical, link-refusing paths for corpus truth records and their sibling files.

Two defects in the owner truth tool motivated this module
(``docs/reviews/followup-truth-corpus.md``):

* **spelling** -- ``\\\\?\\C:\\...`` and ``C:\\...`` name one file, but ``os.path.realpath``
  keeps a Win32 namespace prefix, so a work-tree containment check and the cross-process lock key
  each compared two different strings for the same record;
* **links** -- ``io.atomic_write_bytes`` resolves its destination first, so a sibling such as
  ``review-exposure.json`` that was really a symlink into ``work/`` was written *through*.

Every corpus-set write therefore goes through :func:`pinned_set_directory`.  The set directory is
opened and held for the whole write -- on Windows with list access and without
``FILE_SHARE_DELETE``, which stops that directory and each of its ancestors from being renamed,
deleted or swapped for a junction while it is held; on POSIX as a directory descriptor that every
operation is rooted at.  Its real location, read back from the handle itself, is checked against
the location the caller expects and against the work tree; a destination that is itself a link is
refused; temporary files are created exclusively inside the held directory; and the final rename
replaces a directory entry, never following one.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
#: Reparse tags with this bit (symlinks, junctions) redirect a name; cloud placeholders do not.
_REPARSE_TAG_NAME_SURROGATE = 0x20000000


def strip_namespace_prefix(value: str) -> str:
    """Remove a Win32 namespace prefix: ``\\\\?\\``, ``\\\\.\\`` and their ``UNC`` forms.

    ``\\\\?\\C:\\x`` becomes ``C:\\x`` and ``\\\\?\\UNC\\server\\share`` becomes
    ``\\\\server\\share``.  A device or volume-GUID path has no drive-letter spelling and is
    returned unchanged, as is any path without a prefix.
    """

    text = value
    if text.startswith(("//?/", "//./")):
        text = text.replace("/", "\\")
    for prefix in ("\\\\?\\", "\\\\.\\"):
        if not text.startswith(prefix):
            continue
        rest = text[len(prefix) :]
        if rest[:4].upper() == "UNC\\":
            return "\\\\" + rest[4:]
        if len(rest) >= 2 and rest[1] == ":" and rest[0].isalpha():
            return rest
        return value
    return value


def real_path(path: Path | str) -> Path:
    """The fully resolved, prefix-free spelling of ``path``.

    ``realpath`` follows symlinks and, on Windows, junctions, substituted drives and 8.3 short
    names; stripping the namespace prefix on both sides of it makes ``\\\\?\\`` spellings agree
    with ordinary ones.
    """

    raw = os.fspath(path)
    if os.name == "nt":
        raw = strip_namespace_prefix(raw)
    resolved = os.path.realpath(raw)
    if os.name == "nt":
        resolved = strip_namespace_prefix(resolved)
    return Path(resolved)


def path_key(path: Path | str) -> str:
    """One comparison -- and lock-identity -- string per file, whatever spelling named it."""

    return os.path.normcase(str(real_path(path)))


def is_within(path: Path | str, root: Path | str) -> bool:
    """Whether ``path`` resolves to ``root`` or somewhere beneath it."""

    child, parent = Path(path_key(path)), Path(path_key(root))
    return child == parent or child.is_relative_to(parent)


def _long(path: Path | str) -> str:
    """An absolute spelling Win32 accepts past MAX_PATH, *without* resolving any link."""

    text = os.path.abspath(os.fspath(path))
    if os.name != "nt" or text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def is_link(path: Path | str) -> bool:
    """A symlink, junction or other name-redirecting reparse point; decided without following."""

    try:
        info = os.lstat(_long(path))
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    tag = getattr(info, "st_reparse_tag", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT and tag & _REPARSE_TAG_NAME_SURROGATE)


def _lexical(path: Path | str) -> Path:
    raw = os.fspath(path)
    if os.name == "nt":
        raw = strip_namespace_prefix(raw)
    return Path(os.path.abspath(raw))


def link_components(path: Path | str) -> list[Path]:
    """Every *existing* component of ``path``'s absolute spelling that is a symlink or junction.

    Resolving a path silently follows these; a corpus destination instead refuses them, so a
    directory that was replaced by a link is never mistaken for the one that was validated.
    """

    absolute = _lexical(path)
    current = Path(absolute.anchor)
    found: list[Path] = []
    for part in absolute.parts[1:]:
        current = current / part
        if not os.path.lexists(_long(current)):
            break
        if is_link(current):
            found.append(current)
    return found


def nearest_existing_ancestor(path: Path | str) -> Path:
    """The deepest existing directory on ``path``'s absolute spelling (``path`` itself if it is)."""

    current = _lexical(path)
    while not os.path.lexists(_long(current)) and current.parent != current:
        current = current.parent
    return current


def _plain_name(name: str) -> str:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ":" in name
        or "\x00" in name
    ):
        raise ValueError(f"not a plain file name inside a set directory: {name!r}")
    return name


def link_refusal(path: Path | str, link: Path | str) -> ValueError:
    """The one refusal for a link met on a corpus path: say what was refused and what to do."""

    return ValueError(
        f"refusing to use {path}: {link} is a link (symlink or junction). Links on corpus paths "
        "are refused, never followed, so the location that is checked is the location that is "
        "used. Pass the resolved real path instead (for example the output of Resolve-Path or "
        "realpath), and keep corpus records as real files in their set directory."
    )


def refuse_link_components(path: Path | str) -> None:
    """Raise :func:`link_refusal` for the first existing link component of ``path``, if any."""

    links = link_components(path)
    if links:
        raise link_refusal(path, links[0])


def _link_refusal(target: Path) -> ValueError:
    return link_refusal(target, target)


class PinnedDirectory:
    """A corpus set directory held open for the duration of a write; see the module docstring."""

    def __init__(self, path: Path, *, handle: Any = None, fd: int | None = None) -> None:
        self.path = path
        self._handle = handle
        self._fd = fd

    def _target(self, name: str) -> Path:
        target = self.path / _plain_name(name)
        if self._fd is not None:
            try:
                info = os.lstat(name, dir_fd=self._fd)
            except FileNotFoundError:
                return target
            if stat.S_ISLNK(info.st_mode):
                raise _link_refusal(target)
            return target
        if is_link(target):
            raise _link_refusal(target)
        return target

    def exists(self, name: str) -> bool:
        target = self._target(name)
        if self._fd is not None:
            try:
                os.lstat(name, dir_fd=self._fd)
            except FileNotFoundError:
                return False
            return True
        return os.path.lexists(_long(target))

    def read_bytes(self, name: str) -> bytes | None:
        """The file's bytes, ``None`` when absent; a link is refused, never read through."""

        target = self._target(name)
        if self._fd is not None:
            try:
                descriptor = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self._fd
                )
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise _link_refusal(target) from exc
            with os.fdopen(descriptor, "rb") as handle:
                return handle.read()
        try:
            handle = open(_long(target), "rb")  # noqa: SIM115 - identity checked before reading
        except FileNotFoundError:
            return None
        with handle:
            opened = os.fstat(handle.fileno())
            named = os.lstat(_long(target))
            if is_link(target) or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                raise _link_refusal(target)
            return handle.read()

    def write_bytes(self, name: str, content: bytes) -> None:
        """Atomically replace ``name`` inside the held directory, through no link."""

        target = self._target(name)
        temporary = f".{name}.{secrets.token_hex(8)}.tmp"
        if self._fd is not None:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o644,
                dir_fd=self._fd,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._target(name)
                os.replace(temporary, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
                os.fsync(self._fd)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=self._fd)
                raise
            return
        temporary_path = self.path / temporary
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(_long(temporary_path), flags | getattr(os, "O_NOINHERIT", 0), 0o644)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self._target(name)
            # MoveFileEx replaces the destination *entry*: were it swapped for a link after the
            # check above, the link itself is replaced, and nothing is written through it.
            _move_replace_nt(_long(temporary_path), _long(target))
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(_long(temporary_path))
            raise

    @contextmanager
    def hold_undeletable(self, name: str) -> Iterator[bytes]:
        """Yield ``name``'s bytes while holding it open so that it cannot be deleted or renamed.

        On Windows the file is opened without ``FILE_SHARE_DELETE`` (and without following a
        reparse point): until the block exits, no process -- including one that ignores the
        advisory truth lock -- can unlink or rename it.  POSIX has no deletion-denying open, so
        there the file is only held open by descriptor (see the residual-risk note in the record).
        A missing file raises ``FileNotFoundError``; a link is refused.
        """

        target = self._target(name)
        if self._fd is not None:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self._fd)
            with os.fdopen(descriptor, "rb") as stream:
                yield stream.read()
            return
        import msvcrt

        handle = _open_file_no_delete_nt(_long(target))
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except BaseException:
            _close_nt(handle)
            raise
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            named = os.lstat(_long(target))
            if is_link(target) or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                raise _link_refusal(target)
            yield stream.read()

    def remove(self, name: str) -> None:
        self._target(name)
        with contextlib.suppress(FileNotFoundError):
            if self._fd is not None:
                os.unlink(name, dir_fd=self._fd)
            else:
                os.unlink(_long(self.path / name))


def _kernel32() -> Any:
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)


def _move_replace_nt(source: str, destination: str) -> None:
    import ctypes

    move = _kernel32().MoveFileExW
    move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    move.restype = ctypes.c_int
    # REPLACE_EXISTING | WRITE_THROUGH: Windows has no POSIX directory fsync.
    if not move(source, destination, 0x1 | 0x8):
        raise ctypes.WinError(ctypes.get_last_error())


def _open_file_no_delete_nt(path: str) -> int:
    """A read handle on ``path`` that denies deletion and rename while it is open."""

    import ctypes

    create = _kernel32().CreateFileW
    create.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create.restype = ctypes.c_void_p
    generic_read = 0x80000000
    share_read_write = 0x1 | 0x2  # deliberately *not* FILE_SHARE_DELETE
    open_existing, open_reparse_point = 3, 0x00200000
    handle = create(
        path, generic_read, share_read_write, None, open_existing, open_reparse_point, None
    )
    if handle is None or handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in {2, 3}:  # ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND
            raise FileNotFoundError(error, "exposure evidence is missing", path)
        raise ctypes.WinError(error)
    return int(handle)


def _open_directory_nt(path: Path) -> tuple[Any, Path]:
    """Hold ``path`` open without delete sharing and return where the handle really points."""

    import ctypes

    kernel32 = _kernel32()
    create = kernel32.CreateFileW
    create.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create.restype = ctypes.c_void_p
    file_list_directory, file_read_attributes = 0x1, 0x80
    share_read_write = 0x1 | 0x2  # deliberately *not* FILE_SHARE_DELETE
    open_existing, backup_semantics = 3, 0x02000000
    handle = create(
        _long(path),
        file_list_directory | file_read_attributes,
        share_read_write,
        None,
        open_existing,
        backup_semantics,
        None,
    )
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        final = kernel32.GetFinalPathNameByHandleW
        final.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        final.restype = ctypes.c_uint32
        size = 32_768
        buffer = ctypes.create_unicode_buffer(size)
        length = final(handle, buffer, size, 0)
        if length == 0 or length >= size:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle, Path(strip_namespace_prefix(buffer.value))
    except BaseException:
        _close_nt(handle)
        raise


def _close_nt(handle: Any) -> None:
    import ctypes

    close = _kernel32().CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close(handle)


def _check_location(
    requested: Path, actual: Path, expected_key: str | None, work_root: Path | None
) -> None:
    if work_root is not None and is_within(actual, work_root):
        raise ValueError(
            f"refusing to write a truth record beneath the work tree: {requested} resolves inside "
            f"{work_root}"
        )
    if expected_key is not None and os.path.normcase(str(actual)) != expected_key:
        raise link_refusal(
            requested, f"{requested} (moved or replaced since it was checked; now {actual})"
        )


@contextmanager
def pinned_set_directory(
    directory: Path, *, expected_key: str | None = None, work_root: Path | None = None
) -> Iterator[PinnedDirectory]:
    """Hold a corpus set directory for a write and verify where it really is, first.

    ``expected_key`` is the :func:`path_key` the caller resolved and vetted earlier (for a review
    session, when it opened); a directory that now resolves anywhere else is refused, as is one
    beneath ``work_root``.
    """

    if os.name == "nt":
        handle, actual = _open_directory_nt(real_path(directory))
        try:
            _check_location(directory, actual, expected_key, work_root)
            yield PinnedDirectory(actual, handle=handle)
        finally:
            _close_nt(handle)
        return
    actual = real_path(directory)
    descriptor = os.open(actual, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        named = os.stat(actual)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise ValueError(
                f"refusing to write: the set directory {directory} changed while opening"
            )
        _check_location(directory, actual, expected_key, work_root)
        yield PinnedDirectory(actual, fd=descriptor)
    finally:
        os.close(descriptor)
