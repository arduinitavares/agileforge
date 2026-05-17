"""
Database schema migration utilities.

This module provides idempotent migrations to ensure the runtime database
schema matches the SQLModel definitions. It is designed to run at app startup
and safely handle schema drift without data loss.

Design:
- All migrations are idempotent (safe to run multiple times).
- Migrations only ADD columns/tables, never DROP or modify existing data.
- Each migration logs its action for observability.
- Failures are raised as RuntimeError with clear messages.

Usage:
    from db.migrations import ensure_schema_current
    ensure_schema_current(engine)
"""

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from utils.task_metadata import canonical_task_metadata_json

logger = logging.getLogger(__name__)

AGENT_WORKBENCH_STORAGE_SCHEMA_VERSION = "3"


def _get_existing_tables(engine: Engine) -> set[str]:
    """Return set of table names that exist in the database."""
    inspector = inspect(engine)
    return set(inspector.get_table_names())


def _get_existing_columns(engine: Engine, table_name: str) -> set[str]:
    """Return set of column names for a table, or empty set if table doesn't exist."""
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return set()
    columns = inspector.get_columns(table_name)
    return {col["name"] for col in columns}


def _ensure_table_exists(engine: Engine, table_name: str, create_sql: str) -> bool:
    """
    Ensure a table exists, creating it if necessary.

    Returns True if the table was created, False if it already existed.
    """
    existing_tables = _get_existing_tables(engine)
    if table_name in existing_tables:
        return False

    logger.info(
        "db.migration.create_table",
        extra={"table_name": table_name},
    )
    with engine.begin() as conn:
        conn.execute(text(create_sql))
    return True


def _ensure_column_exists(
    engine: Engine,
    table_name: str,
    column_name: str,
    column_def: str,
) -> bool:
    """
    Ensure a column exists in a table, adding it if necessary.

    Returns True if the column was added, False if it already existed.
    """
    existing_columns = _get_existing_columns(engine, table_name)
    if column_name in existing_columns:
        return False

    alter_sql = f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}"
    logger.info(
        "db.migration.add_column",
        extra={"table_name": table_name, "column_name": column_name},
    )
    with engine.begin() as conn:
        conn.execute(text(alter_sql))
    return True


def _get_existing_indexes(engine: Engine, table_name: str) -> set[str]:
    """Return set of index names for a table, or empty set if table doesn't exist."""
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return set()
    indexes = inspector.get_indexes(table_name)
    return {name for idx in indexes if (name := idx.get("name")) is not None}


def _ensure_index_exists(
    engine: Engine,
    table_name: str,
    index_name: str,
    column_names: list[str],
) -> bool:
    """
    Ensure an index exists on a table, creating it if necessary.

    Returns True if the index was created, False if it already existed.
    """
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return False

    existing_indexes = inspector.get_indexes(table_name)
    existing_by_name = {idx["name"]: idx for idx in existing_indexes}
    if index_name in existing_by_name:
        return False

    # Strict mode: enforce canonical naming for equivalent indexes.
    requested_columns = tuple(column_names)
    conflicting_equivalent_indexes = []
    for idx in existing_indexes:
        idx_columns = tuple(idx.get("column_names") or [])
        if idx_columns == requested_columns:
            conflicting_equivalent_indexes.append(idx["name"])

    if conflicting_equivalent_indexes:
        conflicts = ", ".join(sorted(conflicting_equivalent_indexes))
        message = (
            "Non-canonical index detected for "
            f"{table_name}({', '.join(column_names)}): {conflicts}. "
            f"Expected canonical index name: {index_name}."
        )
        raise RuntimeError(message)

    columns_str = ", ".join(column_names)
    create_index_sql = f"CREATE INDEX {index_name} ON {table_name} ({columns_str})"
    logger.info(
        "db.migration.create_index",
        extra={"table_name": table_name, "index_name": index_name},
    )
    with engine.begin() as conn:
        conn.execute(text(create_index_sql))
    return True


# =============================================================================
# SPEC AUTHORITY TABLES MIGRATION
# =============================================================================

SPEC_REGISTRY_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS spec_registry (
    spec_version_id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    spec_hash VARCHAR NOT NULL,
    content TEXT,
    content_ref VARCHAR,
    status VARCHAR DEFAULT 'draft',
    created_at DATETIME NOT NULL,
    approved_at DATETIME,
    approved_by VARCHAR,
    approval_notes TEXT
)
"""

COMPILED_SPEC_AUTHORITY_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS compiled_spec_authority (
    authority_id INTEGER PRIMARY KEY,
    spec_version_id INTEGER NOT NULL UNIQUE REFERENCES spec_registry(spec_version_id),
    compiler_version VARCHAR NOT NULL,
    prompt_hash VARCHAR NOT NULL,
    compiled_at DATETIME NOT NULL,
    compiled_artifact_json TEXT,
    scope_themes TEXT NOT NULL,
    invariants TEXT NOT NULL,
    eligible_feature_ids TEXT NOT NULL,
    rejected_features TEXT,
    spec_gaps TEXT
)
"""

SPEC_AUTHORITY_ACCEPTANCE_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS spec_authority_acceptance (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    spec_version_id INTEGER NOT NULL REFERENCES spec_registry(spec_version_id),
    status VARCHAR NOT NULL,
    policy VARCHAR NOT NULL,
    decided_by VARCHAR NOT NULL,
    decided_at DATETIME NOT NULL,
    rationale TEXT,
    compiler_version VARCHAR NOT NULL,
    prompt_hash VARCHAR NOT NULL,
    spec_hash VARCHAR NOT NULL,
    pending_authority_id INTEGER,
    authority_fingerprint VARCHAR,
    review_token VARCHAR,
    review_fingerprint VARCHAR,
    disk_spec_hash VARCHAR,
    resolved_spec_path VARCHAR,
    actor_mode VARCHAR,
    review_completeness VARCHAR,
    incomplete_review_override BOOLEAN NOT NULL DEFAULT 0,
    incomplete_review_rationale VARCHAR,
    terminal_decision_key VARCHAR,
    provenance_source VARCHAR NOT NULL DEFAULT 'normal'
)
"""

SPEC_AUTHORITY_ACCEPTANCE_PROVENANCE_COLUMNS: dict[str, str] = {
    "pending_authority_id": "INTEGER",
    "authority_fingerprint": "VARCHAR",
    "review_token": "VARCHAR",
    "review_fingerprint": "VARCHAR",
    "disk_spec_hash": "VARCHAR",
    "resolved_spec_path": "VARCHAR",
    "actor_mode": "VARCHAR",
    "review_completeness": "VARCHAR",
    "incomplete_review_override": "BOOLEAN NOT NULL DEFAULT 0",
    "incomplete_review_rationale": "VARCHAR",
    "terminal_decision_key": "VARCHAR",
    "provenance_source": "VARCHAR NOT NULL DEFAULT 'normal'",
}

SPEC_AUTHORITY_ACCEPTANCE_INDEXES: dict[str, list[str]] = {
    "ix_spec_authority_acceptance_pending_authority_id": ["pending_authority_id"],
    "ix_spec_authority_acceptance_authority_fingerprint": ["authority_fingerprint"],
    "ix_spec_authority_acceptance_review_token": ["review_token"],
}

SPEC_AUTHORITY_TERMINAL_DECISION_INDEX = "uq_spec_authority_terminal_decision_key"
SPEC_AUTHORITY_TERMINAL_DECISION_INDEX_PREDICATE = "terminal_decision_key IS NOT NULL"


def migrate_spec_authority_tables(engine: Engine) -> list[str]:
    """
    Ensure all spec authority tables exist with required columns.

    Returns list of applied migration actions.
    """
    actions: list[str] = []

    # 1. Ensure spec_registry table exists
    if _ensure_table_exists(engine, "spec_registry", SPEC_REGISTRY_CREATE_SQL):
        actions.append("created table: spec_registry")

    # 2. Ensure compiled_spec_authority table exists
    if _ensure_table_exists(
        engine, "compiled_spec_authority", COMPILED_SPEC_AUTHORITY_CREATE_SQL
    ):
        actions.append("created table: compiled_spec_authority")
    # Table exists — ensure compiled_artifact_json column exists
    elif _ensure_column_exists(
        engine,
        "compiled_spec_authority",
        "compiled_artifact_json",
        "TEXT",
    ):
        actions.append("added column: compiled_spec_authority.compiled_artifact_json")

    # 3. Ensure spec_authority_acceptance table exists
    if _ensure_table_exists(
        engine, "spec_authority_acceptance", SPEC_AUTHORITY_ACCEPTANCE_CREATE_SQL
    ):
        actions.append("created table: spec_authority_acceptance")

    actions.extend(_migrate_spec_authority_acceptance_contract(engine))

    return actions


def _migrate_spec_authority_acceptance_contract(engine: Engine) -> list[str]:
    """Ensure authority decision provenance columns, indexes, and backfill exist."""
    actions: list[str] = []

    for column_name, column_def in SPEC_AUTHORITY_ACCEPTANCE_PROVENANCE_COLUMNS.items():
        if _ensure_column_exists(
            engine,
            "spec_authority_acceptance",
            column_name,
            column_def,
        ):
            actions.append(f"added column: spec_authority_acceptance.{column_name}")

    backfilled_rows = _backfill_legacy_authority_decisions(engine)
    if backfilled_rows:
        actions.append(
            f"backfilled spec_authority_acceptance legacy decisions: {backfilled_rows}"
        )

    for index_name, columns in SPEC_AUTHORITY_ACCEPTANCE_INDEXES.items():
        if _ensure_index_exists(
            engine,
            "spec_authority_acceptance",
            index_name,
            columns,
        ):
            actions.append(f"created index: {index_name}")

    if _ensure_terminal_decision_unique_index(engine):
        actions.append(f"created index: {SPEC_AUTHORITY_TERMINAL_DECISION_INDEX}")

    return actions


def _backfill_legacy_authority_decisions(engine: Engine) -> int:
    """Backfill terminal authority provenance for legacy accepted/rejected rows."""
    with engine.begin() as conn:
        legacy_rows = (
            conn.execute(
                text(
                    """
                SELECT id, product_id, spec_version_id
                FROM spec_authority_acceptance
                WHERE status IN ('accepted', 'rejected')
                  AND pending_authority_id IS NULL
                """
                )
            )
            .mappings()
            .all()
        )

        for row in legacy_rows:
            authority_rows = (
                conn.execute(
                    text(
                        """
                    SELECT authority_id
                    FROM compiled_spec_authority
                    WHERE spec_version_id = :spec_version_id
                    ORDER BY authority_id
                    """
                    ),
                    {"spec_version_id": row["spec_version_id"]},
                )
                .mappings()
                .all()
            )
            if len(authority_rows) != 1:
                count = len(authority_rows)
                message = (
                    "Ambiguous legacy authority decision cannot be backfilled: "
                    f"acceptance_id={row['id']} product_id={row['product_id']} "
                    f"spec_version_id={row['spec_version_id']} matched {count} "
                    "compiled_spec_authority rows. Remediation: resolve the "
                    "legacy compiled authority so exactly one row matches this "
                    "spec version, then rerun migrations."
                )
                raise RuntimeError(message)

            authority_id = authority_rows[0]["authority_id"]
            terminal_decision_key = (
                f"{row['product_id']}:{row['spec_version_id']}:{authority_id}"
            )
            conn.execute(
                text(
                    """
                    UPDATE spec_authority_acceptance
                    SET pending_authority_id = :authority_id,
                        terminal_decision_key = :terminal_decision_key,
                        provenance_source = 'legacy_backfill'
                    WHERE id = :acceptance_id
                    """
                ),
                {
                    "authority_id": authority_id,
                    "terminal_decision_key": terminal_decision_key,
                    "acceptance_id": row["id"],
                },
            )

    return len(legacy_rows)


def _ensure_terminal_decision_unique_index(engine: Engine) -> bool:
    """Ensure terminal decision uniqueness using a partial SQLite unique index."""
    existing_indexes = _get_existing_indexes(engine, "spec_authority_acceptance")
    if SPEC_AUTHORITY_TERMINAL_DECISION_INDEX in existing_indexes:
        is_valid, reasons = _terminal_decision_index_contract(engine)
        if not is_valid:
            reason_text = ", ".join(reasons)
            message = (
                "Malformed terminal decision index detected: "
                f"index_name={SPEC_AUTHORITY_TERMINAL_DECISION_INDEX} "
                "table_name=spec_authority_acceptance "
                f"reasons=[{reason_text}]. Remediation: drop the malformed "
                f"index `{SPEC_AUTHORITY_TERMINAL_DECISION_INDEX}` and rerun "
                "database migrations so the canonical partial unique index can "
                "be created."
            )
            raise RuntimeError(message)
        return False

    create_index_sql = f"""
    CREATE UNIQUE INDEX IF NOT EXISTS {SPEC_AUTHORITY_TERMINAL_DECISION_INDEX}
    ON spec_authority_acceptance (terminal_decision_key)
    WHERE {SPEC_AUTHORITY_TERMINAL_DECISION_INDEX_PREDICATE}
    """
    logger.info(
        "db.migration.create_index",
        extra={
            "table_name": "spec_authority_acceptance",
            "index_name": SPEC_AUTHORITY_TERMINAL_DECISION_INDEX,
        },
    )
    with engine.begin() as conn:
        conn.execute(text(create_index_sql))
    return True


def _terminal_decision_index_contract(engine: Engine) -> tuple[bool, list[str]]:
    """Validate the canonical terminal decision unique-index contract."""
    reasons: list[str] = []
    with engine.connect() as conn:
        index_rows = (
            conn.execute(text("PRAGMA index_list('spec_authority_acceptance')"))
            .mappings()
            .all()
        )
        index_row = next(
            (
                row
                for row in index_rows
                if row["name"] == SPEC_AUTHORITY_TERMINAL_DECISION_INDEX
            ),
            None,
        )
        if index_row is None:
            return False, ["index is missing"]
        if int(index_row["unique"]) != 1:
            reasons.append("index is not unique")
        if int(index_row["partial"]) != 1:
            reasons.append("index is not partial")

        indexed_columns = [
            row["name"]
            for row in conn.execute(
                text(f"PRAGMA index_info('{SPEC_AUTHORITY_TERMINAL_DECISION_INDEX}')")
            )
            .mappings()
            .all()
        ]
        if indexed_columns != ["terminal_decision_key"]:
            reasons.append("index columns are not exactly [terminal_decision_key]")

        sql_row = conn.execute(
            text(
                """
                SELECT sql
                FROM sqlite_master
                WHERE type = 'index' AND name = :index_name
                """
            ),
            {"index_name": SPEC_AUTHORITY_TERMINAL_DECISION_INDEX},
        ).first()
        index_sql = "" if sql_row is None else str(sql_row._mapping["sql"] or "")
        if not _has_terminal_decision_partial_predicate(index_sql):
            reasons.append("index predicate is not terminal_decision_key IS NOT NULL")

    return not reasons, reasons


def _has_terminal_decision_partial_predicate(index_sql: str) -> bool:
    """Return whether index SQL contains the canonical partial predicate."""
    normalized_sql = _normalize_index_sql(index_sql)
    _, separator, where_clause = normalized_sql.partition(" where ")
    if not separator:
        return False
    expected = _normalize_index_sql(SPEC_AUTHORITY_TERMINAL_DECISION_INDEX_PREDICATE)
    return where_clause == expected


def _normalize_index_sql(index_sql: str) -> str:
    """Normalize SQLite index SQL enough for canonical predicate comparison."""
    normalized = index_sql.lower()
    for token in ('"', "'", "`", "[", "]", "(", ")", ";"):
        normalized = normalized.replace(token, " ")
    return " ".join(normalized.split())


def migrate_product_spec_cache(engine: Engine) -> list[str]:
    """Ensure product spec cache columns exist on products table."""
    actions: list[str] = []

    if _ensure_column_exists(
        engine,
        "products",
        "compiled_authority_json",
        "TEXT",
    ):
        actions.append("added column: products.compiled_authority_json")

    return actions


def migrate_performance_indexes(engine: Engine) -> list[str]:
    """Ensure performance indexes exist."""
    actions: list[str] = []

    # Optimization: Index on UserStory.product_id for faster filtering
    if _ensure_index_exists(
        engine,
        "user_stories",
        "ix_user_stories_product_id",
        ["product_id"],
    ):
        actions.append("created index: ix_user_stories_product_id")

    existing_columns = _get_existing_columns(engine, "user_stories")
    linkage_columns = {
        "product_id",
        "source_requirement",
        "refinement_slot",
        "is_superseded",
    }
    if linkage_columns.issubset(existing_columns) and _ensure_index_exists(
        engine,
        "user_stories",
        "ix_user_stories_refinement_linkage",
        ["product_id", "source_requirement", "refinement_slot", "is_superseded"],
    ):
        actions.append("created index: ix_user_stories_refinement_linkage")

    return actions


# =============================================================================
# USER STORY REFINEMENT LINKAGE MIGRATION
# =============================================================================


def migrate_user_story_refinement_linkage(engine: Engine) -> list[str]:
    """Ensure refinement linkage columns exist and defaults are backfilled."""
    actions: list[str] = []

    if _ensure_column_exists(
        engine,
        "user_stories",
        "source_requirement",
        "VARCHAR",
    ):
        actions.append("added column: user_stories.source_requirement")

    if _ensure_column_exists(
        engine,
        "user_stories",
        "refinement_slot",
        "INTEGER",
    ):
        actions.append("added column: user_stories.refinement_slot")

    if _ensure_column_exists(
        engine,
        "user_stories",
        "story_origin",
        "VARCHAR",
    ):
        actions.append("added column: user_stories.story_origin")

    if _ensure_column_exists(
        engine,
        "user_stories",
        "is_refined",
        "BOOLEAN DEFAULT 0",
    ):
        actions.append("added column: user_stories.is_refined")

    if _ensure_column_exists(
        engine,
        "user_stories",
        "is_superseded",
        "BOOLEAN DEFAULT 0",
    ):
        actions.append("added column: user_stories.is_superseded")

    if _ensure_column_exists(
        engine,
        "user_stories",
        "superseded_by_story_id",
        "INTEGER",
    ):
        actions.append("added column: user_stories.superseded_by_story_id")

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE user_stories
                SET is_refined = 0
                WHERE is_refined IS NULL
                """
            )
        )
        conn.execute(
            text(
                """
                UPDATE user_stories
                SET is_superseded = 0
                WHERE is_superseded IS NULL
                """
            )
        )

    return actions


# =============================================================================
# SPRINT LIFECYCLE MIGRATION
# =============================================================================


def migrate_sprint_lifecycle(engine: Engine) -> list[str]:
    """Ensure sprint lifecycle columns exist."""
    actions: list[str] = []

    if _ensure_column_exists(
        engine,
        "sprints",
        "started_at",
        "DATETIME",
    ):
        actions.append("added column: sprints.started_at")

    if _ensure_column_exists(
        engine,
        "sprints",
        "completed_at",
        "DATETIME",
    ):
        actions.append("added column: sprints.completed_at")

    if _ensure_column_exists(
        engine,
        "sprints",
        "close_snapshot_json",
        "TEXT",
    ):
        actions.append("added column: sprints.close_snapshot_json")

    return actions


def migrate_task_metadata(engine: Engine) -> list[str]:
    """Ensure persisted task metadata exists and legacy rows are backfilled."""
    actions: list[str] = []

    if _ensure_column_exists(
        engine,
        "tasks",
        "metadata_json",
        "TEXT",
    ):
        actions.append("added column: tasks.metadata_json")

    metadata_json = canonical_task_metadata_json()
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                UPDATE tasks
                SET metadata_json = :metadata_json,
                    updated_at = CURRENT_TIMESTAMP
                WHERE metadata_json IS NULL OR TRIM(metadata_json) = ''
                """
            ),
            {"metadata_json": metadata_json},
        )
    if result.rowcount and result.rowcount > 0:
        actions.append(f"backfilled tasks.metadata_json rows: {result.rowcount}")

    return actions


# =============================================================================
# TASK EXECUTION MIGRATION
# =============================================================================

TASK_EXECUTION_LOGS_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS task_execution_logs (
    log_id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(task_id),
    sprint_id INTEGER NOT NULL REFERENCES sprints(sprint_id),
    old_status VARCHAR,
    new_status VARCHAR NOT NULL,
    outcome_summary TEXT,
    artifact_refs_json TEXT,
    acceptance_result VARCHAR NOT NULL,
    notes TEXT,
    changed_by VARCHAR NOT NULL,
    changed_at DATETIME NOT NULL
)
"""


def migrate_task_execution_logs(engine: Engine) -> list[str]:
    """Ensure task_execution_logs table exists."""
    actions: list[str] = []

    if _ensure_table_exists(
        engine, "task_execution_logs", TASK_EXECUTION_LOGS_CREATE_SQL
    ):
        actions.append("created table: task_execution_logs")

    if _ensure_index_exists(
        engine,
        "task_execution_logs",
        "ix_task_execution_logs_task_id",
        ["task_id"],
    ):
        actions.append("created index: ix_task_execution_logs_task_id")

    if _ensure_index_exists(
        engine,
        "task_execution_logs",
        "ix_task_execution_logs_sprint_id",
        ["sprint_id"],
    ):
        actions.append("created index: ix_task_execution_logs_sprint_id")

    return actions


# =============================================================================
# AGENT WORKBENCH CONTRACT TABLES MIGRATION
# =============================================================================


AGENT_WORKBENCH_SCHEMA_VERSIONS_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS agent_workbench_schema_versions (
    component TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

CLI_MUTATION_LEDGER_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS cli_mutation_ledger (
    mutation_event_id INTEGER PRIMARY KEY,
    command VARCHAR NOT NULL,
    idempotency_key VARCHAR NOT NULL,
    request_hash VARCHAR NOT NULL,
    project_id INTEGER,
    correlation_id VARCHAR NOT NULL,
    changed_by VARCHAR NOT NULL DEFAULT 'cli-agent',
    status VARCHAR NOT NULL,
    current_step VARCHAR NOT NULL DEFAULT 'start',
    completed_steps_json TEXT NOT NULL DEFAULT '[]',
    guard_inputs_json TEXT NOT NULL DEFAULT '{}',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT,
    response_json TEXT,
    recovers_mutation_event_id INTEGER,
    superseded_by_mutation_event_id INTEGER,
    recovery_action VARCHAR NOT NULL DEFAULT 'none',
    recovery_safe_to_auto_resume BOOLEAN NOT NULL DEFAULT 0,
    lease_owner VARCHAR,
    lease_acquired_at TIMESTAMP,
    last_heartbeat_at TIMESTAMP,
    lease_expires_at TIMESTAMP,
    last_error_json TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_cli_mutation_command_idempotency
        UNIQUE (command, idempotency_key)
)
"""


def migrate_agent_workbench_contract_tables(engine: Engine) -> list[str]:
    """Ensure CLI contract hardening persistence tables exist."""
    actions: list[str] = []

    if _ensure_table_exists(
        engine,
        "agent_workbench_schema_versions",
        AGENT_WORKBENCH_SCHEMA_VERSIONS_CREATE_SQL,
    ):
        actions.append("created table: agent_workbench_schema_versions")

    if _ensure_table_exists(
        engine,
        "cli_mutation_ledger",
        CLI_MUTATION_LEDGER_CREATE_SQL,
    ):
        actions.append("created table: cli_mutation_ledger")

    if _ensure_column_exists(
        engine,
        "cli_mutation_ledger",
        "recovers_mutation_event_id",
        "INTEGER",
    ):
        actions.append("added column: cli_mutation_ledger.recovers_mutation_event_id")

    if _ensure_column_exists(
        engine,
        "cli_mutation_ledger",
        "superseded_by_mutation_event_id",
        "INTEGER",
    ):
        actions.append(
            "added column: cli_mutation_ledger.superseded_by_mutation_event_id"
        )

    for index_name, columns in {
        "ix_cli_mutation_ledger_status": ["status"],
        "ix_cli_mutation_ledger_project_id": ["project_id"],
        "ix_cli_mutation_ledger_request_hash": ["request_hash"],
        "ix_cli_mutation_ledger_lease_owner": ["lease_owner"],
        "ix_cli_mutation_ledger_recovers_mutation_event_id": [
            "recovers_mutation_event_id"
        ],
        "ix_cli_mutation_ledger_superseded_by_mutation_event_id": [
            "superseded_by_mutation_event_id"
        ],
    }.items():
        if _ensure_index_exists(engine, "cli_mutation_ledger", index_name, columns):
            actions.append(f"created index: {index_name}")

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO agent_workbench_schema_versions(component, version)
                VALUES ('agent_workbench', :version)
                ON CONFLICT(component) DO UPDATE SET
                    version = excluded.version,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            {"version": AGENT_WORKBENCH_STORAGE_SCHEMA_VERSION},
        )

    return actions


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================


def ensure_schema_current(engine: Engine) -> None:
    """
    Run all idempotent migrations to ensure schema is current.

    This function is safe to call at every app startup. It will:
    - Create missing tables
    - Add missing columns to existing tables
    - Log all actions taken
    - Skip migrations that are already applied

    Raises:
        RuntimeError: If a migration fails (e.g., SQL error)
    """
    logger.info("db.migration.start", extra={})

    try:
        actions = migrate_spec_authority_tables(engine)
        actions.extend(migrate_product_spec_cache(engine))
        actions.extend(migrate_user_story_refinement_linkage(engine))
        actions.extend(migrate_sprint_lifecycle(engine))
        actions.extend(migrate_task_metadata(engine))
        actions.extend(migrate_task_execution_logs(engine))
        actions.extend(migrate_agent_workbench_contract_tables(engine))
        actions.extend(migrate_performance_indexes(engine))

        if actions:
            for action in actions:
                logger.info(
                    "db.migration.applied",
                    extra={"action": action},
                )
            logger.info(
                "db.migration.complete",
                extra={"actions_count": len(actions)},
            )
        else:
            logger.info("db.migration.skip", extra={"reason": "schema_current"})

    except Exception as exc:
        logger.exception(
            "db.migration.fail",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        message = (
            f"Database migration failed: {exc}. "
            "If this persists, consider deleting the database file and restarting."
        )
        raise RuntimeError(message) from exc
