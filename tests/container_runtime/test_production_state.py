"""Installed-build identity and durable production-state contracts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from cli.production_state import (
    ProductionStateError,
    ProductionStateManifest,
    database_schema_sha256,
    initialize_production_state,
    load_production_state,
    production_state_paths,
)
from utils.build_identity import BuildIdentity, BuildIdentityError, load_build_identity

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="installed production state is supported only in Linux containers",
)

_REVISION_A = "1" * 40
_REVISION_B = "2" * 40
_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64
_HASH_D = "d" * 64


def _current_uid() -> int:
    """Return the current POSIX user ID used by Linux-only fixtures."""
    getter = cast("Callable[[], int] | None", getattr(os, "getuid", None))
    if getter is None:
        message = "Linux-only fixture requires a POSIX user ID"
        raise RuntimeError(message)
    return getter()


def _build_payload(*, revision: str = _REVISION_A) -> dict[str, str]:
    return {
        "schema_version": "agileforge.build.v1",
        "revision": revision,
        "source_sha256": _HASH_A,
        "lock_sha256": _HASH_B,
        "python_version": "3.13.15",
        "uv_version": "0.12.8",
        "package_version": "0.1.0",
    }


def _write_build(path: Path, *, revision: str = _REVISION_A) -> None:
    path.write_text(json.dumps(_build_payload(revision=revision)), encoding="utf-8")
    path.chmod(0o444)


def _build(*, revision: str = _REVISION_A) -> BuildIdentity:
    return BuildIdentity.model_validate(_build_payload(revision=revision))


def test_build_identity_requires_full_lowercase_revision() -> None:
    """A shortened revision must never become production provenance."""
    payload = _build_payload(revision="abc123")

    with pytest.raises(ValidationError, match="40-character lowercase"):
        BuildIdentity.model_validate(payload)


def test_build_identity_rejects_unknown_record_version() -> None:
    """A future record shape must fail closed until explicitly supported."""
    payload = _build_payload()
    payload["schema_version"] = "agileforge.build.v2"

    with pytest.raises(ValidationError):
        BuildIdentity.model_validate(payload)


def test_build_loader_requires_root_owned_immutable_regular_file(
    tmp_path: Path,
) -> None:
    """Writable packaged metadata must not identify a production build."""
    build_path = tmp_path / "build.json"
    _write_build(build_path)
    build_path.chmod(0o644)

    with pytest.raises(BuildIdentityError, match="must not be writable"):
        load_build_identity(build_path, expected_owner_uid=_current_uid())


def test_build_loader_rejects_symlink(tmp_path: Path) -> None:
    """A runtime user must not redirect the authoritative build record."""
    target = tmp_path / "real-build.json"
    _write_build(target)
    link = tmp_path / "build.json"
    link.symlink_to(target)

    with pytest.raises(BuildIdentityError, match="regular file"):
        load_build_identity(link, expected_owner_uid=_current_uid())


def _create_business_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE projects (project_id INTEGER PRIMARY KEY)")
    path.chmod(0o600)


def _write_state(
    profile_root: Path,
    *,
    trace_present: bool = False,
    state_id: str = "d931ea27-e9b5-4cba-b047-84570d2de24f",
) -> ProductionStateManifest:
    paths = production_state_paths(profile_root)
    profile_root.mkdir(mode=0o700)
    paths.artifacts.mkdir(mode=0o700)
    paths.config_directory.mkdir(mode=0o700)
    paths.model_config.write_text("models:\n  default: test/model\n", encoding="utf-8")
    paths.model_config.chmod(0o600)
    _create_business_database(paths.business_database)
    if trace_present:
        with sqlite3.connect(paths.trace_database) as connection:
            connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        paths.trace_database.chmod(0o600)
    manifest = ProductionStateManifest(
        schema_version="agileforge.production-state.v1",
        state_id=UUID(state_id),
        profile_name=profile_root.name,
        profile_root=profile_root,
        business_database=paths.business_database,
        trace_database=paths.trace_database,
        trace_database_present=trace_present,
        artifacts=paths.artifacts,
        model_config_path=paths.model_config,
        model_config_sha256=hashlib.sha256(paths.model_config.read_bytes()).hexdigest(),
        business_schema_sha256=database_schema_sha256(paths.business_database),
        created_at=datetime(2026, 9, 16, tzinfo=UTC),
        created_by_build_revision=_REVISION_A,
        provenance="initialized",
        source_backup_sha256=None,
        repository_relocations_sha256=None,
    )
    paths.manifest.write_text(
        manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )
    paths.manifest.chmod(0o600)
    return manifest


def test_image_replacement_keeps_state_identity(tmp_path: Path) -> None:
    """Changing the immutable image revision must not replace durable identity."""
    profile_root = tmp_path / "default"
    expected = _write_state(profile_root)

    first = load_production_state(
        profile_root,
        build=_build(revision=_REVISION_A),
        expected_owner_uid=_current_uid(),
        validate_current_schema=False,
    )
    second = load_production_state(
        profile_root,
        build=_build(revision=_REVISION_B),
        expected_owner_uid=_current_uid(),
        validate_current_schema=False,
    )

    assert first.state_id == UUID(str(expected.state_id))
    assert second.state_id == first.state_id


def test_missing_manifest_never_initializes_empty_state(tmp_path: Path) -> None:
    """Starting a missing profile must stop before application initialization."""
    profile_root = tmp_path / "default"
    profile_root.mkdir(mode=0o700)

    with pytest.raises(ProductionStateError, match="manifest is missing"):
        load_production_state(
            profile_root,
            build=_build(),
            expected_owner_uid=_current_uid(),
            validate_current_schema=False,
        )

    assert tuple(profile_root.iterdir()) == ()


def test_partial_profile_is_rejected(tmp_path: Path) -> None:
    """A published manifest may not mask a missing business database."""
    profile_root = tmp_path / "default"
    _write_state(profile_root)
    production_state_paths(profile_root).business_database.unlink()

    with pytest.raises(ProductionStateError, match="business database"):
        load_production_state(
            profile_root,
            build=_build(),
            expected_owner_uid=_current_uid(),
            validate_current_schema=False,
        )


def test_unused_trace_database_remains_absent(tmp_path: Path) -> None:
    """A profile without provider traces is valid and does not fabricate a store."""
    profile_root = tmp_path / "default"
    _write_state(profile_root, trace_present=False)

    state = load_production_state(
        profile_root,
        build=_build(),
        expected_owner_uid=_current_uid(),
        validate_current_schema=False,
    )

    assert state.trace_database_present is False
    assert not state.trace_database.exists()


def test_first_runtime_trace_can_appear_after_initialization(tmp_path: Path) -> None:
    """An initially absent trace store may be created by the first provider run."""
    profile_root = tmp_path / "default"
    _write_state(profile_root, trace_present=False)
    trace = production_state_paths(profile_root).trace_database
    with sqlite3.connect(trace) as connection:
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    trace.chmod(0o600)

    state = load_production_state(
        profile_root,
        build=_build(),
        expected_owner_uid=_current_uid(),
        validate_current_schema=False,
    )

    assert state.trace_database_present is False
    assert state.trace_database.is_file()


def test_state_loader_rejects_unknown_trace_schema(tmp_path: Path) -> None:
    """An arbitrary trace database must not become a supported provider store."""
    profile_root = tmp_path / "default"
    model_config_source = tmp_path / "models.yaml"
    model_config_source.write_text("models: {}\n", encoding="utf-8")
    state = initialize_production_state(
        profile_root,
        build=_build(),
        model_config_source=model_config_source,
        expected_owner_uid=_current_uid(),
    )
    with sqlite3.connect(state.trace_database) as connection:
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    state.trace_database.chmod(0o600)

    with pytest.raises(ProductionStateError, match="production database schema"):
        load_production_state(
            profile_root,
            build=_build(),
            expected_owner_uid=_current_uid(),
        )


def test_model_configuration_drift_is_rejected(tmp_path: Path) -> None:
    """Editing the state-local model routing must invalidate the profile."""
    profile_root = tmp_path / "default"
    _write_state(profile_root)
    paths = production_state_paths(profile_root)
    paths.model_config.write_text(
        "models:\n  default: changed/model\n",
        encoding="utf-8",
    )

    with pytest.raises(ProductionStateError, match="model configuration drift"):
        load_production_state(
            profile_root,
            build=_build(),
            expected_owner_uid=_current_uid(),
            validate_current_schema=False,
        )


def test_database_schema_drift_is_rejected(tmp_path: Path) -> None:
    """A schema mutation must fail before the application opens the store."""
    profile_root = tmp_path / "default"
    _write_state(profile_root)
    with sqlite3.connect(production_state_paths(profile_root).business_database) as db:
        db.execute("ALTER TABLE projects ADD COLUMN title TEXT")

    with pytest.raises(ProductionStateError, match="business schema drift"):
        load_production_state(
            profile_root,
            build=_build(),
            expected_owner_uid=_current_uid(),
            validate_current_schema=False,
        )


def test_state_loader_rejects_path_aliases(tmp_path: Path) -> None:
    """Distinct manifest names must also refer to distinct filesystem objects."""
    profile_root = tmp_path / "default"
    _write_state(profile_root, trace_present=True)
    paths = production_state_paths(profile_root)
    paths.trace_database.unlink()
    os.link(paths.business_database, paths.trace_database)

    with pytest.raises(ProductionStateError, match="alias"):
        load_production_state(
            profile_root,
            build=_build(),
            expected_owner_uid=_current_uid(),
            validate_current_schema=False,
        )


def test_state_loader_rejects_symlinked_owned_path(tmp_path: Path) -> None:
    """A state path may not escape through a symlink after publication."""
    profile_root = tmp_path / "default"
    _write_state(profile_root)
    paths = production_state_paths(profile_root)
    target = tmp_path / "other-models.yaml"
    target.write_text("models: {}\n", encoding="utf-8")
    paths.model_config.unlink()
    paths.model_config.symlink_to(target)

    with pytest.raises(ProductionStateError, match="regular file"):
        load_production_state(
            profile_root,
            build=_build(),
            expected_owner_uid=_current_uid(),
            validate_current_schema=False,
        )


def test_initialization_publishes_manifest_after_schema_bootstrap(
    tmp_path: Path,
) -> None:
    """Readers must not discover a profile before its database is complete."""
    profile_root = tmp_path / "default"
    model_config_source = tmp_path / "models.yaml"
    model_config_source.write_text("models:\n  default: test/model\n", encoding="utf-8")
    model_config_source.chmod(0o600)
    observed_manifest_states: list[bool] = []

    def bootstrap(database: Path) -> None:
        observed_manifest_states.append(
            production_state_paths(profile_root).manifest.exists()
        )
        _create_business_database(database)

    state = initialize_production_state(
        profile_root,
        build=_build(),
        model_config_source=model_config_source,
        bootstrap_business_database=bootstrap,
        now=datetime(2026, 9, 16, tzinfo=UTC),
        state_id=UUID("d931ea27-e9b5-4cba-b047-84570d2de24f"),
        expected_owner_uid=_current_uid(),
        validate_current_schema=False,
    )

    assert observed_manifest_states == [False]
    assert state.state_id == UUID("d931ea27-e9b5-4cba-b047-84570d2de24f")
    assert production_state_paths(profile_root).manifest.is_file()
    assert not state.trace_database.exists()


def test_initialization_rejects_symlinked_profile_parent_before_writing(
    tmp_path: Path,
) -> None:
    """A profile-parent symlink must not redirect initialization outside state."""
    deployment_root = tmp_path / "deployment"
    deployment_root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (deployment_root / "profiles").symlink_to(outside, target_is_directory=True)
    model_config_source = tmp_path / "models.yaml"
    model_config_source.write_text("models: {}\n", encoding="utf-8")

    with pytest.raises(ProductionStateError, match="must be a real directory"):
        initialize_production_state(
            deployment_root / "profiles" / "default",
            build=_build(),
            model_config_source=model_config_source,
            bootstrap_business_database=_create_business_database,
            expected_owner_uid=_current_uid(),
            validate_current_schema=False,
        )

    assert tuple(outside.iterdir()) == ()


def test_initialization_refuses_unpublished_reservation(tmp_path: Path) -> None:
    """A prior interrupted initialization must require explicit recovery."""
    profile_root = tmp_path / "default"
    profile_root.mkdir(mode=0o700)
    model_config_source = tmp_path / "models.yaml"
    model_config_source.write_text("models: {}\n", encoding="utf-8")

    with pytest.raises(ProductionStateError, match="already exists"):
        initialize_production_state(
            profile_root,
            build=_build(),
            model_config_source=model_config_source,
            bootstrap_business_database=_create_business_database,
            expected_owner_uid=_current_uid(),
            validate_current_schema=False,
        )


def test_failed_initialization_leaves_unpublished_reservation(
    tmp_path: Path,
) -> None:
    """A failed bootstrap must never publish a loadable empty profile."""
    profile_root = tmp_path / "default"
    model_config_source = tmp_path / "models.yaml"
    model_config_source.write_text("models: {}\n", encoding="utf-8")

    def fail_bootstrap(_database: Path) -> None:
        message = "synthetic bootstrap failure"
        raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="synthetic bootstrap failure"):
        initialize_production_state(
            profile_root,
            build=_build(),
            model_config_source=model_config_source,
            bootstrap_business_database=fail_bootstrap,
            expected_owner_uid=_current_uid(),
            validate_current_schema=False,
        )

    assert profile_root.is_dir()
    assert not production_state_paths(profile_root).manifest.exists()
