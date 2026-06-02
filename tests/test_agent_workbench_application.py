"""Tests for the agent workbench application facade."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NoReturn, cast

from sqlalchemy import create_engine, text
from sqlmodel import SQLModel

import services.agent_workbench.application as application_mod
from db.migrations import ensure_schema_current
from models import db as model_db
from services.agent_workbench.application import AgentWorkbenchApplication
from services.agent_workbench.authority_decision import (
    AuthorityAcceptRequest,
    AuthorityRejectRequest,
)
from services.agent_workbench.mutation_ledger import (
    MutationLedgerRepository,
    MutationStatus,
    RecoveryAction,
)
from services.agent_workbench.version import STORAGE_SCHEMA_VERSION

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from sqlalchemy.engine import Engine

    from services.agent_workbench.project_setup import (
        ProjectCreateRequest,
        ProjectSetupRetryRequest,
    )

PROJECT_ID = 7
SPRINT_ID = 11
SPEC_VERSION_ID = 3
STORY_ID = 12
TASK_ID = 123
RECOVERY_MUTATION_EVENT_ID = 42
ACTIVE_BACKLOG_COUNT = 2
WORKFLOW_FINGERPRINT = "sha256:" + "1" * 64
CANDIDATES_FINGERPRINT = "sha256:" + "2" * 64
AUTHORITY_FINGERPRINT = "sha256:" + "3" * 64
PROJECT_FINGERPRINT = "sha256:" + "4" * 64
REVIEW_TOKEN_FIXTURE = "review-token-123"  # noqa: S105  # nosec B105

CLI_MUTATION_LEDGER_CREATE_SQL_PHASE_2A = """
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


class _FakeReadProjection:
    """Fake read projection used to verify facade delegation."""

    def project_list(self) -> dict[str, Any]:
        """Return a project list payload."""
        return {"ok": True, "data": {"items": []}, "warnings": [], "errors": []}

    def project_show(self, *, project_id: int) -> dict[str, Any]:
        """Return a project detail payload."""
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "name": "Workbench",
                "source_fingerprint": PROJECT_FINGERPRINT,
            },
            "warnings": [],
            "errors": [],
        }

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return a workflow state payload."""
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "state": {},
                "source_fingerprint": WORKFLOW_FINGERPRINT,
            },
            "warnings": [],
            "errors": [],
        }

    def story_show(self, *, story_id: int) -> dict[str, Any]:
        """Return a story detail payload."""
        return {
            "ok": True,
            "data": {"story_id": story_id},
            "warnings": [],
            "errors": [],
        }

    def sprint_candidates(self, *, project_id: int) -> dict[str, Any]:
        """Return a sprint candidate payload."""
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "items": [],
                "count": 0,
                "excluded_counts": {},
                "source_fingerprint": CANDIDATES_FINGERPRINT,
            },
            "warnings": [],
            "errors": [],
        }


class _FalseyReadProjection(_FakeReadProjection):
    """Falsey read projection used to verify explicit dependency checks."""

    def __bool__(self) -> bool:
        """Return false to catch truthiness-based dependency selection."""
        return False

    def project_list(self) -> dict[str, Any]:
        """Return a sentinel project list payload."""
        return {
            "ok": True,
            "data": {"sentinel": "falsey-read"},
            "warnings": [],
            "errors": [],
        }


class _SprintReadyReadProjection(_FakeReadProjection):
    """Fake read projection for sprint-planning-valid workflow state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return sprint setup workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "SPRINT_SETUP",
            "setup_status": "passed",
        }
        return result


class _SprintDraftReadProjection(_FakeReadProjection):
    """Fake read projection for a reviewed Sprint draft state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return sprint draft workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "SPRINT_DRAFT",
            "setup_status": "passed",
            "sprint_attempts": [
                {
                    "attempt_id": "sprint-attempt-4",
                    "artifact_fingerprint": "sha256:reviewed",
                    "is_complete": True,
                }
            ],
            "sprint_plan_assessment": {
                "attempt_id": "sprint-attempt-4",
                "artifact_fingerprint": "sha256:reviewed",
                "is_complete": True,
            },
        }
        return result


class _SprintPersistenceReadProjection(_FakeReadProjection):
    """Fake read projection for a saved planned Sprint state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return sprint persistence workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "SPRINT_PERSISTENCE",
            "setup_status": "passed",
        }
        return result


class _SprintViewReadProjection(_FakeReadProjection):
    """Fake read projection for an active Sprint state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return sprint view workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "SPRINT_VIEW",
            "setup_status": "passed",
            "active_sprint_id": 11,
        }
        return result


class _SprintCompleteReadProjection(_FakeReadProjection):
    """Fake read projection for a completed Sprint state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return sprint complete workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "SPRINT_COMPLETE",
            "setup_status": "passed",
            "active_sprint_id": 11,
        }
        return result


class _SprintCompleteWithBacklogAttemptsReadProjection(_SprintCompleteReadProjection):
    """Fake completed Sprint state with a source Backlog attempt."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return sprint complete workflow state with Backlog refinement source."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"]["backlog_attempts"] = [
            {
                "attempt_id": "backlog-attempt-1",
                "artifact_fingerprint": "sha256:old-source",
            },
            {
                "attempt_id": "backlog-attempt-2",
                "artifact_fingerprint": "sha256:latest-source",
            },
        ]
        return result


class _VisionInterviewReadProjection(_FakeReadProjection):
    """Fake read projection for the Vision interview state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Vision interview workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "VISION_INTERVIEW",
            "setup_status": "passed",
        }
        return result


class _VisionReviewReadProjection(_FakeReadProjection):
    """Fake read projection for the Vision review state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Vision review workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "VISION_REVIEW",
            "setup_status": "passed",
        }
        return result


class _VisionPersistenceReadProjection(_FakeReadProjection):
    """Fake read projection for saved Vision state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Vision persistence workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "VISION_PERSISTENCE",
            "setup_status": "passed",
        }
        return result


class _BacklogInterviewReadProjection(_FakeReadProjection):
    """Fake read projection for the Backlog interview state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Backlog interview workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "BACKLOG_INTERVIEW",
            "setup_status": "passed",
        }
        return result


class _BacklogReviewReadProjection(_FakeReadProjection):
    """Fake read projection for the Backlog review state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Backlog review workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "BACKLOG_REVIEW",
            "setup_status": "passed",
        }
        return result


class _BacklogPersistenceReadProjection(_FakeReadProjection):
    """Fake read projection for saved Backlog state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Backlog persistence workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "BACKLOG_PERSISTENCE",
            "setup_status": "passed",
        }
        return result


class _BacklogPersistenceActiveResetReadProjection(_BacklogPersistenceReadProjection):
    """Fake read projection for active-reset stale Backlog persistence."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Backlog persistence workflow state with reset stale marker."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"].update(
            {
                "downstream_backlog_stale": True,
                "stale_backlog_reason": "active_backlog_reset",
                "stale_since_backlog_attempt_id": "backlog-attempt-12",
                "active_backlog_reset_attempt_id": "backlog-attempt-12",
            }
        )
        return result


class _RoadmapInterviewReadProjection(_FakeReadProjection):
    """Fake read projection for the Roadmap interview state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Roadmap interview workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "ROADMAP_INTERVIEW",
            "setup_status": "passed",
        }
        return result


class _RoadmapReviewReadProjection(_FakeReadProjection):
    """Fake read projection for the Roadmap review state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Roadmap review workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "ROADMAP_REVIEW",
            "setup_status": "passed",
        }
        return result


class _RoadmapPersistenceReadProjection(_FakeReadProjection):
    """Fake read projection for saved Roadmap state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Roadmap persistence workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "ROADMAP_PERSISTENCE",
            "setup_status": "passed",
        }
        return result


class _StoryInterviewReadProjection(_FakeReadProjection):
    """Fake read projection for the Story interview state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Story interview workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "STORY_INTERVIEW",
            "setup_status": "passed",
        }
        return result


class _StoryReopenedReadProjection(_FakeReadProjection):
    """Fake read projection for a reopened Story interview state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return reopened Story interview workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "STORY_INTERVIEW",
            "roadmap_releases": [{"items": ["Requirement A", "Requirement B"]}],
            "story_saved": {"Requirement B": True},
        }
        return result


class _StoryReviewReadProjection(_FakeReadProjection):
    """Fake read projection for the Story review state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Story review workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "STORY_REVIEW",
            "setup_status": "passed",
        }
        return result


class _StoryPersistenceReadProjection(_FakeReadProjection):
    """Fake read projection for saved Story state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Story persistence workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "STORY_PERSISTENCE",
            "setup_status": "passed",
            "roadmap_releases": [{"items": ["Requirement A", "Requirement B"]}],
            "story_saved": {"Requirement A": True},
        }
        return result


class _StoryCompleteReadyReadProjection(_FakeReadProjection):
    """Fake read projection for fully covered Story state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Story persistence workflow state with full coverage."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "STORY_PERSISTENCE",
            "setup_status": "passed",
            "roadmap_releases": [{"items": ["Requirement A", "Requirement B"]}],
            "story_saved": {"Requirement A": True, "Requirement B": True},
        }
        return result


class _AuthorityPendingReviewReadProjection(_FakeReadProjection):
    """Fake read projection for setup blocked on authority review."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return pending authority review workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "authority_pending_review",
        }
        return result


class _AuthorityRejectedReadProjection(_FakeReadProjection):
    """Fake read projection for setup blocked on rejected authority."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return rejected authority workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "authority_rejected",
        }
        return result


class _SetupFailedReadProjection(_FakeReadProjection):
    """Fake read projection for failed setup."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return failed setup workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "failed",
        }
        return result


class _ChangedProjectReadProjection(_FakeReadProjection):
    """Fake read projection with a changed project fingerprint."""

    def project_show(self, *, project_id: int) -> dict[str, Any]:
        """Return project detail payload with changed fingerprint inputs."""
        result = super().project_show(project_id=project_id)
        result["data"]["source_fingerprint"] = "sha256:" + "8" * 64
        return result


class _ChangedSprintWorkflowReadProjection(_SprintReadyReadProjection):
    """Fake read projection with changed Sprint workflow fingerprint input."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Sprint setup workflow state with changed fingerprint input."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["source_fingerprint"] = "sha256:" + "9" * 64
        return result


class _FakeAuthorityProjection:
    """Fake authority projection used to verify facade delegation."""

    def status(self, *, project_id: int) -> dict[str, Any]:
        """Return an authority status payload."""
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "status": "missing",
                "authority_fingerprint": AUTHORITY_FINGERPRINT,
            },
            "warnings": [],
            "errors": [],
        }

    def invariants(
        self,
        *,
        project_id: int,
        spec_version_id: int | None = None,
    ) -> dict[str, Any]:
        """Return an authority invariants payload."""
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "spec_version_id": spec_version_id,
                "invariants": [],
            },
            "warnings": [],
            "errors": [],
        }


class _CurrentAuthorityProjection(_FakeAuthorityProjection):
    """Fake authority projection that permits sprint planning."""

    def status(self, *, project_id: int) -> dict[str, Any]:
        """Return a current authority status payload."""
        result = super().status(project_id=project_id)
        result["data"]["status"] = "current"
        return result


class _FalseyAuthorityProjection(_FakeAuthorityProjection):
    """Falsey authority projection used to verify explicit dependency checks."""

    def __bool__(self) -> bool:
        """Return false to catch truthiness-based dependency selection."""
        return False

    def status(self, *, project_id: int) -> dict[str, Any]:
        """Return a sentinel authority status payload."""
        return {
            "ok": True,
            "data": {"project_id": project_id, "status": "falsey-authority"},
            "warnings": [],
            "errors": [],
        }


class _FakeProjectSetupRunner:
    """Fake project setup runner used to verify facade request construction."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def create_project(self, request: ProjectCreateRequest) -> dict[str, Any]:
        """Return a create payload and record the request model."""
        self.calls.append(("create_project", request))
        return {
            "ok": True,
            "data": {"project_id": 1, "name": request.name},
            "warnings": [],
            "errors": [],
        }

    def retry_setup(self, request: ProjectSetupRetryRequest) -> dict[str, Any]:
        """Return a setup retry payload and record the request model."""
        self.calls.append(("retry_setup", request))
        return {
            "ok": True,
            "data": {"project_id": request.project_id},
            "warnings": [],
            "errors": [],
        }


class _FakeAuthorityReview:
    """Fake authority review service used to verify facade delegation."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.response = response or {
            "ok": True,
            "data": {"review_token": REVIEW_TOKEN_FIXTURE},
            "warnings": [],
            "errors": [],
        }

    def review(
        self,
        *,
        project_id: int,
        include_spec: str = "auto",
        output_format: str = "json",
    ) -> dict[str, Any]:
        """Return a review payload and record call arguments."""
        self.calls.append(
            {
                "project_id": project_id,
                "include_spec": include_spec,
                "output_format": output_format,
            }
        )
        return self.response


class _FakeAuthorityDecisionRunner:
    """Fake authority decision runner used to verify facade delegation."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def accept(self, request: AuthorityAcceptRequest) -> dict[str, Any]:
        """Record an accept request."""
        self.calls.append(("accept", request))
        return {
            "ok": True,
            "data": {"decision": "accepted", "project_id": request.project_id},
            "warnings": [],
            "errors": [],
        }

    def reject(self, request: AuthorityRejectRequest) -> dict[str, Any]:
        """Record a reject request."""
        self.calls.append(("reject", request))
        return {
            "ok": True,
            "data": {"decision": "rejected", "project_id": request.project_id},
            "warnings": [],
            "errors": [],
        }


class _FakeVisionRunner:
    """Fake Vision runner used to verify facade delegation."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Record Vision generation."""
        self.calls.append(
            (
                "generate",
                {"project_id": project_id, "user_input": user_input},
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "is_complete": False},
            "warnings": [],
            "errors": [],
        }

    def history(self, *, project_id: int) -> dict[str, Any]:
        """Record Vision history lookup."""
        self.calls.append(("history", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "items": []},
            "warnings": [],
            "errors": [],
        }

    def save(self, *, project_id: int) -> dict[str, Any]:
        """Record Vision save."""
        self.calls.append(("save", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "VISION_PERSISTENCE"},
            "warnings": [],
            "errors": [],
        }


class _FakeBacklogRunner:
    """Fake Backlog runner used to verify facade delegation."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Record Backlog generation."""
        self.calls.append(
            (
                "generate",
                {"project_id": project_id, "user_input": user_input},
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "is_complete": False},
            "warnings": [],
            "errors": [],
        }

    def preview(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Record Backlog preview."""
        self.calls.append(
            (
                "preview",
                {"project_id": project_id, "user_input": user_input},
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "persisted": False},
            "warnings": [],
            "errors": [],
        }

    def refine_preview(
        self,
        *,
        project_id: int,
        source_attempt_id: str | None = None,
        operations_file: str | None = None,
        source_artifact: str | None = None,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Record Backlog refinement preview."""
        self.calls.append(
            (
                "refine_preview",
                {
                    "project_id": project_id,
                    "source_attempt_id": source_attempt_id,
                    "operations_file": operations_file,
                    "source_artifact": source_artifact,
                    "user_input": user_input,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "persisted": False},
            "warnings": [],
            "errors": [],
        }

    def refine_record(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        source_attempt_id: str,
        operations_file: str,
        expected_source_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Record Backlog refinement save."""
        self.calls.append(
            (
                "refine_record",
                {
                    "project_id": project_id,
                    "source_attempt_id": source_attempt_id,
                    "operations_file": operations_file,
                    "expected_source_fingerprint": expected_source_fingerprint,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                    "approval_id": approval_id,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "BACKLOG_REVIEW"},
            "warnings": [],
            "errors": [],
        }

    def approve(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        approved_artifact_fingerprint: str,
        idempotency_key: str,
        source_attempt_id: str | None = None,
        attempt_id: str | None = None,
        operation_set_fingerprint: str | None = None,
        approved_operation_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record Backlog refinement approval."""
        self.calls.append(
            (
                "approve",
                {
                    "project_id": project_id,
                    "source_attempt_id": source_attempt_id,
                    "attempt_id": attempt_id,
                    "operation_set_fingerprint": operation_set_fingerprint,
                    "approved_artifact_fingerprint": approved_artifact_fingerprint,
                    "approved_operation_ids": approved_operation_ids,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "approval_id": "approval:1234"},
            "warnings": [],
            "errors": [],
        }

    def refine_import(
        self,
        *,
        project_id: int,
        source_artifact: str,
        edited_file: str,
        expected_source_fingerprint: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Record Backlog refinement import request."""
        self.calls.append(
            (
                "refine_import",
                {
                    "project_id": project_id,
                    "source_artifact": source_artifact,
                    "edited_file": edited_file,
                    "expected_source_fingerprint": expected_source_fingerprint,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": False,
            "data": None,
            "warnings": [],
            "errors": [{"code": "MUTATION_FAILED"}],
        }

    def history(self, *, project_id: int) -> dict[str, Any]:
        """Record Backlog history lookup."""
        self.calls.append(("history", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "items": []},
            "warnings": [],
            "errors": [],
        }

    def save(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Record Backlog save."""
        self.calls.append(
            (
                "save",
                {
                    "project_id": project_id,
                    "attempt_id": attempt_id,
                    "expected_artifact_fingerprint": expected_artifact_fingerprint,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "BACKLOG_PERSISTENCE"},
            "warnings": [],
            "errors": [],
        }

    def reset_active(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        reset_reason: str,
        archive_all_active_stories: bool,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Record Backlog reset-active."""
        self.calls.append(
            (
                "reset_active",
                {
                    "project_id": project_id,
                    "attempt_id": attempt_id,
                    "expected_artifact_fingerprint": expected_artifact_fingerprint,
                    "expected_state": expected_state,
                    "reset_reason": reset_reason,
                    "archive_all_active_stories": archive_all_active_stories,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "BACKLOG_PERSISTENCE"},
            "warnings": [],
            "errors": [],
        }

    def reconcile(
        self,
        *,
        project_id: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Record Backlog reconciliation."""
        self.calls.append(
            (
                "reconcile",
                {
                    "project_id": project_id,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "active_after": 2},
            "warnings": [],
            "errors": [],
        }


class _FakeRoadmapRunner:
    """Fake Roadmap runner used to verify facade delegation."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Record Roadmap generation."""
        self.calls.append(
            (
                "generate",
                {"project_id": project_id, "user_input": user_input},
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "is_complete": False},
            "warnings": [],
            "errors": [],
        }

    def history(self, *, project_id: int) -> dict[str, Any]:
        """Record Roadmap history lookup."""
        self.calls.append(("history", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "items": []},
            "warnings": [],
            "errors": [],
        }

    def save(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Record Roadmap save."""
        self.calls.append(
            (
                "save",
                {
                    "project_id": project_id,
                    "attempt_id": attempt_id,
                    "expected_artifact_fingerprint": expected_artifact_fingerprint,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "ROADMAP_PERSISTENCE"},
            "warnings": [],
            "errors": [],
        }


class _FakeStoryRunner:
    """Fake Story runner used to verify facade delegation."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def pending(self, *, project_id: int) -> dict[str, Any]:
        """Record Story pending lookup."""
        self.calls.append(("pending", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "pending": []},
            "warnings": [],
            "errors": [],
        }

    def generate(
        self,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Record Story generation."""
        self.calls.append(
            (
                "generate",
                {
                    "project_id": project_id,
                    "parent_requirement": parent_requirement,
                    "user_input": user_input,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "is_complete": False},
            "warnings": [],
            "errors": [],
        }

    def retry(self, *, project_id: int, parent_requirement: str) -> dict[str, Any]:
        """Record Story retry."""
        self.calls.append(
            (
                "retry",
                {
                    "project_id": project_id,
                    "parent_requirement": parent_requirement,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "retry_started": True},
            "warnings": [],
            "errors": [],
        }

    def history(
        self,
        *,
        project_id: int,
        parent_requirement: str,
    ) -> dict[str, Any]:
        """Record Story history lookup."""
        self.calls.append(
            (
                "history",
                {
                    "project_id": project_id,
                    "parent_requirement": parent_requirement,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "items": []},
            "warnings": [],
            "errors": [],
        }

    def save(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        parent_requirement: str,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Record Story save."""
        self.calls.append(
            (
                "save",
                {
                    "project_id": project_id,
                    "parent_requirement": parent_requirement,
                    "attempt_id": attempt_id,
                    "expected_artifact_fingerprint": expected_artifact_fingerprint,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "STORY_PERSISTENCE"},
            "warnings": [],
            "errors": [],
        }

    def complete(
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Record Story completion."""
        self.calls.append(
            (
                "complete",
                {
                    "project_id": project_id,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "SPRINT_SETUP"},
            "warnings": [],
            "errors": [],
        }

    def reopen(
        self,
        *,
        project_id: int,
        parent_requirement: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Record Story reopen."""
        self.calls.append(
            (
                "reopen",
                {
                    "project_id": project_id,
                    "parent_requirement": parent_requirement,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "STORY_INTERVIEW"},
            "warnings": [],
            "errors": [],
        }

    def repair_readiness(
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Record Story readiness repair."""
        self.calls.append(
            (
                "repair_readiness",
                {
                    "project_id": project_id,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "fsm_state": "SPRINT_SETUP",
                "repair_result": {"repaired_count": 1, "story_ids": [66]},
            },
            "warnings": [],
            "errors": [],
        }


class _FakeSprintRunner:
    """Fake Sprint runner used to verify facade delegation."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def generate(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        user_input: str | None = None,
        selected_story_ids: list[int] | None = None,
        team_velocity_assumption: str = "Medium",
        sprint_duration_days: int = 14,
        max_story_points: int | None = None,
        include_task_decomposition: bool = True,
    ) -> dict[str, Any]:
        """Record Sprint generate."""
        self.calls.append(
            (
                "generate",
                {
                    "project_id": project_id,
                    "user_input": user_input,
                    "selected_story_ids": selected_story_ids,
                    "team_velocity_assumption": team_velocity_assumption,
                    "sprint_duration_days": sprint_duration_days,
                    "max_story_points": max_story_points,
                    "include_task_decomposition": include_task_decomposition,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "SPRINT_DRAFT"},
            "warnings": [],
            "errors": [],
        }

    def history(self, *, project_id: int) -> dict[str, Any]:
        """Record Sprint history."""
        self.calls.append(("history", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "items": []},
            "warnings": [],
            "errors": [],
        }

    def save(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        team_name: str,
        sprint_start_date: str,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Record Sprint save."""
        self.calls.append(
            (
                "save",
                {
                    "project_id": project_id,
                    "team_name": team_name,
                    "sprint_start_date": sprint_start_date,
                    "attempt_id": attempt_id,
                    "expected_artifact_fingerprint": expected_artifact_fingerprint,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "SPRINT_PERSISTENCE"},
            "warnings": [],
            "errors": [],
        }

    def start(
        self,
        *,
        project_id: int,
        sprint_id: int | None,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Record Sprint start."""
        self.calls.append(
            (
                "start",
                {
                    "project_id": project_id,
                    "sprint_id": sprint_id,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "sprint_id": sprint_id or 11},
            "warnings": [],
            "errors": [],
        }

    def status(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Record Sprint status."""
        self.calls.append(
            ("status", {"project_id": project_id, "sprint_id": sprint_id})
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "sprint_id": sprint_id or 11},
            "warnings": [],
            "errors": [],
        }

    def tasks(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Record Sprint tasks."""
        self.calls.append(("tasks", {"project_id": project_id, "sprint_id": sprint_id}))
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "sprint_id": sprint_id or SPRINT_ID,
                "tasks": [],
            },
            "warnings": [],
            "errors": [],
        }

    def task_next(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Record Sprint task next."""
        self.calls.append(
            ("task_next", {"project_id": project_id, "sprint_id": sprint_id})
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "task_ticket": None},
            "warnings": [],
            "errors": [],
        }

    def task_show(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Record Sprint task show."""
        self.calls.append(
            (
                "task_show",
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "sprint_id": sprint_id,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "task_ticket": {"task_id": task_id}},
            "warnings": [],
            "errors": [],
        }

    def task_history(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Record Sprint task history."""
        self.calls.append(
            (
                "task_history",
                {"project_id": project_id, "task_id": task_id, "sprint_id": sprint_id},
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "execution": {"history": []}},
            "warnings": [],
            "errors": [],
        }

    def task_update(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        task_id: int,
        status: str,
        expected_status: str,
        expected_task_fingerprint: str,
        idempotency_key: str,
        sprint_id: int | None = None,
        outcome_summary: str | None = None,
        artifact_refs: list[str] | None = None,
        checklist_result: str | None = None,
        validation_summary: str | None = None,
        notes: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record Sprint task update."""
        self.calls.append(
            (
                "task_update",
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "status": status,
                    "expected_status": expected_status,
                    "expected_task_fingerprint": expected_task_fingerprint,
                    "idempotency_key": idempotency_key,
                    "sprint_id": sprint_id,
                    "outcome_summary": outcome_summary,
                    "artifact_refs": artifact_refs,
                    "checklist_result": checklist_result,
                    "validation_summary": validation_summary,
                    "notes": notes,
                    "changed_by": changed_by,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "task_id": task_id},
            "warnings": [],
            "errors": [],
        }

    def story_readiness(
        self,
        *,
        project_id: int,
        story_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Record Sprint story readiness."""
        self.calls.append(
            (
                "story_readiness",
                {
                    "project_id": project_id,
                    "story_id": story_id,
                    "sprint_id": sprint_id,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "story_id": story_id},
            "warnings": [],
            "errors": [],
        }

    def story_close(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        story_id: int,
        expected_status: str,
        expected_story_fingerprint: str,
        idempotency_key: str,
        resolution: str,
        completion_notes: str,
        evidence_links: list[str] | None = None,
        sprint_id: int | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record Sprint story close."""
        self.calls.append(
            (
                "story_close",
                {
                    "project_id": project_id,
                    "story_id": story_id,
                    "expected_status": expected_status,
                    "expected_story_fingerprint": expected_story_fingerprint,
                    "idempotency_key": idempotency_key,
                    "resolution": resolution,
                    "completion_notes": completion_notes,
                    "evidence_links": evidence_links,
                    "sprint_id": sprint_id,
                    "changed_by": changed_by,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "story_id": story_id},
            "warnings": [],
            "errors": [],
        }

    def close_readiness(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Record Sprint close readiness."""
        self.calls.append(
            ("close_readiness", {"project_id": project_id, "sprint_id": sprint_id})
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "sprint_id": sprint_id or 11},
            "warnings": [],
            "errors": [],
        }

    def close(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        expected_state: str,
        expected_status: str,
        expected_sprint_fingerprint: str,
        idempotency_key: str,
        completion_notes: str,
        follow_up_notes: str | None = None,
        sprint_id: int | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record Sprint close."""
        self.calls.append(
            (
                "close",
                {
                    "project_id": project_id,
                    "expected_state": expected_state,
                    "expected_status": expected_status,
                    "expected_sprint_fingerprint": expected_sprint_fingerprint,
                    "idempotency_key": idempotency_key,
                    "completion_notes": completion_notes,
                    "follow_up_notes": follow_up_notes,
                    "sprint_id": sprint_id,
                    "changed_by": changed_by,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "sprint_id": sprint_id or 11},
            "warnings": [],
            "errors": [],
        }


def test_application_delegates_to_read_projection() -> None:
    """Verify application facade is thin and explicit."""
    app = AgentWorkbenchApplication(
        read_projection=_FakeReadProjection(),
        authority_projection=_FakeAuthorityProjection(),
    )

    assert app.project_list()["data"]["items"] == []
    assert app.project_show(project_id=PROJECT_ID)["data"]["project_id"] == PROJECT_ID
    assert app.workflow_state(project_id=PROJECT_ID)["data"]["state"] == {}
    assert app.story_show(story_id=STORY_ID)["data"]["story_id"] == STORY_ID
    assert app.sprint_candidates(project_id=PROJECT_ID)["data"]["items"] == []


def test_application_delegates_to_authority_projection() -> None:
    """Verify authority projections stay behind the facade."""
    app = AgentWorkbenchApplication(
        read_projection=_FakeReadProjection(),
        authority_projection=_FakeAuthorityProjection(),
    )

    assert app.authority_status(project_id=PROJECT_ID)["data"]["status"] == "missing"
    assert app.authority_invariants(
        project_id=PROJECT_ID,
        spec_version_id=SPEC_VERSION_ID,
    )["data"] == {
        "project_id": PROJECT_ID,
        "spec_version_id": SPEC_VERSION_ID,
        "invariants": [],
    }


def test_application_routes_project_create_to_setup_runner() -> None:
    """Verify project create facade builds the request model."""
    runner = _FakeProjectSetupRunner()
    app = AgentWorkbenchApplication(project_setup_runner=runner)

    result = app.project_create(
        name="CLI Project",
        spec_file="specs/app.md",
        idempotency_key="create-cli-project-001",
        dry_run=False,
        dry_run_id=None,
        correlation_id="corr-1",
        changed_by="test-agent",
    )

    assert result["ok"] is True
    assert runner.calls[0][0] == "create_project"
    request = cast("ProjectCreateRequest", runner.calls[0][1])
    assert request.name == "CLI Project"
    assert request.spec_file == "specs/app.md"
    assert request.idempotency_key == "create-cli-project-001"
    assert request.dry_run is False
    assert request.dry_run_id is None
    assert request.correlation_id == "corr-1"
    assert request.changed_by == "test-agent"


def test_application_routes_project_setup_retry_to_setup_runner() -> None:
    """Verify setup retry facade builds the guarded request model."""
    runner = _FakeProjectSetupRunner()
    app = AgentWorkbenchApplication(project_setup_runner=runner)

    result = app.project_setup_retry(
        project_id=PROJECT_ID,
        spec_file="specs/app.md",
        expected_state="SETUP_REQUIRED",
        expected_context_fingerprint="ctx123",
        recovery_mutation_event_id=RECOVERY_MUTATION_EVENT_ID,
        idempotency_key="retry-cli-project-001",
        dry_run=False,
        dry_run_id=None,
        correlation_id="corr-1",
        changed_by="test-agent",
    )

    assert result["ok"] is True
    assert runner.calls[0][0] == "retry_setup"
    request = cast("ProjectSetupRetryRequest", runner.calls[0][1])
    assert request.project_id == PROJECT_ID
    assert request.spec_file == "specs/app.md"
    assert request.expected_state == "SETUP_REQUIRED"
    assert request.expected_context_fingerprint == "ctx123"
    assert request.recovery_mutation_event_id == RECOVERY_MUTATION_EVENT_ID
    assert request.idempotency_key == "retry-cli-project-001"
    assert request.dry_run is False
    assert request.dry_run_id is None
    assert request.correlation_id == "corr-1"
    assert request.changed_by == "test-agent"


def test_application_authority_review_delegates_to_review_service() -> None:
    """Verify authority review facade delegates to the review service."""
    review = _FakeAuthorityReview()
    app = AgentWorkbenchApplication(authority_review=review)

    result = app.authority_review(
        project_id=PROJECT_ID,
        include_spec="summary",
        output_format="json",
    )

    assert result["data"]["review_token"] == REVIEW_TOKEN_FIXTURE
    assert review.calls == [
        {
            "project_id": PROJECT_ID,
            "include_spec": "summary",
            "output_format": "json",
        }
    ]


def test_application_authority_accept_delegates_to_decision_runner() -> None:
    """Verify authority accept facade delegates with the request model."""
    runner = _FakeAuthorityDecisionRunner()
    app = AgentWorkbenchApplication(authority_decision_runner=runner)
    request = AuthorityAcceptRequest(
        project_id=PROJECT_ID,
        review_token=REVIEW_TOKEN_FIXTURE,
        idempotency_key="authority-accept-key",
    )

    result = app.authority_accept(request)

    assert result["data"] == {"decision": "accepted", "project_id": PROJECT_ID}
    assert runner.calls == [("accept", request)]


def test_application_authority_reject_delegates_to_decision_runner() -> None:
    """Verify authority reject facade delegates with the request model."""
    runner = _FakeAuthorityDecisionRunner()
    app = AgentWorkbenchApplication(authority_decision_runner=runner)
    request = AuthorityRejectRequest(
        project_id=PROJECT_ID,
        review_token=REVIEW_TOKEN_FIXTURE,
        idempotency_key="authority-reject-key",
        reason="Source requirements are incomplete.",
    )

    result = app.authority_reject(request)

    assert result["data"] == {"decision": "rejected", "project_id": PROJECT_ID}
    assert runner.calls == [("reject", request)]


def test_application_routes_vision_commands_to_runner() -> None:
    """Verify Vision facade methods delegate to the configured runner."""
    runner = _FakeVisionRunner()
    app = AgentWorkbenchApplication(vision_runner=runner)

    assert (
        app.vision_generate(
            project_id=PROJECT_ID,
            user_input="tighten goals",
        )["data"]["is_complete"]
        is False
    )
    assert app.vision_history(project_id=PROJECT_ID)["data"]["items"] == []
    assert app.vision_save(project_id=PROJECT_ID)["data"]["fsm_state"] == (
        "VISION_PERSISTENCE"
    )
    assert runner.calls == [
        ("generate", {"project_id": PROJECT_ID, "user_input": "tighten goals"}),
        ("history", {"project_id": PROJECT_ID}),
        ("save", {"project_id": PROJECT_ID}),
    ]


def test_application_routes_backlog_commands_to_runner() -> None:
    """Verify Backlog facade methods delegate to the configured runner."""
    runner = _FakeBacklogRunner()
    app = AgentWorkbenchApplication(backlog_runner=runner)

    assert (
        app.backlog_generate(
            project_id=PROJECT_ID,
            user_input="tighten themes",
        )["data"]["is_complete"]
        is False
    )
    assert (
        app.backlog_preview(
            project_id=PROJECT_ID,
            user_input="brownfield smoke",
        )["data"]["persisted"]
        is False
    )
    assert (
        app.backlog_refine_preview(
            project_id=PROJECT_ID,
            source_attempt_id="backlog-attempt-1",
            operations_file="fixtures/operations.json",
        )["data"]["persisted"]
        is False
    )
    assert (
        app.backlog_refine_record(
            project_id=PROJECT_ID,
            source_attempt_id="backlog-attempt-1",
            operations_file="fixtures/operations.json",
            expected_source_fingerprint="sha256:" + "b" * 64,
            expected_state="SPRINT_COMPLETE",
            idempotency_key="refine-record-1",
        )["data"]["fsm_state"]
        == "BACKLOG_REVIEW"
    )
    assert (
        app.backlog_approve(
            project_id=PROJECT_ID,
            source_attempt_id="backlog-attempt-1",
            operation_set_fingerprint="sha256:" + "c" * 64,
            approved_artifact_fingerprint="sha256:" + "d" * 64,
            approved_operation_ids=["op-1"],
            idempotency_key="approve-refinement-1",
        )["data"]["approval_id"]
        == "approval:1234"
    )
    assert (
        app.backlog_refine_import(
            project_id=PROJECT_ID,
            source_artifact="fixtures/source.json",
            edited_file="fixtures/edited.json",
            expected_source_fingerprint="sha256:" + "e" * 64,
            idempotency_key="refine-import-1",
        )["errors"][0]["code"]
        == "MUTATION_FAILED"
    )
    assert app.backlog_history(project_id=PROJECT_ID)["data"]["items"] == []
    assert (
        app.backlog_save(
            project_id=PROJECT_ID,
            attempt_id="backlog-attempt-1",
            expected_artifact_fingerprint="sha256:" + "a" * 64,
            expected_state="BACKLOG_REVIEW",
            idempotency_key="save-backlog-1",
        )["data"]["fsm_state"]
        == "BACKLOG_PERSISTENCE"
    )
    assert (
        app.backlog_reset_active(
            project_id=PROJECT_ID,
            attempt_id="backlog-attempt-1",
            expected_artifact_fingerprint="sha256:" + "f" * 64,
            expected_state="BACKLOG_REVIEW",
            reset_reason="pre-brownfield reset",
            archive_all_active_stories=True,
            idempotency_key="reset-active-1",
        )["data"]["fsm_state"]
        == "BACKLOG_PERSISTENCE"
    )
    assert (
        app.backlog_reconcile(
            project_id=PROJECT_ID,
            idempotency_key="reconcile-backlog-1",
        )["data"]["active_after"]
        == ACTIVE_BACKLOG_COUNT
    )
    assert runner.calls == [
        ("generate", {"project_id": PROJECT_ID, "user_input": "tighten themes"}),
        ("preview", {"project_id": PROJECT_ID, "user_input": "brownfield smoke"}),
        (
            "refine_preview",
            {
                "project_id": PROJECT_ID,
                "source_attempt_id": "backlog-attempt-1",
                "operations_file": "fixtures/operations.json",
                "source_artifact": None,
                "user_input": None,
            },
        ),
        (
            "refine_record",
            {
                "project_id": PROJECT_ID,
                "source_attempt_id": "backlog-attempt-1",
                "operations_file": "fixtures/operations.json",
                "expected_source_fingerprint": "sha256:" + "b" * 64,
                "expected_state": "SPRINT_COMPLETE",
                "idempotency_key": "refine-record-1",
                "approval_id": None,
            },
        ),
        (
            "approve",
            {
                "project_id": PROJECT_ID,
                "source_attempt_id": "backlog-attempt-1",
                "attempt_id": None,
                "operation_set_fingerprint": "sha256:" + "c" * 64,
                "approved_artifact_fingerprint": "sha256:" + "d" * 64,
                "approved_operation_ids": ["op-1"],
                "idempotency_key": "approve-refinement-1",
            },
        ),
        (
            "refine_import",
            {
                "project_id": PROJECT_ID,
                "source_artifact": "fixtures/source.json",
                "edited_file": "fixtures/edited.json",
                "expected_source_fingerprint": "sha256:" + "e" * 64,
                "idempotency_key": "refine-import-1",
            },
        ),
        ("history", {"project_id": PROJECT_ID}),
        (
            "save",
            {
                "project_id": PROJECT_ID,
                "attempt_id": "backlog-attempt-1",
                "expected_artifact_fingerprint": "sha256:" + "a" * 64,
                "expected_state": "BACKLOG_REVIEW",
                "idempotency_key": "save-backlog-1",
            },
        ),
        (
            "reset_active",
            {
                "project_id": PROJECT_ID,
                "attempt_id": "backlog-attempt-1",
                "expected_artifact_fingerprint": "sha256:" + "f" * 64,
                "expected_state": "BACKLOG_REVIEW",
                "reset_reason": "pre-brownfield reset",
                "archive_all_active_stories": True,
                "idempotency_key": "reset-active-1",
            },
        ),
        (
            "reconcile",
            {
                "project_id": PROJECT_ID,
                "idempotency_key": "reconcile-backlog-1",
            },
        ),
    ]


def test_backlog_reset_active_facade_routes_to_runner() -> None:
    """Verify reset-active facade delegates guarded args to the runner."""
    runner = _FakeBacklogRunner()
    app = AgentWorkbenchApplication(backlog_runner=runner)

    payload = app.backlog_reset_active(
        project_id=PROJECT_ID,
        attempt_id="backlog-attempt-1",
        expected_artifact_fingerprint="sha256:" + "f" * 64,
        expected_state="BACKLOG_REVIEW",
        reset_reason="pre-brownfield reset",
        archive_all_active_stories=True,
        idempotency_key="reset-active-1",
    )

    assert payload["data"]["fsm_state"] == "BACKLOG_PERSISTENCE"
    assert runner.calls == [
        (
            "reset_active",
            {
                "project_id": PROJECT_ID,
                "attempt_id": "backlog-attempt-1",
                "expected_artifact_fingerprint": "sha256:" + "f" * 64,
                "expected_state": "BACKLOG_REVIEW",
                "reset_reason": "pre-brownfield reset",
                "archive_all_active_stories": True,
                "idempotency_key": "reset-active-1",
            },
        )
    ]


def test_application_routes_roadmap_commands_to_runner() -> None:
    """Verify Roadmap facade methods delegate to the configured runner."""
    runner = _FakeRoadmapRunner()
    app = AgentWorkbenchApplication(roadmap_runner=runner)

    assert (
        app.roadmap_generate(
            project_id=PROJECT_ID,
            user_input="tighten milestones",
        )["data"]["is_complete"]
        is False
    )
    assert app.roadmap_history(project_id=PROJECT_ID)["data"]["items"] == []
    assert (
        app.roadmap_save(
            project_id=PROJECT_ID,
            attempt_id="roadmap-attempt-1",
            expected_artifact_fingerprint="sha256:" + "a" * 64,
            expected_state="ROADMAP_REVIEW",
            idempotency_key="save-roadmap-1",
        )["data"]["fsm_state"]
        == "ROADMAP_PERSISTENCE"
    )
    assert runner.calls == [
        ("generate", {"project_id": PROJECT_ID, "user_input": "tighten milestones"}),
        ("history", {"project_id": PROJECT_ID}),
        (
            "save",
            {
                "project_id": PROJECT_ID,
                "attempt_id": "roadmap-attempt-1",
                "expected_artifact_fingerprint": "sha256:" + "a" * 64,
                "expected_state": "ROADMAP_REVIEW",
                "idempotency_key": "save-roadmap-1",
            },
        ),
    ]


def test_application_routes_story_commands_to_runner() -> None:
    """Verify Story facade methods delegate to the configured runner."""
    runner = _FakeStoryRunner()
    app = AgentWorkbenchApplication(story_runner=cast("Any", runner))

    assert app.story_pending(project_id=PROJECT_ID)["data"]["pending"] == []
    assert (
        app.story_generate(
            project_id=PROJECT_ID,
            parent_requirement="REQ.checkout",
            user_input="focus payment errors",
        )["data"]["is_complete"]
        is False
    )
    assert (
        app.story_retry(
            project_id=PROJECT_ID,
            parent_requirement="REQ.checkout",
        )["data"]["retry_started"]
        is True
    )
    assert (
        app.story_history(
            project_id=PROJECT_ID,
            parent_requirement="REQ.checkout",
        )["data"]["items"]
        == []
    )
    assert (
        app.story_save(
            project_id=PROJECT_ID,
            parent_requirement="REQ.checkout",
            attempt_id="story-attempt-1",
            expected_artifact_fingerprint="sha256:" + "a" * 64,
            expected_state="STORY_REVIEW",
            idempotency_key="save-story-1",
        )["data"]["fsm_state"]
        == "STORY_PERSISTENCE"
    )
    assert (
        app.story_complete(
            project_id=PROJECT_ID,
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-1",
        )["data"]["fsm_state"]
        == "SPRINT_SETUP"
    )
    assert (
        app.story_reopen(
            project_id=PROJECT_ID,
            parent_requirement="REQ.checkout",
            expected_state="SPRINT_SETUP",
            idempotency_key="reopen-story-1",
        )["data"]["fsm_state"]
        == "STORY_INTERVIEW"
    )
    assert app.story_repair_readiness(
        project_id=PROJECT_ID,
        expected_state="SPRINT_SETUP",
        idempotency_key="repair-story-readiness-7",
    )["data"]["repair_result"] == {"repaired_count": 1, "story_ids": [66]}
    assert runner.calls == [
        ("pending", {"project_id": PROJECT_ID}),
        (
            "generate",
            {
                "project_id": PROJECT_ID,
                "parent_requirement": "REQ.checkout",
                "user_input": "focus payment errors",
            },
        ),
        (
            "retry",
            {
                "project_id": PROJECT_ID,
                "parent_requirement": "REQ.checkout",
            },
        ),
        (
            "history",
            {
                "project_id": PROJECT_ID,
                "parent_requirement": "REQ.checkout",
            },
        ),
        (
            "save",
            {
                "project_id": PROJECT_ID,
                "parent_requirement": "REQ.checkout",
                "attempt_id": "story-attempt-1",
                "expected_artifact_fingerprint": "sha256:" + "a" * 64,
                "expected_state": "STORY_REVIEW",
                "idempotency_key": "save-story-1",
            },
        ),
        (
            "complete",
            {
                "project_id": PROJECT_ID,
                "expected_state": "STORY_PERSISTENCE",
                "idempotency_key": "complete-story-1",
            },
        ),
        (
            "reopen",
            {
                "project_id": PROJECT_ID,
                "parent_requirement": "REQ.checkout",
                "expected_state": "SPRINT_SETUP",
                "idempotency_key": "reopen-story-1",
            },
        ),
        (
            "repair_readiness",
            {
                "project_id": PROJECT_ID,
                "expected_state": "SPRINT_SETUP",
                "idempotency_key": "repair-story-readiness-7",
            },
        ),
    ]


def test_application_routes_sprint_execution_commands_to_runner() -> None:
    """Verify Sprint execution facade methods delegate to the configured runner."""
    runner = _FakeSprintRunner()
    app = AgentWorkbenchApplication(sprint_runner=cast("Any", runner))

    assert (
        app.sprint_start(
            project_id=PROJECT_ID,
            sprint_id=None,
            expected_state="SPRINT_PERSISTENCE",
            idempotency_key="start-sprint-7-001",
        )["data"]["sprint_id"]
        == SPRINT_ID
    )
    assert app.sprint_status(project_id=PROJECT_ID)["data"]["sprint_id"] == SPRINT_ID
    assert app.sprint_tasks(project_id=PROJECT_ID)["data"]["tasks"] == []
    assert app.sprint_task_next(project_id=PROJECT_ID)["data"]["task_ticket"] is None
    assert (
        app.sprint_task_show(project_id=PROJECT_ID, task_id=TASK_ID)["data"][
            "task_ticket"
        ]["task_id"]
        == TASK_ID
    )
    assert app.sprint_task_history(project_id=PROJECT_ID, task_id=TASK_ID)["data"][
        "execution"
    ] == {"history": []}
    assert (
        app.sprint_task_update(
            project_id=PROJECT_ID,
            task_id=TASK_ID,
            status="In Progress",
            expected_status="To Do",
            expected_task_fingerprint="sha256:abc",
            idempotency_key="task-update-123-001",
            notes="Starting work.",
        )["data"]["task_id"]
        == TASK_ID
    )
    assert (
        app.sprint_story_readiness(
            project_id=PROJECT_ID,
            story_id=STORY_ID,
        )["data"]["story_id"]
        == STORY_ID
    )
    assert (
        app.sprint_story_close(
            project_id=PROJECT_ID,
            story_id=STORY_ID,
            expected_status="To Do",
            expected_story_fingerprint="sha256:story",
            idempotency_key="close-story-12-001",
            resolution="Completed",
            completion_notes="Story complete.",
            evidence_links=["scripts/run_live_round.py"],
        )["data"]["story_id"]
        == STORY_ID
    )
    assert app.sprint_close_readiness(project_id=PROJECT_ID)["data"]["sprint_id"] == (
        SPRINT_ID
    )
    assert (
        app.sprint_close(
            project_id=PROJECT_ID,
            expected_state="SPRINT_VIEW",
            expected_status="Active",
            expected_sprint_fingerprint="sha256:sprint",
            idempotency_key="close-sprint-7-001",
            completion_notes="Sprint complete.",
            follow_up_notes="Prepare the next sprint.",
        )["data"]["sprint_id"]
        == SPRINT_ID
    )
    assert runner.calls == [
        (
            "start",
            {
                "project_id": PROJECT_ID,
                "sprint_id": None,
                "expected_state": "SPRINT_PERSISTENCE",
                "idempotency_key": "start-sprint-7-001",
            },
        ),
        ("status", {"project_id": PROJECT_ID, "sprint_id": None}),
        ("tasks", {"project_id": PROJECT_ID, "sprint_id": None}),
        ("task_next", {"project_id": PROJECT_ID, "sprint_id": None}),
        (
            "task_show",
            {"project_id": PROJECT_ID, "task_id": TASK_ID, "sprint_id": None},
        ),
        (
            "task_history",
            {"project_id": PROJECT_ID, "task_id": TASK_ID, "sprint_id": None},
        ),
        (
            "task_update",
            {
                "project_id": PROJECT_ID,
                "task_id": TASK_ID,
                "status": "In Progress",
                "expected_status": "To Do",
                "expected_task_fingerprint": "sha256:abc",
                "idempotency_key": "task-update-123-001",
                "sprint_id": None,
                "outcome_summary": None,
                "artifact_refs": None,
                "checklist_result": None,
                "validation_summary": None,
                "notes": "Starting work.",
                "changed_by": "cli-agent",
            },
        ),
        (
            "story_readiness",
            {"project_id": PROJECT_ID, "story_id": STORY_ID, "sprint_id": None},
        ),
        (
            "story_close",
            {
                "project_id": PROJECT_ID,
                "story_id": STORY_ID,
                "expected_status": "To Do",
                "expected_story_fingerprint": "sha256:story",
                "idempotency_key": "close-story-12-001",
                "resolution": "Completed",
                "completion_notes": "Story complete.",
                "evidence_links": ["scripts/run_live_round.py"],
                "sprint_id": None,
                "changed_by": "cli-agent",
            },
        ),
        ("close_readiness", {"project_id": PROJECT_ID, "sprint_id": None}),
        (
            "close",
            {
                "project_id": PROJECT_ID,
                "expected_state": "SPRINT_VIEW",
                "expected_status": "Active",
                "expected_sprint_fingerprint": "sha256:sprint",
                "idempotency_key": "close-sprint-7-001",
                "completion_notes": "Sprint complete.",
                "follow_up_notes": "Prepare the next sprint.",
                "sprint_id": None,
                "changed_by": "cli-agent",
            },
        ),
    ]


def test_application_keeps_falsey_injected_dependencies() -> None:
    """Verify explicit None checks preserve falsey injected projections."""
    app = AgentWorkbenchApplication(
        read_projection=_FalseyReadProjection(),
        authority_projection=_FalseyAuthorityProjection(),
    )

    assert app.project_list()["data"] == {"sentinel": "falsey-read"}
    assert app.authority_status(project_id=PROJECT_ID)["data"]["status"] == (
        "falsey-authority"
    )


def test_application_context_pack_facade_composes_sprint_planning_pack() -> None:
    """Verify context pack facade returns bounded sprint-planning data."""
    app = AgentWorkbenchApplication(
        read_projection=_SprintReadyReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.context_pack(project_id=PROJECT_ID, phase="sprint-planning")

    assert result["ok"] is True
    data = result["data"]
    assert data["phase"] == "sprint-planning"
    assert data["included_sections"] == [
        "workflow",
        "authority",
        "sprint_candidates",
    ]
    assert data["next_valid_commands"] == [
        "agileforge sprint candidates --project-id 7",
        "agileforge sprint generate --project-id 7",
    ]
    assert data["blocked_commands"] == []
    assert data["blocked_future_commands"] == []


def test_application_status_combines_project_workflow_and_authority() -> None:
    """Verify status facade combines orientation projections."""
    app = AgentWorkbenchApplication(
        read_projection=_FakeReadProjection(),
        authority_projection=_FakeAuthorityProjection(),
    )

    result = app.status(project_id=PROJECT_ID)

    assert result == {
        "ok": True,
        "data": {
            "project": {
                "project_id": PROJECT_ID,
                "name": "Workbench",
                "source_fingerprint": PROJECT_FINGERPRINT,
            },
            "workflow": {
                "project_id": PROJECT_ID,
                "state": {},
                "source_fingerprint": WORKFLOW_FINGERPRINT,
            },
            "authority": {
                "project_id": PROJECT_ID,
                "status": "missing",
                "authority_fingerprint": AUTHORITY_FINGERPRINT,
            },
            "source_fingerprint": result["data"]["source_fingerprint"],
        },
        "warnings": [],
        "errors": [],
    }
    assert result["data"]["source_fingerprint"].startswith("sha256:")


def test_application_status_fingerprint_changes_with_child_inputs() -> None:
    """Verify status source fingerprint includes child fingerprints."""
    first = AgentWorkbenchApplication(
        read_projection=_FakeReadProjection(),
        authority_projection=_FakeAuthorityProjection(),
    ).status(project_id=PROJECT_ID)
    changed = AgentWorkbenchApplication(
        read_projection=_ChangedProjectReadProjection(),
        authority_projection=_FakeAuthorityProjection(),
    ).status(project_id=PROJECT_ID)

    assert first["data"]["source_fingerprint"].startswith("sha256:")
    assert changed["data"]["source_fingerprint"].startswith("sha256:")
    assert first["data"]["source_fingerprint"] != changed["data"]["source_fingerprint"]


def test_application_workflow_next_routes_vision_interview_to_generate() -> None:
    """Expose installed Vision generation while in Vision interview."""
    app = AgentWorkbenchApplication(
        read_projection=_VisionInterviewReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge vision generate --project-id 7"
    ]
    assert result["data"]["blocked_commands"] == []


def test_application_workflow_next_routes_vision_review_to_save_and_refine() -> None:
    """Expose installed Vision save and refinement commands in Vision review."""
    app = AgentWorkbenchApplication(
        read_projection=_VisionReviewReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge vision save --project-id 7",
        "agileforge vision generate --project-id 7 --input <feedback>",
    ]
    assert result["data"]["blocked_commands"] == []


def test_application_workflow_next_routes_vision_persistence_to_backlog() -> None:
    """Expose installed Backlog generation after Vision has been saved."""
    app = AgentWorkbenchApplication(
        read_projection=_VisionPersistenceReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge backlog generate --project-id 7"
    ]
    assert result["data"]["blocked_future_commands"] == []
    assert result["data"]["status"] == "next_phase_available"


def test_application_workflow_next_routes_backlog_interview_to_generate() -> None:
    """Expose installed Backlog generation while in Backlog interview."""
    app = AgentWorkbenchApplication(
        read_projection=_BacklogInterviewReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge backlog generate --project-id 7"
    ]
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["blocked_future_commands"] == []
    assert result["data"]["status"] == "next_phase_available"


def test_application_workflow_next_routes_backlog_review_to_save_and_refine() -> None:
    """Expose installed Backlog save and refinement commands in Backlog review."""
    app = AgentWorkbenchApplication(
        read_projection=_BacklogReviewReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        (
            "agileforge backlog save --project-id 7 "
            "--attempt-id <attempt_id> "
            "--expected-artifact-fingerprint <artifact_fingerprint> "
            "--expected-state BACKLOG_REVIEW "
            "--idempotency-key <idempotency_key>"
        ),
        "agileforge backlog generate --project-id 7 --input <feedback>",
    ]
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["blocked_future_commands"] == []


def test_application_workflow_next_reports_roadmap_after_backlog_save() -> None:
    """Expose installed Roadmap generation after Backlog has been saved."""
    app = AgentWorkbenchApplication(
        read_projection=_BacklogPersistenceReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge roadmap generate --project-id 7"
    ]
    assert result["data"]["blocked_future_commands"] == []
    assert result["data"]["status"] == "next_phase_available"


def test_application_workflow_next_routes_active_reset_to_roadmap_only() -> None:
    """Expose roadmap regeneration while documenting Story/Sprint stale dead-end."""
    app = AgentWorkbenchApplication(
        read_projection=_BacklogPersistenceActiveResetReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge roadmap generate --project-id 7"
    ]
    assert result["data"]["status"] == (
        "active_backlog_reset_requires_roadmap_regeneration"
    )
    assert result["data"]["blocked_commands"] == [
        {
            "command": "agileforge story generate",
            "reason": "DOWNSTREAM_BACKLOG_STALE_AFTER_ACTIVE_RESET",
            "message": (
                "Story generation remains blocked until downstream reset-stale "
                "clearing exists."
            ),
        },
        {
            "command": "agileforge sprint save",
            "reason": "DOWNSTREAM_BACKLOG_STALE_AFTER_ACTIVE_RESET",
            "message": (
                "Sprint generation remains blocked until downstream reset-stale "
                "clearing exists."
            ),
        },
    ]
    assert result["data"]["blocked_future_commands"] == []


def test_application_workflow_next_routes_roadmap_interview_to_generate() -> None:
    """Expose installed Roadmap generation while in Roadmap interview."""
    app = AgentWorkbenchApplication(
        read_projection=_RoadmapInterviewReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge roadmap generate --project-id 7"
    ]
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["blocked_future_commands"] == []
    assert result["data"]["status"] == "next_phase_available"


def test_application_workflow_next_routes_roadmap_review_to_save_and_refine() -> None:
    """Expose installed Roadmap save and refinement commands in Roadmap review."""
    app = AgentWorkbenchApplication(
        read_projection=_RoadmapReviewReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        (
            "agileforge roadmap save --project-id 7 "
            "--attempt-id <attempt_id> "
            "--expected-artifact-fingerprint <artifact_fingerprint> "
            "--expected-state ROADMAP_REVIEW "
            "--idempotency-key <idempotency_key>"
        ),
        "agileforge roadmap generate --project-id 7 --input <feedback>",
    ]
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["blocked_future_commands"] == []


def test_application_workflow_next_routes_story_after_roadmap_save() -> None:
    """Roadmap persistence should point to installed Story commands."""
    app = AgentWorkbenchApplication(
        read_projection=_RoadmapPersistenceReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge story pending --project-id 7",
        (
            "agileforge story generate --project-id 7 "
            "--parent-requirement <parent_requirement>"
        ),
    ]
    assert result["data"]["blocked_future_commands"] == []
    assert result["data"]["status"] == "next_phase_available"


def test_application_workflow_next_routes_story_interview_to_pending_and_generate() -> (
    None
):
    """Expose installed Story pending and generation commands in Story interview."""
    app = AgentWorkbenchApplication(
        read_projection=_StoryInterviewReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge story pending --project-id 7",
        (
            "agileforge story generate --project-id 7 "
            "--parent-requirement <parent_requirement>"
        ),
    ]
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["blocked_future_commands"] == []
    assert result["data"]["status"] == "next_phase_available"


def test_workflow_next_routes_reopened_story_to_generate_not_sprint() -> None:
    """Route reopened Story correction to Story commands instead of Sprint."""
    app = AgentWorkbenchApplication(
        read_projection=_StoryReopenedReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge story pending --project-id 7",
        (
            "agileforge story generate --project-id 7 "
            "--parent-requirement <parent_requirement>"
        ),
    ]


def test_application_workflow_next_routes_story_review_to_history_save_and_refine() -> (
    None
):
    """Expose installed Story review commands without uninstalled messaging."""
    app = AgentWorkbenchApplication(
        read_projection=_StoryReviewReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        (
            "agileforge story history --project-id 7 "
            "--parent-requirement <parent_requirement>"
        ),
        (
            "agileforge story save --project-id 7 "
            "--parent-requirement <parent_requirement> "
            "--attempt-id <attempt_id> "
            "--expected-artifact-fingerprint <artifact_fingerprint> "
            "--expected-state STORY_REVIEW "
            "--idempotency-key <idempotency_key>"
        ),
        (
            "agileforge story generate --project-id 7 "
            "--parent-requirement <parent_requirement> "
            "--input <feedback>"
        ),
    ]
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["blocked_future_commands"] == []


def test_application_workflow_next_routes_story_persistence_to_next_pending_story() -> (
    None
):
    """Keep Story completion hidden until every Roadmap requirement is covered."""
    app = AgentWorkbenchApplication(
        read_projection=_StoryPersistenceReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge story pending --project-id 7",
        (
            "agileforge story generate --project-id 7 "
            "--parent-requirement <parent_requirement>"
        ),
    ]
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["blocked_future_commands"] == []


def test_workflow_next_routes_story_persistence_to_complete_when_covered() -> None:
    """Expose Story completion only after all Roadmap requirements are covered."""
    app = AgentWorkbenchApplication(
        read_projection=_StoryCompleteReadyReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge story pending --project-id 7",
        "agileforge story dependencies inspect --project-id 7",
        (
            "agileforge story dependencies propose --project-id 7 "
            "--expected-state STORY_PERSISTENCE "
            "--idempotency-key <idempotency_key>"
        ),
        (
            "agileforge story dependencies apply --project-id 7 "
            "--attempt-id <attempt_id> "
            "--expected-artifact-fingerprint <artifact_fingerprint> "
            "--expected-state STORY_PERSISTENCE "
            "--idempotency-key <idempotency_key>"
        ),
        (
            "agileforge story complete --project-id 7 "
            "--expected-state STORY_PERSISTENCE "
            "--idempotency-key <idempotency_key>"
        ),
    ]
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["blocked_future_commands"] == []


def test_application_workflow_next_derives_from_sprint_planning_pack() -> None:
    """Verify workflow next facade exposes Sprint setup commands."""
    app = AgentWorkbenchApplication(
        read_projection=_SprintReadyReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result == {
        "ok": True,
        "data": {
            "project_id": PROJECT_ID,
            "next_valid_commands": [
                "agileforge story dependencies inspect --project-id 7",
                (
                    "agileforge story dependencies propose --project-id 7 "
                    "--expected-state SPRINT_SETUP "
                    "--idempotency-key <idempotency_key>"
                ),
                (
                    "agileforge story dependencies apply --project-id 7 "
                    "--attempt-id <attempt_id> "
                    "--expected-artifact-fingerprint <artifact_fingerprint> "
                    "--expected-state SPRINT_SETUP "
                    "--idempotency-key <idempotency_key>"
                ),
                "agileforge sprint candidates --project-id 7",
                "agileforge sprint generate --project-id 7",
            ],
            "blocked_commands": [],
            "blocked_future_commands": [],
            "status": "next_phase_available",
            "source_fingerprint": result["data"]["source_fingerprint"],
        },
        "warnings": [],
        "errors": [],
    }
    assert result["data"]["source_fingerprint"].startswith("sha256:")


def test_workflow_next_routes_sprint_draft_to_guarded_save() -> None:
    """Expose Sprint review commands after a complete Sprint draft exists."""
    app = AgentWorkbenchApplication(
        read_projection=_SprintDraftReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge sprint history --project-id 7",
        "agileforge story dependencies inspect --project-id 7",
        (
            "agileforge story dependencies propose --project-id 7 "
            "--expected-state SPRINT_DRAFT "
            "--idempotency-key <idempotency_key>"
        ),
        (
            "agileforge story dependencies apply --project-id 7 "
            "--attempt-id <attempt_id> "
            "--expected-artifact-fingerprint <artifact_fingerprint> "
            "--expected-state SPRINT_DRAFT "
            "--idempotency-key <idempotency_key>"
        ),
        (
            "agileforge sprint save --project-id 7 "
            "--team-name <team_name> "
            "--sprint-start-date <YYYY-MM-DD> "
            "--attempt-id <attempt_id> "
            "--expected-artifact-fingerprint <artifact_fingerprint> "
            "--expected-state SPRINT_DRAFT "
            "--idempotency-key <idempotency_key>"
        ),
        "agileforge sprint generate --project-id 7 --input <feedback>",
    ]
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["blocked_future_commands"] == []


def test_workflow_next_hides_sprint_save_after_latest_failed_attempt() -> None:
    """Workflow next must not advertise saving an older complete draft."""

    class _LatestFailedSprintDraftReadProjection(_FakeReadProjection):
        """Fake read projection for a stale reviewed Sprint draft state."""

        def workflow_state(self, *, project_id: int) -> dict[str, Any]:
            """Return Sprint draft workflow state with a newer failed attempt."""
            result = super().workflow_state(project_id=project_id)
            result["data"]["state"] = {
                "fsm_state": "SPRINT_DRAFT",
                "setup_status": "passed",
                "sprint_attempts": [
                    {
                        "attempt_id": "sprint-attempt-4",
                        "artifact_fingerprint": "sha256:reviewed",
                        "is_complete": True,
                    },
                    {
                        "attempt_id": "sprint-attempt-5",
                        "artifact_fingerprint": "sha256:failed",
                        "is_complete": False,
                        "failure_stage": "invocation_exception",
                    },
                ],
                "sprint_plan_assessment": {
                    "attempt_id": "sprint-attempt-4",
                    "artifact_fingerprint": "sha256:reviewed",
                    "is_complete": True,
                },
            }
            return result

    app = AgentWorkbenchApplication(
        read_projection=_LatestFailedSprintDraftReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    commands = result["data"]["next_valid_commands"]
    assert any(command.startswith("agileforge sprint generate") for command in commands)
    assert not any(command.startswith("agileforge sprint save") for command in commands)
    assert result["data"]["blocked_commands"] == [
        {
            "command": "agileforge sprint save",
            "reason_code": "SPRINT_DRAFT_NOT_LATEST_COMPLETE",
            "details": {
                "latest_attempt_id": "sprint-attempt-5",
                "draft_attempt_id": "sprint-attempt-4",
            },
        }
    ]


def test_workflow_next_routes_sprint_persistence_to_guarded_start() -> None:
    """Expose guarded Sprint start after a Sprint has been saved."""
    app = AgentWorkbenchApplication(
        read_projection=_SprintPersistenceReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        (
            "agileforge sprint start --project-id 7 "
            "--expected-state SPRINT_PERSISTENCE "
            "--idempotency-key <idempotency_key>"
        ),
        "agileforge sprint history --project-id 7",
    ]
    assert result["data"]["blocked_commands"] == []


def test_workflow_next_routes_sprint_view_to_execution_commands() -> None:
    """Expose execution commands once Sprint start syncs to SPRINT_VIEW."""
    app = AgentWorkbenchApplication(
        read_projection=_SprintViewReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge sprint task next --project-id 7",
        "agileforge sprint status --project-id 7",
        "agileforge sprint tasks --project-id 7",
        "agileforge sprint task show --project-id 7 --task-id <task_id>",
        (
            "agileforge sprint task update --project-id 7 "
            "--task-id <task_id> --status <status> "
            "--expected-status <expected_status> "
            "--expected-task-fingerprint <task_fingerprint> "
            "--idempotency-key <idempotency_key>"
        ),
        ("agileforge sprint story readiness --project-id 7 --story-id <story_id>"),
        (
            "agileforge sprint story close --project-id 7 "
            "--story-id <story_id> --expected-status <expected_status> "
            "--expected-story-fingerprint <story_fingerprint> "
            "--idempotency-key <idempotency_key> "
            "--resolution Completed --completion-notes <notes>"
        ),
        "agileforge sprint close-readiness --project-id 7",
        (
            "agileforge sprint close --project-id 7 "
            "--expected-state SPRINT_VIEW "
            "--expected-status Active "
            "--expected-sprint-fingerprint <sprint_fingerprint> "
            "--idempotency-key <idempotency_key> "
            "--completion-notes <notes>"
        ),
        "agileforge story dependencies inspect --project-id 7",
        (
            "agileforge story dependencies propose --project-id 7 "
            "--expected-state SPRINT_VIEW "
            "--idempotency-key <idempotency_key>"
        ),
        (
            "agileforge story dependencies apply --project-id 7 "
            "--attempt-id <attempt_id> "
            "--expected-artifact-fingerprint <artifact_fingerprint> "
            "--expected-state SPRINT_VIEW "
            "--idempotency-key <idempotency_key>"
        ),
        "agileforge sprint history --project-id 7",
    ]
    assert result["data"]["blocked_commands"] == []


def test_workflow_next_does_not_route_sprint_complete_to_empty_history() -> None:
    """Avoid advertising draft history as the only command after Sprint close."""
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == []
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["blocked_future_commands"] == []
    assert result["data"]["status"] == "sprint_complete"


def test_workflow_next_routes_sprint_complete_backlog_attempt_to_refinement() -> None:
    """Expose review-safe Backlog refinement commands after Sprint close."""
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteWithBacklogAttemptsReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        (
            "agileforge backlog refine-preview --project-id 7 "
            "--source-attempt-id backlog-attempt-2 "
            "--operations-file <operations_file>"
        ),
        (
            "agileforge backlog refine-record --project-id 7 "
            "--source-attempt-id backlog-attempt-2 "
            "--operations-file <operations_file> "
            "--expected-source-fingerprint sha256:latest-source "
            "--expected-state SPRINT_COMPLETE "
            "--idempotency-key <idempotency_key>"
        ),
        (
            "agileforge backlog refine-import --project-id 7 "
            "--source-artifact <source_artifact> "
            "--edited-file <edited_file> "
            "--expected-source-fingerprint sha256:latest-source "
            "--idempotency-key <idempotency_key>"
        ),
    ]
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["blocked_future_commands"] == []
    all_commands = (
        result["data"]["next_valid_commands"]
        + result["data"]["blocked_commands"]
        + result["data"]["blocked_future_commands"]
    )
    assert not any(
        command.startswith("agileforge backlog save") for command in all_commands
    )
    assert result["data"]["status"] == "sprint_complete_backlog_refinement_available"


def test_workflow_next_routes_pending_authority_to_review_and_decision_templates() -> (
    None
):
    """Route setup pending review to authority review before decision commands."""
    review = _FakeAuthorityReview()
    app = AgentWorkbenchApplication(
        read_projection=_AuthorityPendingReviewReadProjection(),
        authority_projection=_FakeAuthorityProjection(),
        authority_review=review,
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == []
    assert len(result["data"]["next_actions"]) == 1
    command_text = result["data"]["next_actions"][0]["command"]
    assert "agileforge authority review --project-id 7 --open" in command_text
    assert result["data"]["next_actions"][0]["installed"] is True
    assert result["data"]["next_actions"][0]["requires_cli_installation"] is False
    assert (
        result["data"]["next_actions"][0]["reason"]
        == "Review pending authority before accepting or rejecting it."
    )

    decision_actions = result["data"]["decision_actions_after_review"]
    accept_action, reject_action = decision_actions
    command_text = accept_action["command"]
    assert "agileforge authority accept --project-id 7" in command_text
    assert "--review-token" not in command_text
    assert "--idempotency-key" not in command_text
    assert accept_action["installed"] is True
    assert accept_action["requires_cli_installation"] is False
    assert accept_action["after_review"] is True
    assert accept_action["requires"] == []
    assert (
        accept_action["reason"] == "Record accepted authority only after review passes."
    )
    assert reject_action == {
        "command": (
            "agileforge authority reject --project-id 7 "
            "--review-token <review_token> "
            "--reason <reason> --idempotency-key <idempotency_key>"
        ),
        "installed": True,
        "requires_cli_installation": False,
        "after_review": True,
        "requires": ["review_token", "reason", "idempotency_key"],
    }
    assert review.calls == [
        {
            "project_id": PROJECT_ID,
            "include_spec": "summary",
            "output_format": "json",
        }
    ]
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["blocked_future_commands"] == []
    assert result["data"]["source_fingerprint"].startswith("sha256:")


def test_workflow_next_marks_accept_blocked_when_review_has_blocking_findings() -> None:
    """Pending authority accept action should reflect incomplete review gating."""
    review = _FakeAuthorityReview(
        response={
            "ok": True,
            "data": {
                "review_summary": {
                    "acceptance_status": "blocked",
                    "blocking_finding_count": 2,
                    "blocking_finding_codes": [
                        "AUTHORITY_CANDIDATE_UNCOVERED",
                        "AUTHORITY_REVIEW_PACKET_TRUNCATED",
                    ],
                    "overrideable_blocking_finding_count": 1,
                    "non_overrideable_blocking_finding_count": 1,
                    "packet_truncated": True,
                }
            },
            "warnings": [],
            "errors": [],
        }
    )
    app = AgentWorkbenchApplication(
        read_projection=_AuthorityPendingReviewReadProjection(),
        authority_projection=_FakeAuthorityProjection(),
        authority_review=review,
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert review.calls == [
        {
            "project_id": PROJECT_ID,
            "include_spec": "summary",
            "output_format": "json",
        }
    ]
    accept_action = result["data"]["decision_actions_after_review"][0]
    assert accept_action["blocked"] is True
    assert accept_action["requires"] == ["fatal_review_resolution"]
    assert accept_action["review_summary"]["acceptance_status"] == "blocked"
    assert "AUTHORITY_REVIEW_PACKET_TRUNCATED" in accept_action["reason"]


def test_workflow_next_routes_rejected_authority_to_manual_recompile_remediation() -> (
    None
):
    """Do not publish an uninstalled recompile command as an actionable next step."""
    app = AgentWorkbenchApplication(
        read_projection=_AuthorityRejectedReadProjection(),
        authority_projection=_FakeAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == []
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["next_actions"] == []
    assert result["data"]["blocked_future_commands"] == [
        {
            "command": (
                "agileforge project spec update --project-id 7 "
                "--spec-file <updated-spec-file>"
            ),
            "installed": False,
            "reason": (
                "Spec update/recompile is required after authority rejection, "
                "but this command is not installed yet."
            ),
        }
    ]
    assert result["data"]["manual_remediation"] == [
        "No installed CLI command can recompile a rejected authority yet.",
        (
            "Revise the spec or compiler, then run the future project spec "
            "update command when installed."
        ),
    ]


def test_workflow_next_no_longer_calls_sprint_context_pack_when_setup_pending_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep authority review setup routing independent from sprint context packs."""
    app = AgentWorkbenchApplication(
        read_projection=_AuthorityPendingReviewReadProjection(),
        authority_projection=_FakeAuthorityProjection(),
    )

    def forbidden_context_pack(
        *,
        project_id: int,
        phase: str = "overview",
    ) -> NoReturn:
        del project_id, phase
        message = "workflow_next must not call context_pack"
        raise AssertionError(message)

    monkeypatch.setattr(app, "context_pack", forbidden_context_pack)

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == []
    assert result["data"]["next_actions"][0]["command"] == (
        "agileforge authority review --project-id 7 --open"
    )


def test_workflow_next_pending_review_emits_installed_authority_command() -> None:
    """Send agents to installed authority CLI commands after parser wiring."""
    app = AgentWorkbenchApplication(
        read_projection=_AuthorityPendingReviewReadProjection(),
        authority_projection=_FakeAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == []
    assert result["data"]["next_actions"][0]["installed"] is True
    assert result["data"]["next_actions"][0]["requires_cli_installation"] is False


def test_workflow_next_no_longer_calls_sprint_context_pack_when_authority_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep rejected authority setup routing independent from sprint context packs."""
    app = AgentWorkbenchApplication(
        read_projection=_AuthorityRejectedReadProjection(),
        authority_projection=_FakeAuthorityProjection(),
    )

    def forbidden_context_pack(
        *,
        project_id: int,
        phase: str = "overview",
    ) -> NoReturn:
        del project_id, phase
        message = "workflow_next must not call context_pack"
        raise AssertionError(message)

    monkeypatch.setattr(app, "context_pack", forbidden_context_pack)

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_actions"] == []
    assert result["data"]["blocked_future_commands"][0]["installed"] is False


def test_workflow_next_routes_failed_setup_to_retry_action() -> None:
    """Route failed setup to setup retry instead of sprint planning."""
    app = AgentWorkbenchApplication(
        read_projection=_SetupFailedReadProjection(),
        authority_projection=_FakeAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        (
            "agileforge project setup retry --project-id 7 "
            "--spec-file <spec-file> --expected-state SETUP_REQUIRED "
            "--expected-context-fingerprint <expected_context_fingerprint>"
        )
    ]


def test_workflow_next_failed_setup_does_not_require_authority_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep failed setup retry routing available without authority projection."""
    app = AgentWorkbenchApplication(read_projection=_SetupFailedReadProjection())

    def forbidden_authority_status(*, project_id: int) -> NoReturn:
        del project_id
        message = "failed setup retry should not require authority status"
        raise AssertionError(message)

    monkeypatch.setattr(app, "authority_status", forbidden_authority_status)

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        (
            "agileforge project setup retry --project-id 7 "
            "--spec-file <spec-file> --expected-state SETUP_REQUIRED "
            "--expected-context-fingerprint <expected_context_fingerprint>"
        )
    ]


def test_application_workflow_next_fingerprint_changes_with_workflow_inputs() -> None:
    """Verify workflow next fingerprint includes Sprint workflow inputs."""
    first = AgentWorkbenchApplication(
        read_projection=_SprintReadyReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    ).workflow_next(project_id=PROJECT_ID)
    changed = AgentWorkbenchApplication(
        read_projection=_ChangedSprintWorkflowReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    ).workflow_next(project_id=PROJECT_ID)

    assert first["data"]["source_fingerprint"].startswith("sha256:")
    assert changed["data"]["source_fingerprint"].startswith("sha256:")
    assert first["data"]["source_fingerprint"] != changed["data"]["source_fingerprint"]


def test_application_diagnostics_facades_return_envelopes(engine: Engine) -> None:
    """Expose diagnostics payloads through application envelopes."""
    ensure_schema_current(engine)
    app = AgentWorkbenchApplication(
        read_projection=_FakeReadProjection(),
        authority_projection=_FakeAuthorityProjection(),
    )

    doctor = app.doctor(business_engine=engine, session_db_url="sqlite:///:memory:")
    schema_check = app.schema_check(
        business_engine=engine,
        session_db_url="sqlite:///:memory:",
    )

    assert doctor["ok"] is True
    assert doctor["warnings"] == []
    assert doctor["errors"] == []
    assert doctor["data"]["central_repo_root"]["status"] == "ok"
    assert doctor["data"]["caller_cwd"]["status"] == "ok"

    assert schema_check == {
        "ok": True,
        "data": schema_check["data"],
        "warnings": [],
        "errors": [],
    }
    assert schema_check["data"]["business_db"]["required_version"] == (
        STORAGE_SCHEMA_VERSION
    )
    assert schema_check["data"]["business_db"]["status"] == "ok"


def test_default_application_doctor_does_not_initialize_read_projections(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow injected diagnostics to run without default read projection DB access."""
    ensure_schema_current(engine)

    def guarded_get_engine() -> Engine:
        message = "default projection DB access should be lazy"
        raise AssertionError(message)

    monkeypatch.setattr(model_db, "get_engine", guarded_get_engine)

    result = AgentWorkbenchApplication().doctor(
        business_engine=engine,
        session_db_url="sqlite:///:memory:",
    )

    assert result["ok"] is True
    assert result["data"]["business_db"]["status"] == "ok"


def test_application_contract_facades_return_envelopes() -> None:
    """Expose capabilities and command schema through application envelopes."""
    app = AgentWorkbenchApplication(
        read_projection=_FakeReadProjection(),
        authority_projection=_FakeAuthorityProjection(),
    )

    capabilities = app.capabilities()
    command_schema = app.command_schema("agileforge status")

    assert capabilities["ok"] is True
    assert capabilities["warnings"] == []
    assert capabilities["errors"] == []
    assert capabilities["data"]["installed_command_count"] >= 1

    assert command_schema == {
        "ok": True,
        "data": command_schema["data"],
        "warnings": [],
        "errors": [],
    }
    assert command_schema["data"]["name"] == "agileforge status"


def test_application_unknown_command_schema_uses_registered_error() -> None:
    """Unknown command schema requests use registry-backed error metadata."""
    app = AgentWorkbenchApplication(
        read_projection=_FakeReadProjection(),
        authority_projection=_FakeAuthorityProjection(),
    )

    result = app.command_schema("agileforge not installed")

    assert result["ok"] is False
    assert result["data"] == {}
    assert result["warnings"] == []
    assert result["errors"] == [
        {
            "code": "COMMAND_NOT_IMPLEMENTED",
            "message": "Unknown command: agileforge not installed",
            "details": {"command_name": "agileforge not installed"},
            "remediation": ["agileforge capabilities"],
            "exit_code": 2,
            "retryable": False,
        }
    ]


def test_application_mutation_facades_return_ledger_envelopes(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose mutation ledger operational methods through the facade."""
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(application_mod, "get_engine", lambda: engine, raising=False)
    repo = MutationLedgerRepository(engine=engine)
    row = repo.create_or_load(
        command="agileforge fake mutate",
        idempotency_key="fake-key-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=datetime(2026, 5, 15, 12, 0, tzinfo=UTC),
        lease_seconds=1,
    ).ledger
    assert row.mutation_event_id is not None
    repo._force_recovery_required_for_test(
        mutation_event_id=row.mutation_event_id,
        recovery_action=RecoveryAction.RESUME_FROM_STEP,
        safe_to_auto_resume=True,
        last_error={"code": "CRASHED"},
        now=datetime(2026, 5, 15, 12, 0, 2, tzinfo=UTC),
    )
    app = AgentWorkbenchApplication()

    show = app.mutation_show(mutation_event_id=row.mutation_event_id)
    listed = app.mutation_list(project_id=PROJECT_ID, status="recovery_required")
    resumed = app.mutation_resume(
        mutation_event_id=row.mutation_event_id,
        correlation_id="corr-resume",
    )

    assert show["ok"] is True
    assert show["data"]["mutation_event_id"] == row.mutation_event_id
    assert listed["ok"] is True
    assert listed["data"]["items"][0]["mutation_event_id"] == row.mutation_event_id
    assert resumed["ok"] is True
    assert resumed["data"]["status"] == MutationStatus.PENDING.value
    assert resumed["data"]["recovery"]["domain_resume_required"] is True


def test_mutation_ledger_repository_replays_superseded_responses(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Superseded rows are visible and replay their stored response."""
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(application_mod, "get_engine", lambda: engine, raising=False)
    repo = MutationLedgerRepository(engine=engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge project create",
        idempotency_key="project-create-key",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=30,
    ).ledger
    assert row.mutation_event_id is not None
    retry_event_id = row.mutation_event_id + 1
    response = {
        "project_id": PROJECT_ID,
        "mutation_event_id": row.mutation_event_id,
        "recovered_by_mutation_event_id": retry_event_id,
        "setup_status": "authority_pending_review",
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE cli_mutation_ledger
                SET status = 'superseded',
                    superseded_by_mutation_event_id = :retry_event_id,
                    response_json = :response_json
                WHERE mutation_event_id = :mutation_event_id
                """
            ),
            {
                "mutation_event_id": row.mutation_event_id,
                "retry_event_id": retry_event_id,
                "response_json": json.dumps(
                    response,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        )
    app = AgentWorkbenchApplication()

    show = app.mutation_show(mutation_event_id=row.mutation_event_id)
    listed = app.mutation_list(project_id=PROJECT_ID, status="superseded")
    replay = repo.create_or_load(
        command="agileforge project create",
        idempotency_key="project-create-key",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-2",
        changed_by="cli-agent",
        lease_owner="worker-2",
        now=now,
    )

    assert show["ok"] is True
    assert show["data"]["status"] == "superseded"
    assert show["data"]["superseded_by_mutation_event_id"] == retry_event_id
    assert show["data"]["response"] == response
    assert listed["ok"] is True
    assert listed["data"]["items"][0]["mutation_event_id"] == row.mutation_event_id
    assert replay.replayed is True
    assert replay.error_code is None
    assert replay.response == response


def test_application_mutation_facades_report_schema_not_ready_without_creating_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation ledger facade methods should not create absent SQLite files."""
    db_path = tmp_path / "missing-ledger.sqlite3"
    missing_engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setattr(
        application_mod,
        "get_engine",
        lambda: missing_engine,
        raising=False,
    )

    result = AgentWorkbenchApplication().mutation_list()

    assert result["ok"] is False
    assert result["data"] is None
    assert result["errors"][0]["code"] == "SCHEMA_NOT_READY"
    assert "cli_mutation_ledger" in result["errors"][0]["details"]["missing"]
    assert not db_path.exists()


def test_mutation_ledger_repository_reports_missing_recovery_linkage_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-Phase-2B mutation ledgers should be blocked with column details."""
    db_path = tmp_path / "pre-phase-2b-ledger.sqlite3"
    old_engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with old_engine.begin() as conn:
        conn.execute(text(CLI_MUTATION_LEDGER_CREATE_SQL_PHASE_2A))
    monkeypatch.setattr(
        application_mod,
        "get_engine",
        lambda: old_engine,
        raising=False,
    )

    result = AgentWorkbenchApplication().mutation_list()

    assert result["ok"] is False
    assert result["data"] is None
    assert result["errors"][0]["code"] == "SCHEMA_NOT_READY"
    assert result["errors"][0]["details"]["missing"] == {
        "cli_mutation_ledger": [
            "recovers_mutation_event_id",
            "superseded_by_mutation_event_id",
        ]
    }
