"""Read-only schema readiness checks for CLI projections."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.sql import text

from services.agent_workbench.version import STORAGE_SCHEMA_VERSION

TERMINAL_DECISION_INDEX = "uq_spec_authority_terminal_decision_key"
TERMINAL_DECISION_INDEX_PREDICATE = "terminal_decision_key IS NOT NULL"
AUTHORITY_CURATION_RUNNING_INDEX = "uq_authority_curation_running_authority"
AUTHORITY_CURATION_RUNNING_INDEX_PREDICATE = "status = 'running'"


@dataclass(frozen=True)
class SchemaRequirement:
    """Required table and columns for a projection."""

    table: str
    columns: Sequence[str]
    indexes: Sequence[str] = ()
    unique_columns: Sequence[Sequence[str]] = ()
    storage_schema_version: str | None = None

    def __post_init__(self) -> None:
        """Normalize sequence fields while rejecting bare strings."""
        if isinstance(self.columns, str):
            message = "columns must be a sequence of column names"
            raise TypeError(message)
        if isinstance(self.indexes, str):
            message = "indexes must be a sequence of index names"
            raise TypeError(message)
        if isinstance(self.unique_columns, str):
            message = "unique_columns must be a sequence of column-name sequences"
            raise TypeError(message)
        normalized_unique_columns: list[tuple[str, ...]] = []
        for unique_columns in self.unique_columns:
            if isinstance(unique_columns, str):
                message = "unique_columns entries must be sequences of column names"
                raise TypeError(message)
            normalized_unique_columns.append(tuple(unique_columns))
        object.__setattr__(self, "columns", tuple(self.columns))
        object.__setattr__(self, "indexes", tuple(self.indexes))
        object.__setattr__(
            self,
            "unique_columns",
            tuple(normalized_unique_columns),
        )


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
    "incomplete_review_overrides_json",
    "terminal_decision_key",
    "provenance_source",
)
AUTHORITY_DECISION_REQUIREMENTS: tuple[SchemaRequirement, ...] = (
    SchemaRequirement(
        table="spec_authority_acceptance",
        columns=AUTHORITY_DECISION_REQUIRED_COLUMNS,
        indexes=(TERMINAL_DECISION_INDEX,),
        storage_schema_version=STORAGE_SCHEMA_VERSION,
    ),
)

AUTHORITY_CURATION_REQUIREMENTS: tuple[SchemaRequirement, ...] = (
    SchemaRequirement(
        table="authority_feedback_attempts",
        columns=(
            "feedback_row_id",
            "project_id",
            "feedback_attempt_id",
            "source_authority_id",
            "source_authority_fingerprint",
            "feedback_fingerprint",
            "status",
            "has_blocking_feedback",
            "feedback_json",
            "request_hash",
            "idempotency_key",
            "changed_by",
            "created_at",
            "updated_at",
        ),
        indexes=(
            "ix_authority_feedback_project_status",
            "ix_authority_feedback_source_authority",
        ),
        unique_columns=(
            ("project_id", "feedback_attempt_id"),
            ("project_id", "idempotency_key"),
        ),
    ),
    SchemaRequirement(
        table="authority_curation_attempts",
        columns=(
            "curation_row_id",
            "project_id",
            "curation_attempt_id",
            "source_authority_id",
            "source_authority_fingerprint",
            "spec_version_id",
            "feedback_attempt_id",
            "status",
            "max_iterations",
            "iteration_count",
            "compiler_model",
            "candidate_authority_id",
            "candidate_authority_fingerprint",
            "request_json",
            "candidate_lineage_json",
            "diff_summary_json",
            "lineage_json",
            "quality_report_json",
            "failure_artifact_id",
            "request_hash",
            "idempotency_key",
            "changed_by",
            "created_at",
            "updated_at",
        ),
        indexes=(
            "ix_authority_curation_project_status",
            "ix_authority_curation_source_authority",
            AUTHORITY_CURATION_RUNNING_INDEX,
        ),
        unique_columns=(
            ("project_id", "curation_attempt_id"),
            ("project_id", "idempotency_key"),
        ),
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
            missing[requirement.table] = _missing_requirement(requirement)
            continue

        existing_columns = {
            column["name"] for column in inspector.get_columns(requirement.table)
        }
        missing_columns = [
            column for column in requirement.columns if column not in existing_columns
        ]
        missing_elements = list(missing_columns)

        if requirement.indexes:
            missing_elements.extend(
                index
                for index in requirement.indexes
                if not _index_contract_ready(engine, requirement.table, index)
            )

        if requirement.unique_columns:
            missing_elements.extend(
                _format_unique_columns(unique_columns)
                for unique_columns in requirement.unique_columns
                if not _unique_contract_ready(engine, requirement.table, unique_columns)
            )

        if requirement.storage_schema_version is not None:
            actual_version = _storage_schema_version(engine)
            if actual_version != requirement.storage_schema_version:
                missing_elements.append(
                    f"storage_schema_version:{requirement.storage_schema_version}"
                )

        if _authority_decision_terminal_data_checkable(
            requirement,
            existing_columns,
        ) and not _authority_decision_terminal_data_ready(engine):
            missing_elements.append("authority_decision_terminal_data")

        if missing_elements:
            missing[requirement.table] = missing_elements

    return SchemaReadiness(ok=not missing, missing=missing)


def check_authority_decision_readiness(engine: Engine) -> SchemaReadiness:
    """
    Return readiness for authority decision write-path storage.

    Authority decision write services must call this before using the guarded
    decision contract introduced in storage schema v3.
    """
    return check_schema_readiness(engine, AUTHORITY_DECISION_REQUIREMENTS)


def check_authority_curation_readiness(engine: Engine) -> SchemaReadiness:
    """Return readiness for authority feedback and curation storage."""
    return check_schema_readiness(engine, AUTHORITY_CURATION_REQUIREMENTS)


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
        requirement.table: _missing_requirement(requirement)
        for requirement in requirements
    }


def _missing_requirement(requirement: SchemaRequirement) -> list[str]:
    """Return all schema elements represented by a requirement."""
    return [
        *requirement.columns,
        *requirement.indexes,
        *(
            _format_unique_columns(unique_columns)
            for unique_columns in requirement.unique_columns
        ),
        *(
            [f"storage_schema_version:{requirement.storage_schema_version}"]
            if requirement.storage_schema_version is not None
            else []
        ),
    ]


def _index_contract_ready(engine: Engine, table_name: str, index_name: str) -> bool:
    """Return whether an index exists and satisfies any known contract."""
    if (
        table_name == "spec_authority_acceptance"
        and index_name == TERMINAL_DECISION_INDEX
    ):
        return _terminal_decision_index_ready(engine)
    if (
        table_name == "authority_curation_attempts"
        and index_name == AUTHORITY_CURATION_RUNNING_INDEX
    ):
        return _authority_curation_running_index_ready(engine)

    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA index_list('{table_name}')")).mappings()
        return any(str(row["name"]) == index_name for row in rows)


def _format_unique_columns(unique_columns: Sequence[str]) -> str:
    """Return the missing-element label for a required unique column set."""
    return f"unique({', '.join(unique_columns)})"


def _unique_contract_ready(
    engine: Engine,
    table_name: str,
    unique_columns: Sequence[str],
) -> bool:
    """Return whether the table enforces uniqueness for the exact column tuple."""
    inspector = inspect(engine)
    expected_columns = tuple(unique_columns)

    for constraint in inspector.get_unique_constraints(table_name):
        constrained_columns = tuple(constraint.get("column_names") or ())
        if constrained_columns == expected_columns:
            return True

    for index in inspector.get_indexes(table_name):
        if not index.get("unique"):
            continue
        indexed_columns = tuple(index.get("column_names") or ())
        if indexed_columns == expected_columns:
            return True

    return False


def _terminal_decision_index_ready(engine: Engine) -> bool:
    """Return whether the terminal decision index enforces the full invariant."""
    with engine.connect() as conn:
        index_rows = (
            conn.execute(text("PRAGMA index_list('spec_authority_acceptance')"))
            .mappings()
            .all()
        )
        index_row = next(
            (row for row in index_rows if row["name"] == TERMINAL_DECISION_INDEX),
            None,
        )
        if index_row is None:
            return False
        if int(index_row["unique"]) != 1 or int(index_row["partial"]) != 1:
            return False

        indexed_columns = [
            row["name"]
            for row in conn.execute(
                text(f"PRAGMA index_info('{TERMINAL_DECISION_INDEX}')")
            )
            .mappings()
            .all()
        ]
        if indexed_columns != ["terminal_decision_key"]:
            return False

        sql_row = conn.execute(
            text(
                """
                SELECT sql
                FROM sqlite_master
                WHERE type = 'index' AND name = :index_name
                """
            ),
            {"index_name": TERMINAL_DECISION_INDEX},
        ).first()
        index_sql = "" if sql_row is None else str(sql_row._mapping["sql"] or "")
        return _has_terminal_decision_partial_predicate(index_sql)


def _authority_curation_running_index_ready(engine: Engine) -> bool:
    """Return whether running curation rows are unique per authority."""
    with engine.connect() as conn:
        index_rows = (
            conn.execute(text("PRAGMA index_list('authority_curation_attempts')"))
            .mappings()
            .all()
        )
        index_row = next(
            (
                row
                for row in index_rows
                if row["name"] == AUTHORITY_CURATION_RUNNING_INDEX
            ),
            None,
        )
        if index_row is None:
            return False
        if int(index_row["unique"]) != 1 or int(index_row["partial"]) != 1:
            return False

        indexed_columns = [
            row["name"]
            for row in conn.execute(
                text(f"PRAGMA index_info('{AUTHORITY_CURATION_RUNNING_INDEX}')")
            )
            .mappings()
            .all()
        ]
        if indexed_columns != ["project_id", "source_authority_id"]:
            return False

        sql_row = conn.execute(
            text(
                """
                SELECT sql
                FROM sqlite_master
                WHERE type = 'index' AND name = :index_name
                """
            ),
            {"index_name": AUTHORITY_CURATION_RUNNING_INDEX},
        ).first()
        index_sql = "" if sql_row is None else str(sql_row._mapping["sql"] or "")
        return _has_authority_curation_running_partial_predicate(index_sql)


def _authority_decision_terminal_data_checkable(
    requirement: SchemaRequirement,
    existing_columns: set[str],
) -> bool:
    """Return whether terminal authority data can be validated for readiness."""
    required_columns = {
        "product_id",
        "spec_version_id",
        "status",
        "pending_authority_id",
        "terminal_decision_key",
    }
    return (
        requirement.table == "spec_authority_acceptance"
        and required_columns.issubset(existing_columns)
    )


def _authority_decision_terminal_data_ready(engine: Engine) -> bool:
    """Return whether terminal decision rows satisfy the storage invariant."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT 1
                FROM spec_authority_acceptance
                WHERE status IN ('accepted', 'rejected')
                  AND (
                    pending_authority_id IS NULL
                    OR terminal_decision_key IS NULL
                    OR terminal_decision_key != (
                        CAST(product_id AS TEXT)
                        || ':'
                        || CAST(spec_version_id AS TEXT)
                        || ':'
                        || CAST(pending_authority_id AS TEXT)
                    )
                  )
                LIMIT 1
                """
            )
        ).first()
    return row is None


def _has_terminal_decision_partial_predicate(index_sql: str) -> bool:
    """Return whether index SQL contains the canonical partial predicate."""
    normalized_sql = _normalize_index_sql(index_sql)
    _, separator, where_clause = normalized_sql.partition(" where ")
    if not separator:
        return False
    expected = _normalize_index_sql(TERMINAL_DECISION_INDEX_PREDICATE)
    return where_clause == expected


def _has_authority_curation_running_partial_predicate(index_sql: str) -> bool:
    """Return whether index SQL contains the running curation predicate."""
    normalized_sql = _normalize_index_sql(index_sql)
    _, separator, where_clause = normalized_sql.partition(" where ")
    if not separator:
        return False
    expected = _normalize_index_sql(AUTHORITY_CURATION_RUNNING_INDEX_PREDICATE)
    return where_clause == expected


def _normalize_index_sql(index_sql: str) -> str:
    """Normalize SQLite index SQL enough for canonical predicate comparison."""
    normalized = index_sql.lower()
    for token in ('"', "'", "`", "[", "]", "(", ")", ";"):
        normalized = normalized.replace(token, " ")
    return " ".join(normalized.split())


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
