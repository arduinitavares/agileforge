# Agentic Sprint Capacity Planning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. For each
> task, write or extend the failing tests first, verify the RED failure,
> implement the minimal code, verify GREEN, then run a two-stage review: spec
> compliance first, code quality second. Steps use checkbox (`- [ ]`) syntax
> for tracking.

**Goal:** Transition Sprint planning from calendar-based to story-point capacity-based planning. Remove all velocity bands, Sprint duration, and planned dates from normal generation/save flows while preserving guarded Sprint persistence.

**Architecture:**
- **Core Policy:** Refactor `services/sprint_selection.py` to select stories based only on priority, dependencies, and `capacity_points` (not velocity band or story limits).
- **Persistence:** Update `models/core.py` and database migrations to allow nullable `start_date` and `end_date` on the `sprints` table using a SQLite-safe table rebuild migration.
- **Planner & FSM:** Align the Sprint planner tool schemas/instructions and FSM adapters to use `capacity_points` instead of duration/velocity assumptions.
- **API & CLI:** Update parser args and request/response models to reject duration/velocity parameters. API save must match guarded CLI save: attempt id, expected artifact fingerprint, expected state, and idempotency key are required.
- **Frontend UI:** Replace duration, velocity, and start date inputs with a read-only project capacity panel and a Max Story Points input.

**Tech Stack:** Python 3.13, SQLModel, FastAPI, AgileForge argparse CLI, HTML/JS, pytest, pyrepo-check.

---

## Preconditions

- Main checkout includes accepted spec commit `91f7cae`.
- Branch: `dev/agentic-sprint-capacity-planning`
- Accepted design:
  `docs/superpowers/specs/2026-06-11-agentic-sprint-capacity-planning-design.md`
- Baseline verification:
  - `uv run --frozen pytest tests/test_sprint_selection.py -q`
  - `uv run --frozen pytest tests/test_api_sprint_flow.py -q -k "sprint_save or sprint_generate"`

---

## Target File Structure

Create:
- `tests/test_db_migrations_sprint_nullable_dates.py`

Modify:
- `models/core.py`
- `db/migrations.py`
- `services/sprint_selection.py`
- `services/agent_workbench/sprint_phase.py`
- `services/agent_workbench/application.py`
- `services/agent_workbench/command_registry.py`
- `cli/main.py`
- `api.py`
- `routers/sprint.py`
- `orchestrator_agent/agent_tools/sprint_planner_tool/schemes.py`
- `orchestrator_agent/agent_tools/sprint_planner_tool/tools.py`
- `orchestrator_agent/agent_tools/sprint_planner_tool/instructions.txt`
- `orchestrator_agent/fsm/definitions.py`
- `orchestrator_agent/fsm/deterministic_tool_adapters.py`
- `frontend/project.html`
- `frontend/project.js`
- `scripts/benchmark_sprint_planning.py`
- `tests/test_sprint_selection.py`
- `tests/test_agent_workbench_sprint_phase.py`
- `tests/test_agent_workbench_cli.py`
- `tests/test_agent_workbench_command_schema.py`
- `tests/test_api_sprint_flow.py`
- `tests/test_api_route_registration.py`

---

## Task 1: Deterministic Selection Policy Refactoring

**Files:**
- Modify: `services/sprint_selection.py`
- Modify: `tests/test_sprint_selection.py`

- [ ] **Step 1: Write failing selection policy tests**
  Update `tests/test_sprint_selection.py` to remove references to `team_velocity_assumption` in test parameters, and verify:
  - Auto-selection respects `capacity_points` without any story count cap (e.g. can select more than 7 stories if they fit in points capacity).
  - Raising of `SPRINT_SELECTION_STORY_LIMIT_BLOCKED` is no longer a code path.
  Run:
  ```bash
  uv run --frozen pytest tests/test_sprint_selection.py -q
  ```
  Expected RED: signature/TypeError or missing field failures.

- [ ] **Step 2: Refactor `services/sprint_selection.py`**
  - Remove `_VELOCITY_STORY_LIMITS` and `_DEFAULT_STORY_LIMIT`.
  - Remove `team_velocity_assumption` and `story_limit` from `select_sprint_story_rows` signature and `_SelectionPolicy` / `SprintSelectionResult` dataclasses.
  - In `_select_auto`, remove the check against `policy.story_limit`.
  - In `_result`, remove `team_velocity_assumption` and `story_limit` fields from the `SprintSelectionResult` initialization.

- [ ] **Step 3: Verify GREEN**
  Run:
  ```bash
  uv run --frozen pytest tests/test_sprint_selection.py -q
  ```
  Expected GREEN.

- [ ] **Step 4: Commit Task 1**
  ```bash
  git add services/sprint_selection.py tests/test_sprint_selection.py
  git commit -m "refactor(sprint): remove velocity story limits"
  ```

---

## Task 2: SQLite Schema Migration

**Files:**
- Modify: `models/core.py`
- Modify: `db/migrations.py`
- Create: `tests/test_db_migrations_sprint_nullable_dates.py`

- [ ] **Step 1: Write failing model nullability regression**
  Add a focused assertion to the new migration test that a fresh `Sprint` can
  be constructed and persisted with `start_date=None` and `end_date=None`.
  Run:
  ```bash
  uv run --frozen pytest tests/test_db_migrations_sprint_nullable_dates.py -q
  ```
  Expected RED: test file or nullable model behavior is missing.

- [ ] **Step 2: Update Sprint model in `models/core.py`**
  - Update `start_date` and `end_date` in `Sprint` model class to be `date | None = Field(default=None, sa_type=Date, nullable=True)`.

- [ ] **Step 3: Write failing migration tests**
  Create `tests/test_db_migrations_sprint_nullable_dates.py` to verify:
  - Migrating a database with an old `sprints` table (`start_date DATE NOT NULL` and `end_date DATE NOT NULL`) runs successfully.
  - Existing sprint rows, foreign key references (product, team), and stories linked via `sprint_stories` are fully preserved.
  - Columns `start_date` and `end_date` in the migrated database are nullable.
  - API/runtime serializers preserve `None` for missing planned dates instead
    of rendering `"None"` or synthetic ranges.
  - The migration is idempotent (running it twice returns no actions on the second run).
  Run:
  ```bash
  uv run --frozen pytest tests/test_db_migrations_sprint_nullable_dates.py -q
  ```
  Expected RED.

- [ ] **Step 4: Implement SQLite table rebuild migration in `db/migrations.py`**
  - Write `migrate_sprint_nullable_dates(engine: Engine) -> list[str]` which:
    - Inspects the `sprints` table columns using `PRAGMA table_info`.
    - If `start_date` or `end_date` has `notnull == 1`, performs a table rebuild:
      - Drops `sprints__new` table if it exists.
      - Creates `sprints__new` table matching the new `Sprint` schema (nullable `start_date` and `end_date`).
      - Copies all columns from `sprints` to `sprints__new`.
      - Drops the old `sprints` table.
      - Renames `sprints__new` to `sprints`.
      - Restores lookup indexes and foreign keys.
    - Adds `migrate_sprint_nullable_dates` to `ensure_schema_current(engine)`.

- [ ] **Step 5: Verify GREEN**
  Run:
  ```bash
  uv run --frozen pytest tests/test_db_migrations_sprint_nullable_dates.py -q
  ```
  Expected GREEN.

- [ ] **Step 6: Commit Task 2**
  ```bash
  git add models/core.py db/migrations.py tests/test_db_migrations_sprint_nullable_dates.py
  git commit -m "fix(sprint): allow planned sprints without dates"
  ```

---

## Task 3: API & CLI Contract Updates

**Files:**
- Modify: `cli/main.py`
- Modify: `api.py`
- Modify: `routers/sprint.py`
- Modify: `services/agent_workbench/sprint_phase.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `tests/test_agent_workbench_cli.py`
- Modify: `tests/test_agent_workbench_command_schema.py`
- Modify: `tests/test_api_sprint_flow.py`
- Modify: `tests/test_api_route_registration.py`

- [ ] **Step 1: Write failing parser and schema tests**
  - Update `tests/test_agent_workbench_command_schema.py` and `tests/test_agent_workbench_cli.py` to verify:
    - `agileforge sprint generate` fails if `--sprint-duration-days` or `--team-velocity-assumption` is passed.
    - `agileforge sprint save` does not require/accept `--sprint-start-date`.
  - Update `tests/test_api_sprint_flow.py` and `tests/test_api_route_registration.py` to verify:
    - `POST /api/projects/{project_id}/sprint/generate` rejects requests containing `sprint_duration_days` or `team_velocity_assumption`.
    - `POST /api/projects/{project_id}/sprint/generate` rejects unknown extra fields with HTTP 422.
    - `POST /api/projects/{project_id}/sprint/save` rejects payloads containing `sprint_start_date`.
    - `POST /api/projects/{project_id}/sprint/save` rejects payloads missing any of:
      `attempt_id`, `expected_artifact_fingerprint`, `expected_state`, or `idempotency_key`.
    - `POST /api/projects/{project_id}/sprint/save` rejects stale `expected_artifact_fingerprint`.
    - `POST /api/projects/{project_id}/sprint/save` rejects stale `expected_state`.
    - `POST /api/projects/{project_id}/sprint/save` replays a repeated `idempotency_key`.
  Run:
  ```bash
  uv run --frozen pytest tests/test_agent_workbench_cli.py -q
  uv run --frozen pytest tests/test_api_sprint_flow.py -q
  ```
  Expected RED.

- [ ] **Step 2: Update CLI parsing and Command Registry**
  - In `cli/main.py`, remove parser options for `--sprint-duration-days` and `--team-velocity-assumption` on `sprint generate` subparser, and `--sprint-start-date` on `sprint save` subparser.
  - In `services/agent_workbench/command_registry.py`, remove these arguments from the schema registration.

- [ ] **Step 3: Update API request validation schemas and routes**
  - In `api.py` and `routers/sprint.py` request schemas, remove `sprint_duration_days` and `team_velocity_assumption` from generation requests.
  - Add forbidden-extra validation to affected Sprint request models, using the repo's active Pydantic style (`ConfigDict(extra="forbid")` for v2 models, or the equivalent local pattern if already present).
  - Replace `SprintSaveRequest` fields with:
    - `team_name: str = Field(min_length=1)`
    - `attempt_id: str = Field(min_length=1)`
    - `expected_artifact_fingerprint: str = Field(min_length=1)`
    - `expected_state: str = Field(min_length=1)`
    - `idempotency_key: str = Field(min_length=1)`
  - Route these guarded fields into `save_sprint_plan_service`.
  - Confirm `sprint_start_date` and other removed calendar fields return HTTP 422 through forbidden-extra validation.

- [ ] **Step 4: Update application facades and sprint runner**
  - In `services/agent_workbench/sprint_phase.py` and `services/agent_workbench/application.py`:
    - Remove duration and velocity arguments from `generate` / `run` signatures.
    - Map `max_story_points` user overrides or metrics-derived recommendations to `capacity_points` to feed FSM inputs.
    - Remove `sprint_start_date` from `save` signatures.
    - Require `attempt_id`, `expected_artifact_fingerprint`, `expected_state`, and `idempotency_key` through every API, CLI, and application save path.
    - Preserve existing guarded-save stale fingerprint, stale state, and idempotency replay semantics.

- [ ] **Step 5: Verify GREEN**
  Run:
  ```bash
  uv run --frozen pytest tests/test_agent_workbench_cli.py -q
  uv run --frozen pytest tests/test_api_sprint_flow.py -q
  ```
  Expected GREEN.

- [ ] **Step 6: Commit Task 3**
  ```bash
  git add cli/main.py api.py routers/sprint.py services/agent_workbench/sprint_phase.py services/agent_workbench/application.py services/agent_workbench/command_registry.py tests/test_agent_workbench_cli.py tests/test_agent_workbench_command_schema.py tests/test_api_sprint_flow.py tests/test_api_route_registration.py
  git commit -m "fix(sprint): enforce capacity API and guarded save contracts"
  ```

---

## Task 4: Sprint Planner Agent & FSM updates

**Files:**
- Modify: `orchestrator_agent/agent_tools/sprint_planner_tool/schemes.py`
- Modify: `orchestrator_agent/agent_tools/sprint_planner_tool/tools.py`
- Modify: `orchestrator_agent/agent_tools/sprint_planner_tool/instructions.txt`
- Modify: `orchestrator_agent/fsm/definitions.py`
- Modify: `orchestrator_agent/fsm/deterministic_tool_adapters.py`
- Modify: `services/phases/sprint_service.py`

- [ ] **Step 1: Write failing tool and FSM tests**
  - Write tests proving that calling the FSM/adapters with duration/velocity arguments fails or is rejected, and that the sprint planner schema conforms to `capacity_points`, `capacity_source`, and `capacity_basis`.
  Run FSM and tool tests:
  ```bash
  uv run --frozen pytest tests/test_agent_workbench_sprint_phase.py -q
  ```
  Expected RED.

- [ ] **Step 2: Update Sprint Planner Tool schemas & instructions**
  - In `orchestrator_agent/agent_tools/sprint_planner_tool/schemes.py`:
    - Remove `sprint_duration_days` and `team_velocity_assumption` from `SprintPlannerInput`.
    - Add `capacity_points`, `capacity_source`, and `capacity_basis` to `SprintPlannerInput`.
    - Remove `duration_days` from `SprintPlannerOutput`.
  - In `orchestrator_agent/agent_tools/sprint_planner_tool/tools.py`:
    - Refactor tool logic to use `capacity_points` directly as the story point constraint. Do not compute dates or use duration.
    - Update `SaveSprintPlanInput` to drop `sprint_start_date` and `sprint_duration_days`.
    - Persist planned Sprints without planned dates.
  - In `orchestrator_agent/agent_tools/sprint_planner_tool/instructions.txt`:
    - Clean up instructions and JSON examples to reflect the capacity-only contract.

- [ ] **Step 3: Update FSM Definitions and deterministic adapters**
  - In `orchestrator_agent/fsm/definitions.py`:
    - Clean up state text, examples, and prompt instructions to remove duration/velocity assumption language.
  - In `orchestrator_agent/fsm/deterministic_tool_adapters.py`:
    - Refactor the sprint planner adapter signature: remove `team_velocity_assumption` and `sprint_duration_days`.
    - Normalize inputs to pass `capacity_points` to the planner tool.

- [ ] **Step 4: Verify GREEN**
  Run:
  ```bash
  uv run --frozen pytest tests/test_agent_workbench_sprint_phase.py -q
  ```
  Expected GREEN.

- [ ] **Step 5: Commit Task 4**
  ```bash
  git add orchestrator_agent/agent_tools/sprint_planner_tool/schemes.py orchestrator_agent/agent_tools/sprint_planner_tool/tools.py orchestrator_agent/agent_tools/sprint_planner_tool/instructions.txt orchestrator_agent/fsm/definitions.py orchestrator_agent/fsm/deterministic_tool_adapters.py services/phases/sprint_service.py tests/test_agent_workbench_sprint_phase.py
  git commit -m "refactor(sprint): use capacity planner contract"
  ```

---

## Task 5: Capacity Error Contracts And Null Date Projections

**Files:**
- Modify: `services/sprint_selection.py`
- Modify: `services/agent_workbench/sprint_phase.py`
- Modify: `api.py`
- Modify: `frontend/project.js`
- Modify: `tests/test_sprint_selection.py`
- Modify: `tests/test_agent_workbench_sprint_phase.py`
- Modify: `tests/test_api_sprint_flow.py`

- [ ] **Step 1: Write failing capacity/dependency overflow tests**
  Add tests proving:
  - missing capacity before planner invocation returns `SPRINT_CAPACITY_REQUIRED`;
  - dependency closure that exceeds `capacity_points` returns a named capacity overflow error;
  - API/runtime sprint projections with null `start_date` / `end_date` expose null/empty values, not `"None"` or synthetic ranges.
  Run:
  ```bash
  uv run --frozen pytest tests/test_sprint_selection.py -q
  uv run --frozen pytest tests/test_agent_workbench_sprint_phase.py -q -k "capacity or projection"
  uv run --frozen pytest tests/test_api_sprint_flow.py -q -k "capacity or sprint"
  ```
  Expected RED.

- [ ] **Step 2: Implement named capacity errors**
  - Use `SPRINT_CAPACITY_REQUIRED` when no user or metrics-derived capacity exists.
  - Use a named overflow error, e.g. `SPRINT_CAPACITY_OVERFLOW`, when selected stories plus required dependency closure exceed `capacity_points`.
  - Include remediation text naming `--max-story-points` for manual override capacity.

- [ ] **Step 3: Implement null planned-date projections**
  - API/runtime projections preserve `None` for null planned dates.
  - Frontend rendering must show an empty value or omit planned date ranges for null planned dates.
  - Do not render the string `"None"`.

- [ ] **Step 4: Verify GREEN**
  Run the same focused commands from Step 1.
  Expected GREEN.

- [ ] **Step 5: Commit Task 5**
  ```bash
  git add services/sprint_selection.py services/agent_workbench/sprint_phase.py api.py frontend/project.js tests/test_sprint_selection.py tests/test_agent_workbench_sprint_phase.py tests/test_api_sprint_flow.py
  git commit -m "fix(sprint): name capacity errors and null date projections"
  ```

---

## Task 6: Frontend Layout & Form Updates

**Files:**
- Modify: `frontend/project.html`
- Modify: `frontend/project.js`

- [ ] **Step 1: Update form controls in `frontend/project.html`**
  - Remove fields for Sprint Duration (Days), Velocity Assumption, and Sprint Start Date from the generate/save forms.
  - Ensure the prefilled "Max Story Points" control remains.

- [ ] **Step 2: Update form interactions in `frontend/project.js`**
  - Remove duration, velocity, and start date fields from request payloads.
  - Send guarded Sprint save fields from the latest Sprint attempt/history:
    `attempt_id`, `expected_artifact_fingerprint`, `expected_state`, and `idempotency_key`.
  - Prefill "Max Story Points" with `recommended_next_sprint_points` from project metrics.
  - Display read-only capacity basis and warning messages when metrics are insufficient.
  - Remove frontend validation/guard blocking saving on missing start date.
  - Keep save disabled until guarded save metadata is available.

- [ ] **Step 3: Verify frontend syntax**
  Run:
  ```bash
  node --check frontend/project.js
  ```
  Expected GREEN.

- [ ] **Step 4: Commit Task 6**
  ```bash
  git add frontend/project.html frontend/project.js
  git commit -m "feat(sprint): show capacity planning UI"
  ```

---

## Task 7: Benchmarks, Prompt grep, & Final Verification

**Files:**
- Modify: `scripts/benchmark_sprint_planning.py`
- Modify: `tests/test_agent_workbench_command_schema.py` (or create a dedicated contract test file)

- [ ] **Step 1: Update benchmark scripts**
  - In `scripts/benchmark_sprint_planning.py`, remove references to duration/velocity assumptions and use `capacity_points`.

- [ ] **Step 2: Add prompt grep contract test**
  - Add a test (e.g. in `tests/test_agent_workbench_command_schema.py` or `tests/test_db_migrations_sprint_nullable_dates.py`) that performs a search over all prompt `.txt` files under `orchestrator_agent/` and FSM definitions to assert that they do not contain forbidden strings: `sprint_duration_days`, `duration_days`, `team_velocity_assumption`, `velocity assumption`, or `sprint_start_date`.

- [ ] **Step 3: Run full verification suite**
  Run all checks and linting:
  ```bash
  uv run --frozen pyrepo-check --all
  uv run --frozen pytest
  git diff --check
  ```
  Expected GREEN.

- [ ] **Step 4: Commit Task 7**
  ```bash
  git add scripts/benchmark_sprint_planning.py tests/test_agent_workbench_command_schema.py
  git commit -m "test(sprint): lock capacity planning contracts"
  ```

---

## Final Done Criteria

- Deterministic Sprint selection has no velocity-based story limits or velocity assumptions.
- SQLite database migrations safely convert `start_date` and `end_date` columns in `sprints` to nullable.
- API and CLI generate/save contracts do not accept calendar duration/velocity assumption fields, and planned start date is removed.
- API and CLI Sprint save require guarded review/idempotency fields: `attempt_id`, `expected_artifact_fingerprint`, `expected_state`, and `idempotency_key`.
- Affected Sprint API request models reject removed/unknown fields with HTTP 422.
- SaveSprintPlanInput and save tool persistence no longer accept start date or duration fields.
- Null planned dates are serialized/rendered as clean null/empty values, not as `"None"` or synthetic date ranges.
- Missing capacity and capacity-overflow paths use named errors.
- FSM definitions, deterministic adapters, and sprint planner tool prompt context are fully aligned with the capacity-based contract.
- Frontend UI removes Sprint duration, start date, and velocity band inputs, showing only capacity recommendation metrics and a capacity override.
- Automated grep test ensures prompt instructions contain no calendar duration or velocity assumptions.
- All unit and integration tests pass, and `pyrepo-check --all` is clean.
