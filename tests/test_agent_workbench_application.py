"""Tests for the agent workbench application facade."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from shlex import quote
from typing import TYPE_CHECKING, Any, NoReturn, cast

from sqlalchemy import create_engine, text
from sqlmodel import SQLModel

import services.agent_workbench.application as application_mod
from db.migrations import ensure_schema_current
from models import db as model_db
from services.agent_workbench import post_sprint_triage as post_sprint_triage_module
from services.agent_workbench.application import AgentWorkbenchApplication
from services.agent_workbench.authority_decision import (
    AuthorityAcceptRequest,
    AuthorityRejectRequest,
)
from services.agent_workbench.authority_regenerate import AuthorityRegenerateRequest
from services.agent_workbench.command_registry import command_contracts
from services.agent_workbench.mutation_ledger import (
    MutationLedgerRepository,
    MutationStatus,
    RecoveryAction,
)
from services.agent_workbench.post_sprint_triage import build_triage_payload
from services.agent_workbench.project_setup_fingerprints import (
    setup_retry_context_fingerprint,
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


def _structured_spec_payload() -> dict[str, Any]:
    """Build a minimal structured spec fixture."""
    return {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": "SPEC.setup-retry",
        "title": "Setup Retry App",
        "status": "draft",
        "version": "0.1",
        "created_at": "2026-06-13",
        "updated_at": "2026-06-13",
        "summary": "Create a project from a structured authority spec.",
        "problem_statement": "Operators need runnable setup retry guidance.",
        "items": [
            {
                "id": "REQ.setup.retry",
                "type": "REQ",
                "status": "proposed",
                "level": "MUST",
                "title": "Setup retry guard",
                "statement": "The system MUST publish runnable setup retry guards.",
                "verification": "system-test",
                "acceptance": ["Setup retry guards can be dry-run without refresh."],
            }
        ],
        "relations": [],
        "controlled_terms": [],
        "external_references": [],
        "rendering": {
            "markdown_profile": "agileforge.spec_markdown.v1",
            "rendered_markdown_sha256": None,
        },
    }

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


class _SprintSetupStaleStoryScopeReadProjection(_SprintReadyReadProjection):
    """Fake read projection for stale Story completion scope in Sprint setup."""

    def sprint_candidates(self, *, project_id: int) -> dict[str, Any]:
        """Return zero candidates excluded by stale completion scope."""
        result = super().sprint_candidates(project_id=project_id)
        result["data"].update(
            {
                "count": 0,
                "message": "Found 0 sprint candidates for milestone Story scope.",
                "excluded_counts": {
                    "story_completion_scope": 28,
                    "superseded": 9,
                },
                "readiness": {
                    "status": "ready",
                    "blocking_codes": [],
                    "blocking_story_ids": [],
                    "default_priority_count": 0,
                    "unsized_count": 0,
                },
                "story_completion_scope": {
                    "scope": "milestone",
                    "scope_id": "milestone_1",
                    "requirements": [
                        "State Window Feature Generation",
                        "Delayed Temperature Reward Scoring",
                    ],
                },
            }
        )
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


class _SprintCompleteRequiresTriageReadProjection(_FakeReadProjection):
    """Fake read projection for a completed Sprint needing triage."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return completed Sprint state with next-cycle Backlog source."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "SPRINT_COMPLETE",
            "latest_completed_sprint_id": 13,
            "backlog_attempts": [
                {
                    "attempt_id": "backlog-attempt-1",
                    "artifact_fingerprint": "sha256:source",
                }
            ],
        }
        return result


class _SprintCompleteStaleRequiresTriageReadProjection(
    _SprintCompleteRequiresTriageReadProjection
):
    """Fake completed Sprint state that is stale and still missing triage."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return stale completed Sprint state with missing triage."""
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


class _SprintCompleteWithCurrentTriageReadProjection(_FakeReadProjection):
    """Fake completed Sprint state with current post-sprint triage."""

    def __init__(
        self,
        *,
        impact: str,
        affected_requirements: list[object] | None = None,
        affected_task_ids: list[object] | None = None,
        affected_story_ids: list[object] | None = None,
        affected_layers: list[object] | None = None,
    ) -> None:
        self._impact = impact
        self._affected_requirements = affected_requirements
        self._affected_task_ids = affected_task_ids
        self._affected_story_ids = affected_story_ids
        self._affected_layers = affected_layers

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return completed Sprint state with current triage."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "SPRINT_COMPLETE",
            "latest_completed_sprint_id": 13,
            "post_sprint_triage": build_triage_payload(
                project_id=project_id,
                sprint_id=13,
                impact=self._impact,
                affected_requirements=self._affected_requirements,
                affected_task_ids=self._affected_task_ids,
                affected_story_ids=self._affected_story_ids,
                affected_backlog_item_ids=None,
                affected_roadmap_item_ids=None,
                affected_layers=self._affected_layers,
                learning_summary="Recorded post-sprint learning.",
                decision_reason="Route the current post-sprint impact.",
                idempotency_key=f"triage-{self._impact}",
                replace_existing=False,
                recorded_at="2026-06-10T00:00:00Z",
                recorded_by="cli-agent",
            ),
        }
        return result

    def sprint_candidates(self, *, project_id: int) -> dict[str, Any]:
        """Return available Sprint candidates for the next-cycle planning path."""
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "items": [{"story_id": 101}, {"story_id": 102}],
                "count": 2,
                "excluded_counts": {},
                "source_fingerprint": CANDIDATES_FINGERPRINT,
            },
            "warnings": [],
            "errors": [],
        }


class _SprintCompleteTriagedNoneNoCandidatesReadProjection(
    _SprintCompleteWithCurrentTriageReadProjection
):
    """Fake completed Sprint state with pending Story work and no candidates."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return completed Sprint state with uncovered Roadmap requirements."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"].update(
            {
                "roadmap_releases": [
                    {
                        "items": [
                            "Technology and Model Research Spike",
                            "pyrepo-check Quality Gate Integration",
                        ]
                    }
                ],
                "story_saved": {"Technology and Model Research Spike": True},
            }
        )
        return result

    def sprint_candidates(self, *, project_id: int) -> dict[str, Any]:
        """Return no eligible Sprint candidates with non-refined exclusions."""
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "items": [],
                "count": 0,
                "excluded_counts": {"non_refined": 1},
                "source_fingerprint": CANDIDATES_FINGERPRINT,
            },
            "warnings": [],
            "errors": [],
        }


class _SprintCompleteTriagedNoneNoRefinedCandidatesReadProjection(
    _SprintCompleteWithCurrentTriageReadProjection
):
    """Fake completed Sprint state with covered requirements but no candidates."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return completed Sprint state with saved but non-refined Story rows."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"].update(
            {
                "roadmap_releases": [
                    {"items": ["Requirement A", "Requirement B"]},
                ],
                "story_saved": {"Requirement A": True, "Requirement B": True},
            }
        )
        return result

    def sprint_candidates(self, *, project_id: int) -> dict[str, Any]:
        """Return zero eligible candidates due to non-refined rows."""
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "items": [],
                "count": 0,
                "excluded_counts": {"non_refined": 2},
                "source_fingerprint": CANDIDATES_FINGERPRINT,
            },
            "warnings": [],
            "errors": [],
        }


class _SprintCompleteWithBacklogAttemptsReadProjection(_SprintCompleteReadProjection):
    """Fake completed Sprint state with a source Backlog attempt."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return sprint complete workflow state with Backlog refinement source."""
        result = super().workflow_state(project_id=project_id)
        state = result["data"]["state"]
        state["latest_completed_sprint_id"] = 13
        state["post_sprint_triage"] = build_triage_payload(
            project_id=project_id,
            sprint_id=13,
            impact="backlog",
            affected_requirements=None,
            affected_task_ids=None,
            affected_story_ids=None,
            affected_backlog_item_ids=None,
            affected_roadmap_item_ids=None,
            affected_layers=None,
            learning_summary="Backlog follow-up is needed.",
            decision_reason="Review the next-cycle Backlog source.",
            idempotency_key="triage-key",
            replace_existing=False,
            recorded_at="2026-06-10T00:00:00Z",
            recorded_by="cli-agent",
        )
        state["backlog_attempts"] = [
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


class _SprintCompleteTriagedBacklogNoSourceReadProjection(_FakeReadProjection):
    """Fake completed Sprint state with backlog impact but no source attempt."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return completed Sprint state with backlog triage and no source."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "SPRINT_COMPLETE",
            "latest_completed_sprint_id": 13,
            "post_sprint_triage": build_triage_payload(
                project_id=project_id,
                sprint_id=13,
                impact="backlog",
                affected_requirements=None,
                affected_task_ids=None,
                affected_story_ids=None,
                affected_backlog_item_ids=None,
                affected_roadmap_item_ids=None,
                affected_layers=None,
                learning_summary="Backlog follow-up is needed.",
                decision_reason="Backlog impact should refine next-cycle source.",
                idempotency_key="triage-backlog-no-source",
                replace_existing=False,
                recorded_at="2026-06-10T00:00:00Z",
                recorded_by="cli-agent",
            ),
            "planned_sprint_id": None,
            "downstream_backlog_stale": False,
            "stale_backlog_reason": None,
            "backlog_attempts": [],
        }
        return result


class _SprintCompleteTriagedNoneActiveResetReadProjection(_FakeReadProjection):
    """Fake completed Sprint state with none triage and active-reset stale guard."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return triaged-none completed Sprint state with reset stale marker."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "SPRINT_COMPLETE",
            "latest_completed_sprint_id": 13,
            "post_sprint_triage": build_triage_payload(
                project_id=project_id,
                sprint_id=13,
                impact="none",
                affected_requirements=None,
                affected_task_ids=None,
                affected_story_ids=None,
                affected_backlog_item_ids=None,
                affected_roadmap_item_ids=None,
                affected_layers=None,
                learning_summary="No follow-up impact.",
                decision_reason="Continue only if no stale guard is active.",
                idempotency_key="triage-none-stale-reset",
                replace_existing=False,
                recorded_at="2026-06-10T00:00:00Z",
                recorded_by="cli-agent",
            ),
            "planned_sprint_id": None,
            "downstream_backlog_stale": True,
            "stale_backlog_reason": "active_backlog_reset",
            "stale_since_backlog_attempt_id": "backlog-attempt-12",
            "active_backlog_reset_attempt_id": "backlog-attempt-12",
            "backlog_attempts": [],
        }
        return result


class _SprintCompletePlannedSprintMissingTriageReadProjection(_FakeReadProjection):
    """Fake completed Sprint state with a planned Sprint but no triage."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return completed Sprint state that must triage before Sprint start."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "SPRINT_COMPLETE",
            "latest_completed_sprint_id": 13,
            "post_sprint_triage": None,
            "planned_sprint_id": 21,
            "downstream_backlog_stale": False,
            "stale_backlog_reason": None,
            "backlog_attempts": [],
        }
        return result


class _SprintCompleteTriagedNonePlannedSprintReadProjection(_FakeReadProjection):
    """Fake completed Sprint state with none triage and planned Sprint."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return completed Sprint state with triage and planned Sprint."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "SPRINT_COMPLETE",
            "latest_completed_sprint_id": 13,
            "post_sprint_triage": build_triage_payload(
                project_id=project_id,
                sprint_id=13,
                impact="none",
                affected_requirements=None,
                affected_task_ids=None,
                affected_story_ids=None,
                affected_backlog_item_ids=None,
                affected_roadmap_item_ids=None,
                affected_layers=None,
                learning_summary="No follow-up impact.",
                decision_reason="Continue only through executable bridges.",
                idempotency_key="triage-none-planned-sprint",
                replace_existing=False,
                recorded_at="2026-06-10T00:00:00Z",
                recorded_by="cli-agent",
            ),
            "planned_sprint_id": 21,
            "downstream_backlog_stale": False,
            "stale_backlog_reason": None,
            "backlog_attempts": [],
        }
        return result


class _SprintCompleteRefinedBacklogRecordedNoSourceReadProjection(_FakeReadProjection):
    """Fake completed Sprint state with stale refined Backlog but no guards."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return refined-backlog stale state missing usable source guards."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "SPRINT_COMPLETE",
            "latest_completed_sprint_id": 13,
            "post_sprint_triage": build_triage_payload(
                project_id=project_id,
                sprint_id=13,
                impact="none",
                affected_requirements=None,
                affected_task_ids=None,
                affected_story_ids=None,
                affected_backlog_item_ids=None,
                affected_roadmap_item_ids=None,
                affected_layers=None,
                learning_summary="No new follow-up impact.",
                decision_reason="Existing refined backlog guard still applies.",
                idempotency_key="triage-none-refined-backlog-stale",
                replace_existing=False,
                recorded_at="2026-06-10T00:00:00Z",
                recorded_by="cli-agent",
            ),
            "planned_sprint_id": None,
            "downstream_backlog_stale": True,
            "stale_backlog_reason": "refined_backlog_recorded",
            "stale_since_backlog_attempt_id": "backlog-attempt-missing-source",
            "backlog_attempts": [
                {
                    "attempt_id": "",
                    "artifact_fingerprint": "",
                }
            ],
        }
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


class _BacklogPersistenceBlankActiveResetReadProjection(
    _BacklogPersistenceReadProjection
):
    """Fake read projection for malformed blank-id active-reset stale marker."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Backlog persistence workflow state with blank reset ids."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"].update(
            {
                "downstream_backlog_stale": True,
                "stale_backlog_reason": "active_backlog_reset",
                "stale_since_backlog_attempt_id": "",
                "active_backlog_reset_attempt_id": "",
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


class _RoadmapPersistenceActiveResetReadProjection(_RoadmapPersistenceReadProjection):
    """Fake read projection for stale active-reset Roadmap persistence."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Roadmap persistence workflow state with reset stale marker."""
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


class _StoryInterviewSaveableDraftReadProjection(_FakeReadProjection):
    """Fake Story interview state that already has a saveable draft."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Story interview state with a saveable complete draft."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "STORY_INTERVIEW",
            "setup_status": "passed",
            "interview_runtime": {
                "story": {
                    "Canonical Process Event Record Definition and Validation": {
                        "draft_projection": {
                            "kind": "complete_draft",
                            "is_complete": True,
                            "latest_reusable_attempt_id": "attempt-2",
                            "artifact_fingerprint": "sha256:canonical",
                        },
                        "attempt_history": [
                            {
                                "attempt_id": "attempt-2",
                                "artifact_fingerprint": "sha256:canonical",
                                "is_reusable": True,
                                "output_artifact": {
                                    "is_complete": True,
                                    "quality": {
                                        "coverage_status": "complete",
                                        "quality_findings": [],
                                        "saveable": True,
                                    },
                                    "user_stories": [
                                        {
                                            "story_title": (
                                                "Define canonical process records"
                                            ),
                                            "invest_score": "High",
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                }
            },
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
    """Fake read projection for incomplete Story state without coverage."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Story persistence workflow state without coverage."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "STORY_PERSISTENCE",
            "setup_status": "passed",
            "roadmap_releases": [{"items": ["Requirement A", "Requirement B"]}],
            "story_saved": {},
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


class _StoryMilestoneReadyReadProjection(_FakeReadProjection):
    """Fake read projection with a partially covered Story state."""

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return Story persistence workflow state with one complete milestone."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = {
            "fsm_state": "STORY_PERSISTENCE",
            "setup_status": "passed",
            "roadmap_releases": [
                {"items": ["Requirement A", "Requirement B"]},
                {"items": ["Requirement C"]},
            ],
            "story_saved": {"Requirement A": True, "Requirement B": True},
        }
        return result


class _WorkflowStateReader(_FakeReadProjection):
    """Fake read projection with caller-supplied workflow state."""

    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return caller-supplied workflow state."""
        result = super().workflow_state(project_id=project_id)
        result["data"]["state"] = self._state
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

    def __init__(self, *, spec_file_path: str | None = None) -> None:
        self._spec_file_path = spec_file_path

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return failed setup workflow state."""
        result = super().workflow_state(project_id=project_id)
        state = {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "failed",
        }
        if self._spec_file_path is not None:
            state["setup_spec_file_path"] = self._spec_file_path
        result["data"]["state"] = state
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


class _RejectedAuthorityProjection(_FakeAuthorityProjection):
    """Fake authority projection for rejected authority regeneration."""

    def status(self, *, project_id: int) -> dict[str, Any]:
        """Return rejected authority status with a concrete spec version."""
        result = super().status(project_id=project_id)
        result["data"].update(
            {
                "status": "rejected",
                "rejected_spec_version_id": SPEC_VERSION_ID,
            }
        )
        return result


class _RejectedWithPendingAuthorityProjection(_RejectedAuthorityProjection):
    """Fake authority projection after rejected authority was regenerated."""

    def status(self, *, project_id: int) -> dict[str, Any]:
        """Return rejected history plus a newer pending authority candidate."""
        result = super().status(project_id=project_id)
        result["data"].update(
            {
                "pending_authority_id": 4,
                "pending_compiled_spec_version_id": SPEC_VERSION_ID,
            }
        )
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


class _FakeAuthorityRegenerateRunner:
    """Fake authority regenerate runner used to verify facade delegation."""

    def __init__(self) -> None:
        self.calls: list[AuthorityRegenerateRequest] = []

    def regenerate(self, request: AuthorityRegenerateRequest) -> dict[str, Any]:
        """Record a regenerate request."""
        self.calls.append(request)
        return {
            "ok": True,
            "data": {
                "project_id": request.project_id,
                "spec_version_id": request.spec_version_id,
                "status": "authority_pending_review",
            },
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
        force_feedback: bool = False,
    ) -> dict[str, Any]:
        """Record Story generation."""
        self.calls.append(
            (
                "generate",
                {
                    "project_id": project_id,
                    "parent_requirement": parent_requirement,
                    "user_input": user_input,
                    "force_feedback": force_feedback,
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

    def complete(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
        scope: str | None = None,
        scope_id: str | None = None,
        parent_requirements: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record Story completion."""
        call_args: dict[str, object] = {
            "project_id": project_id,
            "expected_state": expected_state,
            "idempotency_key": idempotency_key,
        }
        if scope is not None:
            call_args["scope"] = scope
            call_args["scope_id"] = scope_id
        elif scope_id is not None:
            call_args["scope_id"] = scope_id
        if parent_requirements is not None:
            call_args["parent_requirements"] = parent_requirements
        self.calls.append(
            (
                "complete",
                call_args,
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


def test_application_authority_regenerate_delegates_to_runner() -> None:
    """Verify authority regenerate routes through the injected runner."""
    runner = _FakeAuthorityRegenerateRunner()
    app = AgentWorkbenchApplication(authority_regenerate_runner=runner)

    result = app.authority_regenerate(
        project_id=PROJECT_ID,
        spec_version_id=SPEC_VERSION_ID,
        idempotency_key="regen-app-001",
        changed_by="test",
        dry_run=True,
    )

    assert result["ok"] is True
    assert runner.calls == [
        AuthorityRegenerateRequest(
            project_id=PROJECT_ID,
            spec_version_id=SPEC_VERSION_ID,
            idempotency_key="regen-app-001",
            changed_by="test",
            dry_run=True,
        )
    ]


def test_authority_regenerate_is_registered_command() -> None:
    """Verify authority regenerate is discoverable with mutation metadata."""
    contracts = {command.name: command for command in command_contracts()}

    metadata = contracts["agileforge authority regenerate"]

    assert metadata.mutates is True
    assert metadata.phase == "phase_2c"
    assert metadata.requires_idempotency_key is True
    assert metadata.idempotency_policy == {
        "non_dry_run": "required",
        "dry_run": "not_required_no_ledger",
        "dry_run_trace_field": "none",
    }
    assert metadata.input_required == ("project_id", "spec_version_id")
    assert metadata.input_optional == (
        "idempotency_key",
        "changed_by",
        "dry_run",
    )
    assert set(metadata.errors) == {
        "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED",
        "SCHEMA_NOT_READY",
        "PROJECT_NOT_FOUND",
        "SPEC_VERSION_NOT_FOUND",
        "AUTHORITY_REVIEW_REQUIRED",
        "SPEC_COMPILE_FAILED",
        "MUTATION_FAILED",
        "IDEMPOTENCY_KEY_REUSED",
        "MUTATION_IN_PROGRESS",
        "MUTATION_RECOVERY_REQUIRED",
        "MUTATION_RESUME_CONFLICT",
    }


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


def test_story_generate_threads_force_feedback() -> None:
    """Story facade forwards the feedback quality override to the runner."""
    runner = _FakeStoryRunner()
    app = AgentWorkbenchApplication(story_runner=cast("Any", runner))

    result = app.story_generate(
        project_id=PROJECT_ID,
        parent_requirement="REQ.checkout",
        user_input="ship it despite sparse notes",
        force_feedback=True,
    )

    assert result["ok"] is True
    assert runner.calls == [
        (
            "generate",
            {
                "project_id": PROJECT_ID,
                "parent_requirement": "REQ.checkout",
                "user_input": "ship it despite sparse notes",
                "force_feedback": True,
            },
        )
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
        app.story_complete(
            project_id=PROJECT_ID,
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-milestone-0",
            scope="milestone",
            scope_id="milestone_0",
        )["data"]["fsm_state"]
        == "SPRINT_SETUP"
    )
    assert (
        app.story_complete(
            project_id=PROJECT_ID,
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-selection",
            scope="selection",
            parent_requirements=[
                "Technology and Model Research Spike",
                "Python Project Scaffold and uv Management Setup",
            ],
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
    assert runner.calls[6] == (
        "complete",
        {
            "project_id": PROJECT_ID,
            "expected_state": "STORY_PERSISTENCE",
            "idempotency_key": "complete-story-milestone-0",
            "scope": "milestone",
            "scope_id": "milestone_0",
        },
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
                "force_feedback": False,
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
            "complete",
            {
                "project_id": PROJECT_ID,
                "expected_state": "STORY_PERSISTENCE",
                "idempotency_key": "complete-story-milestone-0",
                "scope": "milestone",
                "scope_id": "milestone_0",
            },
        ),
        (
            "complete",
            {
                "project_id": PROJECT_ID,
                "expected_state": "STORY_PERSISTENCE",
                "idempotency_key": "complete-story-selection",
                "scope": "selection",
                "scope_id": None,
                "parent_requirements": [
                    "Technology and Model Research Spike",
                    "Python Project Scaffold and uv Management Setup",
                ],
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


def test_story_complete_facade_routes_selection_scope() -> None:
    """Story complete facade forwards selected parent requirements."""
    runner = _FakeStoryRunner()
    app = AgentWorkbenchApplication(story_runner=cast("Any", runner))

    assert (
        app.story_complete(
            project_id=PROJECT_ID,
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-selection",
            scope="selection",
            parent_requirements=[
                "Technology and Model Research Spike",
                "Python Project Scaffold and uv Management Setup",
            ],
        )["data"]["fsm_state"]
        == "SPRINT_SETUP"
    )
    assert runner.calls == [
        (
            "complete",
            {
                "project_id": PROJECT_ID,
                "expected_state": "STORY_PERSISTENCE",
                "idempotency_key": "complete-story-selection",
                "scope": "selection",
                "scope_id": None,
                "parent_requirements": [
                    "Technology and Model Research Spike",
                    "Python Project Scaffold and uv Management Setup",
                ],
            },
        )
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


def test_application_workflow_next_rejects_blank_active_reset_attempt_ids() -> None:
    """Malformed reset markers should not get active-reset workflow routing."""
    app = AgentWorkbenchApplication(
        read_projection=_BacklogPersistenceBlankActiveResetReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge roadmap generate --project-id 7"
    ]
    assert result["data"]["status"] == "next_phase_available"
    assert result["data"]["blocked_commands"] == []
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


def test_workflow_next_blocks_story_after_stale_reset_roadmap_save() -> None:
    """Stale active reset must block Story handoff until roadmap save clears it."""
    app = AgentWorkbenchApplication(
        read_projection=_RoadmapPersistenceActiveResetReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == []
    assert result["data"]["blocked_commands"] == [
        {
            "command": "agileforge story pending",
            "reason": "DOWNSTREAM_BACKLOG_STALE_AFTER_ACTIVE_RESET",
            "message": (
                "Story generation remains blocked until downstream reset-stale "
                "clearing exists."
            ),
        },
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
    assert result["data"]["status"] == "blocked_by_stale_active_backlog_reset"


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


def test_workflow_next_routes_saveable_story_interview_to_guarded_save() -> None:
    """Recover a stale Story interview state when a saveable draft exists."""
    app = AgentWorkbenchApplication(
        read_projection=_StoryInterviewSaveableDraftReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        (
            "agileforge story history --project-id 7 "
            '--parent-requirement "Canonical Process Event Record Definition '
            'and Validation"'
        ),
        (
            "agileforge story save --project-id 7 "
            '--parent-requirement "Canonical Process Event Record Definition '
            'and Validation" '
            "--attempt-id attempt-2 "
            "--expected-artifact-fingerprint sha256:canonical "
            "--expected-state STORY_REVIEW "
            "--idempotency-key <idempotency_key>"
        ),
        (
            "agileforge story generate --project-id 7 "
            '--parent-requirement "Canonical Process Event Record Definition '
            'and Validation" '
            "--input <feedback>"
        ),
    ]
    assert result["data"]["blocked_commands"] == []


def test_workflow_next_routes_covered_story_interview_to_recovery_complete() -> None:
    """Recover a stale Story interview state when all requirements are covered."""
    app = AgentWorkbenchApplication(
        read_projection=_WorkflowStateReader(
            {
                "fsm_state": "STORY_INTERVIEW",
                "roadmap_releases": [{"items": ["Requirement A", "Requirement B"]}],
                "story_saved": {"Requirement A": True, "Requirement B": True},
                "interview_runtime": {
                    "story": {
                        "Requirement A": {
                            "draft_projection": {
                                "kind": "complete_draft",
                                "is_complete": True,
                                "latest_reusable_attempt_id": "attempt-1",
                                "artifact_fingerprint": "sha256:a",
                            },
                            "attempt_history": [
                                {
                                    "attempt_id": "attempt-1",
                                    "artifact_fingerprint": "sha256:a",
                                    "is_reusable": True,
                                    "output_artifact": {
                                        "is_complete": True,
                                        "quality": {
                                            "coverage_status": "complete",
                                            "quality_findings": [],
                                            "saveable": True,
                                        },
                                        "user_stories": [
                                            {"story_title": "Saved Story"}
                                        ],
                                    },
                                }
                            ],
                        }
                    }
                },
            }
        ),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge story pending --project-id 7",
        (
            "agileforge story complete --project-id 7 "
            "--expected-state STORY_INTERVIEW "
            "--idempotency-key <idempotency_key>"
        ),
    ]
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["blocked_future_commands"] == []


def test_workflow_next_routes_stale_story_interview_to_stored_scope_complete() -> None:
    """Recover stale Story interview state through its stored completion scope."""
    app = AgentWorkbenchApplication(
        read_projection=_WorkflowStateReader(
            {
                "fsm_state": "STORY_INTERVIEW",
                "roadmap_releases": [
                    {"items": ["Requirement A", "Requirement B"]},
                    {"items": ["Requirement C"]},
                ],
                "story_saved": {"Requirement A": True, "Requirement B": True},
                "story_completion_scope": {
                    "schema_version": "agileforge.story_completion_scope.v1",
                    "scope": "milestone",
                    "scope_id": "milestone_0",
                    "requirements": ["Requirement A", "Requirement B"],
                    "completed_at": "2026-06-10T20:16:52Z",
                },
            }
        ),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge story pending --project-id 7",
        (
            "agileforge story complete --project-id 7 "
            "--expected-state STORY_INTERVIEW "
            "--scope milestone --scope-id milestone_0 "
            "--idempotency-key <idempotency_key>"
        ),
    ]
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["blocked_future_commands"] == []


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
    """Keep Story completion hidden until at least one requirement is covered."""
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


def test_workflow_next_routes_story_persistence_to_selection_complete_when_partially_saved() -> None:  # noqa: E501
    """Story persistence should advertise installed selection completion."""
    app = AgentWorkbenchApplication(
        read_projection=_WorkflowStateReader(
            {
                "fsm_state": "STORY_PERSISTENCE",
                "roadmap_releases": [
                    {
                        "items": [
                            "Technology and Model Research Spike",
                            "Python Project Scaffold and uv Management Setup",
                        ]
                    }
                ],
                "story_saved": {
                    "Technology and Model Research Spike": True,
                    "Python Project Scaffold and uv Management Setup": False,
                },
            }
        )
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge story pending --project-id 7",
        (
            "agileforge story generate --project-id 7 "
            "--parent-requirement <parent_requirement>"
        ),
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
            "--scope selection "
            '--parent-requirement "Technology and Model Research Spike" '
            "--idempotency-key <idempotency_key>"
        ),
    ]
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["blocked_future_commands"] == []


def test_workflow_next_routes_story_persistence_to_complete_when_covered() -> None:
    """Expose whole-phase and selection Story completion for full coverage."""
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
        (
            "agileforge story complete --project-id 7 "
            "--expected-state STORY_PERSISTENCE "
            "--scope selection "
            '--parent-requirement "Requirement A" '
            '--parent-requirement "Requirement B" '
            "--idempotency-key <idempotency_key>"
        ),
    ]
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["blocked_future_commands"] == []


def test_workflow_next_routes_story_persistence_to_scoped_complete_when_milestone_ready() -> None:  # noqa: E501
    """Expose scoped Story completion when one milestone is planning-ready."""
    app = AgentWorkbenchApplication(
        read_projection=_StoryMilestoneReadyReadProjection(),
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
            "--scope milestone --scope-id milestone_0 "
            "--idempotency-key <idempotency_key>"
        ),
        (
            "agileforge story complete --project-id 7 "
            "--expected-state STORY_PERSISTENCE "
            "--scope selection "
            '--parent-requirement "Requirement A" '
            '--parent-requirement "Requirement B" '
            "--idempotency-key <idempotency_key>"
        ),
    ]


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


def test_workflow_next_blocks_generate_for_stale_story_scope() -> None:
    """Route stale Story scope in Sprint setup to guarded readiness repair."""
    app = AgentWorkbenchApplication(
        read_projection=_SprintSetupStaleStoryScopeReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "sprint_setup_story_scope_repair_required"
    assert "agileforge sprint generate --project-id 7" not in data[
        "next_valid_commands"
    ]
    assert (
        "agileforge story repair-readiness --project-id 7 "
        "--expected-state SPRINT_SETUP "
        "--idempotency-key <idempotency_key>"
    ) in data["next_valid_commands"]
    assert data["blocked_commands"] == [
        {
            "command": "agileforge sprint generate",
            "reason": "STALE_STORY_COMPLETION_SCOPE",
            "message": (
                "Sprint generation is blocked because the active Story "
                "completion scope excludes all current Sprint candidates. Run "
                "story repair-readiness to refresh Story planning metadata."
            ),
            "candidate_count": 0,
            "excluded_counts": {
                "story_completion_scope": 28,
                "superseded": 9,
            },
            "story_completion_scope": {
                "scope": "milestone",
                "scope_id": "milestone_1",
                "requirements": [
                    "State Window Feature Generation",
                    "Delayed Temperature Reward Scoring",
                ],
            },
        }
    ]


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
            "--task-id <task_id> --status Done "
            '--expected-status "<expected_status>" '
            "--expected-task-fingerprint <task_fingerprint> "
            "--idempotency-key <idempotency_key> "
            '--outcome-summary "<outcome_summary>" '
            '--validation-summary "<validation_summary>" '
            "--checklist-result fully_met "
            "--artifact-ref <artifact_ref>"
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


def test_workflow_next_requires_post_sprint_triage_required_before_backlog_refinement() -> (  # noqa: E501
    None
):
    """Require triage before completed Sprint routing exposes Backlog refinement."""
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteRequiresTriageReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "post_sprint_triage_required"
    assert data["next_valid_commands"] == [
        "agileforge sprint review --project-id 7",
        (
            "agileforge sprint triage --project-id 7 "
            "--expected-state SPRINT_COMPLETE --impact <impact> "
            "--learning-summary <summary> --decision-reason <reason> "
            "--idempotency-key <idempotency_key>"
        ),
        "agileforge sprint history --project-id 7",
    ]
    assert not any(
        "backlog refine" in command for command in data["next_valid_commands"]
    )
    assert data["next_actions"] == [
        {
            "command": (
                "agileforge sprint triage --project-id 7 "
                "--expected-state SPRINT_COMPLETE --impact <impact> "
                "--learning-summary <summary> --decision-reason <reason> "
                "--idempotency-key <idempotency_key>"
            ),
            "status": "post_sprint_triage_required",
            "reason": (
                "A completed Sprint needs learning triage before next-cycle routing."
            ),
            "runnable": True,
            "installed": True,
            "requires_cli_installation": False,
            "requires": [
                "expected_state",
                "impact",
                "learning_summary",
                "decision_reason",
                "idempotency_key",
            ],
        }
    ]


def test_workflow_next_routes_impact_none_to_story_and_sprint_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route impact=none to Story continuation and next Sprint planning."""
    monkeypatch.setattr(
        post_sprint_triage_module,
        "canonical_hash",
        lambda _payload: "sha256:triage",
    )
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteWithCurrentTriageReadProjection(
            impact="none",
        ),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "post_sprint_story_continuation_available"
    assert "agileforge story pending --project-id 7" in data["next_valid_commands"]
    assert "agileforge sprint candidates --project-id 7" in data["next_valid_commands"]
    assert "agileforge sprint generate --project-id 7" in data["next_valid_commands"]
    assert not any(
        command.startswith("agileforge backlog refine")
        for command in data["next_valid_commands"]
    )
    assert {
        "command": "agileforge sprint generate --project-id 7",
        "status": "post_sprint_story_continuation_available",
        "reason": "Post-sprint triage recorded no follow-up impact.",
        "runnable": True,
        "installed": True,
        "requires_cli_installation": False,
    } in data["next_actions"]


def test_workflow_next_routes_impact_none_with_no_candidates_to_story_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route impact=none to Story generation when no Sprint candidates exist."""
    monkeypatch.setattr(
        post_sprint_triage_module,
        "canonical_hash",
        lambda _payload: "sha256:triage",
    )
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteTriagedNoneNoCandidatesReadProjection(
            impact="none",
        ),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "post_sprint_story_generation_required"
    assert data["next_valid_commands"] == [
        "agileforge story pending --project-id 7",
        (
            "agileforge story generate --project-id 7 "
            '--parent-requirement "pyrepo-check Quality Gate Integration"'
        ),
        "agileforge sprint candidates --project-id 7",
    ]
    assert data["blocked_commands"] == [
        {
            "command": "agileforge sprint generate",
            "reason": "NO_SAVED_SPRINT_CANDIDATES",
            "message": (
                "Sprint generation is blocked until at least one saved Story "
                "candidate is available."
            ),
            "candidate_count": 0,
            "pending_story_requirements": 1,
        }
    ]
    assert data["next_actions"] == [
        {
            "command": (
                "agileforge story generate --project-id 7 "
                '--parent-requirement "pyrepo-check Quality Gate Integration"'
            ),
            "status": "post_sprint_story_generation_required",
            "reason": (
                "Post-sprint triage recorded no follow-up impact, but no "
                "Sprint candidates are available and Roadmap requirements "
                "still need saved Stories."
            ),
            "runnable": True,
            "installed": True,
            "requires_cli_installation": False,
        }
    ]


def test_workflow_next_blocks_sprint_generate_when_saved_stories_not_refined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not advertise Sprint generation when no refined candidates exist."""
    monkeypatch.setattr(
        post_sprint_triage_module,
        "canonical_hash",
        lambda _payload: "sha256:triage",
    )
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteTriagedNoneNoRefinedCandidatesReadProjection(
            impact="none",
        ),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "post_sprint_sprint_candidates_unavailable"
    assert data["next_valid_commands"] == [
        "agileforge story pending --project-id 7",
        "agileforge sprint candidates --project-id 7",
    ]
    assert "agileforge sprint generate --project-id 7" not in data[
        "next_valid_commands"
    ]
    assert data["blocked_commands"] == [
        {
            "command": "agileforge sprint generate",
            "reason": "NO_REFINED_SPRINT_CANDIDATES",
            "message": (
                "Sprint generation is blocked because no refined Story candidates "
                "are available."
            ),
            "candidate_count": 0,
            "excluded_counts": {"non_refined": 2},
        }
    ]
    assert data["next_actions"] == [
        {
            "command": "agileforge sprint candidates --project-id 7",
            "status": "post_sprint_sprint_candidates_unavailable",
            "reason": (
                "Post-sprint triage recorded no follow-up impact, but Sprint "
                "generation has no refined candidates to plan."
            ),
            "runnable": True,
            "installed": True,
            "requires_cli_installation": False,
        }
    ]


def test_workflow_next_routes_impact_story_to_story_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route impact=story to affected Story generation without Sprint planning."""
    monkeypatch.setattr(
        post_sprint_triage_module,
        "canonical_hash",
        lambda _payload: "sha256:triage",
    )
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteWithCurrentTriageReadProjection(
            impact="story",
            affected_requirements=["Quality Gate"],
        ),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "post_sprint_story_impact_needs_reconciliation"
    assert "agileforge story pending --project-id 7" in data["next_valid_commands"]
    assert (
        'agileforge story generate --project-id 7 '
        '--parent-requirement "Quality Gate"'
    ) in data["next_valid_commands"]
    assert not any(
        command.startswith("agileforge sprint generate")
        for command in data["next_valid_commands"]
    )
    all_commands = (
        data["next_valid_commands"]
        + data["blocked_future_commands"]
        + [
            item["command"]
            for item in data["blocked_commands"]
            if isinstance(item, dict) and isinstance(item.get("command"), str)
        ]
    )
    assert not any("backlog refine" in command for command in all_commands)


def test_workflow_next_routes_impact_roadmap_to_roadmap_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route impact=roadmap to Roadmap reconciliation without Backlog scope."""
    monkeypatch.setattr(
        post_sprint_triage_module,
        "canonical_hash",
        lambda _payload: "sha256:triage",
    )
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteWithCurrentTriageReadProjection(
            impact="roadmap",
            affected_requirements=["Milestone Ordering"],
        ),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "post_sprint_roadmap_reconciliation_available"
    assert data["next_valid_commands"] == [
        "agileforge roadmap history --project-id 7",
        "agileforge roadmap generate --project-id 7 --input <feedback>",
    ]
    all_commands = (
        data["next_valid_commands"]
        + data["blocked_future_commands"]
        + [
            item["command"]
            for item in data["blocked_commands"]
            if isinstance(item, dict) and isinstance(item.get("command"), str)
        ]
    )
    assert not any("backlog refine" in command for command in all_commands)
    assert "agileforge sprint generate --project-id 7" not in all_commands
    assert data["blocked_commands"] == [
        {
            "command": "agileforge story generate",
            "reason": "POST_SPRINT_ROADMAP_IMPACT_NEEDS_RECONCILIATION",
            "message": (
                "Roadmap-level post-sprint impact must be reconciled before "
                "continuing Story or Sprint planning."
            ),
        },
        {
            "command": "agileforge sprint candidates",
            "reason": "POST_SPRINT_ROADMAP_IMPACT_NEEDS_RECONCILIATION",
            "message": (
                "Roadmap-level post-sprint impact must be reconciled before "
                "continuing Story or Sprint planning."
            ),
        },
        {
            "command": "agileforge sprint generate",
            "reason": "POST_SPRINT_ROADMAP_IMPACT_NEEDS_RECONCILIATION",
            "message": (
                "Roadmap-level post-sprint impact must be reconciled before "
                "continuing Story or Sprint planning."
            ),
        },
    ]


def test_workflow_next_routes_impact_task_to_carryover_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route impact=task to completed Sprint review commands and blocker."""
    monkeypatch.setattr(
        post_sprint_triage_module,
        "canonical_hash",
        lambda _payload: "sha256:triage",
    )
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteWithCurrentTriageReadProjection(
            impact="task",
            affected_task_ids=[123],
        ),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "post_sprint_task_impact_needs_carryover"
    assert data["next_valid_commands"] == [
        "agileforge sprint review --project-id 7",
        "agileforge sprint status --project-id 7 --sprint-id 13",
        "agileforge sprint history --project-id 7",
    ]
    assert data["blocked_commands"] == [
        {
            "command": "agileforge sprint task carryover",
            "reason": "TASK_CARRYOVER_NOT_IMPLEMENTED",
            "message": (
                "Task carryover is not implemented yet; review the completed "
                "Sprint before planning follow-up work."
            ),
            "affected_task_ids": [123],
        }
    ]


def test_workflow_next_routes_impact_multiple_to_guarded_correction_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route impact=multiple only to review and guarded triage correction."""
    monkeypatch.setattr(
        post_sprint_triage_module,
        "canonical_hash",
        lambda _payload: "sha256:triage",
    )
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteWithCurrentTriageReadProjection(
            impact="multiple",
            affected_layers=["story", "backlog"],
        ),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "post_sprint_multiple_impacts_need_decision"
    assert data["next_valid_commands"] == [
        "agileforge sprint review --project-id 7",
        (
            "agileforge sprint triage --project-id 7 "
            "--expected-state SPRINT_COMPLETE --replace-existing "
            "--expected-triage-fingerprint sha256:triage"
        ),
    ]
    assert data["blocked_commands"] == [
        {
            "command": "agileforge story generate",
            "reason": "POST_SPRINT_MULTIPLE_IMPACTS_NEED_DECISION",
            "message": (
                "Resolve the post-sprint triage decision before routing story "
                "follow-up."
            ),
        },
        {
            "command": "agileforge backlog refine",
            "reason": "POST_SPRINT_MULTIPLE_IMPACTS_NEED_DECISION",
            "message": (
                "Resolve the post-sprint triage decision before routing backlog "
                "follow-up."
            ),
        },
    ]
    assert data["blocked_future_commands"] == []


def test_backlog_impact_records_but_blocks_refine_record_without_source() -> None:
    """Block Backlog refinement bridges when backlog impact has no source."""
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteTriagedBacklogNoSourceReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "post_sprint_backlog_source_unavailable"
    assert data["next_valid_commands"] == []
    assert data["blocked_commands"] == [
        {
            "command": "agileforge backlog refine-preview",
            "reason": "BACKLOG_SOURCE_UNAVAILABLE",
            "message": (
                "Backlog impact was recorded, but no source attempt and fingerprint "
                "are available for a runnable refinement bridge."
            ),
        },
        {
            "command": "agileforge backlog refine-record",
            "reason": "BACKLOG_SOURCE_UNAVAILABLE",
            "message": (
                "Backlog impact was recorded, but no source attempt and fingerprint "
                "are available for a runnable refinement bridge."
            ),
        },
        {
            "command": "agileforge backlog refine-import",
            "reason": "BACKLOG_SOURCE_UNAVAILABLE",
            "message": (
                "Backlog impact was recorded, but no source attempt and fingerprint "
                "are available for a runnable refinement bridge."
            ),
        },
    ]


def test_active_reset_stale_guard_overrides_triage_none() -> None:
    """Keep active-reset stale guard ahead of impact=none continuation."""
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteTriagedNoneActiveResetReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["status"] == "post_sprint_blocked_by_stale_backlog"
    assert result["data"]["blocked_commands"][0]["reason"] == (
        "DOWNSTREAM_BACKLOG_STALE_AFTER_ACTIVE_RESET"
    )


def test_planned_sprint_start_is_blocked_until_triage_confirms_none() -> None:
    """Block planned Sprint start before post-sprint triage is recorded."""
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompletePlannedSprintMissingTriageReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["data"]["status"] == "post_sprint_triage_required"
    assert result["data"]["blocked_commands"][0]["reason"] == (
        "POST_SPRINT_TRIAGE_REQUIRED"
    )
    assert not any(
        command.startswith("agileforge sprint start")
        for command in result["data"]["next_valid_commands"]
    )


def test_planned_sprint_after_triage_none_blocks_start_bridge() -> None:
    """Block non-executable planned Sprint start after impact=none."""
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteTriagedNonePlannedSprintReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    expected_command = (
        "agileforge sprint start --project-id 7 --sprint-id 21 "
        "--expected-state SPRINT_COMPLETE --idempotency-key <idempotency_key>"
    )
    assert data["status"] == "post_sprint_planned_sprint_start_blocked"
    assert expected_command not in data["next_valid_commands"]
    assert data["blocked_commands"] == [
        {
            "command": expected_command,
            "reason": "POST_SPRINT_PLANNED_SPRINT_START_NOT_IMPLEMENTED",
            "message": (
                "Starting a planned Sprint from SPRINT_COMPLETE is not executable "
                "until a workflow bridge is implemented."
            ),
        }
    ]
    assert all(
        action.get("command") != expected_command or action.get("runnable") is False
        for action in data["next_actions"]
    )
    assert data["next_actions"] == [
        {
            "command": expected_command,
            "status": "post_sprint_planned_sprint_start_blocked",
            "reason": "POST_SPRINT_PLANNED_SPRINT_START_NOT_IMPLEMENTED",
            "runnable": False,
            "installed": True,
            "requires_cli_installation": False,
        }
    ]


def test_refined_backlog_stale_guard_blocks_placeholder_backlog_commands() -> None:
    """Block refined Backlog save/reset commands when guard values are missing."""
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteRefinedBacklogRecordedNoSourceReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "post_sprint_blocked_by_stale_backlog"
    assert data["next_valid_commands"] == [
        "agileforge backlog history --project-id 7"
    ]
    assert not any("<attempt_id>" in command for command in data["next_valid_commands"])
    assert not any(
        "<artifact_fingerprint>" in command
        for command in data["next_valid_commands"]
    )
    assert {
        "command": "agileforge backlog save",
        "reason": "BACKLOG_SOURCE_UNAVAILABLE",
        "message": (
            "Refined Backlog stale routing cannot advertise guarded Backlog "
            "commands until the latest Backlog attempt id and fingerprint are "
            "available."
        ),
    } in data["blocked_commands"]
    assert {
        "command": "agileforge backlog reset-active",
        "reason": "BACKLOG_SOURCE_UNAVAILABLE",
        "message": (
            "Refined Backlog stale routing cannot advertise guarded Backlog "
            "commands until the latest Backlog attempt id and fingerprint are "
            "available."
        ),
    } in data["blocked_commands"]


def test_workflow_next_post_sprint_triage_required_action_reflects_unavailable_triage_command(  # noqa: E501
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reflect unavailable triage commands in the required next action."""
    def unavailable_triage_command(command_name: str) -> bool:
        return command_name != "agileforge sprint triage"

    monkeypatch.setattr(
        application_mod,
        "command_is_available",
        unavailable_triage_command,
    )
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteRequiresTriageReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    triage_command = (
        "agileforge sprint triage --project-id 7 "
        "--expected-state SPRINT_COMPLETE --impact <impact> "
        "--learning-summary <summary> --decision-reason <reason> "
        "--idempotency-key <idempotency_key>"
    )
    assert triage_command not in data["next_valid_commands"]
    assert triage_command in data["blocked_future_commands"]
    assert data["next_actions"] == [
        {
            "command": triage_command,
            "status": "post_sprint_triage_required",
            "reason": (
                "A completed Sprint needs learning triage before next-cycle routing."
            ),
            "runnable": False,
            "installed": False,
            "requires_cli_installation": True,
            "requires": [
                "expected_state",
                "impact",
                "learning_summary",
                "decision_reason",
                "idempotency_key",
            ],
        }
    ]


def test_workflow_next_sprint_complete_stale_reset_wins_before_triage_required() -> (
    None
):
    """Keep stale active-reset blocking ahead of post-sprint triage routing."""
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteStaleRequiresTriageReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "post_sprint_blocked_by_stale_backlog"
    assert data["next_valid_commands"] == []
    assert "next_actions" not in data
    assert data["blocked_commands"] == [
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
    assert result["data"]["status"] == "post_sprint_backlog_refinement_available"


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


def test_workflow_next_routes_rejected_authority_to_installed_regenerate() -> (
    None
):
    """Rejected authority must expose the installed regenerate repair path."""
    app = AgentWorkbenchApplication(
        read_projection=_AuthorityRejectedReadProjection(),
        authority_projection=_RejectedAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        (
            f"agileforge authority regenerate --project-id {PROJECT_ID} "
            f"--spec-version-id {SPEC_VERSION_ID} "
            "--idempotency-key <idempotency_key>"
        )
    ]
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["next_actions"] == [
        {
            "command": (
                f"agileforge authority regenerate --project-id {PROJECT_ID} "
                f"--spec-version-id {SPEC_VERSION_ID} "
                "--idempotency-key <idempotency_key>"
            ),
            "installed": True,
            "requires_cli_installation": False,
            "reason": (
                "Regenerate compiled authority after rejection, then review "
                "the regenerated pending authority before acceptance."
            ),
            "requires": ["idempotency_key"],
        }
    ]
    assert result["data"]["blocked_future_commands"] == []
    assert result["data"]["manual_remediation"] == []


def test_workflow_next_routes_regenerated_rejected_authority_to_review() -> None:
    """A newer pending candidate wins over stale rejected setup state."""
    review = _FakeAuthorityReview()
    app = AgentWorkbenchApplication(
        read_projection=_AuthorityRejectedReadProjection(),
        authority_projection=_RejectedWithPendingAuthorityProjection(),
        authority_review=review,
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == []
    assert result["data"]["blocked_commands"] == []
    assert result["data"]["blocked_future_commands"] == []
    assert len(result["data"]["next_actions"]) == 1
    assert result["data"]["next_actions"][0]["command"] == (
        f"agileforge authority review --project-id {PROJECT_ID} --open"
    )
    assert result["data"]["next_actions"][0]["installed"] is True
    assert result["data"]["decision_actions_after_review"][0]["command"] == (
        f"agileforge authority accept --project-id {PROJECT_ID}"
    )
    assert review.calls == [
        {
            "project_id": PROJECT_ID,
            "include_spec": "summary",
            "output_format": "json",
        }
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
        authority_projection=_RejectedAuthorityProjection(),
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
    assert result["data"]["blocked_future_commands"] == []
    assert result["data"]["next_valid_commands"] == [
        (
            "agileforge authority regenerate --project-id 7 "
            "--spec-version-id 3 --idempotency-key <idempotency_key>"
        )
    ]
    assert result["data"]["next_actions"][0]["command"] == (
        "agileforge authority regenerate --project-id 7 "
        "--spec-version-id 3 --idempotency-key <idempotency_key>"
    )
    assert result["data"]["next_actions"][0]["installed"] is True


def test_workflow_next_routes_failed_setup_to_retry_action() -> None:
    """Route failed setup to setup retry instead of sprint planning."""
    app = AgentWorkbenchApplication(
        read_projection=_SetupFailedReadProjection(),
        authority_projection=_FakeAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == []
    assert result["data"]["blocked_commands"] == [
        {
            "command": (
                "agileforge project setup retry --project-id 7 "
                "--spec-file <spec-file> --expected-state SETUP_REQUIRED "
                "--expected-context-fingerprint <expected_context_fingerprint>"
            ),
            "installed": True,
            "reason": (
                "Setup retry requires setup_spec_file_path in workflow state "
                "before a runnable guard can be computed."
            ),
        }
    ]


def test_workflow_next_failed_setup_publishes_runnable_retry_guards(
    tmp_path: Path,
) -> None:
    """Publish concrete setup retry guards when failed setup has a spec path."""
    spec_file = tmp_path / "spec file.json"
    spec_file.write_text(json.dumps(_structured_spec_payload()), encoding="utf-8")
    workflow_state = {
        "fsm_state": "SETUP_REQUIRED",
        "setup_status": "failed",
        "setup_spec_file_path": str(spec_file),
    }
    expected_fingerprint = setup_retry_context_fingerprint(
        project_id=PROJECT_ID,
        resolved_spec_path=spec_file.resolve(),
        workflow_state=workflow_state,
    )
    app = AgentWorkbenchApplication(
        read_projection=_SetupFailedReadProjection(spec_file_path=str(spec_file)),
        authority_projection=_FakeAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        (
            f"agileforge project setup retry --project-id {PROJECT_ID} "
            f"--spec-file {quote(str(spec_file.resolve()))} "
            "--expected-state SETUP_REQUIRED "
            f"--expected-context-fingerprint {expected_fingerprint}"
        )
    ]
    assert result["data"]["blocked_commands"] == []


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
    assert result["data"]["next_valid_commands"] == []
    assert result["data"]["blocked_commands"] == [
        {
            "command": (
                "agileforge project setup retry --project-id 7 "
                "--spec-file <spec-file> --expected-state SETUP_REQUIRED "
                "--expected-context-fingerprint <expected_context_fingerprint>"
            ),
            "installed": True,
            "reason": (
                "Setup retry requires setup_spec_file_path in workflow state "
                "before a runnable guard can be computed."
            ),
        }
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
