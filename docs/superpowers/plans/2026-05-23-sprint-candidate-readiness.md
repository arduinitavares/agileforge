# Sprint Candidate Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make refined Story persistence produce Sprint-ready candidates with deterministic story points and rank, and provide a safe repair path for projects already affected by missing sizing/ranking.

**Architecture:** Fix the data at the Story persistence boundary, because Sprint should consume already-sized and ordered backlog rows instead of guessing. Map `UserStoryItem.estimated_effort` to `UserStory.story_points`, derive rank from Roadmap parent order plus child slot, and surface Sprint readiness diagnostics when legacy rows are still incomplete. Add a guarded repair command that backfills only planning metadata for existing refined stories before Sprint work starts.

**Tech Stack:** Python 3.13, Pydantic, SQLModel, existing AgileForge CLI envelopes, existing Story/Workflow state, existing sprint candidate read projection.

---

## File Structure

- Modify `orchestrator_agent/agent_tools/user_story_writer_tool/tools.py`: persist story sizing and rank during refined story save.
- Modify `services/phases/story_service.py`: compute parent Roadmap order and pass it to `SaveStoriesInput`; add a pure service for guarded readiness repair.
- Modify `services/agent_workbench/story_phase.py`: expose Story readiness repair runner and DB safety checks.
- Modify `services/agent_workbench/application.py`: expose `story_repair_readiness`.
- Modify `services/agent_workbench/command_registry.py`: register the repair command.
- Modify `cli/main.py`: add `agileforge story repair-readiness`.
- Modify `services/orchestrator_query_service.py`: add Sprint candidate readiness summary to raw candidate query.
- Modify `services/agent_workbench/read_projection.py`: include readiness in `agileforge sprint candidates`.
- Modify `services/phases/sprint_service.py`: block Sprint generation when candidates are not planning-ready.
- Modify `docs/agent-cli-manual.md`: document story sizing/rank and the repair command.
- Tests:
  - `tests/test_save_stories_tool.py`
  - `tests/test_story_phase_service.py`
  - `tests/test_agent_workbench_story_phase.py`
  - `tests/test_agent_workbench_application.py`
  - `tests/test_agent_workbench_cli.py`
  - `tests/test_agent_workbench_command_schema.py`
  - `tests/test_sprint_planner_tools.py`
  - `tests/test_agent_workbench_read_projection.py`
  - `tests/test_api_sprint_flow.py`

## Behavior Contract

- Story save maps `estimated_effort` into `story_points`:
  - `XS -> 1`
  - `S -> 2`
  - `M -> 3`
  - `L -> 5`
  - `XL -> 8`
- Story save persists rank for every refined child story.
- Rank is deterministic:
  - If the Story phase has Roadmap order, rank is `parent_order * 100 + child_slot`.
  - If Roadmap order is unavailable but existing active rows have rank, use the first existing rank as parent order.
  - If neither exists, fall back to `child_slot`.
- Sprint candidates must expose a readiness object:
  - `status: "ready" | "blocked"`
  - `unsized_count`
  - `default_priority_count`
  - `blocking_codes`
  - `blocking_story_ids`
- `agileforge sprint candidates` remains a read-only diagnostic command and returns items even when readiness is blocked.
- Sprint generation must fail closed when readiness is blocked.
- Existing projects can run a supported repair command before Sprint work starts:

```sh
agileforge story repair-readiness \
  --project-id 2 \
  --expected-state SPRINT_SETUP \
  --idempotency-key repair-story-readiness-2-001
```

- Repair command must only backfill `story_points` and `rank`; it must not rewrite title, statement, acceptance criteria, status, story origin, or validation evidence.
- Repair command must block if any active refined story is already linked to a planned or active Sprint.

## Task 1: Persist Story Points and Rank on Story Save

**Files:**
- Modify: `orchestrator_agent/agent_tools/user_story_writer_tool/tools.py`
- Test: `tests/test_save_stories_tool.py`

- [ ] **Step 1: Write failing test for effort-to-points mapping**

Add this test to `TestSaveStoriesTool` in `tests/test_save_stories_tool.py`:

```python
def test_valid_stories_persist_story_points_from_estimated_effort(
    self,
    session: Session,
) -> None:
    _seed_product(session)

    payload = SaveStoriesInput(
        product_id=1,
        parent_requirement="Attestation Gate",
        idempotency_key="test-persist-story-points",
        parent_rank=2,
        stories=[
            {
                **_valid_story(),
                "estimated_effort": "XS",
            },
            {
                **_alternate_valid_story(),
                "estimated_effort": "XL",
            },
        ],
    )

    result = save_stories_tool(input_data=payload, tool_context=None)

    assert result["success"], result.get("error")
    rows = session.exec(
        select(UserStory)
        .where(UserStory.product_id == 1)
        .order_by(UserStory.refinement_slot)
    ).all()
    assert [row.story_points for row in rows] == [1, 8]
```

- [ ] **Step 2: Write failing test for deterministic rank**

Add:

```python
def test_valid_stories_persist_rank_from_parent_rank_and_slot(
    self,
    session: Session,
) -> None:
    _seed_product(session)

    payload = SaveStoriesInput(
        product_id=1,
        parent_requirement="Attestation Gate",
        idempotency_key="test-persist-refined-rank",
        parent_rank=3,
        stories=[_valid_story(), _alternate_valid_story()],
    )

    result = save_stories_tool(input_data=payload, tool_context=None)

    assert result["success"], result.get("error")
    rows = session.exec(
        select(UserStory)
        .where(UserStory.product_id == 1)
        .order_by(UserStory.refinement_slot)
    ).all()
    assert [row.rank for row in rows] == ["301", "302"]
```

- [ ] **Step 3: Write failing update-path test**

Add:

```python
def test_refinement_update_refreshes_story_points_and_rank(
    self,
    session: Session,
) -> None:
    _seed_product(session)
    seed = UserStory(
        product_id=1,
        title="Attestation Gate",
        story_description="Backlog seed",
        acceptance_criteria=None,
        source_requirement=normalize_requirement_key("Attestation Gate"),
        refinement_slot=1,
        story_origin="backlog_seed",
        is_refined=False,
        is_superseded=False,
        story_points=None,
        rank=None,
    )
    session.add(seed)
    session.commit()
    session.refresh(seed)

    payload = SaveStoriesInput(
        product_id=1,
        parent_requirement="Attestation Gate",
        idempotency_key="test-update-points-rank",
        parent_rank=4,
        stories=[_valid_story()],
    )

    result = save_stories_tool(input_data=payload, tool_context=None)

    assert result["success"], result.get("error")
    session.expire_all()
    refreshed = session.get(UserStory, seed.story_id)
    assert refreshed is not None
    assert refreshed.story_points == 3
    assert refreshed.rank == "401"
```

- [ ] **Step 4: Run failing tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_save_stories_tool.py::TestSaveStoriesTool::test_valid_stories_persist_story_points_from_estimated_effort \
  tests/test_save_stories_tool.py::TestSaveStoriesTool::test_valid_stories_persist_rank_from_parent_rank_and_slot \
  tests/test_save_stories_tool.py::TestSaveStoriesTool::test_refinement_update_refreshes_story_points_and_rank \
  -q
```

Expected: fail because `SaveStoriesInput` does not expose `parent_rank`, and save logic does not assign `story_points` or `rank`.

- [ ] **Step 5: Add persistence helpers**

In `orchestrator_agent/agent_tools/user_story_writer_tool/tools.py`, add near `_format_acceptance_criteria`:

```python
_EFFORT_TO_STORY_POINTS: dict[str, int] = {
    "XS": 1,
    "S": 2,
    "M": 3,
    "L": 5,
    "XL": 8,
}


def _story_points_from_effort(estimated_effort: str) -> int:
    return _EFFORT_TO_STORY_POINTS[estimated_effort]


def _rank_to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parent_rank_from_existing(existing_active: list[UserStory]) -> int | None:
    ranks = [
        parsed
        for parsed in (_rank_to_int(story.rank) for story in existing_active)
        if parsed is not None
    ]
    if not ranks:
        return None
    return max(1, min(ranks) // 100 if min(ranks) > 100 else min(ranks))


def _refined_story_rank(
    *,
    parent_rank: int | None,
    existing_active: list[UserStory],
    slot: int,
) -> str:
    base = parent_rank or _parent_rank_from_existing(existing_active)
    if base is None:
        return str(slot)
    return str((base * 100) + slot)
```

- [ ] **Step 6: Extend `SaveStoriesInput`**

Add an optional field:

```python
parent_rank: Annotated[
    int | None,
    Field(
        default=None,
        ge=1,
        description=(
            "1-based Roadmap parent order used to derive deterministic child story rank."
        ),
    ),
]
```

- [ ] **Step 7: Persist points/rank on create and update**

Change `_upsert_refined_story` signature to accept `rank: str`:

```python
def _upsert_refined_story(
    session: Session,
    *,
    linkage: tuple[int, str],
    slot: int,
    rank: str,
    item: UserStoryItem,
    existing: UserStory | None,
) -> tuple[int, str]:
```

In the update branch, set:

```python
existing.story_points = _story_points_from_effort(item.estimated_effort)
existing.rank = rank
```

In the create branch, set:

```python
story_points=_story_points_from_effort(item.estimated_effort),
rank=rank,
```

In `_persist_validated_stories`, compute rank before `_upsert_refined_story`:

```python
rank = _refined_story_rank(
    parent_rank=input_data.parent_rank,
    existing_active=existing_active,
    slot=idx,
)
```

Pass `rank=rank`.

- [ ] **Step 8: Verify save tests**

Run:

```bash
uv run --frozen pytest tests/test_save_stories_tool.py -q
```

Expected: pass.

## Task 2: Pass Roadmap Parent Rank From Story Phase Save

**Files:**
- Modify: `services/phases/story_service.py`
- Test: `tests/test_story_phase_service.py`

- [ ] **Step 1: Write parent-rank helper tests**

Add:

```python
def test_story_parent_rank_uses_roadmap_order() -> None:
    state = {
        "roadmap_releases": [
            {"items": ["Requirement A", "Requirement B"]},
            {"items": ["Requirement C"]},
        ]
    }

    assert story_parent_rank(state, "Requirement A") == 1
    assert story_parent_rank(state, "Requirement B") == 2
    assert story_parent_rank(state, "Requirement C") == 3
```

Add normalized matching coverage:

```python
def test_story_parent_rank_matches_normalized_requirement() -> None:
    state = {"roadmap_releases": [{"items": ["Live Pre-Lock Recommendation"]}]}

    assert story_parent_rank(state, " live   pre-lock recommendation ") == 1
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_story_phase_service.py::test_story_parent_rank_uses_roadmap_order \
  tests/test_story_phase_service.py::test_story_parent_rank_matches_normalized_requirement \
  -q
```

Expected: fail because `story_parent_rank` does not exist.

- [ ] **Step 3: Implement `story_parent_rank`**

In `services/phases/story_service.py`, add:

```python
def story_parent_rank(state: dict[str, Any], parent_requirement: str) -> int | None:
    """Return 1-based Roadmap order for a parent requirement."""
    parent_key = normalize_requirement_key(parent_requirement)
    roadmap_releases = state.get("roadmap_releases")
    if not isinstance(roadmap_releases, list):
        return None

    rank = 0
    for release in roadmap_releases:
        if not isinstance(release, dict):
            continue
        items = release.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, str) or not item.strip():
                continue
            rank += 1
            if normalize_requirement_key(item) == parent_key:
                return rank
    return None
```

- [ ] **Step 4: Pass rank into `SaveStoriesInput`**

In `save_story_draft`, change the `SaveStoriesInput` call:

```python
SaveStoriesInput(
    product_id=project_id,
    parent_requirement=normalized_parent_requirement,
    idempotency_key=idempotency_key,
    parent_rank=story_parent_rank(state, normalized_parent_requirement),
    stories=stories,
)
```

- [ ] **Step 5: Add save service assertion**

In the existing Story save service test that captures `SaveStoriesInput`, assert:

```python
assert captured_input.parent_rank == 1
```

If no such test exists, add a focused test around `save_story_draft` with `roadmap_releases=[{"items": [parent_requirement]}]`.

- [ ] **Step 6: Verify**

Run:

```bash
uv run --frozen pytest tests/test_story_phase_service.py -q
```

Expected: pass.

## Task 3: Add Sprint Candidate Readiness Diagnostics

**Files:**
- Modify: `services/orchestrator_query_service.py`
- Modify: `services/agent_workbench/read_projection.py`
- Test: `tests/test_sprint_planner_tools.py`
- Test: `tests/test_agent_workbench_read_projection.py`

- [ ] **Step 1: Write failing raw query readiness test**

In `tests/test_sprint_planner_tools.py`, add:

```python
def test_fetch_sprint_candidates_reports_blocked_readiness_for_unsized_rows(
    session: Session,
) -> None:
    product = Product(name="Test Product", vision="Vision", description="Desc")
    session.add(product)
    session.commit()
    session.refresh(product)

    session.add_all(
        [
            UserStory(
                product_id=product.product_id,
                title="Unsized story",
                story_description="As a user, I want a thing, so that I get value.",
                acceptance_criteria="- Verify behavior.",
                story_origin="refined",
                is_refined=True,
                is_superseded=False,
                story_points=None,
                rank=None,
            ),
            UserStory(
                product_id=product.product_id,
                title="Sized story",
                story_description="As a user, I want another thing, so that I get value.",
                acceptance_criteria="- Verify behavior.",
                story_origin="refined",
                is_refined=True,
                is_superseded=False,
                story_points=3,
                rank="101",
            ),
        ]
    )
    session.commit()

    result = fetch_sprint_candidates(product.product_id)

    assert result["success"] is True
    assert result["readiness"] == {
        "status": "blocked",
        "unsized_count": 1,
        "default_priority_count": 1,
        "blocking_codes": ["SPRINT_CANDIDATES_UNSIZED", "SPRINT_CANDIDATES_DEFAULT_PRIORITY"],
        "blocking_story_ids": [1],
    }
```

Adjust the `blocking_story_ids` assertion to compare the actual unsized row ID if the test fixture uses generated IDs:

```python
assert result["readiness"]["blocking_story_ids"] == [unsized.story_id]
```

- [ ] **Step 2: Implement readiness builder**

In `services/orchestrator_query_service.py`, add:

```python
def _sprint_candidate_readiness(
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    unsized_ids = [
        int(candidate["story_id"])
        for candidate in candidates
        if candidate.get("story_id") is not None and candidate.get("story_points") is None
    ]
    default_priority_ids = [
        int(candidate["story_id"])
        for candidate in candidates
        if candidate.get("story_id") is not None and candidate.get("priority") == 999
    ]
    blocking_codes: list[str] = []
    if unsized_ids:
        blocking_codes.append("SPRINT_CANDIDATES_UNSIZED")
    if default_priority_ids:
        blocking_codes.append("SPRINT_CANDIDATES_DEFAULT_PRIORITY")
    return {
        "status": "blocked" if blocking_codes else "ready",
        "unsized_count": len(unsized_ids),
        "default_priority_count": len(default_priority_ids),
        "blocking_codes": blocking_codes,
        "blocking_story_ids": sorted(set(unsized_ids + default_priority_ids)),
    }
```

Add `readiness` to the returned payload:

```python
"readiness": _sprint_candidate_readiness(candidate_list),
```

- [ ] **Step 3: Include readiness in read projection**

In `services/agent_workbench/read_projection.py`, add:

```python
readiness = raw.get("readiness") or {
    "status": "ready",
    "unsized_count": 0,
    "default_priority_count": 0,
    "blocking_codes": [],
    "blocking_story_ids": [],
}
```

Include it in `source_payload` and `data`:

```python
"readiness": readiness,
```

- [ ] **Step 4: Add read projection test**

In `tests/test_agent_workbench_read_projection.py`, add a test that seeds one refined candidate with `story_points=None` and `rank=None`, calls `projection.sprint_candidates(project_id=...)`, and asserts:

```python
assert payload["ok"] is True
assert payload["data"]["readiness"]["status"] == "blocked"
assert payload["data"]["readiness"]["blocking_codes"] == [
    "SPRINT_CANDIDATES_UNSIZED",
    "SPRINT_CANDIDATES_DEFAULT_PRIORITY",
]
```

- [ ] **Step 5: Verify**

Run:

```bash
uv run --frozen pytest \
  tests/test_sprint_planner_tools.py::test_fetch_sprint_candidates_reports_blocked_readiness_for_unsized_rows \
  tests/test_agent_workbench_read_projection.py::test_sprint_candidates_reports_readiness_blockers \
  -q
```

Expected: pass.

## Task 4: Block Sprint Generation on Unready Candidates

**Files:**
- Modify: `services/phases/sprint_service.py`
- Test: `tests/test_api_sprint_flow.py`

- [ ] **Step 1: Locate candidate hydration in Sprint generation**

Inspect `generate_sprint_draft` in `services/phases/sprint_service.py` and identify where Sprint runtime receives candidates. Do not change state transitions yet.

- [ ] **Step 2: Add failing test**

In `tests/test_api_sprint_flow.py`, add a test using the existing Sprint generate setup style:

```python
def test_sprint_generate_blocks_when_candidates_are_not_ready(client, monkeypatch):  # noqa: ANN001, ANN201
    project_id = 7

    monkeypatch.setattr(
        "services.phases.sprint_service.fetch_sprint_candidates_from_session",
        lambda _session, _project_id: {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 1,
                    "story_title": "Unsized",
                    "priority": 999,
                    "story_points": None,
                }
            ],
            "readiness": {
                "status": "blocked",
                "unsized_count": 1,
                "default_priority_count": 1,
                "blocking_codes": [
                    "SPRINT_CANDIDATES_UNSIZED",
                    "SPRINT_CANDIDATES_DEFAULT_PRIORITY",
                ],
                "blocking_story_ids": [1],
            },
        },
    )

    response = client.post(
        f"/api/projects/{project_id}/sprint/generate",
        json={"idempotency_key": "sprint-generate-unready"},
    )

    assert response.status_code == 409
    payload = response.json()
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "MUTATION_FAILED"
    assert "SPRINT_CANDIDATES_UNSIZED" in payload["errors"][0]["message"]
```

Adjust the endpoint payload to match the existing Sprint generate API tests.

- [ ] **Step 3: Implement fail-closed guard**

In `services/phases/sprint_service.py`, after loading candidates and before runtime invocation:

```python
readiness = candidates_payload.get("readiness")
if isinstance(readiness, dict) and readiness.get("status") == "blocked":
    codes = ", ".join(str(code) for code in readiness.get("blocking_codes", []))
    raise SprintPhaseError(
        f"Sprint candidates are not planning-ready: {codes}",
        status_code=409,
    )
```

Use the existing Sprint phase error type and error mapping in that file.

- [ ] **Step 4: Verify Sprint tests**

Run:

```bash
uv run --frozen pytest tests/test_api_sprint_flow.py -q
```

Expected: pass.

## Task 5: Add Guarded Story Readiness Repair Command

**Files:**
- Modify: `services/phases/story_service.py`
- Modify: `services/agent_workbench/story_phase.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `cli/main.py`
- Test: `tests/test_story_phase_service.py`
- Test: `tests/test_agent_workbench_story_phase.py`
- Test: `tests/test_agent_workbench_application.py`
- Test: `tests/test_agent_workbench_cli.py`
- Test: `tests/test_agent_workbench_command_schema.py`

- [ ] **Step 1: Add service test for metadata-only repair**

In `tests/test_story_phase_service.py`, add:

```python
@pytest.mark.asyncio
async def test_repair_story_readiness_backfills_rank_and_points_from_saved_outputs() -> None:
    parent_requirement = "Requirement A"
    state: dict[str, Any] = {
        "fsm_state": "SPRINT_SETUP",
        "roadmap_releases": [{"items": [parent_requirement]}],
        "story_saved": {parent_requirement: True},
        "story_outputs": {
            parent_requirement: {
                "parent_requirement": parent_requirement,
                "is_complete": True,
                "user_stories": [
                    {
                        "story_title": "Story A",
                        "statement": "As a user, I want alpha, so that I get value.",
                        "acceptance_criteria": ["Verify alpha."],
                        "invest_score": "High",
                        "estimated_effort": "L",
                    }
                ],
            }
        },
    }
    repaired: list[dict[str, Any]] = []

    payload = await repair_story_readiness(
        project_id=2,
        expected_state="SPRINT_SETUP",
        idempotency_key="repair-story-readiness-2",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: state.update(updated),
        repair_rows=lambda request: repaired.append(request) or {
            "repaired_count": 1,
            "story_ids": [66],
        },
        assert_repair_safe=lambda _project_id: None,
    )

    assert payload["fsm_state"] == "SPRINT_SETUP"
    assert payload["repair_result"]["repaired_count"] == 1
    assert repaired[0]["items"] == [
        {
            "parent_requirement": parent_requirement,
            "parent_rank": 1,
            "slot": 1,
            "story_points": 5,
            "rank": "101",
        }
    ]
```

- [ ] **Step 2: Add service test for unsafe repair**

Add:

```python
@pytest.mark.asyncio
async def test_repair_story_readiness_blocks_after_sprint_work_exists() -> None:
    state = {"fsm_state": "SPRINT_SETUP"}

    with pytest.raises(StoryPhaseError) as excinfo:
        await repair_story_readiness(
            project_id=2,
            expected_state="SPRINT_SETUP",
            idempotency_key="repair-story-readiness-2",
            load_state=lambda: _async_value(state),
            save_state=lambda _updated: None,
            repair_rows=lambda _request: {},
            assert_repair_safe=lambda _project_id: (_ for _ in ()).throw(
                StoryPhaseError("Story readiness repair is unsafe after Sprint work exists.", status_code=409)
            ),
        )

    assert excinfo.value.status_code == 409
```

- [ ] **Step 3: Implement service**

In `services/phases/story_service.py`, add:

```python
async def repair_story_readiness(
    *,
    project_id: int,
    expected_state: str | None,
    idempotency_key: str | None,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    repair_rows: Callable[[dict[str, Any]], dict[str, Any]],
    assert_repair_safe: Callable[[int], None],
) -> dict[str, Any]:
    if expected_state != OrchestratorState.SPRINT_SETUP.value:
        raise StoryPhaseError(
            "story repair-readiness requires --expected-state SPRINT_SETUP",
            status_code=400,
        )
    if idempotency_key is None or not idempotency_key.strip():
        raise StoryPhaseError("story repair-readiness requires --idempotency-key", status_code=400)

    state = await load_state()
    current_state = _normalize_fsm_state(state.get("fsm_state"))
    if current_state != OrchestratorState.SPRINT_SETUP.value:
        raise StoryPhaseError(
            "Story readiness repair can run only from SPRINT_SETUP.",
            status_code=409,
        )

    registry = state.get("story_readiness_repair_idempotency")
    if isinstance(registry, dict):
        existing = registry.get(idempotency_key.strip())
        if isinstance(existing, dict):
            return dict(existing)

    assert_repair_safe(project_id)
    items = _story_readiness_repair_items(state)
    repair_result = repair_rows({"project_id": project_id, "items": items})
    payload = {
        "project_id": project_id,
        "fsm_state": OrchestratorState.SPRINT_SETUP.value,
        "idempotency_key": idempotency_key.strip(),
        "repair_result": repair_result,
    }
    if not isinstance(registry, dict):
        registry = {}
        state["story_readiness_repair_idempotency"] = registry
    registry[idempotency_key.strip()] = payload
    save_state(state)
    return payload
```

Add `_story_readiness_repair_items(state)` that reads saved `story_outputs`, validates each `user_story` has `estimated_effort`, computes `story_points` and `rank`, and returns metadata-only repair rows. Reuse the same effort mapping constants or duplicate a small private mapping in `story_service.py` to avoid importing low-level tool internals.

- [ ] **Step 4: Implement runner DB repair**

In `services/agent_workbench/story_phase.py`, add `repair_readiness(...)`.

The DB repair callback should:

```python
def _repair_story_readiness_rows(request: dict[str, Any]) -> dict[str, Any]:
    project_id = int(request["project_id"])
    items = list(request.get("items") or [])
    repaired_ids: list[int] = []
    with Session(get_engine()) as session:
        for item in items:
            story = session.exec(
                select(UserStory).where(
                    UserStory.product_id == project_id,
                    UserStory.source_requirement == normalize_requirement_key(item["parent_requirement"]),
                    UserStory.refinement_slot == int(item["slot"]),
                    UserStory.is_refined == True,  # noqa: E712
                    UserStory.is_superseded == False,  # noqa: E712
                )
            ).first()
            if story is None:
                continue
            story.story_points = int(item["story_points"])
            story.rank = str(item["rank"])
            session.add(story)
            if story.story_id is not None:
                repaired_ids.append(story.story_id)
        session.commit()
    return {"repaired_count": len(repaired_ids), "story_ids": repaired_ids}
```

The safety callback must block if any active refined story for the project has a planned/active `SprintStory` link.

- [ ] **Step 5: Add CLI/application/registry**

Add CLI:

```sh
agileforge story repair-readiness \
  --project-id 2 \
  --expected-state SPRINT_SETUP \
  --idempotency-key repair-story-readiness-2-001
```

Register command metadata:

```python
CommandMetadata(
    name="agileforge story repair-readiness",
    mutates=True,
    phase="phase_2d",
    requires_idempotency_key=True,
    input_required=("project_id", "expected_state", "idempotency_key"),
    errors=(
        ErrorCode.PROJECT_NOT_FOUND.value,
        ErrorCode.INVALID_COMMAND.value,
        ErrorCode.WORKFLOW_SESSION_FAILED.value,
        ErrorCode.MUTATION_FAILED.value,
    ),
)
```

- [ ] **Step 6: Add runner/application/CLI tests**

Add tests mirroring `story reopen` patterns:

```python
def test_story_repair_readiness_cli_routes_guard_fields(capsys: pytest.CaptureFixture[str]) -> None:
    app = _FakeApplication()

    exit_code = main(
        [
            "story",
            "repair-readiness",
            "--project-id",
            "7",
            "--expected-state",
            "SPRINT_SETUP",
            "--idempotency-key",
            "repair-story-readiness-7",
        ],
        application=app,
    )

    assert exit_code == 0
    assert app.calls[-1] == (
        "story_repair_readiness",
        {
            "project_id": 7,
            "expected_state": "SPRINT_SETUP",
            "idempotency_key": "repair-story-readiness-7",
        },
    )
```

- [ ] **Step 7: Verify Task 5**

Run:

```bash
uv run --frozen pytest \
  tests/test_story_phase_service.py \
  tests/test_agent_workbench_story_phase.py \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_command_schema.py \
  -q
```

Expected: pass.

## Task 6: Documentation and caRtola Smoke Test

**Files:**
- Modify: `docs/agent-cli-manual.md`
- Live project: `/Users/aaat/projects/caRtola`, project id `2`

- [ ] **Step 1: Document sizing/rank**

Add to the Story section:

```markdown
Story save persists Sprint planning metadata:

| estimated_effort | story_points |
| --- | ---: |
| XS | 1 |
| S | 2 |
| M | 3 |
| L | 5 |
| XL | 8 |

Refined child story rank is derived from Roadmap parent order plus child slot.
```

- [ ] **Step 2: Document repair command**

Add:

```markdown
### Repairing Story Readiness Before Sprint

Use this when refined stories were saved before AgileForge persisted `story_points`
and rank.

```sh
agileforge story repair-readiness \
  --project-id 2 \
  --expected-state SPRINT_SETUP \
  --idempotency-key repair-story-readiness-2-001
```

This command only backfills `story_points` and `rank`. It does not rewrite story
title, description, acceptance criteria, or validation evidence.
```

- [ ] **Step 3: Verify caRtola before repair**

Run:

```bash
cd /Users/aaat/projects/caRtola
agileforge sprint candidates --project-id 2 > /tmp/cartola-sprint-candidates-before-readiness-repair.json
uv run --project /Users/aaat/projects/agileforge --frozen python - <<'PY'
import json
from pathlib import Path
payload = json.loads(Path("/tmp/cartola-sprint-candidates-before-readiness-repair.json").read_text())
data = payload["data"]
print(json.dumps(data["readiness"], indent=2))
PY
```

Expected: readiness is blocked.

- [ ] **Step 4: Run repair**

Run:

```bash
agileforge story repair-readiness \
  --project-id 2 \
  --expected-state SPRINT_SETUP \
  --idempotency-key repair-story-readiness-2-001
```

Expected: `ok: true` and `repair_result.repaired_count > 0`.

- [ ] **Step 5: Verify caRtola after repair**

Run:

```bash
agileforge sprint candidates --project-id 2 > /tmp/cartola-sprint-candidates-after-readiness-repair.json
uv run --project /Users/aaat/projects/agileforge --frozen python - <<'PY'
import json
from collections import Counter
from pathlib import Path
payload = json.loads(Path("/tmp/cartola-sprint-candidates-after-readiness-repair.json").read_text())
data = payload["data"]
items = data["items"]
print(json.dumps({
    "count": data["count"],
    "readiness": data["readiness"],
    "points": Counter(str(item.get("story_points")) for item in items),
    "priorities": Counter(str(item.get("priority")) for item in items),
}, indent=2))
PY
```

Expected:

```text
readiness.status == ready
unsized_count == 0
default_priority_count == 0
```

## Task 7: Final Verification

**Files:**
- All changed files.

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_save_stories_tool.py \
  tests/test_story_phase_service.py \
  tests/test_agent_workbench_story_phase.py \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_command_schema.py \
  tests/test_sprint_planner_tools.py \
  tests/test_agent_workbench_read_projection.py \
  tests/test_api_sprint_flow.py \
  -q
```

Expected: all pass.

- [ ] **Step 2: Run Ruff**

Run:

```bash
uv run --frozen ruff check \
  orchestrator_agent/agent_tools/user_story_writer_tool/tools.py \
  services/phases/story_service.py \
  services/agent_workbench/story_phase.py \
  services/agent_workbench/application.py \
  services/agent_workbench/command_registry.py \
  services/orchestrator_query_service.py \
  services/agent_workbench/read_projection.py \
  services/phases/sprint_service.py \
  cli/main.py \
  tests/test_save_stories_tool.py \
  tests/test_story_phase_service.py \
  tests/test_agent_workbench_story_phase.py \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_command_schema.py \
  tests/test_sprint_planner_tools.py \
  tests/test_agent_workbench_read_projection.py \
  tests/test_api_sprint_flow.py
```

Expected: all checks pass.

- [ ] **Step 3: Check docs for stale guidance**

Run:

```bash
rg -n "story_points: null|priority: 999|manual sizing|default 100\\.0" docs/agent-cli-manual.md
```

Expected: no stale guidance that tells agents to proceed with unsized/default-priority Sprint candidates.

## Execution Notes

- Do not manually edit caRtola database rows.
- Do not continue to Sprint generation until `sprint candidates` returns readiness `ready`.
- Do not make Sprint infer story points from text. Story persistence owns sizing.
- Do not let `sprint generate` proceed with unready candidates.

