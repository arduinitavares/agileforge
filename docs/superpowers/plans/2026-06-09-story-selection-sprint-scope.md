# Story Selection Sprint Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow AgileForge to enter Sprint setup from an explicit saved Story parent-requirement selection while preserving Story quality gates, traceability, and dependency readiness.

**Architecture:** Extend the existing Story completion scope contract with a new `selection` scope resolved by parent requirement names in roadmap order. Reuse the existing `story_completion_scope` Sprint candidate filter, and add one readiness blocker for selected stories that depend on excluded stories. Expose the feature through service, API, CLI, workbench command schema, `workflow next`, and the browser UI.

**Tech Stack:** Python 3.13, FastAPI/Pydantic, SQLModel/SQLAlchemy, pytest, vanilla browser JavaScript, Node test runner.

---

## File Structure

- Modify `services/phases/story_service.py`
  - Resolve `scope=selection`, normalize duplicate parent requirements, derive deterministic `selection:<canonical_hash>` scope IDs, and pass `parent_requirements` through `complete_story_phase`.
- Modify `services/sprint_input.py`
  - After scope filtering, mark readiness blocked when selected candidates reference excluded dependency story IDs.
- Modify `services/agent_workbench/application.py`
  - Pass selection input into the Story runner and advertise an installed selection completion command from `workflow next`.
- Modify `services/agent_workbench/story_phase.py`
  - Accept `parent_requirements` in the Story runner `complete` method and route it to the service.
- Modify `services/agent_workbench/command_registry.py`
  - Add `parent_requirement` to the optional Story complete command schema.
- Modify `cli/main.py`
  - Add `--scope selection` and repeatable `--parent-requirement`, route the list to `AgentWorkbenchApplication.story_complete`.
- Modify `api.py`
  - Add `parent_requirements: list[str] = []` to `StoryCompleteRequest`, route it to `complete_story_phase_service`.
- Modify `frontend/project.html`
  - Add a selection completion affordance next to the existing whole-phase Story completion button.
- Modify `frontend/project.js`
  - Track selected saved/merged requirements, post guarded selection completion payloads, and keep whole-phase completion unchanged.
- Modify tests:
  - `tests/test_story_phase_service.py`
  - `tests/test_sprint_planner_tools.py`
  - `tests/test_agent_workbench_read_projection.py`
  - `tests/test_agent_workbench_application.py`
  - `tests/test_agent_workbench_cli.py`
  - `tests/test_agent_workbench_command_schema.py`
  - `tests/test_api_story_interview_flow.py`
  - Create `tests/test_story_selection_scope_ui.mjs`

---

### Task 1: Story Service Selection Scope

**Files:**
- Modify: `services/phases/story_service.py`
- Test: `tests/test_story_phase_service.py`

- [ ] **Step 1: Add failing Story service tests**

Append these tests after `test_complete_story_phase_allows_saved_milestone_scope_with_pending_later_milestone` in `tests/test_story_phase_service.py`:

```python
@pytest.mark.asyncio
async def test_complete_story_phase_allows_saved_selection_scope_in_roadmap_order() -> None:  # noqa: E501
    """Verify selection scope completes only selected saved parent requirements."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [
            {"items": ["Enable login", "Reset password"]},
            {"items": ["Invite teammates"]},
        ],
        "story_saved": {"Enable login": True, "Reset password": True},
    }
    saved_states: list[JsonDict] = []

    payload = await complete_story_phase(
        expected_state="STORY_PERSISTENCE",
        idempotency_key="complete-story-selection-login",
        scope="selection",
        parent_requirements=[
            " reset password ",
            "Enable login",
            "ENABLE LOGIN",
        ],
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-06-09T12:00:00Z",
    )

    selected_requirements = ["Enable login", "Reset password"]
    expected_scope = {
        "schema_version": "agileforge.story_completion_scope.v1",
        "scope": "selection",
        "scope_id": "selection:"
        + canonical_hash(
            {"scope": "selection", "requirements": selected_requirements}
        ),
        "requirements": selected_requirements,
        "completed_at": "2026-06-09T12:00:00Z",
    }
    assert payload == {
        "fsm_state": "SPRINT_SETUP",
        "coverage": {"saved": 2, "merged": 0, "total": 2},
        "idempotency_key": "complete-story-selection-login",
        "story_completion_scope": expected_scope,
    }
    assert state["fsm_state"] == "SPRINT_SETUP"
    assert state["story_completion_scope"] == expected_scope
    assert len(saved_states) == 1


@pytest.mark.asyncio
async def test_complete_story_phase_rejects_empty_selection_scope() -> None:
    """Verify selection scope requires at least one parent requirement."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [{"items": ["Enable login"]}],
        "story_saved": {"Enable login": True},
    }

    with pytest.raises(StoryPhaseError) as exc_info:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-empty-selection",
            scope="selection",
            parent_requirements=[" ", ""],
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-06-09T12:00:00Z",
        )

    assert exc_info.value.status_code == 400  # noqa: PLR2004
    assert exc_info.value.detail == (
        "story complete --scope selection requires at least one "
        "--parent-requirement"
    )
    assert state["fsm_state"] == "STORY_PERSISTENCE"


@pytest.mark.asyncio
async def test_complete_story_phase_rejects_unknown_selection_requirement() -> None:
    """Verify selection scope rejects parent requirements outside the roadmap."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [{"items": ["Enable login"]}],
        "story_saved": {"Enable login": True},
    }

    with pytest.raises(StoryPhaseError) as exc_info:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-unknown-selection",
            scope="selection",
            parent_requirements=["Missing requirement"],
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-06-09T12:00:00Z",
        )

    assert exc_info.value.status_code == 400  # noqa: PLR2004
    assert exc_info.value.detail == (
        "Story completion selection includes unknown roadmap requirement: "
        "Missing requirement."
    )
    assert state["fsm_state"] == "STORY_PERSISTENCE"


@pytest.mark.asyncio
async def test_complete_story_phase_rejects_unsaved_selection_requirement() -> None:
    """Verify selection scope still requires saved or merged Story output."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [{"items": ["Enable login", "Reset password"]}],
        "story_saved": {"Enable login": True},
    }

    selected_requirements = ["Enable login", "Reset password"]
    expected_scope_id = "selection:" + canonical_hash(
        {"scope": "selection", "requirements": selected_requirements}
    )
    with pytest.raises(StoryPhaseError) as exc_info:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-unsaved-selection",
            scope="selection",
            parent_requirements=selected_requirements,
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-06-09T12:00:00Z",
        )

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert exc_info.value.detail == (
        f"Story phase cannot complete for {expected_scope_id}: "
        "1 of 2 roadmap requirements are saved or merged."
    )
    assert state["fsm_state"] == "STORY_PERSISTENCE"


@pytest.mark.asyncio
async def test_complete_story_phase_rejects_selection_argument_combinations() -> None:
    """Verify selection flags are only accepted for selection completion."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [{"items": ["Enable login"]}],
        "story_saved": {"Enable login": True},
    }

    with pytest.raises(StoryPhaseError) as full_exc:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-parent-no-scope",
            parent_requirements=["Enable login"],
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-06-09T12:00:00Z",
        )
    assert full_exc.value.status_code == 400  # noqa: PLR2004
    assert full_exc.value.detail == (
        "--parent-requirement is only supported with --scope selection"
    )

    with pytest.raises(StoryPhaseError) as milestone_exc:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-parent-milestone",
            scope="milestone",
            scope_id="milestone_0",
            parent_requirements=["Enable login"],
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-06-09T12:00:00Z",
        )
    assert milestone_exc.value.status_code == 400  # noqa: PLR2004
    assert milestone_exc.value.detail == (
        "--parent-requirement is only supported with --scope selection"
    )

    with pytest.raises(StoryPhaseError) as scope_id_exc:
        await complete_story_phase(
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-selection-scope-id",
            scope="selection",
            scope_id="selection:manual",
            parent_requirements=["Enable login"],
            load_state=lambda: _async_value(state),
            save_state=lambda updated: None,  # noqa: ARG005
            now_iso=lambda: "2026-06-09T12:00:00Z",
        )
    assert scope_id_exc.value.status_code == 400  # noqa: PLR2004
    assert scope_id_exc.value.detail == (
        "story complete --scope selection does not accept --scope-id"
    )
```

- [ ] **Step 2: Run the Story service tests and verify failure**

Run:

```bash
pytest tests/test_story_phase_service.py -k "complete_story_phase" -q
```

Expected before implementation: the new selection tests fail with `TypeError: complete_story_phase() got an unexpected keyword argument 'parent_requirements'` or `Unsupported story completion scope: selection`.

- [ ] **Step 3: Implement selection scope resolution**

In `services/phases/story_service.py`, add the schema constant near `_STORY_QUALITY_SCHEMA_VERSION`:

```python
_STORY_COMPLETION_SCOPE_SCHEMA_VERSION = "agileforge.story_completion_scope.v1"
```

Add these helpers above `_story_completion_scope_requirements`:

```python
def _normalized_parent_requirements(values: list[str] | None) -> list[str]:
    """Return non-empty parent requirement inputs in caller order."""
    if values is None:
        return []
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def _selection_scope_id(requirements: list[str]) -> str:
    """Return deterministic selection scope id for roadmap-ordered requirements."""
    return "selection:" + canonical_hash(
        {"scope": "selection", "requirements": requirements}
    )


def _roadmap_ordered_selection_requirements(
    state: dict[str, Any],
    parent_requirements: list[str] | None,
) -> list[str]:
    """Resolve selected parent requirements to canonical roadmap names."""
    requested = _normalized_parent_requirements(parent_requirements)
    if not requested:
        raise StoryPhaseError(
            "story complete --scope selection requires at least one "
            "--parent-requirement",
            status_code=400,
        )

    roadmap_requirements = get_all_roadmap_requirements(state)
    roadmap_by_key = {
        normalize_requirement_key(requirement): requirement
        for requirement in roadmap_requirements
        if normalize_requirement_key(requirement)
    }
    requested_keys: set[str] = set()
    unknown: list[str] = []
    for requirement in requested:
        key = normalize_requirement_key(requirement)
        if not key:
            continue
        if key not in roadmap_by_key:
            unknown.append(requirement)
            continue
        requested_keys.add(key)

    if unknown:
        names = ", ".join(unknown)
        plural = "s" if len(unknown) != 1 else ""
        raise StoryPhaseError(
            f"Story completion selection includes unknown roadmap requirement{plural}: "
            f"{names}.",
            status_code=400,
        )

    return [
        requirement
        for requirement in roadmap_requirements
        if normalize_requirement_key(requirement) in requested_keys
    ]
```

Change `_story_completion_scope_requirements` signature to:

```python
def _story_completion_scope_requirements(
    state: dict[str, Any],
    *,
    scope: str | None,
    scope_id: str | None,
    parent_requirements: list[str] | None = None,
) -> tuple[list[str], dict[str, Any] | None]:
```

Replace its first branch and scope validation with this structure:

```python
    normalized_scope = scope.strip() if isinstance(scope, str) else None
    normalized_scope_id = scope_id.strip() if isinstance(scope_id, str) else None
    normalized_parent_requirements = _normalized_parent_requirements(
        parent_requirements
    )

    if normalized_parent_requirements and normalized_scope != "selection":
        raise StoryPhaseError(
            "--parent-requirement is only supported with --scope selection",
            status_code=400,
        )
    if not normalized_scope and not normalized_scope_id:
        return get_all_roadmap_requirements(state), None
    if normalized_scope == "selection":
        if normalized_scope_id:
            raise StoryPhaseError(
                "story complete --scope selection does not accept --scope-id",
                status_code=400,
            )
        requirements = _roadmap_ordered_selection_requirements(
            state,
            parent_requirements,
        )
        return requirements, {
            "schema_version": _STORY_COMPLETION_SCOPE_SCHEMA_VERSION,
            "scope": "selection",
            "scope_id": _selection_scope_id(requirements),
            "requirements": requirements,
        }
    if not normalized_scope or not normalized_scope_id:
        raise StoryPhaseError(
            "story complete scope requires both --scope and --scope-id",
            status_code=400,
        )
    if normalized_scope != "milestone":
        raise StoryPhaseError(
            f"Unsupported story completion scope: {normalized_scope}",
            status_code=400,
        )
```

In the milestone return payload, replace the literal schema version with:

```python
        "schema_version": _STORY_COMPLETION_SCOPE_SCHEMA_VERSION,
```

Change `complete_story_phase` to accept and pass `parent_requirements`:

```python
async def complete_story_phase(
    *,
    expected_state: str,
    idempotency_key: str,
    scope: str | None = None,
    scope_id: str | None = None,
    parent_requirements: list[str] | None = None,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
) -> dict[str, Any]:
```

And update the resolver call:

```python
    req_names, scope_payload = _story_completion_scope_requirements(
        state,
        scope=scope,
        scope_id=scope_id,
        parent_requirements=parent_requirements,
    )
```

- [ ] **Step 4: Run the Story service tests and verify pass**

Run:

```bash
pytest tests/test_story_phase_service.py -k "complete_story_phase" -q
```

Expected after implementation: all selected tests pass, and existing full/milestone completion tests still pass.

- [ ] **Step 5: Commit Story service selection scope**

Run:

```bash
git add services/phases/story_service.py tests/test_story_phase_service.py
git commit -m "feat(story): complete selected story scope"
```

---

### Task 2: Sprint Candidate Dependency Guard

**Files:**
- Modify: `services/sprint_input.py`
- Test: `tests/test_agent_workbench_read_projection.py`
- Test: `tests/test_sprint_planner_tools.py`

- [ ] **Step 1: Add failing read projection test for scoped external dependencies**

Append this test after `test_sprint_candidates_filters_to_story_completion_scope` in `tests/test_agent_workbench_read_projection.py`:

```python
def test_sprint_candidates_blocks_selection_with_external_dependency(
    session: Session,
) -> None:
    """Selection-scoped candidates should block when dependencies point outside scope."""
    product = Product(name="Scoped Dependency Project", description="Demo")
    session.add(product)
    session.commit()
    session.refresh(product)
    product_id = require_id(product.product_id, "product_id")
    selected = UserStory(
        product_id=product_id,
        title="Selected story",
        story_description="Ready",
        acceptance_criteria="- AC",
        status=StoryStatus.TO_DO,
        is_refined=True,
        rank="1",
        story_points=2,
        source_requirement="Research slice",
    )
    excluded = UserStory(
        product_id=product_id,
        title="Excluded dependency",
        story_description="Later",
        acceptance_criteria="- AC",
        status=StoryStatus.TO_DO,
        is_refined=True,
        rank="2",
        story_points=3,
        source_requirement="Later slice",
    )
    session.add_all([selected, excluded])
    session.commit()
    session.refresh(selected)
    session.refresh(excluded)
    selected.prerequisite_story_ids = [require_id(excluded.story_id, "story_id")]
    session.add(selected)
    session.commit()

    service = ReadProjectionService(
        engine=_engine(session),
        session_reader=cast(
            "ReadOnlySessionReader",
            _FakeSessionReader(
                {
                    "fsm_state": "SPRINT_SETUP",
                    "story_completion_scope": {
                        "scope": "selection",
                        "scope_id": "selection:sha256:fixture",
                        "requirements": ["Research slice"],
                    },
                }
            ),
        ),
    )

    result = service.sprint_candidates(project_id=product_id)

    assert result["ok"] is True
    readiness = result["data"]["readiness"]
    assert readiness["status"] == "blocked"
    assert readiness["blocking_codes"] == ["SPRINT_SCOPE_EXTERNAL_DEPENDENCY"]
    assert readiness["blocking_story_ids"] == [selected.story_id]
    assert readiness["external_dependency_story_ids"] == [excluded.story_id]
```

- [ ] **Step 2: Add failing direct candidate-loader test**

Append this test after `test_fetch_sprint_candidates_reports_blocked_readiness_for_unsized_rows` in `tests/test_sprint_planner_tools.py`:

```python
def test_load_sprint_candidates_blocks_scope_external_dependency(monkeypatch) -> None:  # noqa: ANN001
    """Scoped loader should block selected candidates that depend on excluded rows."""
    def fake_fetch_candidates(*, product_id: int) -> dict[str, object]:
        assert product_id == 77
        return {
            "success": True,
            "stories": [
                {
                    "story_id": 10,
                    "story_title": "Selected",
                    "story_points": 3,
                    "priority": 1,
                    "source_requirement": "Research slice",
                    "prerequisite_story_ids": [20],
                    "blocked_by_story_ids": [],
                },
                {
                    "story_id": 20,
                    "story_title": "Excluded",
                    "story_points": 2,
                    "priority": 2,
                    "source_requirement": "Later slice",
                    "prerequisite_story_ids": [],
                    "blocked_by_story_ids": [],
                },
            ],
        }

    monkeypatch.setattr(
        "services.sprint_input.fetch_sprint_candidates",
        fake_fetch_candidates,
    )

    result = load_sprint_candidates(
        77,
        story_completion_scope={
            "scope": "selection",
            "scope_id": "selection:sha256:fixture",
            "requirements": ["Research slice"],
        },
    )

    assert result["success"] is True
    assert [story["story_id"] for story in result["stories"]] == [10]
    assert result["readiness"]["status"] == "blocked"
    assert result["readiness"]["blocking_codes"] == [
        "SPRINT_SCOPE_EXTERNAL_DEPENDENCY"
    ]
    assert result["readiness"]["blocking_story_ids"] == [10]
    assert result["readiness"]["external_dependency_story_ids"] == [20]
```

Add the import near the other sprint input imports in `tests/test_sprint_planner_tools.py`:

```python
from services.sprint_input import load_sprint_candidates
```

- [ ] **Step 3: Run dependency tests and verify failure**

Run:

```bash
pytest tests/test_agent_workbench_read_projection.py::test_sprint_candidates_blocks_selection_with_external_dependency tests/test_sprint_planner_tools.py::test_load_sprint_candidates_blocks_scope_external_dependency -q
```

Expected before implementation: assertions fail because readiness is still `ready` after filtering.

- [ ] **Step 4: Implement external dependency readiness augmentation**

In `services/sprint_input.py`, add this helper after `_sprint_candidate_readiness`:

```python
def _augment_readiness_with_scope_external_dependencies(
    readiness: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Block readiness when selected stories depend on excluded story ids."""
    selected_ids = {
        int(candidate["story_id"])
        for candidate in candidates
        if normalize_positive_int(candidate.get("story_id")) is not None
    }
    blocking_story_ids: set[int] = set()
    external_dependency_ids: set[int] = set()
    for candidate in candidates:
        candidate_id = normalize_positive_int(candidate.get("story_id"))
        if candidate_id is None:
            continue
        dependency_ids = [
            normalize_positive_int(item)
            for item in [
                *list(candidate.get("prerequisite_story_ids") or []),
                *list(candidate.get("blocked_by_story_ids") or []),
            ]
        ]
        external_ids = [
            dependency_id
            for dependency_id in dependency_ids
            if dependency_id is not None and dependency_id not in selected_ids
        ]
        if external_ids:
            blocking_story_ids.add(candidate_id)
            external_dependency_ids.update(external_ids)

    if not external_dependency_ids:
        return readiness

    updated = dict(readiness)
    blocking_codes = list(updated.get("blocking_codes") or [])
    if "SPRINT_SCOPE_EXTERNAL_DEPENDENCY" not in blocking_codes:
        blocking_codes.append("SPRINT_SCOPE_EXTERNAL_DEPENDENCY")
    existing_blocking_story_ids = {
        int(story_id)
        for story_id in (updated.get("blocking_story_ids") or [])
        if normalize_positive_int(story_id) is not None
    }
    updated["status"] = "blocked"
    updated["blocking_codes"] = blocking_codes
    updated["blocking_story_ids"] = sorted(
        existing_blocking_story_ids | blocking_story_ids
    )
    updated["external_dependency_story_ids"] = sorted(external_dependency_ids)
    return updated
```

In `apply_story_completion_scope_to_candidate_result`, replace the readiness assignment:

```python
    result["readiness"] = _augment_readiness_with_scope_external_dependencies(
        _sprint_candidate_readiness(filtered),
        filtered,
    )
```

- [ ] **Step 5: Run dependency tests and Sprint generation guard test**

Run:

```bash
pytest tests/test_agent_workbench_read_projection.py::test_sprint_candidates_blocks_selection_with_external_dependency tests/test_sprint_planner_tools.py::test_load_sprint_candidates_blocks_scope_external_dependency tests/test_api_sprint_flow.py::test_sprint_candidates_endpoint_returns_normalized_items -q
```

Expected after implementation: all selected tests pass. `generate_sprint_plan` already refuses any candidate payload with `readiness.status == "blocked"`, so no new Sprint service branch is required.

- [ ] **Step 6: Commit Sprint dependency guard**

Run:

```bash
git add services/sprint_input.py tests/test_agent_workbench_read_projection.py tests/test_sprint_planner_tools.py
git commit -m "feat(sprint): block scoped external dependencies"
```

---

### Task 3: API, CLI, Workbench, And Command Schema Plumbing

**Files:**
- Modify: `api.py`
- Modify: `cli/main.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/story_phase.py`
- Modify: `services/agent_workbench/command_registry.py`
- Test: `tests/test_api_story_interview_flow.py`
- Test: `tests/test_agent_workbench_cli.py`
- Test: `tests/test_agent_workbench_application.py`
- Test: `tests/test_agent_workbench_command_schema.py`

- [ ] **Step 1: Add failing API routing assertion**

In `tests/test_api_story_interview_flow.py`, update `fake_complete_story_phase_service` inside `test_story_complete_phase_requires_and_passes_guard_body` to accept and assert `parent_requirements`:

```python
    async def fake_complete_story_phase_service(  # noqa: PLR0913
        *,
        expected_state: str,
        idempotency_key: str,
        scope: str | None,
        scope_id: str | None,
        parent_requirements: list[str] | None,
        load_state: object,
        save_state: object,
        now_iso: object,
    ) -> dict[str, object]:
        assert expected_state == "STORY_PERSISTENCE"
        assert idempotency_key == "story-complete-api"
        assert scope == "selection"
        assert scope_id is None
        assert parent_requirements == ["Requirement A", "Requirement B"]
        assert load_state is not None
        assert save_state is not None
        assert now_iso is not None
        return {
            "fsm_state": "SPRINT_SETUP",
            "coverage": {"saved": 2, "merged": 0, "total": 2},
            "idempotency_key": "story-complete-api",
        }
```

Change the request JSON in that test to:

```python
        json={
            "expected_state": "STORY_PERSISTENCE",
            "idempotency_key": "story-complete-api",
            "scope": "selection",
            "parent_requirements": ["Requirement A", "Requirement B"],
        },
```

Change the expected response coverage to:

```python
            "coverage": {"saved": 2, "merged": 0, "total": 2},
```

- [ ] **Step 2: Add failing CLI selection route case**

In `tests/test_agent_workbench_cli.py`, add this parameter case after the existing milestone `story complete` case:

```python
        (
            [
                "story",
                "complete",
                "--project-id",
                str(PROJECT_ID),
                "--expected-state",
                "STORY_PERSISTENCE",
                "--idempotency-key",
                "complete-story-selection",
                "--scope",
                "selection",
                "--parent-requirement",
                "Technology and Model Research Spike",
                "--parent-requirement",
                "Python Project Scaffold and uv Management Setup",
            ],
            (
                "story_complete",
                {
                    "project_id": PROJECT_ID,
                    "expected_state": "STORY_PERSISTENCE",
                    "idempotency_key": "complete-story-selection",
                    "scope": "selection",
                    "scope_id": None,
                    "parent_requirements": [
                        "Technology and Model Research Spike",
                        "Python Project Scaffold and uv Management Setup",
                    ],
                },
            ),
            "agileforge story complete",
        ),
```

Update the `_Application` protocol and fake implementation in that test to accept `parent_requirements: list[str] | None = None` and include it in `call_args` only when not `None`.

- [ ] **Step 3: Add failing workbench facade and command schema assertions**

In `tests/test_agent_workbench_application.py`, update the existing milestone `app.story_complete` call block to add a new selection call:

```python
    assert (
        app.story_complete(
            project_id=PROJECT_ID,
            expected_state="STORY_PERSISTENCE",
            idempotency_key="complete-story-selection",
            scope="selection",
            parent_requirements=[
                "Technology and Model Research Spike",
                "Python Project Scaffold and uv Management Setup",
            ],
        )["data"]["fsm_state"]
        == "SPRINT_SETUP"
    )
```

Update the expected runner call list to include:

```python
        (
            "complete",
            {
                "project_id": PROJECT_ID,
                "expected_state": "STORY_PERSISTENCE",
                "idempotency_key": "complete-story-selection",
                "scope": "selection",
                "scope_id": None,
                "parent_requirements": [
                    "Technology and Model Research Spike",
                    "Python Project Scaffold and uv Management Setup",
                ],
            },
        ),
```

In `tests/test_agent_workbench_command_schema.py`, change:

```python
    assert complete["input"]["optional"] == ["scope", "scope_id"]
```

to:

```python
    assert complete["input"]["optional"] == [
        "scope",
        "scope_id",
        "parent_requirement",
    ]
```

- [ ] **Step 4: Run plumbing tests and verify failure**

Run:

```bash
pytest tests/test_api_story_interview_flow.py::test_story_complete_phase_requires_and_passes_guard_body tests/test_agent_workbench_cli.py -k "story_complete or story_phase_commands" tests/test_agent_workbench_application.py -k "story_complete" tests/test_agent_workbench_command_schema.py::test_story_command_contracts -q
```

Expected before implementation: failures report missing `parent_requirements`, invalid `selection` choice, or command schema optional mismatch.

- [ ] **Step 5: Implement API and service runner plumbing**

In `api.py`, change `StoryCompleteRequest` to:

```python
class StoryCompleteRequest(BaseModel):
    """Request body for guarded Story completion."""

    expected_state: str
    idempotency_key: str
    scope: str | None = None
    scope_id: str | None = None
    parent_requirements: list[str] = []
```

In `api.py`, pass the list to the service:

```python
            parent_requirements=req.parent_requirements,
```

In `services/agent_workbench/story_phase.py`, change the runner `complete` method signature to include:

```python
        parent_requirements: list[str] | None = None,
```

and pass:

```python
                parent_requirements=parent_requirements,
```

to `complete_story_phase`.

In `services/agent_workbench/application.py`, change `AgentWorkbenchApplication.story_complete` to include:

```python
        parent_requirements: list[str] | None = None,
```

and route:

```python
            parent_requirements=parent_requirements,
```

to the existing `self._get_story_runner().complete` call.

- [ ] **Step 6: Implement CLI and command schema plumbing**

In `cli/main.py`, update the `_Application.story_complete` protocol signature:

```python
        parent_requirements: list[str] | None = None,
```

Change the `story complete` parser scope choices:

```python
        choices=["milestone", "selection"],
```

Update the scope help:

```python
            "Optionally complete only a planning scope. Supported values: "
            "milestone and selection."
```

Add the repeatable argument after `--scope-id`:

```python
    story_complete.add_argument(
        "--parent-requirement",
        action="append",
        default=[],
        help=(
            "Parent requirement to include when --scope selection is used. "
            "Repeat this flag for multiple saved requirements."
        ),
    )
```

Update `_story_complete` routing:

```python
        parent_requirements=args.parent_requirement,
```

In `services/agent_workbench/command_registry.py`, change Story complete optional inputs to:

```python
        input_optional=("scope", "scope_id", "parent_requirement"),
```

- [ ] **Step 7: Run plumbing tests and verify pass**

Run:

```bash
pytest tests/test_api_story_interview_flow.py::test_story_complete_phase_requires_and_passes_guard_body tests/test_agent_workbench_cli.py -k "story_complete or story_phase_commands" tests/test_agent_workbench_application.py -k "story_complete" tests/test_agent_workbench_command_schema.py::test_story_command_contracts -q
```

Expected after implementation: all selected tests pass.

- [ ] **Step 8: Commit public plumbing**

Run:

```bash
git add api.py cli/main.py services/agent_workbench/application.py services/agent_workbench/story_phase.py services/agent_workbench/command_registry.py tests/test_api_story_interview_flow.py tests/test_agent_workbench_cli.py tests/test_agent_workbench_application.py tests/test_agent_workbench_command_schema.py
git commit -m "feat(story): expose selected completion scope"
```

---

### Task 4: Workflow Next Selection Command

**Files:**
- Modify: `services/agent_workbench/application.py`
- Test: `tests/test_agent_workbench_application.py`

- [ ] **Step 1: Add failing workflow-next test**

Append this test after `test_application_workflow_next_routes_story_persistence_to_next_pending_story` in `tests/test_agent_workbench_application.py`:

```python
def test_workflow_next_routes_story_persistence_to_selection_complete_when_partially_saved() -> None:  # noqa: E501
    """Story persistence should advertise installed selection completion."""
    app = AgentWorkbenchApplication(
        workflow_reader=_WorkflowStateReader(
            {
                "fsm_state": "STORY_PERSISTENCE",
                "roadmap_releases": [
                    {
                        "items": [
                            "Technology and Model Research Spike",
                            "Python Project Scaffold and uv Management Setup",
                        ]
                    }
                ],
                "story_saved": {
                    "Technology and Model Research Spike": True,
                    "Python Project Scaffold and uv Management Setup": False,
                },
            }
        )
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    commands = [command["command"] for command in result["data"]["commands"]]
    assert commands == [
        "agileforge story pending --project-id 7",
        (
            "agileforge story generate --project-id 7 "
            "--parent-requirement <parent_requirement>"
        ),
        (
            "agileforge story complete --project-id 7 "
            "--expected-state STORY_PERSISTENCE "
            "--scope selection "
            "--parent-requirement \"Technology and Model Research Spike\" "
            "--idempotency-key <idempotency_key>"
        ),
    ]
```

- [ ] **Step 2: Run workflow-next test and verify failure**

Run:

```bash
pytest tests/test_agent_workbench_application.py::test_workflow_next_routes_story_persistence_to_selection_complete_when_partially_saved -q
```

Expected before implementation: the selection command is missing.

- [ ] **Step 3: Implement saved selection command discovery**

In `services/agent_workbench/application.py`, add this helper near `_covered_story_milestone_complete_commands`:

```python
def _story_requirement_is_covered(
    state_data: dict[str, Any],
    *,
    parent_requirement: str,
) -> bool:
    """Return whether a parent requirement is saved or merged."""
    saved = state_data.get("story_saved")
    saved_map = saved if isinstance(saved, dict) else {}
    return bool(saved_map.get(parent_requirement)) or _story_requirement_has_merge_resolution(
        state_data,
        parent_requirement=parent_requirement,
    )


def _covered_story_selection_complete_command(
    *,
    project_id: int,
    workflow: dict[str, Any],
) -> list[tuple[str, str]]:
    """Return a selection Story complete command when any requirement is covered."""
    state = _envelope_data(workflow).get("state")
    state_data = state if isinstance(state, dict) else {}
    covered_requirements = [
        requirement
        for requirement in _roadmap_requirements_from_state(state_data)
        if _story_requirement_is_covered(
            state_data,
            parent_requirement=requirement,
        )
    ]
    if not covered_requirements:
        return []
    parent_flags = " ".join(
        f'--parent-requirement "{requirement}"'
        for requirement in covered_requirements
    )
    return [
        (
            "agileforge story complete",
            (
                f"agileforge story complete --project-id {project_id} "
                "--expected-state STORY_PERSISTENCE "
                "--scope selection "
                f"{parent_flags} "
                "--idempotency-key <idempotency_key>"
            ),
        )
    ]
```

Update `_story_coverage_is_complete` to call `_story_requirement_is_covered`:

```python
    return all(
        _story_requirement_is_covered(
            state_data,
            parent_requirement=requirement,
        )
        for requirement in requirements
    )
```

In `_covered_story_milestone_complete_commands`, replace the inline saved/merged check with `_story_requirement_is_covered`.

In `_story_command_candidates`, when `fsm_state == "STORY_PERSISTENCE"` and coverage is incomplete, include selection commands after dependency commands:

```python
            selection_complete_commands = _covered_story_selection_complete_command(
                project_id=project_id,
                workflow=workflow,
            )
            return [
                pending_command,
                generate_command,
                *_story_dependency_command_candidates(
                    project_id=project_id,
                    expected_state="STORY_PERSISTENCE",
                ),
                *scoped_complete_commands,
                *selection_complete_commands,
            ]
```

This intentionally advertises the command only when at least one saved or merged requirement exists.

- [ ] **Step 4: Run workflow-next tests and verify pass**

Run:

```bash
pytest tests/test_agent_workbench_application.py -k "workflow_next_routes_story_persistence" -q
```

Expected after implementation: existing pending/full/milestone workflow-next behavior remains unchanged, and the new selection command is present for partial coverage.

- [ ] **Step 5: Commit workflow-next selection command**

Run:

```bash
git add services/agent_workbench/application.py tests/test_agent_workbench_application.py
git commit -m "feat(workflow): advertise selected story completion"
```

---

### Task 5: Browser UI Selection Completion

**Files:**
- Modify: `frontend/project.html`
- Modify: `frontend/project.js`
- Create: `tests/test_story_selection_scope_ui.mjs`

- [ ] **Step 1: Add failing static UI contract test**

Create `tests/test_story_selection_scope_ui.mjs`:

```javascript
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

const projectJsPath = path.resolve(import.meta.dirname, '../frontend/project.js');
const projectHtmlPath = path.resolve(import.meta.dirname, '../frontend/project.html');
const projectJs = fs.readFileSync(projectJsPath, 'utf8');
const projectHtml = fs.readFileSync(projectHtmlPath, 'utf8');

test('Story selection completion UI posts selection scope payload', () => {
    assert.match(projectHtml, /btn-complete-story-selection/);
    assert.match(projectHtml, /Plan Sprint from Selection/);
    assert.match(projectJs, /function toggleStorySelectionRequirement/);
    assert.match(projectJs, /async function completeSelectedStoryScope/);
    assert.match(projectJs, /scope: 'selection'/);
    assert.match(projectJs, /parent_requirements: selectedRequirements/);
    assert.match(projectJs, /expected_state: 'STORY_PERSISTENCE'/);
});
```

- [ ] **Step 2: Run UI contract test and verify failure**

Run:

```bash
node --test tests/test_story_selection_scope_ui.mjs
```

Expected before implementation: test fails because the selection button and functions do not exist.

- [ ] **Step 3: Add selection controls to Story header**

In `frontend/project.html`, replace the single complete button block in the Story header with:

```html
<div class="flex flex-wrap items-center justify-end gap-2">
    <button id="btn-complete-story-selection" onclick="completeSelectedStoryScope()"
        class="hidden inline-flex items-center gap-2 px-4 py-2.5 rounded-lg bg-emerald-600 text-white font-bold transition-all shadow-sm disabled:bg-primary/40 disabled:cursor-not-allowed"
        disabled>
        <span class="material-symbols-outlined text-sm">playlist_add_check</span>
        Plan Sprint from Selection
    </button>
    <button id="btn-complete-story-phase" onclick="completeStoryPhase()"
        class="inline-flex items-center gap-2 px-5 py-2.5 rounded-lg bg-primary/40 text-white font-bold cursor-not-allowed transition-all shadow-sm"
        disabled>
        <span class="material-symbols-outlined text-sm">flag</span>
        Complete Story Phase
    </button>
</div>
```

- [ ] **Step 4: Add JS selection state and status helpers**

Near the existing Story globals in `frontend/project.js`, add:

```javascript
let selectedStoryScopeRequirements = new Set();
```

Add these helpers near the Story section helpers:

```javascript
function isSavedOrMergedStoryStatus(status) {
    return status === 'Saved' || status === 'Merged';
}

function selectedStoryScopeNames() {
    return storyRequirements
        .filter(item => selectedStoryScopeRequirements.has(item.requirement))
        .map(item => item.requirement);
}

function toggleStorySelectionRequirement(requirement) {
    const item = storyRequirements.find(entry => entry.requirement === requirement);
    if (!item || !isSavedOrMergedStoryStatus(item.status)) {
        return;
    }
    if (selectedStoryScopeRequirements.has(requirement)) {
        selectedStoryScopeRequirements.delete(requirement);
    } else {
        selectedStoryScopeRequirements.add(requirement);
    }
    renderStoryRequirements();
    updateCompleteStoryPhaseButton();
}
```

When `loadStoryRequirements` refreshes `storyRequirements`, prune stale selections:

```javascript
            const validSelection = new Set(
                storyRequirements
                    .filter(item => isSavedOrMergedStoryStatus(item.status))
                    .map(item => item.requirement)
            );
            selectedStoryScopeRequirements = new Set(
                Array.from(selectedStoryScopeRequirements).filter(requirement => validSelection.has(requirement))
            );
```

- [ ] **Step 5: Render selection checkboxes for saved or merged requirements**

In `renderStoryRequirements`, add this button inside each requirement row before or next to the status label:

```javascript
const canSelectForSprint = isSavedOrMergedStoryStatus(req.status);
const isSelectedForSprint = selectedStoryScopeRequirements.has(req.requirement);
const selectionControl = canSelectForSprint
    ? `<button type="button" onclick="event.stopPropagation(); toggleStorySelectionRequirement('${escapeAttribute(req.requirement)}')" class="w-6 h-6 rounded border border-slate-400 dark:border-slate-600 inline-flex items-center justify-center ${isSelectedForSprint ? 'bg-emerald-600 text-white border-emerald-600' : 'text-transparent'}" title="Select for next Sprint scope">
            <span class="material-symbols-outlined text-[16px]">check</span>
       </button>`
    : '<span class="w-6 h-6 inline-block"></span>';
```

Then include `${selectionControl}` in the row markup before the requirement title.

If `escapeAttribute` does not exist, add it near the other HTML escaping helpers:

```javascript
function escapeAttribute(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}
```

- [ ] **Step 6: Post guarded selection completion payload**

Replace `completeStoryPhase` request body with a guarded full-completion payload:

```javascript
        const response = await fetch(`/api/projects/${selectedProjectId}/story/complete_phase`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                expected_state: 'STORY_PERSISTENCE',
                idempotency_key: `complete-story-full-${Date.now()}`,
            }),
        });
```

Add this function after `completeStoryPhase`:

```javascript
async function completeSelectedStoryScope() {
    if (!selectedProjectId) return;
    const selectedRequirements = selectedStoryScopeNames();
    if (selectedRequirements.length === 0) {
        alert('Select at least one saved requirement for Sprint planning.');
        return;
    }

    const btn = document.getElementById('btn-complete-story-selection');
    const original = btn?.innerHTML;
    if (btn) {
        btn.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">playlist_add_check</span> Processing';
        btn.disabled = true;
    }

    try {
        const response = await fetch(`/api/projects/${selectedProjectId}/story/complete_phase`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                expected_state: 'STORY_PERSISTENCE',
                idempotency_key: `complete-story-selection-${Date.now()}`,
                scope: 'selection',
                parent_requirements: selectedRequirements,
            }),
        });
        if (response.status >= 400) {
            const body = await response.json();
            throw new Error(body.detail || 'Failed to complete selected Story scope.');
        }

        await fetchProjectFSMState(selectedProjectId);
        await loadSprintCandidates();
    } catch (error) {
        alert(error.message || 'Failed to complete selected Story scope.');
    } finally {
        if (btn) {
            btn.innerHTML = original || '<span class="material-symbols-outlined text-sm">playlist_add_check</span> Plan Sprint from Selection';
            btn.disabled = false;
        }
        updateCompleteStoryPhaseButton();
    }
}
```

Expose it near the window exports:

```javascript
window.toggleStorySelectionRequirement = toggleStorySelectionRequirement;
window.completeSelectedStoryScope = completeSelectedStoryScope;
```

Update `updateCompleteStoryPhaseButton` so the selection button is visible and enabled only when at least one saved/merged requirement is selected:

```javascript
    const selectionBtn = document.getElementById('btn-complete-story-selection');
    const selectedCount = selectedStoryScopeNames().length;
    if (selectionBtn) {
        selectionBtn.classList.toggle('hidden', !anySaved);
        selectionBtn.disabled = selectedCount === 0;
        selectionBtn.title = selectedCount > 0
            ? `${selectedCount} saved requirement${selectedCount === 1 ? '' : 's'} selected for Sprint planning.`
            : 'Select saved requirements to plan the next Sprint.';
    }
```

- [ ] **Step 7: Run UI tests and syntax check**

Run:

```bash
node --test tests/test_story_selection_scope_ui.mjs
node --check frontend/project.js
```

Expected after implementation: the static contract test passes and `frontend/project.js` parses.

- [ ] **Step 8: Commit browser UI selection completion**

Run:

```bash
git add frontend/project.html frontend/project.js tests/test_story_selection_scope_ui.mjs
git commit -m "feat(ui): plan sprint from saved story selection"
```

---

### Task 6: End-To-End Verification And Merge Readiness

**Files:**
- No source changes expected.
- Uses the commits from Tasks 1-5.

- [ ] **Step 1: Run focused Python verification**

Run:

```bash
pytest tests/test_story_phase_service.py -k "complete_story_phase" -q
pytest tests/test_agent_workbench_read_projection.py -k "sprint_candidates" -q
pytest tests/test_sprint_planner_tools.py -k "load_sprint_candidates_blocks_scope_external_dependency or readiness" -q
pytest tests/test_api_story_interview_flow.py::test_story_complete_phase_requires_and_passes_guard_body -q
pytest tests/test_agent_workbench_cli.py -k "story_complete or story_phase_commands" -q
pytest tests/test_agent_workbench_application.py -k "workflow_next_routes_story_persistence or story_complete" -q
pytest tests/test_agent_workbench_command_schema.py::test_story_command_contracts -q
```

Expected: every command exits with status 0.

- [ ] **Step 2: Run focused frontend verification**

Run:

```bash
node --test tests/test_story_selection_scope_ui.mjs
node --test tests/test_story_history_display.mjs
node --test tests/test_story_resolution_projection.mjs
node --check frontend/project.js
```

Expected: every command exits with status 0.

- [ ] **Step 3: Run formatting and diff hygiene checks**

Run:

```bash
git diff --check
git status --short
```

Expected: `git diff --check` exits with status 0. `git status --short` shows no uncommitted source changes after each task commit.

- [ ] **Step 4: Optional live ASA smoke test**

Run from `/Users/aaat/projects/asa-deep-process-control-experiments` only after the implementation commits are installed in the local `agileforge` shim:

```bash
agileforge story complete \
  --project-id 3 \
  --expected-state STORY_PERSISTENCE \
  --scope selection \
  --parent-requirement "Technology and Model Research Spike" \
  --parent-requirement "Python Project Scaffold and uv Management Setup" \
  --idempotency-key story-selection-smoke-$(date +%s)
agileforge sprint candidates --project-id 3
```

Expected: Story completion returns `fsm_state=SPRINT_SETUP` and `story_completion_scope.scope=selection`. Sprint candidates show only stories from the selected parent requirements, or readiness blocks with `SPRINT_SCOPE_EXTERNAL_DEPENDENCY` if selected stories reference excluded dependencies.

- [ ] **Step 5: Final branch review**

Run:

```bash
git log --oneline --decorate -6
git diff "$(git merge-base master HEAD)"..HEAD --stat
```

Expected: the branch contains the spec, plan, and the five feature commits. The stat is limited to Story completion, Sprint candidate readiness, public command/API surfaces, frontend Story UI, and tests.

---

## Acceptance Checklist

- [ ] `scope=selection` requires at least one parent requirement.
- [ ] Unknown selected parent requirements are rejected before state changes.
- [ ] Unsaved and unmerged selected parent requirements are rejected before state changes.
- [ ] Duplicate and differently cased selected requirements collapse to one roadmap-order requirement.
- [ ] Selection scope stores deterministic `scope_id` as `selection:<canonical_hash>`.
- [ ] Selection scope stores `completed_at` when entering `SPRINT_SETUP`.
- [ ] Full completion still clears existing `story_completion_scope`.
- [ ] Milestone completion still stores milestone `story_completion_scope`.
- [ ] Sprint candidates filter to selected parent requirements.
- [ ] Sprint candidates block readiness with `SPRINT_SCOPE_EXTERNAL_DEPENDENCY` when selected candidates depend on excluded story IDs.
- [ ] Sprint generation refuses blocked readiness through the existing `generate_sprint_plan` guard.
- [ ] `workflow next` advertises an installed selection completion command when at least one saved or merged requirement exists.
- [ ] CLI, API, workbench schema, and browser UI expose the same selection capability.
- [ ] ASA can plan Sprint from the two saved requirements without generating every remaining roadmap requirement first.
