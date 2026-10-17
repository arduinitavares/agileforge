# tests/container_runtime/test_schema_upgrade_wal.py
"""Keep live WAL state visible and sealed upgrade and restore evidence intact."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing, contextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cli import container_runtime, production_schema_upgrade
from cli.production_state import (
    ProductionStateManifest,
    database_schema_sha256,
    load_production_state,
    production_state_paths,
)
from cli.state_transfer import (
    TransferError,
    discover_registered_repositories,
    verify_backup,
)
from tests.container_runtime.test_container_runtime import (
    _committed_repository,
    _register_active_repository,
)
from tests.container_runtime.test_provider_index_manifest import _build, _prior_profile

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX runtime fences")
_MASTER_SHA256: str = "d257a8ce162334434d2adc069228ef004db1eb04978db2543a410b7be4a8f29c"
_CURRENT_SHA256: str = (
    "66df69cc4eece12fe24d9645558682134454f1c69060f7d09966af3bb755fd64"
)


def _snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


@contextmanager
def _live_wal(database: Path) -> Iterator[sqlite3.Connection]:
    """Keep committed changes in WAL until the observable operation finishes."""
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("UPDATE projects SET name='Committed WAL history'")
        connection.commit()
        assert Path(f"{database}-wal").stat().st_size > 0
        with closing(
            sqlite3.connect(f"{database.as_uri()}?mode=ro&immutable=1", uri=True)
        ) as frozen:
            assert frozen.execute("SELECT name FROM projects").fetchall() == [
                ("History",)
            ]
        yield connection


def _capture_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[Path, dict[str, bytes | None]]:
    snapshots: dict[Path, dict[str, bytes | None]] = {}
    original = container_runtime.backup_production_state

    def capture(
        state: ProductionStateManifest,
        destination: Path,
        *,
        deployment_root: Path,
        maintenance_fences_held: bool = False,
        additional_provenance: tuple[Path, ...] = (),
    ) -> Path:
        bundle = original(
            state,
            destination,
            deployment_root=deployment_root,
            maintenance_fences_held=maintenance_fences_held,
            additional_provenance=additional_provenance,
        )
        verify_backup(bundle)
        snapshots[bundle] = _snapshot(bundle)
        return bundle

    monkeypatch.setattr(container_runtime, "backup_production_state", capture)
    return snapshots


def _upgrade(state: ProductionStateManifest, deployment: Path) -> dict[str, object]:
    with container_runtime._runtime_fences(deployment, exclusive=True):
        return production_schema_upgrade.upgrade_production_state(
            state.profile_root,
            build=_build(),
            deployment_root=deployment,
            expected_owner_uid=os.getuid(),
        )


def _assert_sealed(bundle: Path, before: dict[str, bytes | None]) -> None:
    assert _snapshot(bundle) == before
    assert not (bundle / "business.sqlite3-wal").exists()
    assert not (bundle / "business.sqlite3-shm").exists()
    verify_backup(bundle)
    assert _snapshot(bundle) == before


@contextmanager
def _read_only_bundle(bundle: Path) -> Iterator[None]:
    """Deny transient sidecar creation without changing the payload inventory."""
    original_mode = stat.S_IMODE(bundle.stat().st_mode)
    bundle.chmod(0o500)
    try:
        yield
    finally:
        bundle.chmod(original_mode)


def test_wal_profile_upgrade_preserves_verified_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrade cannot create sidecars in its sealed pre-DDL rollback bundle."""
    source = _prior_profile(tmp_path, raw=True)
    snapshots = _capture_rollback(monkeypatch)
    with _live_wal(source.business_database) as writer:
        result = _upgrade(source, tmp_path)
        assert result["status"] == "upgraded"
        installed = load_production_state(source.profile_root, build=_build())
        assert installed.business_schema_sha256 == _CURRENT_SHA256
        assert writer.execute("SELECT name FROM projects").fetchall() == [
            ("Committed WAL history",)
        ]
        bundle = Path(str(result["backup"]))
        _assert_sealed(bundle, snapshots[bundle])
        with closing(
            sqlite3.connect(
                f"{(bundle / 'business.sqlite3').as_uri()}?mode=ro&immutable=1",
                uri=True,
            )
        ) as frozen:
            assert frozen.execute("SELECT name FROM projects").fetchall() == [
                ("Committed WAL history",)
            ]
        _assert_sealed(bundle, snapshots[bundle])


def test_wal_journal_recovery_repeatedly_verifies_unchanged_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target DB with a pending manifest remains recoverable from sealed evidence."""
    source = _prior_profile(tmp_path, raw=True)
    paths = production_state_paths(source.profile_root)
    manifest_before = paths.manifest.read_bytes()
    snapshots = _capture_rollback(monkeypatch)

    def interrupted(*_args: object, **_kwargs: object) -> None:
        message = "simulated interruption before manifest publication"
        raise OSError(message)

    with _live_wal(source.business_database):
        with monkeypatch.context() as scoped:
            scoped.setattr(production_schema_upgrade, "_publish_manifest", interrupted)
            with pytest.raises(OSError, match="before manifest publication"):
                _upgrade(source, tmp_path)
        journal = production_schema_upgrade._read_journal(paths, os.getuid())
        assert journal is not None
        assert journal.status == "pending"
        assert paths.manifest.read_bytes() == manifest_before
        assert database_schema_sha256(paths.business_database) == _CURRENT_SHA256
        bundle = journal.backup_directory
        with _read_only_bundle(bundle):
            for _ in range(3):
                assert (
                    production_schema_upgrade._verify_journal_evidence(
                        paths, journal, tmp_path, os.getuid()
                    )
                    == source
                )
                _assert_sealed(bundle, snapshots[bundle])
            assert _upgrade(source, tmp_path)["status"] == "upgraded"
        installed = load_production_state(source.profile_root, build=_build())
        assert installed.business_schema_sha256 == _CURRENT_SHA256
        completed = json.loads((paths.root / "schema-upgrade.json").read_bytes())
        assert completed["status"] == "complete"
        _assert_sealed(bundle, snapshots[bundle])


@pytest.mark.parametrize("current", [False, True])
def test_restore_wal_bundle_preserves_source_bytes_and_provenance(
    tmp_path: Path, current: bool
) -> None:
    """Source provenance and schema preflight must not open a sealed WAL DB live."""
    source = _prior_profile(tmp_path, raw=True)
    if current:
        production_schema_upgrade.upgrade_registered_database(source.business_database)
        source = source.model_copy(update={"business_schema_sha256": _CURRENT_SHA256})
        production_state_paths(source.profile_root).manifest.write_text(
            source.model_dump_json(indent=2), encoding="utf-8"
        )
    with _live_wal(source.business_database):
        bundle = container_runtime.backup_production_state(
            source, tmp_path / "source-bundle", deployment_root=tmp_path
        )
        before = _snapshot(bundle)
        verify_backup(bundle)
        with _read_only_bundle(bundle):
            restored = container_runtime.restore_production_state(
                bundle,
                tmp_path / "profiles" / "restored",
                build=_build(),
                deployment_root=tmp_path,
                expected_owner_uid=os.getuid(),
            )
        assert load_production_state(restored.profile_root, build=_build()) == restored
        assert restored.business_schema_sha256 == _CURRENT_SHA256
        assert restored.state_id == source.state_id
        manifest_bytes = before["manifest.json"]
        assert manifest_bytes is not None
        assert (
            restored.source_backup_sha256 == hashlib.sha256(manifest_bytes).hexdigest()
        )
        assert (
            production_state_paths(restored.profile_root).config_directory
            / "source-runtime.json"
        ).read_bytes() == before["provenance/runtime.json"]
        with closing(sqlite3.connect(restored.business_database)) as connection:
            assert connection.execute("SELECT name FROM projects").fetchall() == [
                ("Committed WAL history",)
            ]
        _assert_sealed(bundle, before)


def test_wal_active_repository_is_discovered_and_captured_in_rollback(
    tmp_path: Path,
) -> None:
    """Committed WAL bindings require a complete repository rollback payload."""
    source = _prior_profile(tmp_path, raw=True)
    repository = _committed_repository(tmp_path / "synthetic-repository")
    repository_path = Path(repository.working_tree_dir or "").resolve(strict=True)
    with _live_wal(source.business_database):
        project_id = _register_active_repository(source.business_database, repository)
        with closing(
            sqlite3.connect(
                f"{source.business_database.as_uri()}?mode=ro&immutable=1", uri=True
            )
        ) as frozen:
            assert (
                frozen.execute(
                    "SELECT active_repository_binding_id FROM projects "
                    "WHERE project_id=?",
                    (project_id,),
                ).fetchone()
                is None
            )
        assert discover_registered_repositories(source.business_database) == (
            repository_path,
        )
        result = _upgrade(source, tmp_path)
        bundle = Path(str(result["backup"]))
        manifest = verify_backup(bundle)
        assert [item["source_path"] for item in manifest.repositories] == [
            str(repository_path)
        ]
        tracked_files = list((bundle / "repositories").rglob("tracked.txt"))
        assert len(tracked_files) == 1
        assert tracked_files[0].read_bytes() == b"committed\n"
        with closing(
            sqlite3.connect(
                f"{(bundle / 'business.sqlite3').as_uri()}?mode=ro&immutable=1",
                uri=True,
            )
        ) as frozen:
            assert frozen.execute(
                "SELECT b.worktree_path FROM projects AS p "
                "JOIN repository_bindings AS b "
                "ON b.repository_binding_id=p.active_repository_binding_id "
                "WHERE p.project_id=?",
                (project_id,),
            ).fetchone() == (str(repository_path),)


@pytest.mark.parametrize("rejection", ["outside-fence", "explicitly-omitted"])
def test_wal_active_repository_rejected_before_schema_or_journal_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rejection: str
) -> None:
    """WAL-only bindings cannot bypass the existing fence or complete-capture gates."""
    deployment = tmp_path / "deployment"
    source = _prior_profile(deployment, raw=True)
    repository_parent = tmp_path if rejection == "outside-fence" else deployment
    repository = _committed_repository(repository_parent / "synthetic-repository")
    paths = production_state_paths(source.profile_root)
    before = paths.manifest.read_bytes()
    expected_error = "escapes maintenance fence roots"
    if rejection == "explicitly-omitted":
        monkeypatch.setattr(
            container_runtime,
            "backup_production_state",
            partial(container_runtime.backup_production_state, repositories=()),
        )
        expected_error = "omits an active registered repository"
    with _live_wal(source.business_database):
        _register_active_repository(source.business_database, repository)
        with pytest.raises(TransferError, match=expected_error):
            _upgrade(source, deployment)
        assert paths.manifest.read_bytes() == before
        assert database_schema_sha256(source.business_database) == _MASTER_SHA256
        assert not (paths.root / "schema-upgrade.json").exists()
        assert not list((deployment / "upgrade-backups").rglob("manifest.json"))
