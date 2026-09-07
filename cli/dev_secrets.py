"""Retained leaf-file reads for launcher credentials, with trusted parents.

Parent links are traversed normally, as with POSIX O_NOFOLLOW. This is not a
repository containment or file ownership check. Validate and parse one retained
object; never reopen its pathname. Windows additionally denies write/delete
sharing while that object is retained.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

if sys.platform == "win32":
    import msvcrt

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import TextIO

_UNSAFE_FILE = "secrets file must be a regular file without a link or reparse point"
_UNAVAILABLE = "secure secrets-file opening is unavailable on this platform"
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x1
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_TYPE_DISK = 1
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class SecretsFileError(RuntimeError):
    """A fixed, content-free failure to acquire a safe credential stream."""


class _NativeFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(self, *args: object) -> int | None: ...


@dataclass(frozen=True)
class _WindowsApi:
    create_file: _NativeFunction
    get_file_type: _NativeFunction
    close_handle: _NativeFunction

    @classmethod
    def load(cls) -> _WindowsApi:
        """Bind pointer-sized handles and explicit Win32 calling conventions."""
        loader = getattr(ctypes, "WinDLL", None)
        if not callable(loader):
            raise SecretsFileError(_UNAVAILABLE)
        try:
            kernel32 = loader("kernel32", use_last_error=True)
            create = cast("_NativeFunction", kernel32.CreateFileW)
            file_type = cast("_NativeFunction", kernel32.GetFileType)
            close = cast("_NativeFunction", kernel32.CloseHandle)
        except (OSError, AttributeError):
            raise SecretsFileError(_UNAVAILABLE) from None
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
        file_type.argtypes = [ctypes.c_void_p]
        file_type.restype = ctypes.c_uint32
        close.argtypes = [ctypes.c_void_p]
        close.restype = ctypes.c_int
        return cls(create, file_type, close)


def _open_windows_descriptor(path: Path) -> int:
    if sys.platform != "win32":
        raise SecretsFileError(_UNAVAILABLE)
    spelling = str(path).replace("/", "\\")
    drive, _tail = ntpath.splitdrive(spelling)
    ipc_share = drive.startswith("\\\\") and drive.rsplit("\\", 1)[-1].casefold() in {
        "ipc$",
        "pipe",
        "mailslot",
    }
    if (
        spelling.startswith(("\\\\?\\", "\\\\.\\"))
        or ntpath.isreserved(spelling)
        or ipc_share
    ):
        raise SecretsFileError(_UNSAFE_FILE)
    api = _WindowsApi.load()
    handle = api.create_file(
        spelling,
        _GENERIC_READ,
        _FILE_SHARE_READ,
        None,  # Non-inheritable handle; no security descriptor override.
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle is None or handle == _INVALID_HANDLE:
        raise SecretsFileError(_UNSAFE_FILE)
    try:
        if api.get_file_type(handle) != _FILE_TYPE_DISK:
            raise SecretsFileError(_UNSAFE_FILE)
        convert = getattr(msvcrt, "open_osfhandle", None)
        no_inherit = getattr(os, "O_NOINHERIT", None)
        if not callable(convert) or not isinstance(no_inherit, int):
            raise SecretsFileError(_UNAVAILABLE)
        descriptor: int = convert(handle, os.O_RDONLY | no_inherit)
        handle = None  # The CRT descriptor now owns the native handle.
        return descriptor
    finally:
        if handle is not None:
            api.close_handle(handle)


def _open_posix_descriptor(path: Path) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    nonblock = getattr(os, "O_NONBLOCK", None)
    if not isinstance(no_follow, int) or not no_follow or not isinstance(nonblock, int):
        raise SecretsFileError(_UNAVAILABLE)
    # A FIFO must reach fstat without blocking for a writer.
    return os.open(path, os.O_RDONLY | no_follow | nonblock)


def _validate_descriptor(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise SecretsFileError(_UNSAFE_FILE)
    if sys.platform == "win32":
        attributes = getattr(metadata, "st_file_attributes", None)
        if not isinstance(attributes, int):
            raise SecretsFileError(_UNAVAILABLE)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise SecretsFileError(_UNSAFE_FILE)


@contextmanager
def open_secrets_file(path: Path) -> Iterator[TextIO]:
    """Yield UTF-8 text from one validated, non-inheritable retained descriptor."""
    descriptor: int | None = None
    try:
        try:
            descriptor = (
                _open_windows_descriptor(path)
                if sys.platform == "win32"
                else _open_posix_descriptor(path)
            )
            _validate_descriptor(descriptor)
            stream = os.fdopen(descriptor, mode="r", encoding="utf-8")
        except OSError:
            raise SecretsFileError(_UNSAFE_FILE) from None
        descriptor = None  # The stream now owns the CRT/POSIX descriptor.
        with stream:
            yield stream
    finally:
        if descriptor is not None:
            os.close(descriptor)
