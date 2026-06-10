from __future__ import annotations

import pytest

from services.agent_workbench.post_sprint_triage import (
    TRIAGE_SCHEMA_VERSION,
    PostSprintTriageValidationError,
    build_triage_payload,
    current_triage_for_latest_sprint,
    post_sprint_triage_required,
)


def _story_triage_kwargs(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "project_id": 7,
        "sprint_id": 13,
        "impact": "story",
        "affected_requirements": [],
        "affected_task_ids": [],
        "affected_story_ids": [4],
        "affected_backlog_item_ids": [],
        "affected_roadmap_item_ids": [],
        "affected_layers": [],
        "learning_summary": "Spike confirmed the next Story.",
        "decision_reason": "Continue Story work.",
        "idempotency_key": "triage-001",
        "replace_existing": False,
        "recorded_at": "2026-06-10T00:00:00Z",
        "recorded_by": "cli-agent",
    }
    values.update(overrides)
    return values


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


def test_build_triage_payload_rejects_null_impact_as_validation_error() -> None:
    with pytest.raises(PostSprintTriageValidationError) as excinfo:
        build_triage_payload(**_story_triage_kwargs(impact=None))

    assert excinfo.value.code == "TRIAGE_IMPACT_FIELDS_INVALID"


@pytest.mark.parametrize("field_name", ["learning_summary", "decision_reason"])
def test_build_triage_payload_rejects_null_required_text(
    field_name: str,
) -> None:
    with pytest.raises(PostSprintTriageValidationError) as excinfo:
        build_triage_payload(**_story_triage_kwargs(**{field_name: None}))

    assert excinfo.value.code == "TRIAGE_REQUIRED_FIELD_MISSING"


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("idempotency_key", None),
        ("recorded_at", "  "),
        ("recorded_by", ""),
    ],
)
def test_build_triage_payload_rejects_blank_metadata_text(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(PostSprintTriageValidationError) as excinfo:
        build_triage_payload(**_story_triage_kwargs(**{field_name: value}))

    assert excinfo.value.code == "TRIAGE_REQUIRED_FIELD_MISSING"


def test_build_triage_payload_rejects_layers_for_single_impact() -> None:
    with pytest.raises(PostSprintTriageValidationError) as excinfo:
        build_triage_payload(
            **_story_triage_kwargs(affected_layers=["backlog"]),
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


def test_build_triage_payload_skips_fractional_numeric_ids() -> None:
    payload = build_triage_payload(
        **_story_triage_kwargs(affected_story_ids=[3.5, 4]),
    )

    assert payload["affected_story_ids"] == [4]


def test_build_triage_payload_normalizes_top_level_ids_before_fingerprinting() -> None:
    int_payload = build_triage_payload(
        **_story_triage_kwargs(
            idempotency_key="triage-top-level-ids",
        ),
    )
    string_payload = build_triage_payload(
        **_story_triage_kwargs(
            project_id="7",
            sprint_id="13",
            idempotency_key="triage-top-level-ids",
        ),
    )

    assert string_payload["project_id"] == 7
    assert string_payload["sprint_id"] == 13
    assert string_payload["request_fingerprint"] == int_payload["request_fingerprint"]


def test_build_triage_payload_fingerprints_normalized_request_only() -> None:
    baseline = build_triage_payload(
        **_story_triage_kwargs(
            project_id=7,
            sprint_id=13,
            affected_story_ids=[4, "4"],
            learning_summary=" Spike confirmed the next Story. ",
            decision_reason=" Continue Story work. ",
            idempotency_key="triage-fingerprint",
            recorded_at="2026-06-10T00:00:00Z",
            recorded_by="cli-agent",
        ),
    )
    equivalent = build_triage_payload(
        **_story_triage_kwargs(
            project_id="7",
            sprint_id="13",
            affected_story_ids=["4"],
            learning_summary="Spike confirmed the next Story.",
            decision_reason="Continue Story work.",
            idempotency_key="triage-fingerprint",
            recorded_at="2026-06-11T00:00:00Z",
            recorded_by="api-agent",
        ),
    )
    material_change = build_triage_payload(
        **_story_triage_kwargs(
            idempotency_key="triage-fingerprint",
            learning_summary="Spike confirmed follow-up Story work.",
        ),
    )

    assert equivalent["request_fingerprint"] == baseline["request_fingerprint"]
    assert material_change["triage_fingerprint"] != baseline["triage_fingerprint"]


def test_current_triage_for_latest_sprint_requires_matching_sprint_id() -> None:
    state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": 14,
        "post_sprint_triage": {"sprint_id": 13, "impact": "none"},
    }

    assert current_triage_for_latest_sprint(state) is None
    assert post_sprint_triage_required(state) is True


def test_current_triage_for_latest_sprint_rejects_invalid_impact_fields() -> None:
    state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": 13,
        "post_sprint_triage": {
            "schema_version": TRIAGE_SCHEMA_VERSION,
            "sprint_id": 13,
            "impact": "story",
            "affected_requirements": [],
            "affected_task_ids": [],
            "affected_story_ids": [],
            "affected_backlog_item_ids": [],
            "affected_roadmap_item_ids": [],
            "affected_layers": [],
            "decision_reason": "Story impact was selected without structured links.",
            "request_fingerprint": "sha256:request",
            "triage_fingerprint": "sha256:triage",
        },
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
    triage = build_triage_payload(
        project_id=7,
        sprint_id=14,
        impact="none",
        affected_requirements=[],
        affected_task_ids=[],
        affected_story_ids=[],
        affected_backlog_item_ids=[],
        affected_roadmap_item_ids=[],
        affected_layers=[],
        learning_summary="No follow-up required.",
        decision_reason="Sprint learning is already accounted for.",
        idempotency_key="triage-current",
        replace_existing=False,
        recorded_at="2026-06-10T00:00:00Z",
        recorded_by="cli-agent",
    )
    state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": 14,
        "post_sprint_triage": triage,
    }

    assert current_triage_for_latest_sprint(state) == triage
    assert post_sprint_triage_required(state) is False


def test_current_triage_for_latest_sprint_rejects_malformed_matching_state() -> None:
    state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": 13,
        "post_sprint_triage": {"sprint_id": 13},
    }

    assert current_triage_for_latest_sprint(state) is None
    assert post_sprint_triage_required(state) is True
