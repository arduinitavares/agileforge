# models/provider_audit_indexes.py
"""Trusted SQLite expressions shared by provider audit indexes and lookups."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from sqlalchemy import Index, String, literal_column, text

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from sqlalchemy.sql.elements import ColumnElement, TextClause

PROVIDER_EVENT_PREDICATE: str = (
    "event_type IN ('PROVIDER_TRY_STARTED', 'PROVIDER_TRY_FINISHED')"
)
_SAFE_METADATA: str = (
    "CASE WHEN json_valid(event_metadata) = 1 THEN event_metadata ELSE '{}' END"
)
PROVIDER_ACTION_EXPRESSION: str = f"json_extract({_SAFE_METADATA}, '$.action_id')"
PROVIDER_CALL_EXPRESSION: str = f"json_extract({_SAFE_METADATA}, '$.call_id')"
PROVIDER_INVALID_PREDICATE: str = (
    f"{PROVIDER_EVENT_PREDICATE} AND coalesce(json_valid(event_metadata), 0) != 1"
)
PROVIDER_AUDIT_INDEX_NAMES: tuple[str, ...] = (
    "ix_workflow_events_provider_action",
    "ix_workflow_events_provider_call",
    "ix_workflow_events_provider_invalid",
)
PROVIDER_AUDIT_INDEX_DDL: dict[str, str] = {
    PROVIDER_AUDIT_INDEX_NAMES[0]: (
        f"CREATE INDEX {PROVIDER_AUDIT_INDEX_NAMES[0]} ON workflow_events "
        f"({PROVIDER_ACTION_EXPRESSION}, project_id, event_id) "
        f"WHERE {PROVIDER_EVENT_PREDICATE}"
    ),
    PROVIDER_AUDIT_INDEX_NAMES[1]: (
        f"CREATE INDEX {PROVIDER_AUDIT_INDEX_NAMES[1]} ON workflow_events "
        f"({PROVIDER_CALL_EXPRESSION}, project_id, event_id) "
        f"WHERE {PROVIDER_EVENT_PREDICATE}"
    ),
    PROVIDER_AUDIT_INDEX_NAMES[2]: (
        f"CREATE INDEX {PROVIDER_AUDIT_INDEX_NAMES[2]} ON workflow_events "
        f"(project_id, event_id) WHERE {PROVIDER_INVALID_PREDICATE}"
    ),
}


class ProviderAuditIndexError(ValueError):
    """The narrow provider-index contract has unsupported provenance or drift."""


def provider_audit_indexes_present(connection: Connection) -> bool:
    """Recognize none or the exact complete set; reject partial or changed DDL."""
    rows = connection.exec_driver_sql(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE lower(name) GLOB ? LIMIT 4",
        ("ix_workflow_events_provider_*",),
    ).all()
    if not rows:
        return False
    if len(rows) != len(PROVIDER_AUDIT_INDEX_NAMES) or {row[1] for row in rows} != set(
        PROVIDER_AUDIT_INDEX_NAMES
    ):
        raise ProviderAuditIndexError
    for object_type, name, table_name, sql in rows:
        # Preserve SQL literal case and whitespace: paths/enum names are identities.
        if (
            object_type != "index"
            or table_name != "workflow_events"
            or sql != PROVIDER_AUDIT_INDEX_DDL[name]
        ):
            raise ProviderAuditIndexError
        keys = connection.exec_driver_sql(f"PRAGMA index_xinfo({name})").all()
        expected_columns = (
            ("project_id", "event_id")
            if name == PROVIDER_AUDIT_INDEX_NAMES[2]
            else (None, "project_id", "event_id")
        )
        if tuple(row[2] for row in keys if row[5]) != expected_columns or any(
            row[3] != 0 or row[4] != "BINARY" for row in keys if row[5]
        ):
            raise ProviderAuditIndexError
    return True


def provider_event_predicate() -> TextClause:
    """Literalize only the two fixed, host-owned provider event names."""
    return text(PROVIDER_EVENT_PREDICATE)


def provider_identity(field: Literal["action_id", "call_id"]) -> ColumnElement[str]:
    """Use an exact indexed expression; incoming identity values stay bound."""
    expression = (
        PROVIDER_ACTION_EXPRESSION if field == "action_id" else PROVIDER_CALL_EXPRESSION
    )
    return literal_column(expression, String())


def provider_invalid_predicate() -> TextClause:
    """Select damaged JSON safely, including SQL NULL metadata."""
    return text(PROVIDER_INVALID_PREDICATE)


def provider_audit_indexes() -> tuple[Index, ...]:
    """Declare the three nonunique indexes on the existing event table."""
    return (
        Index(
            PROVIDER_AUDIT_INDEX_NAMES[0],
            provider_identity("action_id"),
            "project_id",
            "event_id",
            sqlite_where=provider_event_predicate(),
        ),
        Index(
            PROVIDER_AUDIT_INDEX_NAMES[1],
            provider_identity("call_id"),
            "project_id",
            "event_id",
            sqlite_where=provider_event_predicate(),
        ),
        Index(
            PROVIDER_AUDIT_INDEX_NAMES[2],
            "project_id",
            "event_id",
            sqlite_where=provider_invalid_predicate(),
        ),
    )
