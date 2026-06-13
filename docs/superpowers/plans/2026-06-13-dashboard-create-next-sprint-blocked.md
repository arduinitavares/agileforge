# Dashboard Create Next Sprint Blocker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the dashboard from offering an enabled Create Next Sprint action when the backend workflow route blocks Sprint generation.

**Architecture:** Keep the backend as the authority. Extend the Sprint runtime summary returned by `/api/projects/{project_id}/sprints` so the completed-sprint UI sees the same no-candidates blocker that `workflow next` exposes, then render a disabled action with the blocker reason and valid inspection commands.

**Tech Stack:** FastAPI, SQLModel, pytest, vanilla JavaScript dashboard tests with `node:test`.

---

## File Structure

- Modify `api.py`: add a lazy candidate-summary hook to `_build_sprint_runtime_summary()` and use it from `list_project_sprints()`.
- Modify `frontend/project.js`: add helpers for create-next availability text and use them in overview and completed-sprint button rendering.
- Modify `tests/test_api_sprint_flow.py`: add an API regression for zero refined candidates after post-sprint `impact=none`.
- Modify `tests/test_sprint_workspace_display.mjs`: add frontend regressions for blocked create-next behavior.

## Task 1: API Runtime Summary Blocks Create-Next When Candidates Are Unavailable

**Files:**
- Modify: `tests/test_api_sprint_flow.py`
- Modify: `api.py`

- [ ] **Step 1: Write the failing API test**

Add a test next to the existing Sprint runtime-summary tests that seeds a completed sprint with post-sprint triage `impact=none`, stubs `load_sprint_candidates()` to return `count=0`, and asserts `/api/projects/{project_id}/sprints` returns:

```python
assert runtime_summary["can_create_next_sprint"] is False
assert runtime_summary["workflow_next_status"] == "post_sprint_sprint_candidates_unavailable"
assert runtime_summary["create_next_sprint_blocked_reason"] == "NO_REFINED_SPRINT_CANDIDATES"
assert runtime_summary["create_next_sprint_valid_commands"] == [
    f"agileforge story pending --project-id {project_id}",
    f"agileforge sprint candidates --project-id {project_id}",
]
```

- [ ] **Step 2: Run the API test to verify RED**

Run:

```bash
uv run --frozen pytest tests/test_api_sprint_flow.py -q -k "blocks_create_next_when_sprint_generation_blocked"
```

Expected: FAIL because `can_create_next_sprint` is still `True` and blocker fields are absent.

- [ ] **Step 3: Implement the minimal API blocker**

In `api.py`, import `Callable`, add a `load_candidate_summary` callback to `_build_sprint_runtime_summary()`, and call it only when the summary is otherwise able to create the next sprint after a completed sprint with `impact=none`.

The blocker payload must use:

```python
{
    "command": "agileforge sprint generate",
    "reason": "NO_REFINED_SPRINT_CANDIDATES",
    "message": "Sprint generation is blocked because no refined Story candidates are available.",
    "candidate_count": 0,
    "excluded_counts": candidate_summary.get("excluded_counts", {}),
}
```

- [ ] **Step 4: Run the API test to verify GREEN**

Run:

```bash
uv run --frozen pytest tests/test_api_sprint_flow.py -q -k "blocks_create_next_when_sprint_generation_blocked or sprint_runtime_summary"
```

Expected: PASS.

## Task 2: Frontend Renders Blocked Create-Next State

**Files:**
- Modify: `tests/test_sprint_workspace_display.mjs`
- Modify: `frontend/project.js`

- [ ] **Step 1: Write the failing frontend tests**

Add tests that prove:

```javascript
assert.equal(state.canCreate, false);
assert.equal(state.label, 'Sprint Generation Blocked');
assert.match(state.reasonHtml, /NO_REFINED_SPRINT_CANDIDATES/);
assert.match(state.reasonHtml, /agileforge sprint candidates --project-id 3/);
```

and that `openSprintPlanner()` returns without entering planner mode when create-next is blocked and there is no draft or planned sprint to review.

- [ ] **Step 2: Run the frontend tests to verify RED**

Run:

```bash
node --test tests/test_sprint_workspace_display.mjs --test-name-pattern "create-next|blocked"
```

Expected: FAIL because the helper does not exist and `openSprintPlanner()` still opens the planner.

- [ ] **Step 3: Implement the frontend helper and rendering**

Add `getCreateNextSprintActionState()` near `shouldStartFreshSprintCycle()` and use it in `renderOverviewPanel()`, `renderSprintSavedWorkspace()`, and `openSprintPlanner()`.

Behavior:
- If `can_create_next_sprint` is true and there is no reviewable draft, label is `Create Next Sprint`, enabled.
- If a reviewable draft exists, label is `Review Sprint Draft`, enabled.
- If blocked by backend, label is `Sprint Generation Blocked`, disabled, and renders reason plus valid commands.
- `openSprintPlanner()` returns early when backend says create-next is blocked and there is no draft/planned sprint.

- [ ] **Step 4: Run frontend tests to verify GREEN**

Run:

```bash
node --test tests/test_sprint_workspace_display.mjs --test-name-pattern "create-next|blocked"
node --check frontend/project.js
```

Expected: PASS.

## Task 3: Verification And Commit

**Files:**
- Verify all changed files.

- [ ] **Step 1: Run focused backend and frontend verification**

Run:

```bash
uv run --frozen pytest tests/test_api_sprint_flow.py -q -k "sprint_runtime_summary or blocks_create_next_when_sprint_generation_blocked"
node --test tests/test_sprint_workspace_display.mjs
node --check frontend/project.js
```

Expected: all pass.

- [ ] **Step 2: Run full quality gate**

Run:

```bash
pyrepo-check --all
```

Expected: pass.

- [ ] **Step 3: Optional read-only ASA verification**

Run only read-only commands:

```bash
agileforge workflow next --project-id 3
curl -s http://127.0.0.1:8001/api/projects/3/sprints
```

Expected: if project 3 is still in the same state, workflow next reports `post_sprint_sprint_candidates_unavailable`, and the sprint runtime summary reports `can_create_next_sprint=false`.

- [ ] **Step 4: Commit**

Run:

```bash
git add api.py frontend/project.js tests/test_api_sprint_flow.py tests/test_sprint_workspace_display.mjs docs/superpowers/plans/2026-06-13-dashboard-create-next-sprint-blocked.md
git commit -m "fix(ui): block create next sprint when candidates unavailable"
```

Expected: commit succeeds on `dev/dashboard-create-next-sprint-blocked`.

## Self-Review

- Spec coverage: The plan covers backend runtime truth, frontend action rendering, blocked command metadata, and focused verification.
- Placeholder scan: No placeholders or TODOs remain.
- Type consistency: Backend fields use `create_next_sprint_*`; frontend helpers read those exact keys.
