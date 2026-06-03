"""API tests for story interview generation, retry, history, and merge flows."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

import api as api_module
from services.phases.story_service import _story_artifact_fingerprint


@dataclass
class DummyProduct:  # noqa: D101
    product_id: int
    name: str
    description: str | None = None
    spec_file_path: str | None = None
    compiled_authority_json: str | None = None


class DummyProductRepository:  # noqa: D101
    def __init__(self) -> None:  # noqa: D107
        self.products = []

    def get_by_id(self, product_id: int):  # noqa: ANN201, D102
        for product in self.products:
            if product.product_id == product_id:
                return product
        return None

    def create(self, name: str) -> DummyProduct:  # noqa: D102
        product = DummyProduct(
            product_id=len(self.products) + 1,
            name=name,
        )
        self.products.append(product)
        return product


class DummyWorkflowService:  # noqa: D101
    def __init__(self) -> None:  # noqa: D107
        self.states: dict[str, dict[str, Any]] = {}

    async def initialize_session(self, session_id: str | None = None) -> str:  # noqa: D102
        sid = str(session_id or "generated")
        self.states[sid] = {"fsm_state": "STORY_INTERVIEW"}
        return sid

    def get_session_status(self, session_id: str):  # noqa: ANN201, D102
        return dict(self.states.get(str(session_id), {}))

    def update_session_status(self, session_id: str, partial_update):  # noqa: ANN001, ANN201, D102
        current = dict(self.states.get(str(session_id), {}))
        current.update(partial_update)
        self.states[str(session_id)] = current

    def migrate_legacy_setup_state(self) -> int:  # noqa: D102
        return 0


def _story_artifact(
    parent_requirement: str, title: str, *, is_complete: bool = True
) -> dict[str, Any]:
    return {
        "parent_requirement": parent_requirement,
        "user_stories": [
            {
                "story_title": title,
                "statement": "As a developer, I want projection-aware drafts, so that retries and saves stay stable.",  # noqa: E501
                "acceptance_criteria": [
                    "Verify the API reads the reusable projection."
                ],
                "invest_score": "High",
                "estimated_effort": "S",
                "produced_artifacts": [],
            }
        ],
        "is_complete": is_complete,
        "clarifying_questions": [],
    }


def _merge_recommended_artifact(parent_requirement: str) -> dict[str, Any]:
    artifact = _story_artifact(
        parent_requirement,
        "Validate execution evidence meets submission standards",
        is_complete=False,
    )
    artifact["user_stories"][0]["invest_score"] = "Low"
    artifact["user_stories"][0]["produced_artifacts"] = [
        "execution_evidence_validation_report"
    ]
    artifact["user_stories"][0]["acceptance_criteria"] = [
        "Verify that execution evidence exists and is captured as screenshots or a short video as specified in project rubric.",  # noqa: E501
        "Ensure that evidence demonstrates at least three core parking workflows: vehicle entry, payment processing, and exit.",  # noqa: E501
        "Confirm visual evidence is clear, legible, and demonstrates Python 3 compatibility in the execution environment.",  # noqa: E501
        "Validate that evidence shows no errors or crashes during demonstration.",
    ]
    artifact["user_stories"][0]["decomposition_warning"] = (
        "Artifact 'application_execution_evidence' is owned by "
        "'Updated Source Code Package (refactored prototype for submission)' "
        "which already has a creation story. This story validates that artifact "
        "rather than producing it, creating a hard dependency and fragmentation. "
        "Recommend consolidating: merge this validation into the evidence "
        "creation story (adding these acceptance criteria there) and retire "
        "this separate requirement to improve cohesion and eliminate waste."
    )
    return artifact


def _merge_recommended_complete_artifact(parent_requirement: str) -> dict[str, Any]:
    return {
        "parent_requirement": parent_requirement,
        "user_stories": [
            {
                "story_title": "Validate execution evidence meets submission standards",
                "statement": (
                    "As a Student Developer, I want to validate that my application "
                    "execution evidence meets all project submission requirements, "
                    "so that I can confirm the grader will have clear proof of "
                    "behavioral parity and avoid submission rejection."
                ),
                "acceptance_criteria": [
                    "Verify that execution evidence exists in the required format (screenshots or short video) as specified in the submission guidelines",  # noqa: E501
                    "Ensure evidence demonstrates at least three core parking workflows: vehicle entry with ticket issuance, payment processing, and vehicle exit with barrier control",  # noqa: E501
                    "Confirm visual evidence clearly shows Python 3.x execution environment and command-line or GUI interactions are legible at 1080p resolution",  # noqa: E501
                    "Validate that evidence presentation shows zero runtime errors, crashes, or unhandled exceptions during demonstration",  # noqa: E501
                    "Document validation results in a checklist format indicating pass/fail status for each submission criterion",  # noqa: E501
                ],
                "invest_score": "Low",
                "estimated_effort": "S",
                "produced_artifacts": [
                    "execution_evidence_validation_checklist",
                    "execution_evidence_validation_report",
                ],
                "decomposition_warning": (
                    "This story produces validation artifacts for "
                    "'application_execution_evidence' which is owned by "
                    "'Updated Source Code Package (refactored prototype for submission)' "  # noqa: E501
                    "requirement. This creates cross-requirement dependency and "
                    "fragmented responsibility. The validation activity should be "
                    "consolidated into the evidence creation story within the owning "
                    "requirement to eliminate waste and improve cohesion. This "
                    "requirement may be redundant."
                ),
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }


def _build_client(monkeypatch):  # noqa: ANN001, ANN202
    repo = DummyProductRepository()
    workflow = DummyWorkflowService()
    monkeypatch.setattr(api_module, "product_repo", repo)
    monkeypatch.setattr(api_module, "workflow_service", workflow)
    return TestClient(api_module.app), repo, workflow


def test_story_generate_promotes_reusable_draft_records_request_projection_and_absorbs_feedback(  # noqa: ANN201, D103, E501
    monkeypatch,  # noqa: ANN001
):
    client, repo, workflow = _build_client(monkeypatch)
    product = repo.create("Story Project")
    workflow.states[str(product.product_id)] = {
        "fsm_state": "STORY_INTERVIEW",
        "pending_spec_content": "SPEC",
        "compiled_authority_cached": '{"ok": true}',
        "roadmap_releases": [{"items": ["Requirement A"]}],
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
                                "text": "Please also keep the auth path out of scope.",
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

    request_payload = {
        "parent_requirement": "Requirement A",
        "requirement_context": "assembled",
        "technical_spec": "SPEC",
        "compiled_authority": '{"ok": true}',
        "global_roadmap_context": "",
        "already_generated_milestone_stories": "",
        "artifact_registry": {},
    }

    async def fake_run_story_agent_from_state(  # noqa: ANN202
        state,  # noqa: ANN001
        *,
        project_id,  # noqa: ANN001
        parent_requirement,  # noqa: ANN001
        user_input,  # noqa: ANN001
    ):
        assert project_id == product.product_id
        assert parent_requirement == "Requirement A"
        assert user_input is None
        assert state["roadmap_releases"][0]["items"] == ["Requirement A"]
        runtime = state["interview_runtime"]["story"]["Requirement A"]
        assert runtime["feedback_projection"]["items"] == [
            {
                "feedback_id": "feedback-1",
                "text": "Please also keep the auth path out of scope.",
                "created_at": "2026-03-28T09:59:00Z",
                "status": "unabsorbed",
                "absorbed_by_attempt_id": None,
            },
            {
                "feedback_id": "feedback-2",
                "text": "Please keep this to one milestone.",
                "created_at": runtime["feedback_projection"]["items"][1]["created_at"],
                "status": "unabsorbed",
                "absorbed_by_attempt_id": None,
            },
        ]
        return {
            "success": True,
            "input_context": {"requirement_context": "assembled"},
            "output_artifact": _story_artifact(parent_requirement, "Story A"),
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

    monkeypatch.setattr(
        api_module, "run_story_agent_from_state", fake_run_story_agent_from_state
    )

    response = client.post(
        f"/api/projects/{product.product_id}/story/generate",
        params={"parent_requirement": "Requirement A"},
        json={"user_input": "Please keep this to one milestone."},
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()["data"]
    assert payload["output_artifact"]["user_stories"][0]["story_title"] == "Story A"
    artifact_fingerprint = _story_artifact_fingerprint(
        "Requirement A", payload["output_artifact"]
    )
    assert payload["current_draft"] == {
        "attempt_id": "attempt-1",
        "kind": "complete_draft",
        "is_complete": True,
        "artifact_fingerprint": artifact_fingerprint,
    }
    assert payload["retry"] == {
        "available": False,
        "target_attempt_id": None,
    }
    assert payload["save"] == {
        "available": True,
        "attempt_id": "attempt-1",
        "artifact_fingerprint": artifact_fingerprint,
        "expected_state": "STORY_REVIEW",
    }

    state = workflow.states[str(product.product_id)]
    runtime = state["interview_runtime"]["story"]["Requirement A"]
    assert runtime["request_projection"]["payload"] == request_payload
    assert runtime["request_projection"]["included_feedback_ids"] == [
        "feedback-1",
        "feedback-2",
    ]
    assert runtime["draft_projection"] == {
        "latest_reusable_attempt_id": "attempt-1",
        "kind": "complete_draft",
        "is_complete": True,
        "artifact_fingerprint": artifact_fingerprint,
        "updated_at": runtime["draft_projection"]["updated_at"],
    }
    assert runtime["attempt_history"][0]["included_feedback_ids"] == [
        "feedback-1",
        "feedback-2",
    ]
    assert runtime["attempt_history"][0]["input_context"] == {
        "requirement_context": "assembled"
    }
    assert runtime["attempt_history"][0]["classification"] == "reusable_content_result"
    assert runtime["feedback_projection"]["items"] == [
        {
            "feedback_id": "feedback-1",
            "text": "Please also keep the auth path out of scope.",
            "created_at": "2026-03-28T09:59:00Z",
            "status": "absorbed",
            "absorbed_by_attempt_id": "attempt-1",
        },
        {
            "feedback_id": "feedback-2",
            "text": "Please keep this to one milestone.",
            "created_at": runtime["feedback_projection"]["items"][1]["created_at"],
            "status": "absorbed",
            "absorbed_by_attempt_id": "attempt-1",
        },
    ]
    assert (
        state["story_outputs"]["Requirement A"]["user_stories"][0]["story_title"]
        == "Story A"
    )
    assert len(state["story_attempts"]["Requirement A"]) == 1


def test_story_retry_replays_frozen_request_and_preserves_prior_good_draft_when_retry_fails(  # noqa: ANN201, D103, E501
    monkeypatch,  # noqa: ANN001
):
    client, repo, workflow = _build_client(monkeypatch)
    product = repo.create("Story Project")
    reusable_artifact = _story_artifact("Requirement A", "Saved draft")
    workflow.states[str(product.product_id)] = {
        "fsm_state": "STORY_INTERVIEW",
        "pending_spec_content": "SPEC",
        "compiled_authority_cached": '{"ok": true}',
        "roadmap_releases": [{"items": ["Requirement A"]}],
        "interview_runtime": {
            "story": {
                "Requirement A": {
                    "phase": "story",
                    "subject_key": "Requirement A",
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "created_at": "2026-03-28T10:00:00Z",
                            "trigger": "manual_refine",
                            "request_snapshot_id": "request-1",
                            "draft_basis_attempt_id": None,
                            "included_feedback_ids": [],
                            "classification": "reusable_content_result",
                            "is_reusable": True,
                            "retryable": False,
                            "draft_kind": "complete_draft",
                            "output_artifact": reusable_artifact,
                            "failure_artifact_id": None,
                            "failure_stage": None,
                            "failure_summary": None,
                            "raw_output_preview": None,
                            "has_full_artifact": False,
                        },
                        {
                            "attempt_id": "attempt-2",
                            "created_at": "2026-03-28T10:05:00Z",
                            "trigger": "manual_refine",
                            "request_snapshot_id": "request-2",
                            "draft_basis_attempt_id": "attempt-1",
                            "included_feedback_ids": [],
                            "classification": "nonreusable_provider_failure",
                            "is_reusable": False,
                            "retryable": True,
                            "draft_kind": None,
                            "output_artifact": {
                                "error": "STORY_GENERATION_FAILED",
                                "message": "provider timeout",
                            },
                            "failure_artifact_id": "story-failure-1",
                            "failure_stage": "invocation_exception",
                            "failure_summary": "provider timeout",
                            "raw_output_preview": None,
                            "has_full_artifact": True,
                        },
                    ],
                    "draft_projection": {
                        "latest_reusable_attempt_id": "attempt-1",
                        "kind": "complete_draft",
                        "is_complete": True,
                        "updated_at": "2026-03-28T10:00:00Z",
                    },
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {
                        "request_snapshot_id": "request-2",
                        "payload": {
                            "parent_requirement": "Requirement A",
                            "requirement_context": "frozen",
                            "technical_spec": "SPEC",
                            "compiled_authority": '{"ok": true}',
                            "global_roadmap_context": "",
                            "already_generated_milestone_stories": "",
                            "artifact_registry": {},
                        },
                        "request_hash": "hash-2",
                        "created_at": "2026-03-28T10:05:00Z",
                        "draft_basis_attempt_id": "attempt-1",
                        "included_feedback_ids": [],
                        "context_version": "story-runtime.v1",
                    },
                }
            }
        },
    }

    async def fake_retry(request_payload, *, project_id, parent_requirement):  # noqa: ANN001, ANN202
        assert request_payload["requirement_context"] == "frozen"
        assert project_id == product.product_id
        assert parent_requirement == "Requirement A"
        return {
            "success": False,
            "input_context": request_payload,
            "output_artifact": {
                "error": "STORY_GENERATION_FAILED",
                "message": "provider timeout again",
            },
            "classification": "nonreusable_provider_failure",
            "draft_kind": None,
            "is_reusable": False,
            "is_complete": None,
            "request_payload": request_payload,
            "error": "provider timeout again",
            "failure_artifact_id": "story-failure-2",
            "failure_stage": "invocation_exception",
            "failure_summary": "provider timeout again",
            "raw_output_preview": None,
            "has_full_artifact": True,
        }

    monkeypatch.setattr(api_module, "run_story_agent_request", fake_retry)

    response = client.post(
        f"/api/projects/{product.product_id}/story/retry",
        params={"parent_requirement": "Requirement A"},
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()["data"]
    assert payload["retry"] == {
        "available": True,
        "target_attempt_id": "attempt-3",
    }
    assert payload["save"] == {"available": True}
    assert payload["current_draft"] == {
        "attempt_id": "attempt-1",
        "kind": "complete_draft",
        "is_complete": True,
    }

    runtime = workflow.states[str(product.product_id)]["interview_runtime"]["story"][
        "Requirement A"
    ]
    assert runtime["draft_projection"]["latest_reusable_attempt_id"] == "attempt-1"
    assert runtime["attempt_history"][-1]["attempt_id"] == "attempt-3"
    assert runtime["attempt_history"][-1]["trigger"] == "retry_same_input"
    assert runtime["attempt_history"][-1]["request_snapshot_id"] == "request-2"


def test_story_retry_promotes_reusable_draft_and_absorbs_frozen_feedback_ids(  # noqa: ANN201, D103
    monkeypatch,  # noqa: ANN001
):
    client, repo, workflow = _build_client(monkeypatch)
    product = repo.create("Story Project")
    prior_artifact = _story_artifact(
        "Requirement A", "Earlier draft", is_complete=False
    )
    retry_artifact = _story_artifact("Requirement A", "Recovered draft")
    workflow.states[str(product.product_id)] = {
        "fsm_state": "STORY_INTERVIEW",
        "interview_runtime": {
            "story": {
                "Requirement A": {
                    "phase": "story",
                    "subject_key": "Requirement A",
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "created_at": "2026-03-28T10:00:00Z",
                            "trigger": "auto_transition",
                            "request_snapshot_id": "request-1",
                            "draft_basis_attempt_id": None,
                            "included_feedback_ids": [],
                            "classification": "reusable_content_result",
                            "is_reusable": True,
                            "retryable": False,
                            "draft_kind": "incomplete_draft",
                            "output_artifact": prior_artifact,
                            "failure_artifact_id": None,
                            "failure_stage": None,
                            "failure_summary": None,
                            "raw_output_preview": None,
                            "has_full_artifact": False,
                        },
                        {
                            "attempt_id": "attempt-2",
                            "created_at": "2026-03-28T10:05:00Z",
                            "trigger": "manual_refine",
                            "request_snapshot_id": "request-2",
                            "draft_basis_attempt_id": "attempt-1",
                            "included_feedback_ids": ["feedback-1"],
                            "classification": "nonreusable_provider_failure",
                            "is_reusable": False,
                            "retryable": True,
                            "draft_kind": None,
                            "output_artifact": {
                                "error": "STORY_GENERATION_FAILED",
                                "message": "provider timeout",
                            },
                            "failure_artifact_id": "story-failure-1",
                            "failure_stage": "invocation_exception",
                            "failure_summary": "provider timeout",
                            "raw_output_preview": None,
                            "has_full_artifact": True,
                        },
                    ],
                    "draft_projection": {
                        "latest_reusable_attempt_id": "attempt-1",
                        "kind": "incomplete_draft",
                        "is_complete": False,
                        "updated_at": "2026-03-28T10:00:00Z",
                    },
                    "feedback_projection": {
                        "items": [
                            {
                                "feedback_id": "feedback-1",
                                "text": "Please make the draft complete.",
                                "created_at": "2026-03-28T10:04:00Z",
                                "status": "unabsorbed",
                                "absorbed_by_attempt_id": None,
                            }
                        ],
                        "next_feedback_sequence": 1,
                    },
                    "request_projection": {
                        "request_snapshot_id": "request-2",
                        "payload": {
                            "parent_requirement": "Requirement A",
                            "requirement_context": "frozen",
                            "technical_spec": "SPEC",
                            "compiled_authority": '{"ok": true}',
                            "global_roadmap_context": "",
                            "already_generated_milestone_stories": "",
                            "artifact_registry": {},
                        },
                        "request_hash": "hash-2",
                        "created_at": "2026-03-28T10:05:00Z",
                        "draft_basis_attempt_id": "attempt-1",
                        "included_feedback_ids": ["feedback-1"],
                        "context_version": "story-runtime.v1",
                    },
                }
            }
        },
    }

    async def fake_retry(request_payload, *, project_id, parent_requirement):  # noqa: ANN001, ANN202
        assert request_payload["requirement_context"] == "frozen"
        assert project_id == product.product_id
        assert parent_requirement == "Requirement A"
        return {
            "success": True,
            "input_context": request_payload,
            "output_artifact": retry_artifact,
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

    monkeypatch.setattr(api_module, "run_story_agent_request", fake_retry)

    response = client.post(
        f"/api/projects/{product.product_id}/story/retry",
        params={"parent_requirement": "Requirement A"},
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()["data"]
    artifact_fingerprint = _story_artifact_fingerprint(
        "Requirement A", retry_artifact
    )
    assert payload["current_draft"] == {
        "attempt_id": "attempt-3",
        "kind": "complete_draft",
        "is_complete": True,
        "artifact_fingerprint": artifact_fingerprint,
    }
    assert payload["retry"] == {
        "available": False,
        "target_attempt_id": None,
    }
    assert payload["save"] == {
        "available": True,
        "attempt_id": "attempt-3",
        "artifact_fingerprint": artifact_fingerprint,
        "expected_state": "STORY_REVIEW",
    }

    runtime = workflow.states[str(product.product_id)]["interview_runtime"]["story"][
        "Requirement A"
    ]
    assert runtime["draft_projection"] == {
        "latest_reusable_attempt_id": "attempt-3",
        "kind": "complete_draft",
        "is_complete": True,
        "artifact_fingerprint": artifact_fingerprint,
        "updated_at": runtime["draft_projection"]["updated_at"],
    }
    assert runtime["attempt_history"][-1]["attempt_id"] == "attempt-3"
    assert runtime["attempt_history"][-1]["included_feedback_ids"] == ["feedback-1"]
    assert runtime["feedback_projection"]["items"] == [
        {
            "feedback_id": "feedback-1",
            "text": "Please make the draft complete.",
            "created_at": "2026-03-28T10:04:00Z",
            "status": "absorbed",
            "absorbed_by_attempt_id": "attempt-3",
        }
    ]
    assert (
        workflow.states[str(product.product_id)]["story_outputs"]["Requirement A"][
            "user_stories"
        ][0]["story_title"]
        == "Recovered draft"
    )


def test_story_retry_rejects_when_latest_attempt_is_not_retryable_even_with_frozen_payload(  # noqa: ANN201, D103, E501
    monkeypatch,  # noqa: ANN001
):
    client, repo, workflow = _build_client(monkeypatch)
    product = repo.create("Story Project")
    workflow.states[str(product.product_id)] = {
        "fsm_state": "STORY_INTERVIEW",
        "interview_runtime": {
            "story": {
                "Requirement A": {
                    "phase": "story",
                    "subject_key": "Requirement A",
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "created_at": "2026-03-28T10:00:00Z",
                            "trigger": "auto_transition",
                            "request_snapshot_id": "request-1",
                            "draft_basis_attempt_id": None,
                            "included_feedback_ids": [],
                            "classification": "reusable_content_result",
                            "is_reusable": True,
                            "retryable": False,
                            "draft_kind": "complete_draft",
                            "output_artifact": _story_artifact(
                                "Requirement A", "Saved draft"
                            ),
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
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {
                        "request_snapshot_id": "request-1",
                        "payload": {
                            "parent_requirement": "Requirement A",
                            "requirement_context": "frozen",
                            "technical_spec": "SPEC",
                            "compiled_authority": '{"ok": true}',
                            "global_roadmap_context": "",
                            "already_generated_milestone_stories": "",
                            "artifact_registry": {},
                        },
                        "request_hash": "hash-1",
                        "created_at": "2026-03-28T10:00:00Z",
                        "draft_basis_attempt_id": None,
                        "included_feedback_ids": [],
                        "context_version": "story-runtime.v1",
                    },
                }
            }
        },
    }

    async def fail_if_called(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202, ARG001
        raise AssertionError(  # noqa: TRY003
            "retry should be rejected before invoking the story runtime"  # noqa: EM101
        )

    monkeypatch.setattr(api_module, "run_story_agent_request", fail_if_called)

    response = client.post(
        f"/api/projects/{product.product_id}/story/retry",
        params={"parent_requirement": "Requirement A"},
    )

    assert response.status_code == 409  # noqa: PLR2004
    assert (
        response.json()["detail"]
        == "The latest story attempt is not eligible for retry."
    )


def test_story_save_uses_complete_reusable_draft_projection_not_latest_failed_attempt(  # noqa: ANN201, D103
    monkeypatch,  # noqa: ANN001
):
    client, repo, workflow = _build_client(monkeypatch)
    product = repo.create("Story Project")
    reusable_artifact = _story_artifact("Requirement A", "Saved draft")
    artifact_fingerprint = _story_artifact_fingerprint(
        "Requirement A", reusable_artifact
    )
    reusable_artifact["artifact_fingerprint"] = artifact_fingerprint
    workflow.states[str(product.product_id)] = {
        "fsm_state": "STORY_REVIEW",
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
                            "artifact_fingerprint": artifact_fingerprint,
                            "output_artifact": reusable_artifact,
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
                                "is_complete": False,
                            },
                        },
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

    async def fake_hydrate_context(session_id: str, project_id: int) -> SimpleNamespace:
        assert session_id == str(product.product_id)
        assert project_id == product.product_id
        return SimpleNamespace(
            state=workflow.states[str(product.product_id)],
            session_id=session_id,
        )

    def fake_save_stories_tool(save_input, _context):  # noqa: ANN001, ANN202
        assert save_input.parent_requirement == "Requirement A"
        assert save_input.idempotency_key == "story-save-api-requirement-a"
        assert save_input.stories == reusable_artifact["user_stories"]
        return {"success": True, "saved_count": 1}

    monkeypatch.setattr(api_module, "_hydrate_context", fake_hydrate_context)
    monkeypatch.setattr(api_module, "save_stories_tool", fake_save_stories_tool)

    response = client.post(
        f"/api/projects/{product.product_id}/story/save",
        params={"parent_requirement": "Requirement A"},
        json={
            "attempt_id": "attempt-1",
            "expected_artifact_fingerprint": artifact_fingerprint,
            "expected_state": "STORY_REVIEW",
            "idempotency_key": "story-save-api-requirement-a",
        },
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()
    assert payload["status"] == "success"
    assert (
        workflow.states[str(product.product_id)]["story_saved"]["Requirement A"] is True
    )


def test_story_complete_phase_requires_and_passes_guard_body(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    product = repo.create("Story Project")
    workflow.states[str(product.product_id)] = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [{"items": ["Requirement A"]}],
        "story_saved": {"Requirement A": True},
    }

    missing_body_response = client.post(
        f"/api/projects/{product.product_id}/story/complete_phase"
    )
    assert missing_body_response.status_code == 422  # noqa: PLR2004

    async def fake_complete_story_phase_service(  # noqa: PLR0913
        *,
        expected_state: str,
        idempotency_key: str,
        scope: str | None,
        scope_id: str | None,
        load_state: object,
        save_state: object,
        now_iso: object,
    ) -> dict[str, object]:
        assert expected_state == "STORY_PERSISTENCE"
        assert idempotency_key == "story-complete-api"
        assert scope == "milestone"
        assert scope_id == "milestone_0"
        assert load_state is not None
        assert save_state is not None
        assert now_iso is not None
        return {
            "fsm_state": "SPRINT_SETUP",
            "coverage": {"saved": 1, "merged": 0, "total": 1},
            "idempotency_key": "story-complete-api",
        }

    monkeypatch.setattr(
        api_module,
        "complete_story_phase_service",
        fake_complete_story_phase_service,
    )

    response = client.post(
        f"/api/projects/{product.product_id}/story/complete_phase",
        json={
            "expected_state": "STORY_PERSISTENCE",
            "idempotency_key": "story-complete-api",
            "scope": "milestone",
            "scope_id": "milestone_0",
        },
    )

    assert response.status_code == 200  # noqa: PLR2004
    assert response.json() == {
        "status": "success",
        "data": {
            "fsm_state": "SPRINT_SETUP",
            "coverage": {"saved": 1, "merged": 0, "total": 1},
            "idempotency_key": "story-complete-api",
        },
    }


def test_story_save_returns_409_without_complete_reusable_draft_projection(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    product = repo.create("Story Project")
    incomplete_artifact = _story_artifact(
        "Requirement A", "Incomplete draft", is_complete=False
    )
    artifact_fingerprint = _story_artifact_fingerprint(
        "Requirement A", incomplete_artifact
    )
    incomplete_artifact["artifact_fingerprint"] = artifact_fingerprint
    workflow.states[str(product.product_id)] = {
        "fsm_state": "STORY_REVIEW",
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
                            "artifact_fingerprint": artifact_fingerprint,
                            "output_artifact": incomplete_artifact,
                        }
                    ],
                    "draft_projection": {
                        "latest_reusable_attempt_id": "attempt-1",
                        "kind": "incomplete_draft",
                        "is_complete": False,
                        "artifact_fingerprint": artifact_fingerprint,
                    },
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                }
            }
        },
    }

    response = client.post(
        f"/api/projects/{product.product_id}/story/save",
        params={"parent_requirement": "Requirement A"},
        json={
            "attempt_id": "attempt-1",
            "expected_artifact_fingerprint": artifact_fingerprint,
            "expected_state": "STORY_REVIEW",
            "idempotency_key": "story-save-api-requirement-a",
        },
    )

    assert response.status_code == 409  # noqa: PLR2004
    assert response.json()["detail"] == "No story draft available for 'Requirement A'"


def test_story_save_requires_guard_request_body(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, _workflow = _build_client(monkeypatch)
    product = repo.create("Story Project")

    response = client.post(
        f"/api/projects/{product.product_id}/story/save",
        params={"parent_requirement": "Requirement A"},
    )

    assert response.status_code == 422  # noqa: PLR2004


def test_story_history_returns_projection_attempt_history_and_summary(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    product = repo.create("Story Project")
    workflow.states[str(product.product_id)] = {
        "fsm_state": "STORY_INTERVIEW",
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
                        "payload": {
                            "parent_requirement": "Requirement A",
                        },
                    },
                }
            }
        },
    }

    response = client.get(
        f"/api/projects/{product.product_id}/story/history",
        params={"parent_requirement": "  Requirement A  "},
    )

    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["parent_requirement"] == "Requirement A"
    payload = response.json()["data"]
    assert payload["count"] == 2  # noqa: PLR2004
    assert payload["items"][0]["attempt_id"] == "attempt-1"
    assert payload["items"][1]["attempt_id"] == "attempt-2"
    assert payload["current_draft"] == {
        "attempt_id": "attempt-1",
        "kind": "complete_draft",
        "is_complete": True,
    }
    assert payload["retry"] == {
        "available": True,
        "target_attempt_id": "attempt-2",
    }
    assert payload["save"] == {"available": True}


def test_story_history_surfaces_merge_recommendation_for_incomplete_draft(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    product = repo.create("Story Project")
    merge_artifact = _merge_recommended_artifact("Requirement A")
    workflow.states[str(product.product_id)] = {
        "fsm_state": "STORY_INTERVIEW",
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

    response = client.get(
        f"/api/projects/{product.product_id}/story/history",
        params={"parent_requirement": "Requirement A"},
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()["data"]
    assert payload["save"] == {"available": False}
    assert payload["resolution"]["available"] is True
    assert payload["resolution"]["current"] is None
    assert payload["resolution"]["recommendation"] == {
        "action": "merge_into_requirement",
        "owner_requirement": "Updated Source Code Package (refactored prototype for submission)",  # noqa: E501
        "reason": merge_artifact["user_stories"][0]["decomposition_warning"],
        "acceptance_criteria_to_move": merge_artifact["user_stories"][0][
            "acceptance_criteria"
        ],
    }


def test_story_history_translates_story_phase_error(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    product = repo.create("Story Project")
    workflow.states[str(product.product_id)] = {
        "fsm_state": "STORY_INTERVIEW",
        "interview_runtime": {"story": {}},
    }

    async def fake_get_story_history_service(*, parent_requirement, load_state):  # noqa: ANN001, ANN202, ARG001
        raise api_module.StoryPhaseError(  # noqa: TRY003
            f"Requirement '{parent_requirement}' not found in saved story state.",  # noqa: EM102
            status_code=400,
        )

    monkeypatch.setattr(
        api_module, "get_story_history_service", fake_get_story_history_service
    )

    response = client.get(
        f"/api/projects/{product.product_id}/story/history",
        params={"parent_requirement": "  Requirement A  "},
    )

    assert response.status_code == 400  # noqa: PLR2004
    assert response.json()["detail"] == (
        "Requirement '  Requirement A  ' not found in saved story state."
    )


def test_story_merge_accepts_recommendation_and_marks_requirement_resolved(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    product = repo.create("Story Project")
    merge_artifact = _merge_recommended_artifact("Requirement A")
    workflow.states[str(product.product_id)] = {
        "fsm_state": "STORY_INTERVIEW",
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

    response = client.post(
        f"/api/projects/{product.product_id}/story/merge",
        params={"parent_requirement": "Requirement A"},
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()["data"]
    assert payload["resolution"]["available"] is False
    assert payload["resolution"]["current"] == {
        "status": "merged",
        "owner_requirement": "Updated Source Code Package (refactored prototype for submission)",  # noqa: E501
        "reason": merge_artifact["user_stories"][0]["decomposition_warning"],
        "acceptance_criteria_to_move": merge_artifact["user_stories"][0][
            "acceptance_criteria"
        ],
        "resolved_at": payload["resolution"]["current"]["resolved_at"],
    }

    runtime = workflow.states[str(product.product_id)]["interview_runtime"]["story"][
        "Requirement A"
    ]
    assert runtime["resolution_projection"] == payload["resolution"]["current"]

    pending = client.get(f"/api/projects/{product.product_id}/story/pending")
    assert pending.status_code == 200  # noqa: PLR2004
    requirement = pending.json()["data"]["grouped_items"][0]["requirements"][0]
    assert requirement["status"] == "Merged"


def test_story_history_offers_merge_for_complete_low_invest_duplicate(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    product = repo.create("Story Project")
    merge_artifact = _merge_recommended_complete_artifact("Requirement A")
    workflow.states[str(product.product_id)] = {
        "fsm_state": "STORY_INTERVIEW",
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
                            "output_artifact": merge_artifact,
                        }
                    ],
                    "draft_projection": {
                        "latest_reusable_attempt_id": "attempt-1",
                        "kind": "complete_draft",
                        "is_complete": True,
                    },
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                }
            }
        },
    }

    response = client.get(
        f"/api/projects/{product.product_id}/story/history",
        params={"parent_requirement": "Requirement A"},
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()["data"]
    assert payload["save"] == {"available": False}
    assert payload["resolution"]["available"] is True
    assert payload["resolution"]["recommendation"] == {
        "action": "merge_into_requirement",
        "owner_requirement": "Updated Source Code Package (refactored prototype for submission)",  # noqa: E501
        "reason": merge_artifact["user_stories"][0]["decomposition_warning"],
        "acceptance_criteria_to_move": merge_artifact["user_stories"][0][
            "acceptance_criteria"
        ],
    }


def test_story_generate_allows_fresh_run_after_reset_without_manual_refinement_input(  # noqa: ANN201, D103
    monkeypatch,  # noqa: ANN001
):
    client, repo, workflow = _build_client(monkeypatch)
    product = repo.create("Story Project")
    request_payload = {
        "parent_requirement": "Requirement A",
        "requirement_context": "reset fresh start",
        "technical_spec": "SPEC",
        "compiled_authority": '{"ok": true}',
        "global_roadmap_context": "",
        "already_generated_milestone_stories": "",
        "artifact_registry": {},
    }
    workflow.states[str(product.product_id)] = {
        "fsm_state": "STORY_INTERVIEW",
        "pending_spec_content": "SPEC",
        "compiled_authority_cached": '{"ok": true}',
        "roadmap_releases": [{"items": ["Requirement A"]}],
        "interview_runtime": {
            "story": {
                "Requirement A": {
                    "phase": "story",
                    "subject_key": "Requirement A",
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "created_at": "2026-03-28T10:00:00Z",
                            "trigger": "manual_refine",
                            "request_snapshot_id": "request-1",
                            "draft_basis_attempt_id": None,
                            "included_feedback_ids": [],
                            "classification": "reusable_content_result",
                            "is_reusable": True,
                            "retryable": False,
                            "draft_kind": "complete_draft",
                            "output_artifact": _story_artifact(
                                "Requirement A", "Old draft"
                            ),
                            "failure_artifact_id": None,
                            "failure_stage": None,
                            "failure_summary": None,
                            "raw_output_preview": None,
                            "has_full_artifact": False,
                        },
                        {
                            "attempt_id": "reset-marker-2",
                            "created_at": "2026-03-28T10:05:00Z",
                            "trigger": "reset",
                            "classification": "reset_marker",
                            "is_reusable": False,
                            "retryable": False,
                            "summary": "Stories deleted and state reset by user.",
                            "output_artifact": None,
                        },
                    ],
                    "draft_projection": {},
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                }
            }
        },
    }

    async def fake_run_story_agent_from_state(  # noqa: ANN202
        state,  # noqa: ANN001, ARG001
        *,
        project_id,  # noqa: ANN001
        parent_requirement,  # noqa: ANN001
        user_input,  # noqa: ANN001
    ):
        assert project_id == product.product_id
        assert parent_requirement == "Requirement A"
        assert user_input is None
        return {
            "success": True,
            "input_context": {"requirement_context": "reset fresh start"},
            "output_artifact": _story_artifact(parent_requirement, "New draft"),
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

    monkeypatch.setattr(
        api_module, "run_story_agent_from_state", fake_run_story_agent_from_state
    )

    response = client.post(
        f"/api/projects/{product.product_id}/story/generate",
        params={"parent_requirement": "Requirement A"},
        json={},
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()["data"]
    artifact_fingerprint = _story_artifact_fingerprint(
        "Requirement A", payload["output_artifact"]
    )
    assert payload["current_draft"] == {
        "attempt_id": "attempt-3",
        "kind": "complete_draft",
        "is_complete": True,
        "artifact_fingerprint": artifact_fingerprint,
    }
    runtime = workflow.states[str(product.product_id)]["interview_runtime"]["story"][
        "Requirement A"
    ]
    assert runtime["attempt_history"][-1]["attempt_id"] == "attempt-3"
    assert runtime["attempt_history"][-1]["trigger"] == "auto_transition"
    assert runtime["request_projection"]["included_feedback_ids"] == []


def test_story_pending_returns_pending_after_reset_clears_working_state(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    product = repo.create("Story Project")
    workflow.states[str(product.product_id)] = {
        "fsm_state": "STORY_INTERVIEW",
        "roadmap_releases": [{"items": ["Requirement A"]}],
        "story_attempts": {
            "Requirement A": [
                {
                    "created_at": "2026-03-28T10:00:00Z",
                    "trigger": "manual_refine",
                    "input_context": {},
                    "output_artifact": _story_artifact("Requirement A", "Old draft"),
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
                                "Requirement A", "Old draft"
                            ),
                        },
                        {
                            "attempt_id": "reset-marker-2",
                            "created_at": "2026-03-28T10:05:00Z",
                            "trigger": "reset",
                            "classification": "reset_marker",
                            "is_reusable": False,
                            "retryable": False,
                            "summary": "Stories deleted and state reset by user.",
                            "output_artifact": None,
                        },
                    ],
                    "draft_projection": {},
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                }
            }
        },
    }

    response = client.get(f"/api/projects/{product.product_id}/story/pending")

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()["data"]
    requirement = payload["grouped_items"][0]["requirements"][0]
    assert requirement["requirement"] == "Requirement A"
    assert requirement["status"] == "Pending"
    assert requirement["attempt_count"] == 1
