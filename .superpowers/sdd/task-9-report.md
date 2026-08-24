# Task 9 Report

## Status

DONE. Commit: `b2f8bc8 test: verify grounded vision lifecycle retention`.

## Files Changed

Task-owned files:

- `tests/workflow/test_vision_backlog_graph.py`
- `tests/workflow/test_vision_backlog_transitions.py`
- `tests/services/test_durable_product_definition_projections.py`
- `docs/agent-cli-manual.md`

Concrete scan-remnant files:

- `repositories/workflow.py`: renamed live `select_vision_interview_input` to
  `select_vision_input`.
- `services/vision_input.py`: updated the two live selector callers.
- `tests/services/test_vision_input.py`: RED/GREEN import regression for the
  grounded selector name.
- `workflow/definitions/vision.py`: renamed the revision response reason to
  `VISION_REVISION_CLARIFICATION_REQUIRED`.
- `tests/adapters/test_api_workflow_domain.py` and
  `tests/adapters/test_command_renderer.py`: replaced stale generic
  `VISION_INTERVIEW_REQUIRED` fixture reasons.
- `tests/adapters/test_vision_recipe.py`: removed an obsolete prompt-prohibition
  assertion; the positive grounded-prompt assertion remains.

## Retained Lifecycle Coverage

- Only human acceptance unlocks Product Goal:
  `test_only_human_accepted_vision_unlocks_product_goal`.
- Feedback and rejection return to ordinary-language clarification:
  `test_nonaccepted_vision_review_reopens_ordinary_language_clarification`.
- An active Product Goal blocks accepted-Vision revision:
  `test_active_product_goal_blocks_accepted_vision_revision`.
- Vision bootstrap allows no repository attachment:
  `test_vision_bootstrap_accepts_project_without_repository_attachment`.

The selector rename and revision reason rename both had focused RED then GREEN
evidence before their minimal production changes.

## Absence Scans

- `VisionInterviewInput|VisionInterviewOutput|vision_interview_input|VISION_INTERVIEW_REQUIRED`:
  only historical design/plan references remain.
- `Do not infer Vision from repository contents|Who should benefit from this product first`:
  no matches.

## Verification

- Focused lifecycle pytest: 43 passed.
- Vision UI Node test: 9 passed.
- Direct Vision-input regression suite: 14 passed.
- Scan-driven adapter suites: 210 + 47 passed.
- `uv lock --check` and `git diff --check`: passed.
- From committed `b2f8bc8`, `uv run --frozen pyrepo-check --all`: passed
  Ruff, annotations, Ty, Bandit, and the full pytest gate.

## Concerns

None. Existing deprecation, socket-blocking, and experimental-provider warnings
remain in test output; no test or gate failure resulted.
