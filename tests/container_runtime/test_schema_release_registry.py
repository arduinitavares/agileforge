# tests/container_runtime/test_schema_release_registry.py
"""Historical fixtures and executable transitions define production releases."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

import pytest
from sqlalchemy import create_engine, inspect

from cli import production_schema_upgrade as upgrades
from cli.container_runtime import backup_production_state, restore_production_state
from cli.production_state import (
    ProductionStateError,
    connection_schema_sha256,
    database_schema_sha256,
    load_production_state,
)
from cli.state_transfer import _inspect_owned_business_schema, verify_backup
from models import db
from tests.container_runtime.test_production_schema_upgrade import _command, _journal
from tests.container_runtime.test_provider_index_manifest import _build, _prior_profile

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Connection

_FIXTURES: Path = Path(__file__).parents[1] / "fixtures"
_LATER: str = "simulated-later-release"
_LATER_DDL: str = (
    "CREATE TABLE schema_release_receipts (receipt_id INTEGER NOT NULL, "
    "release_name TEXT NOT NULL, PRIMARY KEY (receipt_id))"
)


class FrozenReleaseFixture(TypedDict):
    """A separately captured DDL encoding and its immutable expected fingerprints."""

    identity: str
    encoding: str
    complete_sha256: str
    structural_sha256: str
    sql_files: list[str]


class FrozenHistoricalChange(TypedDict):
    """A test-only later release that removes or renames a historical table."""

    change: str
    encoding: str
    complete_sha256: str
    structural_sha256: str
    migration_sql: str


_HISTORICAL_CHANGES: list[FrozenHistoricalChange] = cast(
    "list[FrozenHistoricalChange]",
    json.loads(
        (_FIXTURES / "issue_230" / "historical_table_changes.json").read_text(
            encoding="utf-8"
        )
    ),
)


_CATALOG: list[FrozenReleaseFixture] = cast(
    "list[FrozenReleaseFixture]",
    json.loads(
        (_FIXTURES / "issue_230" / "schema_releases.json").read_text(encoding="utf-8")
    ),
)


def _fixture(identity: str, encoding: str) -> FrozenReleaseFixture:
    return next(
        item
        for item in _CATALOG
        if item["identity"] == identity and item["encoding"] == encoding
    )


def _materialize(database: Path, item: FrozenReleaseFixture) -> None:
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "\n".join(
                (_FIXTURES / sql_file).read_text(encoding="utf-8")
                for sql_file in item["sql_files"]
            )
        )


def _connection_hash(connection: Connection) -> str:
    rows = connection.exec_driver_sql(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name, tbl_name"
    ).all()
    payload = json.dumps(
        {
            "schema": [tuple(row) for row in rows],
            "user_version": connection.exec_driver_sql("PRAGMA user_version").scalar(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _install_later_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migration: Callable[[Connection], None],
) -> None:
    items = [_fixture(_LATER, encoding) for encoding in ("frozen", "sqlalchemy")]
    future = upgrades.SchemaRelease(
        _LATER,
        str(items[0]["structural_sha256"]),
        tuple(
            upgrades.SchemaVariant(str(item["encoding"]), str(item["complete_sha256"]))
            for item in items
        ),
    )
    transition = upgrades.SchemaTransition(
        "issue230-provider-indexes",
        _LATER,
        tuple(
            (
                str(
                    _fixture("issue230-provider-indexes", str(item["encoding"]))[
                        "complete_sha256"
                    ]
                ),
                str(item["complete_sha256"]),
            )
            for item in items
        ),
        migration=migration,
    )
    monkeypatch.setattr(
        upgrades, "SCHEMA_RELEASES", (*upgrades.SCHEMA_RELEASES, future)
    )
    monkeypatch.setattr(
        upgrades, "SCHEMA_TRANSITIONS", (*upgrades.SCHEMA_TRANSITIONS, transition)
    )
    monkeypatch.setattr(upgrades, "CURRENT_SCHEMA_ID", _LATER)
    current_fixture = tmp_path / "future-fixture.sqlite3"
    _materialize(current_fixture, items[0])
    monkeypatch.setattr(
        db,
        "CURRENT_BUSINESS_SCHEMA_MANIFEST",
        _inspect_owned_business_schema(current_fixture),
    )


def _all_observed_fixture_structures(database: Path) -> db.BusinessSchemaManifest:
    """Materialize a release oracle without the inspector's CURRENT-name filter."""
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        names = frozenset(inspect(engine).get_table_names())
        return db.BusinessSchemaManifest(
            table_names=names,
            structures={
                name: db._inspected_table_structure(engine, name) for name in names
            },
        )
    finally:
        engine.dispose()


def _install_historical_table_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    migration: Callable[[Connection], None],
) -> None:
    items = [item for item in _HISTORICAL_CHANGES if item["change"] == change]
    identity = f"simulated-{change}-historical-table"
    release = upgrades.SchemaRelease(
        identity,
        items[0]["structural_sha256"],
        tuple(
            upgrades.SchemaVariant(item["encoding"], item["complete_sha256"])
            for item in items
        ),
    )
    transition = upgrades.SchemaTransition(
        "issue230-provider-indexes",
        identity,
        tuple(
            (
                _fixture("issue230-provider-indexes", item["encoding"])[
                    "complete_sha256"
                ],
                item["complete_sha256"],
            )
            for item in items
        ),
        migration=migration,
    )
    database = tmp_path / "changed-release-fixture.sqlite3"
    _materialize(database, _fixture("issue230-provider-indexes", "frozen"))
    with sqlite3.connect(database) as connection:
        connection.execute(items[0]["migration_sql"])
    manifest = _all_observed_fixture_structures(database)
    assert database_schema_sha256(database) == items[0]["complete_sha256"]
    assert upgrades._structural_fingerprint(manifest) == items[0]["structural_sha256"]
    monkeypatch.setattr(
        upgrades, "SCHEMA_RELEASES", (*upgrades.SCHEMA_RELEASES, release)
    )
    monkeypatch.setattr(
        upgrades, "SCHEMA_TRANSITIONS", (*upgrades.SCHEMA_TRANSITIONS, transition)
    )
    monkeypatch.setattr(upgrades, "CURRENT_SCHEMA_ID", identity)
    monkeypatch.setattr(db, "CURRENT_BUSINESS_SCHEMA_MANIFEST", manifest)


@pytest.mark.parametrize("operation", ["upgrade", "restore"])
@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("change", ["drop", "rename"])
def test_historical_structure_survives_a_current_table_drop_or_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    raw: bool,
    operation: str,
) -> None:
    """Changed current names cannot erase a historical source or intermediate."""
    source = _prior_profile(tmp_path, raw=raw)
    encoding = "sqlalchemy" if raw else "frozen"
    item = next(
        item
        for item in _HISTORICAL_CHANGES
        if item["change"] == change and item["encoding"] == encoding
    )
    bundle = backup_production_state(
        source, tmp_path / "bundle", deployment_root=tmp_path
    )
    before = {
        file.relative_to(bundle): file.read_bytes()
        for file in bundle.rglob("*")
        if file.is_file()
    }
    historical = upgrades.SCHEMA_TRANSITIONS[0]
    seen: list[str] = []

    def issue230(connection: Connection) -> None:
        seen.append("issue230")
        historical.migration(connection)
        assert (
            _connection_hash(connection)
            == _fixture("issue230-provider-indexes", encoding)["complete_sha256"]
        )

    def later(connection: Connection) -> None:
        seen.append(change)
        connection.exec_driver_sql(item["migration_sql"])

    monkeypatch.setattr(
        upgrades, "SCHEMA_TRANSITIONS", (replace(historical, migration=issue230),)
    )
    _install_historical_table_change(tmp_path, monkeypatch, change, later)
    upgrades.validate_schema_registry()
    assert (
        database_schema_sha256(source.business_database)
        == source.business_schema_sha256
    )

    if operation == "upgrade":
        installed = upgrades.upgrade_registered_database(source.business_database)
        database = source.business_database
    else:
        restored = restore_production_state(
            bundle,
            tmp_path / "profiles" / "restored",
            build=_build(),
            deployment_root=tmp_path,
            expected_owner_uid=os.getuid(),
        )
        database = restored.business_database
        installed = upgrades.registered_schema_state(database)
        assert (
            database_schema_sha256(source.business_database)
            == source.business_schema_sha256
        )

    assert seen == ["issue230", change]
    assert installed.release_id == f"simulated-{change}-historical-table"
    assert installed.complete_sha256 == item["complete_sha256"]
    assert installed.structural_sha256 == item["structural_sha256"]
    assert {
        file.relative_to(bundle): file.read_bytes()
        for file in bundle.rglob("*")
        if file.is_file()
    } == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT name FROM projects").fetchall() == [
            ("History",)
        ]


@pytest.mark.parametrize("raw", [False, True])
def test_historical_steps_execute_in_order_in_one_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bool
) -> None:
    """A later table release must run after the exact historical index output."""
    source = _prior_profile(tmp_path, raw=raw)
    encoding = "sqlalchemy" if raw else "frozen"
    original_hash = source.business_schema_sha256
    seen: list[tuple[str, Connection]] = []
    historical = upgrades.SCHEMA_TRANSITIONS[0]

    def reject_bootstrap(*_arguments: object, **_options: object) -> None:
        message = "production transitions dispatched through development bootstrap"
        raise AssertionError(message)

    monkeypatch.setattr(db, "ensure_business_db_ready", reject_bootstrap)

    def issue230(connection: Connection) -> None:
        seen.append(("issue230", connection))
        historical.migration(connection)
        assert (
            _connection_hash(connection)
            == _fixture("issue230-provider-indexes", encoding)["complete_sha256"]
        )
        assert database_schema_sha256(source.business_database) == original_hash

    def later(connection: Connection) -> None:
        seen.append(("later", connection))
        assert (
            _connection_hash(connection)
            == _fixture("issue230-provider-indexes", encoding)["complete_sha256"]
        )
        connection.exec_driver_sql(
            "CREATE TABLE schema_release_receipts (receipt_id INTEGER NOT NULL, "
            "release_name TEXT NOT NULL, PRIMARY KEY (receipt_id))"
        )

    monkeypatch.setattr(
        upgrades, "SCHEMA_TRANSITIONS", (replace(historical, migration=issue230),)
    )
    _install_later_release(tmp_path, monkeypatch, later)

    installed = upgrades.upgrade_registered_database(source.business_database)

    assert [name for name, _ in seen] == ["issue230", "later"]
    assert seen[0][1] is seen[1][1]
    assert installed.release_id == _LATER
    assert installed.complete_sha256 == _fixture(_LATER, encoding)["complete_sha256"]
    assert database_schema_sha256(source.business_database) == installed.complete_sha256
    with sqlite3.connect(source.business_database) as connection:
        assert connection.execute("SELECT name FROM projects").fetchall() == [
            ("History",)
        ]


def test_each_release_variant_has_one_exact_acyclic_route_to_current() -> None:
    """Pure registry data proves reachability and declared endpoint ownership."""
    upgrades.validate_schema_registry()
    states = {
        variant.complete_sha256: release.identity
        for release in upgrades.SCHEMA_RELEASES
        for variant in release.variants
    }
    assert len(states) == sum(
        len(release.variants) for release in upgrades.SCHEMA_RELEASES
    )
    mappings: dict[str, str] = {}
    for transition in upgrades.SCHEMA_TRANSITIONS:
        for source, target in transition.hash_pairs:
            assert source not in mappings
            assert states[source] == transition.source_id
            assert states[target] == transition.target_id
            mappings[source] = target
    for source, release_id in states.items():
        visited: set[str] = set()
        cursor, observed_release = source, release_id
        while observed_release != upgrades.CURRENT_SCHEMA_ID:
            assert cursor not in visited
            visited.add(cursor)
            cursor = mappings[cursor]
            observed_release = states[cursor]


def test_frozen_fixture_inventory_covers_every_registered_encoding() -> None:
    """Adding a release cannot silently leave its historical DDL unverified."""
    registered = {
        (release.identity, variant.encoding)
        for release in upgrades.SCHEMA_RELEASES
        for variant in release.variants
    }
    frozen = {
        (item["identity"], item["encoding"])
        for item in _CATALOG
        if item["identity"] != _LATER
    }
    assert registered == frozen


@pytest.mark.parametrize(
    "item",
    [item for item in _CATALOG if item["identity"] != _LATER],
    ids=lambda item: f"{item['identity']}-{item['encoding']}",
)
def test_registered_fingerprints_match_independently_frozen_release_ddl(
    tmp_path: Path, item: FrozenReleaseFixture
) -> None:
    """Every complete and structural fingerprint comes from its release fixture."""
    database = tmp_path / "frozen.sqlite3"
    _materialize(database, item)

    observed = upgrades.registered_schema_state(database)

    assert observed.release_id == item["identity"]
    assert observed.complete_sha256 == item["complete_sha256"]
    assert observed.structural_sha256 == item["structural_sha256"]


@pytest.mark.parametrize(
    ("transition", "source_sha256", "target_sha256"),
    [
        pytest.param(
            transition,
            source_sha256,
            target_sha256,
            id=f"{transition.source_id}-to-{transition.target_id}-{source_sha256}",
        )
        for transition in upgrades.SCHEMA_TRANSITIONS
        for source_sha256, target_sha256 in transition.hash_pairs
    ],
)
def test_registered_transition_produces_its_frozen_target(
    tmp_path: Path,
    transition: upgrades.SchemaTransition,
    source_sha256: str,
    target_sha256: str,
) -> None:
    """Every registered callback must produce each exact declared target schema."""
    source = next(
        item
        for item in _CATALOG
        if item["identity"] == transition.source_id
        and item["complete_sha256"] == source_sha256
    )
    target = next(
        release
        for release in upgrades.SCHEMA_RELEASES
        if release.identity == transition.target_id
        and any(
            variant.complete_sha256 == target_sha256 for variant in release.variants
        )
    )
    database = tmp_path / "transition-source.sqlite3"
    _materialize(database, source)
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                upgrades._run_schema_transition(connection, transition)
                assert connection_schema_sha256(connection) == target_sha256
                manifest = db._inspect_business_schema_manifest(connection)
                assert (
                    upgrades._structural_fingerprint(manifest)
                    == target.structural_sha256
                )
            finally:
                connection.rollback()
    finally:
        engine.dispose()


@pytest.mark.parametrize("encoding", ["frozen", "sqlalchemy"])
def test_added_table_fixture_pins_its_complete_structure(
    tmp_path: Path, encoding: str
) -> None:
    """The fixture fingerprint must include the added table's actual columns."""
    item = _fixture(_LATER, encoding)
    database = tmp_path / "added-table-fixture.sqlite3"
    _materialize(database, item)

    observed = _inspect_owned_business_schema(database)

    assert observed.structures["schema_release_receipts"] == db.TableStructure(
        columns=(("receipt_id", False), ("release_name", False)),
        uniques=frozenset(),
        foreign_keys=frozenset(),
        checks=frozenset(),
    )
    assert frozenset(observed.structures) == observed.table_names
    assert upgrades._structural_fingerprint(observed) == item["structural_sha256"]
    assert database_schema_sha256(database) == item["complete_sha256"]


def _failure_steps(
    historical: upgrades.SchemaTransition, seen: list[str], failure: str
) -> tuple[Callable[[Connection], None], Callable[[Connection], None]]:
    def issue230(connection: Connection) -> None:
        seen.append("issue230")
        connection.exec_driver_sql("UPDATE projects SET name = 'Changed'")
        if failure == "intermediate-registered-hash":
            return
        historical.migration(connection)
        if failure == "first-exception":
            message = "first step failed"
            raise RuntimeError(message)
        if failure == "intermediate-hash":
            connection.exec_driver_sql("CREATE INDEX unexpected ON projects(name)")

    def later(connection: Connection) -> None:
        seen.append("later")
        if failure == "final-hash":
            return
        if failure == "intermediate-hash":
            connection.exec_driver_sql("DROP INDEX unexpected")
        connection.exec_driver_sql(_LATER_DDL)
        if failure == "later-exception":
            message = "later step failed"
            raise RuntimeError(message)

    return issue230, later


@pytest.mark.parametrize(
    "failure",
    [
        "intermediate-hash",
        "intermediate-registered-hash",
        "final-hash",
        "later-exception",
        "first-exception",
        "structural-fingerprint",
        "current-manifest",
    ],
)
def test_failed_step_rolls_back_the_complete_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Bad intermediate outputs cannot be repaired by running the next step."""
    source = _prior_profile(tmp_path)
    original_manifest = db.CURRENT_BUSINESS_SCHEMA_MANIFEST
    historical = upgrades.SCHEMA_TRANSITIONS[0]
    seen: list[str] = []

    issue230, later = _failure_steps(historical, seen, failure)

    monkeypatch.setattr(
        upgrades, "SCHEMA_TRANSITIONS", (replace(historical, migration=issue230),)
    )
    _install_later_release(tmp_path, monkeypatch, later)
    if failure == "structural-fingerprint":
        future = replace(upgrades.SCHEMA_RELEASES[-1], structural_sha256="0" * 64)
        monkeypatch.setattr(
            upgrades, "SCHEMA_RELEASES", (*upgrades.SCHEMA_RELEASES[:-1], future)
        )
    elif failure == "current-manifest":
        monkeypatch.setattr(db, "CURRENT_BUSINESS_SCHEMA_MANIFEST", original_manifest)

    with pytest.raises((ProductionStateError, RuntimeError)):
        upgrades.upgrade_registered_database(source.business_database)

    assert seen == (
        ["issue230"]
        if failure
        in {"intermediate-hash", "intermediate-registered-hash", "first-exception"}
        else ["issue230", "later"]
    )
    assert (
        database_schema_sha256(source.business_database)
        == source.business_schema_sha256
    )
    with sqlite3.connect(source.business_database) as connection:
        assert connection.execute("SELECT name FROM projects").fetchall() == [
            ("History",)
        ]


@pytest.mark.parametrize("raw", [False, True])
def test_restore_executes_the_later_structural_release_without_changing_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bool
) -> None:
    """The installed copy follows the entire registry chain to a later structure."""
    source = _prior_profile(tmp_path, raw=raw)
    bundle = backup_production_state(
        source, tmp_path / "bundle", deployment_root=tmp_path
    )
    before = {
        file.relative_to(bundle): file.read_bytes()
        for file in bundle.rglob("*")
        if file.is_file()
    }

    def later(connection: Connection) -> None:
        connection.exec_driver_sql(_LATER_DDL)

    _install_later_release(tmp_path, monkeypatch, later)
    restored = restore_production_state(
        bundle,
        tmp_path / "profiles" / "restored",
        build=_build(),
        deployment_root=tmp_path,
        expected_owner_uid=os.getuid(),
    )

    encoding = "sqlalchemy" if raw else "frozen"
    assert (
        restored.business_schema_sha256 == _fixture(_LATER, encoding)["complete_sha256"]
    )
    assert (
        upgrades.registered_schema_state(restored.business_database).release_id
        == _LATER
    )
    assert {
        file.relative_to(bundle): file.read_bytes()
        for file in bundle.rglob("*")
        if file.is_file()
    } == before
    assert not (restored.profile_root / "schema-upgrade.json").exists()
    with sqlite3.connect(restored.business_database) as connection:
        assert connection.execute("SELECT name FROM projects").fetchall() == [
            ("History",)
        ]


def test_later_step_failure_preserves_pending_journal_and_verified_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed chain retains exact pre-upgrade recovery evidence and source state."""
    source = _prior_profile(tmp_path)
    manifest_bytes = (source.profile_root / "runtime.json").read_bytes()

    def later(connection: Connection) -> None:
        connection.exec_driver_sql(_LATER_DDL)
        message = "later step failed"
        raise RuntimeError(message)

    _install_later_release(tmp_path, monkeypatch, later)
    with pytest.raises(RuntimeError, match="later step failed"):
        _command(tmp_path)

    journal = _journal(source)
    assert journal["status"] == "pending"
    backup = Path(str(journal["backup_directory"]))
    assert (
        database_schema_sha256(backup / "business.sqlite3")
        == source.business_schema_sha256
    )
    assert (
        database_schema_sha256(source.business_database)
        == source.business_schema_sha256
    )
    assert (source.profile_root / "runtime.json").read_bytes() == manifest_bytes
    verify_backup(backup)
    backup_manifest = (backup / "manifest.json").read_bytes()

    def corrected_later(connection: Connection) -> None:
        connection.exec_driver_sql(_LATER_DDL)

    monkeypatch.setattr(
        upgrades,
        "SCHEMA_TRANSITIONS",
        (
            *upgrades.SCHEMA_TRANSITIONS[:-1],
            replace(upgrades.SCHEMA_TRANSITIONS[-1], migration=corrected_later),
        ),
    )
    assert _command(tmp_path) == 0
    assert _journal(source)["status"] == "complete"
    assert (backup / "manifest.json").read_bytes() == backup_manifest
    assert (
        load_production_state(
            source.profile_root, build=_build()
        ).business_schema_sha256
        == _fixture(_LATER, "frozen")["complete_sha256"]
    )


@pytest.mark.parametrize(
    "invalid",
    [
        "duplicate-release",
        "duplicate-hash",
        "duplicate-encoding",
        "missing-current",
        "wrong-source",
        "wrong-target",
        "missing-variant",
        "duplicate-pair",
        "ambiguous-source",
        "branch",
        "cycle",
        "orphan",
        "unregistered-target",
    ],
)
def test_registry_rejects_invalid_data_without_database_access(
    monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    """Registry mistakes fail before a production profile or connection is needed."""
    master, current = upgrades.SCHEMA_RELEASES
    transition = upgrades.SCHEMA_TRANSITIONS[0]
    first, second = transition.hash_pairs
    isolated_current = replace(
        current,
        identity="isolated-current",
        variants=(upgrades.SchemaVariant("frozen", "a" * 64),),
    )
    releases = {
        "duplicate-release": (*upgrades.SCHEMA_RELEASES, master),
        "duplicate-hash": (master, replace(current, variants=master.variants)),
        "duplicate-encoding": (
            replace(master, variants=(master.variants[0], master.variants[0])),
            current,
        ),
        "cycle": (*upgrades.SCHEMA_RELEASES, isolated_current),
        "orphan": (
            *upgrades.SCHEMA_RELEASES,
            replace(isolated_current, identity="orphan"),
        ),
    }.get(invalid, upgrades.SCHEMA_RELEASES)
    transitions = {
        "wrong-source": (
            replace(transition, hash_pairs=((first[1], first[1]), second)),
        ),
        "wrong-target": (
            replace(transition, hash_pairs=((first[0], first[0]), second)),
        ),
        "missing-variant": (replace(transition, hash_pairs=(first,)),),
        "duplicate-pair": (replace(transition, hash_pairs=(first, first, second)),),
        "ambiguous-source": (
            replace(transition, hash_pairs=(first, (first[0], second[1]), second)),
        ),
        "branch": (transition, transition),
        "cycle": (
            transition,
            replace(
                transition,
                source_id=current.identity,
                target_id=master.identity,
                hash_pairs=tuple(
                    (target, source) for source, target in transition.hash_pairs
                ),
            ),
        ),
        "unregistered-target": (
            replace(transition, hash_pairs=((first[0], "a" * 64), second)),
        ),
    }.get(invalid, upgrades.SCHEMA_TRANSITIONS)
    if invalid == "missing-current":
        monkeypatch.setattr(upgrades, "CURRENT_SCHEMA_ID", "missing-release")
    elif invalid == "cycle":
        monkeypatch.setattr(upgrades, "CURRENT_SCHEMA_ID", isolated_current.identity)
    monkeypatch.setattr(upgrades, "SCHEMA_RELEASES", releases)
    monkeypatch.setattr(upgrades, "SCHEMA_TRANSITIONS", transitions)

    with pytest.raises(ProductionStateError, match="registry"):
        upgrades.validate_schema_registry()


@pytest.mark.parametrize(
    "boundary",
    [
        "commit",
        "rollback",
        "raw-commit",
        "dbapi-commit",
        "savepoint",
        "swallowed-commit",
    ],
)
def test_migration_cannot_end_the_callers_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    """A callable's transaction boundary must never durably publish partial DDL."""
    source = _prior_profile(tmp_path)
    original_hash = source.business_schema_sha256

    def later(connection: Connection) -> None:
        connection.exec_driver_sql(
            "CREATE TABLE schema_release_receipts (receipt_id INTEGER NOT NULL, "
            "release_name TEXT NOT NULL, PRIMARY KEY (receipt_id))"
        )
        if boundary == "commit":
            connection.commit()
        elif boundary == "rollback":
            connection.rollback()
        elif boundary == "raw-commit":
            connection.exec_driver_sql("COMMIT")
        elif boundary == "savepoint":
            connection.exec_driver_sql("SAVEPOINT callable_boundary")
        else:
            driver = connection.connection.driver_connection
            assert isinstance(driver, sqlite3.Connection)
            if boundary == "swallowed-commit":
                with suppress(sqlite3.DatabaseError):
                    driver.commit()
            else:
                driver.commit()

    _install_later_release(tmp_path, monkeypatch, later)

    with pytest.raises(ProductionStateError, match="transaction"):
        upgrades.upgrade_registered_database(source.business_database)

    assert database_schema_sha256(source.business_database) == original_hash
    with sqlite3.connect(source.business_database) as connection:
        assert connection.execute("SELECT name FROM projects").fetchall() == [
            ("History",)
        ]
