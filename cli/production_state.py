"""Durable installed-runtime state with strict owned-path validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess  # nosec B404
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from cli.state_transfer import (
    TransferError,
    verify_current_business_schema,
    verify_current_trace_schema,
)
from utils.runtime_controls import LAUNCHER_CHILD_ENV, LAUNCHER_CHILD_VALUE

if TYPE_CHECKING:
    from collections.abc import Callable

    from utils.build_identity import BuildIdentity

PRODUCTION_STATE_BASE = Path("/var/lib/agileforge/profiles")
_PROFILE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


class ProductionStateError(RuntimeError):
    """Installed state is missing, partial, unsafe, or incompatible."""


def _effective_uid() -> int:
    getter = cast("Callable[[], int] | None", getattr(os, "geteuid", None))
    if getter is None:
        message = "production state requires POSIX user ownership"
        raise ProductionStateError(message)
    return getter()


@dataclass(frozen=True, slots=True)
class ProductionStatePaths:
    """Reserved paths owned by one installed production profile."""

    root: Path
    manifest: Path
    business_database: Path
    trace_database: Path
    artifacts: Path
    config_directory: Path
    model_config: Path
    repository_relocations: Path


class ProductionStateManifest(BaseModel):
    """Published, non-secret identity and layout for durable state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["agileforge.production-state.v1"]
    state_id: UUID
    profile_name: str
    profile_root: Path
    business_database: Path
    trace_database: Path
    trace_database_present: bool
    artifacts: Path
    model_config_path: Path
    model_config_sha256: str
    business_schema_sha256: str
    created_at: datetime
    created_by_build_revision: str
    provenance: Literal["initialized", "restored"]
    source_backup_sha256: str | None
    repository_relocations_sha256: str | None

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:  # noqa: C901, PLR0912
        """Reject malformed identity, hashes, provenance, and aliased names."""
        if _PROFILE_PATTERN.fullmatch(self.profile_name) is None:
            message = "invalid production profile name"
            raise ValueError(message)
        if not self.profile_root.is_absolute():
            message = "production profile root must be absolute"
            raise ValueError(message)
        for value in (
            self.business_database,
            self.trace_database,
            self.artifacts,
            self.model_config_path,
        ):
            if not value.is_absolute():
                message = "production state paths must be absolute"
                raise ValueError(message)
        if self.business_database == self.trace_database:
            message = "production database paths must be distinct"
            raise ValueError(message)
        for field_name in ("model_config_sha256", "business_schema_sha256"):
            if _HASH_PATTERN.fullmatch(getattr(self, field_name)) is None:
                message = f"{field_name} must be a lowercase SHA-256 value"
                raise ValueError(message)
        if _COMMIT_PATTERN.fullmatch(self.created_by_build_revision) is None:
            message = "created_by_build_revision must be a full lowercase Git SHA"
            raise ValueError(message)
        if self.created_at.tzinfo is None:
            message = "created_at must be timezone-aware"
            raise ValueError(message)
        if self.provenance == "initialized" and self.source_backup_sha256 is not None:
            message = "initialized state must not name a source backup"
            raise ValueError(message)
        if self.provenance == "restored" and (
            self.source_backup_sha256 is None
            or _HASH_PATTERN.fullmatch(self.source_backup_sha256) is None
        ):
            message = "restored state requires a source backup SHA-256"
            raise ValueError(message)
        if (
            self.repository_relocations_sha256 is not None
            and _HASH_PATTERN.fullmatch(self.repository_relocations_sha256) is None
        ):
            message = "repository_relocations_sha256 must be a lowercase SHA-256 value"
            raise ValueError(message)
        if (
            self.provenance == "initialized"
            and self.repository_relocations_sha256 is not None
        ):
            message = "initialized state must not have repository relocations"
            raise ValueError(message)
        return self


def production_profile_root(profile_name: str) -> Path:
    """Return the fixed production root for a validated profile name."""
    if _PROFILE_PATTERN.fullmatch(profile_name) is None:
        message = f"invalid production profile name: {profile_name!r}"
        raise ProductionStateError(message)
    return PRODUCTION_STATE_BASE / profile_name


def production_state_paths(profile_root: Path) -> ProductionStatePaths:
    """Build the exact layout for an absolute production profile root."""
    root = profile_root.absolute()
    if not profile_root.is_absolute():
        message = "production profile root must be absolute"
        raise ProductionStateError(message)
    if _PROFILE_PATTERN.fullmatch(root.name) is None:
        message = "invalid production profile name"
        raise ProductionStateError(message)
    config_directory = root / "config"
    return ProductionStatePaths(
        root=root,
        manifest=root / "runtime.json",
        business_database=root / "business.sqlite3",
        trace_database=root / "adk-trace.sqlite3",
        artifacts=root / "artifacts",
        config_directory=config_directory,
        model_config=config_directory / "models.yaml",
        repository_relocations=root / "repository-relocations.json",
    )


def _lstat(path: Path, *, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as error:
        message = f"{label} is missing or unavailable: {path}"
        raise ProductionStateError(message) from error


def _require_owned_directory(
    path: Path,
    *,
    label: str,
    expected_owner_uid: int,
) -> os.stat_result:
    metadata = _lstat(path, label=label)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        message = f"{label} must be a real directory: {path}"
        raise ProductionStateError(message)
    if metadata.st_uid != expected_owner_uid:
        message = f"{label} has an unexpected owner"
        raise ProductionStateError(message)
    if metadata.st_mode & 0o022:
        message = f"{label} must not be group/world writable"
        raise ProductionStateError(message)
    if path.resolve(strict=True) != path:
        message = f"{label} path must not contain links"
        raise ProductionStateError(message)
    return metadata


def _require_owned_regular_file(
    path: Path,
    *,
    label: str,
    expected_owner_uid: int,
) -> os.stat_result:
    metadata = _lstat(path, label=label)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        message = f"{label} must be a regular file: {path}"
        raise ProductionStateError(message)
    if metadata.st_uid != expected_owner_uid:
        message = f"{label} has an unexpected owner"
        raise ProductionStateError(message)
    if metadata.st_mode & 0o022:
        message = f"{label} must not be group/world writable"
        raise ProductionStateError(message)
    if path.resolve(strict=True) != path:
        message = f"{label} path must not contain links"
        raise ProductionStateError(message)
    return metadata


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def database_schema_sha256(path: Path) -> str:
    """Hash one SQLite database's complete durable schema definition."""
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%' "
                "ORDER BY type, name, tbl_name"
            ).fetchall()
            user_version = connection.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error as error:
        message = "business database schema could not be inspected"
        raise ProductionStateError(message) from error
    if not rows:
        message = "business database has no application schema"
        raise ProductionStateError(message)
    payload = json.dumps(
        {"schema": rows, "user_version": user_version[0] if user_version else 0},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_manifest_paths(
    state: ProductionStateManifest,
    paths: ProductionStatePaths,
) -> None:
    expected = {
        "profile_root": paths.root,
        "business_database": paths.business_database,
        "trace_database": paths.trace_database,
        "artifacts": paths.artifacts,
        "model_config_path": paths.model_config,
    }
    if state.profile_name != paths.root.name:
        message = "production manifest name does not match its owned directory"
        raise ProductionStateError(message)
    for field_name, expected_path in expected.items():
        if getattr(state, field_name) != expected_path:
            message = f"production manifest {field_name} does not match reserved path"
            raise ProductionStateError(message)


def _validate_no_aliases(
    paths: ProductionStatePaths,
    *,
    trace_present: bool,
) -> None:
    candidates = [
        paths.manifest,
        paths.business_database,
        paths.model_config,
    ]
    if trace_present:
        candidates.append(paths.trace_database)
    identities = [(item.stat().st_dev, item.stat().st_ino) for item in candidates]
    if len(set(identities)) != len(identities):
        message = "production state paths alias the same filesystem object"
        raise ProductionStateError(message)


def load_production_state(  # noqa: C901, PLR0915
    profile_root: Path,
    *,
    build: BuildIdentity,
    expected_owner_uid: int | None = None,
    validate_current_schema: bool = True,
) -> ProductionStateManifest:
    """Load one complete production profile without creating any state."""
    if build.schema_version != "agileforge.build.v1":
        message = "unsupported installed build identity"
        raise ProductionStateError(message)
    owner_uid = _effective_uid() if expected_owner_uid is None else expected_owner_uid
    paths = production_state_paths(profile_root)
    _require_owned_directory(
        paths.root,
        label="production profile root",
        expected_owner_uid=owner_uid,
    )
    try:
        manifest_metadata = paths.manifest.lstat()
    except FileNotFoundError as error:
        message = f"production state manifest is missing: {paths.manifest}"
        raise ProductionStateError(message) from error
    if stat.S_ISLNK(manifest_metadata.st_mode) or not stat.S_ISREG(
        manifest_metadata.st_mode
    ):
        message = f"production state manifest must be a regular file: {paths.manifest}"
        raise ProductionStateError(message)
    _require_owned_regular_file(
        paths.manifest,
        label="production state manifest",
        expected_owner_uid=owner_uid,
    )
    try:
        state = ProductionStateManifest.model_validate_json(paths.manifest.read_bytes())
    except (OSError, ValidationError) as error:
        message = "production state manifest is invalid"
        raise ProductionStateError(message) from error
    _validate_manifest_paths(state, paths)
    _require_owned_regular_file(
        paths.business_database,
        label="business database",
        expected_owner_uid=owner_uid,
    )
    _require_owned_directory(
        paths.artifacts,
        label="artifact directory",
        expected_owner_uid=owner_uid,
    )
    _require_owned_directory(
        paths.config_directory,
        label="configuration directory",
        expected_owner_uid=owner_uid,
    )
    _require_owned_regular_file(
        paths.model_config,
        label="model configuration",
        expected_owner_uid=owner_uid,
    )
    try:
        trace_metadata = paths.trace_database.lstat()
    except FileNotFoundError:
        trace_metadata = None
    if trace_metadata is not None:
        _require_owned_regular_file(
            paths.trace_database,
            label="trace database",
            expected_owner_uid=owner_uid,
        )
    elif state.trace_database_present:
        message = "trace database recorded by production manifest is missing"
        raise ProductionStateError(message)
    _validate_no_aliases(paths, trace_present=trace_metadata is not None)
    if _file_sha256(paths.model_config) != state.model_config_sha256:
        message = "model configuration drift detected"
        raise ProductionStateError(message)
    if database_schema_sha256(paths.business_database) != state.business_schema_sha256:
        message = "business schema drift detected"
        raise ProductionStateError(message)
    if validate_current_schema:
        try:
            verify_current_business_schema(paths.business_database)
            if trace_metadata is not None:
                verify_current_trace_schema(paths.trace_database)
        except TransferError as error:
            message = "unsupported production database schema"
            raise ProductionStateError(message) from error
    _validate_repository_relocations(
        state,
        paths=paths,
        expected_owner_uid=owner_uid,
    )
    return state


def _write_private_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _PRIVATE_FILE_MODE)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def prepare_profile_parent(parent: Path, *, expected_owner_uid: int) -> None:
    try:
        parent.lstat()
    except FileNotFoundError:
        _require_owned_directory(
            parent.parent,
            label="production deployment root",
            expected_owner_uid=expected_owner_uid,
        )
        parent.mkdir(mode=_PRIVATE_DIRECTORY_MODE, exist_ok=False)
    _require_owned_directory(
        parent,
        label="production profiles root",
        expected_owner_uid=expected_owner_uid,
    )


def _publish_manifest(
    paths: ProductionStatePaths,
    state: ProductionStateManifest,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=paths.root,
        prefix=".runtime.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(state.model_dump_json(indent=2))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(paths.manifest)
        directory_descriptor = os.open(paths.root, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _validate_repository_relocations(
    state: ProductionStateManifest,
    *,
    paths: ProductionStatePaths,
    expected_owner_uid: int,
) -> None:
    expected_sha256 = state.repository_relocations_sha256
    try:
        paths.repository_relocations.lstat()
    except FileNotFoundError:
        if expected_sha256 is None:
            return
        message = "repository relocation record is missing"
        raise ProductionStateError(message) from None
    if expected_sha256 is None:
        message = "unexpected repository relocation record"
        raise ProductionStateError(message)
    _require_owned_regular_file(
        paths.repository_relocations,
        label="repository relocation record",
        expected_owner_uid=expected_owner_uid,
    )
    if _file_sha256(paths.repository_relocations) != expected_sha256:
        message = "repository relocation record drift detected"
        raise ProductionStateError(message)


def _bootstrap_current_business_database(database: Path) -> None:
    environment = {
        "AGILEFORGE_DB_URL": f"sqlite:///{database.as_posix()}",
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL": (
            f"sqlite:///{database.with_name('adk-trace.sqlite3').as_posix()}"
        ),
        "HOME": str(database.parent),
        LAUNCHER_CHILD_ENV: LAUNCHER_CHILD_VALUE,
        "MODEL_CONFIG_PATH": str(database.parent / "config" / "models.yaml"),
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONNOUSERSITE": "1",
    }
    result = subprocess.run(  # nosec B603
        (sys.executable, "-m", "agile_sqlmodel"),
        cwd=database.parent,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        message = f"business schema bootstrap failed with exit code {result.returncode}"
        raise ProductionStateError(message)


def initialize_production_state(  # noqa: PLR0913
    profile_root: Path,
    *,
    build: BuildIdentity,
    model_config_source: Path,
    bootstrap_business_database: Callable[[Path], None] | None = None,
    now: datetime | None = None,
    state_id: UUID | None = None,
    expected_owner_uid: int | None = None,
    validate_current_schema: bool = True,
) -> ProductionStateManifest:
    """Reserve, initialize, and publish a new production profile explicitly."""
    owner_uid = _effective_uid() if expected_owner_uid is None else expected_owner_uid
    paths = production_state_paths(profile_root)
    try:
        paths.root.lstat()
    except FileNotFoundError:
        pass
    else:
        message = f"production profile root already exists: {paths.root}"
        raise ProductionStateError(message)
    prepare_profile_parent(paths.root.parent, expected_owner_uid=owner_uid)
    paths.root.mkdir(mode=_PRIVATE_DIRECTORY_MODE, exist_ok=False)
    paths.artifacts.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    paths.config_directory.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    source = model_config_source.absolute()
    source_metadata = _lstat(source, label="model configuration source")
    if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISREG(
        source_metadata.st_mode
    ):
        message = "model configuration source must be a regular file"
        raise ProductionStateError(message)
    if source.resolve(strict=True) != source:
        message = "model configuration source must not contain links"
        raise ProductionStateError(message)
    _write_private_file(paths.model_config, source.read_bytes())
    bootstrap = bootstrap_business_database or _bootstrap_current_business_database
    bootstrap(paths.business_database)
    _require_owned_regular_file(
        paths.business_database,
        label="business database",
        expected_owner_uid=owner_uid,
    )
    if paths.trace_database.exists():
        message = "schema bootstrap created the reserved trace database"
        raise ProductionStateError(message)
    state = ProductionStateManifest(
        schema_version="agileforge.production-state.v1",
        state_id=state_id or uuid4(),
        profile_name=paths.root.name,
        profile_root=paths.root,
        business_database=paths.business_database,
        trace_database=paths.trace_database,
        trace_database_present=False,
        artifacts=paths.artifacts,
        model_config_path=paths.model_config,
        model_config_sha256=_file_sha256(paths.model_config),
        business_schema_sha256=database_schema_sha256(paths.business_database),
        created_at=now or datetime.now(tz=UTC),
        created_by_build_revision=build.revision,
        provenance="initialized",
        source_backup_sha256=None,
        repository_relocations_sha256=None,
    )
    _publish_manifest(paths, state)
    return load_production_state(
        paths.root,
        build=build,
        expected_owner_uid=owner_uid,
        validate_current_schema=validate_current_schema,
    )


def finalize_restored_production_state(  # noqa: PLR0913
    profile_root: Path,
    *,
    source_state: ProductionStateManifest,
    build: BuildIdentity,
    source_backup_sha256: str,
    repository_relocations_sha256: str | None = None,
    expected_owner_uid: int | None = None,
) -> ProductionStateManifest:
    """Publish rebased restored state after payload verification and installation."""
    owner_uid = _effective_uid() if expected_owner_uid is None else expected_owner_uid
    paths = production_state_paths(profile_root)
    try:
        paths.manifest.lstat()
    except FileNotFoundError:
        pass
    else:
        message = "restored production state manifest already exists"
        raise ProductionStateError(message)
    _require_owned_directory(
        paths.root,
        label="production profile root",
        expected_owner_uid=owner_uid,
    )
    _require_owned_regular_file(
        paths.business_database,
        label="business database",
        expected_owner_uid=owner_uid,
    )
    _require_owned_directory(
        paths.artifacts,
        label="artifact directory",
        expected_owner_uid=owner_uid,
    )
    _require_owned_directory(
        paths.config_directory,
        label="configuration directory",
        expected_owner_uid=owner_uid,
    )
    _require_owned_regular_file(
        paths.model_config,
        label="model configuration",
        expected_owner_uid=owner_uid,
    )
    trace_present = paths.trace_database.exists()
    if trace_present:
        _require_owned_regular_file(
            paths.trace_database,
            label="trace database",
            expected_owner_uid=owner_uid,
        )
    state = ProductionStateManifest(
        schema_version="agileforge.production-state.v1",
        state_id=source_state.state_id,
        profile_name=paths.root.name,
        profile_root=paths.root,
        business_database=paths.business_database,
        trace_database=paths.trace_database,
        trace_database_present=trace_present,
        artifacts=paths.artifacts,
        model_config_path=paths.model_config,
        model_config_sha256=_file_sha256(paths.model_config),
        business_schema_sha256=database_schema_sha256(paths.business_database),
        created_at=source_state.created_at,
        created_by_build_revision=source_state.created_by_build_revision,
        provenance="restored",
        source_backup_sha256=source_backup_sha256,
        repository_relocations_sha256=repository_relocations_sha256,
    )
    _publish_manifest(paths, state)
    return load_production_state(
        paths.root,
        build=build,
        expected_owner_uid=owner_uid,
    )


__all__ = [
    "PRODUCTION_STATE_BASE",
    "ProductionStateError",
    "ProductionStateManifest",
    "ProductionStatePaths",
    "database_schema_sha256",
    "finalize_restored_production_state",
    "initialize_production_state",
    "load_production_state",
    "production_profile_root",
    "production_state_paths",
]
