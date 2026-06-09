"""Deterministic quality checks for Story refinement feedback."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

STORY_FEEDBACK_QUALITY_SCHEMA_VERSION: str = (
    "agileforge.story_feedback_quality.v1"
)

_FIELD_LABELS: Mapping[str, Sequence[str]] = {
    "target": ("target:", "story:", "requirement:", "attempt:"),
    "issue": ("issue:", "problem:", "gap:"),
    "evidence": (
        "evidence:",
        "because:",
        "quality.blocking_findings",
        "remaining_scope",
    ),
    "required_change": (
        "required change:",
        "change:",
        "refine only",
        "split",
        "remove",
        "preserve",
        "do not cover",
    ),
    "acceptance_criteria": (
        "acceptance criteria:",
        "- stories",
        "- each story",
        "coverage_status=complete",
        "saveable",
    ),
    "scope_limit": ("scope limit:", "do not", "preserve", "out of scope"),
    "priority": ("priority:", "must fix", "should fix", "optional"),
}

_REQUIRED_FIELDS: Sequence[str] = (
    "target",
    "required_change",
    "acceptance_criteria",
    "scope_limit",
)
_ISSUE_OR_EVIDENCE_FIELDS: Sequence[str] = ("issue", "evidence")
_MIN_STRONG_FEEDBACK_CHARS: int = 80
_VAGUE_FEEDBACK_RE: re.Pattern[str] = re.compile(
    r"\b("
    r"make (this|it) better|"
    r"make (this|it) more invest|"
    r"fix (the )?low stories|"
    r"try again|"
    r"regenerate (this )?better"
    r")\b",
    flags=re.IGNORECASE,
)


def _normalized_text(text: str | None) -> str:
    return " ".join((text or "").strip().split())


def _present_fields(feedback: str) -> list[str]:
    normalized = feedback.casefold()
    present: list[str] = []
    for field, labels in _FIELD_LABELS.items():
        if any(label.casefold() in normalized for label in labels):
            present.append(field)
    return present


def _missing_fields(present_fields: Sequence[str]) -> list[str]:
    present = set(present_fields)
    missing = [field for field in _REQUIRED_FIELDS if field not in present]
    if not any(field in present for field in _ISSUE_OR_EVIDENCE_FIELDS):
        missing.append("issue_or_evidence")
    return sorted(missing)


def _warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _suggested_template(parent_requirement: str) -> str:
    return "\n".join(
        [
            "Target:",
            parent_requirement,
            "",
            "Issue:",
            "[State the observable problem.]",
            "",
            "Evidence:",
            "[Cite the finding, remaining scope, source constraint, or contradiction.]",
            "",
            "Required change:",
            "[Say exactly what to refine, split, add, remove, or preserve.]",
            "",
            "Acceptance criteria:",
            "- [Observable condition 1]",
            "- [Observable condition 2]",
            "- [Observable condition 3]",
            "",
            "Scope limit:",
            "[Say what should not change or should not be covered in this pass.]",
            "",
            "Priority:",
            "Must fix",
        ]
    )


def _suggested_example(parent_requirement: str) -> str:
    return "\n".join(
        [
            "Target:",
            parent_requirement,
            "",
            "Issue:",
            "The current Story draft is too broad to validate.",
            "",
            "Evidence:",
            "The quality findings show the draft does not meet the narrowed scope.",
            "",
            "Required change:",
            "Refine only the named slice and keep unrelated work out of this pass.",
            "",
            "Acceptance criteria:",
            "- Stories cover only the named slice.",
            "- Each story has one user goal.",
            "- The draft can pass the Story quality gate for this target.",
            "",
            "Scope limit:",
            "Do not add unrelated product, architecture, or implementation work.",
            "",
            "Priority:",
            "Must fix.",
        ]
    )


def _quality_score(
    *,
    missing_fields: Sequence[str],
    warnings: Sequence[Mapping[str, str]],
) -> int:
    missing_penalty = len(set(missing_fields)) * 15
    warning_penalty = len(warnings) * 10
    return max(0, 100 - missing_penalty - warning_penalty)


def evaluate_story_feedback_quality(
    feedback: str | None,
    *,
    parent_requirement: str,
    force: bool = False,
) -> dict[str, Any]:
    """Return local quality metadata for one Story refinement feedback string."""
    text = _normalized_text(feedback)
    present_fields = _present_fields(text)
    missing_fields = _missing_fields(present_fields)

    warnings: list[dict[str, str]] = []
    if not text or _VAGUE_FEEDBACK_RE.search(text):
        warnings.append(
            _warning(
                "FEEDBACK_TOO_VAGUE",
                "Feedback does not name a concrete target, issue, evidence, "
                "required change, and success criteria.",
            )
        )
    if len(text) < _MIN_STRONG_FEEDBACK_CHARS:
        warnings.append(
            _warning(
                "FEEDBACK_TOO_SHORT",
                "Feedback is too short to guide reliable Story refinement.",
            )
        )
    if missing_fields:
        warnings.append(
            _warning(
                "FEEDBACK_FIELDS_MISSING",
                f"Missing feedback fields: {', '.join(missing_fields)}.",
            )
        )

    return {
        "schema_version": STORY_FEEDBACK_QUALITY_SCHEMA_VERSION,
        "needs_revision": bool(warnings or missing_fields),
        "can_force": True,
        "forced": force,
        "score": _quality_score(
            missing_fields=missing_fields,
            warnings=warnings,
        ),
        "present_fields": present_fields,
        "missing_fields": missing_fields,
        "warnings": warnings,
        "suggested_template": _suggested_template(parent_requirement),
        "suggested_example": _suggested_example(parent_requirement),
    }
