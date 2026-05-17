"""Read-only schema readiness checks for CLI projections."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.sql import text

from services.agent_workbench.version import STORAGE_SCHEMA_VERSION


@dataclass(frozen=True)
class SchemaRequirement:
    """Required table and columns for a projection."""

    table: str
    columns: Sequence[str]
    indexes: Sequence[str] = ()
    storage_schema_version: str | None = None

    def __post_init__(self) -> None:
        """Normalize sequence fields while rejecting bare strings."""
        if isinstance(self.columns, str):
            message = "columns must be a sequence of column names"
            raise TypeError(message)
        if isinstance(self.indexes, str):
            message = "indexes must be a sequence of index names"
            raise TypeError(message)
        object.__setattr__(self, "columns", tuple(self.columns))
        object.__setattr__(self, "indexes", tuple(self.indexes))


MUTATION_LEDGER_TABLE = "cli_mutation_ledger"
MUTATION_LEDGER_REQUIRED_COLUMNS: tuple[str, ...] = (
    "mutation_event_id",
    "command",
    "idempotency_key",
    "request_hash",
    "project_id",
    "correlation_id",
    "changed_by",
    "status",
    "current_step",
    "completed_steps_json",
    "guard_inputs_json",
    "before_json",
    "after_json",
    "response_json",
    "recovers_mutation_event_id",
    "superseded_by_mutation_event_id",
    "recovery_action",
    "recovery_safe_to_auto_resume",
    "lease_owner",
    "lease_acquired_at",
    "last_heartbeat_at",
    "lease_expires_at",
    "last_error_json",
    "created_at",
    "updated_at",
)
MUTATION_LEDGER_REQUIREMENTS: tuple[SchemaRequirement, ...] = (
    SchemaRequirement(
        table=MUTATION_LEDGER_TABLE,
        columns=MUTATION_LEDGER_REQUIRED_COLUMNS,
    ),
)

AUTHORITY_DECISION_REQUIRED_COLUMNS: tuple[str, ...] = (
    "pending_authority_id",
    "authority_fingerprint",
    "review_token",
    "review_fingerprint",
    "disk_spec_hash",
    "resolved_spec_path",
    "actor_mode",
    "review_completeness",
    "incomplete_review_override",
    "incomplete_review_rationale",
    "terminal_decision_key",
    "provenance_source",
)
AUTHORITY_DECISION_REQUIREMENTS: tuple[SchemaRequirement, ...] = (
    SchemaRequirement(
        table="spec_authority_acceptance",
        columns=AUTHORITY_DECISION_REQUIRED_COLUMNS,
        indexes=("uq_spec_authority_terminal_decision_key",),
        storage_schema_version=STORAGE_SCHEMA_VERSION,
    ),
)


@dataclass(frozen=True)
class SchemaReadiness:
    """Schema readiness result."""

    ok: bool
    missing: dict[str, list[str]]


def check_schema_readiness(
    engine: Engine,
    requirements: Sequence[SchemaRequirement],
) -> SchemaReadiness:
    """Return missing schema elements without creating or migrating anything."""
    if _is_missing_sqlite_file(engine):
        return SchemaReadiness(ok=False, missing=_missing_all(requirements))

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    missing: dict[str, list[str]] = {}

    for requirement in requirements:
        if requirement.table not in table_names:
            missing[requirement.table] = list(requirement.columns)
            continue

        existing_columns = {
            column["name"] for column in inspector.get_columns(requirement.table)
        }
        missing_columns = [
            column for column in requirement.columns if column not in existing_columns
        ]
        missing_elements = list(missing_columns)

        if requirement.indexes:
            existing_indexes = _sqlite_index_names(engine, requirement.table)
            missing_elements.extend(
                index for index in requirement.indexes if index not in existing_indexes
            )

        if requirement.storage_schema_version is not None:
            actual_version = _storage_schema_version(engine)
            if actual_version != requirement.storage_schema_version:
                missing_elements.append(
                    f"storage_schema_version:{requirement.storage_schema_version}"
                )

        if missing_elements:
            missing[requirement.table] = missing_elements

    return SchemaReadiness(ok=not missing, missing=missing)


def _is_missing_sqlite_file(engine: Engine) -> bool:
    """Return whether a SQLite file URL targets an absent database file."""
    if not engine.url.drivername.startswith("sqlite"):
        return False

    database = engine.url.database
    if database in {None, "", ":memory:"}:
        return False

    return not Path(database).exists()


def _missing_all(requirements: Sequence[SchemaRequirement]) -> dict[str, list[str]]:
    """Return every required column as missing for absent schema storage."""
    return {
        requirement.table: [
            *requirement.columns,
            *requirement.indexes,
            *(
                [f"storage_schema_version:{requirement.storage_schema_version}"]
                if requirement.storage_schema_version is not None
                else []
            ),
        ]
        for requirement in requirements
    }


def _sqlite_index_names(engine: Engine, table_name: str) -> set[str]:
    """Return SQLite index names for a table without mutating schema."""
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA index_list('{table_name}')")).mappings()
        return {str(row["name"]) for row in rows}


def _storage_schema_version(engine: Engine) -> str | None:
    """Return the recorded agent workbench storage schema version if present."""
    inspector = inspect(engine)
    if "agent_workbench_schema_versions" not in inspector.get_table_names():
        return None

    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT version
                FROM agent_workbench_schema_versions
                WHERE component = 'agent_workbench'
                """
            )
        ).first()
    if row is None:
        return None
    return str(row._mapping["version"])
