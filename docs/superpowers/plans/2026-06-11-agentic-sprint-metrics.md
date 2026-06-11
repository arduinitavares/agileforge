# Agentic Sprint Metrics Implementation Plan

> **For agentic workers:** REQUIRED WORKFLOW: implement task-by-task with
> TDD. For each task, write or extend the failing tests first, verify the RED
> failure, implement the minimal code, verify GREEN, then run a two-stage
> review: spec compliance first, code quality second. Steps use checkbox
> (`- [ ]`) syntax for tracking.

**Goal:** Implement the accepted read-only Sprint metrics feature without
changing Sprint planner prompts or mutating workflow state.

**Architecture:** Add a pure metrics projection module that accepts completed
Sprint-like rows and Workflow Event-like rows. Keep database reads at the
existing API and `SprintPhaseRunner` boundaries. Expose the projection through
API, application facade, command registry, and CLI. Preserve existing Sprint
history behavior.

**Tech Stack:** Python 3.13, SQLModel, FastAPI, AgileForge argparse CLI,
pytest, pyrepo-check.

---

## Preconditions

- Main checkout cleaned back to `master`.
- Accidental implementation preserved only as reference:
  `/var/folders/fh/0bmky89j2d54xdptjs_1mrjm0000gn/T/agileforge-agentic-sprint-metrics-reference-20260611T161028Z`
- Worktree: `/Users/aaat/projects/agileforge-agentic-sprint-metrics-readonly`
- Branch: `dev/agentic-sprint-metrics-readonly`
- Accepted design:
  `docs/superpowers/specs/2026-06-11-agentic-sprint-metrics-design.md`
- Baseline verification in this worktree:
  - `uv run --frozen pytest tests/test_api_route_registration.py -q -k sprint_router`
  - `uv run --frozen pyrepo-check --all`
  - Result: Ruff, annotations, ty, Bandit, and pytest passed; pytest selected
    `2193` tests with `2` skipped and `13` deselected.

## Reference-Only Branch Decision

The accidental `dev/agentic-sprint-metrics` implementation is not the
implementation source. Salvage only:

- scenario coverage ideas from `tests/test_sprint_metrics.py`
- public command/API shape
- recommendation examples

Do not copy:

- DB-opening metrics function inside `services/phases/sprint_service.py`
- local imports inside a large function
- duplicated Sprint serializers where existing runner/API boundaries can load
  records
- missing command registry coverage

## Target File Structure

Create:

- `services/phases/sprint_metrics.py`
- `tests/test_sprint_metrics.py`

Modify:

- `services/agent_workbench/sprint_phase.py`
- `services/agent_workbench/application.py`
- `services/agent_workbench/command_registry.py`
- `cli/main.py`
- `api.py`
- `routers/sprint.py`
- `tests/test_agent_workbench_sprint_phase.py`
- `tests/test_agent_workbench_cli.py`
- `tests/test_agent_workbench_command_schema.py`
- `tests/test_api_route_registration.py`
- `tests/test_api_sprint_flow.py`

Do not modify:

- Sprint planner prompt files
- LLM provider/runtime token capture paths
- database schema or migrations
- frontend files

---

## Task 1: Pure Metrics Projection

**Files:**

- Create: `services/phases/sprint_metrics.py`
- Create: `tests/test_sprint_metrics.py`

- [x] **Step 1: Write failing pure-projection tests**

Create tests for:

- no completed Sprints returns `status="insufficient_history"` and
  `recommended_next_sprint_points is None`
- four completed Sprint-like rows produce:
  - `completed_sprint_count`
  - total completed points
  - median points
  - average points
  - elapsed seconds totals
  - points per hour
  - newest three Sprint recommendation
- warnings are emitted for:
  - missing elapsed time
  - invalid elapsed time
  - missing turn counts
  - completed Story points missing
- token metrics always returns the unavailable/null contract

Run:

```bash
uv run --frozen pytest tests/test_sprint_metrics.py -q
```

Expected RED: import or missing-function failure.

- [x] **Step 2: Implement `services/phases/sprint_metrics.py`**

Implement a DB-free module with these public functions:

```python
def build_sprint_metrics(
    *,
    project_id: int,
    completed_sprints: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return read-only Sprint metrics and planning recommendation."""
```

Input row contract:

- `sprint_id`
- `goal`
- `status`
- `started_at`
- `completed_at`
- `start_date`
- `end_date`
- `story_count`
- `completed_story_count`
- `task_count`
- `completed_task_count`
- `story_points_planned`
- `story_points_completed`
- `elapsed_seconds`
- `workflow_event_count`
- `workflow_event_duration_seconds`
- `turn_count`
- `history_fidelity`
- `unestimated_completed_story_count`

Output contract follows the accepted design exactly:

- top-level `project_id`
- `status`
- `summary`
- `recommendation`
- `completed_sprints`
- `token_metrics`
- `data_quality_warnings`

Implementation rules:

- Sort rows by `completed_at` descending, then `sprint_id` descending.
- Recommendation samples the newest one to three completed Sprints.
- Use nearest-integer half-up rounding for
  `recommended_next_sprint_points`.
- No completed Sprints means no fake fallback velocity.
- Do not import SQLModel, FastAPI, repositories, or database engines.

- [x] **Step 3: Verify GREEN**

Run:

```bash
uv run --frozen pytest tests/test_sprint_metrics.py -q
```

Expected GREEN.

## Task 2: Runner Boundary Integration

**Files:**

- Modify: `services/agent_workbench/sprint_phase.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `tests/test_agent_workbench_sprint_phase.py`

- [x] **Step 1: Write failing runner tests**

Add tests proving:

- `SprintPhaseRunner.metrics(project_id=...)` returns the same envelope style
  as other runner methods.
- completed Sprint records are loaded from durable DB rows.
- Workflow Event `duration_seconds` and `turn_count` are aggregated per Sprint.
- missing project returns the existing project-load error envelope.

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_sprint_phase.py -q -k metrics
```

Expected RED: missing runner method or missing metrics projection.

- [x] **Step 2: Add runner/application methods**

In `services/agent_workbench/sprint_phase.py`:

- import `build_sprint_metrics`
- add `SprintPhaseRunner.metrics()`
- add async `_metrics()`
- keep `_metrics()` responsible for DB reads:
  - load project via `_load_project`
  - query completed Sprints with `_saved_sprint_query()`
  - query `WorkflowEvent` rows by Sprint id
  - serialize Sprint-like rows with a small helper such as
    `_serialize_sprint_metrics_row(sprint, events)`
- ensure the helper reuses existing concepts:
  - `_sprint_elapsed_seconds`
  - `_history_fidelity`
  - `_serialize_temporal`
  - Story status `DONE` / `ACCEPTED`
  - Task status `DONE`
- return `_data_envelope(build_sprint_metrics(...))`

In `services/agent_workbench/application.py`:

- add `sprint_metrics(project_id=...)`
- forward to `self._get_sprint_runner().metrics(...)`

- [x] **Step 3: Verify GREEN**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_sprint_phase.py -q -k metrics
```

Expected GREEN.

## Task 3: API Endpoint

**Files:**

- Modify: `api.py`
- Modify: `routers/sprint.py`
- Modify: `tests/test_api_route_registration.py`
- Modify: `tests/test_api_sprint_flow.py`

- [x] **Step 1: Write failing API tests**

Add tests proving:

- `/api/projects/{project_id}/sprint/metrics` is registered.
- missing project returns `404`.
- existing project with completed Sprint execution records returns:
  - `status="success"`
  - `data.summary.completed_sprint_count`
  - `data.recommendation.recommended_next_sprint_points`
  - `data.token_metrics.status="unavailable"`

Run:

```bash
uv run --frozen pytest tests/test_api_route_registration.py -q -k sprint_router
uv run --frozen pytest tests/test_api_sprint_flow.py -q -k sprint_metrics
```

Expected RED before endpoint wiring.

- [x] **Step 2: Wire API through existing boundary**

In `routers/sprint.py`:

- add `get_project_sprint_metrics` to route handlers
- register `GET /api/projects/{project_id}/sprint/metrics`

In `api.py`:

- implement `get_project_sprint_metrics(project_id: int)`
- validate project exists through `product_repo`
- load completed Sprints and Workflow Events at the API boundary
- use the same metrics row serialization contract as the runner path
- return existing API envelope style:

```python
{"status": "success", "data": data}
```

Avoid adding a DB-opening function to `services/phases/sprint_service.py`.

- [x] **Step 3: Verify GREEN**

Run:

```bash
uv run --frozen pytest tests/test_api_route_registration.py -q -k sprint_router
uv run --frozen pytest tests/test_api_sprint_flow.py -q -k sprint_metrics
```

Expected GREEN.

## Task 4: CLI And Command Schema

**Files:**

- Modify: `cli/main.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `tests/test_agent_workbench_cli.py`
- Modify: `tests/test_agent_workbench_command_schema.py`

- [x] **Step 1: Write failing CLI/schema tests**

Add tests proving:

- `agileforge sprint metrics --project-id <id>` routes to
  `application.sprint_metrics(project_id=<id>)`
- CLI emits the JSON command result and a concise human-readable summary
- command registry includes `agileforge sprint metrics`
- command schema requires `project_id`

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_cli.py -q -k sprint_metrics
uv run --frozen pytest tests/test_agent_workbench_command_schema.py -q -k sprint_metrics
```

Expected RED before parser/registry changes.

- [x] **Step 2: Wire CLI and command registry**

In `cli/main.py`:

- add `sprint metrics` subparser after `sprint history`
- add `_sprint_metrics()` handler
- add `_print_sprint_metrics_summary()` that writes a human summary to stderr
  only when the result is ok

In `services/agent_workbench/command_registry.py`:

- register `agileforge sprint metrics`
- mark it as read-only
- require `project_id`
- describe it as Sprint metrics and planning recommendation, not Sprint
  generation

- [x] **Step 3: Verify GREEN**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_cli.py -q -k sprint_metrics
uv run --frozen pytest tests/test_agent_workbench_command_schema.py -q -k sprint_metrics
```

Expected GREEN.

## Task 5: Cross-Surface Parity And Final Gate

**Files:**

- Modify only files touched by prior tasks if parity bugs are found.

- [x] **Step 1: Run focused parity tests**

Run:

```bash
uv run --frozen pytest tests/test_sprint_metrics.py -q
uv run --frozen pytest tests/test_agent_workbench_sprint_phase.py -q -k metrics
uv run --frozen pytest tests/test_api_route_registration.py -q -k sprint_router
uv run --frozen pytest tests/test_api_sprint_flow.py -q -k sprint_metrics
uv run --frozen pytest tests/test_agent_workbench_cli.py -q -k sprint_metrics
uv run --frozen pytest tests/test_agent_workbench_command_schema.py -q -k sprint_metrics
```

- [x] **Step 2: Run repo gates**

Run:

```bash
uv run --frozen pyrepo-check --all
git diff --check
```

- [x] **Step 3: Read-only ASA verification**

Run only after tests pass:

```bash
agileforge sprint metrics --project-id 3
```

Summarize:

- completed Sprint count
- completed points
- total elapsed seconds
- recommended next Sprint points
- token metrics status

Do not mutate ASA.

- [x] **Step 4: Commit**

Commit after all gates pass:

```bash
git status --short
git add docs/superpowers/specs/2026-06-11-agentic-sprint-metrics-design.md \
  docs/superpowers/plans/2026-06-11-agentic-sprint-metrics.md \
  services/phases/sprint_metrics.py \
  services/agent_workbench/sprint_phase.py \
  services/agent_workbench/application.py \
  services/agent_workbench/command_registry.py \
  cli/main.py \
  api.py \
  routers/sprint.py \
  tests/test_sprint_metrics.py \
  tests/test_agent_workbench_sprint_phase.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_command_schema.py \
  tests/test_api_route_registration.py \
  tests/test_api_sprint_flow.py
git commit -m "feat(sprint): add read-only agentic metrics"
```

## Review Gates

After each implementation task:

1. Spec compliance review:
   - Compare the diff to
     `docs/superpowers/specs/2026-06-11-agentic-sprint-metrics-design.md`.
   - Block on missing contract fields, prompt mutation, DB schema mutation, or
     missing CLI/API parity.
2. Code quality review:
   - Check boundary shape, duplication, naming, tests, and maintainability.
   - Block on DB access in pure metrics module, broad rewrites, fragile tests,
     or missing command registry coverage.

## Final Done Criteria

- Pure metrics module is DB-free.
- API and runner DB reads remain at existing boundary layers.
- CLI, API, command registry, and application facade all expose Sprint metrics.
- Sprint planner prompts and token persistence remain unchanged.
- Focused tests pass.
- `pyrepo-check --all` passes.
- `git diff --check` passes.
- ASA read-only metrics command produces the expected high-level summary.
