"""Retained leaf-file reads for launcher credentials, with trusted parents.

Parent links are traversed normally, as with POSIX O_NOFOLLOW. This is not a
repository containment or file ownership check. Validate and parse one retained
object; never reopen its pathname.
"""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import TextIO

_UNSAFE_FILE = "secrets file must be a regular file without a link or reparse point"
_UNAVAILABLE = "secure secrets-file opening is unavailable on this platform"


class SecretsFileError(RuntimeError):
    """A fixed, content-free failure to acquire a safe credential stream."""


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


@contextmanager
def open_secrets_file(path: Path) -> Iterator[TextIO]:
    """Yield UTF-8 text from one validated, non-inheritable retained descriptor."""
    descriptor: int | None = None
    try:
        try:
            descriptor = _open_posix_descriptor(path)
            _validate_descriptor(descriptor)
            stream = os.fdopen(descriptor, mode="r", encoding="utf-8")
        except OSError:
            raise SecretsFileError(_UNSAFE_FILE) from None
        descriptor = None  # The stream now owns the POSIX descriptor.
        with stream:
            yield stream
    finally:
        if descriptor is not None:
            os.close(descriptor)
