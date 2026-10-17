# tests/container_runtime/test_production_schema_upgrade.py
"""Exact registered schemas, rollback evidence, and interrupted upgrades."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlmodel import SQLModel, create_engine

from cli import container_runtime, production_model_config
from cli.production_state import (
    ProductionStateError,
    ProductionStateManifest,
    database_schema_sha256,
    load_production_state,
    production_state_paths,
)
from cli.state_transfer import verify_backup
from models.provider_audit_indexes import PROVIDER_AUDIT_INDEX_DDL
from tests.container_runtime.test_provider_index_manifest import (
    _GUARDED_UPGRADE_COMMAND,
    _build,
    _prior_profile,
    _run,
)
from utils.runtime_fence import runtime_fence

if TYPE_CHECKING:
    from types import ModuleType

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX runtime fences")
_SOURCES = (
    "3f1d81bbdec3c5f1f699877cc82f154c0b8e4fb1b2c3dd4824e51603098d0a88",
    "d257a8ce162334434d2adc069228ef004db1eb04978db2543a410b7be4a8f29c",
)
_TARGETS = (
    "f9bd5c17713d15174727cd79e235a1dd62a7e11bac5392c3fbdeb9bf85a339c2",
    "66df69cc4eece12fe24d9645558682134454f1c69060f7d09966af3bb755fd64",
)
_STRUCTURE = "c99398b56ccd7871e6840a8cb218bf595970ee1f8aa492b12ec4053830923425"
_ERROR_EXIT: int = 2
_COMPLETION_PUBLICATION: int = 2


def _upgrade_module() -> ModuleType:
    assert importlib.util.find_spec("cli.production_schema_upgrade") is not None, (
        "explicit production schema upgrade implementation is missing"
    )
    return importlib.import_module("cli.production_schema_upgrade")


def _journal(state: ProductionStateManifest) -> dict[str, object]:
    return json.loads((state.profile_root / "schema-upgrade.json").read_bytes())


def _command(tmp_path: Path) -> int:
    return _run(tmp_path, ["upgrade", "--profile", "default", "--json"])


@pytest.mark.parametrize("raw", [False, True])
def test_independent_master_encodings_upgrade_to_exact_registered_targets(
    tmp_path: Path, raw: bool
) -> None:
    """Preserve each independent master encoding and retained rows across upgrade."""
    state = _prior_profile(tmp_path, raw=raw)
    assert state.business_schema_sha256 == _SOURCES[int(raw)]
    assert _command(tmp_path) == 0
    installed = load_production_state(state.profile_root, build=_build())
    assert installed.business_schema_sha256 == _TARGETS[int(raw)]
    with sqlite3.connect(installed.business_database) as connection:
        assert connection.execute("SELECT name FROM projects").fetchall() == [
            ("History",)
        ]
    journal = _journal(state)
    assert journal["status"] == "complete"
    assert journal["source_schema_sha256"] == _SOURCES[int(raw)]
    assert journal["target_schema_sha256"] == _TARGETS[int(raw)]
    assert journal["state_id"] == str(state.state_id)
    backup = Path(str(journal["backup_directory"]))
    assert not backup.is_relative_to(state.profile_root)
    verify_backup(backup)
    assert database_schema_sha256(backup / "business.sqlite3") == _SOURCES[int(raw)]


def test_append_only_registry_independently_pins_issue230_release(
    tmp_path: Path,
) -> None:
    """Require a new release when current schema changes and freeze historical IDs."""
    module = _upgrade_module()
    releases = {item.identity: item for item in module.SCHEMA_RELEASES}
    assert {
        item.complete_sha256 for item in releases["master-b3a4fb4"].variants
    } == set(_SOURCES)
    assert {
        item.complete_sha256 for item in releases["issue230-provider-indexes"].variants
    } == set(_TARGETS)
    assert releases["master-b3a4fb4"].structural_sha256 == _STRUCTURE
    assert releases["issue230-provider-indexes"].structural_sha256 == _STRUCTURE
    database = tmp_path / "fresh.sqlite3"
    engine = create_engine(f"sqlite:///{database}")
    try:
        SQLModel.metadata.create_all(engine)
    finally:
        engine.dispose()
    registered = module.registered_schema_state(database)
    assert registered.release_id == module.CURRENT_SCHEMA_ID
    assert database_schema_sha256(database) in {
        item.complete_sha256 for item in releases[module.CURRENT_SCHEMA_ID].variants
    }


def test_second_upgrade_does_not_backup_migrate_or_republish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leave current state and retained rollback evidence byte-for-byte intact."""
    state = _prior_profile(tmp_path)
    assert _command(tmp_path) == 0
    module = _upgrade_module()
    manifest = (state.profile_root / "runtime.json").read_bytes()
    journal = (state.profile_root / "schema-upgrade.json").read_bytes()
    evidence = tuple(sorted((tmp_path / "upgrade-backups").rglob("*")))

    def unexpected(*_args: object, **_kwargs: object) -> None:
        message = "already-current upgrade attempted a write"
        raise AssertionError(message)

    monkeypatch.setattr(container_runtime, "backup_production_state", unexpected)
    monkeypatch.setattr(module, "upgrade_registered_database", unexpected)
    monkeypatch.setattr(module, "_publish_manifest", unexpected)
    assert _command(tmp_path) == 0
    assert (state.profile_root / "runtime.json").read_bytes() == manifest
    assert (state.profile_root / "schema-upgrade.json").read_bytes() == journal
    assert tuple(sorted((tmp_path / "upgrade-backups").rglob("*"))) == evidence


@pytest.mark.parametrize("phase", ["journal", "migration", "manifest", "complete"])
def test_explicit_journal_recovery_across_publication_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    phase: str,
) -> None:
    """Resume durable source or target states after publication interruptions."""
    state = _prior_profile(tmp_path)
    module = _upgrade_module()
    name = {
        "journal": "_publish_journal",
        "migration": "upgrade_registered_database",
        "manifest": "_publish_manifest",
        "complete": "_publish_journal",
    }[phase]
    original = getattr(module, name)
    calls = 0

    def interrupted(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if phase != "complete" or calls == _COMPLETION_PUBLICATION:
            message = "simulated publication interruption"
            raise OSError(message)
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(module, name, interrupted)
        assert _command(tmp_path) == _ERROR_EXIT
    capsys.readouterr()
    if _journal(state)["status"] != "complete":
        assert _run(tmp_path, ["info", "--profile", "default", "--json"]) == _ERROR_EXIT
        payload = json.loads(capsys.readouterr().out)
        assert _GUARDED_UPGRADE_COMMAND in payload["error"]
        with pytest.raises(ProductionStateError, match="production upgrade"):
            load_production_state(state.profile_root, build=_build())
    assert _command(tmp_path) == 0
    assert _journal(state)["status"] == "complete"
    assert (
        load_production_state(state.profile_root, build=_build()).business_schema_sha256
        == _TARGETS[0]
    )
    assert len(list((tmp_path / "upgrade-backups").iterdir())) == 1


@pytest.mark.parametrize("drift", ["index", "column", "version", "partial"])
def test_unregistered_schema_is_never_normalized(tmp_path: Path, drift: str) -> None:
    """Reject unknown full schemas even when their manifest records the drift."""
    state = _prior_profile(tmp_path)
    with sqlite3.connect(state.business_database) as connection:
        if drift == "index":
            connection.execute("CREATE INDEX custom_projects ON projects(name)")
        elif drift == "column":
            connection.execute("ALTER TABLE projects ADD COLUMN unregistered TEXT")
        elif drift == "version":
            connection.execute("PRAGMA user_version=1")
        else:
            connection.execute(next(iter(PROVIDER_AUDIT_INDEX_DDL.values())))
    changed = database_schema_sha256(state.business_database)
    paths = production_state_paths(state.profile_root)
    paths.manifest.write_text(
        state.model_copy(update={"business_schema_sha256": changed}).model_dump_json(),
        encoding="utf-8",
    )
    original_manifest = paths.manifest.read_bytes()
    assert _command(tmp_path) == _ERROR_EXIT
    assert database_schema_sha256(state.business_database) == changed
    assert paths.manifest.read_bytes() == original_manifest
    assert not (tmp_path / "upgrade-backups").exists()


def test_backup_verification_failure_precedes_any_schema_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the rollback bundle before publishing a journal or DDL."""
    state = _prior_profile(tmp_path)
    module = _upgrade_module()

    def bad_backup(*_args: object, **_kwargs: object) -> None:
        message = "rollback backup verification failed"
        raise ProductionStateError(message)

    monkeypatch.setattr(module, "verify_backup", bad_backup)
    assert _command(tmp_path) == _ERROR_EXIT
    assert database_schema_sha256(state.business_database) == _SOURCES[0]
    assert not (state.profile_root / "schema-upgrade.json").exists()


@pytest.mark.parametrize("damage", ["backup", "manifest", "target", "identity", "path"])
def test_recovery_revalidates_exact_recorded_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    """Reject changed rollback bytes, identities, schema targets, and owned paths."""
    state = _prior_profile(tmp_path)
    module = _upgrade_module()

    def stop_before_migration(*_args: object, **_kwargs: object) -> None:
        message = "stop before DDL"
        raise OSError(message)

    with monkeypatch.context() as scoped:
        scoped.setattr(module, "upgrade_registered_database", stop_before_migration)
        assert _command(tmp_path) == _ERROR_EXIT
    journal_path = state.profile_root / "schema-upgrade.json"
    journal = _journal(state)
    if damage == "backup":
        (Path(str(journal["backup_directory"])) / "model-config").write_text(
            "changed", encoding="utf-8"
        )
    elif damage == "manifest":
        paths = production_state_paths(state.profile_root)
        paths.manifest.write_text(
            state.model_copy(
                update={"created_by_build_revision": "2" * 40}
            ).model_dump_json(),
            encoding="utf-8",
        )
    elif damage == "target":
        journal["target_schema_sha256"] = "f" * 64
    elif damage == "identity":
        journal["state_id"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    else:
        journal["backup_directory"] = str(tmp_path.parent / "foreign")
    if damage not in {"backup", "manifest"}:
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
    assert _command(tmp_path) == _ERROR_EXIT
    assert database_schema_sha256(state.business_database) == _SOURCES[0]


def _terminal_model_marker(state: ProductionStateManifest, *, recovered: bool) -> bytes:
    """Create a fully verifiable model-operation receipt without current-schema DDL."""
    paths = production_state_paths(state.profile_root)
    recovery = state.profile_root.parent.parent / "model-operation"
    recovery.mkdir(mode=0o700)
    old_model = paths.model_config.read_bytes()
    new_model = old_model.replace(b"test/model", b"test/updated-model")
    old_manifest = paths.manifest.read_bytes()
    new_state = state.model_copy(
        update={"model_config_sha256": hashlib.sha256(new_model).hexdigest()}
    )
    new_manifest = (new_state.model_dump_json(indent=2) + "\n").encode()
    for name, payload in {
        "old-model.yaml": old_model,
        "new-model.yaml": new_model,
        "old-runtime.json": old_manifest,
        "new-runtime.json": new_manifest,
    }.items():
        (recovery / name).write_bytes(payload)
        (recovery / name).chmod(0o600)
    receipt = production_model_config._Receipt(
        version="agileforge.model-config-update.v1",
        operation_id=state.state_id,
        profile_root=str(paths.root),
        profile_name=state.profile_name,
        state_id=state.state_id,
        backup_directory=str(recovery),
        old_model_sha256=hashlib.sha256(old_model).hexdigest(),
        new_model_sha256=hashlib.sha256(new_model).hexdigest(),
        old_manifest_sha256=hashlib.sha256(old_manifest).hexdigest(),
        new_manifest_sha256=hashlib.sha256(new_manifest).hexdigest(),
    )
    receipt_bytes = production_model_config._receipt_bytes(receipt)
    (recovery / "receipt.json").write_bytes(receipt_bytes)
    (recovery / "receipt.json").chmod(0o600)
    marker = production_model_config._Marker(
        **receipt.model_dump(),
        status="recovered" if recovered else "complete",
        receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
    )
    if not recovered:
        paths.model_config.write_bytes(new_model)
        paths.manifest.write_bytes(new_manifest)
    production_model_config._publish_marker(paths, marker)
    return (paths.root / "model-config-update.json").read_bytes()


@pytest.mark.parametrize("recovered", [False, True])
@pytest.mark.parametrize("crash", ["before", "after", "none"])
def test_terminal_model_marker_is_archived_and_retired_recoverably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovered: bool, crash: str
) -> None:
    """Archive exact terminal markers and resume crashes on either retirement side."""
    state = _prior_profile(tmp_path)
    marker = _terminal_model_marker(state, recovered=recovered)
    recovery = tmp_path / "model-operation"
    saved = {item.name: item.read_bytes() for item in recovery.iterdir()}
    module = _upgrade_module()
    original = module._retire_model_marker

    def interrupted(*args: object, **kwargs: object) -> None:
        if crash == "after":
            original(*args, **kwargs)
        message = "interrupted model-marker retirement"
        raise OSError(message)

    if crash != "none":
        with monkeypatch.context() as scoped:
            scoped.setattr(module, "_retire_model_marker", interrupted)
            assert _command(tmp_path) == _ERROR_EXIT
    assert _command(tmp_path) == 0
    assert not (state.profile_root / "model-config-update.json").exists()
    journal = _journal(state)
    backup = Path(str(journal["backup_directory"]))
    verify_backup(backup)
    assert (backup / "provenance" / "model-config-update.json").read_bytes() == marker
    assert journal["model_marker_sha256"] == hashlib.sha256(marker).hexdigest()
    assert {item.name: item.read_bytes() for item in recovery.iterdir()} == saved
    load_production_state(state.profile_root, build=_build())


@pytest.mark.parametrize("status", ["applying", "recovering"])
def test_unfinished_model_operation_requires_recover_models_first(
    tmp_path: Path, status: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Preserve pending model-recovery precedence without schema or backup writes."""
    state = _prior_profile(tmp_path)
    _terminal_model_marker(state, recovered=True)
    marker_path = state.profile_root / "model-config-update.json"
    marker = json.loads(marker_path.read_bytes())
    marker["status"] = status
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    assert _command(tmp_path) == _ERROR_EXIT
    assert "recover-models" in capsys.readouterr().out
    assert not (tmp_path / "upgrade-backups").exists()


def test_terminal_marker_retirement_uses_local_pair_without_external_receipts(
    tmp_path: Path,
) -> None:
    """Retire a valid local terminal pair without requiring external saved receipts."""
    state = _prior_profile(tmp_path)
    marker = _terminal_model_marker(state, recovered=False)
    receipt = tmp_path / "model-operation" / "receipt.json"
    receipt.write_bytes(b"external recovery evidence changed after completed operation")
    assert _command(tmp_path) == 0
    backup = Path(str(_journal(state)["backup_directory"]))
    assert (backup / "provenance" / "model-config-update.json").read_bytes() == marker
    assert (
        receipt.read_bytes()
        == b"external recovery evidence changed after completed operation"
    )


def test_upgrade_is_exclusive_and_current_runtime_remains_shared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Preserve runtime concurrency while excluding maintenance from live readers."""
    state = _prior_profile(tmp_path)
    with runtime_fence(tmp_path):
        assert _command(tmp_path) == _ERROR_EXIT
    assert "busy for maintenance" in capsys.readouterr().out
    assert database_schema_sha256(state.business_database) == _SOURCES[0]
    assert not (tmp_path / "upgrade-backups").exists()
    assert _command(tmp_path) == 0
    with runtime_fence(tmp_path):
        assert _run(tmp_path, ["info", "--profile", "default", "--json"]) == 0


def test_current_profile_without_upgrade_history_requires_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep fresh current profiles free of upgrade journals and rollback backups."""
    state = _prior_profile(tmp_path)
    module = _upgrade_module()
    module.upgrade_registered_database(state.business_database)
    manifest = state.model_copy(update={"business_schema_sha256": _TARGETS[0]})
    paths = production_state_paths(state.profile_root)
    paths.manifest.write_text(manifest.model_dump_json(), encoding="utf-8")
    original = paths.manifest.read_bytes()

    def unexpected(*_args: object, **_kwargs: object) -> None:
        message = "current profile attempted maintenance"
        raise AssertionError(message)

    monkeypatch.setattr(container_runtime, "backup_production_state", unexpected)
    monkeypatch.setattr(module, "upgrade_registered_database", unexpected)
    monkeypatch.setattr(module, "_publish_manifest", unexpected)
    assert _command(tmp_path) == 0
    assert paths.manifest.read_bytes() == original
    assert not (paths.root / "schema-upgrade.json").exists()
    assert not (tmp_path / "upgrade-backups").exists()


def test_retired_model_marker_cannot_recover_an_obsolete_schema_manifest(
    tmp_path: Path,
) -> None:
    """Require full rollback after crossing the schema boundary."""
    state = _prior_profile(tmp_path)
    _terminal_model_marker(state, recovered=False)
    assert _command(tmp_path) == 0
    paths = production_state_paths(state.profile_root)
    manifest = paths.manifest.read_bytes()
    config = paths.model_config.read_bytes()
    assert (
        _run(
            tmp_path,
            [
                "recover-models",
                "--profile",
                "default",
                "--backup-directory",
                str(tmp_path / "model-operation"),
                "--json",
            ],
        )
        == _ERROR_EXIT
    )
    assert paths.manifest.read_bytes() == manifest
    assert paths.model_config.read_bytes() == config


def test_recovery_rejects_an_unregistered_partial_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a partial DDL state even with a verified pending journal."""
    state = _prior_profile(tmp_path)
    module = _upgrade_module()

    def interrupted(*_args: object, **_kwargs: object) -> None:
        message = "stop before migration"
        raise OSError(message)

    with monkeypatch.context() as scoped:
        scoped.setattr(module, "upgrade_registered_database", interrupted)
        assert _command(tmp_path) == _ERROR_EXIT
    with sqlite3.connect(state.business_database) as connection:
        connection.execute(next(iter(PROVIDER_AUDIT_INDEX_DDL.values())))
    partial = database_schema_sha256(state.business_database)
    assert _command(tmp_path) == _ERROR_EXIT
    assert database_schema_sha256(state.business_database) == partial
    assert _journal(state)["status"] == "pending"
