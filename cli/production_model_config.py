"""Fenced, recoverable replacement of one installed profile's model config."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from yaml import YAMLError

from cli.production_state import (
    ProductionStateError,
    ProductionStateManifest,
    ProductionStatePaths,
    _load_production_state_pair,
    _require_owned_directory,
    _require_owned_regular_file,
    _write_private_file,
    load_production_state,
    production_state_paths,
)
from cli.state_transfer import (
    _git_external_roots,
    discover_registered_repositories,
)
from utils.model_config import parse_model_config

if TYPE_CHECKING:
    from utils.build_identity import BuildIdentity

_MARKER_NAME = "model-config-update.json"
_RECEIPT_NAME = "receipt.json"
_HASH = r"^[0-9a-f]{64}$"


class ModelConfigUpdateError(ProductionStateError):
    """An update or its explicit recovery failed a safety check."""


class _Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["agileforge.model-config-update.v1"]
    operation_id: UUID
    profile_root: str
    profile_name: str
    state_id: UUID
    backup_directory: str
    old_model_sha256: str = Field(pattern=_HASH)
    new_model_sha256: str = Field(pattern=_HASH)
    old_manifest_sha256: str = Field(pattern=_HASH)
    new_manifest_sha256: str = Field(pattern=_HASH)


class _Marker(_Receipt):
    status: Literal["applying", "complete", "recovering", "recovered"]
    receipt_sha256: str = Field(pattern=_HASH)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _owned_bytes(path: Path, *, label: str, owner_uid: int) -> bytes:
    before = _require_owned_regular_file(
        path, label=label, expected_owner_uid=owner_uid
    )
    data = path.read_bytes()
    after = _require_owned_regular_file(path, label=label, expected_owner_uid=owner_uid)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        message = f"{label} changed while being read"
        raise ModelConfigUpdateError(message)
    return data


def _marker_path(paths: ProductionStatePaths) -> Path:
    return paths.root / _MARKER_NAME


def _read_marker(paths: ProductionStatePaths, owner_uid: int) -> _Marker | None:
    path = _marker_path(paths)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        marker = _Marker.model_validate_json(
            _owned_bytes(path, label="model update marker", owner_uid=owner_uid)
        )
    except (ValidationError, ValueError) as error:
        message = "model update marker is invalid"
        raise ModelConfigUpdateError(message) from error
    if marker.profile_root != str(paths.root) or marker.profile_name != paths.root.name:
        message = "model update marker names another profile"
        raise ModelConfigUpdateError(message)
    return marker


def validate_startup_marker(
    paths: ProductionStatePaths, *, expected_owner_uid: int
) -> None:
    """Gate public startup using only owned local files, never external recovery."""
    marker = _read_marker(paths, expected_owner_uid)
    if marker is None:
        return
    if marker.status in {"applying", "recovering"}:
        message = "model update needs explicit recovery"
        raise ModelConfigUpdateError(message)
    model_sha = (
        marker.new_model_sha256
        if marker.status == "complete"
        else marker.old_model_sha256
    )
    manifest_sha = (
        marker.new_manifest_sha256
        if marker.status == "complete"
        else marker.old_manifest_sha256
    )
    model_bytes = _owned_bytes(
        paths.model_config, label="model configuration", owner_uid=expected_owner_uid
    )
    manifest_bytes = _owned_bytes(
        paths.manifest, label="production state manifest", owner_uid=expected_owner_uid
    )
    if _digest(model_bytes) != model_sha or _digest(manifest_bytes) != manifest_sha:
        message = "model update final pair has drifted"
        raise ModelConfigUpdateError(message)
    try:
        state = ProductionStateManifest.model_validate_json(manifest_bytes)
    except ValidationError as error:
        message = "model update manifest is invalid"
        raise ModelConfigUpdateError(message) from error
    if state.state_id != marker.state_id:
        message = "model update marker names another state"
        raise ModelConfigUpdateError(message)


def _publish_bytes(destination: Path, payload: bytes) -> None:
    """Atomically replace one private file and persist the containing directory."""
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
        _fsync_directory(destination.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _publish_marker(paths: ProductionStatePaths, marker: _Marker) -> None:
    _publish_bytes(
        _marker_path(paths), (marker.model_dump_json(indent=2) + "\n").encode()
    )


def _safe_destination(
    destination: Path,
    paths: ProductionStatePaths,
    deployment_root: Path,
    owner_uid: int,
) -> Path:
    if not destination.is_absolute() or destination.name in {"", ".", ".."}:
        message = "recovery directory must be an absolute new path"
        raise ModelConfigUpdateError(message)
    parent = destination.parent
    _require_owned_directory(
        parent, label="recovery parent", expected_owner_uid=owner_uid
    )
    target = parent / destination.name
    if target != destination:
        message = "recovery directory path is not canonical"
        raise ModelConfigUpdateError(message)
    allowed = (
        (Path("/workspace"), deployment_root)
        if deployment_root == Path("/var/lib/agileforge")
        else (deployment_root,)
    )
    if not any(target.is_relative_to(root) for root in allowed):
        message = "recovery directory is outside mounted maintenance roots"
        raise ModelConfigUpdateError(message)
    if target.is_relative_to(deployment_root / "profiles"):
        message = "recovery directory overlaps production profiles"
        raise ModelConfigUpdateError(message)
    forbidden = [paths.artifacts, paths.root]
    for repository in discover_registered_repositories(paths.business_database):
        forbidden.append(repository)
        forbidden.extend(root for _, root in _git_external_roots(repository))
    if any(target.is_relative_to(root) for root in forbidden):
        message = "recovery directory overlaps captured state"
        raise ModelConfigUpdateError(message)
    return target


def _receipt_bytes(receipt: _Receipt) -> bytes:
    return (receipt.model_dump_json(indent=2) + "\n").encode()


def _recorded_pair(
    paths: ProductionStatePaths, marker: _Marker, owner_uid: int
) -> tuple[str, str]:
    model_hash = _digest(
        _owned_bytes(
            paths.model_config, label="model configuration", owner_uid=owner_uid
        )
    )
    manifest_hash = _digest(
        _owned_bytes(
            paths.manifest, label="production state manifest", owner_uid=owner_uid
        )
    )
    if model_hash not in {marker.old_model_sha256, marker.new_model_sha256} or (
        manifest_hash not in {marker.old_manifest_sha256, marker.new_manifest_sha256}
    ):
        message = "live configuration differs from both recorded sides"
        raise ModelConfigUpdateError(message)
    return model_hash, manifest_hash


def _validate_saved_pair(
    directory: Path, marker: _Marker, paths: ProductionStatePaths, owner_uid: int
) -> tuple[bytes, bytes]:
    _require_owned_directory(
        directory, label="recovery directory", expected_owner_uid=owner_uid
    )
    receipt_bytes = _owned_bytes(
        directory / _RECEIPT_NAME, label="model update receipt", owner_uid=owner_uid
    )
    if _digest(receipt_bytes) != marker.receipt_sha256:
        message = "model update receipt has changed"
        raise ModelConfigUpdateError(message)
    try:
        receipt = _Receipt.model_validate_json(receipt_bytes)
    except ValidationError as error:
        message = "model update receipt is invalid"
        raise ModelConfigUpdateError(message) from error
    if receipt.model_dump() != marker.model_dump(
        exclude={"status", "receipt_sha256"}
    ) or receipt.profile_root != str(paths.root):
        message = "model update receipt does not match marker"
        raise ModelConfigUpdateError(message)
    files = {
        "old-model.yaml": marker.old_model_sha256,
        "new-model.yaml": marker.new_model_sha256,
        "old-runtime.json": marker.old_manifest_sha256,
        "new-runtime.json": marker.new_manifest_sha256,
    }
    contents: dict[str, bytes] = {}
    for name, expected in files.items():
        contents[name] = _owned_bytes(
            directory / name, label="saved model update file", owner_uid=owner_uid
        )
        if _digest(contents[name]) != expected:
            message = "saved model update file has changed"
            raise ModelConfigUpdateError(message)
    try:
        old = ProductionStateManifest.model_validate_json(contents["old-runtime.json"])
        new = ProductionStateManifest.model_validate_json(contents["new-runtime.json"])
    except ValidationError as error:
        message = "saved model update manifest is invalid"
        raise ModelConfigUpdateError(message) from error
    if (
        old.state_id != marker.state_id
        or new.state_id != marker.state_id
        or old.profile_root != paths.root
        or new.profile_root != paths.root
        or old.model_config_sha256 != marker.old_model_sha256
        or new.model_config_sha256 != marker.new_model_sha256
        or new.model_copy(update={"model_config_sha256": old.model_config_sha256})
        != old
    ):
        message = "saved model update provenance is inconsistent"
        raise ModelConfigUpdateError(message)
    return contents["old-model.yaml"], contents["old-runtime.json"]


def configure_models(  # noqa: PLR0913
    profile_root: Path,
    candidate: Path,
    backup_directory: Path,
    *,
    build: BuildIdentity,
    deployment_root: Path,
    expected_owner_uid: int,
) -> dict[str, object]:
    """Publish a new complete model config under caller-held exclusive fences."""
    state = load_production_state(
        profile_root, build=build, expected_owner_uid=expected_owner_uid
    )
    paths = production_state_paths(profile_root)
    target = _safe_destination(
        backup_directory, paths, deployment_root, expected_owner_uid
    )
    candidate_bytes = _owned_bytes(
        candidate, label="candidate model configuration", owner_uid=expected_owner_uid
    )
    try:
        parse_model_config(candidate_bytes.decode("utf-8"), require_all_roles=True)
    except (UnicodeDecodeError, ValueError, TypeError, YAMLError) as error:
        message = "candidate model configuration is invalid"
        raise ModelConfigUpdateError(message) from error
    old_model = _owned_bytes(
        paths.model_config, label="model configuration", owner_uid=expected_owner_uid
    )
    old_manifest = _owned_bytes(
        paths.manifest, label="production state manifest", owner_uid=expected_owner_uid
    )
    try:
        observed_state = ProductionStateManifest.model_validate_json(old_manifest)
    except ValidationError as error:
        message = "production manifest changed before publication"
        raise ModelConfigUpdateError(message) from error
    if observed_state != state:
        message = "production manifest changed before publication"
        raise ModelConfigUpdateError(message)
    if _digest(old_model) != state.model_config_sha256:
        message = "model configuration changed before publication"
        raise ModelConfigUpdateError(message)
    new_model_hash = _digest(candidate_bytes)
    if new_model_hash == state.model_config_sha256:
        message = "candidate model configuration is unchanged"
        raise ModelConfigUpdateError(message)
    new_state = state.model_copy(update={"model_config_sha256": new_model_hash})
    new_manifest = (new_state.model_dump_json(indent=2) + "\n").encode()
    receipt = _Receipt(
        version="agileforge.model-config-update.v1",
        operation_id=uuid4(),
        profile_root=str(paths.root),
        profile_name=state.profile_name,
        state_id=state.state_id,
        backup_directory=str(target),
        old_model_sha256=_digest(old_model),
        new_model_sha256=new_model_hash,
        old_manifest_sha256=_digest(old_manifest),
        new_manifest_sha256=_digest(new_manifest),
    )
    if target.exists() or target.is_symlink():
        message = "recovery directory already exists"
        raise ModelConfigUpdateError(message)
    target.mkdir(mode=0o700)
    _fsync_directory(target.parent)
    for name, payload in (
        ("old-model.yaml", old_model),
        ("old-runtime.json", old_manifest),
        ("new-model.yaml", candidate_bytes),
        ("new-runtime.json", new_manifest),
    ):
        _write_private_file(target / name, payload)
    receipt_payload = _receipt_bytes(receipt)
    _write_private_file(target / _RECEIPT_NAME, receipt_payload)
    _fsync_directory(target)
    if (
        _digest(
            _owned_bytes(
                paths.model_config,
                label="model configuration",
                owner_uid=expected_owner_uid,
            )
        )
        != receipt.old_model_sha256
        or _digest(
            _owned_bytes(
                paths.manifest,
                label="production state manifest",
                owner_uid=expected_owner_uid,
            )
        )
        != receipt.old_manifest_sha256
    ):
        message = "production pair changed before publication"
        raise ModelConfigUpdateError(message)
    marker = _Marker(
        **receipt.model_dump(),
        status="applying",
        receipt_sha256=_digest(receipt_payload),
    )
    _publish_marker(paths, marker)
    _publish_bytes(paths.model_config, candidate_bytes)
    _publish_bytes(paths.manifest, new_manifest)
    _load_production_state_pair(
        paths.root,
        build=build,
        expected_owner_uid=expected_owner_uid,
        expected_manifest_sha256=receipt.new_manifest_sha256,
        expected_model_sha256=receipt.new_model_sha256,
    )
    _publish_marker(paths, marker.model_copy(update={"status": "complete"}))
    return {
        "ok": True,
        "profile": state.profile_name,
        "state_id": str(state.state_id),
        "old_model_config_sha256": receipt.old_model_sha256,
        "new_model_config_sha256": receipt.new_model_sha256,
        "backup_directory": str(target),
    }


def recover_models(
    profile_root: Path,
    backup_directory: Path,
    *,
    build: BuildIdentity,
    deployment_root: Path,
    expected_owner_uid: int,
) -> dict[str, object]:
    """Explicitly restore a recorded exact pre-update pair under exclusive fences."""
    paths = production_state_paths(profile_root)
    _require_owned_directory(
        paths.root,
        label="production profile root",
        expected_owner_uid=expected_owner_uid,
    )
    marker = _read_marker(paths, expected_owner_uid)
    if marker is None:
        message = "no recorded model update to recover"
        raise ModelConfigUpdateError(message)
    target = _safe_destination(
        backup_directory, paths, deployment_root, expected_owner_uid
    )
    if str(target) != marker.backup_directory:
        message = "recovery directory does not match marker"
        raise ModelConfigUpdateError(message)
    old_model, old_manifest = _validate_saved_pair(
        target, marker, paths, expected_owner_uid
    )
    current = _recorded_pair(paths, marker, expected_owner_uid)
    if marker.status == "recovered":
        if current != (marker.old_model_sha256, marker.old_manifest_sha256):
            message = "recovered model pair has drifted"
            raise ModelConfigUpdateError(message)
    else:
        _publish_marker(paths, marker.model_copy(update={"status": "recovering"}))
        _publish_bytes(paths.model_config, old_model)
        _publish_bytes(paths.manifest, old_manifest)
    _load_production_state_pair(
        paths.root,
        build=build,
        expected_owner_uid=expected_owner_uid,
        expected_manifest_sha256=marker.old_manifest_sha256,
        expected_model_sha256=marker.old_model_sha256,
    )
    if marker.status != "recovered":
        _publish_marker(paths, marker.model_copy(update={"status": "recovered"}))
    return {
        "ok": True,
        "profile": marker.profile_name,
        "state_id": str(marker.state_id),
        "old_model_config_sha256": marker.old_model_sha256,
        "new_model_config_sha256": marker.new_model_sha256,
        "backup_directory": str(target),
    }
