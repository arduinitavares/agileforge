"""Regression tests for sprint input normalization and runtime wiring."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Never, cast

import pytest

from orchestrator_agent.fsm import deterministic_tool_adapters as adapters
from services import sprint_input, sprint_runtime
from utils import adk_runner

if TYPE_CHECKING:
    from collections.abc import Callable

    from google.adk.tools import ToolContext


class MockToolContext:
    """Minimal ToolContext stub for unit tests."""

    def __init__(self, state: object) -> None:
        """Initialize the test helper."""
        self.state = state


def _capacity_analysis_payload(
    *,
    capacity_points: int,
    capacity_source: str = "user_override",
    capacity_basis: str | None = None,
    selected_count: int,
    story_points_used: int,
) -> dict[str, object]:
    basis = capacity_basis or f"{capacity_points} points"
    return {
        "capacity_points": capacity_points,
        "capacity_source": capacity_source,
        "capacity_basis": basis,
        "selected_count": selected_count,
        "story_points_used": story_points_used,
        "remaining_capacity_points": max(capacity_points - story_points_used, 0),
        "commitment_note": "Does this scope feel achievable?",
        "reasoning": "The selected work fits the point capacity.",
    }


def _old_velocity_input_key() -> str:
    return "team" + "_" + "velocity" + "_" + "assumption"


def _old_sprint_duration_input_key() -> str:
    return "sprint" + "_" + "duration" + "_" + "days"


def _valid_sprint_output(*, max_story_points: int | None = 13) -> str:
    capacity_points = 9 if max_story_points is None else max_story_points
    capacity_source = "project_metrics" if max_story_points is None else "user_override"
    return json.dumps(
        {
            "sprint_goal": "Deliver onboarding-ready login flow",
            "sprint_number": 1,
            "selected_stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "tasks": [
                        {
                            "description": "Create schema",
                            "task_kind": "design",
                            "checklist_items": [
                                "Define the event schema shape",
                                "Document the persistence boundary",
                            ],
                            "artifact_targets": ["event schema"],
                            "workstream_tags": ["persistence"],
                            "relevant_invariant_ids": ["INV-12"],
                        },
                        {
                            "description": "Write tests",
                            "task_kind": "testing",
                            "checklist_items": [
                                "Cover the persistence behavior in tests",
                            ],
                            "artifact_targets": ["unit tests"],
                            "workstream_tags": ["testing"],
                            "relevant_invariant_ids": [],
                        },
                    ],
                    "reason_for_selection": "Supports the sprint goal.",
                }
            ],
            "deselected_stories": [],
            "capacity_analysis": _capacity_analysis_payload(
                capacity_points=capacity_points,
                capacity_source=capacity_source,
                selected_count=1,
                story_points_used=3,
            ),
        }
    )


def _sprint_output_for_story_ids(
    story_ids: list[int],
    *,
    selected_count: int | None = None,
    story_points_used: int | None = None,
    max_story_points: int | None = 10,
    deselected_stories: list[dict[str, object]] | None = None,
) -> str:
    capacity_points = 10 if max_story_points is None else max_story_points
    used_points = (
        2 * len(story_ids) if story_points_used is None else story_points_used
    )
    return json.dumps(
        {
            "sprint_goal": "Deliver locked sprint scope",
            "sprint_number": 1,
            "selected_stories": [
                {
                    "story_id": story_id,
                    "story_title": f"Story {story_id}",
                    "tasks": [],
                    "reason_for_selection": "Supports the locked sprint scope.",
                }
                for story_id in story_ids
            ],
            "deselected_stories": deselected_stories or [],
            "capacity_analysis": _capacity_analysis_payload(
                capacity_points=capacity_points,
                selected_count=(
                    len(story_ids) if selected_count is None else selected_count
                ),
                story_points_used=used_points,
            ),
        }
    )


def _governance_spec_update_story(story_id: int = 41) -> dict[str, Any]:
    return {
        "story_id": story_id,
        "story_title": "Update product spec and authority",
        "story_description": (
            "Update specs/spec.json, run authority review, and accept the "
            "compiled authority."
        ),
        "priority": 101,
        "story_points": 1,
    }


def _implementation_story(story_id: int = 42) -> dict[str, Any]:
    return {
        "story_id": story_id,
        "story_title": "Implement approved dashboard filter",
        "story_description": "Add the persisted filter UI.",
        "priority": 102,
        "story_points": 2,
    }


def _candidate_fetcher(stories: list[dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    def fake_fetch_sprint_candidates(*, product_id: int) -> dict[str, Any]:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": len(stories),
            "stories": stories,
        }

    return fake_fetch_sprint_candidates


def _governance_spec_update_warning(story_ids: list[int]) -> list[dict[str, Any]]:
    return [
        {
            "code": "SPRINT_GOVERNANCE_SPEC_UPDATE",
            "message": (
                "Some sprint candidates require governance/spec/authority "
                "workflow before sprint execution."
            ),
            "story_ids": story_ids,
        }
    ]


def test_prepare_sprint_input_context_rejects_invalid_selected_story_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify prepare sprint input context rejects invalid selected story ids."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 11,
                    "story_title": "Attestation Gate UI",
                    "priority": 1,
                    "story_points": 5,
                }
            ],
        }

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )

    prepared = sprint_input.prepare_sprint_input_context(
        product_id=7,
        user_context="Focus on persistence",
        max_story_points=13,
        include_task_decomposition=True,
        selected_story_ids=[999],
        capacity_points=13,
        capacity_source="user_override",
        capacity_basis="13 points",
    )

    assert prepared["success"] is False
    assert prepared["error_code"] == "SPRINT_SELECTION_INVALID"
    assert prepared["invalid_selected_ids"] == [999]


def test_prepare_sprint_input_context_rejects_duplicate_selected_story_ids() -> None:
    """Verify duplicate manual selections reach selector validation."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> dict[str, object]:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 66,
                    "story_title": "Budget parameter",
                    "priority": 101,
                    "story_points": 1,
                }
            ],
        }

    prepared = sprint_input.prepare_sprint_input_context(
        product_id=7,
        user_context=None,
        max_story_points=4,
        include_task_decomposition=True,
        selected_story_ids=[66, 66],
        fetch_candidates=fake_fetch_sprint_candidates,
        capacity_points=4,
        capacity_source="user_override",
        capacity_basis="4 points",
    )

    assert prepared["success"] is False
    assert prepared["error_code"] == "SPRINT_SELECTION_DUPLICATE"
    assert prepared["selection_details"]["duplicate_selected_ids"] == [66]


def test_prepare_sprint_input_context_honors_excluded_story_ids() -> None:
    """Verify explicit exclusions are applied before the Sprint cohort is locked."""
    excluded_story_id = 276

    def fake_fetch_sprint_candidates(*, product_id: int) -> dict[str, object]:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 4,
            "stories": [
                {
                    "story_id": 275,
                    "story_title": "Core Offline Recommendation Engine",
                    "priority": 901,
                    "story_points": 3,
                },
                {
                    "story_id": 276,
                    "story_title": "Integrate Safe Action Envelope Gate",
                    "priority": 902,
                    "story_points": 2,
                },
                {
                    "story_id": 277,
                    "story_title": "Integrate Historical Support Gates",
                    "priority": 903,
                    "story_points": 2,
                },
                {
                    "story_id": 265,
                    "story_title": "Define Abstention Reason Catalog",
                    "priority": 2501,
                    "story_points": 2,
                },
            ],
        }

    prepared = sprint_input.prepare_sprint_input_context(
        product_id=7,
        user_context="Do not select story 276.",
        max_story_points=7,
        include_task_decomposition=True,
        selected_story_ids=None,
        excluded_story_ids=[excluded_story_id],
        fetch_candidates=fake_fetch_sprint_candidates,
        capacity_points=7,
        capacity_source="user_override",
        capacity_basis="7 points",
    )

    assert prepared["success"] is True
    assert prepared["selected_story_ids"] == [275, 277, 265]
    assert [
        story["story_id"] for story in prepared["input_context"]["available_stories"]
    ] == [275, 277, 265]
    assert prepared["selection_policy"]["requested_excluded_story_ids"] == [
        excluded_story_id
    ]
    assert prepared["selection_policy"]["explicitly_excluded_story_ids"] == [
        excluded_story_id
    ]
    assert excluded_story_id in prepared["selection_policy"]["excluded_story_ids"]


def test_prepare_sprint_input_context_rejects_selected_excluded_conflict() -> None:
    """Verify a story cannot be both manually selected and excluded."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> dict[str, object]:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 275,
                    "story_title": "Core Offline Recommendation Engine",
                    "priority": 901,
                    "story_points": 3,
                },
                {
                    "story_id": 276,
                    "story_title": "Integrate Safe Action Envelope Gate",
                    "priority": 902,
                    "story_points": 2,
                },
            ],
        }

    prepared = sprint_input.prepare_sprint_input_context(
        product_id=7,
        user_context=None,
        max_story_points=7,
        include_task_decomposition=True,
        selected_story_ids=[275, 276],
        excluded_story_ids=[276],
        fetch_candidates=fake_fetch_sprint_candidates,
        capacity_points=7,
        capacity_source="user_override",
        capacity_basis="7 points",
    )

    assert prepared["success"] is False
    assert prepared["error_code"] == "SPRINT_SELECTION_CONFLICT"
    assert prepared["conflicting_story_ids"] == [276]


def test_prepare_sprint_input_context_auto_selects_locked_priority_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify sprint input auto-selects a locked priority prefix."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 3,
            "stories": [
                {
                    "story_id": 66,
                    "story_title": "Budget",
                    "priority": 101,
                    "story_points": 1,
                },
                {
                    "story_id": 85,
                    "story_title": "Live workflow",
                    "priority": 102,
                    "story_points": 3,
                },
                {
                    "story_id": 67,
                    "story_title": "Capture",
                    "priority": 201,
                    "story_points": 3,
                },
            ],
        }

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )

    prepared = sprint_input.prepare_sprint_input_context(
        product_id=7,
        user_context=None,
        max_story_points=4,
        include_task_decomposition=True,
        selected_story_ids=None,
        capacity_points=4,
        capacity_source="user_override",
        capacity_basis="4 points",
    )

    assert prepared["success"] is True
    assert prepared["selected_story_ids"] == [66, 85]
    assert prepared["selection_policy"]["mode"] == "auto"
    assert prepared["selection_policy"]["capacity_points"] == 4  # noqa: PLR2004
    assert prepared["selection_policy"]["capacity_source"] == "user_override"
    assert prepared["selection_policy"]["capacity_basis"] == "4 points"
    assert prepared["selection_policy"]["max_story_points"] == 4  # noqa: PLR2004
    assert prepared["input_context"]["capacity_points"] == 4  # noqa: PLR2004
    assert prepared["input_context"]["capacity_source"] == "user_override"
    assert prepared["input_context"]["capacity_basis"] == "4 points"
    assert _old_velocity_input_key() not in prepared["input_context"]
    assert _old_sprint_duration_input_key() not in prepared["input_context"]
    assert "max_story_points" not in prepared["input_context"]
    assert [
        story["story_id"] for story in prepared["input_context"]["available_stories"]
    ] == [66, 85]
    assert prepared["input_context"]["available_stories"][0]["parent_group"] == 1
    assert prepared["input_context"]["available_stories"][0]["group_slot"] == 1


def test_load_sprint_candidates_warns_about_governance_spec_update_stories() -> None:
    """Governance/spec update stories should be advisory candidate warnings."""
    result = sprint_input.load_sprint_candidates(
        7,
        fetch_candidates=_candidate_fetcher(
            [_governance_spec_update_story(), _implementation_story()]
        ),
    )

    assert result["success"] is True
    assert result["readiness"]["status"] == "ready"
    assert result["governance_spec_update_story_ids"] == [41]
    assert result["warnings"] == _governance_spec_update_warning([41])


def test_prepare_sprint_input_context_auto_skips_governance_spec_update_stories() -> (
    None
):
    """Auto-selection should skip governance/spec update stories and warn."""
    prepared = sprint_input.prepare_sprint_input_context(
        product_id=7,
        user_context=None,
        max_story_points=4,
        include_task_decomposition=True,
        selected_story_ids=None,
        fetch_candidates=_candidate_fetcher(
            [_governance_spec_update_story(), _implementation_story()]
        ),
        capacity_points=4,
        capacity_source="user_override",
        capacity_basis="4 points",
    )

    assert prepared["success"] is True
    assert prepared["selected_story_ids"] == [42]
    assert [
        story["story_id"] for story in prepared["input_context"]["available_stories"]
    ] == [42]
    assert prepared["selection_policy"]["warnings"] == (
        _governance_spec_update_warning([41])
    )


def test_prepare_sprint_input_context_rejects_all_governance_spec_update_stories() -> (
    None
):
    """Auto-selection should fail when every candidate is governance/spec update."""
    prepared = sprint_input.prepare_sprint_input_context(
        product_id=7,
        user_context=None,
        max_story_points=4,
        include_task_decomposition=True,
        selected_story_ids=None,
        fetch_candidates=_candidate_fetcher([_governance_spec_update_story()]),
        capacity_points=4,
        capacity_source="user_override",
        capacity_basis="4 points",
    )

    assert prepared["success"] is False
    assert prepared["error_code"] == "SPRINT_SELECTION_GOVERNANCE_SPEC_UPDATE"
    assert prepared["governance_spec_update_story_ids"] == [41]
    assert "scope-extension" in prepared["message"]


def test_prepare_sprint_input_rejects_selected_governance_spec_update_story() -> None:
    """Manual selected story IDs should not include governance/spec update work."""
    prepared = sprint_input.prepare_sprint_input_context(
        product_id=7,
        user_context=None,
        max_story_points=4,
        include_task_decomposition=True,
        selected_story_ids=[41],
        fetch_candidates=_candidate_fetcher(
            [_governance_spec_update_story(), _implementation_story()]
        ),
        capacity_points=4,
        capacity_source="user_override",
        capacity_basis="4 points",
    )

    assert prepared["success"] is False
    assert prepared["error_code"] == "SPRINT_SELECTION_GOVERNANCE_SPEC_UPDATE"
    assert prepared["governance_spec_update_story_ids"] == [41]


def test_prepare_sprint_input_reports_dependency_selection_policy() -> None:
    """Verify sprint input reports dependency-aware selection diagnostics."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> dict[str, object]:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 85,
                    "story_title": "Live workflow",
                    "priority": 101,
                    "story_points": 3,
                    "blocked_by_story_ids": [66],
                    "prerequisite_story_ids": [66],
                    "dependency_status": "blocked",
                },
                {
                    "story_id": 66,
                    "story_title": "Budget parameter",
                    "priority": 201,
                    "story_points": 1,
                    "blocked_by_story_ids": [],
                    "prerequisite_story_ids": [],
                    "dependency_status": "ready",
                },
            ],
        }

    result = sprint_input.prepare_sprint_input_context(
        product_id=7,
        user_context=None,
        max_story_points=4,
        include_task_decomposition=True,
        selected_story_ids=None,
        fetch_candidates=fake_fetch_sprint_candidates,
        capacity_points=4,
        capacity_source="user_override",
        capacity_basis="4 points",
    )

    assert result["selected_story_ids"] == [66, 85]
    assert result["selection_policy"]["dependency_closed"] is True
    assert result["selection_policy"]["dependency_promoted_story_ids"] == [66]
    assert result["selection_policy"]["dependency_edges"] == [
        {"dependent_story_id": 85, "prerequisite_story_id": 66}
    ]


def test_prepare_sprint_input_context_rejects_missing_capacity_points() -> None:
    """Verify sprint input never silently defaults missing capacity."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> dict[str, object]:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 66,
                    "story_title": "Budget parameter",
                    "priority": 101,
                    "story_points": 1,
                }
            ],
        }

    prepare_context = cast("Any", sprint_input.prepare_sprint_input_context)
    result = prepare_context(
        product_id=7,
        user_context=None,
        max_story_points=None,
        include_task_decomposition=True,
        selected_story_ids=None,
        fetch_candidates=fake_fetch_sprint_candidates,
        capacity_source="project_metrics",
        capacity_basis="historical project metrics",
    )

    assert result["success"] is False
    assert result["error_code"] == "SPRINT_CAPACITY_INVALID"


def test_prepare_sprint_input_context_source_fingerprint_changes_with_story_text() -> (
    None
):
    """Sprint source fingerprint changes when candidate content changes."""

    def fetch_with_title(
        title: str,
    ) -> Callable[..., dict[str, object]]:
        def fake_fetch_sprint_candidates(*, product_id: int) -> dict[str, object]:
            assert product_id == 7  # noqa: PLR2004
            return {
                "success": True,
                "count": 1,
                "stories": [
                    {
                        "story_id": 71,
                        "story_title": title,
                        "priority": 302,
                        "story_points": 2,
                        "acceptance_criteria": "- Verify explicit --budget only.",
                        "blocked_by_story_ids": [],
                        "prerequisite_story_ids": [],
                    }
                ],
                "readiness": {"status": "ready", "blocking_codes": []},
                "excluded_counts": {
                    "non_refined": 0,
                    "superseded": 0,
                    "open_sprint": 0,
                },
                "message": "Found 1 sprint candidate.",
            }

        return fake_fetch_sprint_candidates

    first = sprint_input.prepare_sprint_input_context(
        product_id=7,
        user_context=None,
        max_story_points=None,
        include_task_decomposition=True,
        selected_story_ids=None,
        fetch_candidates=fetch_with_title("Validate Squad Budget Compliance"),
        capacity_points=9,
        capacity_source="project_metrics",
        capacity_basis="9 points",
    )
    changed = sprint_input.prepare_sprint_input_context(
        product_id=7,
        user_context=None,
        max_story_points=None,
        include_task_decomposition=True,
        selected_story_ids=None,
        fetch_candidates=fetch_with_title(
            "Require Explicit Budget and Validate Squad Compliance"
        ),
        capacity_points=9,
        capacity_source="project_metrics",
        capacity_basis="9 points",
    )

    assert first["success"] is True
    assert changed["success"] is True
    assert first["source_fingerprint"].startswith("sha256:")
    assert changed["source_fingerprint"].startswith("sha256:")
    assert first["source_fingerprint"] != changed["source_fingerprint"]


def test_prepare_sprint_payload_preserves_source_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prepared Sprint payload preserves candidate source fingerprint."""
    source_fingerprint = "sha256:" + "a" * 64

    def fake_prepare_sprint_input_context(
        *,
        product_id: int,
        **options: object,
    ) -> dict[str, object]:
        assert product_id == 7  # noqa: PLR2004
        assert options["capacity_points"] == 9  # noqa: PLR2004
        assert options["capacity_source"] == "project_metrics"
        assert options["capacity_basis"] == "9 points"
        assert _old_velocity_input_key() not in options
        assert _old_sprint_duration_input_key() not in options
        return {
            "success": True,
            "source_fingerprint": source_fingerprint,
            "selection_policy": {
                "mode": "auto",
                "source_fingerprint": source_fingerprint,
            },
            "input_context": {
                "available_stories": [
                    {
                        "story_id": 66,
                        "story_title": "Budget",
                        "story_description": "Validate the sprint budget.",
                        "priority": 101,
                        "story_points": 1,
                        "acceptance_criteria_items": [],
                        "evaluated_invariant_ids": [],
                        "story_compliance_boundary_summaries": [],
                        "prerequisite_story_ids": [],
                        "blocked_by_story_ids": [],
                        "dependency_status": "ready",
                    }
                ],
                "capacity_points": 9,
                "capacity_source": "project_metrics",
                "capacity_basis": "9 points",
                "include_task_decomposition": True,
            },
        }

    monkeypatch.setattr(
        sprint_runtime,
        "prepare_sprint_input_context",
        fake_prepare_sprint_input_context,
    )

    prepared = sprint_runtime._prepare_sprint_payload(
        project_id=7,
        options={
            "capacity_points": 9,
            "capacity_source": "project_metrics",
            "capacity_basis": "9 points",
            "include_task_decomposition": True,
            "max_story_points": 4,
            "selected_story_ids": None,
            "user_input": None,
        },
    )

    assert isinstance(prepared, sprint_runtime._PreparedSprintPayload)
    assert prepared.source_fingerprint == source_fingerprint
    assert prepared.selection_policy["source_fingerprint"] == source_fingerprint


def test_prepare_sprint_payload_builds_capacity_input_without_legacy_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prepared Sprint payload uses capacity args without velocity/duration input."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> dict[str, object]:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 66,
                    "story_title": "Budget parameter",
                    "priority": 101,
                    "story_points": 4,
                    "evaluated_invariant_ids": [],
                }
            ],
            "readiness": {"status": "ready", "blocking_codes": []},
        }

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )

    prepared = sprint_runtime._prepare_sprint_payload(
        project_id=7,
        options={
            "capacity_points": 4,
            "capacity_source": "user_override",
            "capacity_basis": "4 points",
            "include_task_decomposition": True,
            "max_story_points": 4,
            "selected_story_ids": None,
            "user_input": None,
        },
    )

    assert isinstance(prepared, sprint_runtime._PreparedSprintPayload)
    payload = prepared.payload.model_dump()
    assert payload["capacity_points"] == 4  # noqa: PLR2004
    assert payload["capacity_source"] == "user_override"
    assert payload["capacity_basis"] == "4 points"
    assert _old_velocity_input_key() not in payload
    assert _old_sprint_duration_input_key() not in payload
    assert "max_story_points" not in payload
    assert prepared.selection_policy["max_story_points"] == 4  # noqa: PLR2004


def test_prepare_sprint_payload_preserves_policy_on_input_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify invalid runtime input still reports selection diagnostics."""
    selection_policy = {
        "mode": "auto",
        "selected_story_ids": [66, 85],
        "dependency_closed": True,
        "dependency_edges": [{"dependent_story_id": 85, "prerequisite_story_id": 66}],
        "dependency_promoted_story_ids": [66],
    }
    artifact_context: dict[str, object] = {}

    def fake_prepare_sprint_input_context(
        *,
        product_id: int,
        **options: object,
    ) -> dict[str, object]:
        assert product_id == 7  # noqa: PLR2004
        assert options["capacity_points"] == 9  # noqa: PLR2004
        assert options["capacity_source"] == "project_metrics"
        assert options["capacity_basis"] == "9 points"
        assert _old_velocity_input_key() not in options
        assert _old_sprint_duration_input_key() not in options
        return {
            "success": True,
            "input_context": {},
            "selection_policy": selection_policy,
        }

    def fake_write_failure_artifact(**kwargs: object) -> dict[str, object]:
        artifact_context.update(cast("dict[str, object]", kwargs["context"]))
        return {
            "metadata": {
                "failure_artifact_id": "sprint-failure-1",
                "failure_stage": "input_validation",
                "failure_summary": "Sprint input validation failed",
                "raw_output_preview": None,
                "has_full_artifact": False,
            }
        }

    monkeypatch.setattr(
        sprint_runtime,
        "prepare_sprint_input_context",
        fake_prepare_sprint_input_context,
    )
    monkeypatch.setattr(
        sprint_runtime,
        "write_failure_artifact",
        fake_write_failure_artifact,
    )

    result = cast(
        "dict[str, Any]",
        sprint_runtime._prepare_sprint_payload(
            project_id=7,
            options={
                "capacity_points": 9,
                "capacity_source": "project_metrics",
                "capacity_basis": "9 points",
                "include_task_decomposition": True,
                "max_story_points": 4,
                "selected_story_ids": None,
                "user_input": None,
            },
        ),
    )

    assert isinstance(result, dict)
    assert result["success"] is False
    assert result["failure_stage"] == "input_validation"
    assert result["selection_policy"] == selection_policy
    assert artifact_context["selection_policy"] == selection_policy


def test_prepare_sprint_input_context_returns_selection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify sprint input returns structured selection errors."""
    blocking_story_id = 66

    def fake_fetch_sprint_candidates(*, product_id: int) -> dict[str, Any]:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": blocking_story_id,
                    "story_title": "Budget",
                    "priority": 101,
                    "story_points": 5,
                },
            ],
        }

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )

    prepared = sprint_input.prepare_sprint_input_context(
        product_id=7,
        user_context=None,
        max_story_points=3,
        include_task_decomposition=True,
        selected_story_ids=None,
        capacity_points=3,
        capacity_source="user_override",
        capacity_basis="3 points",
    )

    assert prepared["success"] is False
    assert prepared["error_code"] == "SPRINT_SELECTION_CAPACITY_BLOCKED"
    assert prepared["selection_details"]["blocking_story_id"] == blocking_story_id


def test_load_sprint_candidates_preserves_readiness_from_fetcher() -> None:
    """Verify candidate normalization keeps readiness diagnostics."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> dict[str, Any]:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 11,
                    "story_title": "Unsized story",
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
                "blocking_story_ids": [11],
            },
        }

    result = sprint_input.load_sprint_candidates(
        7,
        fetch_candidates=fake_fetch_sprint_candidates,
    )

    assert result["success"] is True
    assert result["readiness"] == {
        "status": "blocked",
        "unsized_count": 1,
        "default_priority_count": 1,
        "blocking_codes": [
            "SPRINT_CANDIDATES_UNSIZED",
            "SPRINT_CANDIDATES_DEFAULT_PRIORITY",
        ],
        "blocking_story_ids": [11],
    }


def test_prepare_sprint_input_filters_to_story_completion_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint input should only include stories from the completed Story scope."""
    expected_scoped_count = 2
    first_scoped_story_id = 11
    second_scoped_story_id = 12

    def fake_fetch_sprint_candidates(*, product_id: int) -> dict[str, Any]:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 3,
            "stories": [
                {
                    "story_id": 11,
                    "story_title": "Login UI",
                    "priority": 1,
                    "story_points": 2,
                    "source_requirement": "enable login",
                },
                {
                    "story_id": 12,
                    "story_title": "Reset email",
                    "priority": 2,
                    "story_points": 2,
                    "source_requirement": "reset password",
                },
                {
                    "story_id": 13,
                    "story_title": "Invite teammates",
                    "priority": 999,
                    "story_points": None,
                    "source_requirement": "invite teammates",
                },
            ],
            "readiness": {
                "status": "blocked",
                "unsized_count": 1,
                "default_priority_count": 1,
                "blocking_codes": [
                    "SPRINT_CANDIDATES_UNSIZED",
                    "SPRINT_CANDIDATES_DEFAULT_PRIORITY",
                ],
                "blocking_story_ids": [13],
            },
        }

    monkeypatch.setattr(
        sprint_input,
        "fetch_sprint_candidates",
        fake_fetch_sprint_candidates,
    )

    prepared = sprint_input.prepare_sprint_input_context(
        product_id=7,
        user_context=None,
        max_story_points=10,
        include_task_decomposition=True,
        selected_story_ids=None,
        story_completion_scope={
            "scope": "milestone",
            "scope_id": "milestone_0",
            "requirements": ["Enable Login", "Reset Password"],
        },
        capacity_points=10,
        capacity_source="user_override",
        capacity_basis="10 points",
    )

    assert prepared["success"] is True
    assert prepared["candidate_result"]["count"] == expected_scoped_count
    assert prepared["candidate_result"]["readiness"] == {
        "status": "ready",
        "unsized_count": 0,
        "default_priority_count": 0,
        "blocking_codes": [],
        "blocking_story_ids": [],
    }
    assert prepared["candidate_result"]["excluded_counts"] == {
        "story_completion_scope": 1
    }
    assert prepared["input_context"]["available_stories"][0]["story_id"] == (
        first_scoped_story_id
    )
    assert prepared["input_context"]["available_stories"][1]["story_id"] == (
        second_scoped_story_id
    )


def test_selected_story_scope_message_hides_internal_scope_hash() -> None:
    """Selected scope messages should be user-facing and keep hashes in metadata."""
    result = sprint_input.apply_story_completion_scope_to_candidate_result(
        {
            "success": True,
            "count": 3,
            "stories": [
                {
                    "story_id": 11,
                    "story_title": "Login UI",
                    "source_requirement": "enable login",
                },
                {
                    "story_id": 12,
                    "story_title": "Reset email",
                    "source_requirement": "reset password",
                },
                {
                    "story_id": 13,
                    "story_title": "Invite teammates",
                    "source_requirement": "invite teammates",
                },
            ],
            "excluded_counts": {"non_refined": 11},
            "readiness": {"status": "ready", "blocking_codes": []},
        },
        {
            "scope": "selection",
            "scope_id": "selection:sha256:87ff8c3304815fa16f844b512c92528d77bf65e",
            "requirements": ["Enable Login", "Reset Password"],
        },
    )

    assert result["message"] == (
        "Found 2 sprint candidates for selected-story scope. "
        "Excluded: 11 non-refined requirements."
    )
    assert "sha256" not in result["message"]
    assert result["story_completion_scope"]["scope_id"].startswith(
        "selection:sha256:"
    )


def test_prepare_sprint_input_preserves_dependency_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint planner input keeps dependency metadata from candidates."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 11,
                    "story_title": "Capture market data",
                    "priority": 101,
                    "story_points": 2,
                    "prerequisite_story_ids": [],
                    "blocked_by_story_ids": [],
                    "dependency_status": "ready",
                },
                {
                    "story_id": 12,
                    "story_title": "Generate recommendation",
                    "priority": 102,
                    "story_points": 3,
                    "prerequisite_story_ids": [11],
                    "blocked_by_story_ids": [11],
                    "dependency_status": "blocked",
                },
            ],
            "readiness": {"status": "ready", "blocking_codes": []},
        }

    monkeypatch.setattr(
        sprint_input,
        "fetch_sprint_candidates",
        fake_fetch_sprint_candidates,
    )

    result = sprint_input.prepare_sprint_input_context(
        product_id=7,
        user_context=None,
        max_story_points=None,
        include_task_decomposition=True,
        capacity_points=9,
        capacity_source="project_metrics",
        capacity_basis="9 points",
    )

    assert result["success"] is True
    stories = result["input_context"]["available_stories"]
    assert stories[1]["prerequisite_story_ids"] == [11]
    assert stories[1]["blocked_by_story_ids"] == [11]
    assert stories[1]["dependency_status"] == "blocked"


@pytest.mark.asyncio
async def test_run_sprint_agent_filters_candidates_from_story_completion_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint generation should use the Story completion scope from workflow state."""
    captured: dict[str, Any] = {}
    first_scoped_story_id = 11
    second_scoped_story_id = 12

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 3,
            "stories": [
                {
                    "story_id": 11,
                    "story_title": "Login UI",
                    "priority": 1,
                    "story_points": 2,
                    "source_requirement": "enable login",
                },
                {
                    "story_id": 12,
                    "story_title": "Reset email",
                    "priority": 2,
                    "story_points": 2,
                    "source_requirement": "reset password",
                },
                {
                    "story_id": 13,
                    "story_title": "Invite teammates",
                    "priority": 999,
                    "story_points": None,
                    "source_requirement": "invite teammates",
                },
            ],
            "readiness": {
                "status": "blocked",
                "unsized_count": 1,
                "default_priority_count": 1,
                "blocking_codes": [
                    "SPRINT_CANDIDATES_UNSIZED",
                    "SPRINT_CANDIDATES_DEFAULT_PRIORITY",
                ],
                "blocking_story_ids": [13],
            },
        }

    async def fake_invoke(payload: sprint_runtime.SprintPlannerInput) -> str:
        captured["payload"] = payload.model_dump()
        return _sprint_output_for_story_ids([11, 12], max_story_points=10)

    monkeypatch.setattr(
        sprint_input,
        "fetch_sprint_candidates",
        fake_fetch_sprint_candidates,
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {
            "story_completion_scope": {
                "scope": "milestone",
                "scope_id": "milestone_0",
                "requirements": ["Enable Login", "Reset Password"],
            }
        },
        project_id=7,
        max_story_points=10,
        include_task_decomposition=False,
        selected_story_ids=None,
        user_input=None,
        capacity_points=10,
        capacity_source="user_override",
        capacity_basis="10 points",
    )

    assert result["success"] is True
    assert [
        story["story_id"] for story in captured["payload"]["available_stories"]
    ] == [first_scoped_story_id, second_scoped_story_id]
    assert captured["payload"]["capacity_points"] == 10  # noqa: PLR2004
    assert captured["payload"]["capacity_source"] == "user_override"
    assert captured["payload"]["capacity_basis"] == "10 points"
    assert _old_velocity_input_key() not in captured["payload"]
    assert _old_sprint_duration_input_key() not in captured["payload"]
    assert "max_story_points" not in captured["payload"]
    assert result["input_context"]["available_stories"][0]["story_id"] == (
        first_scoped_story_id
    )
    assert result["input_context"]["available_stories"][1]["story_id"] == (
        second_scoped_story_id
    )


@pytest.mark.asyncio
async def test_runtime_and_adapter_build_matching_sprint_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime and adapter build matching sprint input."""
    runtime_capture = {}
    adapter_capture = {}

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 11,
                    "story_title": "Attestation Gate UI",
                    "priority": 1,
                    "story_points": 5,
                    "evaluated_invariant_ids": [],
                },
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 2,
                    "story_points": 3,
                    "evaluated_invariant_ids": ["INV-12"],
                    "source_requirement": "REQ-44",
                },
            ],
        }

    async def fake_invoke(payload: sprint_runtime.SprintPlannerInput) -> object:
        runtime_capture["payload"] = payload.model_dump()
        return _valid_sprint_output()

    async def fake_run_async(*, args: object, tool_context: object) -> object:
        adapter_capture["args"] = args
        adapter_capture["tool_context"] = tool_context
        return {"sprint_goal": "goal", "selected_stories": [], "capacity_analysis": {}}

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(
        adapters, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)
    monkeypatch.setattr(adapters._SPRINT_PLANNER_TOOL, "run_async", fake_run_async)

    runtime_result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        max_story_points=13,
        include_task_decomposition=False,
        selected_story_ids=[12],
        user_input="Focus on persistence",
        capacity_points=13,
        capacity_source="user_override",
        capacity_basis="13 points",
    )
    context = MockToolContext({"active_project": {"product_id": 7}})
    _ = await adapters.sprint_planner_tool(
        capacity_points=13,
        capacity_source="user_override",
        capacity_basis="13 points",
        user_context="Focus on persistence",
        max_story_points=13,
        include_task_decomposition=False,
        selected_story_ids=[12],
        tool_context=cast("ToolContext", context),
    )

    assert runtime_result["success"] is True
    assert runtime_result["output_artifact"]["is_complete"] is True
    adapter_args = adapter_capture["args"]
    assert adapter_args["available_stories"][0]["parent_group"] is None
    assert adapter_args["available_stories"][0]["group_slot"] is None
    assert runtime_capture["payload"] == adapter_args
    assert runtime_capture["payload"] == {
        "available_stories": [
            {
                "story_id": 12,
                "story_title": "Event Delta Persistence",
                "story_description": "",
                "acceptance_criteria_items": [],
                "persona": None,
                "source_requirement": "REQ-44",
                "priority": 2,
                "story_points": 3,
                "evaluated_invariant_ids": ["INV-12"],
                "story_compliance_boundary_summaries": [],
                "parent_group": None,
                "group_slot": None,
                "prerequisite_story_ids": [],
                "blocked_by_story_ids": [],
                "dependency_status": "ready",
            }
        ],
        "capacity_points": 13,
        "capacity_source": "user_override",
        "capacity_basis": "13 points",
        "user_context": "Focus on persistence",
        "include_task_decomposition": False,
    }


@pytest.mark.asyncio
async def test_runtime_rejects_output_that_changes_locked_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime rejects output that changes locked selected story ids."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 3,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 1,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
                {
                    "story_id": 13,
                    "story_title": "Event Delta Replay",
                    "priority": 2,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
                {
                    "story_id": 99,
                    "story_title": "Out of Scope Story",
                    "priority": 3,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return _sprint_output_for_story_ids([12, 99])

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        max_story_points=10,
        include_task_decomposition=False,
        selected_story_ids=[12, 13],
        user_input=None,
        capacity_points=10,
        capacity_source="user_override",
        capacity_basis="10 points",
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert result["validation_errors"] == [
        "selected stories do not match locked Sprint selection: "
        "expected [12, 13], actual [12, 99]"
    ]


@pytest.mark.asyncio
async def test_runtime_rejects_output_that_drops_locked_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime rejects output that drops locked selected story ids."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 1,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
                {
                    "story_id": 13,
                    "story_title": "Event Delta Replay",
                    "priority": 2,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return _sprint_output_for_story_ids([12])

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        max_story_points=10,
        include_task_decomposition=False,
        selected_story_ids=[12, 13],
        user_input=None,
        capacity_points=10,
        capacity_source="user_override",
        capacity_basis="10 points",
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert result["validation_errors"] == [
        "selected stories do not match locked Sprint selection: "
        "expected [12, 13], actual [12]"
    ]


@pytest.mark.asyncio
async def test_runtime_rejects_locked_selection_selected_count_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime rejects locked sprint selected count mismatches."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 1,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
                {
                    "story_id": 13,
                    "story_title": "Event Delta Replay",
                    "priority": 2,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return _sprint_output_for_story_ids([12, 13], selected_count=3)

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        max_story_points=10,
        include_task_decomposition=False,
        selected_story_ids=[12, 13],
        user_input=None,
        capacity_points=10,
        capacity_source="user_override",
        capacity_basis="10 points",
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert "capacity analysis does not match locked Sprint selection" in result["error"]


@pytest.mark.asyncio
async def test_runtime_rejects_locked_selection_story_points_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime rejects locked sprint story point mismatches."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 1,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
                {
                    "story_id": 13,
                    "story_title": "Event Delta Replay",
                    "priority": 2,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return _sprint_output_for_story_ids([12, 13], story_points_used=5)

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        max_story_points=10,
        include_task_decomposition=False,
        selected_story_ids=[12, 13],
        user_input=None,
        capacity_points=10,
        capacity_source="user_override",
        capacity_basis="10 points",
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert "capacity analysis does not match locked Sprint selection" in result["error"]


@pytest.mark.asyncio
async def test_runtime_rejects_locked_selection_max_story_points_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime rejects locked sprint max story point mismatches."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 1,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
                {
                    "story_id": 13,
                    "story_title": "Event Delta Replay",
                    "priority": 2,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return _sprint_output_for_story_ids([12, 13], max_story_points=10)

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        max_story_points=13,
        include_task_decomposition=False,
        selected_story_ids=[12, 13],
        user_input=None,
        capacity_points=13,
        capacity_source="user_override",
        capacity_basis="13 points",
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert "capacity analysis does not match locked Sprint selection" in result["error"]


@pytest.mark.asyncio
async def test_runtime_rejects_locked_selection_deselected_stories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime rejects deselected stories for locked sprint selection."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 1,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
                {
                    "story_id": 13,
                    "story_title": "Event Delta Replay",
                    "priority": 2,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return _sprint_output_for_story_ids(
            [12, 13],
            deselected_stories=[
                {
                    "story_id": 99,
                    "reason": "Not included by the model.",
                }
            ],
        )

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        max_story_points=10,
        include_task_decomposition=False,
        selected_story_ids=[12, 13],
        user_input=None,
        capacity_points=10,
        capacity_source="user_override",
        capacity_basis="10 points",
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert (
        "deselected stories are not allowed for locked Sprint selection"
        in result["error"]
    )


@pytest.mark.asyncio
async def test_runtime_accepts_output_that_matches_locked_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime accepts output with exact locked selection capacity."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 1,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
                {
                    "story_id": 13,
                    "story_title": "Event Delta Replay",
                    "priority": 2,
                    "story_points": 2,
                    "evaluated_invariant_ids": [],
                },
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return _sprint_output_for_story_ids([12, 13])

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        max_story_points=10,
        include_task_decomposition=False,
        selected_story_ids=[12, 13],
        user_input=None,
        capacity_points=10,
        capacity_source="user_override",
        capacity_basis="10 points",
    )

    assert result["success"] is True
    assert [
        story["story_id"] for story in result["output_artifact"]["selected_stories"]
    ] == [12, 13]


@pytest.mark.asyncio
async def test_runtime_rejects_out_of_scope_task_invariant_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime rejects out of scope task invariant bindings."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 2,
                    "story_points": 3,
                    "evaluated_invariant_ids": [],
                }
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return _valid_sprint_output(max_story_points=None)

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        max_story_points=None,
        include_task_decomposition=True,
        selected_story_ids=[12],
        user_input=None,
        capacity_points=9,
        capacity_source="project_metrics",
        capacity_basis="9 points",
    )

    assert result["success"] is False
    assert (
        result["error"]
        == "Sprint output validation failed: invalid task invariant bindings"
    )


@pytest.mark.asyncio
async def test_runtime_passes_story_acceptance_criteria_into_decomposition_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime passes story acceptance criteria into decomposition validator."""
    captured = {}

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 2,
                    "story_points": 3,
                    "evaluated_invariant_ids": ["INV-12"],
                    "acceptance_criteria": "Persist the event\nSurface a success response",  # noqa: E501
                }
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return _valid_sprint_output(max_story_points=None)

    def fake_validate_task_decomposition_quality(
        _output: object,
        *,
        include_task_decomposition: object,
        has_acceptance_criteria_by_story: bool,
        acceptance_criteria_items_by_story: object = None,
    ) -> object:
        captured["include_task_decomposition"] = include_task_decomposition
        captured["has_acceptance_criteria_by_story"] = has_acceptance_criteria_by_story
        captured["acceptance_criteria_items_by_story"] = (
            acceptance_criteria_items_by_story
        )
        return []

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)
    monkeypatch.setattr(
        sprint_runtime,
        "validate_task_decomposition_quality",
        fake_validate_task_decomposition_quality,
    )

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        max_story_points=None,
        include_task_decomposition=True,
        selected_story_ids=[12],
        user_input=None,
        capacity_points=9,
        capacity_source="project_metrics",
        capacity_basis="9 points",
    )

    assert result["success"] is True
    assert captured["include_task_decomposition"] is True
    assert captured["has_acceptance_criteria_by_story"] == {12: True}
    assert captured["acceptance_criteria_items_by_story"] == {
        12: ["Persist the event", "Surface a success response"]
    }


@pytest.mark.asyncio
async def test_runtime_rejects_poor_task_decomposition_quality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime rejects poor task decomposition quality."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 2,
                    "story_points": 3,
                    "evaluated_invariant_ids": [],
                    "acceptance_criteria": "Persist the event\nSurface a success response",  # noqa: E501
                }
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return json.dumps(
            {
                "sprint_goal": "goal",
                "sprint_number": 1,
                "selected_stories": [
                    {
                        "story_id": 12,
                        "story_title": "Event Delta Persistence",
                        "tasks": [
                            {
                                "description": "Do the work",
                                "task_kind": "implementation",
                                "checklist_items": ["Persist the event"],
                                "artifact_targets": ["event persistence service"],
                                "workstream_tags": ["backend"],
                                "relevant_invariant_ids": [],
                            }
                        ],
                        "reason_for_selection": "reason",
                    }
                ],
                "deselected_stories": [],
                "capacity_analysis": _capacity_analysis_payload(
                    capacity_points=9,
                    capacity_source="project_metrics",
                    selected_count=1,
                    story_points_used=3,
                ),
            }
        )

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        max_story_points=None,
        include_task_decomposition=True,
        selected_story_ids=[12],
        user_input=None,
        capacity_points=9,
        capacity_source="project_metrics",
        capacity_basis="9 points",
    )

    assert result["success"] is False
    assert (
        result["error"]
        == "Sprint output validation failed: poor task decomposition quality"
    )


@pytest.mark.asyncio
async def test_runtime_retries_task_decomposition_validation_with_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify task decomposition validation failures receive one repair attempt."""
    invoke_payloads: list[sprint_runtime.SprintPlannerInput] = []
    expected_attempt_count = 2

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 2,
                    "story_points": 3,
                    "evaluated_invariant_ids": [],
                    "acceptance_criteria": "Persist the event",
                }
            ],
        }

    async def fake_invoke(payload: sprint_runtime.SprintPlannerInput) -> object:
        invoke_payloads.append(payload)
        artifact_target = (
            "event_delta.json"
            if len(invoke_payloads) == 1
            else "event delta persistence artifact"
        )
        return json.dumps(
            {
                "sprint_goal": "Deliver onboarding-ready event persistence",
                "sprint_number": 1,
                "selected_stories": [
                    {
                        "story_id": 12,
                        "story_title": "Event Delta Persistence",
                        "tasks": [
                            {
                                "description": "Create event persistence artifact",
                                "task_kind": "implementation",
                                "checklist_items": ["Define persisted event fields"],
                                "artifact_targets": [artifact_target],
                                "workstream_tags": ["persistence"],
                                "relevant_invariant_ids": [],
                            }
                        ],
                        "reason_for_selection": "Supports the sprint goal.",
                    }
                ],
                "deselected_stories": [],
                "capacity_analysis": _capacity_analysis_payload(
                    capacity_points=13,
                    selected_count=1,
                    story_points_used=3,
                ),
            }
        )

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        max_story_points=13,
        include_task_decomposition=True,
        selected_story_ids=[12],
        user_input=None,
        capacity_points=13,
        capacity_source="user_override",
        capacity_basis="13 points",
    )

    assert result["success"] is True
    assert len(invoke_payloads) == expected_attempt_count
    retry_context = invoke_payloads[1].user_context
    assert retry_context is not None
    assert "SYSTEM_FEEDBACK" in retry_context
    assert "event_delta.json" in retry_context
    assert "Use component/module names instead" in retry_context


@pytest.mark.asyncio
async def test_runtime_exposes_compact_public_task_kind_retry_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime exposes compact public task kind retry hints."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 2,
                    "story_points": 3,
                    "evaluated_invariant_ids": [],
                }
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return json.dumps(
            {
                "sprint_goal": "goal",
                "sprint_number": 1,
                "selected_stories": [
                    {
                        "story_id": 12,
                        "story_title": "Event Delta Persistence",
                        "tasks": [
                            {
                                "description": "Get approval",
                                "task_kind": "approval",
                                "checklist_items": ["Confirm the change can proceed"],
                                "artifact_targets": ["approval decision"],
                                "workstream_tags": ["governance"],
                                "relevant_invariant_ids": [],
                            }
                        ],
                        "reason_for_selection": "reason",
                    }
                ],
                "deselected_stories": [],
                "capacity_analysis": _capacity_analysis_payload(
                    capacity_points=13,
                    selected_count=1,
                    story_points_used=3,
                ),
            }
        )

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        max_story_points=13,
        include_task_decomposition=True,
        selected_story_ids=[12],
        user_input=None,
        capacity_points=13,
        capacity_source="user_override",
        capacity_basis="13 points",
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert result["output_artifact"]["validation_errors"] == [
        "Task 'Get approval' uses unsupported task_kind 'approval'. Use one of: analysis, design, implementation, testing, documentation, refactor."  # noqa: E501
    ]


@pytest.mark.asyncio
async def test_runtime_uses_canonical_public_hint_for_non_string_task_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime uses canonical public hint for non string task kind."""

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 2,
                    "story_points": 3,
                    "evaluated_invariant_ids": [],
                }
            ],
        }

    async def fake_invoke(_payload: object) -> object:
        return json.dumps(
            {
                "sprint_goal": "goal",
                "sprint_number": 1,
                "selected_stories": [
                    {
                        "story_id": 12,
                        "story_title": "Event Delta Persistence",
                        "tasks": [
                            {
                                "description": "Get approval",
                                "task_kind": None,
                                "checklist_items": ["Confirm the change can proceed"],
                                "artifact_targets": ["approval decision"],
                                "workstream_tags": ["governance"],
                                "relevant_invariant_ids": [],
                            }
                        ],
                        "reason_for_selection": "reason",
                    }
                ],
                "deselected_stories": [],
                "capacity_analysis": _capacity_analysis_payload(
                    capacity_points=13,
                    selected_count=1,
                    story_points_used=3,
                ),
            }
        )

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        max_story_points=13,
        include_task_decomposition=True,
        selected_story_ids=[12],
        user_input=None,
        capacity_points=13,
        capacity_source="user_override",
        capacity_basis="13 points",
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert result["output_artifact"]["validation_errors"] == [
        "Task 'Get approval' has invalid task_kind. Use one of: analysis, design, implementation, testing, documentation, refactor."  # noqa: E501
    ]
    assert "other" not in result["output_artifact"]["validation_errors"][0]


@pytest.mark.asyncio
async def test_adk_runner_preserves_structured_validation_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify adk runner preserves structured validation details."""
    structured_errors = [
        {
            "type": "literal_error",
            "loc": ("selected_stories", 0, "tasks", 0, "task_kind"),
            "msg": "Input should be 'analysis' or 'design'",
            "input": "approval",
        }
    ]

    class FakeSessionService:
        async def create_session(self, *, app_name: object, user_id: int) -> object:
            del app_name, user_id
            return SimpleNamespace(id="session-1")

    class FakeRunner:
        def __init__(
            self, *, agent: object, app_name: object, session_service: object
        ) -> None:
            self.agent = agent
            self.app_name = app_name
            self.session_service = session_service

        async def run_async(
            self, *, user_id: int, session_id: int, new_message: object
        ) -> object:
            _ = (user_id, session_id, new_message)

            class FakeStructuredValidationError(Exception):
                def errors(self) -> object:
                    return structured_errors

            msg = "ADK validation failed"
            raise RuntimeError(msg) from FakeStructuredValidationError()
            yield None

    class FakePart:
        @staticmethod
        def from_text(*, text: object) -> object:
            return SimpleNamespace(text=text)

    class FakeContent:
        def __init__(self, *, role: object, parts: object) -> None:
            self.role = role
            self.parts = parts

    monkeypatch.setattr(adk_runner, "InMemorySessionService", FakeSessionService)
    monkeypatch.setattr(adk_runner, "Runner", FakeRunner)
    monkeypatch.setattr(
        adk_runner,
        "types",
        SimpleNamespace(Content=FakeContent, Part=FakePart),
    )

    with pytest.raises(adk_runner.AgentInvocationError) as exc_info:
        await adk_runner.invoke_agent_to_text(
            agent=SimpleNamespace(name="sprint"),
            runner_identity=SimpleNamespace(app_name="app", user_id="user"),
            payload_json="{}",
            no_text_error="missing",
        )

    assert exc_info.value.validation_errors == structured_errors


@pytest.mark.asyncio
async def test_runtime_falls_back_to_public_hint_for_adk_task_kind_errors_without_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime falls back to public hint for adk task kind errors without input."""  # noqa: E501

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 2,
                    "story_points": 3,
                    "evaluated_invariant_ids": [],
                }
            ],
        }

    async def fake_invoke(_payload: object) -> Never:
        msg = "ADK validation failed"
        raise adk_runner.AgentInvocationError(
            msg,
            validation_errors=[
                {
                    "type": "missing",
                    "loc": ("selected_stories", 0, "tasks", 0, "task_kind"),
                    "msg": "Field required",
                }
            ],
        )

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        max_story_points=13,
        include_task_decomposition=True,
        selected_story_ids=[12],
        user_input=None,
        capacity_points=13,
        capacity_source="user_override",
        capacity_basis="13 points",
    )

    assert result["success"] is False
    assert result["failure_stage"] == "invocation_exception"
    assert result["output_artifact"]["validation_errors"] == [
        "Task has invalid task_kind. Use one of: analysis, design, implementation, testing, documentation, refactor."  # noqa: E501
    ]
