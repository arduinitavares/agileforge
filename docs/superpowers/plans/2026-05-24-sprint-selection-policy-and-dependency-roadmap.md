# Sprint Selection Policy And Dependency Roadmap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Sprint generation from producing incoherent story selections by making AgileForge own Sprint scope selection before the LLM runs, while preserving the path to explicit story dependency graphs.

**Architecture:** Phase 1 adds a deterministic Sprint selection policy: AgileForge selects the sprint cohort from planning-ready stories, passes only that locked cohort to the Sprint Planner, and validates that the LLM output uses exactly those story IDs. Future phases add first-class persisted story dependencies and upgrade selection from rank-prefix rules to DAG/topological rules.

**Tech Stack:** Python, SQLModel, Pydantic, pytest, AgileForge CLI, existing Sprint runtime and agent-workbench services.

---

## Scope And Execution Boundary

Implement **Phase 1 only** in the next code change:

1. Deterministic pre-LLM Sprint selection.
2. Parent group/slot hints in Sprint planner input.
3. Prompt update that removes LLM selection authority.
4. Runtime validator that blocks if the LLM adds, drops, or changes selected story IDs.
5. CLI documentation update.

Do **not** implement the explicit dependency graph in this slice. It is recorded below so the architecture does not drift or get forgotten.

## File Map

Phase 1 files:

- Create: `services/sprint_selection.py`
  - Pure selection helpers. No DB access. Owns velocity story limits, priority grouping, greedy prefix selection, and selected-ID exact-match validation helpers.
- Modify: `services/sprint_input.py`
  - Calls `services.sprint_selection` after candidates are normalized. Auto-selects when `selected_story_ids` is absent. Preserves manual override behavior when `selected_story_ids` is present. Adds `parent_group` and `group_slot` to each input story.
- Modify: `orchestrator_agent/agent_tools/sprint_planner_tool/schemes.py`
  - Adds optional `parent_group` and `group_slot` fields to `SprintPlannerStory`.
- Modify: `orchestrator_agent/agent_tools/sprint_planner_tool/instructions.txt`
  - Changes Sprint Planner role from "select and decompose" to "explain and decompose the preselected cohort."
- Modify: `services/sprint_runtime.py`
  - Validates that `SprintPlannerOutput.selected_stories[*].story_id` exactly matches the locked `available_stories[*].story_id` from input.
- Modify: `docs/agent-cli-manual.md`
  - Documents that `sprint generate` auto-selects a locked cohort unless `--selected-story-ids` is supplied.
- Create or modify: `tests/test_sprint_selection.py`
  - Unit tests for the pure selector.
- Modify: `tests/test_sprint_runtime.py`
  - Runtime regression tests for exact selected-ID validation and input context behavior.
- Modify: `tests/test_sprint_planner_schemes.py`
  - Schema regression test for `parent_group` and `group_slot`.

Future dependency graph files:

- Modify: `models/core.py`
  - Add a first-class story dependency model or equivalent relationship table.
- Modify: `db/migrations.py`
  - Add schema migration and legacy backfill guard.
- Modify: `orchestrator_agent/agent_tools/user_story_writer_tool/schemes.py`
  - Allow story artifacts to declare dependency candidates.
- Modify: `orchestrator_agent/agent_tools/user_story_writer_tool/tools.py`
  - Resolve dependency candidates during story save.
- Modify: `services/agent_workbench/story_phase.py`
  - Add a repair/review command path for existing story dependencies.
- Modify: `services/sprint_selection.py`
  - Replace rank-prefix selection with dependency-closed DAG selection once dependency rows exist.

---

## Phase 1: Locked Deterministic Sprint Selection

### Task 1: Add Pure Sprint Selection Policy

**Files:**
- Create: `services/sprint_selection.py`
- Test: `tests/test_sprint_selection.py`

- [ ] **Step 1: Write tests for automatic prefix selection**

Create `tests/test_sprint_selection.py` with these initial tests:

```python
from __future__ import annotations

from services.sprint_selection import (
    SprintSelectionError,
    derive_parent_group,
    derive_group_slot,
    select_sprint_story_rows,
)


def _row(story_id: int, priority: int, points: int) -> dict[str, object]:
    return {
        "story_id": story_id,
        "story_title": f"Story {story_id}",
        "priority": priority,
        "story_points": points,
    }


def test_derive_priority_group_metadata_from_rank_priority() -> None:
    assert derive_parent_group(101) == 1
    assert derive_group_slot(101) == 1
    assert derive_parent_group(1002) == 10
    assert derive_group_slot(1002) == 2


def test_auto_selection_uses_priority_prefix_and_capacity() -> None:
    rows = [_row(66, 101, 1), _row(85, 102, 3), _row(67, 201, 3)]

    result = select_sprint_story_rows(
        rows,
        team_velocity_assumption="Medium",
        max_story_points=4,
        selected_story_ids=[],
    )

    assert [row["story_id"] for row in result.selected_rows] == [66, 85]
    assert result.mode == "auto"
    assert result.story_points_used == 4
    assert result.excluded_story_ids == [67]


def test_auto_selection_stops_instead_of_skipping_over_capacity_story() -> None:
    rows = [_row(1, 101, 2), _row(2, 102, 5), _row(3, 103, 1)]

    result = select_sprint_story_rows(
        rows,
        team_velocity_assumption="High",
        max_story_points=3,
        selected_story_ids=[],
    )

    assert [row["story_id"] for row in result.selected_rows] == [1]
    assert result.excluded_story_ids == [2, 3]


def test_auto_selection_blocks_when_first_story_exceeds_explicit_capacity() -> None:
    rows = [_row(1, 101, 5), _row(2, 102, 1)]

    try:
        select_sprint_story_rows(
            rows,
            team_velocity_assumption="Low",
            max_story_points=3,
            selected_story_ids=[],
        )
    except SprintSelectionError as exc:
        assert exc.code == "SPRINT_SELECTION_CAPACITY_BLOCKED"
        assert exc.details["blocking_story_id"] == 1
    else:
        raise AssertionError("Expected SprintSelectionError")


def test_manual_selection_preserves_explicit_story_order() -> None:
    rows = [_row(1, 101, 2), _row(2, 102, 3), _row(3, 201, 1)]

    result = select_sprint_story_rows(
        rows,
        team_velocity_assumption="Medium",
        max_story_points=3,
        selected_story_ids=[3, 1],
    )

    assert [row["story_id"] for row in result.selected_rows] == [3, 1]
    assert result.mode == "manual"
    assert result.story_points_used == 3
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
pytest tests/test_sprint_selection.py -q
```

Expected: FAIL because `services.sprint_selection` does not exist.

- [ ] **Step 3: Implement the pure selector**

Create `services/sprint_selection.py`:

```python
"""Pure Sprint selection policy helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


_VELOCITY_STORY_LIMITS: dict[str, int] = {
    "Low": 3,
    "Medium": 5,
    "High": 7,
}


class SprintSelectionError(ValueError):
    """Raised when AgileForge cannot produce a safe locked Sprint selection."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class SprintSelectionResult:
    """Deterministic Sprint selection result."""

    mode: str
    selected_rows: list[dict[str, Any]]
    selected_story_ids: list[int]
    excluded_story_ids: list[int]
    story_points_used: int
    max_story_points: int | None
    team_velocity_assumption: str
    story_limit: int
    warnings: list[dict[str, Any]] = field(default_factory=list)


def derive_parent_group(priority: int | None) -> int | None:
    """Return the parent group encoded by rank-style priority."""
    if priority is None or priority < 100:
        return None
    return priority // 100


def derive_group_slot(priority: int | None) -> int | None:
    """Return the child slot encoded by rank-style priority."""
    if priority is None or priority < 100:
        return None
    slot = priority % 100
    return slot or None


def select_sprint_story_rows(
    rows: list[dict[str, Any]],
    *,
    team_velocity_assumption: str,
    max_story_points: int | None,
    selected_story_ids: list[int],
) -> SprintSelectionResult:
    """Select the locked Sprint cohort before the LLM runs."""
    story_limit = _VELOCITY_STORY_LIMITS.get(team_velocity_assumption, 5)
    by_id = {
        int(row["story_id"]): row
        for row in rows
        if isinstance(row, dict) and row.get("story_id") is not None
    }

    if selected_story_ids:
        selected_rows = [by_id[story_id] for story_id in selected_story_ids]
        return _result(
            mode="manual",
            selected_rows=selected_rows,
            all_rows=rows,
            max_story_points=max_story_points,
            team_velocity_assumption=team_velocity_assumption,
            story_limit=story_limit,
        )

    selected_rows: list[dict[str, Any]] = []
    used_points = 0
    for row in rows:
        row_points = int(row.get("story_points") or 0)
        if row_points <= 0:
            raise SprintSelectionError(
                code="SPRINT_SELECTION_UNSIZED_STORY",
                message="Sprint selection requires positive story_points.",
                details={"story_id": row.get("story_id")},
            )
        if len(selected_rows) >= story_limit:
            break
        if max_story_points is not None and used_points + row_points > max_story_points:
            if not selected_rows:
                raise SprintSelectionError(
                    code="SPRINT_SELECTION_CAPACITY_BLOCKED",
                    message=(
                        "The highest-priority story exceeds the explicit Sprint "
                        "capacity. Increase --max-story-points or split the story."
                    ),
                    details={
                        "blocking_story_id": row.get("story_id"),
                        "story_points": row_points,
                        "max_story_points": max_story_points,
                    },
                )
            break
        selected_rows.append(row)
        used_points += row_points

    if not selected_rows:
        raise SprintSelectionError(
            code="SPRINT_SELECTION_EMPTY",
            message="Sprint selection produced no stories.",
        )

    return _result(
        mode="auto",
        selected_rows=selected_rows,
        all_rows=rows,
        max_story_points=max_story_points,
        team_velocity_assumption=team_velocity_assumption,
        story_limit=story_limit,
    )


def _result(
    *,
    mode: str,
    selected_rows: list[dict[str, Any]],
    all_rows: list[dict[str, Any]],
    max_story_points: int | None,
    team_velocity_assumption: str,
    story_limit: int,
) -> SprintSelectionResult:
    selected_ids = [int(row["story_id"]) for row in selected_rows]
    selected_id_set = set(selected_ids)
    excluded_ids = [
        int(row["story_id"])
        for row in all_rows
        if int(row["story_id"]) not in selected_id_set
    ]
    return SprintSelectionResult(
        mode=mode,
        selected_rows=selected_rows,
        selected_story_ids=selected_ids,
        excluded_story_ids=excluded_ids,
        story_points_used=sum(int(row.get("story_points") or 0) for row in selected_rows),
        max_story_points=max_story_points,
        team_velocity_assumption=team_velocity_assumption,
        story_limit=story_limit,
    )
```

- [ ] **Step 4: Run selector tests**

Run:

```bash
pytest tests/test_sprint_selection.py -q
```

Expected: PASS.

### Task 2: Wire Selection Policy Into Sprint Input

**Files:**
- Modify: `services/sprint_input.py`
- Test: `tests/test_sprint_runtime.py`

- [ ] **Step 1: Add failing input-context tests**

Add tests to `tests/test_sprint_runtime.py`:

```python
def test_prepare_sprint_input_context_auto_selects_locked_priority_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7
        return {
            "success": True,
            "count": 3,
            "stories": [
                {"story_id": 66, "story_title": "Budget", "priority": 101, "story_points": 1},
                {"story_id": 85, "story_title": "Live workflow", "priority": 102, "story_points": 3},
                {"story_id": 67, "story_title": "Capture", "priority": 201, "story_points": 3},
            ],
        }

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )

    prepared = sprint_input.prepare_sprint_input_context(
        product_id=7,
        team_velocity_assumption="Medium",
        sprint_duration_days=14,
        user_context=None,
        max_story_points=4,
        include_task_decomposition=True,
        selected_story_ids=None,
    )

    assert prepared["success"] is True
    assert prepared["selected_story_ids"] == [66, 85]
    assert prepared["selection_policy"]["mode"] == "auto"
    assert [
        story["story_id"] for story in prepared["input_context"]["available_stories"]
    ] == [66, 85]
    assert prepared["input_context"]["available_stories"][0]["parent_group"] == 1
    assert prepared["input_context"]["available_stories"][0]["group_slot"] == 1


def test_prepare_sprint_input_context_returns_selection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7
        return {
            "success": True,
            "count": 1,
            "stories": [
                {"story_id": 66, "story_title": "Budget", "priority": 101, "story_points": 5},
            ],
        }

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )

    prepared = sprint_input.prepare_sprint_input_context(
        product_id=7,
        team_velocity_assumption="Low",
        sprint_duration_days=14,
        user_context=None,
        max_story_points=3,
        include_task_decomposition=True,
        selected_story_ids=None,
    )

    assert prepared["success"] is False
    assert prepared["error_code"] == "SPRINT_SELECTION_CAPACITY_BLOCKED"
    assert prepared["selection_details"]["blocking_story_id"] == 66
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
pytest tests/test_sprint_runtime.py::test_prepare_sprint_input_context_auto_selects_locked_priority_prefix tests/test_sprint_runtime.py::test_prepare_sprint_input_context_returns_selection_error -q
```

Expected: FAIL because `prepare_sprint_input_context` has not been wired to the selector.

- [ ] **Step 3: Update `services/sprint_input.py` imports**

Add:

```python
from services.sprint_selection import (
    SprintSelectionError,
    derive_group_slot,
    derive_parent_group,
    select_sprint_story_rows,
)
```

- [ ] **Step 4: Replace selected row selection logic**

Inside `prepare_sprint_input_context`, keep the existing invalid `selected_story_ids` check, then call the selector:

```python
    try:
        selection = select_sprint_story_rows(
            candidate_rows,
            team_velocity_assumption=normalize_velocity(
                options["team_velocity_assumption"]
            ),
            max_story_points=normalize_positive_int(options["max_story_points"]),
            selected_story_ids=normalized_selected_ids,
        )
    except SprintSelectionError as exc:
        return {
            "success": False,
            "error_code": exc.code,
            "message": str(exc),
            "selection_details": exc.details,
            "candidate_result": candidate_result,
            "input_context": {},
        }

    selected_rows = selection.selected_rows
```

- [ ] **Step 5: Add group metadata to each story dict**

In the `available_stories` dict comprehension, add:

```python
                "parent_group": derive_parent_group(int(row["priority"])),
                "group_slot": derive_group_slot(int(row["priority"])),
```

- [ ] **Step 6: Return selection policy metadata**

Extend the success return:

```python
        "selected_story_ids": selection.selected_story_ids,
        "selection_policy": {
            "mode": selection.mode,
            "selected_story_ids": selection.selected_story_ids,
            "excluded_story_ids": selection.excluded_story_ids,
            "story_points_used": selection.story_points_used,
            "max_story_points": selection.max_story_points,
            "team_velocity_assumption": selection.team_velocity_assumption,
            "story_limit": selection.story_limit,
            "warnings": selection.warnings,
        },
```

- [ ] **Step 7: Run focused input tests**

Run:

```bash
pytest tests/test_sprint_runtime.py::test_prepare_sprint_input_context_auto_selects_locked_priority_prefix tests/test_sprint_runtime.py::test_prepare_sprint_input_context_returns_selection_error -q
```

Expected: PASS.

### Task 3: Update Sprint Planner Input Schema

**Files:**
- Modify: `orchestrator_agent/agent_tools/sprint_planner_tool/schemes.py`
- Test: `tests/test_sprint_planner_schemes.py`

- [ ] **Step 1: Add schema test for group metadata**

Extend `test_input_schema_accepts_optional_fields` story payload with:

```python
                "parent_group": 1,
                "group_slot": 1,
```

Then add assertions:

```python
    assert model.available_stories[0].parent_group == 1
    assert model.available_stories[0].group_slot == 1
```

- [ ] **Step 2: Run the schema test and verify it fails**

Run:

```bash
pytest tests/test_sprint_planner_schemes.py::test_input_schema_accepts_optional_fields -q
```

Expected: FAIL if extra input fields are rejected or attributes are missing.

- [ ] **Step 3: Add optional fields to `SprintPlannerStory`**

Add these fields after `priority`:

```python
    parent_group: Annotated[
        int | None,
        Field(
            default=None,
            description="Rank-derived parent group, usually priority // 100.",
        ),
    ]
    group_slot: Annotated[
        int | None,
        Field(
            default=None,
            description="Rank-derived child slot inside parent group, usually priority % 100.",
        ),
    ]
```

- [ ] **Step 4: Run the schema test**

Run:

```bash
pytest tests/test_sprint_planner_schemes.py::test_input_schema_accepts_optional_fields -q
```

Expected: PASS.

### Task 4: Remove LLM Selection Authority From Instructions

**Files:**
- Modify: `orchestrator_agent/agent_tools/sprint_planner_tool/instructions.txt`

- [ ] **Step 1: Replace selection language**

Replace the current "Selection Logic (The Pull)" section with:

```text
3.  **Locked Selection Contract:**
    * The `available_stories` list has already been selected by AgileForge's deterministic Sprint selection policy.
    * Your job is NOT to choose a different Sprint scope.
    * You MUST emit exactly one `selected_stories` entry for every input story in `available_stories`.
    * You MUST NOT add stories that are not in `available_stories`.
    * You MUST NOT drop stories from `available_stories`.
    * Emit `deselected_stories: []` because deselection happened before this planner call.
    * Use `parent_group` and `group_slot` only to explain sequencing and cohesion; do not reinterpret them as hard dependency truth.
```

- [ ] **Step 2: Update process wording above the algorithm**

In the role/goal section, change wording from "convert a list of Product Backlog Items into a committed Sprint Backlog" to:

```text
Your goal is to convert AgileForge's locked Sprint story cohort into a coherent Sprint Goal and implementation task plan.
```

- [ ] **Step 3: Run instruction smoke tests**

Run:

```bash
pytest tests/test_sprint_runtime.py tests/test_sprint_planner_schemes.py -q
```

Expected: PASS.

### Task 5: Add Runtime Exact Selected-ID Validator

**Files:**
- Modify: `services/sprint_runtime.py`
- Test: `tests/test_sprint_runtime.py`

- [ ] **Step 1: Add failing validation test**

Add to `tests/test_sprint_runtime.py`:

```python
@pytest.mark.asyncio
async def test_sprint_runtime_blocks_output_that_changes_locked_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 101,
                    "story_points": 3,
                    "acceptance_criteria": "Persist deltas",
                    "evaluated_invariant_ids": ["INV-12"],
                },
                {
                    "story_id": 13,
                    "story_title": "Forbidden Reselection",
                    "priority": 102,
                    "story_points": 3,
                    "acceptance_criteria": "Should not be selected",
                    "evaluated_invariant_ids": ["INV-13"],
                },
            ],
        }

    async def fake_run_agent(*args: object, **kwargs: object) -> str:
        _ = args, kwargs
        payload = json.loads(_valid_sprint_output())
        payload["selected_stories"][0]["story_id"] = 13
        payload["selected_stories"][0]["story_title"] = "Forbidden Reselection"
        return json.dumps(payload)

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "run_agent", fake_run_agent)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        team_velocity_assumption="Low",
        sprint_duration_days=14,
        max_story_points=3,
        include_task_decomposition=True,
        selected_story_ids=None,
        user_input=None,
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert "selected stories do not match locked Sprint selection" in result["error"]
```

- [ ] **Step 2: Run the validation test and verify it fails**

Run:

```bash
pytest tests/test_sprint_runtime.py::test_sprint_runtime_blocks_output_that_changes_locked_selection -q
```

Expected: FAIL because `_validate_sprint_output` does not compare selected IDs.

- [ ] **Step 3: Add validator helper to `services/sprint_runtime.py`**

Add near `_validate_sprint_output`:

```python
def _selected_story_ids_from_input(prepared: _PreparedSprintPayload) -> list[int]:
    return [int(story.story_id) for story in prepared.payload.available_stories]


def _selected_story_ids_from_output(output_model: SprintPlannerOutput) -> list[int]:
    return [int(story.story_id) for story in output_model.selected_stories]
```

- [ ] **Step 4: Call validator after Pydantic output validation**

After `output_model = SprintPlannerOutput.model_validate(parsed)` succeeds, add:

```python
    expected_story_ids = _selected_story_ids_from_input(prepared)
    actual_story_ids = _selected_story_ids_from_output(output_model)
    if actual_story_ids != expected_story_ids:
        selection_errors = [
            (
                "Sprint output validation failed: selected stories do not match "
                "locked Sprint selection"
            ),
            f"expected selected_story_ids={expected_story_ids}",
            f"actual selected_story_ids={actual_story_ids}",
        ]
        structured_errors = [{"msg": error} for error in selection_errors]
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="output_validation",
            details=_FailureDetails(
                message=selection_errors[0],
                raw_text=raw_text,
                validation_errors=_normalize_validation_errors(structured_errors),
                public_validation_errors=_compact_public_validation_errors(
                    selection_errors
                ),
            ),
        )
```

- [ ] **Step 5: Run focused validation test**

Run:

```bash
pytest tests/test_sprint_runtime.py::test_sprint_runtime_blocks_output_that_changes_locked_selection -q
```

Expected: PASS.

### Task 6: Update CLI Documentation

**Files:**
- Modify: `docs/agent-cli-manual.md`

- [ ] **Step 1: Add Sprint selection contract text**

In the Sprint section, add:

```markdown
#### Sprint Selection Contract

`agileforge sprint generate` does not ask the model to choose arbitrary Sprint scope. AgileForge first locks a deterministic story cohort from planning-ready candidates:

- default mode: priority/rank prefix, bounded by `--max-story-points` when provided and by the velocity story-count band
- manual mode: exact `--selected-story-ids`, preserving caller order

The Sprint Planner receives only the locked cohort. Its job is to write the Sprint Goal, explain cohesion, and decompose the selected stories into tasks. If the model adds, drops, or changes selected story IDs, AgileForge fails the run with `MUTATION_FAILED`.

The current selector uses rank/group order as a temporary planning policy. Rank is not treated as dependency truth. Explicit story dependency graphs are planned as the next architecture slice.
```

- [ ] **Step 2: Run docs grep sanity check**

Run:

```bash
rg -n "Sprint Selection Contract|locked cohort|dependency graphs" docs/agent-cli-manual.md
```

Expected: all three phrases appear.

### Task 7: Run Phase 1 Verification

**Files:**
- Test only.

- [ ] **Step 1: Run focused tests**

Run:

```bash
pytest tests/test_sprint_selection.py tests/test_sprint_runtime.py tests/test_sprint_planner_schemes.py tests/test_sprint_phase_service.py tests/test_api_sprint_flow.py -q
```

Expected: PASS.

- [ ] **Step 2: Run full test suite**

Run:

```bash
pytest -q
```

Expected: PASS.

- [ ] **Step 3: Run lint and format checks**

Run:

```bash
ruff check .
ruff format --check .
git diff --check
```

Expected: all pass.

- [ ] **Step 4: Live caRtola smoke test without saving**

Run:

```bash
cd /Users/aaat/projects/caRtola
agileforge sprint generate --project-id 2
agileforge sprint history --project-id 2 > sprint-history-after-selection.json
python - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("sprint-history-after-selection.json").read_text())
attempts = payload["data"]["attempts"]
latest = attempts[-1]
artifact = latest["output_artifact"]
print({
    "attempt_id": latest["attempt_id"],
    "is_complete": latest["is_complete"],
    "selected_story_ids": [
        story["story_id"] for story in artifact["selected_stories"]
    ],
})
PY
```

Expected: generated attempt is complete or fails closed for provider/runtime reasons. If complete, selected IDs must be the locked prefix cohort rather than the previous bad mix that skipped foundational stories.

---

## Phase 2: Explicit Story Dependency Graph

Do not implement this phase with Phase 1. It is the next architecture slice after locked Sprint selection is stable.

### Task 8: Add First-Class Dependency Storage

**Files:**
- Modify: `models/core.py`
- Modify: `db/migrations.py`
- Create: `tests/test_story_dependencies_model.py`

- [ ] **Step 1: Add a relational dependency model**

Add a table like:

```python
class UserStoryDependency(SQLModel, table=True):
    """Explicit prerequisite edge between two active user stories."""

    __tablename__ = "user_story_dependencies"  # type: ignore[assignment]

    dependency_id: int | None = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="products.product_id", index=True)
    dependent_story_id: int = Field(foreign_key="user_stories.story_id", index=True)
    prerequisite_story_id: int = Field(foreign_key="user_stories.story_id", index=True)
    dependency_kind: str = Field(default="blocks", index=True)
    reason: str | None = Field(default=None, sa_type=Text)
    source: str = Field(default="story_review", index=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )
```

- [ ] **Step 2: Add migration**

Migration must create the table and enforce no duplicate edge for:

```text
product_id, dependent_story_id, prerequisite_story_id
```

- [ ] **Step 3: Add model tests**

Tests must verify:

- duplicate dependency edges are rejected or deduplicated
- dependencies cannot point to the same story ID
- only active, non-superseded stories are valid graph nodes

### Task 9: Let Story Artifacts Propose Dependency Candidates

**Files:**
- Modify: `orchestrator_agent/agent_tools/user_story_writer_tool/schemes.py`
- Modify: `orchestrator_agent/agent_tools/user_story_writer_tool/tools.py`
- Modify: `services/phases/story_service.py`
- Test: `tests/test_story_phase_service.py`

- [ ] **Step 1: Add dependency candidate schema**

Add a story-output field such as:

```python
dependency_candidates: list[StoryDependencyCandidate] = Field(default_factory=list)
```

where each candidate contains:

```python
prerequisite_ref: str
reason: str
confidence: Literal["explicit", "inferred"]
```

- [ ] **Step 2: Resolve candidates during story save**

Resolve `prerequisite_ref` by active story ID, title, `source_requirement`, and `refinement_slot`. Do not persist unresolved inferred edges as active dependencies.

- [ ] **Step 3: Fail closed on explicit unresolved dependencies**

If the story output marks a dependency as `confidence="explicit"` and AgileForge cannot resolve it, story save must fail with a diagnostic listing the unresolved dependency reference.

### Task 10: Add Dependency Review And Repair CLI

**Files:**
- Modify: `cli/main.py`
- Modify: `services/agent_workbench/story_phase.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `docs/agent-cli-manual.md`
- Test: `tests/test_agent_workbench_story_phase.py`
- Test: `tests/test_agent_workbench_cli.py`

- [ ] **Step 1: Add inspect command**

Add:

```bash
agileforge story dependencies --project-id <project_id>
```

It must show dependency edges, unresolved candidates, cycle status, and affected story IDs.

- [ ] **Step 2: Add repair command**

Add:

```bash
agileforge story dependencies repair --project-id <project_id> --expected-state <state> --idempotency-key <key>
```

It must propose or persist reviewed dependency edges for existing projects like caRtola.

- [ ] **Step 3: Add cycle diagnostics**

Cycles must block Sprint planning until resolved. The diagnostic must include the cycle path.

---

## Phase 3: DAG-Based Sprint Selection

Do not implement this phase until Phase 2 creates trusted dependency data.

### Task 11: Upgrade Selector To Dependency-Closed DAG Selection

**Files:**
- Modify: `services/sprint_selection.py`
- Modify: `services/sprint_input.py`
- Test: `tests/test_sprint_selection.py`

- [ ] **Step 1: Load dependency edges into selector input**

`prepare_sprint_input_context` must pass:

```python
dependency_edges = {
    dependent_story_id: set(prerequisite_story_ids),
}
```

- [ ] **Step 2: Validate graph before selection**

The selector must fail closed when:

- a dependency points to a missing story
- a dependency points to a superseded story
- the graph contains a cycle

- [ ] **Step 3: Select dependency-closed cohorts**

The selector must only select a story if all prerequisites are either:

- already completed
- already selected in the same Sprint

- [ ] **Step 4: Keep exact LLM selected-ID validation**

The runtime validator from Phase 1 remains mandatory. The LLM still cannot add or drop story IDs.

### Task 12: Add Manual Override Diagnostics

**Files:**
- Modify: `services/sprint_selection.py`
- Modify: `services/sprint_input.py`
- Modify: `docs/agent-cli-manual.md`
- Test: `tests/test_sprint_selection.py`

- [ ] **Step 1: Validate `--selected-story-ids` against graph**

Manual selections that skip prerequisites must return warnings by default.

- [ ] **Step 2: Add explicit force semantics**

If the product decision is to allow unsafe manual overrides, add:

```bash
agileforge sprint generate --selected-story-ids 1,2,3 --force-selection
```

The command must write a mutation/audit event explaining which dependency rules were overridden.

---

## Self-Review

- Spec coverage: Phase 1 covers the immediate caRtola-class failure by removing LLM selection authority and validating exact selected IDs. Phases 2 and 3 cover the long-term explicit dependency graph and topological selection path.
- Placeholder scan: no `TBD`, `TODO`, or vague implementation placeholders remain. Future work is expressed as concrete tasks and files.
- Type consistency: Phase 1 uses `SprintSelectionResult`, `SprintSelectionError`, `parent_group`, `group_slot`, and `selected_story_ids` consistently across selector, input context, schema, and runtime validation.
- Scope check: Phase 1 is a focused implementation slice. Dependency persistence is intentionally separated because it requires DB migration, story artifact changes, and repair tooling.
