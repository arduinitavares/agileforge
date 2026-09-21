# cli/state_transfer.py
"""Validated, portable backup payloads for current AgileForge SQLite state."""

# ruff: noqa: EM101, EM102, TRY003

from __future__ import annotations

import asyncio
import base64
import configparser
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import struct
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, cast

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from adapters.git.repository_probe import GitPythonRepositoryProbe
from services.repository_probe import RepositoryProbeError, RepositoryStatusEntry
from utils.runtime_fence import runtime_fence
from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

_FORMAT = "agileforge.transfer.v1"
_MANIFEST_NAME = "manifest.json"
_UNPUBLISHED_NAME = ".agileforge-unpublished"
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_FILE_MODE = 0o777
_SHA256_HEX_LENGTH = 64
_GIT_POINTER_MAX_BYTES = 4096
_GIT_SHA_LENGTH = 40
_REPOSITORY_LINK_MIN_PARTS = 3
_ADMIN_LINK_MIN_PARTS = 4
_REPOSITORY_PATH_MIN_PARTS = 2
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BUSINESS_DATABASE_ENV = "AGILEFORGE_DB_URL"
_LAUNCHER_CHILD_ENV = "AGILEFORGE_LAUNCHER_CHILD"
_APPROVED_CORE_KEYS = frozenset(
    {"repositoryformatversion", "filemode", "bare", "logallrefupdates"}
)
_APPROVED_EXTENSION_KEYS = frozenset({"worktreeconfig"})
_DATABASE_PATHS = {
    "business": "business.sqlite3",
    "trace": "trace.sqlite3",
}
_TOP_LEVEL_KEYS = frozenset(
    {
        "format",
        "created_at",
        "files",
        "databases",
        "repositories",
        "model_config_sha256",
        "observed_links",
    }
)


class TransferError(RuntimeError):
    """Raised when state cannot be captured or verified without ambiguity."""


@dataclass(frozen=True, slots=True)
class StateLayout:
    """Explicit durable inputs for one backup operation."""

    root: Path
    business_database: Path
    trace_database: Path
    artifacts: Path
    model_config: Path
    repositories: tuple[Path, ...] | None = None
    include_registered_repositories: bool = True
    maintenance_roots: tuple[Path, ...] = ()
    provenance_files: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class FileRecord:
    """One complete payload filesystem entry."""

    path: str
    kind: str
    mode: int
    size: int | None = None
    sha256: str | None = None
    link_target: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON representation."""
        result: dict[str, object] = {
            "path": self.path,
            "kind": self.kind,
            "mode": self.mode,
        }
        if self.size is not None:
            result["size"] = self.size
        if self.sha256 is not None:
            result["sha256"] = self.sha256
        if self.link_target is not None:
            result["link_target"] = self.link_target
        return result

    @classmethod
    def from_dict(cls, payload: object) -> FileRecord:
        """Parse one strict file record from untrusted JSON."""
        if not isinstance(payload, dict):
            raise TransferError("manifest file inventory entry must be an object")
        record = cast("dict[str, object]", payload)
        path = record.get("path")
        kind = record.get("kind")
        mode = record.get("mode")
        if not isinstance(path, str) or not isinstance(kind, str):
            raise TransferError("manifest file inventory entry has invalid text fields")
        if not isinstance(mode, int) or isinstance(mode, bool):
            raise TransferError("manifest file inventory entry has invalid mode")
        if not 0 <= mode <= _MAX_FILE_MODE:
            raise TransferError("manifest file inventory entry has unsafe mode")
        _validated_relative(path)
        allowed = {
            "directory": {"path", "kind", "mode"},
            "file": {"path", "kind", "mode", "size", "sha256"},
            "symlink": {
                "path",
                "kind",
                "mode",
                "size",
                "sha256",
                "link_target",
            },
        }
        expected_keys = allowed.get(kind)
        if expected_keys is None or set(record) != expected_keys:
            raise TransferError("manifest file inventory entry has invalid fields")
        if kind == "directory":
            return cls(path=path, kind=kind, mode=mode)
        size = record.get("size")
        digest = record.get("sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise TransferError("manifest file inventory entry has invalid size")
        if not _is_sha256(digest):
            raise TransferError("manifest file inventory entry has invalid digest")
        link_target = record.get("link_target")
        if kind == "symlink" and not isinstance(link_target, str):
            raise TransferError("manifest symlink entry has invalid target")
        return cls(
            path=path,
            kind=kind,
            mode=mode,
            size=size,
            sha256=cast("str", digest),
            link_target=cast("str | None", link_target),
        )


@dataclass(frozen=True, slots=True)
class TransferManifest:
    """Logical and byte inventory for one complete transfer bundle."""

    format: str
    created_at: str
    files: tuple[FileRecord, ...]
    databases: dict[str, dict[str, object]]
    repositories: tuple[dict[str, object], ...]
    model_config_sha256: str
    observed_links: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON representation."""
        return {
            "format": self.format,
            "created_at": self.created_at,
            "files": [record.to_dict() for record in self.files],
            "databases": self.databases,
            "repositories": list(self.repositories),
            "model_config_sha256": self.model_config_sha256,
            "observed_links": list(self.observed_links),
        }

    @classmethod
    def from_dict(cls, payload: object) -> TransferManifest:
        """Parse one strict v1 manifest from untrusted JSON."""
        if not isinstance(payload, dict) or set(payload) != _TOP_LEVEL_KEYS:
            raise TransferError("transfer manifest has invalid top-level fields")
        document = cast("dict[str, object]", payload)
        format_name = document.get("format")
        if format_name != _FORMAT:
            raise TransferError(f"unsupported transfer format: {format_name!r}")
        created_at = document.get("created_at")
        model_digest = document.get("model_config_sha256")
        if not isinstance(created_at, str) or not _is_sha256(model_digest):
            raise TransferError("transfer manifest provenance is invalid")
        raw_files = document.get("files")
        if not isinstance(raw_files, list):
            raise TransferError("transfer manifest files must be a list")
        files = tuple(FileRecord.from_dict(item) for item in raw_files)
        if tuple(sorted(files, key=lambda item: item.path)) != files:
            raise TransferError("transfer manifest files must be sorted")
        paths = [record.path for record in files]
        if len(paths) != len(set(paths)):
            raise TransferError("transfer manifest contains duplicate paths")
        databases = _parse_databases(document.get("databases"))
        repositories = _parse_repositories(document.get("repositories"))
        observed_links = _parse_links(document.get("observed_links"))
        return cls(
            format=cast("str", format_name),
            created_at=created_at,
            files=files,
            databases=databases,
            repositories=repositories,
            model_config_sha256=cast("str", model_digest),
            observed_links=observed_links,
        )


@dataclass(frozen=True, slots=True)
class RestoredRepository:
    """One verified repository payload and its captured source identity."""

    index: int
    source_path: str
    common_repository: str
    worktree: Path
    gitdir: Path | None
    common: Path | None
    identity: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe operator mapping without reading repository data."""
        return {
            "index": self.index,
            "source_path": self.source_path,
            "common_repository": self.common_repository,
            "worktree": str(self.worktree),
            "gitdir": None if self.gitdir is None else str(self.gitdir),
            "common": None if self.common is None else str(self.common),
            "identity": self.identity,
        }


@dataclass(frozen=True, slots=True)
class _DatabaseFacts:
    attempts: frozenset[str]
    sessions: frozenset[str]
    links: tuple[tuple[str, str], ...]


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_HEX_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _parse_databases(payload: object) -> dict[str, dict[str, object]]:
    if not isinstance(payload, dict) or set(payload) != set(_DATABASE_PATHS):
        raise TransferError("transfer manifest database inventory is invalid")
    database_payload = cast("dict[str, object]", payload)
    result: dict[str, dict[str, object]] = {}
    for role in sorted(_DATABASE_PATHS):
        entry = database_payload.get(role)
        if not isinstance(entry, dict):
            raise TransferError("transfer manifest database entry is invalid")
        database_entry = cast("dict[str, object]", entry)
        present = database_entry.get("present")
        expected_path = _DATABASE_PATHS[role]
        if present is False and database_entry == {
            "path": expected_path,
            "present": False,
        }:
            result[role] = database_entry
            continue
        if present is not True or database_entry.get("path") != expected_path:
            raise TransferError("transfer manifest database entry is invalid")
        result[role] = database_entry
    return result


def _parse_repository_components(payload: object, *, index: int) -> None:
    if not isinstance(payload, list) or not payload:
        raise TransferError("transfer manifest repository components are invalid")
    expected_worktree = f"repositories/{index:04d}"
    component_payloads: list[str] = []
    for component in payload:
        if not isinstance(component, dict) or set(component) != {"kind", "payload"}:
            raise TransferError("transfer manifest repository component is invalid")
        component_record = cast("dict[str, object]", component)
        kind = component_record.get("kind")
        component_payload = component_record.get("payload")
        if kind not in {"worktree", "gitdir", "common"} or not isinstance(
            component_payload, str
        ):
            raise TransferError("transfer manifest repository component is invalid")
        _validated_relative(component_payload)
        component_payloads.append(component_payload)
    if component_payloads.count(expected_worktree) != 1:
        raise TransferError("transfer manifest repository worktree is invalid")


def _parse_repositories(payload: object) -> tuple[dict[str, object], ...]:
    if not isinstance(payload, list):
        raise TransferError("transfer manifest repositories must be a list")
    repositories: list[dict[str, object]] = []
    for expected_index, entry in enumerate(payload):
        if not isinstance(entry, dict) or set(entry) != {
            "index",
            "excluded",
            "components",
            "source_path",
            "identity",
        }:
            raise TransferError("transfer manifest repository entry is invalid")
        repository_record = cast("dict[str, object]", entry)
        if repository_record.get("index") != expected_index:
            raise TransferError("transfer manifest repository indexes are invalid")
        source_path = repository_record.get("source_path")
        if (
            not isinstance(source_path, str)
            or not (
                Path(source_path).is_absolute()
                or PureWindowsPath(source_path).is_absolute()
            )
            or "\x00" in source_path
        ):
            raise TransferError("transfer manifest repository source is invalid")
        _parse_repository_identity(repository_record.get("identity"))
        excluded = repository_record.get("excluded")
        if not isinstance(excluded, list) or not all(
            isinstance(item, str) for item in excluded
        ):
            raise TransferError("transfer manifest repository exclusions are invalid")
        if excluded != sorted(set(excluded)):
            raise TransferError("transfer manifest repository exclusions are invalid")
        _parse_repository_components(
            repository_record.get("components"),
            index=expected_index,
        )
        repositories.append(repository_record)
    return tuple(repositories)


def _parse_repository_identity(payload: object) -> None:
    expected = {
        "head_sha",
        "common_git_dir",
        "branch_name",
        "detached_head",
        "dirty",
        "status_entries",
        "status_fingerprint",
        "remotes",
        "probe_version",
        "approved_config",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise TransferError("transfer manifest repository identity is invalid")
    identity = cast("dict[str, object]", payload)
    head = identity.get("head_sha")
    branch = identity.get("branch_name")
    remotes = identity.get("remotes")
    approved_config = identity.get("approved_config")
    if (
        not isinstance(head, str)
        or len(head) != _GIT_SHA_LENGTH
        or any(character not in "0123456789abcdef" for character in head)
        or (branch is not None and not isinstance(branch, str))
        or not isinstance(identity.get("common_git_dir"), str)
        or not isinstance(identity.get("detached_head"), bool)
        or not isinstance(identity.get("dirty"), bool)
        or not isinstance(identity.get("status_entries"), list)
        or not isinstance(identity.get("status_fingerprint"), str)
        or not isinstance(remotes, list)
        or not all(isinstance(item, str) for item in remotes)
        or identity.get("probe_version") != "agileforge.repository-probe.v1"
        or not isinstance(approved_config, dict)
        or set(approved_config) != {"core", "extensions"}
        or not all(
            isinstance(section, dict)
            and all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in section.items()
            )
            for section in approved_config.values()
        )
    ):
        raise TransferError("transfer manifest repository identity is invalid")


def _parse_links(payload: object) -> tuple[dict[str, str], ...]:
    if not isinstance(payload, list):
        raise TransferError("transfer manifest observed links must be a list")
    links: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != {
            "attempt_fingerprint",
            "session_id",
        }:
            raise TransferError("transfer manifest observed link is invalid")
        link_record = cast("dict[str, object]", item)
        attempt = link_record.get("attempt_fingerprint")
        session = link_record.get("session_id")
        if not isinstance(attempt, str) or not isinstance(session, str):
            raise TransferError("transfer manifest observed link is invalid")
        links.append({"attempt_fingerprint": attempt, "session_id": session})
    if links != sorted(
        links,
        key=lambda item: (item["attempt_fingerprint"], item["session_id"]),
    ):
        raise TransferError("transfer manifest observed links must be sorted")
    return tuple(links)


def _validated_relative(raw_path: str) -> PurePosixPath:
    path = PurePosixPath(raw_path)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != raw_path
    ):
        raise TransferError(f"unsafe transfer inventory path: {raw_path!r}")
    if path.parts[0] == _MANIFEST_NAME:
        raise TransferError("transfer inventory must not include its manifest")
    return path


def _owned(metadata: os.stat_result, *, label: str) -> None:
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise TransferError(f"{label} is not owned by this user")


def _reject_privileged_mode(metadata: os.stat_result, *, label: str) -> None:
    if stat.S_IMODE(metadata.st_mode) & 0o7000:
        raise TransferError(f"{label} has a privileged mode")


def _real_directory(path: Path, *, label: str) -> Path:
    candidate = path.expanduser().absolute()
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise TransferError(f"{label} is unavailable: {candidate}") from error
    if stat.S_ISLNK(metadata.st_mode) or resolved != candidate:
        raise TransferError(f"{label} must not contain a symlink: {candidate}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise TransferError(f"{label} must be a directory: {candidate}")
    _owned(metadata, label=label)
    return resolved


def _regular_file(
    path: Path,
    *,
    root: Path,
    label: str,
    required: bool = True,
) -> Path | None:
    candidate = path.expanduser().absolute()
    if not candidate.is_relative_to(root):
        raise TransferError(f"{label} escapes the state root: {candidate}")
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        if required:
            raise TransferError(f"{label} is missing: {candidate}") from None
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise TransferError(f"{label} is unavailable: {candidate}") from error
    if stat.S_ISLNK(metadata.st_mode) or resolved != candidate:
        raise TransferError(f"{label} must not contain a symlink: {candidate}")
    if not stat.S_ISREG(metadata.st_mode):
        raise TransferError(f"{label} must be a regular file: {candidate}")
    _owned(metadata, label=label)
    return resolved


def _maintenance_roots(layout: StateLayout) -> tuple[Path, ...]:
    candidates = layout.maintenance_roots or (layout.root,)
    canonical: set[Path] = set()
    for root in candidates:
        canonical.add(_real_directory(root, label="maintenance root"))
    ordered = list(canonical)
    ordered.sort(key=lambda item: item.as_posix())
    return tuple(ordered)


def discover_registered_repositories(database: Path) -> tuple[Path, ...]:
    """Read canonical active Project repository roots without initializing state."""
    parent = _real_directory(database.parent, label="business database parent")
    validated = _regular_file(database, root=parent, label="business database")
    if validated is None:  # pragma: no cover - required above
        raise TransferError("business database is missing")
    try:
        connection = sqlite3.connect(_sqlite_uri(validated, immutable=True), uri=True)
        tables = {
            cast("str", row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        required = {"projects", "repository_bindings"}
        if "repository_bindings" not in tables:
            return ()
        if not required.issubset(tables):
            raise TransferError("business repository registration schema is partial")
        project_columns = {
            cast("str", row[1])
            for row in connection.execute("PRAGMA table_info('projects')")
        }
        binding_columns = {
            cast("str", row[1])
            for row in connection.execute("PRAGMA table_info('repository_bindings')")
        }
        if "active_repository_binding_id" not in project_columns or not {
            "repository_binding_id",
            "worktree_path",
        }.issubset(binding_columns):
            raise TransferError("business repository registration schema is invalid")
        rows = connection.execute(
            "SELECT DISTINCT b.worktree_path "
            "FROM projects AS p JOIN repository_bindings AS b "
            "ON b.repository_binding_id = p.active_repository_binding_id "
            "WHERE p.active_repository_binding_id IS NOT NULL "
            "ORDER BY b.worktree_path"
        ).fetchall()
    except sqlite3.Error as error:
        raise TransferError("registered repository discovery failed") from error
    finally:
        if "connection" in locals():
            connection.close()
    repositories: list[Path] = []
    for row in rows:
        raw = _text_value(row[0])
        if raw is None:
            raise TransferError("registered repository path is invalid")
        repositories.append(_real_directory(Path(raw), label="registered repository"))
    if len(repositories) != len(set(repositories)):
        raise TransferError("registered repository roots alias each other")
    return _expand_repository_groups(tuple(repositories))


def _validate_repository_boundaries(
    repositories: tuple[Path, ...],
    artifacts: Path,
    files: Sequence[Path],
) -> None:
    if len(repositories) != len(set(repositories)):
        raise TransferError("repository roots alias each other")
    for index, repository in enumerate(repositories):
        for other in repositories[index + 1 :]:
            if repository.is_relative_to(other) or other.is_relative_to(repository):
                raise TransferError("repository roots must not overlap")
        if artifacts.is_relative_to(repository) or repository.is_relative_to(artifacts):
            raise TransferError("repository and artifact roots must not overlap")
        if any(item.is_relative_to(repository) for item in files):
            raise TransferError("repository root contains a state file")


def _validate_layout(  # noqa: C901, PLR0912
    layout: StateLayout,
    maintenance_roots: tuple[Path, ...],
) -> tuple[
    Path,
    Path,
    Path | None,
    Path,
    Path,
    tuple[Path, ...],
    tuple[Path, ...],
]:
    root = _real_directory(layout.root, label="state root")
    business = _regular_file(
        layout.business_database,
        root=root,
        label="business database",
    )
    trace = _regular_file(
        layout.trace_database,
        root=root,
        label="trace database",
        required=False,
    )
    artifacts = _real_directory(layout.artifacts, label="artifact root")
    if not artifacts.is_relative_to(root):
        raise TransferError(f"artifact root escapes the state root: {artifacts}")
    model_config = _regular_file(
        layout.model_config,
        root=root,
        label="model configuration",
    )
    if business is None or model_config is None:  # pragma: no cover - required above
        raise TransferError("required state layout file is absent")
    files = [business, model_config]
    if trace is not None:
        files.append(trace)
    provenance = tuple(
        cast(
            "Path",
            _regular_file(
                path,
                root=root,
                label="state provenance file",
            ),
        )
        for path in layout.provenance_files
    )
    provenance_names = [path.name for path in provenance]
    if len(provenance_names) != len(set(provenance_names)):
        raise TransferError("state provenance basenames must be unique")
    for path in provenance:
        relative = PurePosixPath(path.name)
        if _is_secret_path(relative, admin=False):
            raise TransferError("state provenance must not contain a secret file")
    files.extend(provenance)
    identities = [(item.stat().st_dev, item.stat().st_ino) for item in files]
    if len(identities) != len(set(identities)):
        raise TransferError("state layout paths alias the same regular file")

    registered = (
        discover_registered_repositories(business)
        if layout.include_registered_repositories
        else ()
    )
    if layout.repositories is None:
        repositories = registered
    else:
        repositories = tuple(
            _real_directory(path, label="repository root")
            for path in layout.repositories
        )
        expanded_repositories = _expand_repository_groups(repositories)
        if set(expanded_repositories) != set(repositories):
            raise TransferError(
                "explicit repository set omits a main or linked worktree"
            )
        if layout.include_registered_repositories and not set(registered).issubset(
            repositories
        ):
            raise TransferError("backup omits an active registered repository")
    if not layout.include_registered_repositories and repositories:
        raise TransferError("state-only backup must not include repositories")
    for repository in repositories:
        external = tuple(path for _, path in _git_external_roots(repository))
        if not all(
            any(path.is_relative_to(root) for root in maintenance_roots)
            for path in (repository, *external)
        ):
            raise TransferError("repository content escapes maintenance fence roots")
    _validate_repository_boundaries(repositories, artifacts, files)
    return root, business, trace, artifacts, model_config, repositories, provenance


def _publication_target(destination: Path) -> Path:
    target = destination.expanduser().absolute()
    if target.name in {"", ".", ".."}:
        raise TransferError("transfer destination must name a child directory")
    parent = _real_directory(target.parent, label="transfer destination parent")
    target = parent / target.name
    try:
        target.lstat()
    except FileNotFoundError:
        return target
    raise TransferError(f"transfer destination already exists: {target}")


def _safe_rmtree(path: Path) -> None:
    target = path
    if not target.exists():
        return

    def _unlock_and_remove(func: Callable[[str], object], p: str, _: object) -> None:
        with suppress(OSError):
            Path(p).chmod(stat.S_IWRITE)
            func(p)

    try:
        shutil.rmtree(target, onexc=_unlock_and_remove)
    except TypeError:
        shutil.rmtree(target, onerror=_unlock_and_remove)


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | _O_CLOEXEC | _O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise TransferError(f"transfer payload entry is not regular: {path}")
        _owned(metadata, label="transfer payload file")
        while chunk := os.read(descriptor, _COPY_CHUNK_BYTES):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _copy_regular(source: Path, destination: Path) -> None:
    source_metadata = source.lstat()
    if not stat.S_ISREG(source_metadata.st_mode):
        raise TransferError(f"source changed from a regular file: {source}")
    _owned(source_metadata, label="transfer source file")
    _reject_privileged_mode(source_metadata, label="transfer source file")
    flags = os.O_RDONLY | _O_CLOEXEC | _O_NOFOLLOW
    source_fd = os.open(source, flags)
    try:
        opened = os.fstat(source_fd)
        if (opened.st_dev, opened.st_ino) != (
            source_metadata.st_dev,
            source_metadata.st_ino,
        ):
            raise TransferError(f"source changed during transfer: {source}")
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_CLOEXEC,
            stat.S_IMODE(opened.st_mode),
        )
        try:
            while chunk := os.read(source_fd, _COPY_CHUNK_BYTES):
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    view = view[written:]
            os.fchmod(destination_fd, stat.S_IMODE(opened.st_mode))
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)


def _is_secret_path(relative: PurePosixPath, *, admin: bool) -> bool:
    name = relative.name
    if name == ".env" or name.startswith(".env."):
        return True
    if name in {
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".envrc",
        "auth.json",
        "credentials",
        "credential-store",
    }:
        return True
    if name in {"id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"} or name.endswith(
        (".pem", ".key")
    ):
        return True
    parts = relative.parts
    return (admin and parts in {("config",), ("config.worktree",)}) or parts[-2:] in {
        (".git", "config"),
        (".git", "config.worktree"),
        (".git", "credentials"),
        (".aws", "credentials"),
    }


def _safe_link(source_root: Path, link: Path, relative: PurePosixPath) -> str:
    target = str(link.readlink())
    target_path = Path(target)
    if target_path.is_absolute():
        raise TransferError(f"repository symlink must be relative: {relative}")
    resolved = (link.parent / target_path).resolve(strict=False)
    if not resolved.is_relative_to(source_root):
        raise TransferError(f"repository symlink escapes its root: {relative}")
    return target


def _is_tree_excluded(relative: PurePosixPath, *, admin: bool) -> bool:
    if not admin and relative.parts and relative.parts[0] == ".worktrees":
        return True
    return _is_secret_path(relative, admin=admin)


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    allow_symlinks: bool,
    admin: bool = False,
) -> list[str]:
    metadata = source.lstat()
    _owned(metadata, label="transfer source directory")
    _reject_privileged_mode(metadata, label="transfer source directory")
    destination.mkdir(mode=stat.S_IMODE(metadata.st_mode))
    destination.chmod(stat.S_IMODE(metadata.st_mode))
    excluded: list[str] = []

    def visit(
        source_directory: Path,
        destination_directory: Path,
        prefix: Path,
    ) -> None:
        with os.scandir(source_directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
        for entry in entries:
            relative_path = prefix / entry.name
            relative = PurePosixPath(relative_path.as_posix())
            if _is_tree_excluded(relative, admin=admin):
                excluded.append(relative.as_posix())
                continue
            source_path = Path(entry.path)
            destination_path = destination_directory / entry.name
            entry_metadata = entry.stat(follow_symlinks=False)
            _owned(entry_metadata, label="transfer tree entry")
            _reject_privileged_mode(entry_metadata, label="transfer tree entry")
            if stat.S_ISDIR(entry_metadata.st_mode):
                destination_path.mkdir(mode=stat.S_IMODE(entry_metadata.st_mode))
                destination_path.chmod(stat.S_IMODE(entry_metadata.st_mode))
                visit(source_path, destination_path, relative_path)
            elif stat.S_ISREG(entry_metadata.st_mode):
                _copy_regular(source_path, destination_path)
            elif stat.S_ISLNK(entry_metadata.st_mode) and allow_symlinks:
                target = _safe_link(source, source_path, relative)
                destination_path.symlink_to(target)
            elif stat.S_ISLNK(entry_metadata.st_mode):
                raise TransferError(f"artifact tree contains a symlink: {relative}")
            else:
                raise TransferError(
                    f"transfer tree contains a special file: {relative}"
                )

    visit(source, destination, Path())
    return excluded


def _sqlite_uri(path: Path, *, immutable: bool) -> str:
    immutable_query = "&immutable=1" if immutable else ""
    return f"{path.as_uri()}?mode=ro{immutable_query}"


def _backup_database(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(_sqlite_uri(source, immutable=False), uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    except sqlite3.Error as error:
        raise TransferError("SQLite backup failed") from error
    finally:
        destination_connection.close()
        source_connection.close()
    flags = os.O_RDONLY | _O_CLOEXEC | _O_NOFOLLOW
    descriptor = os.open(destination, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    destination.chmod(stat.S_IMODE(source.stat().st_mode))


def _canonical_cell(value: object) -> dict[str, object]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "real", "ieee754": struct.pack(">d", value).hex()}
    if isinstance(value, str):
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return {"type": "text", "base64": encoded}
    if isinstance(value, bytes):
        encoded = base64.b64encode(value).decode("ascii")
        return {"type": "blob", "base64": encoded}
    raise TransferError(f"unsupported SQLite value type: {type(value).__name__}")


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _text_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def _quoted_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_inventory(
    connection: sqlite3.Connection,
    table_name: str,
) -> tuple[dict[str, object], _DatabaseFacts]:
    escaped_literal = table_name.replace("'", "''")
    column_rows = connection.execute(
        f"PRAGMA table_info('{escaped_literal}')"
    ).fetchall()
    columns = [_text_value(row[1]) for row in column_rows]
    if any(column is None for column in columns):
        raise TransferError("SQLite table contains a non-text column name")
    column_names = cast("list[str]", columns)
    primary_key = [
        cast("str", column_names[index])
        for index, row in sorted(
            enumerate(column_rows),
            key=lambda item: int(item[1][5]) if int(item[1][5]) else 2**31,
        )
        if int(row[5])
    ]
    rows = connection.execute(
        f"SELECT * FROM {_quoted_identifier(table_name)}"  # noqa: S608 # nosec B608
    ).fetchall()
    row_hashes: Counter[str] = Counter()
    attempts: set[str] = set()
    sessions: set[str] = set()
    links: set[tuple[str, str]] = set()
    attempt_index = (
        column_names.index("attempt_fingerprint")
        if "attempt_fingerprint" in column_names
        else None
    )
    session_column = next(
        (
            candidate
            for candidate in ("session_id", "id")
            if candidate in column_names and "session" in table_name.lower()
        ),
        None,
    )
    session_index = (
        column_names.index(session_column) if session_column is not None else None
    )
    state_index = (
        column_names.index("state")
        if session_index is not None and "state" in column_names
        else None
    )
    for row in rows:
        encoded = [_canonical_cell(value) for value in row]
        row_hashes[_sha256_bytes(_canonical_json(encoded))] += 1
        attempt = _text_value(row[attempt_index]) if attempt_index is not None else None
        session = _text_value(row[session_index]) if session_index is not None else None
        if attempt is None and state_index is not None:
            state = _text_value(row[state_index])
            try:
                state_payload = json.loads(state) if state is not None else None
            except json.JSONDecodeError:
                state_payload = None
            if isinstance(state_payload, dict):
                state_attempt = state_payload.get("attempt_fingerprint")
                if isinstance(state_attempt, str):
                    attempt = state_attempt
        if attempt is not None:
            attempts.add(attempt)
        if session is not None:
            sessions.add(session)
        if attempt is not None and session is not None:
            links.add((attempt, session))
    hashes = [
        {"sha256": digest, "count": count}
        for digest, count in sorted(row_hashes.items())
    ]
    inventory: dict[str, object] = {
        "columns": column_names,
        "declared_primary_key": primary_key,
        "row_count": len(rows),
        "row_hashes": hashes,
        "rows_sha256": _sha256_bytes(_canonical_json(hashes)),
    }
    facts = _DatabaseFacts(
        attempts=frozenset(attempts),
        sessions=frozenset(sessions),
        links=tuple(sorted(links)),
    )
    return inventory, facts


def _database_inventory(path: Path) -> tuple[dict[str, object], _DatabaseFacts]:
    try:
        connection = sqlite3.connect(_sqlite_uri(path, immutable=True), uri=True)
        connection.text_factory = bytes
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [(b"ok",)]:
            raise TransferError("SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise TransferError("SQLite foreign-key check failed")
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version != 0:
            raise TransferError(
                f"unsupported SQLite application schema: {user_version}"
            )
        schema_rows = connection.execute(
            "SELECT type, name, tbl_name, rootpage, sql "
            "FROM sqlite_schema ORDER BY type, name"
        ).fetchall()
        encoded_schema = [
            [_canonical_cell(value) for value in row] for row in schema_rows
        ]
        table_names = [
            cast("str", _text_value(row[1]))
            for row in schema_rows
            if _text_value(row[0]) == "table"
        ]
        application_tables = [
            name for name in table_names if not name.startswith("sqlite_")
        ]
        if not application_tables:
            raise TransferError("unsupported SQLite application schema: no tables")
        tables: dict[str, object] = {}
        attempts: set[str] = set()
        sessions: set[str] = set()
        links: set[tuple[str, str]] = set()
        for table_name in sorted(table_names):
            inventory, facts = _table_inventory(connection, table_name)
            tables[table_name] = inventory
            attempts.update(facts.attempts)
            sessions.update(facts.sessions)
            links.update(facts.links)
    except sqlite3.Error as error:
        raise TransferError("SQLite inventory failed") from error
    finally:
        if "connection" in locals():
            connection.close()
    inventory: dict[str, object] = {
        "present": True,
        "user_version": user_version,
        "schema": encoded_schema,
        "schema_sha256": _sha256_bytes(_canonical_json(encoded_schema)),
        "tables": tables,
        "session_ids": sorted(sessions),
    }
    return inventory, _DatabaseFacts(
        attempts=frozenset(attempts),
        sessions=frozenset(sessions),
        links=tuple(sorted(links)),
    )


def _database_manifest(
    root: Path,
) -> tuple[dict[str, dict[str, object]], tuple[dict[str, str], ...]]:
    inventories: dict[str, dict[str, object]] = {}
    facts: dict[str, _DatabaseFacts] = {}
    for role, relative in _DATABASE_PATHS.items():
        database = root / relative
        if not database.exists():
            inventories[role] = {"path": relative, "present": False}
            facts[role] = _DatabaseFacts(frozenset(), frozenset(), ())
            continue
        inventory, database_facts = _database_inventory(database)
        inventory["path"] = relative
        inventories[role] = inventory
        facts[role] = database_facts
    business_attempts = facts["business"].attempts
    links = tuple(
        {
            "attempt_fingerprint": attempt,
            "session_id": session,
        }
        for attempt, session in facts["trace"].links
        if attempt in business_attempts
    )
    return inventories, links


def _symlink_scope(root: Path, relative: PurePosixPath) -> Path:
    parts = relative.parts
    if parts[:1] == ("repositories",) and len(parts) >= _REPOSITORY_LINK_MIN_PARTS:
        return root.joinpath(*parts[:2])
    if parts[:1] == ("repository-admin",) and len(parts) >= _ADMIN_LINK_MIN_PARTS:
        return root.joinpath(*parts[:3])
    raise TransferError(f"symlink outside repository payload: {relative}")


def _entry_record(root: Path, path: Path, relative: PurePosixPath) -> FileRecord:
    metadata = path.lstat()
    _owned(metadata, label="transfer payload entry")
    _reject_privileged_mode(metadata, label="transfer payload entry")
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISDIR(metadata.st_mode):
        return FileRecord(path=relative.as_posix(), kind="directory", mode=mode)
    if stat.S_ISREG(metadata.st_mode):
        return FileRecord(
            path=relative.as_posix(),
            kind="file",
            mode=mode,
            size=metadata.st_size,
            sha256=_sha256_file(path),
        )
    if stat.S_ISLNK(metadata.st_mode):
        target = str(path.readlink())
        if Path(target).is_absolute():
            raise TransferError(f"absolute symlink in transfer payload: {relative}")
        scope = _symlink_scope(root, relative).resolve(strict=True)
        resolved = (path.parent / target).resolve(strict=False)
        if not resolved.is_relative_to(scope):
            raise TransferError(f"repository symlink escapes payload: {relative}")
        content = os.fsencode(target)
        return FileRecord(
            path=relative.as_posix(),
            kind="symlink",
            mode=mode,
            size=len(content),
            sha256=_sha256_bytes(content),
            link_target=target,
        )
    raise TransferError(f"special file in transfer payload: {relative}")


def _file_inventory(root: Path) -> tuple[FileRecord, ...]:
    records: list[FileRecord] = []

    def visit(directory: Path, prefix: Path) -> None:
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
        for entry in entries:
            if not prefix.parts and entry.name in {_MANIFEST_NAME, _UNPUBLISHED_NAME}:
                continue
            relative_path = prefix / entry.name
            relative = PurePosixPath(relative_path.as_posix())
            record = _entry_record(root, Path(entry.path), relative)
            records.append(record)
            if record.kind == "directory":
                visit(Path(entry.path), relative_path)

    visit(root, Path())
    return tuple(sorted(records, key=lambda item: item.path))


def _git_external_roots(  # noqa: C901
    repository: Path,
) -> tuple[tuple[str, Path], ...]:
    dot_git = repository / ".git"
    try:
        metadata = dot_git.lstat()
    except FileNotFoundError:
        return ()
    if stat.S_ISDIR(metadata.st_mode):
        return ()
    if not stat.S_ISREG(metadata.st_mode):
        raise TransferError("repository .git entry must be a file or directory")
    content = dot_git.read_bytes()
    if len(content) > _GIT_POINTER_MAX_BYTES:
        raise TransferError("repository .git pointer is too large")
    try:
        line = content.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise TransferError("repository .git pointer is not UTF-8") from error
    if not line.startswith("gitdir: "):
        raise TransferError("repository .git pointer is invalid")
    raw_gitdir = Path(line.removeprefix("gitdir: "))
    gitdir = raw_gitdir if raw_gitdir.is_absolute() else repository / raw_gitdir
    gitdir = _real_directory(gitdir, label="repository Git directory")
    if gitdir.is_relative_to(repository):
        return ()
    roots: list[tuple[str, Path]] = [("gitdir", gitdir)]
    commondir_file = gitdir / "commondir"
    if commondir_file.is_file() and not commondir_file.is_symlink():
        raw_common = Path(commondir_file.read_text(encoding="utf-8").strip())
        common_candidate = (
            raw_common if raw_common.is_absolute() else gitdir / raw_common
        )
        try:
            common = common_candidate.resolve(strict=True)
        except OSError as error:
            raise TransferError("repository common directory is unavailable") from error
        common = _real_directory(common, label="repository common directory")
        if not common.is_relative_to(gitdir) and not common.is_relative_to(repository):
            roots.append(("common", common))
    return tuple(roots)


def _expand_repository_groups(repositories: tuple[Path, ...]) -> tuple[Path, ...]:
    expanded: set[Path] = set(repositories)
    common_roots: set[Path] = set()
    for repository in repositories:
        external = dict(_git_external_roots(repository))
        common_roots.add(external.get("common", repository / ".git"))
    for common_candidate in common_roots:
        common = _real_directory(
            common_candidate,
            label="repository common directory",
        )
        main = common.parent
        if common.name == ".git" and (main / ".git").is_dir():
            expanded.add(_real_directory(main, label="repository main worktree"))
        registrations = common / "worktrees"
        if not registrations.exists():
            continue
        registrations = _real_directory(
            registrations,
            label="repository worktree registrations",
        )
        for admin_candidate in sorted(
            registrations.iterdir(), key=lambda item: item.name
        ):
            admin = _real_directory(
                admin_candidate,
                label="repository worktree registration",
            )
            pointer = _regular_file(
                admin / "gitdir",
                root=admin,
                label="repository worktree pointer",
            )
            if pointer is None:  # pragma: no cover - required above
                raise TransferError("repository worktree pointer is missing")
            raw = Path(pointer.read_text(encoding="utf-8").strip())
            git_file = raw if raw.is_absolute() else admin / raw
            worktree = git_file.parent
            expanded.add(_real_directory(worktree, label="repository linked worktree"))
    return tuple(
        sorted(
            expanded,
            key=lambda item: (
                str(_repository_common_root(item)),
                0 if (item / ".git").is_dir() else 1,
                item.as_posix(),
            ),
        )
    )


def _repository_common_root(repository: Path) -> Path:
    external = dict(_git_external_roots(repository))
    return external.get("common", repository / ".git").resolve(strict=True)


def _approved_git_config(common_git_dir: Path) -> dict[str, dict[str, str]]:
    parser = configparser.RawConfigParser(interpolation=None, strict=False)
    config = common_git_dir / "config"
    if config.is_file() and not config.is_symlink():
        try:
            parser.read(config, encoding="utf-8")
        except configparser.Error as error:
            raise TransferError("repository Git configuration is invalid") from error
    approved: dict[str, dict[str, str]] = {"core": {}, "extensions": {}}
    for section_name, allowed in (
        ("core", _APPROVED_CORE_KEYS),
        ("extensions", _APPROVED_EXTENSION_KEYS),
    ):
        if not parser.has_section(section_name):
            continue
        for key, value in parser.items(section_name, raw=True):
            if key.casefold() in allowed:
                if any(character in value for character in ("\x00", "\r", "\n")):
                    raise TransferError("repository Git configuration is unsafe")
                approved[section_name][key.casefold()] = value
    return approved


def _repository_identity(
    repository: Path,
    *,
    excluded: Sequence[str] = (),
) -> dict[str, object]:
    try:
        observed = GitPythonRepositoryProbe().inspect(repository)
    except RepositoryProbeError as error:
        raise TransferError("repository semantic identity is unavailable") from error
    entries = observed.status_entries
    if excluded:
        excluded_patterns = {
            item.replace("\\", "/").rstrip("/")
            for item in excluded
            if not item.startswith(("gitdir:", "commondir:"))
        }
        filtered: list[RepositoryStatusEntry] = []
        for entry in entries:
            entry_path = entry.path.replace("\\", "/")
            prev_path = (
                entry.previous_path.replace("\\", "/") if entry.previous_path else None
            )
            if any(
                entry_path == excl or entry_path.startswith(f"{excl}/")
                for excl in excluded_patterns
            ):
                continue
            if prev_path and any(
                prev_path == excl or prev_path.startswith(f"{excl}/")
                for excl in excluded_patterns
            ):
                continue
            filtered.append(entry)
        entries = tuple(filtered)
        dirty = bool(entries)
        status_entries_json = [item.model_dump(mode="json") for item in entries]
        remote_omitted = any(w.code == "REMOTE_OMITTED" for w in observed.warnings)
        fingerprint_payload = {
            "probe_version": observed.probe_version,
            "head_sha": observed.head_sha,
            "branch_name": observed.branch_name,
            "detached_head": observed.detached_head,
            "dirty": dirty,
            "status_entries": status_entries_json,
            "remotes": list(observed.remotes),
            "remote_omitted": remote_omitted,
        }
        status_fingerprint = canonical_hash(fingerprint_payload)
    else:
        dirty = observed.dirty
        status_entries_json = [
            item.model_dump(mode="json") for item in observed.status_entries
        ]
        status_fingerprint = observed.status_fingerprint

    return {
        "head_sha": observed.head_sha,
        "common_git_dir": observed.common_git_dir,
        "branch_name": observed.branch_name,
        "detached_head": observed.detached_head,
        "dirty": dirty,
        "status_entries": status_entries_json,
        "status_fingerprint": status_fingerprint,
        "remotes": list(observed.remotes),
        "probe_version": observed.probe_version,
        "approved_config": _approved_git_config(Path(observed.common_git_dir)),
    }


def _capture_repositories(
    repositories: Sequence[Path],
    staging: Path,
) -> tuple[dict[str, object], ...]:
    if not repositories:
        return ()
    payload_root = staging / "repositories"
    payload_root.mkdir(mode=0o700)
    admin_root = staging / "repository-admin"
    manifests: list[dict[str, object]] = []
    for index, repository in enumerate(repositories):
        label = f"{index:04d}"
        destination = payload_root / label
        worktree_excluded = _copy_tree(repository, destination, allow_symlinks=True)
        excluded = list(worktree_excluded)
        components: list[dict[str, object]] = [
            {"kind": "worktree", "payload": f"repositories/{label}"}
        ]
        external_roots = _git_external_roots(repository)
        if external_roots:
            admin_root.mkdir(mode=0o700, exist_ok=True)
            repository_admin = admin_root / label
            repository_admin.mkdir(mode=0o700)
            for kind, source in external_roots:
                admin_destination = repository_admin / kind
                admin_excluded = _copy_tree(
                    source,
                    admin_destination,
                    allow_symlinks=True,
                    admin=True,
                )
                excluded.extend(f"{kind}:{item}" for item in admin_excluded)
                components.append(
                    {
                        "kind": kind,
                        "payload": f"repository-admin/{label}/{kind}",
                    }
                )
        manifests.append(
            {
                "index": index,
                "source_path": str(repository),
                "identity": _repository_identity(
                    repository,
                    excluded=worktree_excluded,
                ),
                "components": components,
                "excluded": sorted(excluded),
            }
        )
    return tuple(manifests)


def _write_manifest(root: Path, manifest: TransferManifest) -> None:
    descriptor, raw_temporary = tempfile.mkstemp(prefix=".manifest-", dir=root)
    temporary = Path(raw_temporary)
    try:
        content = _canonical_json(manifest.to_dict()) + b"\n"
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(root / _MANIFEST_NAME)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_unpublished_marker(root: Path, target_name: str) -> None:
    marker = root / _UNPUBLISHED_NAME
    descriptor = os.open(
        marker,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_CLOEXEC,
        0o600,
    )
    try:
        os.write(descriptor, target_name.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reject_unpublished(root: Path) -> None:
    try:
        root.joinpath(_UNPUBLISHED_NAME).lstat()
    except FileNotFoundError:
        return
    raise TransferError("transfer bundle publication is incomplete")


def _load_manifest(bundle: Path) -> TransferManifest:
    manifest_path = bundle / _MANIFEST_NAME
    try:
        metadata = manifest_path.lstat()
    except FileNotFoundError as error:
        raise TransferError("transfer bundle manifest is missing") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise TransferError("transfer bundle manifest must be a regular file")
    _owned(metadata, label="transfer bundle manifest")
    if metadata.st_size > _MAX_MANIFEST_BYTES:
        raise TransferError("transfer bundle manifest is too large")
    try:
        payload = json.loads(manifest_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TransferError("transfer bundle manifest is invalid JSON") from error
    return TransferManifest.from_dict(payload)


def _verify_payload(root: Path, manifest: TransferManifest) -> None:
    actual_files = _file_inventory(root)
    if actual_files != manifest.files:
        expected_paths = {record.path for record in manifest.files}
        actual_paths = {record.path for record in actual_files}
        if actual_paths - expected_paths:
            raise TransferError("transfer payload contains unlisted entries")
        if expected_paths - actual_paths:
            raise TransferError("transfer payload is missing inventory entries")
        raise TransferError("transfer payload does not match byte inventory")
    _verify_repository_inventory(manifest)
    databases, observed_links = _database_manifest(root)
    if databases != manifest.databases:
        raise TransferError("transfer database logical inventory does not match")
    if observed_links != manifest.observed_links:
        raise TransferError("transfer observed session links do not match")
    model_config = root / "model-config"
    if _sha256_file(model_config) != manifest.model_config_sha256:
        raise TransferError("transfer model configuration does not match inventory")


def _verify_repository_inventory(manifest: TransferManifest) -> None:
    file_records = {record.path: record for record in manifest.files}
    observed_indexes: set[str] = set()
    for record in manifest.files:
        parts = PurePosixPath(record.path).parts
        if len(parts) >= _REPOSITORY_PATH_MIN_PARTS and parts[0] == "repositories":
            observed_indexes.add(parts[1])
        if len(parts) >= _REPOSITORY_PATH_MIN_PARTS and parts[0] == "repository-admin":
            observed_indexes.add(parts[1])
    expected_indexes = {f"{index:04d}" for index in range(len(manifest.repositories))}
    if observed_indexes != expected_indexes:
        raise TransferError("transfer repository inventory does not match payload")
    for repository in manifest.repositories:
        components = cast("list[dict[str, object]]", repository["components"])
        for component in components:
            payload = cast("str", component["payload"])
            record = file_records.get(payload)
            if record is None or record.kind != "directory":
                raise TransferError("transfer repository inventory is incomplete")


def verify_current_business_schema(database: Path) -> None:
    """Require the exact reviewed business schema without creating or migrating it."""
    parent = _real_directory(database.parent, label="business database parent")
    validated = _regular_file(
        database,
        root=parent,
        label="business database",
    )
    if validated is None:  # pragma: no cover - required above
        raise TransferError("business database is missing")
    previous_database = os.environ.get(_BUSINESS_DATABASE_ENV)
    previous_launcher_child = os.environ.get(_LAUNCHER_CHILD_ENV)
    os.environ[_BUSINESS_DATABASE_ENV] = str(validated)
    os.environ[_LAUNCHER_CHILD_ENV] = "1"
    get_business_db_target = None
    try:
        from utils.runtime_config import get_business_db_target  # noqa: PLC0415

        get_business_db_target.cache_clear()
        from models.db import (  # noqa: PLC0415
            CURRENT_BUSINESS_SCHEMA_MANIFEST,
            _inspect_business_schema_manifest,
        )
    finally:
        if previous_database is None:
            os.environ.pop(_BUSINESS_DATABASE_ENV, None)
        else:
            os.environ[_BUSINESS_DATABASE_ENV] = previous_database
        if previous_launcher_child is None:
            os.environ.pop(_LAUNCHER_CHILD_ENV, None)
        else:
            os.environ[_LAUNCHER_CHILD_ENV] = previous_launcher_child
        if get_business_db_target is not None:
            get_business_db_target.cache_clear()
    engine = create_engine(
        "sqlite://",
        creator=lambda: sqlite3.connect(
            _sqlite_uri(validated, immutable=True),
            uri=True,
        ),
    )
    try:
        observed = _inspect_business_schema_manifest(engine)
    except (SQLAlchemyError, sqlite3.Error) as error:
        raise TransferError("current business schema inspection failed") from error
    finally:
        engine.dispose()
    if observed != CURRENT_BUSINESS_SCHEMA_MANIFEST:
        raise TransferError("unsupported current business schema")


def _trace_schema_shape(path: Path) -> tuple[tuple[str, str, str, str | None], ...]:
    try:
        connection = sqlite3.connect(_sqlite_uri(path, immutable=True), uri=True)
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version != 0:
            raise TransferError("trace database schema version is unsupported")
        rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
        ).fetchall()
    except sqlite3.Error as error:
        raise TransferError("trace database schema inspection failed") from error
    finally:
        if "connection" in locals():
            connection.close()
    return tuple(
        (
            cast("str", row[0]),
            cast("str", row[1]),
            cast("str", row[2]),
            None if row[3] is None else " ".join(cast("str", row[3]).split()),
        )
        for row in rows
    )


@cache
def _current_trace_schema_shape() -> tuple[tuple[str, str, str, str | None], ...]:
    async def initialize(path: Path) -> None:
        from google.adk.sessions import DatabaseSessionService  # noqa: PLC0415

        service = DatabaseSessionService(
            db_url=f"sqlite+aiosqlite:///{path.as_posix()}"
        )
        try:
            await service.create_session(
                app_name="agileforge-schema-probe",
                user_id="local-schema-probe",
                session_id="schema-probe",
                state={},
            )
        finally:
            await service.close()

    with tempfile.TemporaryDirectory(prefix="agileforge-trace-schema-") as directory:
        database = Path(directory) / "trace.sqlite3"
        # The gate is synchronous but also runs under the dashboard's live event
        # loop, so the probe gets a private loop on a worker thread.
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(asyncio.run, initialize(database)).result()
        return _trace_schema_shape(database)


def verify_current_trace_schema(database: Path) -> None:
    """Require the exact locked ADK trace schema without mutating the input DB."""
    parent = _real_directory(database.parent, label="trace database parent")
    validated = _regular_file(database, root=parent, label="trace database")
    if validated is None:  # pragma: no cover - required above
        raise TransferError("trace database is missing")
    if _trace_schema_shape(validated) != _current_trace_schema_shape():
        raise TransferError("unsupported current trace schema")


def backup_state(
    layout: StateLayout,
    destination: Path,
    *,
    maintenance_fences_held: bool = False,
) -> Path:
    """Capture and atomically publish one verified portable state bundle.

    Every effective ``maintenance_roots`` entry is locked exclusively in sorted
    canonical order. The trace database may be absent and is never fabricated.
    """
    target = _publication_target(destination)
    maintenance_roots = _maintenance_roots(layout)
    staging: Path | None = None
    with ExitStack() as stack:
        if not maintenance_fences_held:
            for root in maintenance_roots:
                stack.enter_context(runtime_fence(root, exclusive=True))
        (
            _,
            business,
            trace,
            artifacts,
            model_config,
            repositories,
            provenance,
        ) = _validate_layout(layout, maintenance_roots)
        captured_trees = [artifacts, *repositories]
        for repository in repositories:
            captured_trees.extend(path for _, path in _git_external_roots(repository))
        if any(target.is_relative_to(source) for source in captured_trees):
            raise TransferError("backup destination overlaps a captured source tree")
        staging = Path(
            tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent)
        )
        try:
            _write_unpublished_marker(staging, target.name)
            _backup_database(business, staging / "business.sqlite3")
            if trace is not None:
                _backup_database(trace, staging / "trace.sqlite3")
            _copy_tree(artifacts, staging / "artifacts", allow_symlinks=False)
            _copy_regular(model_config, staging / "model-config")
            if provenance:
                provenance_root = staging / "provenance"
                provenance_root.mkdir(mode=0o700)
                for source in provenance:
                    _copy_regular(source, provenance_root / source.name)
            repository_manifest = _capture_repositories(repositories, staging)
            databases, observed_links = _database_manifest(staging)
            manifest = TransferManifest(
                format=_FORMAT,
                created_at=datetime.now(tz=UTC).isoformat(),
                files=_file_inventory(staging),
                databases=databases,
                repositories=repository_manifest,
                model_config_sha256=_sha256_file(staging / "model-config"),
                observed_links=observed_links,
            )
            _write_manifest(staging, manifest)
            _verify_payload(staging, manifest)
            _fsync_directory(staging)
            staging.rename(target)
            staging = None
            _fsync_directory(target.parent)
            marker = target / _UNPUBLISHED_NAME
            if marker.read_text(encoding="utf-8") != target.name:
                raise TransferError("transfer publication marker does not match target")
            marker.unlink()
            _fsync_directory(target)
            _fsync_directory(target.parent)
            verify_backup(target)
        finally:
            if staging is not None:
                _safe_rmtree(staging)
    return target


def verify_backup(bundle: Path) -> TransferManifest:
    """Verify bundle ownership, complete bytes, schemas, rows, and links."""
    root = _real_directory(bundle, label="transfer bundle")
    _reject_unpublished(root)
    manifest = _load_manifest(root)
    _verify_payload(root, manifest)
    return manifest


def _copy_manifest_payload(
    bundle: Path,
    destination: Path,
    files: Iterable[FileRecord],
) -> None:
    records = tuple(files)
    for record in sorted(
        (item for item in records if item.kind == "directory"),
        key=lambda item: (len(PurePosixPath(item.path).parts), item.path),
    ):
        target = destination.joinpath(*_validated_relative(record.path).parts)
        target.mkdir(mode=record.mode)
        target.chmod(record.mode)
    for record in (item for item in records if item.kind != "directory"):
        relative = _validated_relative(record.path)
        source = bundle.joinpath(*relative.parts)
        target = destination.joinpath(*relative.parts)
        if record.kind == "file":
            _copy_regular(source, target)
        else:
            if record.link_target is None:  # pragma: no cover - parser invariant
                raise TransferError("manifest symlink target is absent")
            target.symlink_to(record.link_target)


def restore_payload(bundle: Path, destination: Path) -> TransferManifest:
    """Verify and atomically publish payload files into a missing directory.

    This primitive intentionally does not infer a maintenance lock from the
    destination. Its caller must hold the trusted, sorted exclusive workspace
    or deployment fences through this call and final profile/runtime manifest
    publication. This function never initializes application stores.
    """
    source = _real_directory(bundle, label="transfer bundle")
    manifest = verify_backup(source)
    target = _publication_target(destination)
    staging: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent)
    )
    try:
        _copy_manifest_payload(source, staging, manifest.files)
        _verify_payload(staging, manifest)
        staging.rename(target)
        staging = None
    finally:
        if staging is not None:
            _safe_rmtree(staging)
    return manifest


def restored_repositories(
    manifest: TransferManifest,
    destination: Path,
) -> tuple[RestoredRepository, ...]:
    """Map verified repository components to restored paths and source groups."""
    root = _real_directory(destination, label="restored payload root")
    restored: list[RestoredRepository] = []
    for record in manifest.repositories:
        index = cast("int", record["index"])
        components = cast("list[dict[str, object]]", record["components"])
        paths = {
            cast("str", component["kind"]): root.joinpath(
                *_validated_relative(cast("str", component["payload"])).parts
            )
            for component in components
        }
        worktree = _real_directory(paths["worktree"], label="restored worktree")
        gitdir = (
            _real_directory(paths["gitdir"], label="restored Git directory")
            if "gitdir" in paths
            else None
        )
        common = (
            _real_directory(paths["common"], label="restored common Git directory")
            if "common" in paths
            else None
        )
        identity = cast("dict[str, object]", record["identity"])
        restored.append(
            RestoredRepository(
                index=index,
                source_path=cast("str", record["source_path"]),
                common_repository=cast("str", identity["common_git_dir"]),
                worktree=worktree,
                gitdir=gitdir,
                common=common,
                identity=identity,
            )
        )
    return tuple(restored)


def install_approved_git_config(  # noqa: C901
    repositories: tuple[RestoredRepository, ...],
) -> None:
    """Rebuild only reviewed core/extensions settings and sanitized remote URLs."""
    groups: dict[str, list[RestoredRepository]] = {}
    for repository in repositories:
        groups.setdefault(repository.common_repository, []).append(repository)
    for group in groups.values():
        mains = [item for item in group if (item.worktree / ".git").is_dir()]
        if len(mains) != 1:
            raise TransferError("restored repository group has no unique main worktree")
        main = mains[0]
        approved = main.identity.get("approved_config")
        if not isinstance(approved, dict):  # pragma: no cover - manifest parser
            raise TransferError("approved Git configuration is absent")
        approved_sections = cast("dict[str, object]", approved)
        if any(item.identity.get("approved_config") != approved for item in group):
            raise TransferError("restored repository group configuration disagrees")
        remotes = main.identity.get("remotes")
        if not isinstance(remotes, list):  # pragma: no cover - manifest parser
            raise TransferError("approved Git remotes are absent")
        parser = configparser.RawConfigParser(interpolation=None)
        for section_name in ("core", "extensions"):
            values = cast("dict[str, str]", approved_sections[section_name])
            if values:
                parser[section_name] = values
        for index, remote in enumerate(cast("list[str]", remotes)):
            if any(character in remote for character in ("\x00", "\r", "\n")):
                raise TransferError("approved Git remote is unsafe")
            parser[f'remote "restored-{index:04d}"'] = {
                "url": remote,
                "fetch": f"+refs/heads/*:refs/remotes/restored-{index:04d}/*",
            }
        target = main.worktree / ".git" / "config"
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_CLOEXEC | _O_NOFOLLOW,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
                parser.write(handle, space_around_delimiters=True)
                handle.flush()
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def verify_restored_repositories(
    repositories: tuple[RestoredRepository, ...],
) -> None:
    """Require every restored Git worktree to match its captured semantic state."""
    observed_groups: dict[str, set[str]] = {}
    for repository in repositories:
        observed = _repository_identity(repository.worktree)
        expected = dict(repository.identity)
        expected_common = cast("str", expected.pop("common_git_dir"))
        observed_common = cast("str", observed.pop("common_git_dir"))
        if observed != expected:
            raise TransferError("restored repository semantic identity changed")
        observed_groups.setdefault(expected_common, set()).add(observed_common)
    if any(len(common_paths) != 1 for common_paths in observed_groups.values()):
        raise TransferError("restored linked worktree grouping changed")


__all__ = [
    "RestoredRepository",
    "StateLayout",
    "TransferError",
    "TransferManifest",
    "backup_state",
    "discover_registered_repositories",
    "install_approved_git_config",
    "restore_payload",
    "restored_repositories",
    "verify_backup",
    "verify_current_business_schema",
    "verify_current_trace_schema",
    "verify_restored_repositories",
]
