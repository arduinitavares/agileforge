"""Tests for Story feedback quality evaluation."""

from services.story_feedback_quality import (
    STORY_FEEDBACK_QUALITY_SCHEMA_VERSION,
    evaluate_story_feedback_quality,
)

PASSING_SCORE_FLOOR = 80


def test_vague_feedback_needs_revision() -> None:
    """Vague feedback should be soft-gated before generation."""
    result = evaluate_story_feedback_quality(
        "Make this more INVEST.",
        parent_requirement="Technology and Model Research Spike",
        force=False,
    )

    assert result["schema_version"] == STORY_FEEDBACK_QUALITY_SCHEMA_VERSION
    assert result["needs_revision"] is True
    assert result["can_force"] is True
    assert result["forced"] is False
    assert "target" in result["missing_fields"]
    assert "required_change" in result["missing_fields"]
    assert "acceptance_criteria" in result["missing_fields"]
    assert "scope_limit" in result["missing_fields"]
    assert "Target:" in result["suggested_template"]
    assert result["warnings"][0]["code"] == "FEEDBACK_TOO_VAGUE"


def test_structured_feedback_passes() -> None:
    """Structured feedback with target, evidence, change, criteria, and scope passes."""
    feedback = """
Target:
Technology and Model Research Spike, attempt-6

Issue:
Draft is partial_capacity_limited and not saveable.

Evidence:
quality.blocking_findings includes PARTIAL_CAPACITY_LIMITED.

Required change:
Refine only delay-horizon validation.

Acceptance criteria:
- Stories cover only delay-horizon validation.
- Each story has one user goal.
- Draft returns coverage_status=complete for the narrowed slice.

Scope limit:
Do not cover state-window, stack, action-set, or recovered-code work.

Priority:
Must fix.
"""

    result = evaluate_story_feedback_quality(
        feedback,
        parent_requirement="Technology and Model Research Spike",
        force=False,
    )

    assert result["needs_revision"] is False
    assert result["missing_fields"] == []
    assert result["forced"] is False
    assert result["score"] >= PASSING_SCORE_FLOOR


def test_force_records_override_but_keeps_warnings() -> None:
    """Force override should not hide weak feedback warnings."""
    result = evaluate_story_feedback_quality(
        "Try again.",
        parent_requirement="Requirement A",
        force=True,
    )

    assert result["needs_revision"] is True
    assert result["forced"] is True
    assert result["can_force"] is True
    assert result["warnings"][0]["code"] == "FEEDBACK_TOO_VAGUE"
