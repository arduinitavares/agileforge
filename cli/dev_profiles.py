"""Worktree-local developer runtime profile contracts and persistence."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self, cast

from git import Git
from pydantic import BaseModel, ConfigDict, model_validator

from workflow.contracts import GRAPH_VERSION

_PROFILE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_SCHEMA_VERSION: Literal["1"] = "1"
_DIRECTORY_MODE = 0o700
_MANIFEST_MODE = 0o600


class ProfileMode(StrEnum):
    """Supported developer runtime profile modes."""

    DEVELOPMENT = "development"
    ACCEPTANCE = "acceptance"


class CheckoutProvenance(BaseModel):
    """Git identity recorded when a runtime profile is initialized."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root: Path
    branch: str | None
    commit: str

    @model_validator(mode="after")
    def validate_checkout(self) -> Self:
        """Require canonical absolute roots and full lowercase Git commits."""
        if not self.root.is_absolute():
            message = "checkout root must be absolute"
            raise ValueError(message)
        if _COMMIT_PATTERN.fullmatch(self.commit) is None:
            message = "checkout commit must be a 40-character lowercase hex value"
            raise ValueError(message)
        return self


class RuntimeProfile(BaseModel):
    """Immutable non-secret provenance for one worktree-local runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"]
    name: str
    mode: ProfileMode
    checkout: CheckoutProvenance
    expected_commit: str | None
    graph_version: str
    python_version: str
    uv_version: str
    business_database: Path
    trace_database: Path
    model_config_path: Path
    model_config_sha256: str
    schema_source_sha256: str
    created_at: datetime
    last_used_at: datetime

    def _validate_identity(self) -> None:
        if _PROFILE_NAME_PATTERN.fullmatch(self.name) is None:
            message = "invalid profile name"
            raise ValueError(message)
        if self.mode is ProfileMode.DEVELOPMENT and self.expected_commit is not None:
            message = "development profile expected_commit must be None"
            raise ValueError(message)
        if self.mode is ProfileMode.ACCEPTANCE:
            if (
                self.expected_commit is None
                or _COMMIT_PATTERN.fullmatch(self.expected_commit) is None
            ):
                message = (
                    "acceptance profile expected_commit must be a 40-character "
                    "lowercase hex value"
                )
                raise ValueError(message)
            if self.expected_commit != self.checkout.commit:
                message = "acceptance profile expected_commit must equal current commit"
                raise ValueError(message)

    def _validate_paths(self) -> None:
        if self.business_database == self.trace_database:
            message = "profile database paths must be distinct"
            raise ValueError(message)
        required_paths = (
            self.checkout.root,
            self.business_database,
            self.trace_database,
            self.model_config_path,
        )
        if any(not path.is_absolute() for path in required_paths):
            message = "profile paths must be absolute"
            raise ValueError(message)

    def _validate_hashes_and_times(self) -> None:
        if _HASH_PATTERN.fullmatch(self.model_config_sha256) is None:
            message = "model_config_sha256 must be a lowercase SHA-256 value"
            raise ValueError(message)
        if _HASH_PATTERN.fullmatch(self.schema_source_sha256) is None:
            message = "schema_source_sha256 must be a lowercase SHA-256 value"
            raise ValueError(message)
        if self.created_at.tzinfo is None or self.last_used_at.tzinfo is None:
            message = "profile timestamps must be timezone-aware"
            raise ValueError(message)
        if self.last_used_at < self.created_at:
            message = "last_used_at must not precede created_at"
            raise ValueError(message)

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        """Enforce mode, fingerprint, timestamp, and database invariants."""
        self._validate_identity()
        self._validate_paths()
        self._validate_hashes_and_times()
        return self


@dataclass(frozen=True, slots=True)
class ProfilePaths:
    """Absolute paths owned by one worktree-local runtime profile."""

    root: Path
    manifest: Path
    business_database: Path
    trace_database: Path
    artifacts: Path
    logs: Path


@dataclass(frozen=True, slots=True)
class ProfileRuntimeMetadata:
    """Injectable runtime values captured during profile preparation."""

    now: datetime | None = None
    uv_version: str | None = None


def _run_command(arguments: tuple[str, ...]) -> str:
    """Run fixed argv and return stripped stdout."""
    output = Git().execute(command=list(arguments))
    return cast("str", output).strip()


def _run_git(checkout: Path, *arguments: str) -> str:
    """Run one fixed-form Git command against a checkout."""
    return _run_command(("git", "-C", str(checkout), *arguments))


def resolve_checkout_root(anchor: Path) -> Path:
    """Resolve the canonical Git checkout root containing ``anchor``."""
    root_output = _run_git(anchor, "rev-parse", "--show-toplevel")
    root = Path(root_output).resolve(strict=True)
    metadata = root.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        message = f"checkout root is not a real directory: {root}"
        raise ValueError(message)
    return root


def _checkout_provenance(checkout_root: Path) -> CheckoutProvenance:
    root = resolve_checkout_root(checkout_root)
    commit = _run_git(root, "rev-parse", "HEAD")
    branch_output = _run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
    branch = None if branch_output == "HEAD" else branch_output
    return CheckoutProvenance(root=root, branch=branch, commit=commit)


def _absolute_path(path: Path) -> Path:
    return path.expanduser().absolute().resolve(strict=False)


def _state_base(checkout_root: Path) -> Path:
    return checkout_root / ".agileforge" / "dev" / "profiles"


def _path_chain(base: Path, path: Path) -> tuple[Path, ...]:
    relative = path.relative_to(base)
    current = base
    chain = [base]
    for part in relative.parts:
        current /= part
        chain.append(current)
    return tuple(chain)


def _validate_state_path(checkout_root: Path, path: Path) -> None:
    root = _absolute_path(checkout_root)
    base = _state_base(root)
    candidate = path.absolute()
    if not candidate.is_relative_to(base):
        message = f"profile path is outside checkout-local state: {candidate}"
        raise ValueError(message)

    state_root = root / ".agileforge"
    for item in _path_chain(state_root, candidate):
        try:
            metadata = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            message = f"profile state path must not be a symlink: {item}"
            raise ValueError(message)

    resolved_base = base.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    if not resolved_candidate.is_relative_to(resolved_base):
        message = f"resolved profile path escapes checkout-local state: {candidate}"
        raise ValueError(message)


def profile_paths(checkout_root: Path, profile_name: str) -> ProfilePaths:
    """Build and validate absolute paths for one profile name."""
    if _PROFILE_NAME_PATTERN.fullmatch(profile_name) is None:
        message = f"invalid profile name: {profile_name!r}"
        raise ValueError(message)

    root = _state_base(_absolute_path(checkout_root)) / profile_name
    paths = ProfilePaths(
        root=root,
        manifest=root / "profile.json",
        business_database=root / "business.sqlite3",
        trace_database=root / "adk-trace.sqlite3",
        artifacts=root / "artifacts",
        logs=root / "logs",
    )
    for path in (
        paths.root,
        paths.manifest,
        paths.business_database,
        paths.trace_database,
        paths.artifacts,
        paths.logs,
    ):
        _validate_state_path(_absolute_path(checkout_root), path)
    return paths


def _validate_checkout_file(checkout_root: Path, path: Path) -> None:
    root = _absolute_path(checkout_root)
    candidate = path.absolute()
    if not candidate.is_relative_to(root):
        message = f"checkout file is outside the checkout: {candidate}"
        raise ValueError(message)
    for item in _path_chain(root, candidate):
        metadata = item.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            message = f"checkout source path must not be a symlink: {item}"
            raise ValueError(message)
    if not stat.S_ISREG(candidate.lstat().st_mode):
        message = f"checkout source path must be a regular file: {candidate}"
        raise ValueError(message)


def _file_sha256(checkout_root: Path, path: Path) -> str:
    _validate_checkout_file(checkout_root, path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _schema_source_sha256(checkout_root: Path) -> str:
    tracked_output = _run_git(
        checkout_root,
        "ls-files",
        "-z",
        "--",
        "models/*.py",
    )
    tracked_models = tuple(Path(item) for item in tracked_output.split("\0") if item)
    relative_paths = tuple(
        sorted(
            (Path("agile_sqlmodel.py"), *tracked_models),
            key=lambda item: item.as_posix(),
        )
    )
    if len(set(relative_paths)) != len(relative_paths):
        message = "schema source list contains duplicate paths"
        raise ValueError(message)

    digest = hashlib.sha256()
    for relative_path in relative_paths:
        source_path = checkout_root / relative_path
        _validate_checkout_file(checkout_root, source_path)
        digest.update(relative_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(source_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _ensure_private_directory(checkout_root: Path, path: Path) -> None:
    _validate_state_path(checkout_root, path)
    path.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        message = f"profile state path must be a real directory: {path}"
        raise ValueError(message)
    path.chmod(_DIRECTORY_MODE)


def _create_profile_root(checkout_root: Path, path: Path) -> None:
    _validate_state_path(checkout_root, path)
    try:
        path.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=False)
    except FileExistsError as error:
        message = f"profile root already exists: {path}"
        raise FileExistsError(message) from error
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        message = f"profile root must be a real directory: {path}"
        raise ValueError(message)
    path.chmod(_DIRECTORY_MODE)


def _write_profile(
    checkout_root: Path,
    paths: ProfilePaths,
    profile: RuntimeProfile,
) -> None:
    _validate_state_path(checkout_root, paths.manifest)
    try:
        manifest_metadata = paths.manifest.lstat()
    except FileNotFoundError:
        manifest_metadata = None
    if manifest_metadata is not None and stat.S_ISLNK(manifest_metadata.st_mode):
        message = f"profile manifest must not be a symlink: {paths.manifest}"
        raise ValueError(message)

    payload = json.dumps(
        profile.model_dump(mode="json"),
        indent=2,
        sort_keys=True,
    )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=paths.root,
        prefix=".profile.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        _validate_state_path(checkout_root, temporary_path)
        temporary_path.chmod(_MANIFEST_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(payload)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(paths.manifest)
        paths.manifest.chmod(_MANIFEST_MODE)
        directory_descriptor = os.open(paths.root, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            _validate_state_path(checkout_root, temporary_path)
            temporary_metadata = temporary_path.lstat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(temporary_metadata.st_mode):
                message = f"temporary manifest became a symlink: {temporary_path}"
                raise ValueError(message)
            temporary_path.unlink()


def prepare_profile_record(
    checkout_root: Path,
    profile_name: str,
    mode: ProfileMode = ProfileMode.DEVELOPMENT,
    expected_commit: str | None = None,
    *,
    runtime: ProfileRuntimeMetadata | None = None,
) -> RuntimeProfile:
    """Claim private profile state and build provenance without a manifest."""
    checkout = _checkout_provenance(checkout_root)
    paths = profile_paths(checkout.root, profile_name)
    if mode is ProfileMode.ACCEPTANCE:
        if (
            expected_commit is None
            or _COMMIT_PATTERN.fullmatch(expected_commit) is None
        ):
            message = (
                "acceptance expected_commit must be a 40-character lowercase hex value"
            )
            raise ValueError(message)
        if expected_commit != checkout.commit:
            message = "acceptance expected_commit does not match current commit"
            raise ValueError(message)
    elif expected_commit is not None:
        message = "development expected_commit must be None"
        raise ValueError(message)

    model_config_path = checkout.root / "config" / "models.yaml"
    metadata = runtime or ProfileRuntimeMetadata()
    timestamp = metadata.now or datetime.now(tz=UTC)
    profile = RuntimeProfile(
        schema_version=_PROFILE_SCHEMA_VERSION,
        name=profile_name,
        mode=mode,
        checkout=checkout,
        expected_commit=expected_commit,
        graph_version=GRAPH_VERSION,
        python_version=platform.python_version(),
        uv_version=(
            metadata.uv_version or _run_command(("uv", "--version")).split()[1]
        ),
        business_database=paths.business_database,
        trace_database=paths.trace_database,
        model_config_path=model_config_path,
        model_config_sha256=_file_sha256(checkout.root, model_config_path),
        schema_source_sha256=_schema_source_sha256(checkout.root),
        created_at=timestamp,
        last_used_at=timestamp,
    )
    _create_profile_root(checkout.root, paths.root)
    for directory in (paths.artifacts, paths.logs):
        _ensure_private_directory(checkout.root, directory)
    return profile


def initialize_profile_record(
    checkout_root: Path,
    profile_name: str,
    mode: ProfileMode = ProfileMode.DEVELOPMENT,
    expected_commit: str | None = None,
    *,
    now: datetime | None = None,
) -> RuntimeProfile:
    """Create and atomically persist one profile provenance record."""
    profile = prepare_profile_record(
        checkout_root,
        profile_name,
        mode,
        expected_commit,
        runtime=ProfileRuntimeMetadata(now=now),
    )
    return finalize_profile_record(profile)


def _validate_profile_ownership(
    profile: RuntimeProfile,
    checkout: CheckoutProvenance,
    paths: ProfilePaths,
) -> None:
    if profile.name != paths.root.name:
        message = "profile manifest name does not match its owned directory"
        raise ValueError(message)
    if profile.checkout.root != checkout.root:
        message = "profile checkout root does not match current checkout"
        raise ValueError(message)
    if profile.graph_version != GRAPH_VERSION:
        message = "workflow graph version drift detected"
        raise ValueError(message)
    expected_config_path = checkout.root / "config" / "models.yaml"
    if profile.model_config_path != expected_config_path:
        message = "profile model configuration path does not match checkout"
        raise ValueError(message)
    if (
        profile.business_database != paths.business_database
        or profile.trace_database != paths.trace_database
        or profile.business_database == profile.trace_database
    ):
        message = "profile database paths are not distinct owned paths"
        raise ValueError(message)
    if not profile.business_database.is_relative_to(paths.root) or not (
        profile.trace_database.is_relative_to(paths.root)
    ):
        message = "profile database paths escape the profile root"
        raise ValueError(message)
    if profile.mode is ProfileMode.ACCEPTANCE and (
        profile.expected_commit != checkout.commit
    ):
        message = "acceptance commit mismatch"
        raise ValueError(message)


def finalize_profile_record(profile: RuntimeProfile) -> RuntimeProfile:
    """Atomically publish one prepared profile after external verification."""
    checkout = _checkout_provenance(profile.checkout.root)
    paths = profile_paths(checkout.root, profile.name)
    _validate_profile_ownership(profile, checkout, paths)
    root_metadata = paths.root.lstat()
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(
        root_metadata.st_mode
    ):
        message = f"profile root must be a real directory: {paths.root}"
        raise ValueError(message)
    try:
        paths.manifest.lstat()
    except FileNotFoundError:
        pass
    else:
        message = f"profile manifest already exists: {paths.manifest}"
        raise FileExistsError(message)
    for directory in (paths.artifacts, paths.logs):
        metadata = directory.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            message = f"profile state path must be a real directory: {directory}"
            raise ValueError(message)
    _write_profile(checkout.root, paths, profile)
    return profile


def load_profile(checkout_root: Path, profile_name: str) -> RuntimeProfile:
    """Load a profile after validating ownership and current provenance."""
    checkout = _checkout_provenance(checkout_root)
    paths = profile_paths(checkout.root, profile_name)
    for path in (
        paths.root,
        paths.manifest,
        paths.business_database,
        paths.trace_database,
        paths.artifacts,
        paths.logs,
    ):
        _validate_state_path(checkout.root, path)
    manifest_metadata = paths.manifest.lstat()
    if not stat.S_ISREG(manifest_metadata.st_mode):
        message = f"profile manifest must be a regular file: {paths.manifest}"
        raise ValueError(message)

    profile = RuntimeProfile.model_validate_json(paths.manifest.read_bytes())
    _validate_profile_ownership(profile, checkout, paths)
    for database_path in (profile.business_database, profile.trace_database):
        _validate_state_path(checkout.root, database_path)
    if profile.model_config_sha256 != _file_sha256(
        checkout.root,
        profile.model_config_path,
    ):
        message = "model configuration drift detected"
        raise ValueError(message)
    if profile.schema_source_sha256 != _schema_source_sha256(checkout.root):
        message = "schema source drift detected"
        raise ValueError(message)
    return profile


def touch_profile_last_used(
    checkout_root: Path,
    profile_name: str,
    *,
    now: datetime | None = None,
) -> RuntimeProfile:
    """Validate a profile and atomically advance only its last-use time."""
    profile = load_profile(checkout_root, profile_name)
    timestamp = now or datetime.now(tz=UTC)
    if timestamp <= profile.last_used_at:
        message = "last_used_at must advance"
        raise ValueError(message)
    touched = profile.model_copy(update={"last_used_at": timestamp})
    paths = profile_paths(profile.checkout.root, profile.name)
    _write_profile(profile.checkout.root, paths, touched)
    return touched


def profile_environment(profile: RuntimeProfile) -> dict[str, str]:
    """Return the exact non-secret runtime environment for a profile."""
    return {
        "AGILEFORGE_DB_URL": f"sqlite:///{profile.business_database.as_posix()}",
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL": (
            f"sqlite:///{profile.trace_database.as_posix()}"
        ),
        "MODEL_CONFIG_PATH": str(profile.model_config_path),
    }


def _collect_profile_paths(checkout_root: Path, root: Path) -> tuple[Path, ...]:
    _validate_state_path(checkout_root, root)
    metadata = root.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        message = f"profile content must not be a symlink: {root}"
        raise ValueError(message)
    if not stat.S_ISDIR(metadata.st_mode):
        return (root,)

    collected: list[Path] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        collected.extend(_collect_profile_paths(checkout_root, child))
    collected.append(root)
    return tuple(collected)


def reset_profile(
    checkout_root: Path,
    profile_name: str,
    confirmation: str,
) -> tuple[Path, ...]:
    """Remove only one confirmed, fully validated profile tree."""
    if confirmation != profile_name:
        message = "profile reset confirmation must exactly match the profile name"
        raise ValueError(message)
    checkout = _checkout_provenance(checkout_root)
    paths = profile_paths(checkout.root, profile_name)
    removed_paths = _collect_profile_paths(checkout.root, paths.root)
    for path in removed_paths:
        _validate_state_path(checkout.root, path)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            message = f"profile content must not be a symlink: {path}"
            raise ValueError(message)
        if stat.S_ISDIR(metadata.st_mode):
            path.rmdir()
        else:
            path.unlink()
    return removed_paths
