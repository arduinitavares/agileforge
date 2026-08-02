"""Canonical audit contract for explicit Backlog authority reconciliation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.contracts import JsonObject

BACKLOG_RECONCILIATION_ACTION: Final = "backlog_authority_reconciled"


def reconciliation_audit_metadata(  # noqa: PLR0913
    *,
    reconciliation_id: int,
    reconciled_by: str,
    replacement_authority_id: int,
    replacement_authority_fingerprint: str,
    affected_artifact_ids: tuple[int, ...],
    affected_artifacts_fingerprint: str,
) -> JsonObject:
    """Return the exact canonical metadata for one reconciliation audit event."""
    return {
        "action": BACKLOG_RECONCILIATION_ACTION,
        "backlog_authority_reconciliation_id": reconciliation_id,
        "reconciled_by": reconciled_by,
        "replacement_authority_id": replacement_authority_id,
        "replacement_authority_fingerprint": replacement_authority_fingerprint,
        "affected_artifact_ids": list(affected_artifact_ids),
        "affected_artifacts_fingerprint": affected_artifacts_fingerprint,
    }


def reconciliation_audit_event_fingerprint(
    *,
    event_id: int,
    event_type: str,
    project_id: int,
    timestamp: datetime,
    metadata: JsonObject,
) -> str:
    """Bind one event identity and its complete canonical content."""
    return canonical_hash(
        {
            "event_id": event_id,
            "event_type": event_type,
            "project_id": project_id,
            "timestamp": timestamp,
            "metadata": metadata,
        }
    )


__all__ = [
    "BACKLOG_RECONCILIATION_ACTION",
    "reconciliation_audit_event_fingerprint",
    "reconciliation_audit_metadata",
]
