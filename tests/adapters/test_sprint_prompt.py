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
        "        * The schema literal set is: analysis, design, "
        "implementation, testing, documentation, refactor, other.",
    )
    _require_exact_line(
        lines,
        "        * For normal sprint output, emit only: analysis, design, "
        "implementation, testing, documentation, refactor.",
    )
    _require_exact_line(
        lines,
        "        * Do not emit `other` or `review` for normal sprint task "
        "decomposition.",
    )
    _require_exact_line(lines, "            * final verification -> testing")
    _require_exact_line(
        lines,
        "            * documenting decisions -> documentation",
    )
    _require_exact_line(lines, "            * inspection/audit -> analysis")
    _require_substring(
        instructions,
        "Do not restate story acceptance criteria",
    )
    _require_substring(instructions, "Do not use file paths")
    _require_substring(instructions, "auth API")
