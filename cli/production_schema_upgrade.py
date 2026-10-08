# cli/production_schema_upgrade.py
"""Explicit production schema maintenance with exact releases and rollback evidence."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import (
    Path,  # noqa: TC003 - Pydantic resolves journal field types at runtime.
)
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import create_engine

from cli.production_model_config import (
    _fsync_directory,
    _Marker,
    _marker_path,
    _owned_bytes,
    _publish_bytes,
    _read_marker,
    validate_startup_marker,
)
from cli.production_state import (
    ProductionStateError,
    ProductionStateManifest,
    ProductionStatePaths,
    _load_production_state_pair,
    _publish_manifest,
    _require_owned_directory,
    connection_schema_sha256,
    database_schema_sha256,
    load_production_state,
    production_state_paths,
)
from cli.state_transfer import (
    _inspect_owned_business_schema,
    verify_backup,
    verify_current_business_schema,
    verify_current_trace_schema,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Connection

    from models.db import BusinessSchemaManifest
    from utils.build_identity import BuildIdentity


@dataclass(frozen=True, slots=True)
class SchemaVariant:
    """One reviewed, exact SQLite DDL encoding of a release."""

    encoding: str
    complete_sha256: str


@dataclass(frozen=True, slots=True)
class SchemaRelease:
    """An immutable historical release; future releases append to the registry."""

    identity: str
    structural_sha256: str
    variants: tuple[SchemaVariant, ...]


@dataclass(frozen=True, slots=True)
class SchemaTransition:
    """Reviewed exact hash pairs and one caller-transaction migration step."""

    source_id: str
    target_id: str
    hash_pairs: tuple[tuple[str, str], ...]
    migration: Callable[[Connection], None]


@dataclass(frozen=True, slots=True)
class RegisteredSchemaState:
    """The exact release and DDL encoding observed in an owned database."""

    release_id: str
    complete_sha256: str
    structural_sha256: str


_ISSUE230_INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX ix_workflow_events_provider_action ON workflow_events "
    "(json_extract(CASE WHEN json_valid(event_metadata) = 1 THEN event_metadata "
    "ELSE '{}' END, '$.action_id'), project_id, event_id) "
    "WHERE event_type IN ('PROVIDER_TRY_STARTED', 'PROVIDER_TRY_FINISHED')",
    "CREATE INDEX ix_workflow_events_provider_call ON workflow_events "
    "(json_extract(CASE WHEN json_valid(event_metadata) = 1 THEN event_metadata "
    "ELSE '{}' END, '$.call_id'), project_id, event_id) "
    "WHERE event_type IN ('PROVIDER_TRY_STARTED', 'PROVIDER_TRY_FINISHED')",
    "CREATE INDEX ix_workflow_events_provider_invalid ON workflow_events "
    "(project_id, event_id) WHERE event_type IN "
    "('PROVIDER_TRY_STARTED', 'PROVIDER_TRY_FINISHED') "
    "AND coalesce(json_valid(event_metadata), 0) != 1",
)


def _migrate_issue230_provider_indexes(connection: Connection) -> None:
    """Install this release's frozen indexes independently of later metadata."""
    for ddl in _ISSUE230_INDEX_DDL:
        connection.exec_driver_sql(ddl)


SCHEMA_RELEASES: tuple[SchemaRelease, ...] = (
    SchemaRelease(
        "master-b3a4fb4",
        "c99398b56ccd7871e6840a8cb218bf595970ee1f8aa492b12ec4053830923425",
        (
            SchemaVariant(
                "frozen",
                "3f1d81bbdec3c5f1f699877cc82f154c0b8e4fb1b2c3dd4824e51603098d0a88",
            ),
            SchemaVariant(
                "sqlalchemy",
                "d257a8ce162334434d2adc069228ef004db1eb04978db2543a410b7be4a8f29c",
            ),
        ),
    ),
    SchemaRelease(
        "issue230-provider-indexes",
        "c99398b56ccd7871e6840a8cb218bf595970ee1f8aa492b12ec4053830923425",
        (
            SchemaVariant(
                "frozen",
                "f9bd5c17713d15174727cd79e235a1dd62a7e11bac5392c3fbdeb9bf85a339c2",
            ),
            SchemaVariant(
                "sqlalchemy",
                "66df69cc4eece12fe24d9645558682134454f1c69060f7d09966af3bb755fd64",
            ),
        ),
    ),
)
SCHEMA_TRANSITIONS: tuple[SchemaTransition, ...] = (
    SchemaTransition(
        "master-b3a4fb4",
        "issue230-provider-indexes",
        (
            (
                "3f1d81bbdec3c5f1f699877cc82f154c0b8e4fb1b2c3dd4824e51603098d0a88",
                "f9bd5c17713d15174727cd79e235a1dd62a7e11bac5392c3fbdeb9bf85a339c2",
            ),
            (
                "d257a8ce162334434d2adc069228ef004db1eb04978db2543a410b7be4a8f29c",
                "66df69cc4eece12fe24d9645558682134454f1c69060f7d09966af3bb755fd64",
            ),
        ),
        migration=_migrate_issue230_provider_indexes,
    ),
)
CURRENT_SCHEMA_ID: str = "issue230-provider-indexes"
_JOURNAL_NAME: str = "schema-upgrade.json"
_HASH: str = r"^[0-9a-f]{64}$"


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _structural_fingerprint(manifest: BusinessSchemaManifest) -> str:
    structures = {
        name: {
            "columns": shape.columns,
            "uniques": sorted(shape.uniques, key=repr),
            "foreign_keys": sorted(shape.foreign_keys, key=repr),
            "checks": sorted(shape.checks),
        }
        for name, shape in sorted(manifest.structures.items())
    }
    payload = {"tables": sorted(manifest.table_names), "structures": structures}
    return _digest(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    )


def _registry_states() -> dict[str, RegisteredSchemaState]:
    identities = [release.identity for release in SCHEMA_RELEASES]
    if len(identities) != len(set(identities)) or CURRENT_SCHEMA_ID not in identities:
        message = "production schema registry release identities are inconsistent"
        raise ProductionStateError(message)
    states: dict[str, RegisteredSchemaState] = {}
    for release in SCHEMA_RELEASES:
        encodings = [variant.encoding for variant in release.variants]
        if (
            not encodings
            or len(encodings) != len(set(encodings))
            or not re.fullmatch(_HASH, release.structural_sha256)
        ):
            message = "production schema registry release variants are inconsistent"
            raise ProductionStateError(message)
        for variant in release.variants:
            if variant.complete_sha256 in states or not re.fullmatch(
                _HASH, variant.complete_sha256
            ):
                message = "production schema registry complete hashes are inconsistent"
                raise ProductionStateError(message)
            states[variant.complete_sha256] = RegisteredSchemaState(
                release.identity, variant.complete_sha256, release.structural_sha256
            )
    return states


def _validate_transition(
    transition: SchemaTransition,
    releases: dict[str, SchemaRelease],
    states: dict[str, RegisteredSchemaState],
) -> None:
    if (
        transition.source_id not in releases
        or transition.target_id not in releases
        or not callable(transition.migration)
    ):
        message = "production schema registry transition releases are inconsistent"
        raise ProductionStateError(message)
    source_hashes = [source for source, _ in transition.hash_pairs]
    expected_sources = {
        variant.complete_sha256 for variant in releases[transition.source_id].variants
    }
    if (
        len(source_hashes) != len(set(source_hashes))
        or set(source_hashes) != expected_sources
    ):
        message = "production schema registry transition mappings are inconsistent"
        raise ProductionStateError(message)
    for source_hash, target_hash in transition.hash_pairs:
        source = states.get(source_hash)
        target = states.get(target_hash)
        if (
            source is None
            or source.release_id != transition.source_id
            or target is None
            or target.release_id != transition.target_id
        ):
            message = "production schema registry transition endpoints are inconsistent"
            raise ProductionStateError(message)


def validate_schema_registry() -> None:
    """Prove every exact registered variant has one acyclic route to current."""
    states = _registry_states()
    releases = {release.identity: release for release in SCHEMA_RELEASES}
    outgoing: dict[str, SchemaTransition] = {}
    for transition in SCHEMA_TRANSITIONS:
        _validate_transition(transition, releases, states)
        if (
            transition.source_id in outgoing
            or transition.source_id == CURRENT_SCHEMA_ID
        ):
            message = "production schema registry has no unique current upgrade path"
            raise ProductionStateError(message)
        outgoing[transition.source_id] = transition
    for release in SCHEMA_RELEASES:
        visited: set[str] = set()
        identity = release.identity
        while identity != CURRENT_SCHEMA_ID:
            if identity in visited or identity not in outgoing:
                message = (
                    "production schema registry has an unreachable or cyclic release"
                )
                raise ProductionStateError(message)
            visited.add(identity)
            identity = outgoing[identity].target_id


def _state_by_hash(complete_sha256: str) -> RegisteredSchemaState:
    matches = [
        RegisteredSchemaState(
            release.identity, variant.complete_sha256, release.structural_sha256
        )
        for release in SCHEMA_RELEASES
        for variant in release.variants
        if variant.complete_sha256 == complete_sha256
    ]
    if len(matches) != 1:
        message = "unregistered production database schema"
        raise ProductionStateError(message)
    return matches[0]


def registered_schema_state(
    database: Path, *, immutable: bool = False
) -> RegisteredSchemaState:
    """Recognize only a complete registered hash and its reviewed table structure."""
    validate_schema_registry()
    state = _state_by_hash(database_schema_sha256(database, immutable=immutable))
    observed = _inspect_owned_business_schema(database, immutable=immutable)
    if _structural_fingerprint(observed) != state.structural_sha256:
        message = "registered production schema structure has drifted"
        raise ProductionStateError(message)
    return state


def _upgrade_message(profile_name: str) -> str:
    return (
        "production schema needs explicit upgrade or recovery; "
        "set project_name to your selected Compose project, then run "
        'docker compose --project-name "${project_name:?set project_name to your '
        'selected Compose project}" run --rm production upgrade '
        f"--profile {profile_name}"
    )


def validate_registered_database(
    database: Path,
    *,
    profile_name: str,
    require_current: bool = False,
    immutable: bool = False,
) -> RegisteredSchemaState:
    """Shared exact-state gate for startup, backup, and installed restore payloads."""
    state = registered_schema_state(database, immutable=immutable)
    if require_current:
        if state.release_id != CURRENT_SCHEMA_ID:
            raise ProductionStateError(_upgrade_message(profile_name))
        verify_current_business_schema(database, immutable=immutable)
    return state


def _upgrade_path(
    source: RegisteredSchemaState,
) -> tuple[tuple[SchemaTransition, RegisteredSchemaState], ...]:
    validate_schema_registry()
    visited: set[str] = set()
    current = source
    steps: list[tuple[SchemaTransition, RegisteredSchemaState]] = []
    while current.release_id != CURRENT_SCHEMA_ID:
        if current.release_id in visited:
            message = "production schema registry contains a migration cycle"
            raise ProductionStateError(message)
        visited.add(current.release_id)
        transitions = [
            entry
            for entry in SCHEMA_TRANSITIONS
            if entry.source_id == current.release_id
        ]
        if len(transitions) != 1:
            message = "registered schema has no unique upgrade path"
            raise ProductionStateError(message)
        transition = transitions[0]
        targets = [
            target
            for source_hash, target in transition.hash_pairs
            if source_hash == current.complete_sha256
        ]
        if len(targets) != 1:
            message = "registered schema encoding has no exact upgrade target"
            raise ProductionStateError(message)
        current = _state_by_hash(targets[0])
        if current.release_id != transition.target_id:
            message = "production schema transition target is inconsistent"
            raise ProductionStateError(message)
        steps.append((transition, current))
    return tuple(steps)


def _target_state(source: RegisteredSchemaState) -> RegisteredSchemaState:
    path = _upgrade_path(source)
    return path[-1][1] if path else source


def _registered_connection_state(connection: Connection) -> RegisteredSchemaState:
    from models.db import _inspect_business_schema_manifest  # noqa: PLC0415

    state = _state_by_hash(connection_schema_sha256(connection))
    observed = _inspect_business_schema_manifest(connection)
    if _structural_fingerprint(observed) != state.structural_sha256:
        message = "registered production schema structure has drifted"
        raise ProductionStateError(message)
    return state


def _require_connection_state(
    connection: Connection, expected: RegisteredSchemaState, *, message: str
) -> None:
    if _registered_connection_state(connection) != expected:
        raise ProductionStateError(message)


def _require_current_connection_schema(connection: Connection) -> None:
    from models.db import (  # noqa: PLC0415
        CURRENT_BUSINESS_SCHEMA_MANIFEST,
        _inspect_business_schema_manifest,
    )

    if (
        _inspect_business_schema_manifest(connection)
        != CURRENT_BUSINESS_SCHEMA_MANIFEST
    ):
        message = "unsupported current business schema"
        raise ProductionStateError(message)


def _run_schema_transition(
    connection: Connection, transition: SchemaTransition
) -> None:
    """Keep transaction boundaries owned by the caller of each trusted step."""
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        message = "production migration requires a SQLite transaction"
        raise ProductionStateError(message)
    boundary_attempted = False

    def authorize(
        operation: int,
        _argument_one: str | None,
        _argument_two: str | None,
        _database_name: str | None,
        _trigger_name: str | None,
    ) -> int:
        nonlocal boundary_attempted
        if operation in {sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT}:
            boundary_attempted = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    driver.set_authorizer(authorize)
    try:
        transition.migration(connection)
    except BaseException as error:
        if boundary_attempted:
            message = "production migration callable attempted transaction control"
            raise ProductionStateError(message) from error
        raise
    finally:
        driver.set_authorizer(None)
    if boundary_attempted:
        message = "production migration callable attempted transaction control"
        raise ProductionStateError(message)


def upgrade_registered_database(database: Path) -> RegisteredSchemaState:
    """Run the reviewed migration chain and require its statically registered target."""
    source = registered_schema_state(database)
    steps = _upgrade_path(source)
    if not steps:
        verify_current_business_schema(database, immutable=False)
        return source
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                _require_connection_state(
                    connection,
                    source,
                    message="production migration source changed before transaction",
                )
                for transition, target in steps:
                    _run_schema_transition(connection, transition)
                    _require_connection_state(
                        connection,
                        target,
                        message=(
                            "production migration did not produce the registered target"
                        ),
                    )
                _require_current_connection_schema(connection)
                connection.commit()
            except BaseException:
                # A denied Connection.commit() can end SQLAlchemy's transaction
                # object while SQLite still owns all uncommitted DDL.
                connection.connection.rollback()
                connection.rollback()
                raise
    finally:
        engine.dispose()
    return steps[-1][1]


class _Journal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["agileforge.schema-upgrade.v1"]
    operation_id: UUID
    status: Literal["pending", "complete"]
    profile_root: Path
    profile_name: str
    state_id: UUID
    source_schema_id: str
    target_schema_id: str
    source_schema_sha256: str = Field(pattern=_HASH)
    target_schema_sha256: str = Field(pattern=_HASH)
    source_manifest_sha256: str = Field(pattern=_HASH)
    target_manifest_sha256: str = Field(pattern=_HASH)
    backup_directory: Path
    backup_manifest_sha256: str = Field(pattern=_HASH)
    model_marker_sha256: str | None = Field(default=None, pattern=_HASH)


def _read_journal(paths: ProductionStatePaths, owner_uid: int) -> _Journal | None:
    file = paths.root / _JOURNAL_NAME
    if not file.exists() and not file.is_symlink():
        return None
    try:
        journal = _Journal.model_validate_json(
            _owned_bytes(file, label="schema upgrade journal", owner_uid=owner_uid)
        )
    except ValidationError as error:
        message = "schema upgrade journal is invalid"
        raise ProductionStateError(message) from error
    if journal.profile_root != paths.root or journal.profile_name != paths.root.name:
        message = "schema upgrade journal names another profile"
        raise ProductionStateError(message)
    return journal


def validate_upgrade_startup(
    paths: ProductionStatePaths, *, expected_owner_uid: int
) -> None:
    """Reject pending operations outside explicit exclusive maintenance."""
    journal = _read_journal(paths, expected_owner_uid)
    if journal is not None and journal.status != "complete":
        raise ProductionStateError(_upgrade_message(paths.root.name))


def load_registered_production_state(
    profile_root: Path, *, build: BuildIdentity, expected_owner_uid: int | None = None
) -> ProductionStateManifest:
    """Validate a registered profile for an independent rollback backup."""
    state = load_production_state(
        profile_root,
        build=build,
        expected_owner_uid=expected_owner_uid,
        validate_current_schema=False,
    )
    validate_registered_database(
        state.business_database, profile_name=state.profile_name
    )
    if state.trace_database.exists():
        verify_current_trace_schema(state.trace_database)
    return state


def _publish_journal(paths: ProductionStatePaths, journal: _Journal) -> None:
    payload = (journal.model_dump_json(indent=2) + "\n").encode()
    # Retain the final receipt when a later release replaces the active journal.
    _publish_bytes(journal.backup_directory.parent / "journal.json", payload)
    _publish_bytes(paths.root / _JOURNAL_NAME, payload)


def _retire_model_marker(
    paths: ProductionStatePaths, journal: _Journal, owner_uid: int
) -> None:
    marker_path = _marker_path(paths)
    if not marker_path.exists() and not marker_path.is_symlink():
        return
    payload = _owned_bytes(
        marker_path, label="model update marker", owner_uid=owner_uid
    )
    if (
        journal.model_marker_sha256 is None
        or _digest(payload) != journal.model_marker_sha256
    ):
        message = "model update marker differs from upgrade rollback evidence"
        raise ProductionStateError(message)
    marker_path.unlink()
    _fsync_directory(paths.root)


def _private_directory(path: Path, owner_uid: int, *, create: bool = False) -> None:
    if create and not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
        _fsync_directory(path.parent)
    metadata = _require_owned_directory(
        path, label="schema upgrade evidence directory", expected_owner_uid=owner_uid
    )
    if metadata.st_mode & 0o077:
        message = "schema upgrade evidence directory must be private"
        raise ProductionStateError(message)


def _verified_terminal_marker(
    paths: ProductionStatePaths, owner_uid: int
) -> Path | None:
    marker = _read_marker(paths, owner_uid)
    if marker is None:
        return None
    if marker.status in {"applying", "recovering"}:
        message = "model update needs explicit recover-models before schema upgrade"
        raise ProductionStateError(message)
    validate_startup_marker(paths, expected_owner_uid=owner_uid)
    return _marker_path(paths)


def _target_manifest(
    state: ProductionStateManifest, target_hash: str
) -> ProductionStateManifest:
    return state.model_copy(update={"business_schema_sha256": target_hash})


def _manifest_digest(state: ProductionStateManifest) -> str:
    return _digest((state.model_dump_json(indent=2) + "\n").encode())


def _verify_journal_evidence(  # noqa: C901
    paths: ProductionStatePaths,
    journal: _Journal,
    deployment_root: Path,
    owner_uid: int,
) -> ProductionStateManifest:
    expected = (
        deployment_root / "upgrade-backups" / str(journal.operation_id) / "bundle"
    )
    if journal.backup_directory != expected:
        message = "schema upgrade backup path does not match deployment-owned operation"
        raise ProductionStateError(message)
    _private_directory(expected.parent.parent, owner_uid)
    _private_directory(expected.parent, owner_uid)
    verify_backup(expected)
    backup_manifest = _owned_bytes(
        expected / "manifest.json",
        label="rollback bundle manifest",
        owner_uid=owner_uid,
    )
    source_manifest = _owned_bytes(
        expected / "provenance" / "runtime.json",
        label="rollback profile manifest",
        owner_uid=owner_uid,
    )
    if (
        _digest(backup_manifest) != journal.backup_manifest_sha256
        or _digest(source_manifest) != journal.source_manifest_sha256
    ):
        message = "schema upgrade rollback receipt has drifted"
        raise ProductionStateError(message)
    try:
        state = ProductionStateManifest.model_validate_json(source_manifest)
    except ValidationError as error:
        message = "schema upgrade rollback profile manifest is invalid"
        raise ProductionStateError(message) from error
    if (
        state.state_id != journal.state_id
        or state.profile_root != paths.root
        or state.profile_name != paths.root.name
        or state.business_schema_sha256 != journal.source_schema_sha256
    ):
        message = "schema upgrade rollback profile identity is inconsistent"
        raise ProductionStateError(message)
    source = registered_schema_state(expected / "business.sqlite3", immutable=True)
    target = _target_state(source)
    if (
        source.release_id,
        source.complete_sha256,
        target.release_id,
        target.complete_sha256,
    ) != (
        journal.source_schema_id,
        journal.source_schema_sha256,
        journal.target_schema_id,
        journal.target_schema_sha256,
    ):
        message = "schema upgrade journal does not name an exact registered transition"
        raise ProductionStateError(message)
    if (
        _digest(
            _owned_bytes(
                expected / "model-config",
                label="rollback model configuration",
                owner_uid=owner_uid,
            )
        )
        != state.model_config_sha256
        or _manifest_digest(_target_manifest(state, target.complete_sha256))
        != journal.target_manifest_sha256
    ):
        message = "schema upgrade journal does not match its rollback pair"
        raise ProductionStateError(message)
    marker_path = expected / "provenance" / "model-config-update.json"
    if journal.model_marker_sha256 is not None:
        marker_bytes = _owned_bytes(
            marker_path, label="archived model update marker", owner_uid=owner_uid
        )
        if _digest(marker_bytes) != journal.model_marker_sha256:
            message = "archived model update marker has drifted"
            raise ProductionStateError(message)
        try:
            marker = _Marker.model_validate_json(marker_bytes)
        except ValidationError as error:
            message = "archived model update marker is invalid"
            raise ProductionStateError(message) from error
        if marker.status not in {"complete", "recovered"}:
            message = "archived model update marker is unfinished"
            raise ProductionStateError(message)
        model_hash = (
            marker.new_model_sha256
            if marker.status == "complete"
            else marker.old_model_sha256
        )
        manifest_hash = (
            marker.new_manifest_sha256
            if marker.status == "complete"
            else marker.old_manifest_sha256
        )
        if (
            marker.state_id != state.state_id
            or marker.profile_root != str(paths.root)
            or marker.profile_name != state.profile_name
            or model_hash != state.model_config_sha256
            or manifest_hash != journal.source_manifest_sha256
        ):
            message = "archived model update marker does not match rollback pair"
            raise ProductionStateError(message)
    elif marker_path.exists() or marker_path.is_symlink():
        message = "unexpected archived model update marker"
        raise ProductionStateError(message)
    return state


def _resume_upgrade(
    paths: ProductionStatePaths,
    journal: _Journal,
    *,
    build: BuildIdentity,
    deployment_root: Path,
    owner_uid: int,
) -> ProductionStateManifest:
    source = _verify_journal_evidence(paths, journal, deployment_root, owner_uid)
    observed_hash = database_schema_sha256(paths.business_database)
    if observed_hash not in {
        journal.source_schema_sha256,
        journal.target_schema_sha256,
    }:
        message = "interrupted production upgrade has an unregistered database state"
        raise ProductionStateError(message)
    manifest_hash = _digest(
        _owned_bytes(
            paths.manifest, label="production state manifest", owner_uid=owner_uid
        )
    )
    if manifest_hash not in {
        journal.source_manifest_sha256,
        journal.target_manifest_sha256,
    }:
        message = "interrupted production upgrade manifest has drifted"
        raise ProductionStateError(message)
    if (
        manifest_hash == journal.target_manifest_sha256
        and observed_hash != journal.target_schema_sha256
    ):
        message = "upgraded manifest precedes its required database state"
        raise ProductionStateError(message)
    _load_production_state_pair(
        paths.root,
        build=build,
        expected_owner_uid=owner_uid,
        validate_current_schema=False,
        expected_manifest_sha256=manifest_hash,
        expected_model_sha256=source.model_config_sha256,
        allowed_business_schema_sha256=journal.target_schema_sha256,
    )
    if paths.trace_database.exists():
        verify_current_trace_schema(paths.trace_database)
    # The exact archived terminal marker belongs to the source rollback pair.
    marker = _read_marker(paths, owner_uid)
    if marker is not None and marker.status in {"applying", "recovering"}:
        message = "model update needs explicit recover-models before schema upgrade"
        raise ProductionStateError(message)
    _retire_model_marker(paths, journal, owner_uid)
    if observed_hash == journal.source_schema_sha256:
        upgrade_registered_database(paths.business_database)
    target = validate_registered_database(
        paths.business_database, profile_name=paths.root.name, require_current=True
    )
    if target.complete_sha256 != journal.target_schema_sha256:
        message = "production upgrade target differs from journal"
        raise ProductionStateError(message)
    updated = _target_manifest(source, target.complete_sha256)
    if manifest_hash != journal.target_manifest_sha256:
        _publish_manifest(paths, updated)
    _load_production_state_pair(
        paths.root,
        build=build,
        expected_owner_uid=owner_uid,
        expected_manifest_sha256=journal.target_manifest_sha256,
        expected_model_sha256=source.model_config_sha256,
    )
    _publish_journal(paths, journal.model_copy(update={"status": "complete"}))
    return load_production_state(paths.root, build=build, expected_owner_uid=owner_uid)


def upgrade_production_state(
    profile_root: Path,
    *,
    build: BuildIdentity,
    deployment_root: Path,
    expected_owner_uid: int,
) -> dict[str, object]:
    """Upgrade or recover one profile under caller-held exclusive runtime fences."""
    from cli.container_runtime import backup_production_state  # noqa: PLC0415

    paths = production_state_paths(profile_root)
    _require_owned_directory(
        paths.root,
        label="production profile root",
        expected_owner_uid=expected_owner_uid,
    )
    marker = _read_marker(paths, expected_owner_uid)
    if marker is not None and marker.status in {"applying", "recovering"}:
        message = "model update needs explicit recover-models before schema upgrade"
        raise ProductionStateError(message)
    journal = _read_journal(paths, expected_owner_uid)
    if journal is not None and journal.status == "pending":
        state = _resume_upgrade(
            paths,
            journal,
            build=build,
            deployment_root=deployment_root,
            owner_uid=expected_owner_uid,
        )
        return {
            "ok": True,
            "status": "upgraded",
            "state": state.model_dump(mode="json"),
            "backup": str(journal.backup_directory),
        }
    state = load_registered_production_state(
        profile_root, build=build, expected_owner_uid=expected_owner_uid
    )
    source = registered_schema_state(paths.business_database)
    target = _target_state(source)
    if source == target:
        load_production_state(
            profile_root, build=build, expected_owner_uid=expected_owner_uid
        )
        return {
            "ok": True,
            "status": "already-current",
            "state": state.model_dump(mode="json"),
        }
    marker_path = _verified_terminal_marker(paths, expected_owner_uid)
    operation = uuid4()
    backup_parent = deployment_root / "upgrade-backups"
    _private_directory(backup_parent, expected_owner_uid, create=True)
    operation_directory = backup_parent / str(operation)
    _private_directory(operation_directory, expected_owner_uid, create=True)
    bundle = backup_production_state(
        state,
        operation_directory / "bundle",
        deployment_root=deployment_root,
        maintenance_fences_held=True,
        additional_provenance=() if marker_path is None else (marker_path,),
    )
    verify_backup(bundle)
    journal = _Journal(
        version="agileforge.schema-upgrade.v1",
        operation_id=operation,
        status="pending",
        profile_root=paths.root,
        profile_name=state.profile_name,
        state_id=state.state_id,
        source_schema_id=source.release_id,
        target_schema_id=target.release_id,
        source_schema_sha256=source.complete_sha256,
        target_schema_sha256=target.complete_sha256,
        source_manifest_sha256=_digest(
            _owned_bytes(
                paths.manifest,
                label="production state manifest",
                owner_uid=expected_owner_uid,
            )
        ),
        target_manifest_sha256=_manifest_digest(
            _target_manifest(state, target.complete_sha256)
        ),
        backup_directory=bundle,
        backup_manifest_sha256=_digest(
            _owned_bytes(
                bundle / "manifest.json",
                label="rollback bundle manifest",
                owner_uid=expected_owner_uid,
            )
        ),
        model_marker_sha256=None
        if marker_path is None
        else _digest(
            _owned_bytes(
                marker_path, label="model update marker", owner_uid=expected_owner_uid
            )
        ),
    )
    _verify_journal_evidence(paths, journal, deployment_root, expected_owner_uid)
    _publish_journal(paths, journal)
    updated = _resume_upgrade(
        paths,
        journal,
        build=build,
        deployment_root=deployment_root,
        owner_uid=expected_owner_uid,
    )
    return {
        "ok": True,
        "status": "upgraded",
        "state": updated.model_dump(mode="json"),
        "backup": str(bundle),
    }
