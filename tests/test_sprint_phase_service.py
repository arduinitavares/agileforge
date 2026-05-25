"""Tests for sprint phase service."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, Never

import pytest

from orchestrator_agent.agent_tools.sprint_planner_tool.tools import SaveSprintPlanInput
from services.phases.sprint_service import (
    SprintPhaseError,
    close_sprint,
    ensure_sprint_attempts,
    generate_sprint_plan,
    get_saved_sprint_detail,
    get_sprint_close_readiness,
    get_sprint_history,
    list_saved_sprints,
    normalize_sprint_output_artifact,
    record_sprint_attempt,
    reset_sprint_planner,
    reset_sprint_planner_working_set,
    reset_stale_saved_sprint_planner_working_set,
    save_sprint_plan,
    start_saved_sprint,
)

JsonDict = dict[str, Any]


def test_record_sprint_attempt_updates_working_state() -> None:
    """Verify record sprint attempt updates working state."""
    state: JsonDict = {}

    count = record_sprint_attempt(
        state,
        trigger="manual_refine",
        input_context={"stories": [1]},
        output_artifact={"validation_errors": [" Unsupported task_kind 'other'. "]},
        is_complete=False,
        failure_meta={"failure_stage": "planner"},
        created_at="2026-04-04T00:00:00Z",
    )

    assert count == 1
    assert state["sprint_last_input_context"] == {"stories": [1]}
    assert state["sprint_plan_assessment"]["validation_errors"] == [
        state["sprint_plan_assessment"]["validation_errors"][0]
    ]
    assert state["sprint_plan_assessment"]["validation_errors"][0].startswith(
        "Unsupported task_kind 'other'."
    )
    assert state["sprint_attempts"][0]["failure_stage"] == "planner"


def test_reset_stale_saved_sprint_planner_working_set_clears_orphaned_owner() -> None:
    """Verify reset stale saved sprint planner working set clears orphaned owner."""
    state: JsonDict = {
        "sprint_attempts": [{"created_at": "old"}],
        "sprint_last_input_context": {"stories": [1]},
        "sprint_plan_assessment": {"draft": True},
        "sprint_saved_at": "2026-04-01T00:00:00Z",
        "sprint_planner_owner_sprint_id": 9,
    }

    changed = reset_stale_saved_sprint_planner_working_set(
        state,
        current_planned_sprint_id=11,
    )

    assert changed is True
    assert state["sprint_attempts"] == []
    assert state["sprint_plan_assessment"] is None


def test_reset_stale_saved_sprint_planner_working_set_keeps_current_owner() -> None:
    """Verify reset stale saved sprint planner working set keeps current owner."""
    state: JsonDict = {"sprint_planner_owner_sprint_id": 11}

    changed = reset_stale_saved_sprint_planner_working_set(
        state,
        current_planned_sprint_id=11,
    )

    assert changed is False


def test_ensure_sprint_attempts_returns_existing_list() -> None:
    """Verify ensure sprint attempts returns existing list."""
    attempts = [{"created_at": "2026-04-04T00:00:00Z"}]
    state: JsonDict = {"sprint_attempts": attempts}

    assert ensure_sprint_attempts(state) is attempts


def test_reset_sprint_planner_working_set_clears_transient_fields() -> None:
    """Verify reset sprint planner working set clears transient fields."""
    state: JsonDict = {
        "sprint_attempts": [{"created_at": "old"}],
        "sprint_last_input_context": {"stories": [1]},
        "sprint_plan_assessment": {"draft": True},
        "sprint_saved_at": "2026-04-01T00:00:00Z",
        "sprint_planner_owner_sprint_id": 9,
    }

    reset_sprint_planner_working_set(state)

    assert state == {
        "sprint_attempts": [],
        "sprint_last_input_context": None,
        "sprint_plan_assessment": None,
        "sprint_saved_at": None,
        "sprint_planner_owner_sprint_id": None,
    }


def test_normalize_sprint_output_artifact_deduplicates_validation_hints() -> None:
    """Verify normalize sprint output artifact deduplicates validation hints."""
    payload = normalize_sprint_output_artifact(
        {
            "validation_errors": [
                " Unsupported task_kind 'other'. ",
                "Unsupported task_kind 'other'.",
            ]
        }
    )

    assert len(payload["validation_errors"]) == 1
    assert payload["validation_errors"][0].startswith("Unsupported task_kind 'other'.")


def _failure_meta_builder(
    source: JsonDict | None, fallback_summary: str | None = None
) -> JsonDict:
    payload = source or {}
    return {
        "failure_artifact_id": payload.get("failure_artifact_id"),
        "failure_stage": payload.get("failure_stage"),
        "failure_summary": payload.get("failure_summary") or fallback_summary,
        "raw_output_preview": payload.get("raw_output_preview"),
        "has_full_artifact": bool(payload.get("has_full_artifact", False)),
    }


@pytest.mark.asyncio
async def test_generate_sprint_plan_updates_state_and_returns_payload() -> None:
    """Verify generate sprint plan updates state and returns payload."""
    state: JsonDict = {"fsm_state": "SPRINT_SETUP"}
    saved: JsonDict = {}
    captured: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        saved["state"] = dict(updated)

    async def fake_run_sprint_agent(state: object, **kwargs: object) -> JsonDict:
        captured["state"] = state
        captured.update(kwargs)
        return {
            "success": True,
            "input_context": {"available_stories": [], "note": "ready"},
            "output_artifact": {
                "is_complete": True,
                "validation_errors": [" Unsupported task_kind 'other'. "],
            },
            "is_complete": True,
            "error": None,
        }

    payload = await generate_sprint_plan(
        project_id=7,
        load_state=load_state,
        save_state=save_state,
        current_planned_sprint_id=None,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        run_sprint_agent=fake_run_sprint_agent,
        failure_meta_builder=_failure_meta_builder,
        team_velocity_assumption="Medium",
        sprint_duration_days=14,
        max_story_points=13,
        include_task_decomposition=True,
        selected_story_ids=[12],
        user_input="Focus on persistence",
    )

    assert payload["fsm_state"] == "SPRINT_DRAFT"
    assert payload["attempt_count"] == 1
    assert payload["attempt_id"] == "sprint-attempt-1"
    assert str(payload["artifact_fingerprint"]).startswith("sha256:")
    assert payload["trigger"] == "auto_transition"
    assert payload["output_artifact"]["validation_errors"][0].startswith(
        "Unsupported task_kind 'other'."
    )
    assert state["fsm_state"] == "SPRINT_DRAFT"
    assert state["fsm_state_entered_at"] == "2026-04-04T00:00:00Z"
    assert state["sprint_attempts"][0]["trigger"] == "auto_transition"
    assert state["sprint_attempts"][0]["attempt_id"] == "sprint-attempt-1"
    assert (
        state["sprint_attempts"][0]["artifact_fingerprint"]
        == payload["artifact_fingerprint"]
    )
    assert state["sprint_plan_assessment"]["attempt_id"] == "sprint-attempt-1"
    assert (
        state["sprint_plan_assessment"]["artifact_fingerprint"]
        == payload["artifact_fingerprint"]
    )
    assert captured["project_id"] == 7  # noqa: PLR2004
    assert captured["selected_story_ids"] == [12]


@pytest.mark.asyncio
async def test_generate_sprint_plan_stamps_attempt_with_source_fingerprint() -> None:
    """Verify generated sprint attempts keep the candidate source fingerprint."""
    state: JsonDict = {"fsm_state": "SPRINT_SETUP"}
    source_fingerprint = "sha256:" + "b" * 64

    async def load_state() -> JsonDict:
        return state

    async def fake_run_sprint_agent(_state: object, **_kwargs: object) -> JsonDict:
        return {
            "success": True,
            "source_fingerprint": source_fingerprint,
            "input_context": {"available_stories": [{"story_id": 66}]},
            "output_artifact": {
                "sprint_goal": "Ship budget guard",
                "sprint_number": 1,
                "duration_days": 14,
                "selected_stories": [],
                "deselected_stories": [],
                "capacity_analysis": {
                    "velocity_assumption": "Medium",
                    "capacity_band": "1 story",
                    "selected_count": 0,
                    "story_points_used": 0,
                    "max_story_points": 4,
                    "commitment_note": "Fits.",
                    "reasoning": "Small locked scope.",
                },
                "is_complete": True,
            },
            "is_complete": True,
            "error": None,
        }

    await generate_sprint_plan(
        project_id=7,
        load_state=load_state,
        save_state=lambda _state: None,
        current_planned_sprint_id=None,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        run_sprint_agent=fake_run_sprint_agent,
        failure_meta_builder=_failure_meta_builder,
        team_velocity_assumption="Medium",
        sprint_duration_days=14,
        max_story_points=4,
        include_task_decomposition=True,
        selected_story_ids=[66],
        user_input=None,
        load_candidates=lambda: {
            "success": True,
            "count": 1,
            "stories": [{"story_id": 66}],
            "readiness": {"status": "ready"},
        },
    )

    assert state["sprint_candidate_source_fingerprint"] == source_fingerprint
    assert state["sprint_attempts"][0]["source_fingerprint"] == source_fingerprint
    assert state["sprint_plan_assessment"]["source_fingerprint"] == source_fingerprint


@pytest.mark.asyncio
async def test_generate_sprint_plan_uses_shared_fsm_transition_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify generate sprint plan uses shared fsm transition helper."""
    from services.phases import workflow_state  # noqa: PLC0415

    state: JsonDict = {"fsm_state": "SPRINT_SETUP"}
    saved: JsonDict = {}
    called: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        saved["state"] = dict(updated)

    async def fake_run_sprint_agent(state: object, **kwargs: object) -> JsonDict:
        del state
        called["agent_kwargs"] = kwargs
        return {
            "success": True,
            "input_context": {"available_stories": [], "note": "ready"},
            "output_artifact": {"is_complete": False},
            "is_complete": False,
            "error": None,
        }

    def fake_set_sprint_fsm_state(
        state: JsonDict, *, is_complete: bool, now_iso: Callable[[], str]
    ) -> str:
        called["fsm_kwargs"] = {
            "is_complete": is_complete,
            "now_iso": now_iso(),
        }
        next_state = "SPRINT_DRAFT" if is_complete else "SPRINT_SETUP"
        state["fsm_state"] = next_state
        state["fsm_state_entered_at"] = called["fsm_kwargs"]["now_iso"]
        return next_state

    monkeypatch.setattr(
        workflow_state,
        "set_sprint_fsm_state",
        fake_set_sprint_fsm_state,
    )
    payload = await generate_sprint_plan(
        project_id=7,
        load_state=load_state,
        save_state=save_state,
        current_planned_sprint_id=None,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        run_sprint_agent=fake_run_sprint_agent,
        failure_meta_builder=_failure_meta_builder,
        team_velocity_assumption="Medium",
        sprint_duration_days=14,
        max_story_points=None,
        include_task_decomposition=True,
        selected_story_ids=None,
        user_input=None,
    )

    assert payload["fsm_state"] == "SPRINT_SETUP"
    assert payload["attempt_count"] == 1
    assert called["fsm_kwargs"]["is_complete"] is False
    assert state["fsm_state"] == "SPRINT_SETUP"
    assert saved["state"]["fsm_state_entered_at"] == "2026-04-04T00:00:00Z"


@pytest.mark.asyncio
async def test_generate_sprint_plan_rejects_invalid_fsm_state() -> None:
    """Verify generate sprint plan rejects invalid fsm state."""
    state: JsonDict = {"fsm_state": "VISION_REVIEW"}

    async def load_state() -> JsonDict:
        return state

    async def fake_run_sprint_agent(**_kwargs: object) -> Never:
        msg = "runner should not be called"
        raise AssertionError(msg)

    with pytest.raises(SprintPhaseError) as exc_info:
        await generate_sprint_plan(
            project_id=7,
            load_state=load_state,
            save_state=lambda _state: None,
            current_planned_sprint_id=None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            run_sprint_agent=fake_run_sprint_agent,
            failure_meta_builder=_failure_meta_builder,
            team_velocity_assumption="Medium",
            sprint_duration_days=14,
            max_story_points=None,
            include_task_decomposition=True,
            selected_story_ids=None,
            user_input=None,
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert "Invalid phase for sprint generation" in exc_info.value.detail


@pytest.mark.asyncio
async def test_generate_sprint_plan_blocks_unready_candidates_before_runtime() -> None:
    """Verify blocked candidate readiness prevents sprint runtime invocation."""
    state: JsonDict = {"fsm_state": "SPRINT_SETUP"}
    saved: list[JsonDict] = []

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        saved.append(dict(updated))

    async def fake_run_sprint_agent(*_args: object, **_kwargs: object) -> Never:
        msg = "runtime should not run"
        raise AssertionError(msg)

    with pytest.raises(SprintPhaseError) as exc_info:
        await generate_sprint_plan(
            project_id=7,
            load_state=load_state,
            save_state=save_state,
            current_planned_sprint_id=None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            run_sprint_agent=fake_run_sprint_agent,
            failure_meta_builder=_failure_meta_builder,
            team_velocity_assumption="Medium",
            sprint_duration_days=14,
            max_story_points=None,
            include_task_decomposition=True,
            selected_story_ids=None,
            user_input=None,
            load_candidates=lambda: {
                "success": True,
                "count": 1,
                "stories": [
                    {
                        "story_id": 1,
                        "story_title": "Unsized",
                        "priority": 999,
                        "story_points": None,
                    }
                ],
                "readiness": {
                    "status": "blocked",
                    "unsized_count": 1,
                    "default_priority_count": 1,
                    "blocking_codes": [
                        "SPRINT_CANDIDATES_UNSIZED",
                        "SPRINT_CANDIDATES_DEFAULT_PRIORITY",
                    ],
                    "blocking_story_ids": [1],
                },
            },
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert "SPRINT_CANDIDATES_UNSIZED" in exc_info.value.detail
    assert "SPRINT_CANDIDATES_DEFAULT_PRIORITY" in exc_info.value.detail
    assert state == {"fsm_state": "SPRINT_SETUP"}
    assert saved == []


@pytest.mark.asyncio
async def test_failed_sprint_refinement_preserves_previous_complete_draft() -> None:
    """Verify failed refinement records history without replacing reviewed draft."""
    previous_assessment: JsonDict = {
        "attempt_id": "sprint-attempt-1",
        "artifact_fingerprint": "sha256:reviewed",
        "sprint_goal": "Ship live workflow",
        "is_complete": True,
    }
    state: JsonDict = {
        "fsm_state": "SPRINT_DRAFT",
        "sprint_last_input_context": {"available_stories": [{"story_id": 1}]},
        "sprint_plan_assessment": dict(previous_assessment),
        "sprint_attempts": [
            {
                "attempt_id": "sprint-attempt-1",
                "artifact_fingerprint": "sha256:reviewed",
                "is_complete": True,
                "output_artifact": dict(previous_assessment),
            }
        ],
    }

    async def load_state() -> JsonDict:
        return state

    async def fake_run_sprint_agent(_state: object, **_kwargs: object) -> JsonDict:
        return {
            "success": False,
            "input_context": {"available_stories": [{"story_id": 1}]},
            "output_artifact": {
                "error": "SPRINT_GENERATION_FAILED",
                "message": "Provider rate limited",
                "is_complete": False,
            },
            "is_complete": False,
            "error": "Provider rate limited",
            "failure_stage": "invocation_exception",
            "failure_summary": "Provider rate limited",
        }

    payload = await generate_sprint_plan(
        project_id=7,
        load_state=load_state,
        save_state=lambda _state: None,
        current_planned_sprint_id=None,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        run_sprint_agent=fake_run_sprint_agent,
        failure_meta_builder=_failure_meta_builder,
        team_velocity_assumption="Medium",
        sprint_duration_days=14,
        max_story_points=None,
        include_task_decomposition=True,
        selected_story_ids=None,
        user_input="Refine the draft",
        load_candidates=lambda: {
            "success": True,
            "count": 1,
            "stories": [{"story_id": 1}],
            "readiness": {"status": "ready"},
        },
    )

    assert payload["sprint_run_success"] is False
    assert payload["fsm_state"] == "SPRINT_DRAFT"
    assert state["fsm_state"] == "SPRINT_DRAFT"
    assert state["sprint_plan_assessment"] == previous_assessment
    assert len(state["sprint_attempts"]) == 2  # noqa: PLR2004
    assert state["sprint_attempts"][1]["is_complete"] is False
    assert state["sprint_attempts"][1]["failure_stage"] == "invocation_exception"


@pytest.mark.asyncio
async def test_get_sprint_history_normalizes_and_persists_legacy_attempts() -> None:
    """Verify get sprint history normalizes and persists legacy attempts."""
    state: JsonDict = {
        "sprint_attempts": [
            {
                "created_at": "2026-03-29T12:00:00Z",
                "trigger": "manual_refine",
                "input_context": {"available_stories": []},
                "output_artifact": {
                    "error": "SPRINT_GENERATION_FAILED",
                    "validation_errors": [
                        {
                            "loc": [
                                "selected_stories",
                                0,
                                "tasks",
                                0,
                                "task_kind",
                            ],
                            "msg": "Input should be 'analysis', 'design', 'implementation', 'testing', 'documentation', 'refactor' or 'other'",  # noqa: E501
                            "input": "review",
                        }
                    ],
                },
                "is_complete": False,
            }
        ]
    }
    saves: list[JsonDict] = []

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        saves.append(dict(updated))

    payload = await get_sprint_history(
        load_state=load_state,
        save_state=save_state,
        current_planned_sprint_id=None,
    )

    assert payload["count"] == 1
    assert payload["items"][0]["output_artifact"]["validation_errors"] == [
        "Unsupported task_kind 'review'. Use one of: analysis, design, implementation, testing, documentation, refactor."  # noqa: E501
    ]
    assert len(saves) == 1


@pytest.mark.asyncio
async def test_get_sprint_history_restores_complete_draft_after_failed_refine() -> None:
    """Verify history can repair legacy failed-refinement working state."""
    complete_artifact: JsonDict = {
        "sprint_goal": "Ship live workflow",
        "sprint_number": 1,
        "duration_days": 14,
        "selected_stories": [],
        "deselected_stories": [],
        "capacity_analysis": {
            "velocity_assumption": "Medium",
            "capacity_band": "4-5 stories",
            "selected_count": 0,
            "story_points_used": 0,
            "max_story_points": None,
            "commitment_note": "Fits",
            "reasoning": "Fits",
        },
        "is_complete": True,
    }
    state: JsonDict = {
        "fsm_state": "SPRINT_SETUP",
        "sprint_plan_assessment": {
            "error": "SPRINT_GENERATION_FAILED",
            "is_complete": False,
        },
        "sprint_attempts": [
            {
                "attempt_id": "sprint-attempt-1",
                "artifact_fingerprint": "sha256:reviewed",
                "is_complete": True,
                "input_context": {"available_stories": [{"story_id": 1}]},
                "output_artifact": dict(complete_artifact),
            },
            {
                "attempt_id": "sprint-attempt-2",
                "artifact_fingerprint": "sha256:failed",
                "is_complete": False,
                "output_artifact": {"error": "SPRINT_GENERATION_FAILED"},
            },
        ],
    }
    saved_states: list[JsonDict] = []

    async def load_state() -> JsonDict:
        return state

    payload = await get_sprint_history(
        load_state=load_state,
        save_state=lambda updated: saved_states.append(dict(updated)),
        current_planned_sprint_id=None,
    )

    assert payload["count"] == 2  # noqa: PLR2004
    assert state["fsm_state"] == "SPRINT_DRAFT"
    assert state["sprint_plan_assessment"]["attempt_id"] == "sprint-attempt-1"
    assert state["sprint_plan_assessment"]["artifact_fingerprint"] == "sha256:reviewed"
    assert state["sprint_last_input_context"] == {
        "available_stories": [{"story_id": 1}]
    }
    assert saved_states


@pytest.mark.asyncio
async def test_reset_sprint_planner_rejects_existing_planned_sprint() -> None:
    """Verify reset sprint planner rejects existing planned sprint."""

    async def load_state() -> JsonDict:
        return {"sprint_attempts": [{"created_at": "old"}]}

    with pytest.raises(SprintPhaseError) as exc_info:
        await reset_sprint_planner(
            load_state=load_state,
            save_state=lambda _state: None,
            current_planned_sprint_id=99,
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert "A planned sprint already exists." in exc_info.value.detail


def test_list_saved_sprints_returns_serialized_items_and_runtime_summary() -> None:
    """Verify list saved sprints returns serialized items and runtime summary."""
    sprint_a = SimpleNamespace(sprint_id=1)
    sprint_b = SimpleNamespace(sprint_id=2)

    payload = list_saved_sprints(
        load_sprints=lambda: [sprint_a, sprint_b],
        build_runtime_summary=lambda sprints: {
            "planned_sprint_id": sprints[0].sprint_id,
            "active_sprint_id": None,
        },
        serialize_sprint_list_item=lambda sprint, runtime_summary: {
            "id": sprint.sprint_id,
            "planned_sprint_id": runtime_summary["planned_sprint_id"],
        },
    )

    assert payload == {
        "items": [
            {"id": 1, "planned_sprint_id": 1},
            {"id": 2, "planned_sprint_id": 1},
        ],
        "count": 2,
        "runtime_summary": {
            "planned_sprint_id": 1,
            "active_sprint_id": None,
        },
    }


def test_get_saved_sprint_detail_rejects_missing_sprint() -> None:
    """Verify get saved sprint detail rejects missing sprint."""
    with pytest.raises(SprintPhaseError) as exc_info:
        get_saved_sprint_detail(
            load_sprint=lambda: None,
            load_sprints=list,
            build_runtime_summary=lambda _sprints: {},
            serialize_sprint_detail=lambda _sprint, _summary: {},
        )

    assert exc_info.value.status_code == 404  # noqa: PLR2004
    assert exc_info.value.detail == "Sprint not found"


def test_get_saved_sprint_detail_returns_serialized_detail_and_summary() -> None:
    """Verify get saved sprint detail returns serialized detail and summary."""
    sprint = SimpleNamespace(sprint_id=7)
    all_sprints = [sprint, SimpleNamespace(sprint_id=8)]

    payload = get_saved_sprint_detail(
        load_sprint=lambda: sprint,
        load_sprints=lambda: all_sprints,
        build_runtime_summary=lambda sprints: {
            "planned_sprint_id": sprints[0].sprint_id,
            "latest_completed_sprint_id": sprints[-1].sprint_id,
        },
        serialize_sprint_detail=lambda sprint_obj, runtime_summary: {
            "id": sprint_obj.sprint_id,
            "summary": runtime_summary,
        },
    )

    assert payload == {
        "sprint": {
            "id": 7,
            "summary": {
                "planned_sprint_id": 7,
                "latest_completed_sprint_id": 8,
            },
        },
        "runtime_summary": {
            "planned_sprint_id": 7,
            "latest_completed_sprint_id": 8,
        },
    }


def test_get_sprint_close_readiness_returns_guidance_for_completed_sprint() -> None:
    """Verify get sprint close readiness returns guidance for completed sprint."""
    sprint = SimpleNamespace(
        status="Completed",
        completed_at="2026-04-04T12:00:00Z",
    )
    readiness = SimpleNamespace(
        completed_story_count=2,
        open_story_count=0,
        unfinished_story_ids=[],
        stories=[],
    )

    payload = get_sprint_close_readiness(
        sprint_id=7,
        load_sprint=lambda: sprint,
        build_readiness=lambda sprint_obj: readiness,  # noqa: ARG005
        history_fidelity=lambda sprint_obj: "snapshotted",  # noqa: ARG005
        load_close_snapshot=lambda sprint_obj: {"closed_at": "2026-04-04T12:00:00Z"},  # noqa: ARG005
    )

    assert payload["sprint_id"] == 7  # noqa: PLR2004
    assert payload["close_eligible"] is False
    assert payload["ineligible_reason"] == "Sprint is already completed."
    assert payload["history_fidelity"] == "snapshotted"


def test_close_sprint_rejects_non_active_sprint() -> None:
    """Verify close sprint rejects non active sprint."""
    sprint = SimpleNamespace(status="Planned", completed_at=None)

    with pytest.raises(SprintPhaseError) as exc_info:
        close_sprint(
            sprint_id=7,
            completion_notes="Ship it",
            follow_up_notes=None,
            load_sprint=lambda: sprint,
            build_readiness=lambda sprint_obj: SimpleNamespace(  # noqa: ARG005
                completed_story_count=0,
                open_story_count=1,
                unfinished_story_ids=[12],
                stories=[],
            ),
            now_iso=lambda: "2026-04-04T12:00:00Z",
            persist_closed_sprint=lambda snapshot: sprint,  # noqa: ARG005
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert exc_info.value.detail == "Only active sprints can be closed."


def test_close_sprint_returns_completed_snapshot_payload() -> None:
    """Verify close sprint returns completed snapshot payload."""
    story_summary = SimpleNamespace(
        model_dump=lambda mode="json": {"story_id": 12, "story_title": "Done"}  # noqa: ARG005
    )
    readiness = SimpleNamespace(
        completed_story_count=1,
        open_story_count=0,
        unfinished_story_ids=[],
        stories=[story_summary],
    )
    sprint = SimpleNamespace(status="Active", completed_at=None)
    closed_sprint = SimpleNamespace(completed_at="2026-04-04T12:34:56Z")

    payload = close_sprint(
        sprint_id=7,
        completion_notes="Closed after review.",
        follow_up_notes="Carry remaining backlog forward manually.",
        load_sprint=lambda: sprint,
        build_readiness=lambda sprint_obj: readiness,  # noqa: ARG005
        now_iso=lambda: "2026-04-04T12:00:00Z",
        persist_closed_sprint=lambda snapshot: closed_sprint,  # noqa: ARG005
    )

    assert payload["current_status"] == "Completed"
    assert payload["close_eligible"] is False
    assert payload["ineligible_reason"] == "Sprint is already completed."
    assert payload["history_fidelity"] == "snapshotted"
    assert payload["close_snapshot"] == {
        "closed_at": "2026-04-04T12:00:00Z",
        "completion_notes": "Closed after review.",
        "follow_up_notes": "Carry remaining backlog forward manually.",
        "completed_story_count": 1,
        "open_story_count": 0,
        "unfinished_story_ids": [],
        "stories": [{"story_id": 12, "story_title": "Done"}],
    }
    assert payload["completed_at"] == "2026-04-04T12:34:56Z"


@pytest.mark.asyncio
async def test_save_sprint_plan_sanitizes_assessment_and_updates_state() -> None:
    """Verify save sprint plan sanitizes assessment and updates state."""
    state: JsonDict = {
        "fsm_state": "SPRINT_DRAFT",
        "sprint_plan_assessment": {
            "sprint_goal": "Persist safely",
            "sprint_number": 1,
            "duration_days": 14,
            "selected_stories": [],
            "deselected_stories": [],
            "capacity_analysis": {
                "velocity_assumption": "Medium",
                "capacity_band": "4-5 stories",
                "selected_count": 0,
                "story_points_used": 0,
                "max_story_points": 13,
                "commitment_note": "Fits",
                "reasoning": "Fits",
            },
            "is_complete": True,
        },
    }
    hydrated_context = SimpleNamespace(
        state={"preserved": True},
        session_id="7",
    )
    saved_states: list[JsonDict] = []
    captured: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    def save_state(updated: JsonDict) -> None:
        saved_states.append(dict(updated))

    async def hydrate_context(session_id: str, project_id: int) -> object:
        assert session_id == "7"
        assert project_id == 7  # noqa: PLR2004
        return hydrated_context

    def build_tool_context(context: object) -> object:
        return context

    def save_plan_tool(
        input_data: SaveSprintPlanInput,
        tool_context: object,
    ) -> JsonDict:
        captured["input_data"] = input_data
        captured["tool_context"] = tool_context
        return {"success": True, "sprint_id": 9}

    payload = await save_sprint_plan(
        project_id=7,
        load_state=load_state,
        save_state=save_state,
        current_planned_sprint_id=None,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        hydrate_context=hydrate_context,
        build_tool_context=build_tool_context,
        save_plan_tool=save_plan_tool,
        team_name="Team Alpha",
        sprint_start_date="2026-03-15",
    )

    assert payload["fsm_state"] == "SPRINT_PERSISTENCE"
    assert payload["save_result"]["sprint_id"] == 9  # noqa: PLR2004
    assert state["fsm_state"] == "SPRINT_PERSISTENCE"
    assert state["sprint_saved_at"] == "2026-04-04T00:00:00Z"
    assert state["sprint_planner_owner_sprint_id"] == 9  # noqa: PLR2004
    assert captured["input_data"].team_name == "Team Alpha"
    assert captured["input_data"].sprint_start_date == "2026-03-15"
    assert captured["tool_context"].state["sprint_plan"]["duration_days"] == 14  # noqa: PLR2004
    assert "is_complete" not in captured["tool_context"].state["sprint_plan"]


@pytest.mark.asyncio
async def test_save_sprint_plan_enforces_reviewed_attempt_guards() -> None:
    """Verify guarded sprint save requires the reviewed draft fingerprint."""
    state: JsonDict = {
        "fsm_state": "SPRINT_DRAFT",
        "sprint_attempts": [
            {
                "attempt_id": "sprint-attempt-1",
                "artifact_fingerprint": "sha256:reviewed",
            }
        ],
        "sprint_plan_assessment": {
            "attempt_id": "sprint-attempt-1",
            "artifact_fingerprint": "sha256:reviewed",
            "sprint_goal": "Persist safely",
            "sprint_number": 1,
            "duration_days": 14,
            "selected_stories": [],
            "deselected_stories": [],
            "capacity_analysis": {
                "velocity_assumption": "Medium",
                "capacity_band": "4-5 stories",
                "selected_count": 0,
                "story_points_used": 0,
                "max_story_points": 13,
                "commitment_note": "Fits",
                "reasoning": "Fits",
            },
            "is_complete": True,
        },
    }

    async def load_state() -> JsonDict:
        return state

    async def hydrate_context(_session_id: str, _project_id: int) -> object:
        return SimpleNamespace(state=state, session_id="7")

    with pytest.raises(SprintPhaseError) as exc_info:
        await save_sprint_plan(
            project_id=7,
            load_state=load_state,
            save_state=lambda _state: None,
            current_planned_sprint_id=None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_plan_tool=lambda _input_data, _tool_context: {
                "success": True,
                "sprint_id": 9,
            },
            team_name="Team Alpha",
            sprint_start_date="2026-03-15",
            attempt_id="sprint-attempt-1",
            expected_artifact_fingerprint="sha256:stale",
            expected_state="SPRINT_DRAFT",
            idempotency_key="save-sprint-7-001",
        )

    assert "save guard mismatch" in exc_info.value.detail


@pytest.mark.asyncio
async def test_save_sprint_plan_blocks_when_latest_attempt_failed() -> None:
    """Verify guarded sprint save requires latest attempt to be complete."""
    state: JsonDict = {
        "fsm_state": "SPRINT_DRAFT",
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
            },
        ],
        "sprint_plan_assessment": {
            "attempt_id": "sprint-attempt-4",
            "artifact_fingerprint": "sha256:reviewed",
            "sprint_goal": "Persist safely",
            "sprint_number": 1,
            "duration_days": 14,
            "selected_stories": [],
            "deselected_stories": [],
            "capacity_analysis": {
                "velocity_assumption": "Medium",
                "capacity_band": "4-5 stories",
                "selected_count": 0,
                "story_points_used": 0,
                "max_story_points": 13,
                "commitment_note": "Fits",
                "reasoning": "Fits",
            },
            "is_complete": True,
        },
    }

    async def load_state() -> JsonDict:
        return state

    async def hydrate_context(_session_id: str, _project_id: int) -> object:
        return SimpleNamespace(state=state, session_id="7")

    def save_plan_tool(_input_data: object, _tool_context: object) -> Never:
        msg = "save tool should not be called for stale sprint attempt"
        raise AssertionError(msg)

    with pytest.raises(SprintPhaseError) as exc_info:
        await save_sprint_plan(
            project_id=7,
            load_state=load_state,
            save_state=lambda _state: None,
            current_planned_sprint_id=None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_plan_tool=save_plan_tool,
            team_name="Team Alpha",
            sprint_start_date="2026-03-15",
            attempt_id="sprint-attempt-4",
            expected_artifact_fingerprint="sha256:reviewed",
            expected_state="SPRINT_DRAFT",
            idempotency_key="save-sprint-7-004",
        )

    assert "latest complete Sprint attempt" in exc_info.value.detail


@pytest.mark.asyncio
async def test_save_sprint_plan_blocks_when_candidate_source_changed() -> None:
    """Verify guarded sprint save rejects stale story/dependency source."""
    state: JsonDict = {
        "fsm_state": "SPRINT_DRAFT",
        "sprint_candidate_source_fingerprint": "sha256:old_source",
        "sprint_attempts": [
            {
                "attempt_id": "sprint-attempt-4",
                "artifact_fingerprint": "sha256:reviewed",
                "is_complete": True,
                "source_fingerprint": "sha256:old_source",
            }
        ],
        "sprint_plan_assessment": {
            "attempt_id": "sprint-attempt-4",
            "artifact_fingerprint": "sha256:reviewed",
            "source_fingerprint": "sha256:old_source",
            "sprint_goal": "Persist safely",
            "sprint_number": 1,
            "duration_days": 14,
            "selected_stories": [],
            "deselected_stories": [],
            "capacity_analysis": {
                "velocity_assumption": "Medium",
                "capacity_band": "4-5 stories",
                "selected_count": 0,
                "story_points_used": 0,
                "max_story_points": 13,
                "commitment_note": "Fits",
                "reasoning": "Fits",
            },
            "is_complete": True,
        },
    }

    async def load_state() -> JsonDict:
        return state

    async def hydrate_context(_session_id: str, _project_id: int) -> object:
        return SimpleNamespace(state=state, session_id="7")

    def save_plan_tool(_input_data: object, _tool_context: object) -> Never:
        msg = "save tool should not be called for changed sprint source"
        raise AssertionError(msg)

    with pytest.raises(SprintPhaseError) as exc_info:
        await save_sprint_plan(
            project_id=7,
            load_state=load_state,
            save_state=lambda _state: None,
            current_planned_sprint_id=None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_plan_tool=save_plan_tool,
            team_name="Team Alpha",
            sprint_start_date="2026-03-15",
            attempt_id="sprint-attempt-4",
            expected_artifact_fingerprint="sha256:reviewed",
            expected_state="SPRINT_DRAFT",
            idempotency_key="save-sprint-7-004",
            load_candidates=lambda: {"source_fingerprint": "sha256:current_source"},
        )

    assert "Story/dependency source changed" in exc_info.value.detail


@pytest.mark.asyncio
async def test_save_sprint_plan_replays_matching_idempotency_key() -> None:
    """Verify repeat guarded save returns the recorded payload without mutation."""
    replay_payload: JsonDict = {
        "fsm_state": "SPRINT_PERSISTENCE",
        "save_result": {"success": True, "sprint_id": 9},
        "attempt_id": "sprint-attempt-1",
        "artifact_fingerprint": "sha256:reviewed",
        "idempotency_key": "save-sprint-7-001",
    }
    state: JsonDict = {
        "fsm_state": "SPRINT_PERSISTENCE",
        "sprint_save_idempotency_keys": {
            "save-sprint-7-001": replay_payload,
        },
    }

    async def load_state() -> JsonDict:
        return state

    async def hydrate_context(_session_id: str, _project_id: int) -> object:
        return SimpleNamespace(state=state, session_id="7")

    def save_plan_tool(_input_data: object, _tool_context: object) -> Never:
        msg = "save tool should not be called for idempotency replay"
        raise AssertionError(msg)

    payload = await save_sprint_plan(
        project_id=7,
        load_state=load_state,
        save_state=lambda _state: None,
        current_planned_sprint_id=9,
        now_iso=lambda: "2026-04-04T00:00:00Z",
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_plan_tool=save_plan_tool,
        team_name="Team Alpha",
        sprint_start_date="2026-03-15",
        attempt_id="sprint-attempt-1",
        expected_artifact_fingerprint="sha256:reviewed",
        expected_state="SPRINT_DRAFT",
        idempotency_key="save-sprint-7-001",
    )

    assert payload == replay_payload


@pytest.mark.asyncio
async def test_save_sprint_plan_maps_open_sprint_conflict_to_phase_error() -> None:
    """Verify save sprint plan maps open sprint conflict to phase error."""
    state: JsonDict = {
        "fsm_state": "SPRINT_DRAFT",
        "sprint_plan_assessment": {
            "sprint_goal": "Persist safely",
            "sprint_number": 1,
            "duration_days": 14,
            "selected_stories": [],
            "deselected_stories": [],
            "capacity_analysis": {
                "velocity_assumption": "Medium",
                "capacity_band": "4-5 stories",
                "selected_count": 0,
                "story_points_used": 0,
                "max_story_points": 13,
                "commitment_note": "Fits",
                "reasoning": "Fits",
            },
            "is_complete": True,
        },
    }

    async def load_state() -> JsonDict:
        return state

    async def hydrate_context(_session_id: str, _project_id: int) -> object:
        return SimpleNamespace(state={}, session_id="7")

    def save_plan_tool(
        _input_data: SaveSprintPlanInput,
        _tool_context: object,
    ) -> JsonDict:
        return {
            "success": False,
            "error_code": "STORY_ALREADY_IN_OPEN_SPRINT",
            "error": "Stories already assigned to active or planned sprints: [12]",
        }

    with pytest.raises(SprintPhaseError) as exc_info:
        await save_sprint_plan(
            project_id=7,
            load_state=load_state,
            save_state=lambda _state: None,
            current_planned_sprint_id=None,
            now_iso=lambda: "2026-04-04T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_plan_tool=save_plan_tool,
            team_name="Team Alpha",
            sprint_start_date="2026-03-15",
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert exc_info.value.detail == (
        "Stories already assigned to active or planned sprints: [12]"
    )


def test_start_saved_sprint_rejects_other_active_sprint() -> None:
    """Verify start saved sprint rejects other active sprint."""
    sprint = SimpleNamespace(status="PLANNED", started_at=None)

    with pytest.raises(SprintPhaseError) as exc_info:
        start_saved_sprint(
            project_id=7,
            sprint_id=3,
            load_sprint=lambda: sprint,
            load_other_active=object,
            persist_started_sprint=lambda: sprint,
            build_runtime_summary=dict,
            serialize_sprint=lambda _sprint, _summary: {"id": 3},
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert exc_info.value.detail == "Another sprint is already active for this project."


def test_start_saved_sprint_returns_existing_active_detail_without_persisting() -> None:
    """Verify start saved sprint returns existing active detail without persisting."""
    sprint = SimpleNamespace(status="ACTIVE", started_at="2026-04-01T09:00:00Z")
    called = {"persist": False}

    payload = start_saved_sprint(
        project_id=7,
        sprint_id=3,
        load_sprint=lambda: sprint,
        load_other_active=lambda: None,
        persist_started_sprint=lambda: called.__setitem__("persist", True),
        build_runtime_summary=lambda: {"active_sprint_id": 3},
        serialize_sprint=lambda _sprint, summary: {"id": 3, "summary": summary},
    )

    assert payload["sprint"]["id"] == 3  # noqa: PLR2004
    assert payload["sprint"]["summary"] == {"active_sprint_id": 3}
    assert called["persist"] is False


def test_start_saved_sprint_rejects_completed_status_variants() -> None:
    """Verify start saved sprint rejects completed status variants."""
    sprint = SimpleNamespace(status="Completed", started_at="2026-04-01T09:00:00Z")

    with pytest.raises(SprintPhaseError) as exc_info:
        start_saved_sprint(
            project_id=7,
            sprint_id=3,
            load_sprint=lambda: sprint,
            load_other_active=lambda: None,
            persist_started_sprint=lambda: sprint,
            build_runtime_summary=dict,
            serialize_sprint=lambda _sprint, _summary: {"id": 3},
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert exc_info.value.detail == "Completed sprints cannot be restarted."


def test_start_saved_sprint_persists_planned_sprint_and_returns_detail() -> None:
    """Verify start saved sprint persists planned sprint and returns detail."""
    sprint = SimpleNamespace(status="PLANNED", started_at=None)
    started = SimpleNamespace(status="ACTIVE", started_at="2026-04-01T09:00:00Z")

    payload = start_saved_sprint(
        project_id=7,
        sprint_id=3,
        load_sprint=lambda: sprint,
        load_other_active=lambda: None,
        persist_started_sprint=lambda: started,
        build_runtime_summary=lambda: {"active_sprint_id": 3},
        serialize_sprint=lambda sprint_obj, summary: {
            "status": sprint_obj.status,
            "summary": summary,
        },
    )

    assert payload["sprint"]["status"] == "ACTIVE"
    assert payload["sprint"]["summary"] == {"active_sprint_id": 3}
