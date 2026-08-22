"""Prompt contract tests for the sprint planner instructions."""

from adapters.adk.prompts import load_prompt


def _require_substring(instructions: str, expected: str) -> None:
    if expected not in instructions:
        raise AssertionError(expected)


def _require_exact_line(lines: set[str], expected: str) -> None:
    if expected not in lines:
        raise AssertionError(expected)


def test_sprint_planner_instructions_pin_task_kind_contract() -> None:
    """Pin the sprint planner's task-kind and decomposition prompt contract."""
    instructions = load_prompt("sprint.txt")
    lines = set(instructions.splitlines())

    _require_exact_line(
        lines,
        "- task_kind is implementation, test, documentation, or research.",
    )
    _require_exact_line(
        lines,
        "- Each task contains description, relevant_spec_item_ids, task_kind,",
    )
    _require_exact_line(
        lines,
        "- relevant_spec_item_ids must be non-empty exact IDs from that parent Story's",
    )
    _require_exact_line(
        lines,
        "- Keep artifact targets at component or subsystem level, not file paths.",
    )
