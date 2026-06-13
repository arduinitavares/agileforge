# Stale Story Scope Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `workflow next` block Sprint generation and advertise guarded Story readiness repair when stale Story completion scope leaves zero Sprint candidates in `SPRINT_SETUP`.

**Architecture:** Reuse the existing `sprint_candidates` read projection and the existing `story repair-readiness` mutation. Add a narrow SPRINT_SETUP candidate check in `AgentWorkbenchApplication.workflow_next()`, then let `_sprint_workflow_next()` convert the stale-scope condition into blocked command metadata and repair routing.

**Tech Stack:** Python 3.13, pytest, Ruff, ty, AgileForge agent workbench services.

---

## File Structure

- Modify `services/agent_workbench/application.py`: pass SPRINT_SETUP candidate data into `_sprint_workflow_next()`, detect stale Story scope, block Sprint generation, and advertise Story repair readiness.
- Modify `tests/test_agent_workbench_application.py`: add fake projection and regression test for the stale-scope route.
- Verify `tests/test_story_phase_service.py`: existing repair command behavior remains unchanged.

## Task 1: Add RED Regression For SPRINT_SETUP Stale Story Scope

**Files:**
- Modify: `tests/test_agent_workbench_application.py`

- [ ] **Step 1: Add fake projection**

Add this class near `_SprintReadyReadProjection`:

```python
class _SprintSetupStaleStoryScopeReadProjection(_SprintReadyReadProjection):
    """Fake read projection for stale Story completion scope in Sprint setup."""

    def sprint_candidates(self, *, project_id: int) -> dict[str, Any]:
        """Return zero candidates excluded by stale completion scope."""
        result = super().sprint_candidates(project_id=project_id)
        result["data"].update(
            {
                "count": 0,
                "message": "Found 0 sprint candidates for milestone Story scope.",
                "excluded_counts": {
                    "story_completion_scope": 28,
                    "superseded": 9,
                },
                "readiness": {
                    "status": "ready",
                    "blocking_codes": [],
                    "blocking_story_ids": [],
                    "default_priority_count": 0,
                    "unsized_count": 0,
                },
                "story_completion_scope": {
                    "scope": "milestone",
                    "scope_id": "milestone_1",
                    "requirements": [
                        "State Window Feature Generation",
                        "Delayed Temperature Reward Scoring",
                    ],
                },
            }
        )
        return result
```

- [ ] **Step 2: Add failing test**

Add this test near `test_application_workflow_next_derives_from_sprint_planning_pack`:

```python
def test_workflow_next_blocks_generate_for_stale_story_scope() -> None:
    """Route stale Story scope in Sprint setup to guarded readiness repair."""
    app = AgentWorkbenchApplication(
        read_projection=_SprintSetupStaleStoryScopeReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "sprint_setup_story_scope_repair_required"
    assert "agileforge sprint generate --project-id 7" not in data[
        "next_valid_commands"
    ]
    assert (
        "agileforge story repair-readiness --project-id 7 "
        "--expected-state SPRINT_SETUP "
        "--idempotency-key <idempotency_key>"
    ) in data["next_valid_commands"]
    assert data["blocked_commands"] == [
        {
            "command": "agileforge sprint generate",
            "reason": "STALE_STORY_COMPLETION_SCOPE",
            "message": (
                "Sprint generation is blocked because the active Story "
                "completion scope excludes all current Sprint candidates. Run "
                "story repair-readiness to refresh Story planning metadata."
            ),
            "candidate_count": 0,
            "excluded_counts": {
                "story_completion_scope": 28,
                "superseded": 9,
            },
            "story_completion_scope": {
                "scope": "milestone",
                "scope_id": "milestone_1",
                "requirements": [
                    "State Window Feature Generation",
                    "Delayed Temperature Reward Scoring",
                ],
            },
        }
    ]
```

- [ ] **Step 3: Run RED test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "stale_story_scope"
```

Expected: FAIL because current `workflow next` still advertises `sprint generate`.

## Task 2: Implement SPRINT_SETUP Stale Scope Blocker

**Files:**
- Modify: `services/agent_workbench/application.py`

- [ ] **Step 1: Add stale-scope blocker helper**

Add helper near `_sprint_candidate_excluded_counts()`:

```python
def _sprint_setup_stale_story_scope_blocker(
    candidates: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return blocker when a stale Story scope excludes all Sprint candidates."""
    if _sprint_candidate_count(candidates) != 0:
        return None
    excluded_counts = _sprint_candidate_excluded_counts(candidates)
    scoped_exclusion_count = excluded_counts.get("story_completion_scope")
    if (
        isinstance(scoped_exclusion_count, bool)
        or not isinstance(scoped_exclusion_count, int)
        or scoped_exclusion_count <= 0
    ):
        return None
    scope = _envelope_data(candidates or {}).get("story_completion_scope")
    if not isinstance(scope, dict):
        return None
    return {
        "command": "agileforge sprint generate",
        "reason": "STALE_STORY_COMPLETION_SCOPE",
        "message": (
            "Sprint generation is blocked because the active Story completion "
            "scope excludes all current Sprint candidates. Run story "
            "repair-readiness to refresh Story planning metadata."
        ),
        "candidate_count": 0,
        "excluded_counts": excluded_counts,
        "story_completion_scope": scope,
    }
```

- [ ] **Step 2: Load candidates for SPRINT_SETUP**

In `AgentWorkbenchApplication.workflow_next()`, before `phase_next_handlers`, add:

```python
sprint_candidates_for_setup = (
    self.sprint_candidates(project_id=project_id)
    if fsm_state == "SPRINT_SETUP"
    else None
)
```

When calling `_sprint_workflow_next`, pass `sprint_candidates=sprint_candidates_for_setup`.

- [ ] **Step 3: Use blocker in `_sprint_workflow_next()`**

Change `_sprint_workflow_next()` to accept `sprint_candidates`. If state is `SPRINT_SETUP`, compute `stale_scope_blocker`.

When iterating `_sprint_command_candidates()`, if command name is `agileforge sprint generate` and the blocker exists, append the blocker to `blocked_commands` and skip adding the runnable command.

After the loop, add the guarded repair command:

```python
repair_command = (
    f"agileforge story repair-readiness --project-id {project_id} "
    "--expected-state SPRINT_SETUP "
    "--idempotency-key <idempotency_key>"
)
```

If installed, append to `next_valid_commands`; otherwise append to `blocked_future_commands`.

Set status to `sprint_setup_story_scope_repair_required` when the blocker exists.

- [ ] **Step 4: Run GREEN test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "stale_story_scope or workflow_next_routes_sprint_view_to_execution_commands or application_workflow_next_derives_from_sprint_planning_pack"
```

Expected: PASS.

## Task 3: Verify Full Scope And Commit

**Files:**
- Verify changed files only plus full gate.

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "stale_story_scope or workflow_next_routes_sprint_view_to_execution_commands or application_workflow_next_derives_from_sprint_planning_pack"
uv run --frozen pytest tests/test_story_phase_service.py -q -k "repair_readiness or complete_story_phase"
```

Expected: PASS.

- [ ] **Step 2: Run full gate**

Run:

```bash
pyrepo-check --all
```

Expected: PASS.

- [ ] **Step 3: Commit**

Run:

```bash
git add services/agent_workbench/application.py tests/test_agent_workbench_application.py docs/superpowers/specs/2026-06-13-stale-story-scope-repair-design.md docs/superpowers/plans/2026-06-13-stale-story-scope-repair.md
git commit -m "fix(workflow): route stale story scope repair"
```

Expected: commit succeeds on `dev/stale-story-scope-repair`.

## Self-Review

- Spec coverage: Tasks cover stale-scope detection, command blocking, repair routing, #137 regression protection, and verification.
- Placeholder scan: No placeholder steps remain.
- Type consistency: Status, blocker reason, and repair command names match the design.
