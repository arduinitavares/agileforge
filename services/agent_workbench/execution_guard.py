"""Shared execution guards for lower-level phase commands."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlmodel import Session, select

from models.agent_workbench import (
    DiscoveryChallengeArtifact,
    DiscoveryPrd,
    DiscoverySpecAmendmentDraft,
)
from services.agent_workbench.authority_projection import AuthorityProjectionService
from services.agent_workbench.error_codes import ErrorCode, workbench_error

JsonDict = dict[str, Any]

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_MESSAGE: str = (
    "Executable work requires Accepted Authority. Discovery artifacts such as "
    "Challenge Artifacts, PRDs, and Spec Amendment Drafts are not executable "
    "until the resulting authority has been accepted."
)
_REMEDIATION: list[str] = [
    "Complete Scope Discovery and compile the resulting authority.",
    (
        "Accept the authority as Accepted Authority before running backlog, "
        "roadmap, story, task, or sprint commands."
    ),
]


class AcceptedAuthorityExecutionGuard:
    """Reject executable phase commands until authority is current and accepted."""

    def __init__(self, *, engine: Engine) -> None:
        """Initialize the guard with the current database engine."""
        self._engine = engine

    def reject_unless_current(self, *, project_id: int) -> JsonDict | None:
        """Return an error envelope when the project lacks accepted authority."""
        status = AuthorityProjectionService(engine=self._engine).status(
            project_id=project_id
        )
        if not status.get("ok"):
            return status
        data = status.get("data")
        authority_status = data if isinstance(data, dict) else {}
        if (
            authority_status.get("status") == "current"
            and authority_status.get("accepted_decision_id") is not None
        ):
            return None
        return {
            "ok": False,
            "data": None,
            "warnings": [],
            "errors": [
                workbench_error(
                    ErrorCode.AUTHORITY_NOT_ACCEPTED,
                    message=_MESSAGE,
                    details={
                        "project_id": project_id,
                        "authority_status": authority_status.get("status"),
                        "authority_reason": authority_status.get("reason"),
                        "latest_spec_version_id": authority_status.get(
                            "latest_spec_version_id"
                        ),
                        "accepted_spec_version_id": authority_status.get(
                            "accepted_spec_version_id"
                        ),
                        "pending_authority_id": authority_status.get(
                            "pending_authority_id"
                        ),
                        "scope_discovery": self._scope_discovery_state(project_id),
                    },
                    remediation=_REMEDIATION,
                ).to_dict()
            ],
        }

    def _scope_discovery_state(self, project_id: int) -> JsonDict:
        """Return latest discovery status metadata for error diagnostics."""
        with Session(self._engine) as session:
            challenge = session.exec(
                select(DiscoveryChallengeArtifact)
                .where(DiscoveryChallengeArtifact.project_id == project_id)
                .order_by(
                    cast("Any", DiscoveryChallengeArtifact.challenge_artifact_id).desc()
                )
            ).first()
            prd = session.exec(
                select(DiscoveryPrd)
                .where(DiscoveryPrd.project_id == project_id)
                .order_by(cast("Any", DiscoveryPrd.prd_id).desc())
            ).first()
            amendment = session.exec(
                select(DiscoverySpecAmendmentDraft)
                .where(DiscoverySpecAmendmentDraft.project_id == project_id)
                .order_by(
                    cast(
                        "Any",
                        DiscoverySpecAmendmentDraft.spec_amendment_draft_id,
                    ).desc()
                )
            ).first()
        return {
            "challenge_artifact_id": (
                challenge.challenge_artifact_id if challenge is not None else None
            ),
            "challenge_readiness": (
                challenge.readiness if challenge is not None else None
            ),
            "prd_id": prd.prd_id if prd is not None else None,
            "prd_status": prd.status if prd is not None else None,
            "spec_amendment_draft_id": (
                amendment.spec_amendment_draft_id
                if amendment is not None
                else None
            ),
            "spec_amendment_status": (
                amendment.status if amendment is not None else None
            ),
        }
