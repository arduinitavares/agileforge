# Lifecycle Task 7 Fix 5

## Scope

- Worktree: `/Users/aaat/projects/agileforge/.worktrees/domain-workflow-graph-task5-integration`
- Baseline HEAD: `b7f22eadcb674af9cfcfb3f0ca3d37e4abd8a79a`
- Commit: `fix: suppress ambiguous story review commands`

## TDD Evidence

- RED: `uv run --frozen pytest tests/adapters/test_command_renderer.py::test_duplicate_story_review_selectors_are_ambiguous -q`
  failed because duplicate `decide_story` selectors produced two identical commands.
- GREEN: the same focused test passed after the fix.

## Change

`render_workflow_next` now counts selector-bearing decisions by `(request_kind, instance_key)` and emits selector-bearing semantic commands only when that pair is unique. Selectorless and unrelated rendering paths remain unchanged.

## Verification

- `uv run --frozen pytest tests/adapters/test_command_renderer.py tests/adapters/test_cli_workflow_domain.py -q`: `87 passed`
- `uv run --frozen ruff check cli/workflow_commands.py tests/adapters/test_command_renderer.py`: passed
- `uv run --frozen ty check cli/workflow_commands.py tests/adapters/test_command_renderer.py`: passed
- `uv run --frozen ruff format --check cli/workflow_commands.py tests/adapters/test_command_renderer.py`: passed
- `git diff --check`: passed

## Concerns

Pytest emitted four pre-existing `BaseAgentConfig` deprecation warnings. No other concerns observed.
