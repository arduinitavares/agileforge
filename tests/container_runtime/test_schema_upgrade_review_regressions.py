# tests/container_runtime/test_schema_upgrade_review_regressions.py
"""Prior model recovery and cold schema imports preserve maintenance contracts."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest

from cli.production_state import (
    database_schema_sha256,
    load_production_state,
)
from models.provider_audit_indexes import PROVIDER_AUDIT_INDEX_DDL
from tests.container_runtime.test_production_schema_upgrade import (
    _SOURCES,
    _TARGETS,
    _command,
    _journal,
    _terminal_model_marker,
    _upgrade_module,
)
from tests.container_runtime.test_provider_index_manifest import (
    _build,
    _prior_profile,
    _run,
)

_ERROR_EXIT: int = 2


@pytest.mark.parametrize("status", ["applying", "recovering"])
def test_prior_model_recovery_finishes_before_schema_upgrade(
    tmp_path: Path, status: str
) -> None:
    """Complete the exact old pair before retiring its marker at schema upgrade."""
    state = _prior_profile(tmp_path)
    _terminal_model_marker(state, recovered=False)
    marker_path = state.profile_root / "model-config-update.json"
    marker = json.loads(marker_path.read_bytes())
    marker["status"] = status
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
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
        == 0
    )
    recovered_bytes = marker_path.read_bytes()
    assert json.loads(recovered_bytes)["status"] == "recovered"
    assert database_schema_sha256(state.business_database) == _SOURCES[0]
    assert _command(tmp_path) == 0
    backup = Path(str(_journal(state)["backup_directory"]))
    assert (
        backup / "provenance" / "model-config-update.json"
    ).read_bytes() == recovered_bytes
    assert not marker_path.exists()
    assert (
        load_production_state(state.profile_root, build=_build()).business_schema_sha256
        == _TARGETS[0]
    )


@pytest.mark.parametrize("drift", ["recorded-schema", "unrecorded-schema", "trace"])
def test_model_recovery_rejects_unregistered_or_drifted_databases(
    tmp_path: Path, drift: str
) -> None:
    """Retain exact full-schema equality and registered/trace gates during recovery."""
    state = _prior_profile(tmp_path)
    if drift == "trace":
        with sqlite3.connect(state.trace_database) as connection:
            connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        state.trace_database.chmod(0o600)
        state = state.model_copy(update={"trace_database_present": True})
        (state.profile_root / "runtime.json").write_text(
            state.model_dump_json(), encoding="utf-8"
        )
    elif drift == "recorded-schema":
        with sqlite3.connect(state.business_database) as connection:
            connection.execute("CREATE INDEX unregistered_projects ON projects(name)")
        state = state.model_copy(
            update={
                "business_schema_sha256": database_schema_sha256(
                    state.business_database
                )
            }
        )
        (state.profile_root / "runtime.json").write_text(
            state.model_dump_json(), encoding="utf-8"
        )
    _terminal_model_marker(state, recovered=False)
    marker_path = state.profile_root / "model-config-update.json"
    marker = json.loads(marker_path.read_bytes())
    marker["status"] = "applying"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    if drift == "unrecorded-schema":
        with sqlite3.connect(state.business_database) as connection:
            connection.execute("CREATE INDEX unrecorded_projects ON projects(name)")
    before = database_schema_sha256(state.business_database)
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
    assert json.loads(marker_path.read_bytes())["status"] != "recovered"
    assert database_schema_sha256(state.business_database) == before


def test_pending_schema_journal_blocks_model_recovery_before_pair_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Protect the recorded source pair while terminal-marker retirement is pending."""
    state = _prior_profile(tmp_path)
    _terminal_model_marker(state, recovered=False)
    module = _upgrade_module()

    def before_retirement(*_args: object, **_kwargs: object) -> None:
        message = "stop before marker retirement"
        raise OSError(message)

    with monkeypatch.context() as scoped:
        scoped.setattr(module, "_retire_model_marker", before_retirement)
        assert _command(tmp_path) == _ERROR_EXIT
    capsys.readouterr()
    files = [
        state.profile_root / "runtime.json",
        state.model_config_path,
        state.profile_root / "model-config-update.json",
    ]
    before = [file.read_bytes() for file in files]
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
    assert "production upgrade --profile default" in capsys.readouterr().out
    assert [file.read_bytes() for file in files] == before
    assert _command(tmp_path) == 0


_COLD_IMPORT_PROBE: str = """
import builtins
import os
import sys
from pathlib import Path
import pytest_socket
pytest_socket.disable_socket()
from cli.production_schema_upgrade import registered_schema_state
from utils.runtime_config import get_business_db_target
assert "models.db" not in sys.modules
database = Path(sys.argv[1])
fail_import = sys.argv[2] == "failure"
environment_before = dict(os.environ)
cached_before = get_business_db_target() if "AGILEFORGE_DB_URL" in os.environ else None
real_import = builtins.__import__
observed_scope = []
class InjectedImportError(RuntimeError):
    pass
def probe_import(name, *args, **kwargs):
    if name == "models.db":
        observed_scope.append((os.environ.get("AGILEFORGE_DB_URL"),
                               os.environ.get("AGILEFORGE_LAUNCHER_CHILD")))
        if fail_import:
            raise InjectedImportError("injected schema import failure")
    return real_import(name, *args, **kwargs)
builtins.__import__ = probe_import
try:
    if fail_import:
        try:
            registered_schema_state(database)
        except InjectedImportError:
            pass
        else:
            raise AssertionError("schema import failure was hidden")
    else:
        assert registered_schema_state(database).release_id == "master-b3a4fb4"
finally:
    builtins.__import__ = real_import
assert observed_scope and observed_scope[0] == (str(database), "1"), observed_scope
assert dict(os.environ) == environment_before
if cached_before is not None:
    assert get_business_db_target() == cached_before
else:
    from utils.runtime_config import RuntimeConfigError
    try:
        get_business_db_target()
    except RuntimeConfigError:
        pass
    else:
        raise AssertionError("schema database target leaked through the cache")
print("COLD_SCHEMA_IMPORT_OK")
"""


@pytest.mark.parametrize("environment", ["missing", "configured"])
@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_registry_import_is_scoped_in_a_clean_library_interpreter(
    tmp_path: Path, environment: str, outcome: str
) -> None:
    """Scope cold imports to the supplied DB and restore ambient env/cache semantics."""
    state = _prior_profile(tmp_path)
    child_environment = os.environ.copy()
    child_environment.pop("AGILEFORGE_DB_URL", None)
    child_environment.pop("AGILEFORGE_LAUNCHER_CHILD", None)
    child_environment["AGILEFORGE_CONFIG_ROOT"] = str(tmp_path)
    unused_database = tmp_path / "caller-unused.sqlite3"
    if environment == "configured":
        child_environment["AGILEFORGE_DB_URL"] = f"sqlite:///{unused_database}"
        child_environment["AGILEFORGE_LAUNCHER_CHILD"] = "0"
    result = subprocess.run(  # noqa: S603 # nosec B603
        [
            sys.executable,
            "-c",
            _COLD_IMPORT_PROBE,
            str(state.business_database),
            outcome,
        ],
        cwd=Path.cwd(),
        env=child_environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "COLD_SCHEMA_IMPORT_OK" in result.stdout
    assert not unused_database.exists()


@pytest.mark.parametrize("current", [False, True])
def test_registered_schema_inspection_reads_committed_wal_schema(
    tmp_path: Path,
    current: bool,
) -> None:
    """Inspect the live read-only schema rather than an immutable pre-WAL image."""
    fixture = (
        Path(__file__).parents[1]
        / "fixtures"
        / "issue_230"
        / "master_b3a4fb4_raw_schema.sql"
    )
    database = tmp_path / "wal-business.sqlite3"
    module = _upgrade_module()
    with sqlite3.connect(database) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.executescript(fixture.read_text(encoding="utf-8"))
        if current:
            for ddl in PROVIDER_AUDIT_INDEX_DDL.values():
                writer.execute(ddl)
        writer.commit()
        database.chmod(0o600)
        before = database.read_bytes()
        wal = database.with_name(database.name + "-wal")
        wal_before = wal.read_bytes()
        observed = module.validate_registered_database(
            database, profile_name="default", require_current=current
        )
        assert observed.complete_sha256 == (_TARGETS[1] if current else _SOURCES[1])
        assert database.read_bytes() == before
        assert wal.read_bytes() == wal_before
