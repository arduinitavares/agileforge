"""Authoritative installed-build identity loaded from packaged metadata."""

from __future__ import annotations

import re
import stat
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

BUILD_IDENTITY_PATH = Path("/opt/agileforge/build.json")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class BuildIdentityError(RuntimeError):
    """Packaged build identity is missing, unsafe, or invalid."""


class BuildIdentity(BaseModel):
    """Versioned immutable provenance for one installed AgileForge build."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["agileforge.build.v1"]
    revision: str
    source_sha256: str
    lock_sha256: str
    python_version: str
    uv_version: str
    package_version: str

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        """Reject abbreviated revisions, malformed hashes, and empty versions."""
        if _COMMIT_PATTERN.fullmatch(self.revision) is None:
            message = "revision must be a 40-character lowercase Git SHA"
            raise ValueError(message)
        for field_name in ("source_sha256", "lock_sha256"):
            value = getattr(self, field_name)
            if _HASH_PATTERN.fullmatch(value) is None:
                message = f"{field_name} must be a 64-character lowercase SHA-256"
                raise ValueError(message)
        for field_name in ("python_version", "uv_version", "package_version"):
            value = getattr(self, field_name)
            if not value or value != value.strip():
                message = f"{field_name} must be a nonempty normalized version"
                raise ValueError(message)
        return self


def _validate_identity_file(path: Path, *, expected_owner_uid: int) -> None:
    if not path.is_absolute():
        message = "build identity path must be absolute"
        raise BuildIdentityError(message)
    try:
        metadata = path.lstat()
    except OSError as error:
        message = f"build identity is unavailable: {path}"
        raise BuildIdentityError(message) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        message = f"build identity must be a regular file: {path}"
        raise BuildIdentityError(message)
    if metadata.st_uid != expected_owner_uid:
        message = "build identity has an unexpected owner"
        raise BuildIdentityError(message)
    if metadata.st_mode & 0o222:
        message = "build identity must not be writable"
        raise BuildIdentityError(message)
    try:
        canonical = path.resolve(strict=True)
    except OSError as error:
        message = f"build identity is unavailable: {path}"
        raise BuildIdentityError(message) from error
    if canonical != path:
        message = "build identity path must not contain links"
        raise BuildIdentityError(message)


def load_build_identity(
    path: Path = BUILD_IDENTITY_PATH,
    *,
    expected_owner_uid: int = 0,
) -> BuildIdentity:
    """Load the one validated packaged identity record.

    ``path`` and ``expected_owner_uid`` are injectable for isolated tests. Runtime
    callers use the fixed root-owned ``/opt/agileforge/build.json`` default.
    """
    absolute_path = path.absolute()
    _validate_identity_file(absolute_path, expected_owner_uid=expected_owner_uid)
    try:
        payload = absolute_path.read_bytes()
        return BuildIdentity.model_validate_json(payload)
    except (OSError, ValidationError) as error:
        message = "build identity record is invalid"
        raise BuildIdentityError(message) from error


def runtime_build_identity() -> BuildIdentity:
    """Load production identity from the fixed root-owned packaged record."""
    return load_build_identity(BUILD_IDENTITY_PATH, expected_owner_uid=0)


__all__ = [
    "BUILD_IDENTITY_PATH",
    "BuildIdentity",
    "BuildIdentityError",
    "load_build_identity",
    "runtime_build_identity",
]
