from __future__ import annotations

import pytest

from services.agent_workbench.post_sprint_triage import (
    TRIAGE_SCHEMA_VERSION,
    PostSprintTriageValidationError,
    build_triage_payload,
    current_triage_for_latest_sprint,
    post_sprint_triage_required,
)


def test_build_triage_payload_normalizes_and_fingerprints_story_impact() -> None:
    payload = build_triage_payload(
        project_id=7,
        sprint_id=13,
        impact="story",
        affected_requirements=["  Quality Gate  ", "Quality Gate"],
        affected_task_ids=[],
        affected_story_ids=[4, "4", "5"],
        affected_backlog_item_ids=[],
        affected_roadmap_item_ids=[],
        affected_layers=[],
        learning_summary="  Spike confirmed the next Story. ",
        decision_reason=" Continue Story work. ",
        idempotency_key="triage-001",
        replace_existing=False,
        recorded_at="2026-06-10T00:00:00Z",
        recorded_by="cli-agent",
    )

    assert payload["schema_version"] == TRIAGE_SCHEMA_VERSION
    assert payload["sprint_id"] == 13
    assert payload["impact"] == "story"
    assert payload["affected_requirements"] == ["Quality Gate"]
    assert payload["affected_story_ids"] == [4, 5]
    assert payload["learning_summary"] == "Spike confirmed the next Story."
    assert payload["decision_reason"] == "Continue Story work."
    assert payload["request_fingerprint"].startswith("sha256:")
    assert payload["triage_fingerprint"].startswith("sha256:")


def test_build_triage_payload_rejects_multiple_without_structured_layers() -> None:
    with pytest.raises(PostSprintTriageValidationError) as excinfo:
        build_triage_payload(
            project_id=7,
            sprint_id=13,
            impact="multiple",
            affected_requirements=[],
            affected_task_ids=[],
            affected_story_ids=[],
            affected_backlog_item_ids=[],
            affected_roadmap_item_ids=[],
            affected_layers=["story"],
            learning_summary="Several things changed.",
            decision_reason="Story and backlog are mentioned in prose.",
            idempotency_key="triage-002",
            replace_existing=False,
            recorded_at="2026-06-10T00:00:00Z",
            recorded_by="cli-agent",
        )

    assert excinfo.value.code == "TRIAGE_IMPACT_FIELDS_INVALID"


def test_build_triage_payload_retains_int_convertible_positive_ids() -> None:
    payload = build_triage_payload(
        project_id=7,
        sprint_id=13,
        impact="story",
        affected_requirements=[],
        affected_task_ids=[],
        affected_story_ids=[3.0, True, 0, -1, "bad", 4],
        affected_backlog_item_ids=[],
        affected_roadmap_item_ids=[],
        affected_layers=[],
        learning_summary="Spike confirmed the next Story.",
        decision_reason="Continue Story work.",
        idempotency_key="triage-003",
        replace_existing=False,
        recorded_at="2026-06-10T00:00:00Z",
        recorded_by="cli-agent",
    )

    assert payload["affected_story_ids"] == [3, 4]


def test_current_triage_for_latest_sprint_requires_matching_sprint_id() -> None:
    state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": 14,
        "post_sprint_triage": {"sprint_id": 13, "impact": "none"},
    }

    assert current_triage_for_latest_sprint(state) is None
    assert post_sprint_triage_required(state) is True


def test_current_triage_for_latest_sprint_requires_latest_completed_sprint_id() -> None:
    state = {
        "fsm_state": "SPRINT_COMPLETE",
        "post_sprint_triage": {"impact": "none"},
    }

    assert current_triage_for_latest_sprint(state) is None


def test_current_triage_for_latest_sprint_accepts_matching_sprint_id() -> None:
    triage = {"sprint_id": 14, "impact": "none"}
    state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": 14,
        "post_sprint_triage": triage,
    }

    assert current_triage_for_latest_sprint(state) == triage
    assert post_sprint_triage_required(state) is False
