"""Tests for story phase service."""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from repositories.session import WorkflowSessionRepository
from services.agent_workbench.fingerprints import canonical_hash
from services.interview_runtime import reset_subject_working_set
from services.phases import story_service
from services.phases.story_service import (
    StoryPhaseError,
    _story_artifact_fingerprint,
    complete_story_phase,
    delete_story_requirement,
    generate_story_draft,
    get_story_history,
    get_story_pending,
    merge_story_resolution,
    repair_story_completion_scope,
    repair_story_readiness,
    retry_story_draft,
    save_story_draft,
    save_story_patch,
    story_parent_rank,
    story_quality_summary,
)
from utils.runtime_config import WORKFLOW_RUNNER_IDENTITY, resolve_database_target

JsonDict = dict[str, Any]
_TEST_SESSION_ID = "2"


def _session_repo_with_state(
    tmp_path: Path,
    state: JsonDict,
) -> WorkflowSessionRepository:
    """Create a real session repository row for persistence regressions."""
    db_path = tmp_path / "sessions.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                app_name TEXT NOT NULL,
                user_id TEXT NOT NULL,
                id TEXT NOT NULL,
                state TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO sessions (app_name, user_id, id, state)
            VALUES (?, ?, ?, ?)
            """,
            (
                WORKFLOW_RUNNER_IDENTITY.app_name,
                WORKFLOW_RUNNER_IDENTITY.user_id,
                _TEST_SESSION_ID,
                json.dumps(state),
            ),
        )
    return WorkflowSessionRepository(
        db_target=resolve_database_target(
            str(db_path),
            env_name="AGILEFORGE_SESSION_DB_URL",
        )
    )


def _load_repo_story_state(repo: WorkflowSessionRepository) -> JsonDict:
    """Load persisted Story state from a test session repository."""
    return repo.get_session_state(
        WORKFLOW_RUNNER_IDENTITY.app_name,
        WORKFLOW_RUNNER_IDENTITY.user_id,
        _TEST_SESSION_ID,
    )


def _save_repo_story_state(
    repo: WorkflowSessionRepository,
    state: JsonDict,
) -> None:
    """Persist Story state through the real merge-update session repository."""
    repo.update_session_state(
        WORKFLOW_RUNNER_IDENTITY.app_name,
        WORKFLOW_RUNNER_IDENTITY.user_id,
        _TEST_SESSION_ID,
        state,
    )


def _story_runtime_for(state: JsonDict, parent_requirement: str) -> JsonDict:
    """Return typed story runtime state for a parent requirement."""
    interview_runtime = cast("JsonDict", state["interview_runtime"])
    story_runtime = cast("dict[str, JsonDict]", interview_runtime["story"])
    return story_runtime[parent_requirement]


def _story_artifact(
    parent_requirement: str, title: str, *, is_complete: bool = True
) -> JsonDict:
    return {
        "parent_requirement": parent_requirement,
        "user_stories": [
            {
                "story_title": title,
                "statement": "As a developer, I want projection-aware drafts, so that retries and saves stay stable.",  # noqa: E501
                "acceptance_criteria": [
                    "Verify the service reads the reusable projection."
                ],
                "invest_score": "High",
                "estimated_effort": "S",
                "produced_artifacts": [],
            }
        ],
        "is_complete": is_complete,
        "clarifying_questions": [],
    }


def _merge_recommended_artifact(parent_requirement: str) -> JsonDict:
    artifact = _story_artifact(
        parent_requirement,
        "Validate execution evidence meets submission standards",
        is_complete=False,
    )
    artifact["user_stories"][0]["invest_score"] = "Low"
    artifact["user_stories"][0]["acceptance_criteria"] = [
        "Move the validation checklist into the owning requirement.",
    ]
    artifact["user_stories"][0]["decomposition_warning"] = (
        "Artifact 'application_execution_evidence' is owned by "
        "'Updated Source Code Package (refactored prototype for submission)' "
        "which already has a creation story. Recommend consolidating: merge this "
        "validation into the evidence creation story and retire this separate requirement."  # noqa: E501
    )
    return artifact


def _state_with_complete_story_draft(
    parent_requirement: str = "Requirement A",
) -> JsonDict:
    artifact = _story_artifact(parent_requirement, "Saved draft")
    artifact_fingerprint = _story_artifact_fingerprint(parent_requirement, artifact)
    artifact["artifact_fingerprint"] = artifact_fingerprint
    return {
        "roadmap_releases": [{"items": [parent_requirement]}],
        "fsm_state": "STORY_REVIEW",
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "phase": "story",
                    "subject_key": parent_requirement,
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "classification": "reusable_content_result",
                            "is_reusable": True,
                            "retryable": False,
                            "draft_kind": "complete_draft",
                            "artifact_fingerprint": artifact_fingerprint,
                            "output_artifact": artifact,
                        }
                    ],
                    "draft_projection": {
                        "latest_reusable_attempt_id": "attempt-1",
                        "kind": "complete_draft",
                        "is_complete": True,
                        "artifact_fingerprint": artifact_fingerprint,
                    },
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                }
            }
        },
    }


def _story_patch_artifact(
    parent_requirement: str = "Requirement A",
    title: str = "Refined target story",
    *,
    target_refinement_slot: int = 2,
) -> JsonDict:
    return {
        "artifact_kind": "story_patch",
        "parent_requirement": parent_requirement,
        "target_refinement_slot": target_refinement_slot,
        "story": {
            "story_title": title,
            "statement": (
                "As a developer, I want a refined target story, so that it is "
                "ready for sprint planning."
            ),
            "acceptance_criteria": ["Verify that the target story is actionable."],
            "invest_score": "High",
            "estimated_effort": "S",
            "produced_artifacts": [],
        },
        "is_complete": True,
        "clarifying_questions": [],
    }


def _state_with_story_patch_draft(
    parent_requirement: str = "Requirement A",
) -> JsonDict:
    artifact = _story_patch_artifact(parent_requirement)
    artifact_fingerprint = _story_artifact_fingerprint(parent_requirement, artifact)
    artifact["artifact_fingerprint"] = artifact_fingerprint
    return {
        "roadmap_releases": [{"items": [parent_requirement]}],
        "fsm_state": "STORY_REVIEW",
        "story_outputs": {
            parent_requirement: {
                "parent_requirement": parent_requirement,
                "user_stories": [
                    {
                        "story_title": "Protected sibling",
                        "statement": (
                            "As a user, I want sibling work preserved, so that "
                            "completed scope is stable."
                        ),
                        "acceptance_criteria": ["Verify sibling preservation."],
                        "invest_score": "High",
                        "estimated_effort": "S",
                        "produced_artifacts": [],
                    },
                    {
                        "story_title": "Old target story",
                        "statement": (
                            "As a user, I want target work refined, so that the "
                            "next draft is better."
                        ),
                        "acceptance_criteria": ["Verify target replacement."],
                        "invest_score": "High",
                        "estimated_effort": "S",
                        "produced_artifacts": [],
                    },
                ],
                "is_complete": True,
                "clarifying_questions": [],
            }
        },
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "phase": "story",
                    "subject_key": parent_requirement,
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "classification": "reusable_content_result",
                            "is_reusable": True,
                            "retryable": False,
                            "draft_kind": "story_patch",
                            "target_refinement_slot": 2,
                            "artifact_fingerprint": artifact_fingerprint,
                            "output_artifact": artifact,
                        }
                    ],
                    "draft_projection": {
                        "latest_reusable_attempt_id": "attempt-1",
                        "kind": "story_patch",
                        "is_complete": True,
                        "target_refinement_slot": 2,
                        "artifact_fingerprint": artifact_fingerprint,
                    },
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                }
            }
        },
    }


def _scope_extension_state_with_legacy_story_draft() -> JsonDict:
    """Return scope-extension state with stale same-name Story runtime."""
    parent_requirement = "Shared requirement"
    artifact = _story_artifact(parent_requirement, "Legacy saved-looking draft")
    artifact_fingerprint = _story_artifact_fingerprint(parent_requirement, artifact)
    artifact["artifact_fingerprint"] = artifact_fingerprint
    legacy_attempt: JsonDict = {
        "attempt_id": "attempt-1",
        "created_at": "2026-06-09T10:45:25Z",
        "classification": "reusable_content_result",
        "is_reusable": True,
        "retryable": False,
        "draft_kind": "complete_draft",
        "artifact_fingerprint": artifact_fingerprint,
        "input_context": {
            "requirement_context": "Part of Release: Old milestone",
        },
        "output_artifact": artifact,
    }
    return {
        "scope_extension_context": {
            "schema": "agileforge.scope_extension.v1",
            "base_spec_version_id": 7,
            "amended_spec_version_id": 12,
            "added_source_item_ids": ["SRC-NEW"],
        },
        "roadmap_releases": [
            {
                "theme": "Old milestone",
                "reasoning": "Old scope.",
                "items": [parent_requirement],
            },
            {
                "theme": "Extension milestone",
                "reasoning": "New amended scope.",
                "items": [parent_requirement],
                "extension_of_spec_version_id": 7,
                "accepted_spec_version_id": 12,
                "source_item_ids": ["SRC-NEW"],
            },
        ],
        "fsm_state": "STORY_INTERVIEW",
        "story_attempts": {
            parent_requirement: [
                {
                    "created_at": legacy_attempt["created_at"],
                    "output_artifact": artifact,
                    "is_complete": True,
                }
            ]
        },
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "phase": "story",
                    "subject_key": parent_requirement,
                    "attempt_history": [legacy_attempt],
                    "draft_projection": {
                        "latest_reusable_attempt_id": "attempt-1",
                        "kind": "complete_draft",
                        "is_complete": True,
                        "artifact_fingerprint": artifact_fingerprint,
                    },
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {
                        "payload": {"parent_requirement": parent_requirement}
                    },
                }
            }
        },
    }


def _mark_current_story_draft_scope_extension(
    state: JsonDict,
    parent_requirement: str,
) -> None:
    """Tag the active Story draft as generated for the amended extension scope."""
    runtime = _story_runtime_for(state, parent_requirement)
    extension_metadata: JsonDict = {
        "extension_scope": True,
        "accepted_spec_version_id": 12,
        "source_item_ids": ["SRC-NEW"],
    }
    draft_projection = runtime.get("draft_projection")
    if isinstance(draft_projection, dict):
        draft_projection.update(extension_metadata)
    attempts = runtime.get("attempt_history")
    if isinstance(attempts, list):
        for attempt in attempts:
            if isinstance(attempt, dict) and attempt.get("attempt_id") == "attempt-1":
                attempt.update(extension_metadata)


def test_story_quality_summary_projects_runtime_failure_as_blocking_finding() -> None:
    """Runtime failures should not look like empty clarification drafts."""
    summary = story_quality_summary(
        {
            "error": "STORY_GENERATION_FAILED",
            "message": "Story response is not valid JSON",
            "failure_stage": "invalid_json",
            "failure_summary": "Story response is not valid JSON",
            "failure_artifact_id": "story-failure-1",
            "is_complete": False,
        }
    )

    assert summary["saveable"] is False
    assert summary["story_count"] == 0
    assert summary["coverage_status"] == "needs_clarification"
    assert summary["blocking_findings"] == [
        {
            "code": "STORY_RUNTIME_FAILURE",
            "severity": "blocking",
            "message": "Story response is not valid JSON",
            "failure_stage": "invalid_json",
            "failure_artifact_id": "story-failure-1",
        }
    ]


def test_story_quality_summary_deduplicates_runtime_failure_finding() -> None:
    """Runtime failure projection should not duplicate existing quality findings."""
    runtime_finding = {
        "code": "STORY_RUNTIME_FAILURE",
        "severity": "blocking",
        "message": "Story response is not valid JSON",
        "failure_stage": "invalid_json",
        "failure_artifact_id": "story-failure-1",
    }
    summary = story_quality_summary(
        {
            "error": "STORY_GENERATION_FAILED",
            "message": "Story response is not valid JSON",
            "failure_stage": "invalid_json",
            "failure_summary": "Story response is not valid JSON",
            "failure_artifact_id": "story-failure-1",
            "quality_findings": [runtime_finding],
            "is_complete": False,
        }
    )

    assert summary["blocking_findings"] == [runtime_finding]


def test_story_save_payload_blocks_complete_all_low_artifact() -> None:
    """Complete all-Low drafts are not saveable even without quality metadata."""
    state = _state_with_complete_story_draft()
    runtime = _story_runtime_for(state, "Requirement A")
    artifact = runtime["attempt_history"][0]["output_artifact"]
    artifact["user_stories"][0]["invest_score"] = "Low"
    artifact["user_stories"][0]["decomposition_warning"] = (
        "Story is too broad to satisfy INVEST decomposition."
    )

    assert story_service.story_save_payload(runtime) is None


def test_story_artifact_fingerprint_ignores_existing_guard_metadata() -> None:
    """Verify story artifact fingerprint ignores existing guard metadata."""
    parent_requirement = "Requirement A"
    artifact = _story_artifact(parent_requirement, "Guarded story")
    guarded_artifact = {
        **artifact,
        "attempt_id": "attempt-9",
        "artifact_fingerprint": "sha256:stale",
    }

    assert _story_artifact_fingerprint(parent_requirement, guarded_artifact) == (
        _story_artifact_fingerprint(parent_requirement, artifact)
    )


def test_story_parent_rank_uses_roadmap_order() -> None:
    """Verify story parent rank follows flattened Roadmap order."""
    state = {
        "roadmap_releases": [
            {"items": ["Requirement A", "Requirement B"]},
            {"items": ["Requirement C"]},
        ]
    }

    assert story_parent_rank(state, "Requirement A") == 1
    assert story_parent_rank(state, "Requirement B") == 2  # noqa: PLR2004
    assert story_parent_rank(state, "Requirement C") == 3  # noqa: PLR2004


def test_story_parent_rank_matches_normalized_requirement() -> None:
    """Verify story parent rank matches normalized requirement names."""
    state = {"roadmap_releases": [{"items": ["Live Pre-Lock Recommendation"]}]}

    assert story_parent_rank(state, " live   pre-lock recommendation ") == 1


def _pending_state() -> JsonDict:
    return {
        "roadmap_releases": [
            {
                "theme": "Milestone 1",
                "reasoning": "First slice",
                "items": [
                    "Requirement A",
                    "Requirement B",
                ],
            }
        ],
        "story_saved": {"Requirement A": True},
        "story_attempts": {
            "Requirement A": [
                {
                    "created_at": "2026-03-28T10:00:00Z",
                    "trigger": "manual_refine",
                    "input_context": {},
                    "output_artifact": _story_artifact("Requirement A", "Saved draft"),
                    "is_complete": True,
                    "failure_artifact_id": None,
                    "failure_stage": None,
                    "failure_summary": None,
                    "raw_output_preview": None,
                    "has_full_artifact": False,
                }
            ],
            "Requirement B": [
                {
                    "created_at": "2026-03-28T10:00:00Z",
                    "trigger": "manual_refine",
                    "input_context": {},
                    "output_artifact": {},
                    "is_complete": False,
                    "failure_artifact_id": None,
                    "failure_stage": None,
                    "failure_summary": None,
                    "raw_output_preview": None,
                    "has_full_artifact": False,
                }
            ],
        },
        "interview_runtime": {
            "story": {
                "Requirement A": {
                    "phase": "story",
                    "subject_key": "Requirement A",
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "trigger": "manual_refine",
                            "input_context": {},
                            "output_artifact": _story_artifact(
                                "Requirement A",
                                "Saved draft",
                            ),
                            "classification": "reusable_content_result",
                            "is_reusable": True,
                            "retryable": False,
                            "draft_kind": "complete_draft",
                        }
                    ],
                    "draft_projection": {
                        "latest_reusable_attempt_id": "attempt-1",
                        "kind": "complete_draft",
                        "is_complete": True,
                    },
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                },
                "Requirement B": {
                    "phase": "story",
                    "subject_key": "Requirement B",
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "trigger": "manual_refine",
                            "input_context": {},
                            "output_artifact": {},
                            "classification": "nonreusable_provider_failure",
                            "is_reusable": False,
                            "retryable": True,
                            "draft_kind": None,
                        }
                    ],
                    "draft_projection": {
                        "latest_reusable_attempt_id": None,
                        "kind": "incomplete_draft",
                        "is_complete": False,
                    },
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                },
            }
        },
    }


@pytest.mark.asyncio
async def test_get_story_history_returns_attempts_and_projection_summary() -> None:
    """Verify get story history returns attempts and projection summary."""
    state: JsonDict = {
        "interview_runtime": {
            "story": {
                "Requirement A": {
                    "phase": "story",
                    "subject_key": "Requirement A",
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "classification": "reusable_content_result",
                            "is_reusable": True,
                            "retryable": False,
                            "draft_kind": "complete_draft",
                            "output_artifact": _story_artifact(
                                "Requirement A", "Saved draft"
                            ),
                        },
                        {
                            "attempt_id": "attempt-2",
                            "classification": "nonreusable_provider_failure",
                            "is_reusable": False,
                            "retryable": True,
                            "draft_kind": None,
                            "output_artifact": {
                                "error": "STORY_GENERATION_FAILED",
                                "message": "provider timeout",
                            },
                        },
                    ],
                    "draft_projection": {
                        "latest_reusable_attempt_id": "attempt-1",
                        "kind": "complete_draft",
                        "is_complete": True,
                    },
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {
                        "request_snapshot_id": "request-2",
                        "payload": {"parent_requirement": "Requirement A"},
                    },
                }
            }
        }
    }

    payload = await get_story_history(
        parent_requirement="  Requirement A  ",
        load_state=lambda: _async_value(state),
    )

    assert payload["parent_requirement"] == "Requirement A"
    data = payload["data"]
    assert data["count"] == 2  # noqa: PLR2004
    assert data["items"][0]["attempt_id"] == "attempt-1"
    assert data["current_draft"] == {
        "attempt_id": "attempt-1",
        "kind": "complete_draft",
        "is_complete": True,
    }
    assert data["retry"] == {
        "available": True,
        "target_attempt_id": "attempt-2",
    }
    assert data["save"] == {"available": True}


def _pending_selection_fields(*, eligible: bool, reason: str) -> JsonDict:
    return {
        "sprint_eligible": eligible,
        "sprint_eligibility_reason": reason,
        "stories": [],
        "story_ids": [],
    }


@pytest.mark.asyncio
async def test_get_story_pending_groups_requirements_by_status() -> None:
    """Verify get story pending groups requirements by status."""
    state = _pending_state()

    payload = await get_story_pending(load_state=lambda: _async_value(state))

    assert payload["total_count"] == 2  # noqa: PLR2004
    assert payload["saved_count"] == 1
    assert payload["grouped_items"] == [
        {
            "group_id": "milestone_0",
            "theme": "Milestone 1",
            "reasoning": "First slice",
            "requirements": [
                    {
                        "requirement": "Requirement A",
                        "status": "Saved",
                        "attempt_count": 1,
                        **_pending_selection_fields(
                            eligible=True,
                            reason="eligible",
                        ),
                    },
                    {
                        "requirement": "Requirement B",
                        "status": "Attempted",
                        "attempt_count": 1,
                        **_pending_selection_fields(
                            eligible=False,
                            reason="attempt_in_progress",
                        ),
                    },
                ],
            }
    ]


@pytest.mark.asyncio
async def test_get_story_pending_marks_requirement_reconciliation_handled() -> (
    None
):
    """Terminal requirement reconciliation is visible and counted as handled."""
    state = _pending_state()
    state["requirement_reconciliations"] = {
        "requirement b": {
            "schema_version": "agileforge.requirement_reconciliation.v1",
            "requirement": "Requirement B",
            "action": "already-implemented",
            "reason": "Delivered in Sprint 7.",
            "evidence_links": ["sprint-7-closeout.md"],
            "changed_by": "agent",
            "reconciled_at": "2026-06-25T10:00:00Z",
            "idempotency_key": "req-rec-1",
            "terminal": True,
        }
    }

    payload = await get_story_pending(load_state=lambda: _async_value(state))

    assert payload["total_count"] == 2  # noqa: PLR2004
    assert payload["saved_count"] == 1
    assert payload["reconciled_count"] == 1
    assert payload["handled_count"] == 2  # noqa: PLR2004
    assert payload["grouped_items"][0]["requirements"][1] == {
        "requirement": "Requirement B",
        "status": "Reconciled",
        "attempt_count": 1,
        **_pending_selection_fields(
            eligible=False,
            reason="reconciled",
        ),
        "reconciliation": {
            "action": "already-implemented",
            "reason": "Delivered in Sprint 7.",
            "evidence_links": ["sprint-7-closeout.md"],
            "changed_by": "agent",
            "reconciled_at": "2026-06-25T10:00:00Z",
            "terminal": True,
        },
    }


@pytest.mark.asyncio
async def test_get_story_pending_scope_extension_filters_appended_requirements() -> (
    None
):
    """Scope extension pending only exposes appended extension Roadmap items."""
    state: JsonDict = {
        "scope_extension_context": {
            "schema": "agileforge.scope_extension.v1",
            "base_spec_version_id": 7,
            "amended_spec_version_id": 12,
            "added_source_item_ids": ["SRC-NEW"],
        },
        "roadmap_releases": [
            {
                "theme": "Original milestone",
                "reasoning": "Already delivered.",
                "items": ["Original requirement"],
            },
            {
                "theme": "Extension milestone",
                "reasoning": "New amended scope.",
                "items": ["Extension requirement"],
                "extension_of_spec_version_id": 7,
                "accepted_spec_version_id": 12,
                "source_item_ids": ["SRC-NEW"],
            },
        ],
        "story_saved": {"Original requirement": True},
    }

    payload = await get_story_pending(load_state=lambda: _async_value(state))

    assert payload["total_count"] == 1
    assert payload["saved_count"] == 0
    assert payload["grouped_items"] == [
        {
            "group_id": "milestone_1",
            "theme": "Extension milestone",
            "reasoning": "New amended scope.",
            "extension_scope": True,
            "accepted_spec_version_id": 12,
            "source_item_ids": ["SRC-NEW"],
            "requirements": [
                {
                    "requirement": "Extension requirement",
                    "status": "Pending",
                    "attempt_count": 0,
                        "extension_scope": True,
                        "accepted_spec_version_id": 12,
                        "source_item_ids": ["SRC-NEW"],
                        **_pending_selection_fields(
                            eligible=False,
                            reason="pending_refinement",
                        ),
                    }
                ],
            }
    ]


@pytest.mark.asyncio
async def test_get_story_pending_scope_extension_does_not_use_legacy_same_name_saved_flag() -> (  # noqa: E501
    None
):
    """Extension pending status is based on extension provenance, not legacy names."""
    state: JsonDict = {
        "scope_extension_context": {
            "schema": "agileforge.scope_extension.v1",
            "base_spec_version_id": 7,
            "amended_spec_version_id": 12,
            "added_source_item_ids": ["SRC-NEW"],
        },
        "roadmap_releases": [
            {
                "theme": "Original milestone",
                "reasoning": "Old scope.",
                "items": ["Shared requirement"],
            },
            {
                "theme": "Extension milestone",
                "reasoning": "New amended scope.",
                "items": ["Shared requirement"],
                "extension_of_spec_version_id": 7,
                "accepted_spec_version_id": 12,
                "source_item_ids": ["SRC-NEW"],
            },
        ],
        "story_saved": {"Shared requirement": True},
    }

    payload = await get_story_pending(load_state=lambda: _async_value(state))

    assert payload["saved_count"] == 0
    assert payload["grouped_items"] == [
        {
            "group_id": "milestone_1",
            "theme": "Extension milestone",
            "reasoning": "New amended scope.",
            "extension_scope": True,
            "accepted_spec_version_id": 12,
            "source_item_ids": ["SRC-NEW"],
            "requirements": [
                {
                    "requirement": "Shared requirement",
                    "status": "Pending",
                    "attempt_count": 0,
                        "extension_scope": True,
                        "accepted_spec_version_id": 12,
                        "source_item_ids": ["SRC-NEW"],
                        **_pending_selection_fields(
                            eligible=False,
                            reason="pending_refinement",
                        ),
                    }
                ],
            }
    ]


@pytest.mark.asyncio
async def test_get_story_pending_scope_extension_does_not_use_legacy_same_name_draft() -> (  # noqa: E501
    None
):
    """Extension pending status ignores stale same-name Story drafts."""
    state = _scope_extension_state_with_legacy_story_draft()

    payload = await get_story_pending(load_state=lambda: _async_value(state))

    assert payload["saved_count"] == 0
    assert payload["grouped_items"] == [
        {
            "group_id": "milestone_1",
            "theme": "Extension milestone",
            "reasoning": "New amended scope.",
            "extension_scope": True,
            "accepted_spec_version_id": 12,
            "source_item_ids": ["SRC-NEW"],
            "requirements": [
                {
                    "requirement": "Shared requirement",
                    "status": "Pending",
                    "attempt_count": 0,
                        "extension_scope": True,
                        "accepted_spec_version_id": 12,
                        "source_item_ids": ["SRC-NEW"],
                        **_pending_selection_fields(
                            eligible=False,
                            reason="pending_refinement",
                        ),
                    }
                ],
            }
    ]


@pytest.mark.asyncio
async def test_get_story_history_scope_extension_does_not_advertise_stale_draft_save() -> (  # noqa: E501
    None
):
    """Story history does not expose save guards for stale scope drafts."""
    state = _scope_extension_state_with_legacy_story_draft()

    payload = await get_story_history(
        parent_requirement="Shared requirement",
        load_state=lambda: _async_value(state),
    )

    data = payload["data"]
    assert data["current_draft"] is None
    assert data["save"] == {"available": False}


@pytest.mark.asyncio
async def test_generate_story_draft_normalizes_requirement_and_persists_reusable_output() -> (  # noqa: E501
    None
):
    """Verify generate story draft normalizes requirement and persists reusable output."""  # noqa: E501
    refinement_feedback = """
Target:
Requirement A, latest story draft

Issue:
Draft needs a narrower milestone boundary.

Evidence:
Current feedback requests one milestone only.

Required change:
Keep the stories to one milestone.

Acceptance criteria:
- Stories cover one milestone.
- Each story has a single user goal.
- Draft remains saveable.

Scope limit:
Do not add cross-milestone work.
"""
    state: JsonDict = {
        "roadmap_releases": [
            {
                "items": ["Requirement A"],
            }
        ],
        "interview_runtime": {
            "story": {
                "Requirement A": {
                    "phase": "story",
                    "subject_key": "Requirement A",
                    "attempt_history": [],
                    "draft_projection": {},
                    "feedback_projection": {
                        "items": [
                            {
                                "feedback_id": "feedback-1",
                                "text": refinement_feedback,
                                "created_at": "2026-03-28T09:59:00Z",
                                "status": "unabsorbed",
                                "absorbed_by_attempt_id": None,
                            }
                        ],
                        "next_feedback_sequence": 1,
                    },
                    "request_projection": {},
                }
            }
        },
    }
    saved_states: list[JsonDict] = []
    captured: JsonDict = {}

    async def fake_run_story_agent_from_state(
        state_arg: JsonDict,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None,
    ) -> JsonDict:
        assert project_id == 7  # noqa: PLR2004
        assert parent_requirement == "Requirement A"
        assert user_input is None
        captured["feedback"] = state_arg["interview_runtime"]["story"]["Requirement A"][
            "feedback_projection"
        ]["items"]
        return {
            "success": True,
            "input_context": {"requirement_context": "assembled"},
            "output_artifact": _story_artifact("Requirement A", "Story A"),
            "classification": "reusable_content_result",
            "draft_kind": "complete_draft",
            "is_reusable": True,
            "is_complete": True,
            "request_payload": {"parent_requirement": "Requirement A"},
            "error": None,
            "failure_artifact_id": None,
            "failure_stage": None,
            "failure_summary": None,
            "raw_output_preview": None,
            "has_full_artifact": False,
        }

    payload = await generate_story_draft(
        project_id=7,
        parent_requirement="  Requirement A  ",
        user_input=refinement_feedback,
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-04-04T12:00:00Z",
        run_story_agent_from_state=fake_run_story_agent_from_state,
        append_feedback_entry=lambda runtime, text, created_at, **_kwargs: runtime[
            "feedback_projection"
        ]["items"].append(
            {
                "feedback_id": f"feedback-{len(runtime['feedback_projection']['items']) + 1}",  # noqa: E501
                "text": text,
                "created_at": created_at,
                "status": "unabsorbed",
                "absorbed_by_attempt_id": None,
            }
        ),
        set_request_projection=lambda runtime, **kwargs: (
            runtime.setdefault("request_projection", {}).update(kwargs)
            or runtime["request_projection"]
        ),
        append_attempt=lambda runtime, attempt: runtime.setdefault(
            "attempt_history", []
        ).append(attempt),
        promote_reusable_draft=lambda runtime, **kwargs: runtime.setdefault(
            "draft_projection", {}
        ).update(
            {
                "latest_reusable_attempt_id": kwargs["attempt_id"],
                "kind": kwargs["kind"],
                "is_complete": kwargs["is_complete"],
                "updated_at": kwargs["updated_at"],
            }
        ),
        mark_feedback_absorbed=lambda runtime, *, feedback_ids, attempt_id: [
            item.update({"status": "absorbed", "absorbed_by_attempt_id": attempt_id})
            for item in runtime["feedback_projection"]["items"]
            if item["feedback_id"] in set(feedback_ids)
        ],
        failure_meta=lambda story_result, fallback_summary: {},  # noqa: ARG005
    )

    assert payload["parent_requirement"] == "Requirement A"
    assert (
        payload["data"]["output_artifact"]["user_stories"][0]["story_title"]
        == "Story A"
    )
    current_draft = payload["data"]["current_draft"]
    assert current_draft["attempt_id"] == "attempt-1"
    assert current_draft["kind"] == "complete_draft"
    assert current_draft["is_complete"] is True
    assert current_draft["artifact_fingerprint"].startswith("sha256:")
    assert captured["feedback"][0]["status"] == "absorbed"
    assert state["interview_runtime"]["story"]["Requirement A"]["request_projection"][
        "payload"
    ] == {"parent_requirement": "Requirement A"}
    assert (
        state["story_outputs"]["Requirement A"]["user_stories"][0]["story_title"]
        == "Story A"
    )
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_generate_story_draft_with_target_slot_stores_story_patch_attempt() -> (
    None
):
    """Targeted generation stores a one-story patch artifact, not a full list."""
    parent_requirement = "Requirement A"
    target_slot = 2
    patch_artifact: JsonDict = {
        "artifact_kind": "story_patch",
        "parent_requirement": parent_requirement,
        "target_refinement_slot": target_slot,
        "story": {
            "story_title": "Refined target story",
            "statement": (
                "As a developer, I want a refined target story, so that it is "
                "ready for sprint planning."
            ),
            "acceptance_criteria": ["Verify that the target story is actionable."],
            "invest_score": "High",
            "estimated_effort": "S",
            "produced_artifacts": [],
        },
        "is_complete": True,
        "clarifying_questions": [],
    }
    state: JsonDict = {
        "roadmap_releases": [{"items": [parent_requirement]}],
        "story_outputs": {
            parent_requirement: {
                "parent_requirement": parent_requirement,
                "user_stories": [
                    {
                        "story_title": "Protected sibling",
                        "statement": (
                            "As a user, I want sibling work preserved, so that "
                            "completed scope is stable."
                        ),
                        "acceptance_criteria": ["Verify sibling preservation."],
                        "invest_score": "High",
                        "estimated_effort": "S",
                        "produced_artifacts": [],
                    },
                    {
                        "story_title": "Old target story",
                        "statement": (
                            "As a user, I want target work refined, so that the "
                            "next draft is better."
                        ),
                        "acceptance_criteria": ["Verify target replacement."],
                        "invest_score": "High",
                        "estimated_effort": "S",
                        "produced_artifacts": [],
                    },
                ],
                "is_complete": True,
                "clarifying_questions": [],
            }
        },
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "phase": "story",
                    "subject_key": parent_requirement,
                    "attempt_history": [],
                    "draft_projection": {},
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                }
            }
        },
    }
    saved_states: list[JsonDict] = []

    async def fake_run_story_agent_from_state(  # noqa: PLR0913
        state_arg: JsonDict,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None,
        target_story_id: int | None = None,
        target_refinement_slot: int | None = None,
    ) -> JsonDict:
        del state_arg, project_id, parent_requirement, user_input
        assert target_story_id is None
        assert target_refinement_slot == target_slot
        return {
            "success": True,
            "input_context": {"requirement_context": "assembled"},
            "output_artifact": patch_artifact,
            "classification": "reusable_content_result",
            "draft_kind": "story_patch",
            "is_reusable": True,
            "is_complete": True,
            "request_payload": {
                "parent_requirement": "Requirement A",
                "target_refinement_slot": target_slot,
            },
            "error": None,
            "failure_artifact_id": None,
            "failure_stage": None,
            "failure_summary": None,
            "raw_output_preview": None,
            "has_full_artifact": False,
        }

    payload = await generate_story_draft(
        project_id=7,
        parent_requirement=parent_requirement,
        user_input="Refine slot 2 only",
        target_story_id=None,
        target_refinement_slot=target_slot,
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-04-04T12:00:00Z",
        run_story_agent_from_state=fake_run_story_agent_from_state,
        append_feedback_entry=lambda runtime, text, created_at, **_kwargs: runtime[
            "feedback_projection"
        ]["items"].append(
            {
                "feedback_id": f"feedback-{len(runtime['feedback_projection']['items']) + 1}",  # noqa: E501
                "text": text,
                "created_at": created_at,
                "status": "unabsorbed",
                "absorbed_by_attempt_id": None,
            }
        ),
        set_request_projection=lambda runtime, **kwargs: (
            runtime.setdefault("request_projection", {}).update(kwargs)
            or runtime["request_projection"]
        ),
        append_attempt=lambda runtime, attempt: runtime.setdefault(
            "attempt_history", []
        ).append(attempt),
        promote_reusable_draft=lambda runtime, **kwargs: runtime.setdefault(
            "draft_projection",
            {},
        ).update(
            {
                "latest_reusable_attempt_id": kwargs["attempt_id"],
                "kind": kwargs["kind"],
                "is_complete": kwargs["is_complete"],
                "updated_at": kwargs["updated_at"],
            }
        ),
        mark_feedback_absorbed=lambda runtime, *, feedback_ids, attempt_id: [
            item.update({"status": "absorbed", "absorbed_by_attempt_id": attempt_id})
            for item in runtime["feedback_projection"]["items"]
            if item["feedback_id"] in set(feedback_ids)
        ],
        failure_meta=lambda story_result, fallback_summary: {},  # noqa: ARG005
    )

    runtime = _story_runtime_for(state, parent_requirement)
    attempt = runtime["attempt_history"][-1]
    output_artifact = attempt["output_artifact"]
    assert payload["fsm_state"] == "STORY_REVIEW"
    assert payload["data"]["current_draft"]["kind"] == "story_patch"
    assert payload["data"]["current_draft"]["target_refinement_slot"] == target_slot
    assert attempt["draft_kind"] == "story_patch"
    assert attempt["target_refinement_slot"] == target_slot
    assert output_artifact["artifact_kind"] == "story_patch"
    assert output_artifact["story"]["story_title"] == "Refined target story"
    assert "user_stories" not in output_artifact
    assert runtime["draft_projection"]["target_refinement_slot"] == target_slot
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_generate_story_draft_rejects_both_patch_targets() -> None:
    """Targeted generation accepts at most one target selector."""
    state: JsonDict = {
        "roadmap_releases": [{"items": ["Requirement A"]}],
        "interview_runtime": {
            "story": {
                "Requirement A": {
                    "phase": "story",
                    "subject_key": "Requirement A",
                    "attempt_history": [],
                    "draft_projection": {},
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                }
            }
        },
    }

    async def fail_if_called(*_args: object, **_kwargs: object) -> JsonDict:
        msg = "story agent should not run for invalid target selectors"
        raise AssertionError(msg)

    def noop_dict(*_args: object, **_kwargs: object) -> JsonDict:
        return {}

    def noop_list(*_args: object, **_kwargs: object) -> list[JsonDict]:
        return []

    with pytest.raises(StoryPhaseError, match="Exactly one"):
        await generate_story_draft(
            project_id=7,
            parent_requirement="Requirement A",
            user_input="Refine this story",
            target_story_id=29,
            target_refinement_slot=2,
            load_state=lambda: _async_value(state),
            save_state=lambda _updated: None,
            now_iso=lambda: "2026-04-04T12:00:00Z",
            run_story_agent_from_state=fail_if_called,
            append_feedback_entry=noop_dict,
            set_request_projection=noop_dict,
            append_attempt=noop_dict,
            promote_reusable_draft=noop_dict,
            mark_feedback_absorbed=noop_list,
            failure_meta=noop_dict,
        )


@pytest.mark.asyncio
async def test_generate_story_draft_first_generation_allows_guidance_input() -> None:
    """Weak-looking first-generation guidance still runs the Story writer."""
    parent_requirement = "Requirement A"
    artifact = _story_artifact(parent_requirement, "Initial draft")
    state: JsonDict = {"roadmap_releases": [{"items": [parent_requirement]}]}
    calls = {"agent": 0, "feedback": 0}
    captured: JsonDict = {}

    async def fake_run_story_agent_from_state(
        state_arg: JsonDict,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None,
    ) -> JsonDict:
        del state_arg
        calls["agent"] += 1
        captured["project_id"] = project_id
        captured["parent_requirement"] = parent_requirement
        captured["user_input"] = user_input
        return {
            "success": True,
            "input_context": {"requirement_context": "assembled"},
            "output_artifact": artifact,
            "classification": "reusable_content_result",
            "draft_kind": "complete_draft",
            "is_reusable": True,
            "is_complete": True,
            "request_payload": {"parent_requirement": parent_requirement},
            "error": None,
        }

    def fake_append_feedback_entry(*args: object, **kwargs: object) -> JsonDict:
        del args, kwargs
        calls["feedback"] += 1
        return {}

    payload = await generate_story_draft(
        project_id=7,
        parent_requirement=parent_requirement,
        user_input="Focus on operator workflow.",
        force_feedback=False,
        load_state=lambda: _async_value(state),
        save_state=lambda _updated: None,
        now_iso=lambda: "2026-06-09T00:00:00Z",
        run_story_agent_from_state=fake_run_story_agent_from_state,
        append_feedback_entry=fake_append_feedback_entry,
        set_request_projection=lambda runtime, **kwargs: (
            runtime.setdefault("request_projection", {}).update(kwargs)
            or runtime["request_projection"]
        ),
        append_attempt=lambda runtime, attempt: runtime.setdefault(
            "attempt_history", []
        ).append(attempt),
        promote_reusable_draft=lambda runtime, **kwargs: runtime.setdefault(
            "draft_projection", {}
        ).update(
            {
                "latest_reusable_attempt_id": kwargs["attempt_id"],
                "kind": kwargs["kind"],
                "is_complete": kwargs["is_complete"],
                "updated_at": kwargs["updated_at"],
            }
        ),
        mark_feedback_absorbed=lambda _runtime, **_kwargs: [],
        failure_meta=lambda *_args, **_kwargs: {},
    )

    assert calls == {"agent": 1, "feedback": 0}
    assert captured == {
        "project_id": 7,
        "parent_requirement": parent_requirement,
        "user_input": "Focus on operator workflow.",
    }
    assert payload["data"]["generation_ran"] is True
    assert payload["data"]["feedback_quality"] is None


@pytest.mark.asyncio
async def test_generate_story_draft_scope_extension_allows_guidance_after_legacy_attempt() -> (  # noqa: E501
    None
):
    """First extension generation is not soft-gated by stale same-name attempts."""
    parent_requirement = "Shared requirement"
    artifact = _story_artifact(parent_requirement, "Extension draft")
    state = _scope_extension_state_with_legacy_story_draft()
    runtime = state["interview_runtime"]["story"][parent_requirement]
    runtime["feedback_projection"]["items"].append(
        {
            "feedback_id": "feedback-legacy",
            "text": "Legacy scope feedback.",
            "created_at": "2026-06-09T10:46:00Z",
            "status": "unabsorbed",
            "absorbed_by_attempt_id": None,
        }
    )
    calls = {"agent": 0, "feedback": 0}
    captured: JsonDict = {}

    async def fake_run_story_agent_from_state(
        state_arg: JsonDict,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None,
    ) -> JsonDict:
        del state_arg
        calls["agent"] += 1
        captured["project_id"] = project_id
        captured["parent_requirement"] = parent_requirement
        captured["user_input"] = user_input
        return {
            "success": True,
            "input_context": {"requirement_context": "extension"},
            "output_artifact": artifact,
            "classification": "reusable_content_result",
            "draft_kind": "complete_draft",
            "is_reusable": True,
            "is_complete": True,
            "request_payload": {"parent_requirement": parent_requirement},
            "error": None,
        }

    def fake_append_feedback_entry(*args: object, **kwargs: object) -> JsonDict:
        del args, kwargs
        calls["feedback"] += 1
        return {}

    payload = await generate_story_draft(
        project_id=7,
        parent_requirement=parent_requirement,
        user_input="Make this more INVEST.",
        force_feedback=False,
        load_state=lambda: _async_value(state),
        save_state=lambda _updated: None,
        now_iso=lambda: "2026-06-22T12:00:00Z",
        run_story_agent_from_state=fake_run_story_agent_from_state,
        append_feedback_entry=fake_append_feedback_entry,
        set_request_projection=lambda runtime, **kwargs: (
            runtime.setdefault("request_projection", {}).update(kwargs)
            or runtime["request_projection"]
        ),
        append_attempt=lambda runtime, attempt: runtime.setdefault(
            "attempt_history", []
        ).append(attempt),
        promote_reusable_draft=lambda runtime, **kwargs: runtime.setdefault(
            "draft_projection", {}
        ).update(
            {
                "latest_reusable_attempt_id": kwargs["attempt_id"],
                "kind": kwargs["kind"],
                "is_complete": kwargs["is_complete"],
                "updated_at": kwargs["updated_at"],
            }
        ),
        mark_feedback_absorbed=lambda _runtime, **_kwargs: [],
        failure_meta=lambda *_args, **_kwargs: {},
    )

    assert calls == {"agent": 1, "feedback": 0}
    assert captured == {
        "project_id": 7,
        "parent_requirement": parent_requirement,
        "user_input": "Make this more INVEST.",
    }
    assert payload["fsm_state"] == "STORY_REVIEW"
    assert payload["data"]["generation_ran"] is True
    assert payload["data"]["feedback_quality"] is None
    assert runtime["attempt_history"][-1]["draft_basis_attempt_id"] is None
    assert runtime["attempt_history"][-1]["included_feedback_ids"] == []


@pytest.mark.asyncio
async def test_generate_story_draft_soft_gates_weak_feedback() -> None:
    """Weak refinement feedback returns guidance without running generation."""
    parent_requirement = "Requirement A"
    state: JsonDict = {
        "roadmap_releases": [{"items": [parent_requirement]}],
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "phase": "story",
                    "subject_key": parent_requirement,
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "classification": "quality_gate_failed",
                            "is_reusable": False,
                            "retryable": False,
                            "draft_kind": "quality_blocked_draft",
                            "output_artifact": _story_artifact(
                                parent_requirement,
                                "Broad draft",
                                is_complete=False,
                            ),
                        }
                    ],
                    "draft_projection": {},
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                }
            }
        },
    }
    calls = {"agent": 0, "feedback": 0}

    async def fake_run_story_agent_from_state(
        *args: object,
        **kwargs: object,
    ) -> JsonDict:
        del args, kwargs
        calls["agent"] += 1
        return {"success": True}

    def fake_append_feedback_entry(*args: object, **kwargs: object) -> JsonDict:
        del args, kwargs
        calls["feedback"] += 1
        return {}

    payload = await generate_story_draft(
        project_id=7,
        parent_requirement=parent_requirement,
        user_input="Make this more INVEST.",
        force_feedback=False,
        load_state=lambda: _async_value(state),
        save_state=lambda _updated: None,
        now_iso=lambda: "2026-06-09T00:00:00Z",
        run_story_agent_from_state=fake_run_story_agent_from_state,
        append_feedback_entry=fake_append_feedback_entry,
        set_request_projection=lambda runtime, **kwargs: (
            runtime.setdefault("request_projection", {}).update(kwargs)
            or runtime["request_projection"]
        ),
        append_attempt=lambda runtime, attempt: runtime.setdefault(
            "attempt_history", []
        ).append(attempt),
        promote_reusable_draft=lambda runtime, **kwargs: runtime.setdefault(
            "draft_projection", {}
        ).update(kwargs),
        mark_feedback_absorbed=lambda _runtime, **_kwargs: [],
        failure_meta=lambda *_args, **_kwargs: {},
    )

    assert calls == {"agent": 0, "feedback": 0}
    assert payload["fsm_state"] == "STORY_INTERVIEW"
    assert payload["data"]["generation_ran"] is False
    assert payload["data"]["feedback_quality"]["needs_revision"] is True
    assert "required_change" in payload["data"]["feedback_quality"]["missing_fields"]
    runtime = state["interview_runtime"]["story"][parent_requirement]
    assert len(runtime["attempt_history"]) == 1


@pytest.mark.asyncio
async def test_generate_story_draft_clarification_answer_runs_generation() -> None:
    """Answers to current clarifying questions should not hit the refinement gate."""
    parent_requirement = "First Model Baseline Evaluation and Reporting"
    artifact = _story_artifact(parent_requirement, "Validation-only draft")
    current_artifact = _story_artifact(
        parent_requirement,
        "Validate Baseline Evaluation Report Completeness",
        is_complete=False,
    )
    current_artifact["coverage_status"] = "partial_capacity_limited"
    current_artifact["clarifying_questions"] = [
        (
            "Should this requirement focus only on post-hoc validation and "
            "quality checks of the existing baseline comparison report?"
        )
    ]
    current_artifact["quality"] = {
        "coverage_status": "partial_capacity_limited",
        "blocking_findings": [
            {
                "code": "PARTIAL_CAPACITY_LIMITED",
                "severity": "blocking",
                "message": "Regenerate with a narrower slice.",
            }
        ],
        "quality_findings": [],
        "remaining_scope": [
            (
                "The scope that can be independently added is limited to quality "
                "validation."
            )
        ],
    }
    state: JsonDict = {
        "roadmap_releases": [{"items": [parent_requirement]}],
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "phase": "story",
                    "subject_key": parent_requirement,
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "classification": "quality_gate_failed",
                            "is_reusable": False,
                            "retryable": False,
                            "draft_kind": "quality_blocked_draft",
                            "output_artifact": current_artifact,
                        }
                    ],
                    "draft_projection": {},
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {
                        "payload": {
                            "parent_requirement": parent_requirement,
                            "requirement_context": "Requirement context",
                            "technical_spec": "{}",
                            "compiled_authority": "{}",
                            "global_roadmap_context": "Roadmap context",
                            "already_generated_milestone_stories": "Existing stories",
                            "artifact_registry": {},
                        }
                    },
                }
            }
        },
    }
    calls = {"agent": 0, "feedback": 0}

    async def fake_run_story_agent_from_state(
        _state: JsonDict,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None,
    ) -> JsonDict:
        assert project_id == 7  # noqa: PLR2004
        assert parent_requirement == "First Model Baseline Evaluation and Reporting"
        assert user_input is None
        calls["agent"] += 1
        return {
            "success": True,
            "input_context": {"requirement_context": "assembled"},
            "output_artifact": artifact,
            "classification": "reusable_content_result",
            "draft_kind": "complete_draft",
            "is_reusable": True,
            "is_complete": True,
            "request_payload": {"parent_requirement": parent_requirement},
            "error": None,
        }

    def fake_append_feedback_entry(
        runtime: JsonDict,
        text: str,
        created_at: str,
        **kwargs: object,
    ) -> JsonDict:
        calls["feedback"] += 1
        feedback_entry = {
            "feedback_id": "feedback-1",
            "text": text,
            "created_at": created_at,
            "status": "unabsorbed",
            **kwargs,
        }
        feedback_projection = runtime.setdefault(
            "feedback_projection",
            {"items": [], "next_feedback_sequence": 0},
        )
        cast("list[JsonDict]", feedback_projection.setdefault("items", [])).append(
            feedback_entry
        )
        return feedback_entry

    payload = await generate_story_draft(
        project_id=7,
        parent_requirement=parent_requirement,
        user_input=(
            "Answer: focus this requirement only on post-hoc validation and "
            "quality checks of the existing baseline comparison report. Do not "
            "duplicate or re-implement core baseline evaluation/report generation."
        ),
        force_feedback=False,
        load_state=lambda: _async_value(state),
        save_state=lambda _updated: None,
        now_iso=lambda: "2026-07-01T00:00:00Z",
        run_story_agent_from_state=fake_run_story_agent_from_state,
        append_feedback_entry=fake_append_feedback_entry,
        set_request_projection=lambda runtime, **kwargs: (
            runtime.setdefault("request_projection", {}).update(kwargs)
            or runtime["request_projection"]
        ),
        append_attempt=lambda runtime, attempt: runtime.setdefault(
            "attempt_history", []
        ).append(attempt),
        promote_reusable_draft=lambda runtime, **kwargs: runtime.setdefault(
            "draft_projection", {}
        ).update(
            {
                "latest_reusable_attempt_id": kwargs["attempt_id"],
                "kind": kwargs["kind"],
                "is_complete": kwargs["is_complete"],
                "updated_at": kwargs["updated_at"],
            }
        ),
        mark_feedback_absorbed=lambda _runtime, **_kwargs: [],
        failure_meta=lambda *_args, **_kwargs: {},
    )

    assert calls == {"agent": 1, "feedback": 1}
    assert payload["fsm_state"] == "STORY_REVIEW"
    assert payload["data"]["generation_ran"] is True
    assert payload["data"]["feedback_quality"]["needs_revision"] is True
    runtime = cast("JsonDict", state["interview_runtime"]["story"][parent_requirement])
    assert len(runtime["attempt_history"]) == 2  # noqa: PLR2004
    assert runtime["attempt_history"][-1]["attempt_id"] == "attempt-2"


@pytest.mark.asyncio
async def test_generate_story_draft_dependency_blocker_repair_runs_generation() -> None:
    """Dependency-preflight repair feedback should run instead of soft-gating."""
    parent_requirement = "First Model Baseline Evaluation and Reporting"
    artifact = _story_artifact(parent_requirement, "Validation-only draft")
    current_artifact = _story_artifact(
        parent_requirement,
        "Validate Baseline Evaluation Report Completeness",
        is_complete=False,
    )
    current_artifact["clarifying_questions"] = []
    current_artifact["quality"] = {
        "coverage_status": "complete",
        "blocking_findings": [
            {
                "code": "STORY_DEPENDENCY_CANDIDATE_UNRESOLVED",
                "severity": "blocking",
                "message": "Dependency candidate did not resolve to an active story.",
                "prerequisite_ref": "First Real Offline Delayed-Outcome Model Attempt",
                "confidence": "explicit",
            }
        ],
        "quality_findings": [],
        "remaining_scope": [],
    }
    state: JsonDict = {
        "roadmap_releases": [{"items": [parent_requirement]}],
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "phase": "story",
                    "subject_key": parent_requirement,
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "classification": "quality_gate_failed",
                            "is_reusable": False,
                            "retryable": False,
                            "draft_kind": "quality_blocked_draft",
                            "output_artifact": current_artifact,
                        }
                    ],
                    "draft_projection": {},
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {
                        "payload": {
                            "parent_requirement": parent_requirement,
                            "requirement_context": "Requirement context",
                            "technical_spec": "{}",
                            "compiled_authority": "{}",
                            "global_roadmap_context": "Roadmap context",
                            "already_generated_milestone_stories": "Existing stories",
                            "artifact_registry": {},
                        }
                    },
                }
            }
        },
    }
    calls = {"agent": 0, "feedback": 0}

    async def fake_run_story_agent_from_state(
        _state: JsonDict,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None,
    ) -> JsonDict:
        assert project_id == 7  # noqa: PLR2004
        assert parent_requirement == "First Model Baseline Evaluation and Reporting"
        assert user_input is None
        calls["agent"] += 1
        return {
            "success": True,
            "input_context": {"requirement_context": "assembled"},
            "output_artifact": artifact,
            "classification": "reusable_content_result",
            "draft_kind": "complete_draft",
            "is_reusable": True,
            "is_complete": True,
            "request_payload": {"parent_requirement": parent_requirement},
            "error": None,
        }

    def fake_append_feedback_entry(
        runtime: JsonDict,
        text: str,
        created_at: str,
        **kwargs: object,
    ) -> JsonDict:
        calls["feedback"] += 1
        feedback_entry = {
            "feedback_id": "feedback-1",
            "text": text,
            "created_at": created_at,
            "status": "unabsorbed",
            **kwargs,
        }
        feedback_projection = runtime.setdefault(
            "feedback_projection",
            {"items": [], "next_feedback_sequence": 0},
        )
        cast("list[JsonDict]", feedback_projection.setdefault("items", [])).append(
            feedback_entry
        )
        return feedback_entry

    payload = await generate_story_draft(
        project_id=7,
        parent_requirement=parent_requirement,
        user_input=(
            "Remove dependency_candidates and put the existing model attempt report "
            "as an acceptance-criteria precondition instead."
        ),
        force_feedback=False,
        load_state=lambda: _async_value(state),
        save_state=lambda _updated: None,
        now_iso=lambda: "2026-07-01T00:00:00Z",
        run_story_agent_from_state=fake_run_story_agent_from_state,
        append_feedback_entry=fake_append_feedback_entry,
        set_request_projection=lambda runtime, **kwargs: (
            runtime.setdefault("request_projection", {}).update(kwargs)
            or runtime["request_projection"]
        ),
        append_attempt=lambda runtime, attempt: runtime.setdefault(
            "attempt_history", []
        ).append(attempt),
        promote_reusable_draft=lambda runtime, **kwargs: runtime.setdefault(
            "draft_projection", {}
        ).update(
            {
                "latest_reusable_attempt_id": kwargs["attempt_id"],
                "kind": kwargs["kind"],
                "is_complete": kwargs["is_complete"],
                "updated_at": kwargs["updated_at"],
            }
        ),
        mark_feedback_absorbed=lambda _runtime, **_kwargs: [],
        failure_meta=lambda *_args, **_kwargs: {},
    )

    assert calls == {"agent": 1, "feedback": 1}
    assert payload["fsm_state"] == "STORY_REVIEW"
    assert payload["data"]["generation_ran"] is True
    assert payload["data"]["feedback_quality"]["needs_revision"] is True
    runtime = cast("JsonDict", state["interview_runtime"]["story"][parent_requirement])
    assert len(runtime["attempt_history"]) == 2  # noqa: PLR2004
    assert runtime["attempt_history"][-1]["attempt_id"] == "attempt-2"


@pytest.mark.asyncio
async def test_generate_story_draft_weak_feedback_after_schema_failure_runs_generation() -> None:  # noqa: E501
    """Weak feedback after a hard schema failure starts a fresh generation."""
    parent_requirement = "Requirement A"
    expected_attempt_count = 2
    artifact = _story_artifact(parent_requirement, "Recovered draft")
    state: JsonDict = {
        "roadmap_releases": [{"items": [parent_requirement]}],
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "phase": "story",
                    "subject_key": parent_requirement,
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "classification": "nonreusable_schema_failure",
                            "failure_stage": "invalid_json",
                            "failure_summary": "Story response is not valid JSON",
                            "is_reusable": False,
                            "retryable": False,
                            "draft_kind": None,
                            "output_artifact": {
                                "error": "STORY_GENERATION_FAILED",
                                "failure_stage": "invalid_json",
                                "is_complete": False,
                            },
                        }
                    ],
                    "draft_projection": {},
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {
                        "payload": {
                            "parent_requirement": parent_requirement,
                            "requirement_context": "Requirement context",
                            "technical_spec": "{}",
                            "compiled_authority": "{}",
                            "global_roadmap_context": "Roadmap context",
                            "already_generated_milestone_stories": "Existing stories",
                            "artifact_registry": {},
                        }
                    },
                }
            }
        },
    }
    calls = {"agent": 0, "feedback": 0}

    async def fake_run_story_agent_from_state(
        *args: object,
        **kwargs: object,
    ) -> JsonDict:
        del args, kwargs
        calls["agent"] += 1
        return {
            "success": True,
            "input_context": {"requirement_context": "assembled"},
            "output_artifact": artifact,
            "classification": "reusable_content_result",
            "draft_kind": "complete_draft",
            "is_reusable": True,
            "is_complete": True,
            "request_payload": {"parent_requirement": parent_requirement},
            "error": None,
        }

    def fake_append_feedback_entry(
        runtime: JsonDict,
        text: str,
        created_at: str,
        **kwargs: object,
    ) -> JsonDict:
        calls["feedback"] += 1
        feedback_entry = {
            "feedback_id": "feedback-1",
            "text": text,
            "created_at": created_at,
            "status": "unabsorbed",
            **kwargs,
        }
        feedback_projection = runtime.setdefault(
            "feedback_projection",
            {"items": [], "next_feedback_sequence": 0},
        )
        cast("list[JsonDict]", feedback_projection.setdefault("items", [])).append(
            feedback_entry
        )
        return feedback_entry

    payload = await generate_story_draft(
        project_id=7,
        parent_requirement=parent_requirement,
        user_input="Try again with this requirement only.",
        force_feedback=False,
        load_state=lambda: _async_value(state),
        save_state=lambda _updated: None,
        now_iso=lambda: "2026-06-30T00:00:00Z",
        run_story_agent_from_state=fake_run_story_agent_from_state,
        append_feedback_entry=fake_append_feedback_entry,
        set_request_projection=lambda runtime, **kwargs: (
            runtime.setdefault("request_projection", {}).update(kwargs)
            or runtime["request_projection"]
        ),
        append_attempt=lambda runtime, attempt: runtime.setdefault(
            "attempt_history", []
        ).append(attempt),
        promote_reusable_draft=lambda runtime, **kwargs: runtime.setdefault(
            "draft_projection", {}
        ).update(
            {
                "latest_reusable_attempt_id": kwargs["attempt_id"],
                "kind": kwargs["kind"],
                "is_complete": kwargs["is_complete"],
                "updated_at": kwargs["updated_at"],
            }
        ),
        mark_feedback_absorbed=lambda _runtime, **_kwargs: [],
        failure_meta=lambda *_args, **_kwargs: {},
    )

    assert calls == {"agent": 1, "feedback": 1}
    assert payload["fsm_state"] == "STORY_REVIEW"
    assert payload["data"]["generation_ran"] is True
    assert payload["data"]["feedback_quality"]["needs_revision"] is True
    runtime = cast(
        "JsonDict",
        state["interview_runtime"]["story"][parent_requirement],
    )
    assert len(runtime["attempt_history"]) == expected_attempt_count
    feedback_projection = cast("JsonDict", runtime["feedback_projection"])
    feedback_items = cast("list[JsonDict]", feedback_projection["items"])
    assert feedback_items[0]["feedback_id"] == "feedback-1"


@pytest.mark.asyncio
async def test_generate_story_draft_force_feedback_runs_generation() -> None:
    """Forced weak feedback records quality metadata and still runs generation."""
    parent_requirement = "Requirement A"
    artifact = _story_artifact(parent_requirement, "Forced draft")
    state: JsonDict = {
        "roadmap_releases": [{"items": [parent_requirement]}],
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "phase": "story",
                    "subject_key": parent_requirement,
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "classification": "quality_gate_failed",
                            "is_reusable": False,
                            "retryable": False,
                            "draft_kind": "quality_blocked_draft",
                            "output_artifact": _story_artifact(
                                parent_requirement,
                                "Broad draft",
                                is_complete=False,
                            ),
                        }
                    ],
                    "draft_projection": {},
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                }
            }
        },
    }
    captured_feedback: dict[str, Any] = {}

    async def fake_run_story_agent_from_state(
        *args: object,
        **kwargs: object,
    ) -> JsonDict:
        del args, kwargs
        return {
            "success": True,
            "input_context": {"requirement_context": "assembled"},
            "output_artifact": artifact,
            "classification": "reusable_content_result",
            "draft_kind": "complete_draft",
            "is_reusable": True,
            "is_complete": True,
            "request_payload": {"parent_requirement": parent_requirement},
            "error": None,
        }

    def fake_append_feedback_entry(
        runtime: JsonDict,
        text: str,
        created_at: str,
        **kwargs: object,
    ) -> JsonDict:
        del runtime, text, created_at
        captured_feedback.update(kwargs)
        return {"feedback_id": "feedback-1"}

    payload = await generate_story_draft(
        project_id=7,
        parent_requirement=parent_requirement,
        user_input="Try again.",
        force_feedback=True,
        load_state=lambda: _async_value(state),
        save_state=lambda _updated: None,
        now_iso=lambda: "2026-06-09T00:00:00Z",
        run_story_agent_from_state=fake_run_story_agent_from_state,
        append_feedback_entry=fake_append_feedback_entry,
        set_request_projection=lambda runtime, **kwargs: (
            runtime.setdefault("request_projection", {}).update(kwargs)
            or runtime["request_projection"]
        ),
        append_attempt=lambda runtime, attempt: runtime.setdefault(
            "attempt_history", []
        ).append(attempt),
        promote_reusable_draft=lambda runtime, **kwargs: runtime.setdefault(
            "draft_projection", {}
        ).update(
            {
                "latest_reusable_attempt_id": kwargs["attempt_id"],
                "kind": kwargs["kind"],
                "is_complete": kwargs["is_complete"],
                "updated_at": kwargs["updated_at"],
            }
        ),
        mark_feedback_absorbed=lambda _runtime, **_kwargs: [],
        failure_meta=lambda *_args, **_kwargs: {},
    )

    assert payload["data"]["generation_ran"] is True
    assert payload["data"]["feedback_quality"]["forced"] is True
    feedback_quality = cast("JsonDict", captured_feedback["feedback_quality"])
    assert feedback_quality["forced"] is True


@pytest.mark.asyncio
async def test_generate_story_patch_force_feedback_runs_targeted_generation() -> None:
    """Forced feedback should run generation for an existing targeted patch draft."""
    parent_requirement = "Requirement A"
    target_slot = 2
    state = _state_with_story_patch_draft(parent_requirement)
    captured_feedback: dict[str, Any] = {}
    captured_text: dict[str, str] = {}

    async def fake_run_story_agent_from_state(  # noqa: PLR0913
        state_arg: JsonDict,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None,
        target_story_id: int | None = None,
        target_refinement_slot: int | None = None,
    ) -> JsonDict:
        del state_arg, project_id
        assert parent_requirement == "Requirement A"
        assert user_input is None
        assert target_story_id is None
        assert target_refinement_slot == target_slot
        return {
            "success": True,
            "input_context": {"requirement_context": "assembled"},
            "output_artifact": _story_patch_artifact(
                parent_requirement,
                "Forced target refinement",
                target_refinement_slot=target_slot,
            ),
            "classification": "reusable_content_result",
            "draft_kind": "story_patch",
            "is_reusable": True,
            "is_complete": True,
            "request_payload": {
                "parent_requirement": parent_requirement,
                "target_refinement_slot": target_slot,
            },
            "error": None,
        }

    def fake_append_feedback_entry(
        runtime: JsonDict,
        text: str,
        created_at: str,
        **kwargs: object,
    ) -> JsonDict:
        del created_at
        captured_text["value"] = text
        captured_feedback.update(kwargs)
        item = {
            "feedback_id": "feedback-1",
            "text": text,
            "status": "unabsorbed",
            "extension_metadata": None,
        }
        runtime["feedback_projection"]["items"].append(item)
        return item

    payload = await generate_story_draft(
        project_id=7,
        parent_requirement=parent_requirement,
        user_input="Try again.",
        force_feedback=True,
        target_story_id=None,
        target_refinement_slot=target_slot,
        load_state=lambda: _async_value(state),
        save_state=lambda _updated: None,
        now_iso=lambda: "2026-06-09T00:00:00Z",
        run_story_agent_from_state=fake_run_story_agent_from_state,
        append_feedback_entry=fake_append_feedback_entry,
        set_request_projection=lambda runtime, **kwargs: (
            runtime.setdefault("request_projection", {}).update(kwargs)
            or runtime["request_projection"]
        ),
        append_attempt=lambda runtime, attempt: runtime.setdefault(
            "attempt_history", []
        ).append(attempt),
        promote_reusable_draft=lambda runtime, **kwargs: runtime.setdefault(
            "draft_projection",
            {},
        ).update(
            {
                "latest_reusable_attempt_id": kwargs["attempt_id"],
                "kind": kwargs["kind"],
                "is_complete": kwargs["is_complete"],
                "updated_at": kwargs["updated_at"],
            }
        ),
        mark_feedback_absorbed=lambda _runtime, **_kwargs: [],
        failure_meta=lambda *_args, **_kwargs: {},
    )

    runtime = _story_runtime_for(state, parent_requirement)
    attempt = runtime["attempt_history"][-1]
    feedback_quality = cast("JsonDict", captured_feedback["feedback_quality"])
    assert payload["data"]["generation_ran"] is True
    assert payload["data"]["feedback_quality"]["forced"] is True
    assert captured_text["value"] == "Try again."
    assert feedback_quality["forced"] is True
    assert attempt["draft_kind"] == "story_patch"
    assert attempt["target_refinement_slot"] == target_slot


@pytest.mark.asyncio
async def test_generate_story_draft_returns_attempt_guards() -> None:
    """Verify generate story draft returns attempt guards."""
    parent_requirement = "Requirement A"
    artifact = _story_artifact(parent_requirement, "Guarded story")
    state: JsonDict = {
        "roadmap_releases": [{"items": [parent_requirement]}],
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "phase": "story",
                    "subject_key": parent_requirement,
                    "attempt_history": [],
                    "draft_projection": {},
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                }
            }
        },
    }
    saved_states: list[JsonDict] = []

    async def fake_run_story_agent_from_state(
        state_arg: JsonDict,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None,
    ) -> JsonDict:
        del state_arg
        assert project_id == 7  # noqa: PLR2004
        assert parent_requirement == "Requirement A"
        assert user_input is None
        return {
            "success": True,
            "input_context": {"requirement_context": "assembled"},
            "output_artifact": artifact,
            "classification": "reusable_content_result",
            "draft_kind": "complete_draft",
            "is_reusable": True,
            "is_complete": True,
            "request_payload": {"parent_requirement": parent_requirement},
            "error": None,
        }

    expected_fingerprint = canonical_hash(
        {
            "phase": "story",
            "parent_requirement": parent_requirement,
            "output_artifact": artifact,
        }
    )
    payload = await generate_story_draft(
        project_id=7,
        parent_requirement=parent_requirement,
        user_input=None,
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-04-04T12:00:00Z",
        run_story_agent_from_state=fake_run_story_agent_from_state,
        append_feedback_entry=lambda runtime, text, created_at: runtime[
            "feedback_projection"
        ]["items"].append(
            {
                "feedback_id": f"feedback-{len(runtime['feedback_projection']['items']) + 1}",  # noqa: E501
                "text": text,
                "created_at": created_at,
                "status": "unabsorbed",
                "absorbed_by_attempt_id": None,
            }
        ),
        set_request_projection=lambda runtime, **kwargs: (
            runtime.setdefault("request_projection", {}).update(kwargs)
            or runtime["request_projection"]
        ),
        append_attempt=lambda runtime, attempt: runtime.setdefault(
            "attempt_history", []
        ).append(attempt),
        promote_reusable_draft=lambda runtime, **kwargs: runtime.setdefault(
            "draft_projection", {}
        ).update(
            {
                "latest_reusable_attempt_id": kwargs["attempt_id"],
                "kind": kwargs["kind"],
                "is_complete": kwargs["is_complete"],
                "updated_at": kwargs["updated_at"],
            }
        ),
        mark_feedback_absorbed=lambda runtime, *, feedback_ids, attempt_id: [
            item.update({"status": "absorbed", "absorbed_by_attempt_id": attempt_id})
            for item in runtime["feedback_projection"]["items"]
            if item["feedback_id"] in set(feedback_ids)
        ],
        failure_meta=lambda story_result, fallback_summary: {},  # noqa: ARG005
    )

    data = payload["data"]
    assert payload["fsm_state"] == "STORY_REVIEW"
    assert data["attempt_id"] == "attempt-1"
    assert data["artifact_fingerprint"] == expected_fingerprint
    assert data["story_count"] == 1
    assert data["invest_score_counts"] == {"High": 1, "Medium": 0, "Low": 0}
    assert data["is_reusable"] is True
    assert data["quality"]["saveable"] is True
    assert data["current_draft"]["attempt_id"] == "attempt-1"
    assert data["current_draft"]["artifact_fingerprint"] == expected_fingerprint
    assert str(data["current_draft"]["artifact_fingerprint"]).startswith("sha256:")
    assert data["save"] == {
        "available": True,
        "attempt_id": "attempt-1",
        "artifact_fingerprint": expected_fingerprint,
        "expected_state": "STORY_REVIEW",
    }
    runtime = _story_runtime_for(state, parent_requirement)
    attempt_history = cast("list[JsonDict]", runtime["attempt_history"])
    latest_attempt = attempt_history[-1]
    output_artifact = cast("JsonDict", latest_attempt["output_artifact"])
    draft_projection = cast("JsonDict", runtime["draft_projection"])
    assert latest_attempt["artifact_fingerprint"] == expected_fingerprint
    assert output_artifact["artifact_fingerprint"] == expected_fingerprint
    assert draft_projection["artifact_fingerprint"] == expected_fingerprint
    assert state["fsm_state"] == "STORY_REVIEW"
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_generate_story_draft_sets_interview_state_when_incomplete() -> None:
    """Verify generate story draft sets interview state when incomplete."""
    parent_requirement = "Requirement A"
    artifact = _story_artifact(
        parent_requirement, "Incomplete story", is_complete=False
    )
    state: JsonDict = {
        "roadmap_releases": [{"items": [parent_requirement]}],
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "phase": "story",
                    "subject_key": parent_requirement,
                    "attempt_history": [],
                    "draft_projection": {},
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                }
            }
        },
    }
    saved_states: list[JsonDict] = []

    async def fake_run_story_agent_from_state(
        state_arg: JsonDict,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None,
    ) -> JsonDict:
        del state_arg
        assert project_id == 7  # noqa: PLR2004
        assert parent_requirement == "Requirement A"
        assert user_input is None
        return {
            "success": True,
            "input_context": {"requirement_context": "assembled"},
            "output_artifact": artifact,
            "classification": "reusable_content_result",
            "draft_kind": "incomplete_draft",
            "is_reusable": True,
            "is_complete": False,
            "request_payload": {"parent_requirement": parent_requirement},
            "error": None,
        }

    payload = await generate_story_draft(
        project_id=7,
        parent_requirement=parent_requirement,
        user_input=None,
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-04-04T12:00:00Z",
        run_story_agent_from_state=fake_run_story_agent_from_state,
        append_feedback_entry=lambda runtime, text, created_at: runtime[
            "feedback_projection"
        ]["items"].append(
            {
                "feedback_id": f"feedback-{len(runtime['feedback_projection']['items']) + 1}",  # noqa: E501
                "text": text,
                "created_at": created_at,
                "status": "unabsorbed",
                "absorbed_by_attempt_id": None,
            }
        ),
        set_request_projection=lambda runtime, **kwargs: (
            runtime.setdefault("request_projection", {}).update(kwargs)
            or runtime["request_projection"]
        ),
        append_attempt=lambda runtime, attempt: runtime.setdefault(
            "attempt_history", []
        ).append(attempt),
        promote_reusable_draft=lambda runtime, **kwargs: runtime.setdefault(
            "draft_projection", {}
        ).update(
            {
                "latest_reusable_attempt_id": kwargs["attempt_id"],
                "kind": kwargs["kind"],
                "is_complete": kwargs["is_complete"],
                "updated_at": kwargs["updated_at"],
            }
        ),
        mark_feedback_absorbed=lambda runtime, *, feedback_ids, attempt_id: [
            item.update({"status": "absorbed", "absorbed_by_attempt_id": attempt_id})
            for item in runtime["feedback_projection"]["items"]
            if item["feedback_id"] in set(feedback_ids)
        ],
        failure_meta=lambda story_result, fallback_summary: {},  # noqa: ARG005
    )

    data = payload["data"]
    assert payload["fsm_state"] == "STORY_INTERVIEW"
    assert data["current_draft"]["attempt_id"] == "attempt-1"
    assert data["current_draft"]["artifact_fingerprint"].startswith("sha256:")
    assert data["save"]["available"] is False
    assert "attempt_id" not in data["save"]
    assert state["fsm_state"] == "STORY_INTERVIEW"
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_generate_story_draft_keeps_quality_blocked_in_interview() -> None:
    """Quality-blocked complete drafts are visible but not reusable/saveable."""
    parent_requirement = "Requirement A"
    artifact = _story_artifact(parent_requirement, "Too broad research draft")
    artifact["user_stories"][0]["invest_score"] = "Low"
    artifact["user_stories"][0]["decomposition_warning"] = (
        "Story is too broad to satisfy INVEST decomposition."
    )
    artifact["is_complete"] = False
    artifact["quality"] = {
        "schema_version": "agileforge.story_quality.v1",
        "coverage_status": "complete",
        "story_count": 1,
        "invest_score_counts": {"High": 0, "Medium": 0, "Low": 1},
        "requested_story_count": None,
        "quality_findings": [
            {
                "code": "ALL_STORIES_LOW_INVEST",
                "severity": "blocking",
                "message": "Every generated story has invest_score Low.",
                "affected_story_indexes": [1],
                "affected_story_titles": ["Too broad research draft"],
            }
        ],
        "blocking_findings": [
            {
                "code": "ALL_STORIES_LOW_INVEST",
                "severity": "blocking",
                "message": "Every generated story has invest_score Low.",
                "affected_story_indexes": [1],
                "affected_story_titles": ["Too broad research draft"],
            }
        ],
        "saveable": False,
    }
    state: JsonDict = {
        "roadmap_releases": [{"items": [parent_requirement]}],
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "phase": "story",
                    "subject_key": parent_requirement,
                    "attempt_history": [],
                    "draft_projection": {},
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                }
            }
        },
    }

    async def fake_run_story_agent_from_state(
        state_arg: JsonDict,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None,
    ) -> JsonDict:
        del state_arg, project_id, parent_requirement, user_input
        return {
            "success": True,
            "input_context": {"requirement_context": "assembled"},
            "output_artifact": artifact,
            "classification": "quality_gate_failed",
            "draft_kind": "quality_blocked_draft",
            "is_reusable": False,
            "is_complete": False,
            "quality": artifact["quality"],
            "request_payload": {"parent_requirement": "Requirement A"},
            "error": None,
        }

    payload = await generate_story_draft(
        project_id=7,
        parent_requirement=parent_requirement,
        user_input=None,
        load_state=lambda: _async_value(state),
        save_state=lambda _updated: None,
        now_iso=lambda: "2026-04-04T12:00:00Z",
        run_story_agent_from_state=fake_run_story_agent_from_state,
        append_feedback_entry=lambda runtime, text, created_at: runtime[
            "feedback_projection"
        ]["items"].append(
            {
                "feedback_id": f"feedback-{len(runtime['feedback_projection']['items']) + 1}",  # noqa: E501
                "text": text,
                "created_at": created_at,
                "status": "unabsorbed",
                "absorbed_by_attempt_id": None,
            }
        ),
        set_request_projection=lambda runtime, **kwargs: (
            runtime.setdefault("request_projection", {}).update(kwargs)
            or runtime["request_projection"]
        ),
        append_attempt=lambda runtime, attempt: runtime.setdefault(
            "attempt_history", []
        ).append(attempt),
        promote_reusable_draft=lambda runtime, **kwargs: runtime.setdefault(
            "draft_projection", {}
        ).update(
            {
                "latest_reusable_attempt_id": kwargs["attempt_id"],
                "kind": kwargs["kind"],
                "is_complete": kwargs["is_complete"],
                "updated_at": kwargs["updated_at"],
            }
        ),
        mark_feedback_absorbed=lambda runtime, *, feedback_ids, attempt_id: [
            item.update({"status": "absorbed", "absorbed_by_attempt_id": attempt_id})
            for item in runtime["feedback_projection"]["items"]
            if item["feedback_id"] in set(feedback_ids)
        ],
        failure_meta=lambda story_result, fallback_summary: {},  # noqa: ARG005
    )

    data = payload["data"]
    assert payload["fsm_state"] == "STORY_INTERVIEW"
    assert data["attempt_id"] == "attempt-1"
    assert str(data["artifact_fingerprint"]).startswith("sha256:")
    assert data["story_count"] == 1
    assert data["invest_score_counts"] == {"High": 0, "Medium": 0, "Low": 1}
    assert data["is_reusable"] is False
    assert data["quality"]["saveable"] is False
    assert data["quality"]["blocking_findings"][0]["code"] == (
        "ALL_STORIES_LOW_INVEST"
    )
    assert data["current_draft"] is None
    assert data["save"] == {"available": False}
    assert state["fsm_state"] == "STORY_INTERVIEW"


@pytest.mark.asyncio
async def test_generate_story_draft_blocks_dependency_preflight() -> None:
    """Dependency preflight blockers prevent saveable Story drafts."""
    parent_requirement = "Requirement A"
    artifact = _story_artifact(parent_requirement, "Needs missing prerequisite")
    artifact["user_stories"][0]["dependency_candidates"] = [
        {
            "prerequisite_ref": "Missing prerequisite",
            "reason": "The source explicitly names this prerequisite.",
            "confidence": "explicit",
        }
    ]
    artifact["quality"] = {
        "schema_version": "agileforge.story_quality.v1",
        "coverage_status": "complete",
        "story_count": 1,
        "invest_score_counts": {"High": 1, "Medium": 0, "Low": 0},
        "requested_story_count": None,
        "quality_findings": [],
        "blocking_findings": [],
        "saveable": True,
    }
    state: JsonDict = {
        "roadmap_releases": [{"items": [parent_requirement]}],
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "phase": "story",
                    "subject_key": parent_requirement,
                    "attempt_history": [],
                    "draft_projection": {},
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                }
            }
        },
    }

    async def fake_run_story_agent_from_state(
        state_arg: JsonDict,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None,
    ) -> JsonDict:
        del state_arg, project_id, parent_requirement, user_input
        return {
            "success": True,
            "input_context": {"requirement_context": "assembled"},
            "output_artifact": artifact,
            "classification": "reusable_content_result",
            "draft_kind": "complete_draft",
            "is_reusable": True,
            "is_complete": True,
            "quality": artifact["quality"],
            "request_payload": {"parent_requirement": "Requirement A"},
            "error": None,
        }

    payload = await generate_story_draft(
        project_id=7,
        parent_requirement=parent_requirement,
        user_input=None,
        load_state=lambda: _async_value(state),
        save_state=lambda _updated: None,
        now_iso=lambda: "2026-04-04T12:00:00Z",
        run_story_agent_from_state=fake_run_story_agent_from_state,
        dependency_preflight=lambda _input_data: {
            "success": True,
            "blocking_findings": [
                {
                    "code": "STORY_DEPENDENCY_CANDIDATE_UNRESOLVED",
                    "severity": "blocking",
                    "message": (
                        "Dependency candidate did not resolve to an active story."
                    ),
                    "story_id": -1,
                    "prerequisite_ref": "Missing prerequisite",
                    "confidence": "explicit",
                    "affected_story_indexes": [1],
                    "affected_story_titles": ["Needs missing prerequisite"],
                }
            ],
            "warning_findings": [],
        },
        append_feedback_entry=lambda runtime, text, created_at: runtime[
            "feedback_projection"
        ]["items"].append(
            {
                "feedback_id": f"feedback-{len(runtime['feedback_projection']['items']) + 1}",  # noqa: E501
                "text": text,
                "created_at": created_at,
                "status": "unabsorbed",
                "absorbed_by_attempt_id": None,
            }
        ),
        set_request_projection=lambda runtime, **kwargs: (
            runtime.setdefault("request_projection", {}).update(kwargs)
            or runtime["request_projection"]
        ),
        append_attempt=lambda runtime, attempt: runtime.setdefault(
            "attempt_history", []
        ).append(attempt),
        promote_reusable_draft=lambda runtime, **kwargs: runtime.setdefault(
            "draft_projection", {}
        ).update(
            {
                "latest_reusable_attempt_id": kwargs["attempt_id"],
                "kind": kwargs["kind"],
                "is_complete": kwargs["is_complete"],
                "updated_at": kwargs["updated_at"],
            }
        ),
        mark_feedback_absorbed=lambda runtime, *, feedback_ids, attempt_id: [
            item.update({"status": "absorbed", "absorbed_by_attempt_id": attempt_id})
            for item in runtime["feedback_projection"]["items"]
            if item["feedback_id"] in set(feedback_ids)
        ],
        failure_meta=lambda story_result, fallback_summary: {},  # noqa: ARG005
    )

    data = payload["data"]
    interview_runtime = cast("JsonDict", state["interview_runtime"])
    story_runtime = cast("dict[str, JsonDict]", interview_runtime["story"])
    requirement_runtime = story_runtime[parent_requirement]
    attempt_history = cast("list[JsonDict]", requirement_runtime["attempt_history"])
    attempt = attempt_history[0]
    assert payload["fsm_state"] == "STORY_INTERVIEW"
    assert data["save"] == {"available": False}
    assert data["quality"]["saveable"] is False
    assert data["quality"]["blocking_findings"][0]["code"] == (
        "STORY_DEPENDENCY_CANDIDATE_UNRESOLVED"
    )
    assert data["quality"]["blocking_findings"][0]["story_id"] == -1
    assert data["quality"]["blocking_findings"][0]["prerequisite_ref"] == (
        "Missing prerequisite"
    )
    assert data["quality"]["blocking_findings"][0]["confidence"] == "explicit"
    assert data["current_draft"] is None
    assert attempt["classification"] == "quality_gate_failed"
    assert attempt["is_reusable"] is False
    assert attempt["output_artifact"]["quality"]["blocking_findings"][0][
        "prerequisite_ref"
    ] == "Missing prerequisite"
    assert attempt["output_artifact"]["is_complete"] is False


@pytest.mark.asyncio
async def test_retry_story_draft_replays_request_projection_and_promotes_reusable_output() -> (  # noqa: E501
    None
):
    """Verify retry story draft replays request projection and promotes reusable output."""  # noqa: E501
    state: JsonDict = {
        "interview_runtime": {
            "story": {
                "Requirement A": {
                    "phase": "story",
                    "subject_key": "Requirement A",
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "trigger": "manual_refine",
                            "input_context": {},
                            "output_artifact": _story_artifact(
                                "Requirement A", "Saved draft"
                            ),
                            "classification": "reusable_content_result",
                            "is_reusable": True,
                            "retryable": False,
                            "draft_kind": "complete_draft",
                        },
                        {
                            "attempt_id": "attempt-2",
                            "trigger": "manual_refine",
                            "input_context": {},
                            "output_artifact": {
                                "error": "STORY_GENERATION_FAILED",
                                "message": "provider timeout",
                            },
                            "classification": "nonreusable_provider_failure",
                            "is_reusable": False,
                            "retryable": True,
                            "draft_kind": None,
                        },
                    ],
                    "draft_projection": {
                        "latest_reusable_attempt_id": "attempt-1",
                        "kind": "complete_draft",
                        "is_complete": True,
                    },
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {
                        "request_snapshot_id": "request-1",
                        "payload": {"parent_requirement": "Requirement A"},
                        "included_feedback_ids": ["feedback-1"],
                        "draft_basis_attempt_id": "attempt-1",
                    },
                }
            }
        }
    }
    saved_states: list[JsonDict] = []

    async def fake_run_story_agent_request(
        request_payload: JsonDict, *, project_id: int, parent_requirement: str
    ) -> JsonDict:
        assert project_id == 7  # noqa: PLR2004
        assert parent_requirement == "Requirement A"
        assert request_payload == {"parent_requirement": "Requirement A"}
        return {
            "success": True,
            "input_context": {"request": "replayed"},
            "output_artifact": _story_artifact("Requirement A", "Retried story"),
            "classification": "reusable_content_result",
            "draft_kind": "complete_draft",
            "is_reusable": True,
            "is_complete": True,
            "request_payload": request_payload,
            "error": None,
            "failure_artifact_id": None,
            "failure_stage": None,
            "failure_summary": None,
            "raw_output_preview": None,
            "has_full_artifact": False,
        }

    def mark_feedback_absorbed(
        runtime: JsonDict,
        *,
        feedback_ids: list[str],
        attempt_id: str,
    ) -> list[dict[str, Any]]:
        del runtime, feedback_ids, attempt_id
        return []

    payload = await retry_story_draft(
        project_id=7,
        parent_requirement="  Requirement A  ",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-04-04T12:00:00Z",
        run_story_agent_request=fake_run_story_agent_request,
        append_attempt=lambda runtime, attempt: runtime.setdefault(
            "attempt_history", []
        ).append(attempt),
        promote_reusable_draft=lambda runtime, **kwargs: runtime.setdefault(
            "draft_projection", {}
        ).update(
            {
                "latest_reusable_attempt_id": kwargs["attempt_id"],
                "kind": kwargs["kind"],
                "is_complete": kwargs["is_complete"],
                "updated_at": kwargs["updated_at"],
            }
        ),
        mark_feedback_absorbed=mark_feedback_absorbed,
        failure_meta=lambda story_result, fallback_summary: {},  # noqa: ARG005
    )

    assert payload["parent_requirement"] == "Requirement A"
    assert (
        payload["data"]["output_artifact"]["user_stories"][0]["story_title"]
        == "Retried story"
    )
    assert payload["data"]["retry"] == {
        "available": False,
        "target_attempt_id": None,
    }
    assert (
        state["story_outputs"]["Requirement A"]["user_stories"][0]["story_title"]
        == "Retried story"
    )
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_retry_story_draft_returns_attempt_guards() -> None:
    """Verify retry story draft returns attempt guards."""
    parent_requirement = "Requirement A"
    artifact = _story_artifact(parent_requirement, "Retried guarded story")
    state: JsonDict = {
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "phase": "story",
                    "subject_key": parent_requirement,
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "trigger": "manual_refine",
                            "input_context": {},
                            "output_artifact": _story_artifact(
                                parent_requirement, "Original draft"
                            ),
                            "classification": "reusable_content_result",
                            "is_reusable": True,
                            "retryable": False,
                            "draft_kind": "complete_draft",
                        },
                        {
                            "attempt_id": "attempt-2",
                            "trigger": "manual_refine",
                            "input_context": {},
                            "output_artifact": {
                                "error": "STORY_GENERATION_FAILED",
                                "message": "provider timeout",
                            },
                            "classification": "nonreusable_provider_failure",
                            "is_reusable": False,
                            "retryable": True,
                            "draft_kind": None,
                        },
                    ],
                    "draft_projection": {
                        "latest_reusable_attempt_id": "attempt-1",
                        "kind": "complete_draft",
                        "is_complete": True,
                    },
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {
                        "request_snapshot_id": "request-1",
                        "payload": {"parent_requirement": parent_requirement},
                        "included_feedback_ids": [],
                        "draft_basis_attempt_id": "attempt-1",
                    },
                }
            }
        }
    }
    saved_states: list[JsonDict] = []

    async def fake_run_story_agent_request(
        request_payload: JsonDict, *, project_id: int, parent_requirement: str
    ) -> JsonDict:
        assert project_id == 7  # noqa: PLR2004
        assert parent_requirement == "Requirement A"
        assert request_payload == {"parent_requirement": "Requirement A"}
        return {
            "success": True,
            "input_context": {"request": "replayed"},
            "output_artifact": artifact,
            "classification": "reusable_content_result",
            "draft_kind": "complete_draft",
            "is_reusable": True,
            "is_complete": True,
            "request_payload": request_payload,
            "error": None,
        }

    expected_fingerprint = canonical_hash(
        {
            "phase": "story",
            "parent_requirement": parent_requirement,
            "output_artifact": artifact,
        }
    )

    def mark_feedback_absorbed(
        runtime: JsonDict,
        *,
        feedback_ids: list[str],
        attempt_id: str,
    ) -> list[dict[str, Any]]:
        del runtime, feedback_ids, attempt_id
        return []

    payload = await retry_story_draft(
        project_id=7,
        parent_requirement=parent_requirement,
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-04-04T12:00:00Z",
        run_story_agent_request=fake_run_story_agent_request,
        append_attempt=lambda runtime, attempt: runtime.setdefault(
            "attempt_history", []
        ).append(attempt),
        promote_reusable_draft=lambda runtime, **kwargs: runtime.setdefault(
            "draft_projection", {}
        ).update(
            {
                "latest_reusable_attempt_id": kwargs["attempt_id"],
                "kind": kwargs["kind"],
                "is_complete": kwargs["is_complete"],
                "updated_at": kwargs["updated_at"],
            }
        ),
        mark_feedback_absorbed=mark_feedback_absorbed,
        failure_meta=lambda story_result, fallback_summary: {},  # noqa: ARG005
    )

    data = payload["data"]
    assert data["current_draft"]["attempt_id"] == "attempt-3"
    assert data["current_draft"]["artifact_fingerprint"] == expected_fingerprint
    assert data["save"] == {
        "available": True,
        "attempt_id": "attempt-3",
        "artifact_fingerprint": expected_fingerprint,
        "expected_state": "STORY_REVIEW",
    }
    runtime = _story_runtime_for(state, parent_requirement)
    attempt_history = cast("list[JsonDict]", runtime["attempt_history"])
    latest_attempt = attempt_history[-1]
    output_artifact = cast("JsonDict", latest_attempt["output_artifact"])
    draft_projection = cast("JsonDict", runtime["draft_projection"])
    assert latest_attempt["artifact_fingerprint"] == expected_fingerprint
    assert output_artifact["artifact_fingerprint"] == expected_fingerprint
    assert draft_projection["artifact_fingerprint"] == expected_fingerprint
    assert payload["fsm_state"] == "STORY_REVIEW"
    assert state["fsm_state"] == "STORY_REVIEW"
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_save_story_draft_marks_requirement_saved_and_persists_state() -> None:
    """Verify save story draft marks requirement saved and persists state."""
    state = _state_with_complete_story_draft()
    artifact = state["interview_runtime"]["story"]["Requirement A"]["attempt_history"][
        0
    ]["output_artifact"]
    artifact_fingerprint = state["interview_runtime"]["story"]["Requirement A"][
        "draft_projection"
    ]["artifact_fingerprint"]
    hydrated = SimpleNamespace(state=state, session_id="7")
    saved_states: list[JsonDict] = []
    captured: JsonDict = {}

    def save_state(updated: JsonDict) -> None:
        saved_states.append(dict(updated))

    async def hydrate_context(session_id: str, project_id: int) -> SimpleNamespace:
        assert session_id == "7"
        assert project_id == 7  # noqa: PLR2004
        return hydrated

    def fake_save_stories_tool(save_input: object, _context: object) -> JsonDict:
        assert hasattr(save_input, "stories")
        save_payload = cast("Any", save_input)
        captured["save_input"] = save_input
        captured["stories"] = save_payload.stories
        captured["idempotency_key"] = save_payload.idempotency_key
        return {"success": True, "saved_count": 1}

    payload = await save_story_draft(
        project_id=7,
        parent_requirement="  Requirement A  ",
        load_state=lambda: _async_value(state),
        save_state=save_state,
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_stories_tool=fake_save_stories_tool,
        attempt_id="attempt-1",
        expected_artifact_fingerprint=artifact_fingerprint,
        expected_state="STORY_REVIEW",
        idempotency_key="story-save-7-requirement-a",
    )

    assert payload["parent_requirement"] == "Requirement A"
    assert payload["attempt_id"] == "attempt-1"
    assert payload["artifact_fingerprint"] == artifact_fingerprint
    assert payload["fsm_state"] == "STORY_PERSISTENCE"
    assert payload["data"]["save_result"]["saved_count"] == 1
    assert state["story_saved"]["Requirement A"] is True
    assert state["fsm_state"] == "STORY_PERSISTENCE"
    assert (
        state["story_outputs"]["Requirement A"]["user_stories"][0]["story_title"]
        == "Saved draft"
    )
    assert captured["stories"] == artifact["user_stories"]
    assert captured["idempotency_key"] == "story-save-7-requirement-a"
    assert captured["save_input"].parent_rank == 1
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_save_story_patch_sends_single_target_story_from_review_draft() -> None:
    """Story patch save sends the reviewed patch story to the persistence tool."""
    state = _state_with_story_patch_draft()
    runtime = state["interview_runtime"]["story"]["Requirement A"]
    artifact = runtime["attempt_history"][0]["output_artifact"]
    artifact_fingerprint = runtime["draft_projection"]["artifact_fingerprint"]
    hydrated = SimpleNamespace(state=state, session_id="7")
    saved_states: list[JsonDict] = []
    captured: JsonDict = {}

    def save_state(updated: JsonDict) -> None:
        saved_states.append(dict(updated))

    async def hydrate_context(session_id: str, project_id: int) -> SimpleNamespace:
        assert session_id == "7"
        assert project_id == 7  # noqa: PLR2004
        return hydrated

    def fake_save_story_patch_tool(save_input: object, _context: object) -> JsonDict:
        assert hasattr(save_input, "story")
        save_payload = cast("Any", save_input)
        captured["save_input"] = save_input
        captured["story"] = save_payload.story
        captured["target_refinement_slot"] = save_payload.target_refinement_slot
        captured["target_story_id"] = save_payload.target_story_id
        return {"success": True, "saved_count": 1, "updated_story_ids": [29]}

    payload = await save_story_patch(
        project_id=7,
        parent_requirement="Requirement A",
        load_state=lambda: _async_value(state),
        save_state=save_state,
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_story_patch_tool=fake_save_story_patch_tool,
        attempt_id="attempt-1",
        expected_artifact_fingerprint=artifact_fingerprint,
        expected_state="STORY_REVIEW",
        idempotency_key="story-patch-7-requirement-a",
        target_story_id=None,
        target_refinement_slot=2,
        resolve_target_refinement_slot=None,
    )

    assert payload["parent_requirement"] == "Requirement A"
    assert payload["fsm_state"] == "STORY_PERSISTENCE"
    assert payload["data"]["save_result"]["updated_story_ids"] == [29]
    assert captured["story"] == artifact["story"]
    assert captured["target_refinement_slot"] == 2  # noqa: PLR2004
    assert captured["target_story_id"] is None
    assert state["story_saved"]["Requirement A"] is True
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_save_story_patch_reads_story_patch_artifact_story() -> None:
    """Story patch save reads output_artifact.story without requiring user_stories."""
    state = _state_with_story_patch_draft()
    runtime = state["interview_runtime"]["story"]["Requirement A"]
    artifact = runtime["attempt_history"][0]["output_artifact"]
    artifact_fingerprint = runtime["draft_projection"]["artifact_fingerprint"]
    hydrated = SimpleNamespace(state=state, session_id="7")
    saved_states: list[JsonDict] = []
    captured: JsonDict = {}

    async def hydrate_context(session_id: str, project_id: int) -> SimpleNamespace:
        assert session_id == "7"
        assert project_id == 7  # noqa: PLR2004
        return hydrated

    def fake_save_story_patch_tool(save_input: object, _context: object) -> JsonDict:
        save_payload = cast("Any", save_input)
        captured["story"] = save_payload.story
        captured["target_refinement_slot"] = save_payload.target_refinement_slot
        return {"success": True, "saved_count": 1, "updated_story_ids": [29]}

    payload = await save_story_patch(
        project_id=7,
        parent_requirement="Requirement A",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_story_patch_tool=fake_save_story_patch_tool,
        attempt_id="attempt-1",
        expected_artifact_fingerprint=artifact_fingerprint,
        expected_state="STORY_REVIEW",
        idempotency_key="story-patch-7-requirement-a",
        target_story_id=None,
        target_refinement_slot=2,
        resolve_target_refinement_slot=None,
    )

    assert payload["operation"] == "story_patch"
    assert captured["story"] == artifact["story"]
    assert captured["target_refinement_slot"] == 2  # noqa: PLR2004
    stories = state["story_outputs"]["Requirement A"]["user_stories"]
    assert stories[0]["story_title"] == "Protected sibling"
    assert stories[1]["story_title"] == "Refined target story"
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_save_story_patch_rejects_complete_draft_attempt() -> None:
    """Story save-patch only accepts story_patch drafts."""
    state = _state_with_complete_story_draft()
    runtime = state["interview_runtime"]["story"]["Requirement A"]
    artifact_fingerprint = runtime["draft_projection"]["artifact_fingerprint"]

    async def hydrate_context(_session_id: str, _project_id: int) -> SimpleNamespace:
        msg = "save-patch should reject before hydrating context"
        raise AssertionError(msg)

    with pytest.raises(StoryPhaseError, match="story_patch"):
        await save_story_patch(
            project_id=7,
            parent_requirement="Requirement A",
            load_state=lambda: _async_value(state),
            save_state=lambda _updated: None,
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_story_patch_tool=lambda _save_input, _context: {},
            attempt_id="attempt-1",
            expected_artifact_fingerprint=artifact_fingerprint,
            expected_state="STORY_REVIEW",
            idempotency_key="story-patch-7-requirement-a",
            target_story_id=None,
            target_refinement_slot=1,
            resolve_target_refinement_slot=None,
        )


@pytest.mark.asyncio
async def test_save_story_draft_rejects_story_patch_current_draft() -> None:
    """Full-list Story save must not save a targeted patch draft."""
    state = _state_with_story_patch_draft()
    runtime = state["interview_runtime"]["story"]["Requirement A"]
    artifact_fingerprint = runtime["draft_projection"]["artifact_fingerprint"]

    async def hydrate_context(_session_id: str, _project_id: int) -> SimpleNamespace:
        msg = "story save should reject patch drafts before hydration"
        raise AssertionError(msg)

    with pytest.raises(StoryPhaseError, match="complete draft"):
        await save_story_draft(
            project_id=7,
            parent_requirement="Requirement A",
            load_state=lambda: _async_value(state),
            save_state=lambda _updated: None,
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_stories_tool=lambda _save_input, _context: {},
            attempt_id="attempt-1",
            expected_artifact_fingerprint=artifact_fingerprint,
            expected_state="STORY_REVIEW",
            idempotency_key="story-save-7-requirement-a",
        )


@pytest.mark.asyncio
async def test_save_story_patch_merges_target_into_existing_story_output() -> None:
    """Story patch save preserves sibling story output content."""
    state = _state_with_story_patch_draft()
    runtime = state["interview_runtime"]["story"]["Requirement A"]
    artifact_fingerprint = runtime["draft_projection"]["artifact_fingerprint"]
    state["story_outputs"] = {
        "Requirement A": {
            "parent_requirement": "Requirement A",
            "user_stories": [
                {
                    "story_title": "Protected sibling",
                    "statement": "As a user, I want old sibling content preserved.",
                    "acceptance_criteria": ["Keep the sibling untouched."],
                    "invest_score": "High",
                    "estimated_effort": "S",
                    "produced_artifacts": [],
                },
                {
                    "story_title": "Old target story",
                    "statement": "As a user, I want old target content replaced.",
                    "acceptance_criteria": ["Replace this target story."],
                    "invest_score": "High",
                    "estimated_effort": "S",
                    "produced_artifacts": [],
                },
            ],
            "is_complete": True,
            "clarifying_questions": [],
        }
    }
    hydrated = SimpleNamespace(state=state, session_id="7")

    async def hydrate_context(session_id: str, project_id: int) -> SimpleNamespace:
        assert session_id == "7"
        assert project_id == 7  # noqa: PLR2004
        return hydrated

    def fake_save_story_patch_tool(_save_input: object, _context: object) -> JsonDict:
        return {"success": True, "saved_count": 1, "updated_story_ids": [29]}

    await save_story_patch(
        project_id=7,
        parent_requirement="Requirement A",
        load_state=lambda: _async_value(state),
        save_state=lambda _updated: None,
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_story_patch_tool=fake_save_story_patch_tool,
        attempt_id="attempt-1",
        expected_artifact_fingerprint=artifact_fingerprint,
        expected_state="STORY_REVIEW",
        idempotency_key="story-patch-7-requirement-a",
        target_story_id=None,
        target_refinement_slot=2,
        resolve_target_refinement_slot=None,
    )

    stories = state["story_outputs"]["Requirement A"]["user_stories"]
    assert stories[0]["story_title"] == "Protected sibling"
    assert stories[1]["story_title"] == "Refined target story"


@pytest.mark.asyncio
async def test_save_story_draft_scope_extension_uses_amended_spec_metadata() -> None:
    """Extension story save preserves amended spec provenance on persistence input."""
    state = _state_with_complete_story_draft()
    state["scope_extension_context"] = {
        "schema": "agileforge.scope_extension.v1",
        "base_spec_version_id": 7,
        "amended_spec_version_id": 12,
        "added_source_item_ids": ["SRC-NEW"],
    }
    state["roadmap_releases"] = [
        {"items": ["Original requirement"]},
        {
            "items": ["Requirement A"],
            "extension_of_spec_version_id": 7,
            "accepted_spec_version_id": 12,
            "source_item_ids": ["SRC-NEW"],
        },
    ]
    _mark_current_story_draft_scope_extension(state, "Requirement A")
    artifact_fingerprint = state["interview_runtime"]["story"]["Requirement A"][
        "draft_projection"
    ]["artifact_fingerprint"]
    hydrated = SimpleNamespace(state=state, session_id="7")
    captured: JsonDict = {}

    def fake_save_stories_tool(save_input: object, _context: object) -> JsonDict:
        save_payload = cast("Any", save_input)
        captured["story_origin"] = save_payload.story_origin
        captured["accepted_spec_version_id"] = save_payload.accepted_spec_version_id
        return {"success": True, "saved_count": 1}

    await save_story_draft(
        project_id=7,
        parent_requirement="Requirement A",
        load_state=lambda: _async_value(state),
        save_state=lambda _updated: None,
        hydrate_context=lambda _session_id, _project_id: _async_value(hydrated),
        build_tool_context=lambda context: context,
        save_stories_tool=fake_save_stories_tool,
        attempt_id="attempt-1",
        expected_artifact_fingerprint=artifact_fingerprint,
        expected_state="STORY_REVIEW",
        idempotency_key="story-save-7-extension",
    )

    assert captured == {
        "story_origin": "scope_extension",
        "accepted_spec_version_id": 12,
    }


@pytest.mark.asyncio
async def test_save_story_draft_requires_attempt_guards() -> None:
    """Verify save story draft requires all attempt guard fields."""
    state = _state_with_complete_story_draft()

    with pytest.raises(StoryPhaseError, match="attempt-id"):
        await save_story_draft(
            project_id=7,
            parent_requirement="Requirement A",
            load_state=lambda: _async_value(state),
            save_state=lambda _updated: None,
            hydrate_context=lambda _session_id, _project_id: _async_value(
                SimpleNamespace(state=state, session_id="7")
            ),
            build_tool_context=lambda context: context,
            save_stories_tool=lambda _input_data, _tool_context: {"success": True},
            attempt_id=None,
            expected_artifact_fingerprint=None,
            expected_state=None,
            idempotency_key=None,
        )


@pytest.mark.asyncio
async def test_save_story_draft_rejects_stale_fingerprint() -> None:
    """Verify save story draft rejects a stale artifact fingerprint."""
    state = _state_with_complete_story_draft()

    with pytest.raises(StoryPhaseError, match="artifact fingerprint"):
        await save_story_draft(
            project_id=7,
            parent_requirement="Requirement A",
            load_state=lambda: _async_value(state),
            save_state=lambda _updated: None,
            hydrate_context=lambda _session_id, _project_id: _async_value(
                SimpleNamespace(state=state, session_id="7")
            ),
            build_tool_context=lambda context: context,
            save_stories_tool=lambda _input_data, _tool_context: {"success": True},
            attempt_id="attempt-1",
            expected_artifact_fingerprint="sha256:stale",
            expected_state="STORY_REVIEW",
            idempotency_key="story-save-7-requirement-a",
        )


@pytest.mark.asyncio
async def test_save_story_draft_rejects_mutated_attempt_artifact() -> None:
    """Verify save story draft rejects artifacts changed after review."""
    state = _state_with_complete_story_draft()
    runtime = state["interview_runtime"]["story"]["Requirement A"]
    artifact_fingerprint = runtime["draft_projection"]["artifact_fingerprint"]
    runtime["attempt_history"][0]["output_artifact"]["user_stories"][0][
        "story_title"
    ] = "Mutated after review"
    save_calls: list[object] = []

    with pytest.raises(StoryPhaseError, match="artifact fingerprint"):
        await save_story_draft(
            project_id=7,
            parent_requirement="Requirement A",
            load_state=lambda: _async_value(state),
            save_state=lambda _updated: None,
            hydrate_context=lambda _session_id, _project_id: _async_value(
                SimpleNamespace(state=state, session_id="7")
            ),
            build_tool_context=lambda context: context,
            save_stories_tool=lambda input_data, _tool_context: (
                save_calls.append(input_data) or {"success": True}
            ),
            attempt_id="attempt-1",
            expected_artifact_fingerprint=artifact_fingerprint,
            expected_state="STORY_REVIEW",
            idempotency_key="story-save-7-requirement-a",
        )

    assert len(save_calls) == 0


@pytest.mark.asyncio
async def test_save_story_draft_maps_unsafe_replacement_to_conflict() -> None:
    """Verify unsafe replacement from persistence tool is a 409 domain conflict."""
    state = _state_with_complete_story_draft()
    artifact_fingerprint = state["interview_runtime"]["story"]["Requirement A"][
        "draft_projection"
    ]["artifact_fingerprint"]

    with pytest.raises(StoryPhaseError) as exc_info:
        await save_story_draft(
            project_id=7,
            parent_requirement="Requirement A",
            load_state=lambda: _async_value(state),
            save_state=lambda _updated: None,
            hydrate_context=lambda _session_id, _project_id: _async_value(
                SimpleNamespace(state=state, session_id="7")
            ),
            build_tool_context=lambda context: context,
            save_stories_tool=lambda _input_data, _tool_context: {
                "success": False,
                "error_code": "STORY_REPLACEMENT_UNSAFE",
                "error": "Existing active stories have progressed downstream.",
                "blockers": [{"story_id": 12, "reasons": ["linked_sprint"]}],
            },
            attempt_id="attempt-1",
            expected_artifact_fingerprint=artifact_fingerprint,
            expected_state="STORY_REVIEW",
            idempotency_key="story-save-7-requirement-a",
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert "STORY_REPLACEMENT_UNSAFE" in exc_info.value.detail
    assert "Existing active stories" in exc_info.value.detail


@pytest.mark.asyncio
async def test_save_story_draft_replays_same_idempotency_key() -> None:
    """Verify save story draft replays matching idempotency keys."""
    state = _state_with_complete_story_draft()
    artifact_fingerprint = state["interview_runtime"]["story"]["Requirement A"][
        "draft_projection"
    ]["artifact_fingerprint"]
    save_calls: list[object] = []

    def save_state(updated: JsonDict) -> None:
        state.update(updated)

    async def hydrate_context(_session_id: str, _project_id: int) -> SimpleNamespace:
        return SimpleNamespace(state=state, session_id="7")

    first = await save_story_draft(
        project_id=7,
        parent_requirement="Requirement A",
        load_state=lambda: _async_value(state),
        save_state=save_state,
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_stories_tool=lambda input_data, _tool_context: (
            save_calls.append(input_data)
            or {"success": True, "saved_count": 1, "story_ids": [7]}
        ),
        attempt_id="attempt-1",
        expected_artifact_fingerprint=artifact_fingerprint,
        expected_state="STORY_REVIEW",
        idempotency_key="story-save-7-requirement-a",
    )
    second = await save_story_draft(
        project_id=7,
        parent_requirement="Requirement A",
        load_state=lambda: _async_value(state),
        save_state=save_state,
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_stories_tool=lambda input_data, _tool_context: (
            save_calls.append(input_data)
            or {"success": True, "saved_count": 1, "story_ids": [8]}
        ),
        attempt_id="attempt-1",
        expected_artifact_fingerprint=artifact_fingerprint,
        expected_state="STORY_REVIEW",
        idempotency_key="story-save-7-requirement-a",
    )

    assert second == first
    assert len(save_calls) == 1


@pytest.mark.asyncio
async def test_save_story_draft_rejects_patch_idempotency_payload() -> None:
    """Do not replay a targeted patch payload as a full-list save."""
    state = _state_with_complete_story_draft()
    artifact_fingerprint = state["interview_runtime"]["story"]["Requirement A"][
        "draft_projection"
    ]["artifact_fingerprint"]
    state["story_save_idempotency"] = {
        "story-save-cross-operation": {
            "operation": "story_patch",
            "parent_requirement": "Requirement A",
            "attempt_id": "attempt-1",
            "artifact_fingerprint": artifact_fingerprint,
            "target_story_id": None,
            "target_refinement_slot": 1,
            "fsm_state": "STORY_PERSISTENCE",
            "data": {"save_result": {"saved_count": 1}},
        }
    }

    async def fail_hydrate_context(
        _session_id: str, _project_id: int
    ) -> SimpleNamespace:
        pytest.fail("hydrate should not be called")

    with pytest.raises(StoryPhaseError, match="operation"):
        await save_story_draft(
            project_id=7,
            parent_requirement="Requirement A",
            load_state=lambda: _async_value(state),
            save_state=lambda _updated: None,
            hydrate_context=fail_hydrate_context,
            build_tool_context=lambda context: context,
            save_stories_tool=lambda _input_data, _tool_context: pytest.fail(
                "save should not be called"
            ),
            attempt_id="attempt-1",
            expected_artifact_fingerprint=artifact_fingerprint,
            expected_state="STORY_REVIEW",
            idempotency_key="story-save-cross-operation",
        )


@pytest.mark.asyncio
async def test_save_story_draft_rejects_idempotency_replay_guard_mismatch() -> None:
    """Verify save story draft rejects replay with stale guard identity."""
    state = _state_with_complete_story_draft()
    artifact_fingerprint = state["interview_runtime"]["story"]["Requirement A"][
        "draft_projection"
    ]["artifact_fingerprint"]
    save_calls: list[object] = []

    def save_state(updated: JsonDict) -> None:
        state.update(updated)

    async def hydrate_context(_session_id: str, _project_id: int) -> SimpleNamespace:
        return SimpleNamespace(state=state, session_id="7")

    await save_story_draft(
        project_id=7,
        parent_requirement="Requirement A",
        load_state=lambda: _async_value(state),
        save_state=save_state,
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_stories_tool=lambda input_data, _tool_context: (
            save_calls.append(input_data)
            or {"success": True, "saved_count": 1, "story_ids": [7]}
        ),
        attempt_id="attempt-1",
        expected_artifact_fingerprint=artifact_fingerprint,
        expected_state="STORY_REVIEW",
        idempotency_key="story-save-7-requirement-a",
    )

    with pytest.raises(StoryPhaseError, match="artifact fingerprint"):
        await save_story_draft(
            project_id=7,
            parent_requirement="Requirement A",
            load_state=lambda: _async_value(state),
            save_state=save_state,
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_stories_tool=lambda input_data, _tool_context: (
                save_calls.append(input_data)
                or {"success": True, "saved_count": 1, "story_ids": [8]}
            ),
            attempt_id="attempt-1",
            expected_artifact_fingerprint="sha256:stale",
            expected_state="STORY_REVIEW",
            idempotency_key="story-save-7-requirement-a",
        )

    assert len(save_calls) == 1


@pytest.mark.asyncio
async def test_merge_story_resolution_normalizes_requirement_name() -> None:
    """Verify merge story resolution normalizes requirement name."""
    merge_artifact = _merge_recommended_artifact("Requirement A")
    state: JsonDict = {
        "roadmap_releases": [{"items": ["Requirement A"]}],
        "interview_runtime": {
            "story": {
                "Requirement A": {
                    "phase": "story",
                    "subject_key": "Requirement A",
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "classification": "reusable_content_result",
                            "is_reusable": True,
                            "retryable": False,
                            "draft_kind": "incomplete_draft",
                            "output_artifact": merge_artifact,
                        }
                    ],
                    "draft_projection": {
                        "latest_reusable_attempt_id": "attempt-1",
                        "kind": "incomplete_draft",
                        "is_complete": False,
                    },
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                }
            }
        },
    }
    saved_states: list[JsonDict] = []

    payload = await merge_story_resolution(
        parent_requirement="  Requirement A  ",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-04-04T12:00:00Z",
    )

    assert payload["parent_requirement"] == "Requirement A"
    resolution = payload["data"]["resolution"]["current"]
    assert (
        resolution["owner_requirement"]
        == "Updated Source Code Package (refactored prototype for submission)"
    )
    assert (
        state["interview_runtime"]["story"]["Requirement A"]["resolution_projection"]
        == resolution
    )
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_delete_story_requirement_normalizes_requirement_name() -> None:
    """Verify delete story requirement normalizes requirement name."""
    parent_requirement = "Requirement A"
    state: JsonDict = {
        "story_saved": {parent_requirement: True},
        "story_outputs": {parent_requirement: {"data": "some artifact"}},
        "story_attempts": {
            parent_requirement: [
                {
                    "created_at": "2026-03-28T10:00:00Z",
                    "trigger": "manual_refine",
                    "input_context": {},
                    "output_artifact": {"data": "some artifact"},
                    "is_complete": True,
                    "failure_artifact_id": None,
                    "failure_stage": None,
                    "failure_summary": None,
                    "raw_output_preview": None,
                    "has_full_artifact": False,
                }
            ]
        },
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "phase": "story",
                    "subject_key": parent_requirement,
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "created_at": "2026-03-28T10:00:00Z",
                            "trigger": "manual_refine",
                            "request_snapshot_id": "request-1",
                            "draft_basis_attempt_id": None,
                            "included_feedback_ids": ["feedback-1"],
                            "classification": "reusable_content_result",
                            "is_reusable": True,
                            "retryable": False,
                            "draft_kind": "complete_draft",
                            "output_artifact": {
                                "data": "some artifact",
                                "is_complete": True,
                            },
                            "failure_artifact_id": None,
                            "failure_stage": None,
                            "failure_summary": None,
                            "raw_output_preview": None,
                            "has_full_artifact": False,
                        }
                    ],
                    "draft_projection": {
                        "latest_reusable_attempt_id": "attempt-1",
                        "kind": "complete_draft",
                        "is_complete": True,
                        "updated_at": "2026-03-28T10:00:00Z",
                    },
                    "feedback_projection": {
                        "items": [
                            {
                                "feedback_id": "feedback-1",
                                "text": "keep it smaller",
                                "created_at": "2026-03-28T09:59:00Z",
                                "status": "absorbed",
                                "absorbed_by_attempt_id": "attempt-1",
                            }
                        ],
                        "next_feedback_sequence": 1,
                    },
                    "request_projection": {
                        "request_snapshot_id": "request-1",
                        "payload": {"parent_requirement": parent_requirement},
                        "request_hash": "hash-1",
                        "created_at": "2026-03-28T10:00:00Z",
                        "draft_basis_attempt_id": None,
                        "included_feedback_ids": ["feedback-1"],
                        "context_version": "story-runtime.v1",
                    },
                }
            }
        },
        "another_req": "should not be touched",
    }
    saved_states: list[JsonDict] = []

    payload = await delete_story_requirement(
        parent_requirement="  Requirement A  ",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-04-04T12:00:00Z",
        delete_requirement_stories=lambda normalized_requirement: 3,  # noqa: ARG005
        reset_subject_working_set=_reset_subject_working_set,
    )

    assert payload["parent_requirement"] == "Requirement A"
    assert payload["data"] == {
        "deleted_count": 3,
        "message": "Stories deleted successfully",
    }
    assert parent_requirement not in state["story_saved"]
    assert parent_requirement not in state["story_outputs"]
    assert len(state["story_attempts"][parent_requirement]) == 1
    assert state["another_req"] == "should not be touched"
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_merge_story_resolution_persists_merged_projection() -> None:
    """Verify merge story resolution persists merged projection."""
    merge_artifact = _merge_recommended_artifact("Requirement A")
    state: JsonDict = {
        "roadmap_releases": [{"items": ["Requirement A"]}],
        "interview_runtime": {
            "story": {
                "Requirement A": {
                    "phase": "story",
                    "subject_key": "Requirement A",
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "classification": "reusable_content_result",
                            "is_reusable": True,
                            "retryable": False,
                            "draft_kind": "incomplete_draft",
                            "output_artifact": merge_artifact,
                        }
                    ],
                    "draft_projection": {
                        "latest_reusable_attempt_id": "attempt-1",
                        "kind": "incomplete_draft",
                        "is_complete": False,
                    },
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                }
            }
        },
    }
    saved_states: list[JsonDict] = []

    payload = await merge_story_resolution(
        parent_requirement="Requirement A",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-04-04T12:00:00Z",
    )

    assert payload["parent_requirement"] == "Requirement A"
    resolution = payload["data"]["resolution"]["current"]
    assert resolution["status"] == "merged"
    assert (
        resolution["owner_requirement"]
        == "Updated Source Code Package (refactored prototype for submission)"
    )
    assert resolution["resolved_at"] == "2026-04-04T12:00:00Z"
    assert (
        state["interview_runtime"]["story"]["Requirement A"]["resolution_projection"]
        == resolution
    )
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_reconcile_requirement_reused_key_with_changed_request_fails_without_side_effects() -> None:  # noqa: E501
    """Requirement reconciliation keys can replay only the same request."""
    state: JsonDict = {
        "fsm_state": "STORY_INTERVIEW",
        "roadmap_releases": [{"items": ["Requirement A", "Requirement B"]}],
    }
    saved_states: list[JsonDict] = []

    first = await story_service.reconcile_requirement(
        project_id=1,
        requirement="Requirement A",
        action="already-implemented",
        reason="Delivered earlier.",
        idempotency_key="req-rec-reused",
        changed_by="agent",
        evidence_links=["evidence-a.md"],
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-06-26T10:00:00Z",
    )

    assert first["requirement"] == "Requirement A"
    assert first["action"] == "already-implemented"
    assert list(state["requirement_reconciliations"]) == ["requirement a"]
    assert len(state["requirement_reconciliation_history"]) == 1
    assert len(saved_states) == 1

    with pytest.raises(StoryPhaseError) as exc_info:
        await story_service.reconcile_requirement(
            project_id=1,
            requirement="Requirement B",
            action="duplicate",
            reason="Covered elsewhere.",
            idempotency_key="req-rec-reused",
            changed_by="agent",
            evidence_links=["evidence-b.md"],
            load_state=lambda: _async_value(state),
            save_state=lambda updated: saved_states.append(dict(updated)),
            now_iso=lambda: "2026-06-26T10:01:00Z",
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert "idempotency" in exc_info.value.detail.lower()
    assert list(state["requirement_reconciliations"]) == ["requirement a"]
    assert len(state["requirement_reconciliation_history"]) == 1
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_complete_story_phase_moves_to_sprint_setup_once_all_stories_are_saved() -> None:  # noqa: E501
    """Verify complete story phase moves to sprint setup once all stories are saved."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [{"items": ["Enable login", "Reset password"]}],
        "story_saved": {"Enable login": True, "Reset password": True},
        "story_completion_scope": {"scope": "milestone", "scope_id": "milestone_0"},
    }
    saved_states: list[JsonDict] = []

    payload = await complete_story_phase(
        expected_state="STORY_PERSISTENCE",
        idempotency_key="complete-story-all-saved",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-04-04T12:00:00Z",
    )

    assert payload == {
        "fsm_state": "SPRINT_SETUP",
        "coverage": {"saved": 2, "merged": 0, "total": 2},
        "idempotency_key": "complete-story-all-saved",
    }
    assert state["fsm_state"] == "SPRINT_SETUP"
    assert state["story_phase_completed_at"] == "2026-04-04T12:00:00Z"
    assert state["story_completion_scope"] is None
    assert state["story_complete_idempotency"]["complete-story-all-saved"] == payload
    assert len(saved_states) == 1

    replay_payload = await complete_story_phase(
        expected_state="STORY_PERSISTENCE",
        idempotency_key="complete-story-all-saved",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-04-04T12:01:00Z",
    )

    assert replay_payload == payload
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_complete_story_phase_persists_scope_clear_through_repo(
    tmp_path: Path,
) -> None:
    """Verify no-scope Story completion clears persisted stale scope."""
    repo = _session_repo_with_state(
        tmp_path,
        {
            "fsm_state": "STORY_PERSISTENCE",
            "roadmap_releases": [{"items": ["Enable login"]}],
            "story_saved": {"Enable login": True},
            "story_completion_scope": {
                "scope": "milestone",
                "scope_id": "milestone_5",
                "requirements": ["Old requirement"],
            },
        },
    )

    payload = await complete_story_phase(
        expected_state="STORY_PERSISTENCE",
        idempotency_key="complete-story-all-saved",
        load_state=lambda: _async_value(_load_repo_story_state(repo)),
        save_state=lambda updated: _save_repo_story_state(repo, updated),
        now_iso=lambda: "2026-04-04T12:00:00Z",
    )

    persisted = _load_repo_story_state(repo)
    assert payload["fsm_state"] == "SPRINT_SETUP"
    assert "story_completion_scope" not in payload
    assert persisted["story_completion_scope"] is None


@pytest.mark.asyncio
async def test_complete_story_phase_recovers_story_interview_when_all_stories_are_saved() -> None:  # noqa: E501
    """Allow guarded completion from stale Story interview with full coverage."""
    state: JsonDict = {
        "fsm_state": "STORY_INTERVIEW",
        "roadmap_releases": [{"items": ["Enable login", "Reset password"]}],
        "story_saved": {"Enable login": True, "Reset password": True},
    }
    saved_states: list[JsonDict] = []

    payload = await complete_story_phase(
        expected_state="STORY_INTERVIEW",
        idempotency_key="complete-story-interview-recovery",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-04-04T12:00:00Z",
    )

    assert payload["fsm_state"] == "SPRINT_SETUP"
    assert payload["coverage"] == {"saved": 2, "merged": 0, "total": 2}
    assert state["fsm_state"] == "SPRINT_SETUP"
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_complete_story_phase_recovers_story_interview_with_milestone_scope() -> None:  # noqa: E501
    """Allow stale Story interview recovery for a saved milestone scope."""
    state: JsonDict = {
        "fsm_state": "STORY_INTERVIEW",
        "roadmap_releases": [
            {"items": ["Enable login", "Reset password"]},
            {"items": ["Export reports"]},
        ],
        "story_saved": {"Enable login": True, "Reset password": True},
    }
    saved_states: list[JsonDict] = []

    payload = await complete_story_phase(
        expected_state="STORY_INTERVIEW",
        idempotency_key="complete-story-interview-scope",
        scope="milestone",
        scope_id="milestone_0",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-04-04T12:00:00Z",
    )

    assert payload["fsm_state"] == "SPRINT_SETUP"
    assert payload["coverage"] == {"saved": 2, "merged": 0, "total": 2}
    assert payload["story_completion_scope"]["scope"] == "milestone"
    assert payload["story_completion_scope"]["scope_id"] == "milestone_0"
    assert state["fsm_state"] == "SPRINT_SETUP"
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_complete_story_phase_blocks_until_all_roadmap_requirements_saved() -> None:  # noqa: E501
    """Verify complete story phase blocks until every roadmap requirement is covered."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [{"items": ["Enable login", "Reset password"]}],
        "story_saved": {"Enable login": True},
    }

    with pytest.raises(StoryPhaseError) as exc_info:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-incomplete",
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-04-04T12:00:00Z",
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert exc_info.value.detail == (
        "Story phase cannot complete: 1 of 2 roadmap requirements are saved, "
        "merged, or terminal-reconciled."
    )


@pytest.mark.asyncio
async def test_complete_story_phase_allows_saved_milestone_scope_with_pending_later_milestone() -> None:  # noqa: E501
    """Verify scoped completion gates only the selected roadmap milestone."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [
            {
                "theme": "First Slice",
                "items": ["Enable login", "Reset password"],
            },
            {
                "theme": "Later Slice",
                "items": ["Invite teammates"],
            },
        ],
        "story_saved": {"Enable login": True, "Reset password": True},
    }
    saved_states: list[JsonDict] = []

    payload = await complete_story_phase(
        expected_state="STORY_PERSISTENCE",
        idempotency_key="complete-story-milestone-0",
        scope="milestone",
        scope_id="milestone_0",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-06-03T12:00:00Z",
    )

    expected_scope = {
        "schema_version": "agileforge.story_completion_scope.v1",
        "scope": "milestone",
        "scope_id": "milestone_0",
        "requirements": ["Enable login", "Reset password"],
        "completed_at": "2026-06-03T12:00:00Z",
    }
    assert payload == {
        "fsm_state": "SPRINT_SETUP",
        "coverage": {"saved": 2, "merged": 0, "total": 2},
        "idempotency_key": "complete-story-milestone-0",
        "story_completion_scope": expected_scope,
    }
    assert state["fsm_state"] == "SPRINT_SETUP"
    assert state["story_completion_scope"] == expected_scope
    assert state["story_complete_idempotency"]["complete-story-milestone-0"] == payload
    assert len(saved_states) == 1

    replay_payload = await complete_story_phase(
        expected_state="STORY_PERSISTENCE",
        idempotency_key="complete-story-milestone-0",
        scope="milestone",
        scope_id="milestone_0",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-06-03T12:01:00Z",
    )

    assert replay_payload == payload
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_complete_story_phase_reused_key_with_changed_scope_fails_without_side_effects() -> None:  # noqa: E501
    """Story completion idempotency keys cannot move to a different scope."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [
            {"items": ["Enable login", "Reset password"]},
            {"items": ["Invite teammates"]},
        ],
        "story_saved": {
            "Enable login": True,
            "Reset password": True,
            "Invite teammates": True,
        },
    }
    saved_states: list[JsonDict] = []

    payload = await complete_story_phase(
        expected_state="STORY_PERSISTENCE",
        idempotency_key="complete-story-reused-scope",
        scope="milestone",
        scope_id="milestone_0",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-06-03T12:00:00Z",
    )

    assert payload["story_completion_scope"]["scope_id"] == "milestone_0"
    assert state["story_completion_scope"]["scope_id"] == "milestone_0"
    assert len(saved_states) == 1

    with pytest.raises(StoryPhaseError) as exc_info:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-reused-scope",
            scope="milestone",
            scope_id="milestone_1",
            load_state=lambda: _async_value(state),
            save_state=lambda updated: saved_states.append(dict(updated)),
            now_iso=lambda: "2026-06-03T12:01:00Z",
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert "idempotency" in exc_info.value.detail.lower()
    assert state["story_completion_scope"]["scope_id"] == "milestone_0"
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_complete_story_phase_scope_extension_milestone_keeps_extension_metadata() -> (  # noqa: E501
    None
):
    """Extension milestone completion does not require old original milestones."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "scope_extension_context": {
            "schema": "agileforge.scope_extension.v1",
            "base_spec_version_id": 7,
            "amended_spec_version_id": 12,
            "added_source_item_ids": ["SRC-NEW"],
        },
        "roadmap_releases": [
            {
                "theme": "Original milestone",
                "items": ["Original requirement"],
            },
            {
                "theme": "Extension milestone",
                "items": ["Extension requirement"],
                "extension_of_spec_version_id": 7,
                "accepted_spec_version_id": 12,
                "source_item_ids": ["SRC-NEW"],
            },
        ],
        "story_saved": {"Extension requirement": True},
        "story_saved_metadata": {
            "Extension requirement": {
                "extension_scope": True,
                "accepted_spec_version_id": 12,
                "source_item_ids": ["SRC-NEW"],
            }
        },
    }

    payload = await complete_story_phase(
        expected_state="STORY_PERSISTENCE",
        idempotency_key="complete-extension-milestone",
        scope="milestone",
        scope_id="milestone_1",
        load_state=lambda: _async_value(state),
        save_state=lambda _updated: None,
        now_iso=lambda: "2026-06-14T12:00:00Z",
    )

    assert payload["coverage"] == {"saved": 1, "merged": 0, "total": 1}
    assert payload["story_completion_scope"] == {
        "schema_version": "agileforge.story_completion_scope.v1",
        "scope": "milestone",
        "scope_id": "milestone_1",
        "requirements": ["Extension requirement"],
        "extension_scope": True,
        "accepted_spec_version_id": 12,
        "source_item_ids": ["SRC-NEW"],
        "completed_at": "2026-06-14T12:00:00Z",
    }


@pytest.mark.asyncio
async def test_complete_story_phase_scope_extension_requires_same_name_extension_save() -> (  # noqa: E501
    None
):
    """Same-name legacy saves do not cover appended extension milestones."""
    parent_requirement = "Shared requirement"
    state = _state_with_complete_story_draft(parent_requirement)
    state.update(
        {
            "fsm_state": "STORY_PERSISTENCE",
            "scope_extension_context": {
                "schema": "agileforge.scope_extension.v1",
                "base_spec_version_id": 7,
                "amended_spec_version_id": 12,
                "added_source_item_ids": ["SRC-NEW"],
            },
            "roadmap_releases": [
                {
                    "theme": "Original milestone",
                    "items": [parent_requirement],
                },
                {
                    "theme": "Extension milestone",
                    "items": [parent_requirement],
                    "extension_of_spec_version_id": 7,
                    "accepted_spec_version_id": 12,
                    "source_item_ids": ["SRC-NEW"],
                },
            ],
            "story_saved": {parent_requirement: True},
        }
    )

    with pytest.raises(StoryPhaseError, match="0 of 1"):
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-extension-milestone-legacy-only",
            scope="milestone",
            scope_id="milestone_1",
            load_state=lambda: _async_value(state),
            save_state=lambda _updated: None,
            now_iso=lambda: "2026-06-14T12:00:00Z",
        )

    state["fsm_state"] = "STORY_REVIEW"
    _mark_current_story_draft_scope_extension(state, parent_requirement)
    artifact_fingerprint = state["interview_runtime"]["story"][parent_requirement][
        "draft_projection"
    ]["artifact_fingerprint"]
    hydrated = SimpleNamespace(state=state, session_id="7")

    def fake_save_stories_tool(save_input: object, _context: object) -> JsonDict:
        save_payload = cast("Any", save_input)
        assert save_payload.story_origin == "scope_extension"
        assert save_payload.accepted_spec_version_id == 12  # noqa: PLR2004
        return {"success": True, "saved_count": 1}

    await save_story_draft(
        project_id=7,
        parent_requirement=parent_requirement,
        load_state=lambda: _async_value(state),
        save_state=lambda _updated: None,
        hydrate_context=lambda _session_id, _project_id: _async_value(hydrated),
        build_tool_context=lambda context: context,
        save_stories_tool=fake_save_stories_tool,
        attempt_id="attempt-1",
        expected_artifact_fingerprint=artifact_fingerprint,
        expected_state="STORY_REVIEW",
        idempotency_key="story-save-7-extension-shared",
    )

    payload = await complete_story_phase(
        expected_state="STORY_PERSISTENCE",
        idempotency_key="complete-extension-milestone-after-extension-save",
        scope="milestone",
        scope_id="milestone_1",
        load_state=lambda: _async_value(state),
        save_state=lambda _updated: None,
        now_iso=lambda: "2026-06-14T12:01:00Z",
    )

    assert payload["coverage"] == {"saved": 1, "merged": 0, "total": 1}
    assert payload["story_completion_scope"] == {
        "schema_version": "agileforge.story_completion_scope.v1",
        "scope": "milestone",
        "scope_id": "milestone_1",
        "requirements": [parent_requirement],
        "extension_scope": True,
        "accepted_spec_version_id": 12,
        "source_item_ids": ["SRC-NEW"],
        "completed_at": "2026-06-14T12:01:00Z",
    }
    assert state["story_completion_scope"]["extension_scope"] is True


@pytest.mark.asyncio
async def test_complete_story_phase_allows_saved_selection_scope_in_roadmap_order() -> None:  # noqa: E501
    """Verify selection scope completes only selected saved parent requirements."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [
            {"items": ["Enable login", "Reset password"]},
            {"items": ["Invite teammates"]},
        ],
        "story_saved": {"Enable login": True, "Reset password": True},
    }
    saved_states: list[JsonDict] = []

    payload = await complete_story_phase(
        expected_state="STORY_PERSISTENCE",
        idempotency_key="complete-story-selection-login",
        scope="selection",
        parent_requirements=[
            " reset password ",
            "Enable login",
            "ENABLE LOGIN",
        ],
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-06-09T12:00:00Z",
    )

    selected_requirements = ["Enable login", "Reset password"]
    expected_scope = {
        "schema_version": "agileforge.story_completion_scope.v1",
        "scope": "selection",
        "scope_id": "selection:"
        + canonical_hash(
            {"scope": "selection", "requirements": selected_requirements}
        ),
        "requirements": selected_requirements,
        "completed_at": "2026-06-09T12:00:00Z",
    }
    assert payload == {
        "fsm_state": "SPRINT_SETUP",
        "coverage": {"saved": 2, "merged": 0, "total": 2},
        "idempotency_key": "complete-story-selection-login",
        "story_completion_scope": expected_scope,
    }
    assert state["fsm_state"] == "SPRINT_SETUP"
    assert state["story_completion_scope"] == expected_scope
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_complete_story_phase_rejects_empty_selection_scope() -> None:
    """Verify selection scope requires at least one parent requirement."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [{"items": ["Enable login"]}],
        "story_saved": {"Enable login": True},
    }

    with pytest.raises(StoryPhaseError) as exc_info:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-empty-selection",
            scope="selection",
            parent_requirements=[" ", ""],
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-06-09T12:00:00Z",
        )

    assert exc_info.value.status_code == 400  # noqa: PLR2004
    assert exc_info.value.detail == (
        "story complete --scope selection requires at least one "
        "--parent-requirement"
    )
    assert state["fsm_state"] == "STORY_PERSISTENCE"


@pytest.mark.asyncio
async def test_complete_story_phase_rejects_unknown_selection_requirement() -> None:
    """Verify selection scope rejects parent requirements outside the roadmap."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [{"items": ["Enable login"]}],
        "story_saved": {"Enable login": True},
    }

    with pytest.raises(StoryPhaseError) as exc_info:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-unknown-selection",
            scope="selection",
            parent_requirements=["Missing requirement"],
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-06-09T12:00:00Z",
        )

    assert exc_info.value.status_code == 400  # noqa: PLR2004
    assert exc_info.value.detail == (
        "Story completion selection includes unknown roadmap requirement: "
        "Missing requirement."
    )
    assert state["fsm_state"] == "STORY_PERSISTENCE"


@pytest.mark.asyncio
async def test_complete_story_phase_rejects_unsaved_selection_requirement() -> None:
    """Verify selection scope still requires saved or merged Story output."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [{"items": ["Enable login", "Reset password"]}],
        "story_saved": {"Enable login": True},
    }

    selected_requirements = ["Enable login", "Reset password"]
    expected_scope_id = "selection:" + canonical_hash(
        {"scope": "selection", "requirements": selected_requirements}
    )
    with pytest.raises(StoryPhaseError) as exc_info:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-unsaved-selection",
            scope="selection",
            parent_requirements=selected_requirements,
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-06-09T12:00:00Z",
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert exc_info.value.detail == (
        f"Story phase cannot complete for {expected_scope_id}: "
        "1 of 2 roadmap requirements are saved, merged, or terminal-reconciled."
    )
    assert state["fsm_state"] == "STORY_PERSISTENCE"


@pytest.mark.asyncio
async def test_complete_story_phase_rejects_selection_argument_combinations() -> None:
    """Verify selection flags are only accepted for selection completion."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [{"items": ["Enable login"]}],
        "story_saved": {"Enable login": True},
    }

    with pytest.raises(StoryPhaseError) as full_exc:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-parent-no-scope",
            parent_requirements=["Enable login"],
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-06-09T12:00:00Z",
        )
    assert full_exc.value.status_code == 400  # noqa: PLR2004
    assert full_exc.value.detail == (
        "--parent-requirement is only supported with --scope selection"
    )

    with pytest.raises(StoryPhaseError) as milestone_exc:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-parent-milestone",
            scope="milestone",
            scope_id="milestone_0",
            parent_requirements=["Enable login"],
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-06-09T12:00:00Z",
        )
    assert milestone_exc.value.status_code == 400  # noqa: PLR2004
    assert milestone_exc.value.detail == (
        "--parent-requirement is only supported with --scope selection"
    )

    with pytest.raises(StoryPhaseError) as scope_id_exc:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-selection-scope-id",
            scope="selection",
            scope_id="selection:manual",
            parent_requirements=["Enable login"],
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-06-09T12:00:00Z",
        )
    assert scope_id_exc.value.status_code == 400  # noqa: PLR2004
    assert scope_id_exc.value.detail == (
        "story complete --scope selection does not accept --scope-id"
    )


@pytest.mark.asyncio
async def test_complete_story_phase_rejects_unknown_milestone_scope_id() -> None:
    """Verify unknown milestone scopes fail without advancing the FSM."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [{"items": ["Enable login"]}],
        "story_saved": {"Enable login": True},
    }

    with pytest.raises(StoryPhaseError) as exc_info:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-missing-scope",
            scope="milestone",
            scope_id="milestone_7",
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-06-03T12:00:00Z",
        )

    assert exc_info.value.status_code == 400  # noqa: PLR2004
    assert exc_info.value.detail == (
        "Story completion scope milestone_7 does not match any roadmap milestone."
    )
    assert state["fsm_state"] == "STORY_PERSISTENCE"
    assert "story_completion_scope" not in state


@pytest.mark.asyncio
async def test_complete_story_phase_blocks_incomplete_milestone_scope() -> None:
    """Verify scoped completion reports coverage only for the selected milestone."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [
            {"items": ["Enable login", "Reset password"]},
            {"items": ["Invite teammates"]},
        ],
        "story_saved": {"Enable login": True, "Invite teammates": True},
    }

    with pytest.raises(StoryPhaseError) as exc_info:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-incomplete-scope",
            scope="milestone",
            scope_id="milestone_0",
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-06-03T12:00:00Z",
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert exc_info.value.detail == (
        "Story phase cannot complete for milestone_0: "
        "1 of 2 roadmap requirements are saved, merged, or terminal-reconciled."
    )
    assert state["fsm_state"] == "STORY_PERSISTENCE"


@pytest.mark.asyncio
async def test_complete_story_phase_incomplete_coverage_does_not_create_runtime() -> None:  # noqa: E501
    """Verify incomplete story completion does not create missing runtime state."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [{"items": ["Enable login", "Reset password"]}],
        "story_saved": {"Enable login": True},
        "interview_runtime": {"story": {}},
    }

    with pytest.raises(StoryPhaseError) as exc_info:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-incomplete-no-runtime",
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-04-04T12:00:00Z",
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert "Reset password" not in state["interview_runtime"]["story"]


@pytest.mark.asyncio
async def test_complete_story_phase_counts_merged_resolution_as_covered() -> None:
    """Verify complete story phase treats merged resolutions as covered."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [{"items": ["Enable login", "Reset password"]}],
        "story_saved": {"Enable login": True},
        "interview_runtime": {
            "story": {
                "Reset password": {
                    "resolution_projection": {
                        "status": "merged",
                        "owner_requirement": "Enable login",
                        "reason": "Reset password belongs with login.",
                        "acceptance_criteria_to_move": [
                            "Verify reset password remains available from login."
                        ],
                        "resolved_at": "2026-04-04T11:00:00Z",
                    }
                }
            }
        },
    }

    payload = await complete_story_phase(
        expected_state="STORY_PERSISTENCE",
        idempotency_key="complete-story-with-merge",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: None,  # noqa: ARG005
        now_iso=lambda: "2026-04-04T12:00:00Z",
    )

    assert payload["fsm_state"] == "SPRINT_SETUP"
    assert payload["coverage"] == {"saved": 1, "merged": 1, "total": 2}


@pytest.mark.asyncio
async def test_complete_story_phase_counts_terminal_requirement_reconciliation() -> (
    None
):
    """Story phase can complete when uncovered requirements are reconciled."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [
            {"items": ["Requirement A", "Requirement B"]},
        ],
        "story_saved": {"Requirement A": True},
        "requirement_reconciliations": {
            "requirement b": {
                "schema_version": "agileforge.requirement_reconciliation.v1",
                "requirement": "Requirement B",
                "action": "duplicate",
                "reason": "Covered by Requirement A.",
                "evidence_links": ["story-17"],
                "changed_by": "agent",
                "reconciled_at": "2026-06-25T10:00:00Z",
                "idempotency_key": "req-rec-2",
                "terminal": True,
            }
        },
    }
    saved_state: JsonDict = {}

    payload = await complete_story_phase(
        expected_state="STORY_PERSISTENCE",
        idempotency_key="complete-with-reconciled-req",
        load_state=lambda: _async_value(state),
        save_state=saved_state.update,
        now_iso=lambda: "2026-06-25T10:05:00Z",
    )

    assert payload["coverage"] == {
        "saved": 1,
        "merged": 0,
        "reconciled": 1,
        "total": 2,
    }
    assert saved_state["fsm_state"] == "SPRINT_SETUP"


@pytest.mark.asyncio
async def test_complete_story_phase_requires_guards() -> None:
    """Verify complete story phase requires state and idempotency guards."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [{"items": ["Enable login"]}],
        "story_saved": {"Enable login": True},
    }

    with pytest.raises(StoryPhaseError) as missing_key:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="",
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-04-04T12:00:00Z",
        )
    assert missing_key.value.status_code == 400  # noqa: PLR2004
    assert missing_key.value.detail == ("story complete requires --idempotency-key")

    payload = await complete_story_phase(
        expected_state="STORY_PERSISTENCE",
        idempotency_key="complete-story-guarded",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: None,  # noqa: ARG005
        now_iso=lambda: "2026-04-04T12:00:00Z",
    )
    assert payload["fsm_state"] == "SPRINT_SETUP"

    with pytest.raises(StoryPhaseError) as wrong_expected:
        await complete_story_phase(
            expected_state="STORY_REVIEW",
            idempotency_key="complete-story-guarded",
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-04-04T12:00:00Z",
        )
    assert wrong_expected.value.status_code == 400  # noqa: PLR2004
    assert wrong_expected.value.detail == (
        "story complete requires --expected-state STORY_PERSISTENCE "
        "or STORY_INTERVIEW"
    )

    with pytest.raises(StoryPhaseError) as stale_fsm:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-stale-fsm",
            load_state=lambda: _async_value({"fsm_state": "STORY_REVIEW"}),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-04-04T12:00:00Z",
        )
    assert stale_fsm.value.status_code == 409  # noqa: PLR2004


@pytest.mark.asyncio
async def test_reopen_story_requirement_clears_saved_projection_before_sprint_work() -> None:  # noqa: E501
    """Verify Story reopen clears saved state before Sprint work exists."""
    parent_requirement = (
        "Live Pre-Lock Recommendation Workflow with Risk-Audited Artifact"
    )
    state: dict[str, Any] = {
        "fsm_state": "SPRINT_SETUP",
        "roadmap_releases": [{"items": [parent_requirement]}],
        "story_saved": {parent_requirement: True},
        "story_outputs": {
            parent_requirement: _story_artifact(parent_requirement, "Old story")
        },
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "draft_projection": {"latest_reusable_attempt_id": "attempt-1"},
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "trigger": "manual_refine",
                            "output_artifact": _story_artifact(
                                parent_requirement,
                                "Old story",
                            ),
                        }
                    ],
                }
            }
        },
    }
    saved_states: list[dict[str, Any]] = []

    payload = await story_service.reopen_story_requirement(
        parent_requirement=f"  {parent_requirement.lower()}  ",
        expected_state="SPRINT_SETUP",
        idempotency_key="reopen-story-live-budget",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-05-23T12:00:00Z",
        assert_reopen_safe=lambda _normalized_requirement: None,
        reset_subject_working_set=reset_subject_working_set,
    )

    assert payload == {
        "parent_requirement": parent_requirement,
        "fsm_state": "STORY_INTERVIEW",
        "idempotency_key": "reopen-story-live-budget",
    }
    assert state["fsm_state"] == "STORY_INTERVIEW"
    assert parent_requirement not in state["story_saved"]
    assert parent_requirement not in state["story_outputs"]
    runtime = state["interview_runtime"]["story"][parent_requirement]
    assert runtime["draft_projection"] == {}
    assert saved_states


@pytest.mark.asyncio
async def test_reopen_story_requirement_reused_key_with_changed_requirement_fails_without_side_effects() -> None:  # noqa: E501
    """Story reopen keys can replay only the same parent requirement."""
    first_requirement = "Live Pre-Lock Recommendation Workflow"
    second_requirement = "Risk-Audited Artifact"
    state: dict[str, Any] = {
        "fsm_state": "SPRINT_SETUP",
        "roadmap_releases": [{"items": [first_requirement, second_requirement]}],
        "story_saved": {
            first_requirement: True,
            second_requirement: True,
        },
    }
    saved_states: list[dict[str, Any]] = []

    first = await story_service.reopen_story_requirement(
        parent_requirement=first_requirement,
        expected_state="SPRINT_SETUP",
        idempotency_key="reopen-story-reused",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-05-23T12:00:00Z",
        assert_reopen_safe=lambda _normalized_requirement: None,
        reset_subject_working_set=reset_subject_working_set,
    )

    assert first["parent_requirement"] == first_requirement
    assert state["story_saved"] == {second_requirement: True}
    assert len(saved_states) == 1

    with pytest.raises(StoryPhaseError) as exc_info:
        await story_service.reopen_story_requirement(
            parent_requirement=second_requirement,
            expected_state="SPRINT_SETUP",
            idempotency_key="reopen-story-reused",
            load_state=lambda: _async_value(state),
            save_state=lambda updated: saved_states.append(dict(updated)),
            now_iso=lambda: "2026-05-23T12:01:00Z",
            assert_reopen_safe=lambda _normalized_requirement: None,
            reset_subject_working_set=reset_subject_working_set,
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert "idempotency" in exc_info.value.detail.lower()
    assert state["story_saved"] == {second_requirement: True}
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_reopen_story_requirement_blocks_unsaved_roadmap_requirement() -> None:
    """Verify Story reopen requires a saved roadmap requirement."""
    parent_requirement = (
        "Live Pre-Lock Recommendation Workflow with Risk-Audited Artifact"
    )
    state: dict[str, Any] = {
        "fsm_state": "SPRINT_SETUP",
        "roadmap_releases": [{"items": [parent_requirement]}],
        "story_saved": {parent_requirement: False},
        "story_outputs": {
            parent_requirement: _story_artifact(parent_requirement, "Draft story")
        },
    }
    saved_states: list[dict[str, Any]] = []

    with pytest.raises(StoryPhaseError) as excinfo:
        await story_service.reopen_story_requirement(
            parent_requirement=parent_requirement,
            expected_state="SPRINT_SETUP",
            idempotency_key="reopen-story-unsaved",
            load_state=lambda: _async_value(state),
            save_state=lambda updated: saved_states.append(dict(updated)),
            now_iso=lambda: "2026-05-23T12:00:00Z",
            assert_reopen_safe=lambda _normalized_requirement: None,
            reset_subject_working_set=reset_subject_working_set,
        )

    assert excinfo.value.status_code == 409  # noqa: PLR2004
    assert "saved" in excinfo.value.detail.lower()
    assert state["fsm_state"] == "SPRINT_SETUP"
    assert state["story_saved"] == {parent_requirement: False}
    assert parent_requirement in state["story_outputs"]
    assert saved_states == []


@pytest.mark.asyncio
async def test_reopen_story_requirement_blocks_when_downstream_work_exists() -> None:
    """Verify Story reopen blocks when active stories have Sprint links."""
    parent_requirement = (
        "Live Pre-Lock Recommendation Workflow with Risk-Audited Artifact"
    )
    state: dict[str, Any] = {
        "fsm_state": "SPRINT_SETUP",
        "roadmap_releases": [{"items": [parent_requirement]}],
        "story_saved": {parent_requirement: True},
    }

    with pytest.raises(StoryPhaseError) as excinfo:
        await story_service.reopen_story_requirement(
            parent_requirement=parent_requirement,
            expected_state="SPRINT_SETUP",
            idempotency_key="reopen-story-live-budget",
            load_state=lambda: _async_value(state),
            save_state=lambda _updated: None,
            now_iso=lambda: "2026-05-23T12:00:00Z",
            assert_reopen_safe=lambda _normalized_requirement: (_ for _ in ()).throw(
                StoryPhaseError(
                    "Story correction is unsafe: story has sprint links.",
                    status_code=409,
                )
            ),
            reset_subject_working_set=reset_subject_working_set,
        )

    assert excinfo.value.status_code == 409  # noqa: PLR2004
    assert "unsafe" in excinfo.value.detail.lower()
    assert state["fsm_state"] == "SPRINT_SETUP"


@pytest.mark.asyncio
async def test_repair_story_completion_scope_clears_expected_scope() -> None:
    """Verify stale Story completion scope can be cleared without Story row repair."""
    stale_scope = {
        "scope": "milestone",
        "scope_id": "milestone_5",
        "requirements": ["Old requirement"],
        "extension_scope": True,
        "accepted_spec_version_id": 4,
    }
    state: dict[str, Any] = {
        "fsm_state": "SPRINT_SETUP",
        "story_completion_scope": dict(stale_scope),
    }
    saved_states: list[dict[str, Any]] = []

    payload = await repair_story_completion_scope(
        project_id=2,
        expected_state="SPRINT_SETUP",
        expected_scope_id="milestone_5",
        idempotency_key="repair-story-completion-scope-2",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-06-29T12:00:00Z",
    )

    assert payload == {
        "project_id": 2,
        "fsm_state": "SPRINT_SETUP",
        "cleared_story_completion_scope": stale_scope,
        "cleared_at": "2026-06-29T12:00:00Z",
        "idempotency_key": "repair-story-completion-scope-2",
    }
    assert state["story_completion_scope"] is None
    assert state["story_completion_scope_repair_idempotency"][
        "repair-story-completion-scope-2"
    ] == payload
    assert len(saved_states) == 1

    replay_payload = await repair_story_completion_scope(
        project_id=2,
        expected_state="SPRINT_SETUP",
        expected_scope_id="milestone_5",
        idempotency_key="repair-story-completion-scope-2",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-06-29T12:01:00Z",
    )

    assert replay_payload == payload
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_repair_story_completion_scope_persists_clear_through_repo(
    tmp_path: Path,
) -> None:
    """Verify completion-scope repair clears persisted stale scope."""
    repo = _session_repo_with_state(
        tmp_path,
        {
            "fsm_state": "SPRINT_SETUP",
            "story_completion_scope": {
                "scope": "milestone",
                "scope_id": "milestone_5",
                "requirements": ["Old requirement"],
                "extension_scope": True,
                "accepted_spec_version_id": 4,
            },
        },
    )

    payload = await repair_story_completion_scope(
        project_id=2,
        expected_state="SPRINT_SETUP",
        expected_scope_id="milestone_5",
        idempotency_key="repair-story-completion-scope-2",
        load_state=lambda: _async_value(_load_repo_story_state(repo)),
        save_state=lambda updated: _save_repo_story_state(repo, updated),
        now_iso=lambda: "2026-06-29T12:00:00Z",
    )

    persisted = _load_repo_story_state(repo)
    assert payload["fsm_state"] == "SPRINT_SETUP"
    assert persisted["story_completion_scope"] is None


@pytest.mark.asyncio
async def test_repair_story_readiness_backfills_rank_and_points_from_saved_outputs() -> None:  # noqa: E501
    """Verify Story readiness repair computes metadata without rewriting stories."""
    parent_requirement = "Requirement A"
    state: dict[str, Any] = {
        "fsm_state": "SPRINT_SETUP",
        "roadmap_releases": [{"items": [parent_requirement]}],
        "story_saved": {parent_requirement: True},
        "story_outputs": {
            parent_requirement: {
                "parent_requirement": parent_requirement,
                "is_complete": True,
                "user_stories": [
                    {
                        "story_title": "Story A",
                        "statement": "As a user, I want alpha, so that I get value.",
                        "acceptance_criteria": ["Verify alpha."],
                        "invest_score": "High",
                        "estimated_effort": "L",
                    }
                ],
            }
        },
    }
    repaired: list[dict[str, Any]] = []

    payload = await repair_story_readiness(
        project_id=2,
        expected_state="SPRINT_SETUP",
        idempotency_key="repair-story-readiness-2",
        load_state=lambda: _async_value(state),
        save_state=state.update,
        repair_rows=lambda request: (
            repaired.append(request)
            or {
                "repaired_count": 1,
                "story_ids": [66],
            }
        ),
        assert_repair_safe=lambda _project_id: None,
    )

    assert payload["fsm_state"] == "SPRINT_SETUP"
    assert payload["repair_result"]["repaired_count"] == 1
    assert repaired[0]["items"] == [
        {
            "parent_requirement": parent_requirement,
            "parent_rank": 1,
            "slot": 1,
            "story_points": 5,
            "rank": "101",
        }
    ]


@pytest.mark.asyncio
async def test_repair_story_readiness_blocks_after_sprint_work_exists() -> None:
    """Verify Story readiness repair fails closed after Sprint work starts."""
    state = {"fsm_state": "SPRINT_SETUP"}

    with pytest.raises(StoryPhaseError) as excinfo:
        await repair_story_readiness(
            project_id=2,
            expected_state="SPRINT_SETUP",
            idempotency_key="repair-story-readiness-2",
            load_state=lambda: _async_value(state),
            save_state=lambda _updated: None,
            repair_rows=lambda _request: {},
            assert_repair_safe=lambda _project_id: (_ for _ in ()).throw(
                StoryPhaseError(
                    "Story readiness repair is unsafe after Sprint work exists.",
                    status_code=409,
                )
            ),
        )

    assert excinfo.value.status_code == 409  # noqa: PLR2004


@pytest.mark.asyncio
async def test_repair_story_readiness_replays_after_state_advances() -> None:
    """Verify Story readiness repair idempotency survives later FSM changes."""
    payload = {
        "project_id": 2,
        "fsm_state": "SPRINT_SETUP",
        "idempotency_key": "repair-story-readiness-2",
        "repair_result": {"repaired_count": 1, "story_ids": [66]},
    }
    state = {
        "fsm_state": "SPRINT_PLANNING",
        "story_readiness_repair_idempotency": {
            "repair-story-readiness-2": payload,
        },
    }

    result = await repair_story_readiness(
        project_id=2,
        expected_state="SPRINT_SETUP",
        idempotency_key="repair-story-readiness-2",
        load_state=lambda: _async_value(state),
        save_state=lambda _updated: None,
        repair_rows=lambda _request: pytest.fail("repair should replay"),
        assert_repair_safe=lambda _project_id: pytest.fail("guard should replay"),
    )

    assert result == payload


@pytest.mark.asyncio
async def test_repair_story_readiness_blocks_without_saved_outputs() -> None:
    """Verify Story readiness repair cannot record a no-op success."""
    state = {
        "fsm_state": "SPRINT_SETUP",
        "roadmap_releases": [{"items": ["Requirement A"]}],
    }

    with pytest.raises(StoryPhaseError) as excinfo:
        await repair_story_readiness(
            project_id=2,
            expected_state="SPRINT_SETUP",
            idempotency_key="repair-story-readiness-2",
            load_state=lambda: _async_value(state),
            save_state=lambda _updated: None,
            repair_rows=lambda _request: {},
            assert_repair_safe=lambda _project_id: None,
        )

    assert excinfo.value.status_code == 409  # noqa: PLR2004
    assert "saved Story outputs" in excinfo.value.detail


@pytest.mark.asyncio
async def test_delete_story_requirement_resets_runtime_and_clears_saved_projection() -> (  # noqa: E501
    None
):
    """Verify delete story requirement resets runtime and clears saved projection."""
    parent_requirement = "Requirement A"
    state: JsonDict = {
        "story_saved": {parent_requirement: True},
        "story_outputs": {parent_requirement: {"data": "some artifact"}},
        "story_attempts": {
            parent_requirement: [
                {
                    "created_at": "2026-03-28T10:00:00Z",
                    "trigger": "manual_refine",
                    "input_context": {},
                    "output_artifact": {"data": "some artifact"},
                    "is_complete": True,
                    "failure_artifact_id": None,
                    "failure_stage": None,
                    "failure_summary": None,
                    "raw_output_preview": None,
                    "has_full_artifact": False,
                }
            ]
        },
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "phase": "story",
                    "subject_key": parent_requirement,
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "created_at": "2026-03-28T10:00:00Z",
                            "trigger": "manual_refine",
                            "request_snapshot_id": "request-1",
                            "draft_basis_attempt_id": None,
                            "included_feedback_ids": ["feedback-1"],
                            "classification": "reusable_content_result",
                            "is_reusable": True,
                            "retryable": False,
                            "draft_kind": "complete_draft",
                            "output_artifact": {
                                "data": "some artifact",
                                "is_complete": True,
                            },
                            "failure_artifact_id": None,
                            "failure_stage": None,
                            "failure_summary": None,
                            "raw_output_preview": None,
                            "has_full_artifact": False,
                        }
                    ],
                    "draft_projection": {
                        "latest_reusable_attempt_id": "attempt-1",
                        "kind": "complete_draft",
                        "is_complete": True,
                        "updated_at": "2026-03-28T10:00:00Z",
                    },
                    "feedback_projection": {
                        "items": [
                            {
                                "feedback_id": "feedback-1",
                                "text": "keep it smaller",
                                "created_at": "2026-03-28T09:59:00Z",
                                "status": "absorbed",
                                "absorbed_by_attempt_id": "attempt-1",
                            }
                        ],
                        "next_feedback_sequence": 1,
                    },
                    "request_projection": {
                        "request_snapshot_id": "request-1",
                        "payload": {"parent_requirement": parent_requirement},
                        "request_hash": "hash-1",
                        "created_at": "2026-03-28T10:00:00Z",
                        "draft_basis_attempt_id": None,
                        "included_feedback_ids": ["feedback-1"],
                        "context_version": "story-runtime.v1",
                    },
                }
            }
        },
        "another_req": "should not be touched",
    }
    saved_states: list[JsonDict] = []

    payload = await delete_story_requirement(
        parent_requirement=parent_requirement,
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-04-04T12:00:00Z",
        delete_requirement_stories=lambda normalized_requirement: 3,  # noqa: ARG005
        reset_subject_working_set=_reset_subject_working_set,
    )

    assert payload == {
        "parent_requirement": "Requirement A",
        "data": {
            "deleted_count": 3,
            "message": "Stories deleted successfully",
        },
    }
    assert parent_requirement not in state["story_saved"]
    assert parent_requirement not in state["story_outputs"]
    assert len(state["story_attempts"][parent_requirement]) == 1
    assert state["story_attempts"][parent_requirement][0]["trigger"] == "manual_refine"
    runtime = state["interview_runtime"]["story"][parent_requirement]
    assert isinstance(runtime, dict)
    feedback_projection = runtime["feedback_projection"]
    assert isinstance(feedback_projection, dict)
    attempt_history = runtime["attempt_history"]
    assert isinstance(attempt_history, list)
    last_attempt = attempt_history[-1]
    assert isinstance(last_attempt, dict)
    summary = last_attempt["summary"]
    assert isinstance(summary, str)
    assert runtime["draft_projection"] == {}
    assert runtime["request_projection"] == {}
    assert feedback_projection["items"] == []
    assert len(attempt_history) == 2  # noqa: PLR2004
    assert last_attempt["trigger"] == "reset"
    assert last_attempt["classification"] == "reset_marker"
    assert "state reset by user" in summary
    assert state["another_req"] == "should not be touched"
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_delete_story_requirement_rejects_unknown_requirement_before_repo_delete() -> (  # noqa: E501
    None
):
    """Verify delete story requirement rejects unknown requirement before repo delete."""  # noqa: E501
    state: JsonDict = {
        "story_saved": {"Requirement A": True},
        "story_outputs": {"Requirement A": {"data": "some artifact"}},
        "story_attempts": {"Requirement A": []},
        "interview_runtime": {"story": {"Requirement A": {"attempt_history": []}}},
    }
    delete_called = False

    def delete_requirement_stories(_normalized_requirement: str) -> int:
        nonlocal delete_called
        delete_called = True
        return 1

    with pytest.raises(StoryPhaseError) as exc_info:
        await delete_story_requirement(
            parent_requirement="  Missing Requirement  ",
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-04-04T12:00:00Z",
            delete_requirement_stories=delete_requirement_stories,
            reset_subject_working_set=_reset_subject_working_set,
        )

    assert exc_info.value.status_code == 400  # noqa: PLR2004
    assert delete_called is False


def _reset_subject_working_set(
    runtime: JsonDict, *, created_at: str, summary: str
) -> JsonDict:
    runtime["draft_projection"] = {}
    runtime["request_projection"] = {}
    runtime["feedback_projection"] = {"items": [], "next_feedback_sequence": 0}
    attempts = list(runtime.get("attempt_history") or [])
    attempts.append(
        {
            "attempt_id": f"reset-marker-{len(attempts) + 1}",
            "created_at": created_at,
            "trigger": "reset",
            "classification": "reset_marker",
            "is_reusable": False,
            "retryable": False,
            "summary": summary,
            "output_artifact": None,
        }
    )
    runtime["attempt_history"] = attempts
    return runtime


async def _async_value[T](value: T) -> T:
    return value


@pytest.mark.asyncio
async def test_get_story_pending_with_metadata() -> None:
    """Verify get_story_pending incorporates stories_metadata correctly."""
    story_id = 101
    state = {
        "roadmap_releases": [
            {
                "theme": "Milestone 1",
                "items": ["Requirement A"],
            }
        ],
        "story_saved": {
            "Requirement A": True
        },
    }

    stories_metadata = {
        "requirement a": {
            "stories": [
                {
                    "story_id": story_id,
                    "title": "A story",
                    "status": "To Do",
                    "is_refined": True,
                    "is_superseded": False,
                }
            ],
            "story_ids": [story_id],
            "has_candidates": True,
        }
    }

    payload = await get_story_pending(
        load_state=lambda: _async_value(state),
        stories_metadata=stories_metadata,
    )

    req = payload["grouped_items"][0]["requirements"][0]
    assert req["requirement"] == "Requirement A"
    assert req["status"] == "Saved"
    assert req["sprint_eligible"] is True
    assert req["sprint_eligibility_reason"] == "eligible"
    assert req["story_ids"] == [story_id]
    assert len(req["stories"]) == 1
    assert req["stories"][0]["story_id"] == story_id


@pytest.mark.asyncio
async def test_get_story_pending_fallback_includes_selection_metadata() -> None:
    """Fallback pending payload should still expose selector metadata."""
    state = {
        "roadmap_releases": [
            {
                "theme": "Milestone 1",
                "items": ["Requirement A"],
            }
        ],
        "story_saved": {"Requirement A": True},
    }

    payload = await get_story_pending(load_state=lambda: _async_value(state))

    req = payload["grouped_items"][0]["requirements"][0]
    assert req["sprint_eligible"] is True
    assert req["sprint_eligibility_reason"] == "eligible"
    assert req["stories"] == []
    assert req["story_ids"] == []


@pytest.mark.asyncio
async def test_get_story_pending_prioritizes_superseded_reason() -> None:
    """All-superseded requirements should not be reported as completed."""
    story_id = 276
    state = {
        "roadmap_releases": [
            {
                "theme": "Milestone 1",
                "items": ["Requirement A"],
            }
        ],
        "story_saved": {"Requirement A": True},
    }
    stories_metadata = {
        "requirement a": {
            "stories": [
                {
                    "story_id": story_id,
                    "title": "Duplicate story",
                    "status": "To Do",
                    "is_refined": True,
                    "is_superseded": True,
                }
            ],
            "story_ids": [story_id],
            "has_candidates": False,
            "all_completed": True,
            "all_superseded": True,
        }
    }

    payload = await get_story_pending(
        load_state=lambda: _async_value(state),
        stories_metadata=stories_metadata,
    )

    req = payload["grouped_items"][0]["requirements"][0]
    assert req["sprint_eligible"] is False
    assert req["sprint_eligibility_reason"] == "all_stories_superseded"
