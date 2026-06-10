# services/phases/authority_guard.py
"""Authority guards shared by API and CLI phase generation."""

from __future__ import annotations

from typing import Any, Final

from services.agent_workbench.authority_projection import AuthorityProjectionService
from services.agent_workbench.context_pack import AUTHORITY_BLOCKING_STATUSES
from services.agent_workbench.error_codes import ErrorCode, workbench_error

JsonDict = dict[str, Any]

PHASE_AUTHORITY_BLOCKING_STATUSES: Final[frozenset[str]] = frozenset(
    AUTHORITY_BLOCKING_STATUSES | {"unsupported_schema", "rejected"}
)

_STATUS_ERROR_CODE: Final[dict[str, ErrorCode]] = {
    "missing": ErrorCode.AUTHORITY_NOT_ACCEPTED,
    "not_compiled": ErrorCode.AUTHORITY_NOT_COMPILED,
    "pending_acceptance": ErrorCode.AUTHORITY_REVIEW_REQUIRED,
    "stale": ErrorCode.STALE_AUTHORITY_VERSION,
    "rejected": ErrorCode.AUTHORITY_REVIEW_REQUIRED,
    "unsupported_schema": ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED,
}


def phase_authority_block_error(*, project_id: int) -> JsonDict | None:
    """Return a workbench error dict when phase generation must be blocked."""
    projection = AuthorityProjectionService()
    result = projection.status(project_id=project_id)
    if not result.get("ok"):
        errors = result.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return first
        return None

    data = result.get("data")
    if not isinstance(data, dict):
        return None

    status = str(data.get("status") or "").strip().lower()
    if status not in PHASE_AUTHORITY_BLOCKING_STATUSES:
        return None

    code = _STATUS_ERROR_CODE.get(status, ErrorCode.AUTHORITY_NOT_ACCEPTED)
    return workbench_error(
        code,
        message=_blocking_message(status=status, data=data),
        details={
            "project_id": project_id,
            "authority_status": data.get("status"),
            "reason": data.get("reason"),
            "stale_reason": data.get("stale_reason"),
        },
        remediation=_blocking_remediation(
            status=status,
            project_id=project_id,
            data=data,
        ),
    ).to_dict()


def structured_workbench_error_envelope(error: object) -> JsonDict | None:
    """Return a failure envelope when *error* is a structured workbench error dict."""
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return {
            "ok": False,
            "data": None,
            "warnings": [],
            "errors": [error],
        }
    return None


def sync_compiled_authority_cache(
    *,
    state: JsonDict,
    product_authority_json: object,
) -> bool:
    """Sync workflow session authority cache from the product row."""
    if not isinstance(product_authority_json, str) or not product_authority_json:
        return False
    if state.get("compiled_authority_cached") == product_authority_json:
        return False
    state["compiled_authority_cached"] = product_authority_json
    return True


def _blocking_message(*, status: str, data: JsonDict) -> str:
    """Return a user-facing block message for the authority status."""
    if status == "stale":
        stale_reason = data.get("stale_reason") or data.get("reason") or "stale"
        return (
            "Accepted authority is stale and must be reviewed before phase generation."
            f" ({stale_reason})"
        )
    if status == "pending_acceptance":
        return (
            "Compiled authority is pending acceptance and must be reviewed before "
            "phase generation."
        )
    if status == "rejected":
        return (
            "Authority review was rejected. Regenerate or update authority before "
            "phase generation."
        )
    if status == "unsupported_schema":
        return "Compiled authority artifact schema is unsupported."
    if status == "not_compiled":
        return "Specification authority is not compiled for the active spec version."
    return "Specification authority is not accepted for phase generation."


def _blocking_remediation(
    *,
    status: str,
    project_id: int,
    data: JsonDict,
) -> list[str]:
    """Return remediation steps for a blocked authority status."""
    spec_version_id = data.get("latest_spec_version_id") or data.get("spec_version_id")
    review_command = (
        f"Run agileforge authority review --project-id {project_id} --open."
    )
    if status == "unsupported_schema":
        if spec_version_id is not None:
            return [
                (
                    "Run agileforge authority regenerate "
                    f"--project-id {project_id} "
                    f"--spec-version-id {spec_version_id} "
                    "--idempotency-key <new-key>."
                )
            ]
        return [
            (
                "Run agileforge authority regenerate "
                f"--project-id {project_id} "
                "--spec-version-id <spec-version-id> "
                "--idempotency-key <new-key>."
            )
        ]
    if status in {"stale", "pending_acceptance"}:
        return [review_command, "Accept reviewed authority before continuing."]
    if status == "rejected":
        return [
            (
                "Update the specification or regenerate authority, then complete "
                "authority review."
            )
        ]
    if status == "not_compiled":
        return ["Run setup retry or compile authority for the active spec version."]
    return [review_command]
