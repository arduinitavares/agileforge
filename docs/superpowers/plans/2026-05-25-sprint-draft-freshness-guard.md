# Sprint Draft Freshness Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent AgileForge from saving or advertising stale Sprint drafts after Story or dependency inputs change, especially after failed Sprint regeneration.

**Architecture:** Treat a Sprint draft as valid only for the exact Sprint candidate source it was generated from. Stamp generated Sprint attempts with a candidate/source fingerprint, clear stale Sprint working state when upstream Story/dependency data mutates, require the selected save attempt to be the latest complete attempt, and hide `sprint save` from `workflow next` unless the current draft is save-ready. Provider failures remain fail-closed and must not leave older drafts saveable.

**Tech Stack:** Python, SQLModel-backed query services, existing AgileForge phase services, pytest, ruff.

---

## Scope Boundary

Implement now:

1. Candidate-source fingerprint for Sprint generation inputs.
2. Attempt/source stamping for Sprint draft attempts.
3. Sprint save guard that blocks stale or non-latest attempts.
4. `workflow next` filtering so `sprint save` is advertised only for a latest complete draft.
5. Story/dependency mutation invalidation of unsaved Sprint drafts.
6. Tests and CLI docs for the recovery behavior.

Do not implement now:

1. New LLM semantic dependency inference.
2. A full UI graph editor.
3. Provider retry/backoff logic for OpenRouter 429s.
4. Authority-vs-story semantic drift validator. That is a separate guard.

## File Map

- Modify: `services/sprint_input.py`
  - Compute and return a deterministic candidate source fingerprint from normalized candidate stories, readiness, excluded counts, and message.
- Modify: `services/sprint_runtime.py`
  - Preserve candidate source fingerprint through prepared Sprint payloads and runtime result dictionaries.
- Modify: `services/phases/sprint_service.py`
  - Stamp attempts and assessment with candidate source fingerprint.
  - Clear prior attempts when the current candidate source differs from the saved Sprint source before generation.
  - Enforce latest-complete/source-current save guard.
- Modify: `services/agent_workbench/application.py`
  - Hide `agileforge sprint save` unless workflow state contains a latest complete saveable draft.
- Modify: `services/agent_workbench/story_phase.py`
  - Invalidate unsaved Sprint planning state after successful Story save/reopen and dependency apply.
- Modify: `docs/agent-cli-manual.md`
  - Document stale draft behavior and recovery commands.
- Test: `tests/test_sprint_runtime.py`
  - Fingerprint propagation.
- Test: `tests/test_sprint_phase_service.py`
  - Save guard and source-change reset behavior.
- Test: `tests/test_agent_workbench_application.py`
  - `workflow next` does not advertise stale save.
- Test: `tests/test_agent_workbench_story_phase.py`
  - Story/dependency mutation clears unsaved Sprint working set.

---

## Task 1: Candidate Source Fingerprint

**Files:**
- Modify: `services/sprint_input.py`
- Test: `tests/test_sprint_runtime.py`

- [ ] **Step 1: Write failing test for candidate source fingerprint**

Add this test to `tests/test_sprint_runtime.py`:

```python
def test_prepare_sprint_input_context_source_fingerprint_changes_with_story_text() -> None:
    """Sprint source fingerprint changes when candidate content changes."""

    def fetch_with_title(title: str):
        def fake_fetch_sprint_candidates(*, product_id: int) -> dict[str, object]:
            assert product_id == 7  # noqa: PLR2004
            return {
                "success": True,
                "count": 1,
                "stories": [
                    {
                        "story_id": 71,
                        "story_title": title,
                        "priority": 302,
                        "story_points": 2,
                        "acceptance_criteria": "- Verify explicit --budget only.",
                        "blocked_by_story_ids": [],
                        "prerequisite_story_ids": [],
                    }
                ],
                "readiness": {"status": "ready", "blocking_codes": []},
                "excluded_counts": {"non_refined": 0, "superseded": 0, "open_sprint": 0},
                "message": "Found 1 sprint candidate.",
            }

        return fake_fetch_sprint_candidates

    first = sprint_input.prepare_sprint_input_context(
        product_id=7,
        team_velocity_assumption="Medium",
        sprint_duration_days=14,
        user_context=None,
        max_story_points=None,
        include_task_decomposition=True,
        selected_story_ids=None,
        fetch_candidates=fetch_with_title("Validate Squad Budget Compliance"),
    )
    changed = sprint_input.prepare_sprint_input_context(
        product_id=7,
        team_velocity_assumption="Medium",
        sprint_duration_days=14,
        user_context=None,
        max_story_points=None,
        include_task_decomposition=True,
        selected_story_ids=None,
        fetch_candidates=fetch_with_title("Require Explicit Budget and Validate Squad Compliance"),
    )

    assert first["success"] is True
    assert changed["success"] is True
    assert first["source_fingerprint"].startswith("sha256:")
    assert changed["source_fingerprint"].startswith("sha256:")
    assert first["source_fingerprint"] != changed["source_fingerprint"]
```

- [ ] **Step 2: Run failing test**

Run:

```bash
uv run --frozen pytest tests/test_sprint_runtime.py::test_prepare_sprint_input_context_source_fingerprint_changes_with_story_text -q
```

Expected: fail with missing `source_fingerprint`.

- [ ] **Step 3: Implement fingerprint in `services/sprint_input.py`**

Import `canonical_hash`:

```python
from services.agent_workbench.fingerprints import canonical_hash
```

In `load_sprint_candidates`, after `stories` is built and before return, compute:

```python
readiness = (
    raw_result.get("readiness")
    if isinstance(raw_result.get("readiness"), dict)
    else _sprint_candidate_readiness(stories)
)
excluded_counts = raw_result.get("excluded_counts") or {}
message = raw_result.get("message") or f"Found {len(stories)} sprint candidates."
source_fingerprint = canonical_hash(
    {
        "command": "agileforge sprint candidates",
        "product_id": product_id,
        "stories": stories,
        "readiness": readiness,
        "excluded_counts": excluded_counts,
        "message": message,
    }
)
```

Return those exact local values, including:

```python
"source_fingerprint": source_fingerprint,
```

In `prepare_sprint_input_context`, include:

```python
"source_fingerprint": candidate_result.get("source_fingerprint"),
```

Also include the same value inside `selection_policy`:

```python
"source_fingerprint": candidate_result.get("source_fingerprint"),
```

- [ ] **Step 4: Run test**

Run:

```bash
uv run --frozen pytest tests/test_sprint_runtime.py::test_prepare_sprint_input_context_source_fingerprint_changes_with_story_text -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add services/sprint_input.py tests/test_sprint_runtime.py
git commit -m "feat: fingerprint sprint candidate source"
```

---

## Task 2: Runtime and Attempt Source Stamping

**Files:**
- Modify: `services/sprint_runtime.py`
- Modify: `services/phases/sprint_service.py`
- Test: `tests/test_sprint_runtime.py`
- Test: `tests/test_sprint_phase_service.py`

- [ ] **Step 1: Write failing runtime propagation test**

Add this test to `tests/test_sprint_runtime.py`:

```python
def test_prepare_sprint_payload_preserves_source_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prepared Sprint payload preserves candidate source fingerprint."""
    source_fingerprint = "sha256:" + "a" * 64

    def fake_prepare_sprint_input_context(*, product_id: int, **options: object) -> dict[str, object]:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "source_fingerprint": source_fingerprint,
            "selection_policy": {"mode": "auto", "source_fingerprint": source_fingerprint},
            "input_context": {
                "available_stories": [
                    {
                        "story_id": 66,
                        "story_title": "Budget",
                        "priority": 101,
                        "story_points": 1,
                        "acceptance_criteria_items": [],
                        "evaluated_invariant_ids": [],
                        "story_compliance_boundary_summaries": [],
                        "prerequisite_story_ids": [],
                        "blocked_by_story_ids": [],
                        "dependency_status": "ready",
                    }
                ],
                "team_velocity_assumption": "Medium",
                "sprint_duration_days": 14,
                "include_task_decomposition": True,
            },
        }

    monkeypatch.setattr(
        sprint_runtime,
        "prepare_sprint_input_context",
        fake_prepare_sprint_input_context,
    )

    prepared = sprint_runtime._prepare_sprint_payload(
        project_id=7,
        options={
            "team_velocity_assumption": "Medium",
            "sprint_duration_days": 14,
            "include_task_decomposition": True,
            "max_story_points": None,
            "selected_story_ids": None,
            "user_input": None,
        },
    )

    assert not isinstance(prepared, dict)
    assert prepared.source_fingerprint == source_fingerprint
    assert prepared.selection_policy["source_fingerprint"] == source_fingerprint
```

- [ ] **Step 2: Run failing runtime test**

Run:

```bash
uv run --frozen pytest tests/test_sprint_runtime.py::test_prepare_sprint_payload_preserves_source_fingerprint -q
```

Expected: fail because `_PreparedSprintPayload` has no `source_fingerprint`.

- [ ] **Step 3: Implement runtime source propagation**

In `services/sprint_runtime.py`, add field to `_PreparedSprintPayload`:

```python
source_fingerprint: str | None
```

In `_prepare_sprint_payload`, normalize:

```python
source_fingerprint = prepared.get("source_fingerprint")
if not isinstance(source_fingerprint, str):
    source_fingerprint = None
```

Return it in `_PreparedSprintPayload`.

In every returned runtime result from `_validate_sprint_output` and failure helpers that already receive prepared data, include:

```python
"source_fingerprint": prepared.source_fingerprint,
```

For prepare failures before `_PreparedSprintPayload` exists, include:

```python
"source_fingerprint": prepared.get("source_fingerprint"),
```

- [ ] **Step 4: Write failing attempt stamping test**

Add this test to `tests/test_sprint_phase_service.py`:

```python
@pytest.mark.asyncio
async def test_generate_sprint_plan_stamps_attempt_with_source_fingerprint() -> None:
    """Sprint attempts remember the candidate source used to generate them."""
    state: JsonDict = {"fsm_state": "SPRINT_SETUP"}
    source_fingerprint = "sha256:" + "b" * 64

    async def load_state() -> JsonDict:
        return state

    async def run_sprint_agent(_state: JsonDict, **_kwargs: object) -> JsonDict:
        return {
            "success": True,
            "source_fingerprint": source_fingerprint,
            "input_context": {"available_stories": []},
            "output_artifact": {
                "sprint_goal": "Persist safely",
                "sprint_number": 1,
                "duration_days": 14,
                "selected_stories": [],
                "deselected_stories": [],
                "capacity_analysis": {
                    "velocity_assumption": "Medium",
                    "capacity_band": "4-5 stories",
                    "selected_count": 0,
                    "story_points_used": 0,
                    "max_story_points": 13,
                    "commitment_note": "Fits",
                    "reasoning": "Fits",
                },
                "is_complete": True,
            },
            "is_complete": True,
        }

    payload = await generate_sprint_plan(
        project_id=7,
        load_state=load_state,
        save_state=lambda _state: None,
        current_planned_sprint_id=None,
        now_iso=lambda: "2026-05-25T00:00:00Z",
        run_sprint_agent=run_sprint_agent,
        failure_meta_builder=lambda _result, _error: {},
        team_velocity_assumption="Medium",
        sprint_duration_days=14,
        max_story_points=13,
        include_task_decomposition=True,
        selected_story_ids=None,
        user_input=None,
        load_candidates=lambda: {
            "readiness": {"status": "ready"},
            "source_fingerprint": source_fingerprint,
        },
    )

    assert payload["fsm_state"] == "SPRINT_DRAFT"
    assert state["sprint_candidate_source_fingerprint"] == source_fingerprint
    assert state["sprint_attempts"][0]["source_fingerprint"] == source_fingerprint
    assert state["sprint_plan_assessment"]["source_fingerprint"] == source_fingerprint
```

- [ ] **Step 5: Run failing attempt stamping test**

Run:

```bash
uv run --frozen pytest tests/test_sprint_phase_service.py::test_generate_sprint_plan_stamps_attempt_with_source_fingerprint -q
```

Expected: fail with missing source fields.

- [ ] **Step 6: Implement attempt stamping**

In `services/phases/sprint_service.py`, add helper:

```python
def _attach_attempt_source_fingerprint(
    state: dict[str, Any],
    *,
    source_fingerprint: str | None,
) -> None:
    if not source_fingerprint:
        return
    state["sprint_candidate_source_fingerprint"] = source_fingerprint
    attempts = ensure_sprint_attempts(state)
    if attempts:
        attempts[-1]["source_fingerprint"] = source_fingerprint
    assessment = state.get("sprint_plan_assessment")
    if isinstance(assessment, dict):
        assessment["source_fingerprint"] = source_fingerprint
```

In `generate_sprint_plan`, after `_attach_attempt_guards(...)`, call:

```python
_attach_attempt_source_fingerprint(
    state,
    source_fingerprint=cast("str | None", sprint_result.get("source_fingerprint")),
)
```

- [ ] **Step 7: Run tests**

Run:

```bash
uv run --frozen pytest tests/test_sprint_runtime.py::test_prepare_sprint_payload_preserves_source_fingerprint tests/test_sprint_phase_service.py::test_generate_sprint_plan_stamps_attempt_with_source_fingerprint -q
```

Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add services/sprint_runtime.py services/phases/sprint_service.py tests/test_sprint_runtime.py tests/test_sprint_phase_service.py
git commit -m "feat: stamp sprint drafts with source fingerprint"
```

---

## Task 3: Save Guard for Latest Complete and Current Source

**Files:**
- Modify: `services/phases/sprint_service.py`
- Test: `tests/test_sprint_phase_service.py`

- [ ] **Step 1: Write failing test for latest failed attempt blocking older complete save**

Add this test to `tests/test_sprint_phase_service.py`:

```python
@pytest.mark.asyncio
async def test_save_sprint_plan_blocks_when_latest_attempt_failed() -> None:
    """Older complete drafts cannot be saved after a later failed attempt."""
    source_fingerprint = "sha256:" + "c" * 64
    state: JsonDict = {
        "fsm_state": "SPRINT_DRAFT",
        "sprint_candidate_source_fingerprint": source_fingerprint,
        "sprint_attempts": [
            {
                "attempt_id": "sprint-attempt-4",
                "artifact_fingerprint": "sha256:reviewed",
                "source_fingerprint": source_fingerprint,
                "is_complete": True,
            },
            {
                "attempt_id": "sprint-attempt-5",
                "artifact_fingerprint": "sha256:failed",
                "source_fingerprint": source_fingerprint,
                "is_complete": False,
                "failure_stage": "invocation_exception",
            },
        ],
        "sprint_plan_assessment": {
            "attempt_id": "sprint-attempt-4",
            "artifact_fingerprint": "sha256:reviewed",
            "source_fingerprint": source_fingerprint,
            "sprint_goal": "Old draft",
            "sprint_number": 1,
            "duration_days": 14,
            "selected_stories": [],
            "deselected_stories": [],
            "capacity_analysis": {
                "velocity_assumption": "Medium",
                "capacity_band": "4-5 stories",
                "selected_count": 0,
                "story_points_used": 0,
                "max_story_points": 13,
                "commitment_note": "Fits",
                "reasoning": "Fits",
            },
            "is_complete": True,
        },
    }

    async def load_state() -> JsonDict:
        return state

    with pytest.raises(SprintPhaseError) as exc_info:
        await save_sprint_plan(
            project_id=7,
            load_state=load_state,
            save_state=lambda _state: None,
            current_planned_sprint_id=None,
            now_iso=lambda: "2026-05-25T00:00:00Z",
            hydrate_context=lambda _session_id, _project_id: None,
            build_tool_context=lambda context: context,
            save_plan_tool=lambda _input_data, _tool_context: {"success": True, "sprint_id": 9},
            team_name="Team Alpha",
            sprint_start_date="2026-05-25",
            attempt_id="sprint-attempt-4",
            expected_artifact_fingerprint="sha256:reviewed",
            expected_state="SPRINT_DRAFT",
            idempotency_key="save-stale-sprint",
            load_candidates=lambda: {
                "success": True,
                "source_fingerprint": source_fingerprint,
                "readiness": {"status": "ready"},
            },
        )

    assert "latest complete Sprint attempt" in exc_info.value.detail
```

- [ ] **Step 2: Write failing test for source fingerprint drift**

Add this test to `tests/test_sprint_phase_service.py`:

```python
@pytest.mark.asyncio
async def test_save_sprint_plan_blocks_when_candidate_source_changed() -> None:
    """Sprint save blocks when Story/dependency source changed after generation."""
    old_source = "sha256:" + "d" * 64
    current_source = "sha256:" + "e" * 64
    state: JsonDict = {
        "fsm_state": "SPRINT_DRAFT",
        "sprint_candidate_source_fingerprint": old_source,
        "sprint_attempts": [
            {
                "attempt_id": "sprint-attempt-4",
                "artifact_fingerprint": "sha256:reviewed",
                "source_fingerprint": old_source,
                "is_complete": True,
            }
        ],
        "sprint_plan_assessment": {
            "attempt_id": "sprint-attempt-4",
            "artifact_fingerprint": "sha256:reviewed",
            "source_fingerprint": old_source,
            "sprint_goal": "Old draft",
            "sprint_number": 1,
            "duration_days": 14,
            "selected_stories": [],
            "deselected_stories": [],
            "capacity_analysis": {
                "velocity_assumption": "Medium",
                "capacity_band": "4-5 stories",
                "selected_count": 0,
                "story_points_used": 0,
                "max_story_points": 13,
                "commitment_note": "Fits",
                "reasoning": "Fits",
            },
            "is_complete": True,
        },
    }

    async def load_state() -> JsonDict:
        return state

    with pytest.raises(SprintPhaseError) as exc_info:
        await save_sprint_plan(
            project_id=7,
            load_state=load_state,
            save_state=lambda _state: None,
            current_planned_sprint_id=None,
            now_iso=lambda: "2026-05-25T00:00:00Z",
            hydrate_context=lambda _session_id, _project_id: None,
            build_tool_context=lambda context: context,
            save_plan_tool=lambda _input_data, _tool_context: {"success": True, "sprint_id": 9},
            team_name="Team Alpha",
            sprint_start_date="2026-05-25",
            attempt_id="sprint-attempt-4",
            expected_artifact_fingerprint="sha256:reviewed",
            expected_state="SPRINT_DRAFT",
            idempotency_key="save-stale-source",
            load_candidates=lambda: {
                "success": True,
                "source_fingerprint": current_source,
                "readiness": {"status": "ready"},
            },
        )

    assert "Story/dependency source changed" in exc_info.value.detail
```

- [ ] **Step 3: Run failing tests**

Run:

```bash
uv run --frozen pytest tests/test_sprint_phase_service.py::test_save_sprint_plan_blocks_when_latest_attempt_failed tests/test_sprint_phase_service.py::test_save_sprint_plan_blocks_when_candidate_source_changed -q
```

Expected: fail because `save_sprint_plan` has no `load_candidates` guard and permits older complete attempts.

- [ ] **Step 4: Implement save guard**

In `save_sprint_plan`, add parameter:

```python
load_candidates: Callable[[], dict[str, Any]] | None = None,
```

After `_assert_save_guards(...)`, call:

```python
_assert_latest_complete_sprint_attempt(state=state, attempt_id=attempt_id)
_assert_sprint_source_current(
    state=state,
    assessment=assessment,
    load_candidates=load_candidates,
    project_id=project_id,
)
```

Add helpers:

```python
def _assert_latest_complete_sprint_attempt(
    *,
    state: dict[str, Any],
    attempt_id: str | None,
) -> None:
    attempts = ensure_sprint_attempts(state)
    latest_attempt = attempts[-1] if attempts else None
    if (
        not isinstance(latest_attempt, dict)
        or latest_attempt.get("attempt_id") != attempt_id
        or latest_attempt.get("is_complete") is not True
    ):
        raise SprintPhaseError(
            "Sprint save requires the latest complete Sprint attempt. "
            "Regenerate the Sprint after any failed attempt before saving.",
        )
```

```python
def _assert_sprint_source_current(
    *,
    state: dict[str, Any],
    assessment: dict[str, Any],
    load_candidates: Callable[[], dict[str, Any]] | None,
    project_id: int,
) -> None:
    candidate_payload = load_candidates() if load_candidates is not None else load_sprint_candidates(project_id)
    current_source = candidate_payload.get("source_fingerprint")
    draft_source = assessment.get("source_fingerprint") or state.get("sprint_candidate_source_fingerprint")
    if isinstance(current_source, str) and isinstance(draft_source, str) and current_source != draft_source:
        raise SprintPhaseError(
            "Sprint save blocked: Story/dependency source changed after this "
            "Sprint draft was generated. Regenerate Sprint before saving.",
        )
```

In `services/agent_workbench/sprint_phase.py`, pass `load_candidates=lambda: load_sprint_candidates(project_id)` or rely on the default in `save_sprint_plan`.

- [ ] **Step 5: Run tests**

Run:

```bash
uv run --frozen pytest tests/test_sprint_phase_service.py::test_save_sprint_plan_blocks_when_latest_attempt_failed tests/test_sprint_phase_service.py::test_save_sprint_plan_blocks_when_candidate_source_changed -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add services/phases/sprint_service.py services/agent_workbench/sprint_phase.py tests/test_sprint_phase_service.py
git commit -m "fix: block stale sprint draft saves"
```

---

## Task 4: Workflow Next Save Advertising Guard

**Files:**
- Modify: `services/agent_workbench/application.py`
- Test: `tests/test_agent_workbench_application.py`

- [ ] **Step 1: Write failing test**

Add this test to `tests/test_agent_workbench_application.py`:

```python
def test_workflow_next_hides_sprint_save_after_latest_failed_attempt() -> None:
    """Workflow next must not advertise saving an older complete draft."""
    app = AgentWorkbenchApplication(
        read_projection=FakeReadProjection(
            workflow_state={
                "project_id": 7,
                "state": {
                    "fsm_state": "SPRINT_DRAFT",
                    "sprint_attempts": [
                        {
                            "attempt_id": "sprint-attempt-4",
                            "artifact_fingerprint": "sha256:reviewed",
                            "is_complete": True,
                        },
                        {
                            "attempt_id": "sprint-attempt-5",
                            "artifact_fingerprint": "sha256:failed",
                            "is_complete": False,
                            "failure_stage": "invocation_exception",
                        },
                    ],
                    "sprint_plan_assessment": {
                        "attempt_id": "sprint-attempt-4",
                        "artifact_fingerprint": "sha256:reviewed",
                        "is_complete": True,
                    },
                },
                "source_fingerprint": "sha256:" + "1" * 64,
            }
        )
    )

    result = app.workflow_next(project_id=7)

    assert result["ok"] is True
    commands = result["data"]["next_valid_commands"]
    assert any(command.startswith("agileforge sprint generate") for command in commands)
    assert not any(command.startswith("agileforge sprint save") for command in commands)
    assert result["data"]["blocked_commands"] == [
        {
            "command": "agileforge sprint save",
            "reason_code": "SPRINT_DRAFT_NOT_LATEST_COMPLETE",
            "details": {
                "latest_attempt_id": "sprint-attempt-5",
                "draft_attempt_id": "sprint-attempt-4",
            },
        }
    ]
```

Adjust `FakeReadProjection` construction to match the existing helper shape in the file.

- [ ] **Step 2: Run failing test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py::test_workflow_next_hides_sprint_save_after_latest_failed_attempt -q
```

Expected: fail because `sprint save` is currently always advertised in `SPRINT_DRAFT`.

- [ ] **Step 3: Implement workflow guard**

In `services/agent_workbench/application.py`, add helper:

```python
def _sprint_save_blocker(workflow: dict[str, Any]) -> dict[str, Any] | None:
    state = _envelope_data(workflow).get("state")
    state_data = state if isinstance(state, dict) else {}
    assessment = state_data.get("sprint_plan_assessment")
    attempts = state_data.get("sprint_attempts")
    if not isinstance(assessment, dict) or assessment.get("is_complete") is not True:
        return {"reason_code": "SPRINT_DRAFT_INCOMPLETE", "details": {}}
    if not isinstance(attempts, list) or not attempts:
        return {"reason_code": "SPRINT_DRAFT_NO_ATTEMPT", "details": {}}
    latest = attempts[-1]
    if not isinstance(latest, dict):
        return {"reason_code": "SPRINT_DRAFT_NO_ATTEMPT", "details": {}}
    if (
        latest.get("is_complete") is not True
        or latest.get("attempt_id") != assessment.get("attempt_id")
        or latest.get("artifact_fingerprint") != assessment.get("artifact_fingerprint")
    ):
        return {
            "reason_code": "SPRINT_DRAFT_NOT_LATEST_COMPLETE",
            "details": {
                "latest_attempt_id": latest.get("attempt_id"),
                "draft_attempt_id": assessment.get("attempt_id"),
            },
        }
    return None
```

Change `_sprint_workflow_next` or `_sprint_command_candidates` so `sprint save` is omitted when this blocker is present, and add a blocked command payload:

```python
{
    "command": "agileforge sprint save",
    **save_blocker,
}
```

- [ ] **Step 4: Run test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py::test_workflow_next_hides_sprint_save_after_latest_failed_attempt -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add services/agent_workbench/application.py tests/test_agent_workbench_application.py
git commit -m "fix: hide stale sprint save command"
```

---

## Task 5: Invalidate Sprint Working State on Upstream Mutation

**Files:**
- Modify: `services/agent_workbench/story_phase.py`
- Test: `tests/test_agent_workbench_story_phase.py`

- [ ] **Step 1: Write failing dependency-apply invalidation test**

Add this test to `tests/test_agent_workbench_story_phase.py` near dependency tests:

```python
def test_dependency_apply_invalidates_unsaved_sprint_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    """Applying dependencies clears stale unsaved Sprint attempts."""
    runner = StoryPhaseRunner()
    state = {
        "fsm_state": "SPRINT_DRAFT",
        "sprint_attempts": [{"attempt_id": "sprint-attempt-4", "is_complete": True}],
        "sprint_plan_assessment": {"attempt_id": "sprint-attempt-4", "is_complete": True},
        "sprint_last_input_context": {"available_stories": []},
        "story_dependency_attempts": [
            {
                "attempt_id": "story-dependencies-1",
                "artifact_fingerprint": "sha256:deps",
                "edges": [],
            }
        ],
    }

    monkeypatch.setattr(runner, "_load_project", lambda _project_id: object())
    monkeypatch.setattr(runner, "_ensure_session", lambda _session_id: state)
    monkeypatch.setattr(
        "services.agent_workbench.story_phase._dependency_guard_error",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "services.agent_workbench.story_phase._find_dependency_attempt",
        lambda *_args, **_kwargs: state["story_dependency_attempts"][0],
    )
    monkeypatch.setattr(
        "services.agent_workbench.story_phase._apply_dependency_attempt",
        lambda *_args, **_kwargs: {
            "success": True,
            "project_id": 7,
            "attempt_id": "story-dependencies-1",
            "artifact_fingerprint": "sha256:deps",
            "activated_edge_count": 0,
            "activated_edges": [],
            "rejected_edge_count": 0,
            "rejected_edges": [],
            "active_edge_count": 0,
            "cycle_count_after_apply": 0,
        },
    )

    saved = {}
    monkeypatch.setattr(
        runner,
        "_save_session_state",
        lambda _session_id, updated: saved.update(updated),
    )

    result = runner.dependency_apply(
        project_id=7,
        attempt_id="story-dependencies-1",
        expected_artifact_fingerprint="sha256:deps",
        expected_state="SPRINT_DRAFT",
        idempotency_key="apply-deps",
    )

    assert result["ok"] is True
    assert saved["fsm_state"] == "SPRINT_SETUP"
    assert saved["sprint_attempts"] == []
    assert saved["sprint_plan_assessment"] is None
```

If this test is too coupled to SQL session setup, instead add a focused unit test for a new helper `_invalidate_unsaved_sprint_working_set(state, reason, now_iso)` and a smaller integration assertion around `_dependency_apply`.

- [ ] **Step 2: Run failing test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_story_phase.py::test_dependency_apply_invalidates_unsaved_sprint_draft -q
```

Expected: fail because dependency apply leaves Sprint attempts intact.

- [ ] **Step 3: Implement invalidation helper**

In `services/agent_workbench/story_phase.py`, import:

```python
from services.phases.sprint_service import reset_sprint_planner_working_set
```

Add helper:

```python
def _invalidate_unsaved_sprint_working_set(
    state: dict[str, Any],
    *,
    reason: str,
    now_iso: str,
) -> None:
    if state.get("sprint_planner_owner_sprint_id") is not None:
        return
    if state.get("fsm_state") not in {"SPRINT_SETUP", "SPRINT_DRAFT"}:
        return
    reset_sprint_planner_working_set(state)
    state["fsm_state"] = OrchestratorState.SPRINT_SETUP.value
    state["fsm_state_entered_at"] = now_iso
    state["sprint_invalidated_reason"] = reason
    state["sprint_invalidated_at"] = now_iso
```

Call it after successful dependency apply and before `_save_session_state`:

```python
_invalidate_unsaved_sprint_working_set(
    state,
    reason="story_dependencies_applied",
    now_iso=_now_iso(),
)
```

Also call it after successful Story save and Story reopen when the resulting state is or was in Sprint setup/draft:

```python
_invalidate_unsaved_sprint_working_set(
    state,
    reason="story_saved",
    now_iso=_now_iso(),
)
```

Use the state object passed through the save callback so the invalidation is persisted in the same session write.

- [ ] **Step 4: Run test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_story_phase.py::test_dependency_apply_invalidates_unsaved_sprint_draft -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add services/agent_workbench/story_phase.py tests/test_agent_workbench_story_phase.py
git commit -m "fix: invalidate sprint drafts after story dependency changes"
```

---

## Task 6: Docs and Final Verification

**Files:**
- Modify: `docs/agent-cli-manual.md`

- [ ] **Step 1: Update docs**

Add a short Sprint safety note:

```markdown
### Sprint Draft Freshness

Sprint drafts are saveable only when they are the latest complete Sprint attempt
and were generated from the current Story/dependency candidate source. If a
Story is reopened/saved or dependency edges are applied, AgileForge clears the
unsaved Sprint draft and returns to Sprint setup. If a Sprint regeneration fails,
older complete attempts remain visible in history but cannot be saved; rerun
`agileforge sprint generate --project-id <id> --input "<feedback>"` after fixing
provider/runtime issues.
```

- [ ] **Step 2: Run focused tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_sprint_runtime.py \
  tests/test_sprint_phase_service.py \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_story_phase.py \
  -q
```

Expected: all pass.

- [ ] **Step 3: Run full verification**

Run:

```bash
uv run --frozen pytest -q
uv run --frozen ruff check .
node --check frontend/project.js
git diff --check
```

Expected:

- pytest: all tests pass.
- ruff: `All checks passed!`
- node: exit code 0.
- diff check: exit code 0.

- [ ] **Step 4: Commit docs**

```bash
git add docs/agent-cli-manual.md
git commit -m "docs: document sprint draft freshness"
```

---

## Acceptance Criteria

1. After a failed latest Sprint attempt, `workflow next` does not advertise `sprint save`.
2. `sprint save` fails when the requested attempt is not the latest complete attempt.
3. `sprint save` fails when current Story/dependency candidate source differs from the attempt source.
4. Story save/reopen and dependency apply clear unsaved Sprint draft working state when no Sprint has been persisted.
5. Older complete attempts remain visible in `sprint history` for audit, but cannot be persisted after stale source or a newer failed attempt.
6. Provider 429 failures remain hard failures and do not save anything.

## Manual caRtola Verification

After implementation, use project `2`:

```bash
agileforge workflow next --project-id 2
agileforge sprint history --project-id 2
agileforge sprint generate --project-id 2 --input "Regenerate after corrected Story 71 and applied dependency graph. Use locked deterministic cohort."
```

Expected after current failed `sprint-attempt-5`:

- Before a successful regeneration, `workflow next` offers `sprint generate --input <feedback>` and `sprint history`.
- It does not offer `sprint save`.
- A direct `sprint save` using attempt `4` fails.
- A successful new complete attempt becomes the only saveable attempt.

