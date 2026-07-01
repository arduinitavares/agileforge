"""Tests for ADK JSON response parsing."""

from __future__ import annotations

import json

from utils.adk_runner import parse_json_payload

STORY_OUTPUT_KEYS = {
    "parent_requirement",
    "user_stories",
    "is_complete",
    "clarifying_questions",
}


def test_parse_json_payload_returns_incomplete_dict_for_validation() -> None:
    """Valid JSON objects should reach schema validation even when incomplete."""
    assert parse_json_payload("{}", required_keys=STORY_OUTPUT_KEYS) == {}


def test_parse_json_payload_selects_schema_object_from_multi_object_text() -> None:
    """Recover the first schema-shaped JSON object from prose plus two objects."""
    first = {
        "parent_requirement": "First Model Baseline Evaluation and Reporting",
        "user_stories": [{"story_title": "Compare model to baselines"}],
        "is_complete": True,
        "clarifying_questions": [],
        "coverage_status": "complete",
    }
    second = {
        "parent_requirement": "First Model Baseline Evaluation and Reporting",
        "user_stories": [{"story_title": "Publish baseline report"}],
        "is_complete": True,
        "clarifying_questions": [],
        "coverage_status": "complete",
    }
    raw_text = (
        "Reasoning before JSON.\n"
        f"{json.dumps(first)}\n"
        "More prose between candidate objects.\n"
        f"{json.dumps(second)}\n"
    )

    parsed = parse_json_payload(raw_text, required_keys=STORY_OUTPUT_KEYS)

    assert parsed is not None
    assert parsed["user_stories"][0]["story_title"] == "Compare model to baselines"


def test_parse_json_payload_skips_unrelated_json_before_schema_object() -> None:
    """Do not return a small example object when required keys are supplied."""
    raw_text = """
Example: {"not_the_payload": true}

{
  "parent_requirement": "Requirement A",
  "user_stories": [{"story_title": "Generate report"}],
  "is_complete": true,
  "clarifying_questions": [],
  "coverage_status": "complete"
}
"""

    parsed = parse_json_payload(raw_text, required_keys=STORY_OUTPUT_KEYS)

    assert parsed is not None
    assert parsed["parent_requirement"] == "Requirement A"
    assert "not_the_payload" not in parsed


def test_parse_json_payload_default_mode_does_not_scan_independent_objects() -> None:
    """Default parsing keeps the old first-brace to last-brace fallback."""
    raw_text = """
Example: {"not_the_payload": true}

{
  "parent_requirement": "Requirement A",
  "user_stories": [{"story_title": "Generate report"}],
  "is_complete": true,
  "clarifying_questions": [],
  "coverage_status": "complete"
}
"""

    assert parse_json_payload(raw_text) is None


def test_parse_json_payload_preserves_fenced_json_behavior() -> None:
    """Existing fenced JSON behavior stays intact."""
    raw_text = """```json
{"ok": true}
```"""

    assert parse_json_payload(raw_text) == {"ok": True}
