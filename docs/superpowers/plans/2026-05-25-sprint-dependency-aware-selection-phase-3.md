# Sprint Dependency-Aware Selection Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Sprint generation select a dependency-closed, topologically ordered story cohort before the LLM runs.

**Architecture:** AgileForge keeps Sprint scope selection deterministic in `services/sprint_selection.py`. Phase 3 upgrades the existing rank-prefix selector so it honors active story dependencies from Phase 2, blocks unsafe manual selections, and passes only dependency-safe locked cohorts to the Sprint Planner. The LLM remains responsible only for Sprint goal, explanation, and task decomposition.

**Tech Stack:** Python, SQLModel, Pydantic, pytest, AgileForge CLI, existing Sprint runtime, existing frontend static UI.

---

## Scope Boundary

Implement this phase:

1. Dependency-closed automatic Sprint selection.
2. Defensive DAG/cycle validation in pure selector.
3. Manual `--selected-story-ids` closure validation and safe topological ordering.
4. Selection-policy diagnostics exposed through existing Sprint generate/history payloads.
5. Sprint Planner prompt/schema text updated to treat input order as locked dependency-safe order.
6. Minimal UI compatibility: display dependency metadata and avoid manual checkbox selections that omit visible prerequisites.
7. CLI docs updated.

Do not implement:

1. New DB schema. Phase 2 already created `user_story_dependencies`.
2. LLM-driven dependency inference in Sprint.
3. Auto-repair of bad active dependency graphs. Phase 2 inspect/propose/apply remains the repair path.
4. Full UI dependency graph editor. CLI remains source of truth for dependency review.

## File Map

- Modify: `services/sprint_selection.py`
  - Owns pure dependency-aware selection, topological ordering, manual closure validation, and selection diagnostics.
- Modify: `services/sprint_input.py`
  - Passes candidate dependency fields into selector and exposes richer `selection_policy`.
- Modify: `services/sprint_runtime.py`
  - Preserves locked exact selection validation and includes selection diagnostics in runtime attempts.
- Modify: `orchestrator_agent/agent_tools/sprint_planner_tool/instructions.txt`
  - Clarifies that input stories are dependency-closed and ordered.
- Modify: `orchestrator_agent/agent_tools/sprint_planner_tool/schemes.py`
  - Improves field descriptions only if needed; no new required schema fields expected.
- Modify: `frontend/project.js`
  - Shows dependency badges in Sprint candidates and keeps manual checkbox selection dependency-closed for visible candidates.
- Modify: `docs/agent-cli-manual.md`
  - Documents dependency-aware Sprint generation and manual override behavior.
- Test: `tests/test_sprint_selection.py`
  - New and existing pure selector tests.
- Test: `tests/test_sprint_runtime.py`
  - Selection-policy propagation and runtime exact-lock regression.
- Test: `tests/test_deterministic_tool_adapters.py`
  - Adapter input shape remains stable with dependency metadata.
- Test: `tests/test_agent_workbench_phase1_integration.py`
  - CLI flow remains valid.

---

## Task 1: Pure Dependency Graph Selection

**Files:**
- Modify: `services/sprint_selection.py`
- Test: `tests/test_sprint_selection.py`

- [ ] **Step 1: Add failing tests for dependency-closed auto selection**

Append these tests to `tests/test_sprint_selection.py`:

```python
def _dep_row(
    story_id: int,
    priority: int,
    points: int,
    *,
    blocked_by: list[int] | None = None,
) -> dict[str, object]:
    return {
        "story_id": story_id,
        "story_title": f"Story {story_id}",
        "priority": priority,
        "story_points": points,
        "blocked_by_story_ids": blocked_by or [],
        "prerequisite_story_ids": blocked_by or [],
        "dependency_status": "blocked" if blocked_by else "ready",
    }


def test_auto_selection_promotes_prerequisite_before_dependent() -> None:
    rows = [
        _dep_row(85, 101, 3, blocked_by=[66]),
        _dep_row(66, 201, 1),
        _dep_row(79, 301, 2),
    ]

    result = select_sprint_story_rows(
        rows,
        team_velocity_assumption="Medium",
        max_story_points=4,
        selected_story_ids=[],
    )

    assert result.selected_story_ids == [66, 85]
    assert result.story_points_used == 4
    assert result.dependency_promoted_story_ids == [66]
    assert result.dependency_closed is True


def test_auto_selection_promotes_transitive_prerequisites() -> None:
    rows = [
        _dep_row(30, 101, 2, blocked_by=[20]),
        _dep_row(20, 201, 2, blocked_by=[10]),
        _dep_row(10, 301, 1),
    ]

    result = select_sprint_story_rows(
        rows,
        team_velocity_assumption="Medium",
        max_story_points=5,
        selected_story_ids=[],
    )

    assert result.selected_story_ids == [10, 20, 30]
    assert result.dependency_promoted_story_ids == [10, 20]
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run --frozen pytest tests/test_sprint_selection.py \
  -k "dependency_closed or transitive" -q
```

Expected: FAIL because `SprintSelectionResult` has no dependency diagnostics and selector ignores `blocked_by_story_ids`.

- [ ] **Step 3: Extend `SprintSelectionResult`**

In `services/sprint_selection.py`, add fields:

```python
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
    dependency_closed: bool = True
    dependency_edges: list[dict[str, int]] = field(default_factory=list)
    dependency_promoted_story_ids: list[int] = field(default_factory=list)
```

- [ ] **Step 4: Add pure dependency helpers**

Add helpers to `services/sprint_selection.py`:

```python
def _candidate_dependency_edges(
    rows: list[dict[str, Any]],
) -> dict[int, set[int]]:
    by_id = _rows_by_story_id(rows)
    edges: dict[int, set[int]] = {}
    for row in rows:
        story_id = int(row["story_id"])
        prerequisites = {
            int(prerequisite_id)
            for prerequisite_id in row.get("blocked_by_story_ids") or []
            if int(prerequisite_id) in by_id
        }
        edges[story_id] = prerequisites
    return edges


def _priority_index(rows: list[dict[str, Any]]) -> dict[int, tuple[int, int]]:
    return {
        int(row["story_id"]): (int(row.get("priority") or 999), index)
        for index, row in enumerate(rows)
    }


def _dependency_closure(
    story_id: int,
    *,
    edges: dict[int, set[int]],
) -> set[int]:
    closure: set[int] = set()
    visiting: set[int] = set()

    def visit(current_id: int) -> None:
        if current_id in visiting:
            raise SprintSelectionError(
                code="SPRINT_SELECTION_DEPENDENCY_CYCLE",
                message="Sprint selection dependency graph contains a cycle.",
                details={"story_id": current_id},
            )
        if current_id in closure:
            return
        visiting.add(current_id)
        for prerequisite_id in sorted(edges.get(current_id, set())):
            visit(prerequisite_id)
        visiting.remove(current_id)
        closure.add(current_id)

    visit(story_id)
    return closure


def _topological_story_order(
    story_ids: set[int],
    *,
    edges: dict[int, set[int]],
    priority_index: dict[int, tuple[int, int]],
) -> list[int]:
    ordered: list[int] = []
    visited: set[int] = set()
    visiting: set[int] = set()

    def visit(current_id: int) -> None:
        if current_id in visiting:
            raise SprintSelectionError(
                code="SPRINT_SELECTION_DEPENDENCY_CYCLE",
                message="Sprint selection dependency graph contains a cycle.",
                details={"story_id": current_id},
            )
        if current_id in visited:
            return
        visiting.add(current_id)
        prerequisites = edges.get(current_id, set()) & story_ids
        for prerequisite_id in sorted(
            prerequisites,
            key=lambda item: priority_index.get(item, (999, item)),
        ):
            visit(prerequisite_id)
        visiting.remove(current_id)
        visited.add(current_id)
        ordered.append(current_id)

    for story_id in sorted(story_ids, key=lambda item: priority_index.get(item, (999, item))):
        visit(story_id)
    return ordered


def _edge_payloads(
    edges: dict[int, set[int]],
    selected_ids: set[int],
) -> list[dict[str, int]]:
    return [
        {
            "dependent_story_id": dependent_id,
            "prerequisite_story_id": prerequisite_id,
        }
        for dependent_id in sorted(selected_ids)
        for prerequisite_id in sorted(edges.get(dependent_id, set()) & selected_ids)
    ]
```

- [ ] **Step 5: Replace auto selection loop**

In `_select_auto`, build `by_id`, `edges`, and `priority_index`. For each priority row, calculate closure and add missing prerequisite rows before the dependent:

```python
def _select_auto(
    *,
    rows: list[dict[str, Any]],
    policy: _SelectionPolicy,
) -> SprintSelectionResult:
    by_id = _rows_by_story_id(rows)
    edges = _candidate_dependency_edges(rows)
    priority_index = _priority_index(rows)
    selected_ids: set[int] = set()
    promoted_ids: list[int] = []
    used_points = 0

    for row in rows:
        story_id = int(row["story_id"])
        if story_id in selected_ids:
            continue
        closure_ids = _dependency_closure(story_id, edges=edges)
        missing_ids = closure_ids - selected_ids
        ordered_missing_ids = _topological_story_order(
            missing_ids,
            edges=edges,
            priority_index=priority_index,
        )
        missing_rows = [by_id[item] for item in ordered_missing_ids]
        added_points = sum(_story_points(item) for item in missing_rows)
        for missing_row in missing_rows:
            if _story_points(missing_row) <= 0:
                raise SprintSelectionError(
                    code="SPRINT_SELECTION_UNSIZED_STORY",
                    message="Sprint selection requires positive story_points.",
                    details={"story_id": missing_row.get("story_id")},
                )
        if len(selected_ids) + len(missing_rows) > policy.story_limit:
            if not selected_ids:
                raise SprintSelectionError(
                    code="SPRINT_SELECTION_STORY_LIMIT_BLOCKED",
                    message="The next dependency-closed story cohort exceeds the Sprint story limit.",
                    details={
                        "blocking_story_id": story_id,
                        "required_story_ids": ordered_missing_ids,
                        "story_limit": policy.story_limit,
                    },
                )
            break
        if (
            policy.max_story_points is not None
            and used_points + added_points > policy.max_story_points
        ):
            if not selected_ids:
                raise SprintSelectionError(
                    code="SPRINT_SELECTION_CAPACITY_BLOCKED",
                    message=(
                        "The highest-priority dependency-closed story cohort exceeds "
                        "the explicit Sprint capacity. Increase --max-story-points or split the story."
                    ),
                    details={
                        "blocking_story_id": story_id,
                        "required_story_ids": ordered_missing_ids,
                        "story_points": added_points,
                        "max_story_points": policy.max_story_points,
                    },
                )
            break
        for selected_id in ordered_missing_ids:
            if selected_id != story_id:
                promoted_ids.append(selected_id)
            selected_ids.add(selected_id)
        used_points += added_points

    if not selected_ids:
        raise SprintSelectionError(
            code="SPRINT_SELECTION_EMPTY",
            message="Sprint selection produced no stories.",
            details={},
        )

    selected_rows = [by_id[item] for item in _topological_story_order(selected_ids, edges=edges, priority_index=priority_index)]
    return _result(
        mode="auto",
        selected_rows=selected_rows,
        all_rows=rows,
        policy=policy,
        dependency_edges=_edge_payloads(edges, selected_ids),
        dependency_promoted_story_ids=promoted_ids,
    )
```

- [ ] **Step 6: Run pure selector tests**

Run:

```bash
uv run --frozen pytest tests/test_sprint_selection.py -q
```

Expected: PASS.

---

## Task 2: Manual Selection Closure Validation

**Files:**
- Modify: `services/sprint_selection.py`
- Test: `tests/test_sprint_selection.py`

- [ ] **Step 1: Add failing tests for manual selection**

Append:

```python
def test_manual_selection_blocks_missing_prerequisite() -> None:
    rows = [
        _dep_row(85, 101, 3, blocked_by=[66]),
        _dep_row(66, 201, 1),
    ]

    try:
        select_sprint_story_rows(
            rows,
            team_velocity_assumption="Medium",
            max_story_points=4,
            selected_story_ids=[85],
        )
    except SprintSelectionError as exc:
        assert exc.code == "SPRINT_SELECTION_DEPENDENCY_MISSING"
        assert exc.details["missing_prerequisite_story_ids"] == [66]
        assert exc.details["dependent_story_id"] == 85
    else:
        raise AssertionError("Expected dependency closure failure")


def test_manual_selection_reorders_to_dependency_safe_order() -> None:
    rows = [
        _dep_row(85, 101, 3, blocked_by=[66]),
        _dep_row(66, 201, 1),
    ]

    result = select_sprint_story_rows(
        rows,
        team_velocity_assumption="Medium",
        max_story_points=4,
        selected_story_ids=[85, 66],
    )

    assert result.mode == "manual"
    assert result.selected_story_ids == [66, 85]
    assert result.warnings == [
        {
            "code": "SPRINT_SELECTION_MANUAL_REORDERED",
            "message": "Manual Sprint selection was reordered to satisfy dependencies.",
            "requested_story_ids": [85, 66],
            "selected_story_ids": [66, 85],
        }
    ]
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run --frozen pytest tests/test_sprint_selection.py \
  -k "manual_selection" -q
```

Expected: FAIL because manual mode still preserves unsafe input order.

- [ ] **Step 3: Update `_select_manual`**

Implement:

```python
def _select_manual(
    *,
    rows: list[dict[str, Any]],
    selected_story_ids: list[int],
    policy: _SelectionPolicy,
) -> SprintSelectionResult:
    by_id = _rows_by_story_id(rows)
    edges = _candidate_dependency_edges(rows)
    priority_index = _priority_index(rows)
    invalid_selected_ids = [
        story_id for story_id in selected_story_ids if story_id not in by_id
    ]
    if invalid_selected_ids:
        raise SprintSelectionError(
            code="SPRINT_SELECTION_INVALID",
            message="Some selected_story_ids are not sprint candidate stories.",
            details={"invalid_selected_ids": invalid_selected_ids},
        )

    selected_id_set = set(selected_story_ids)
    for dependent_id in selected_story_ids:
        missing_prerequisites = sorted(edges.get(dependent_id, set()) - selected_id_set)
        if missing_prerequisites:
            raise SprintSelectionError(
                code="SPRINT_SELECTION_DEPENDENCY_MISSING",
                message="Manual Sprint selection omits required prerequisite stories.",
                details={
                    "dependent_story_id": dependent_id,
                    "missing_prerequisite_story_ids": missing_prerequisites,
                },
            )

    ordered_ids = _topological_story_order(
        selected_id_set,
        edges=edges,
        priority_index=priority_index,
    )
    warnings: list[dict[str, Any]] = []
    if ordered_ids != selected_story_ids:
        warnings.append(
            {
                "code": "SPRINT_SELECTION_MANUAL_REORDERED",
                "message": "Manual Sprint selection was reordered to satisfy dependencies.",
                "requested_story_ids": selected_story_ids,
                "selected_story_ids": ordered_ids,
            }
        )

    return _result(
        mode="manual",
        selected_rows=[by_id[story_id] for story_id in ordered_ids],
        all_rows=rows,
        policy=policy,
        warnings=warnings,
        dependency_edges=_edge_payloads(edges, selected_id_set),
    )
```

- [ ] **Step 4: Update `_result` signature**

Allow diagnostics:

```python
def _result(
    *,
    mode: str,
    selected_rows: list[dict[str, Any]],
    all_rows: list[dict[str, Any]],
    policy: _SelectionPolicy,
    warnings: list[dict[str, Any]] | None = None,
    dependency_edges: list[dict[str, int]] | None = None,
    dependency_promoted_story_ids: list[int] | None = None,
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
        story_points_used=sum(_story_points(row) for row in selected_rows),
        max_story_points=policy.max_story_points,
        team_velocity_assumption=policy.team_velocity_assumption,
        story_limit=policy.story_limit,
        warnings=warnings or [],
        dependency_edges=dependency_edges or [],
        dependency_promoted_story_ids=dependency_promoted_story_ids or [],
        dependency_closed=True,
    )
```

- [ ] **Step 5: Run selector tests**

Run:

```bash
uv run --frozen pytest tests/test_sprint_selection.py -q
```

Expected: PASS.

---

## Task 3: Runtime/Input Diagnostics

**Files:**
- Modify: `services/sprint_input.py`
- Modify: `services/sprint_runtime.py`
- Test: `tests/test_sprint_runtime.py`

- [ ] **Step 1: Add failing test for selection diagnostics**

Append to `tests/test_sprint_runtime.py`:

```python
def test_prepare_sprint_input_reports_dependency_selection_policy() -> None:
    def fake_fetch_sprint_candidates(*, product_id: int) -> dict[str, object]:
        return {
            "success": True,
            "count": 2,
            "stories": [
                {
                    "story_id": 85,
                    "story_title": "Live workflow",
                    "priority": 101,
                    "story_points": 3,
                    "blocked_by_story_ids": [66],
                    "prerequisite_story_ids": [66],
                    "dependency_status": "blocked",
                },
                {
                    "story_id": 66,
                    "story_title": "Budget parameter",
                    "priority": 201,
                    "story_points": 1,
                    "blocked_by_story_ids": [],
                    "prerequisite_story_ids": [],
                    "dependency_status": "ready",
                },
            ],
        }

    result = sprint_input.prepare_sprint_input_context(
        product_id=7,
        team_velocity_assumption="Medium",
        sprint_duration_days=14,
        user_context=None,
        max_story_points=4,
        include_task_decomposition=True,
        selected_story_ids=None,
        fetch_candidates=fake_fetch_sprint_candidates,
    )

    assert result["selected_story_ids"] == [66, 85]
    assert result["selection_policy"]["dependency_closed"] is True
    assert result["selection_policy"]["dependency_promoted_story_ids"] == [66]
    assert result["selection_policy"]["dependency_edges"] == [
        {"dependent_story_id": 85, "prerequisite_story_id": 66}
    ]
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
uv run --frozen pytest tests/test_sprint_runtime.py \
  -k dependency_selection_policy -q
```

Expected: FAIL until `selection_policy` exposes new fields.

- [ ] **Step 3: Extend `selection_policy` payload**

In `services/sprint_input.py`, add these fields:

```python
"selection_policy": {
    "mode": selection.mode,
    "selected_story_ids": selection.selected_story_ids,
    "excluded_story_ids": selection.excluded_story_ids,
    "story_points_used": selection.story_points_used,
    "max_story_points": selection.max_story_points,
    "team_velocity_assumption": selection.team_velocity_assumption,
    "story_limit": selection.story_limit,
    "warnings": selection.warnings,
    "dependency_closed": selection.dependency_closed,
    "dependency_edges": selection.dependency_edges,
    "dependency_promoted_story_ids": selection.dependency_promoted_story_ids,
}
```

- [ ] **Step 4: Ensure runtime attempts persist selection policy**

Inspect `services/sprint_runtime.py` for attempt artifact creation. If `prepared.input_context` already includes `selection_policy`, no code change is required. If not, add `selection_policy` into the runtime result metadata next to `selected_story_ids`.

- [ ] **Step 5: Run runtime tests**

Run:

```bash
uv run --frozen pytest tests/test_sprint_runtime.py -q
```

Expected: PASS.

---

## Task 4: Planner Prompt Contract

**Files:**
- Modify: `orchestrator_agent/agent_tools/sprint_planner_tool/instructions.txt`
- Test: `tests/test_sprint_runtime.py`

- [ ] **Step 1: Update prompt wording**

In `orchestrator_agent/agent_tools/sprint_planner_tool/instructions.txt`, replace the locked selection contract text with:

```text
3.  **Locked Dependency-Safe Selection Contract:**
    * `available_stories` have already been selected by the AgileForge deterministic Sprint selection policy.
    * The cohort is dependency-closed: if a selected story has an unresolved active prerequisite, that prerequisite is also included in `available_stories`.
    * The input order is the dependency-safe execution order. Earlier stories are prerequisites or higher-priority foundations for later stories.
    * The model must not choose a different scope.
    * Output exactly one `selected_stories` entry for every input story in `available_stories`, in the same order.
    * MUST NOT add stories that are not present in `available_stories`.
    * MUST NOT drop stories from `available_stories`.
    * Emit `deselected_stories: []` because deselection happened before the planner call.
    * Use `prerequisite_story_ids`, `blocked_by_story_ids`, `parent_group`, and `group_slot` only to explain sequencing and cohesion. Do not invent new dependencies.
```

- [ ] **Step 2: Run prompt-adjacent runtime tests**

Run:

```bash
uv run --frozen pytest tests/test_sprint_runtime.py tests/test_sprint_planner_schemes.py -q
```

Expected: PASS.

---

## Task 5: Minimal Frontend Compatibility

**Files:**
- Modify: `frontend/project.js`

- [ ] **Step 1: Add dependency labels in candidate render**

In `renderSprintCandidates`, after `origin`, add:

```javascript
const prerequisiteIds = Array.isArray(story.prerequisite_story_ids) ? story.prerequisite_story_ids : [];
const blockedByIds = Array.isArray(story.blocked_by_story_ids) ? story.blocked_by_story_ids : [];
const dependencyLabel = prerequisiteIds.length
    ? `Requires: ${prerequisiteIds.map(id => `#${id}`).join(', ')}`
    : 'No active prerequisites';
const dependencyTone = blockedByIds.length
    ? 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200'
    : 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200';
```

Render it near the story metadata:

```javascript
<span class="rounded-full px-2 py-0.5 text-[10px] font-black uppercase ${dependencyTone}">
    ${dependencyLabel}
</span>
```

- [ ] **Step 2: Add dependency-closed checkbox behavior**

Add helpers near Sprint selection helpers:

```javascript
function sprintCandidateById(storyId) {
    return sprintCandidates.find(candidate => Number(candidate.story_id) === Number(storyId)) || null;
}

function addSprintStoryWithPrerequisites(storyId) {
    const story = sprintCandidateById(storyId);
    if (!story) return;
    const prerequisiteIds = Array.isArray(story.blocked_by_story_ids) ? story.blocked_by_story_ids : [];
    prerequisiteIds.forEach(prerequisiteId => addSprintStoryWithPrerequisites(prerequisiteId));
    selectedSprintStoryIds.add(Number(storyId));
}

function removeSprintStoryAndDependents(storyId) {
    selectedSprintStoryIds.delete(Number(storyId));
    sprintCandidates.forEach(candidate => {
        const blockedByIds = Array.isArray(candidate.blocked_by_story_ids) ? candidate.blocked_by_story_ids : [];
        if (blockedByIds.map(Number).includes(Number(storyId))) {
            removeSprintStoryAndDependents(candidate.story_id);
        }
    });
}
```

In the candidate checkbox listener, replace direct add/delete with:

```javascript
if (checkbox.checked) {
    addSprintStoryWithPrerequisites(story.story_id);
} else {
    removeSprintStoryAndDependents(story.story_id);
}
renderSprintCandidates();
updateSprintSelectionSummary();
updateSprintCapacityWarning();
```

- [ ] **Step 3: Run frontend smoke lint by syntax parsing**

Run:

```bash
node --check frontend/project.js
```

Expected: PASS.

---

## Task 6: CLI Documentation

**Files:**
- Modify: `docs/agent-cli-manual.md`

- [ ] **Step 1: Update Sprint section**

Add:

```markdown
### Dependency-Aware Sprint Selection

`agileforge sprint generate` does not let the model choose arbitrary Sprint scope.
AgileForge first selects a dependency-closed cohort from active sprint candidates.

Default behavior:

```bash
agileforge sprint generate --project-id <project_id>
```

Manual override:

```bash
agileforge sprint generate \
  --project-id <project_id> \
  --selected-story-ids <prerequisite_id>,<dependent_id>
```

Manual selections must include active unresolved prerequisites. If a selected story
requires another active candidate story and that prerequisite is omitted, Sprint
generation fails with `SPRINT_SELECTION_DEPENDENCY_MISSING`.
```

- [ ] **Step 2: Run docs grep**

Run:

```bash
rg "SPRINT_SELECTION_DEPENDENCY_MISSING|Dependency-Aware Sprint Selection" docs/agent-cli-manual.md
```

Expected: both terms found.

---

## Task 7: Verification

**Files:**
- All changed files.

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_sprint_selection.py \
  tests/test_sprint_runtime.py \
  tests/test_sprint_planner_schemes.py \
  tests/test_deterministic_tool_adapters.py \
  tests/test_agent_workbench_phase1_integration.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run ruff**

Run:

```bash
uv run --frozen ruff check \
  services/sprint_selection.py \
  services/sprint_input.py \
  services/sprint_runtime.py \
  orchestrator_agent/agent_tools/sprint_planner_tool/schemes.py \
  tests/test_sprint_selection.py \
  tests/test_sprint_runtime.py \
  tests/test_deterministic_tool_adapters.py \
  tests/test_agent_workbench_phase1_integration.py
```

Expected: PASS.

Run:

```bash
node --check frontend/project.js
```

Expected: PASS.

- [ ] **Step 3: Run full test suite**

Run:

```bash
uv run --frozen pytest -q
```

Expected: full suite passes.

- [ ] **Step 4: Check git diff**

Run:

```bash
git diff --check
git status --short --branch
```

Expected: no whitespace errors. Branch contains only Phase 3 files.

---

## Self-Review

- Scope is one subsystem: dependency-aware Sprint selection.
- DB dependency storage is not repeated; Phase 2 remains source.
- LLM does not regain selection authority.
- Manual override stays possible but fail-closed if dependency closure is broken.
- UI update is minimal and additive, not a dependency graph editor.
- No placeholders remain in task steps.
