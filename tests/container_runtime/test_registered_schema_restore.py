# tests/container_runtime/test_registered_schema_restore.py
"""Restore exact historical schemas without changing their rollback bundles."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from cli import container_runtime, production_schema_upgrade
from cli.container_runtime import backup_production_state, restore_production_state
from cli.production_state import (
    ProductionStateError,
    ProductionStateManifest,
    database_schema_sha256,
    load_production_state,
    production_state_paths,
)
from cli.state_transfer import TransferError, verify_backup
from tests.container_runtime.test_container_runtime import _create_adk_trace_database
from tests.container_runtime.test_provider_index_manifest import _build, _prior_profile

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX profile ownership")
_SOURCES: tuple[str, ...] = (
    "3f1d81bbdec3c5f1f699877cc82f154c0b8e4fb1b2c3dd4824e51603098d0a88",
    "d257a8ce162334434d2adc069228ef004db1eb04978db2543a410b7be4a8f29c",
)
_TARGETS: tuple[str, ...] = (
    "f9bd5c17713d15174727cd79e235a1dd62a7e11bac5392c3fbdeb9bf85a339c2",
    "66df69cc4eece12fe24d9645558682134454f1c69060f7d09966af3bb755fd64",
)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _record_source(state: ProductionStateManifest) -> None:
    production_state_paths(state.profile_root).manifest.write_text(
        state.model_dump_json(indent=2), encoding="utf-8"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("trace_present", [False, True])
async def test_master_bundle_restores_to_exact_current_schema_without_bundle_changes(
    tmp_path: Path, raw: bool, trace_present: bool
) -> None:
    """Current-only preflight must not reject either independently frozen master."""
    source = _prior_profile(tmp_path, raw=raw)
    if trace_present:
        await _create_adk_trace_database(source.trace_database)
        source.trace_database.chmod(0o600)
        source = source.model_copy(update={"trace_database_present": True})
        _record_source(source)
    (source.artifacts / "history.txt").write_bytes(b"retained artifact\n")
    with sqlite3.connect(source.business_database) as connection:
        connection.execute(
            "INSERT INTO workflow_events "
            "(event_type, project_id, event_metadata) "
            "VALUES ('PROJECT_CREATED', 1, '{}')"
        )
        history = connection.execute("SELECT * FROM workflow_events").fetchall()
        projects = connection.execute("SELECT * FROM projects").fetchall()
    bundle = backup_production_state(
        source, tmp_path / "master-bundle", deployment_root=tmp_path
    )
    before = _snapshot(bundle)
    assert database_schema_sha256(bundle / "business.sqlite3") == _SOURCES[int(raw)]

    restored = restore_production_state(
        bundle,
        tmp_path / "profiles" / "restored",
        build=_build(),
        deployment_root=tmp_path,
        expected_owner_uid=os.getuid(),
    )

    assert restored.business_schema_sha256 == _TARGETS[int(raw)]
    assert load_production_state(restored.profile_root, build=_build()) == restored
    assert restored.state_id == source.state_id
    assert restored.created_at == source.created_at
    assert restored.created_by_build_revision == source.created_by_build_revision
    assert restored.trace_database_present is trace_present
    assert restored.provenance == "restored"
    assert (
        restored.source_backup_sha256
        == hashlib.sha256(before["manifest.json"]).hexdigest()
    )
    paths = production_state_paths(restored.profile_root)
    assert (paths.config_directory / "source-runtime.json").read_bytes() == before[
        "provenance/runtime.json"
    ]
    assert paths.model_config.read_bytes() == before["model-config"]
    assert restored.model_config_sha256 == source.model_config_sha256
    assert (paths.artifacts / "history.txt").read_bytes() == b"retained artifact\n"
    with sqlite3.connect(paths.business_database) as connection:
        assert connection.execute("SELECT * FROM projects").fetchall() == projects
        assert connection.execute("SELECT * FROM workflow_events").fetchall() == history
    after_bundle = backup_production_state(
        restored, tmp_path / "restored-bundle", deployment_root=tmp_path
    )
    verify_backup(after_bundle)
    assert (
        database_schema_sha256(after_bundle / "business.sqlite3") == _TARGETS[int(raw)]
    )
    assert _snapshot(bundle) == before
    assert not (restored.profile_root / "schema-upgrade.json").exists()
    assert not (tmp_path / "upgrade-backups").exists()


def test_current_bundle_restore_needs_no_database_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered current bundle must bypass the DDL installation path."""
    source = _prior_profile(tmp_path)
    production_schema_upgrade.upgrade_registered_database(source.business_database)
    source = source.model_copy(update={"business_schema_sha256": _TARGETS[0]})
    _record_source(source)
    bundle = backup_production_state(
        source, tmp_path / "current-bundle", deployment_root=tmp_path
    )
    before = _snapshot(bundle)
    from models import db  # noqa: PLC0415

    def reject_ddl(*_arguments: object, **_options: object) -> None:
        message = "current restore attempted schema installation"
        raise AssertionError(message)

    monkeypatch.setattr(db, "ensure_business_db_ready", reject_ddl)
    restored = restore_production_state(
        bundle,
        tmp_path / "profiles" / "restored",
        build=_build(),
        deployment_root=tmp_path,
    )
    assert load_production_state(restored.profile_root, build=_build()) == restored
    assert restored.business_schema_sha256 == _TARGETS[0]
    assert _snapshot(bundle) == before


@pytest.mark.parametrize(
    "change", ["extra-index", "column", "partial-index", "provenance"]
)
def test_unregistered_or_drifted_bundle_fails_before_destination_creation(
    tmp_path: Path, change: str
) -> None:
    """Transport validity must not authorize unknown DDL or provenance drift."""
    source = _prior_profile(tmp_path)
    with sqlite3.connect(source.business_database) as connection:
        if change == "column":
            connection.execute("ALTER TABLE projects ADD COLUMN extra TEXT")
        elif change == "partial-index":
            connection.execute(
                "CREATE INDEX ix_workflow_events_provider_invalid "
                "ON workflow_events(project_id)"
            )
        else:
            connection.execute("CREATE INDEX unregistered ON projects(name)")
    if change != "provenance":
        source = source.model_copy(
            update={
                "business_schema_sha256": database_schema_sha256(
                    source.business_database
                )
            }
        )
        _record_source(source)
    bundle = backup_production_state(
        source, tmp_path / "invalid-bundle", deployment_root=tmp_path
    )
    before = _snapshot(bundle)
    destination = tmp_path / "unpublished-parent" / "rejected"
    error = (
        container_runtime.ContainerRuntimeError
        if change == "provenance"
        else ProductionStateError
    )
    match = (
        "hash does not match provenance" if change == "provenance" else "unregistered"
    )
    with pytest.raises(error, match=match):
        restore_production_state(
            bundle, destination, build=_build(), deployment_root=tmp_path
        )
    assert not destination.parent.exists()
    assert _snapshot(bundle) == before


def test_bundle_user_version_drift_is_rejected_before_destination_creation(
    tmp_path: Path,
) -> None:
    """Unchanged transport verification rejects a tampered schema-version header."""
    source = _prior_profile(tmp_path)
    bundle = backup_production_state(
        source, tmp_path / "tampered-bundle", deployment_root=tmp_path
    )
    with sqlite3.connect(bundle / "business.sqlite3") as connection:
        connection.execute("PRAGMA user_version = 7")
    before = _snapshot(bundle)
    destination = tmp_path / "unpublished-parent" / "rejected"
    with pytest.raises(TransferError):
        restore_production_state(
            bundle, destination, build=_build(), deployment_root=tmp_path
        )
    assert not destination.parent.exists()
    assert _snapshot(bundle) == before


@pytest.mark.parametrize("failure", ["migration", "wrong-target"])
def test_installed_migration_failure_never_publishes_active_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Failing after copy must retain rollback bytes and leave runtime unpublished."""
    source = _prior_profile(tmp_path)
    bundle = backup_production_state(
        source, tmp_path / "master-bundle", deployment_root=tmp_path
    )
    before = _snapshot(bundle)

    def failed_upgrade(
        database: Path,
    ) -> production_schema_upgrade.RegisteredSchemaState:
        if failure == "migration":
            message = "injected installed migration failure"
            raise ProductionStateError(message)
        target = production_schema_upgrade.upgrade_registered_database(database)
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE INDEX unregistered ON projects(name)")
        return target

    monkeypatch.setattr(
        container_runtime, "upgrade_registered_database", failed_upgrade, raising=False
    )
    destination = tmp_path / "profiles" / "unfinished"
    with pytest.raises(ProductionStateError, match=r"injected|unregistered"):
        restore_production_state(
            bundle, destination, build=_build(), deployment_root=tmp_path
        )
    assert destination.is_dir()
    assert not production_state_paths(destination).manifest.exists()
    assert _snapshot(bundle) == before
    verify_backup(bundle)


def _marker(source: ProductionStateManifest, status: str) -> dict[str, object]:
    manifest = production_state_paths(source.profile_root).manifest.read_bytes()
    return {
        "version": "agileforge.model-config-update.v1",
        "operation_id": str(source.state_id),
        "profile_root": str(source.profile_root),
        "profile_name": source.profile_name,
        "state_id": str(source.state_id),
        "backup_directory": str(
            source.profile_root.parent.parent / "never-read-receipt"
        ),
        "old_model_sha256": source.model_config_sha256,
        "new_model_sha256": source.model_config_sha256,
        "old_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "new_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "receipt_sha256": "a" * 64,
        "status": status,
    }


def _marker_bundle(
    source: ProductionStateManifest, destination: Path, marker: dict[str, object]
) -> Path:
    marker_path = source.profile_root / "model-config-update.json"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    marker_path.chmod(0o600)
    return backup_production_state(
        source,
        destination,
        deployment_root=source.profile_root.parent.parent,
        additional_provenance=(marker_path,),
    )


@pytest.mark.parametrize("status", ["complete", "recovered"])
def test_terminal_marker_is_archived_without_following_external_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """Terminal source evidence stays archival when the manifest is rebased."""
    source = _prior_profile(tmp_path)
    marker = _marker(source, status)
    bundle = _marker_bundle(source, tmp_path / "marked-bundle", marker)
    before = _snapshot(bundle)
    original_read = Path.read_bytes
    external = Path(str(marker["backup_directory"]))
    external.mkdir(mode=0o700)
    receipt = external / "receipt.json"
    receipt.write_bytes(b"saved receipt must stay untouched\n")
    receipt_before = _snapshot(external)

    def scoped_read(path: Path) -> bytes:
        if path.is_relative_to(external):
            message = "restore followed archived marker recovery path"
            raise AssertionError(message)
        return original_read(path)

    with monkeypatch.context() as guard:
        guard.setattr(Path, "read_bytes", scoped_read)
        restored = restore_production_state(
            bundle,
            tmp_path / "profiles" / "restored",
            build=_build(),
            deployment_root=tmp_path,
        )
    assert load_production_state(restored.profile_root, build=_build()) == restored
    paths = production_state_paths(restored.profile_root)
    assert not (paths.root / "model-config-update.json").exists()
    archived_marker = paths.config_directory / "source-model-config-update.json"
    assert (
        archived_marker.read_bytes() == (before["provenance/model-config-update.json"])
    )
    assert _snapshot(bundle) == before
    assert _snapshot(external) == receipt_before


@pytest.mark.parametrize(
    "change",
    [
        "applying",
        "recovering",
        "malformed",
        "profile_root",
        "profile_name",
        "state_id",
        "old_model_sha256",
        "new_manifest_sha256",
    ],
)
def test_invalid_archived_marker_fails_before_destination_creation(
    tmp_path: Path, change: str
) -> None:
    """Archived markers require source identity and terminal-pair verification."""
    source = _prior_profile(tmp_path)
    production_schema_upgrade.upgrade_registered_database(source.business_database)
    source = source.model_copy(update={"business_schema_sha256": _TARGETS[0]})
    _record_source(source)
    status = "recovered" if change == "old_model_sha256" else "complete"
    marker = _marker(source, status)
    if change in {"applying", "recovering"}:
        marker["status"] = change
    elif change == "malformed":
        marker.pop("receipt_sha256")
    elif change == "state_id":
        marker[change] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    elif change.endswith("sha256"):
        marker[change] = "b" * 64
    else:
        marker[change] = "/other/profile" if change == "profile_root" else "other"
    bundle = _marker_bundle(source, tmp_path / "marked-bundle", marker)
    before = _snapshot(bundle)
    destination = tmp_path / "unpublished-parent" / "rejected"
    with pytest.raises(ProductionStateError, match=r"marker|model update"):
        restore_production_state(
            bundle, destination, build=_build(), deployment_root=tmp_path
        )
    assert not destination.parent.exists()
    assert _snapshot(bundle) == before
