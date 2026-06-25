# Requirement-Level Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `agileforge requirement reconcile` so brownfield roadmap requirements can be explicitly kept, deferred, archived, superseded, marked duplicate, or marked already implemented before story generation.

**Architecture:** Store requirement decisions in workflow session state beside `roadmap_releases`, `story_saved`, and story runtime state, and write a `WorkflowEvent` audit entry through the existing Story phase runner. Treat only `archive`, `defer`, `supersede`, `already-implemented`, and `duplicate` as story-satisfying decisions; `keep` and `rewrite-needed` remain unresolved and must keep routing to Story generation/refinement.

**Tech Stack:** Python, SQLModel, argparse CLI, existing Agent Workbench facade, workflow session state, pytest with `uv run --frozen`.

---

## Assessment Synthesis

The reports are right on the main gap: `agileforge story reconcile` requires a saved `UserStory.story_id`, and `agileforge backlog reconcile` only repairs duplicate active backlog seed rows. Neither can record a PO decision for a pending roadmap requirement in `state["roadmap_releases"]`.

Do not start with a new SQL table. Current roadmap/story phase state already lives in workflow session JSON, and adding a migration/table would make this first unblock bigger than needed. The minimum durable fix is session-state reconciliation plus a `WorkflowEvent` entry.

Correct one overstatement in the reports: `sprint candidates` does not directly read roadmap requirements. It reads `UserStory` rows and then applies `story_completion_scope`. So the required downstream behavior is:

- `story pending` shows terminal reconciliations as handled.
- `story complete` and `workflow next` treat terminal reconciliations as satisfied.
- Sprint setup no-candidate recovery does not keep routing to Story generation when all relevant requirements are terminal-reconciled.

## File Map

- Modify `services/phases/story_service.py`
  - Owns workflow-session Story state helpers.
  - Add requirement reconciliation validation, storage, lookup, and completion accounting.

- Modify `services/agent_workbench/story_phase.py`
  - Add `StoryPhaseRunner.requirement_reconcile(...)`.
  - Delegate to the service helper and write a `WorkflowEvent` audit record.

- Modify `services/agent_workbench/application.py`
  - Add facade/protocol method.
  - Use "covered or terminal-reconciled" only where the code means "does not need more Story work."
  - Keep true story-coverage checks separate where an actual story row/draft is required.

- Modify `cli/main.py`
  - Add `_Application.requirement_reconcile(...)`.
  - Add top-level `requirement reconcile` parser and handler.

- Modify `services/agent_workbench/command_registry.py`
  - Register `agileforge requirement reconcile` for capabilities/schema availability.

- Modify tests:
  - `tests/test_story_phase_service.py`
  - `tests/test_agent_workbench_story_phase.py`
  - `tests/test_agent_workbench_application.py`
  - `tests/test_agent_workbench_cli.py`

## Task 1: Prove Requirement Reconciliation In Story Service

**Files:**
- Modify: `tests/test_story_phase_service.py`

- [ ] **Step 1: Add failing pending-output test**

Append near `test_get_story_pending_groups_requirements_by_status`:

```python
@pytest.mark.asyncio
async def test_get_story_pending_marks_terminal_requirement_reconciliation_handled() -> None:
    """Terminal requirement reconciliation is visible and counted as handled."""
    state = _pending_state()
    state["requirement_reconciliations"] = {
        "requirement b": {
            "schema_version": "agileforge.requirement_reconciliation.v1",
            "requirement": "Requirement B",
            "action": "already-implemented",
            "reason": "Delivered in Sprint 7.",
            "evidence_links": ["sprint-7-closeout.md"],
            "changed_by": "agent",
            "reconciled_at": "2026-06-25T10:00:00Z",
            "idempotency_key": "req-rec-1",
            "terminal": True,
        }
    }

    payload = await get_story_pending(load_state=lambda: _async_value(state))

    assert payload["total_count"] == 2
    assert payload["saved_count"] == 1
    assert payload["reconciled_count"] == 1
    assert payload["handled_count"] == 2
    assert payload["grouped_items"][0]["requirements"][1] == {
        "requirement": "Requirement B",
        "status": "Reconciled",
        "attempt_count": 1,
        "reconciliation": {
            "action": "already-implemented",
            "reason": "Delivered in Sprint 7.",
            "evidence_links": ["sprint-7-closeout.md"],
            "changed_by": "agent",
            "reconciled_at": "2026-06-25T10:00:00Z",
            "terminal": True,
        },
    }
```

- [ ] **Step 2: Add failing completion test**

Append near existing `complete_story_phase` tests:

```python
@pytest.mark.asyncio
async def test_complete_story_phase_counts_terminal_requirement_reconciliation() -> None:
    """Story phase can complete when uncovered requirements are terminal-reconciled."""
    state: JsonDict = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [
            {"items": ["Requirement A", "Requirement B"]},
        ],
        "story_saved": {"Requirement A": True},
        "requirement_reconciliations": {
            "requirement b": {
                "schema_version": "agileforge.requirement_reconciliation.v1",
                "requirement": "Requirement B",
                "action": "duplicate",
                "reason": "Covered by Requirement A.",
                "evidence_links": ["story-17"],
                "changed_by": "agent",
                "reconciled_at": "2026-06-25T10:00:00Z",
                "idempotency_key": "req-rec-2",
                "terminal": True,
            }
        },
    }
    saved_state: JsonDict = {}

    payload = await complete_story_phase(
        expected_state="STORY_PERSISTENCE",
        idempotency_key="complete-with-reconciled-req",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_state.update(updated),
        now_iso=lambda: "2026-06-25T10:05:00Z",
    )

    assert payload["coverage"] == {
        "saved": 1,
        "merged": 0,
        "reconciled": 1,
        "total": 2,
    }
    assert saved_state["fsm_state"] == "SPRINT_SETUP"
```

- [ ] **Step 3: Run red tests**

Run:

```bash
uv run --frozen pytest tests/test_story_phase_service.py \
  -k 'requirement_reconciliation or complete_story_phase_counts_terminal_requirement_reconciliation' -q
```

Expected: fail because reconciliation helpers/counts do not exist yet.

## Task 2: Add Minimal Requirement Reconciliation State Helpers

**Files:**
- Modify: `services/phases/story_service.py`
- Test: `tests/test_story_phase_service.py`

- [ ] **Step 1: Add constants and helpers**

Add near existing Story phase constants:

```python
REQUIREMENT_RECONCILIATION_SCHEMA_VERSION = (
    "agileforge.requirement_reconciliation.v1"
)
REQUIREMENT_RECONCILIATION_STATE_KEY = "requirement_reconciliations"
REQUIREMENT_RECONCILIATION_HISTORY_KEY = "requirement_reconciliation_history"
REQUIREMENT_RECONCILIATION_IDEMPOTENCY_KEY = (
    "requirement_reconciliation_idempotency"
)
REQUIREMENT_RECONCILIATION_ACTIONS = frozenset(
    {
        "keep",
        "archive",
        "defer",
        "supersede",
        "already-implemented",
        "duplicate",
        "rewrite-needed",
    }
)
REQUIREMENT_RECONCILIATION_SATISFIED_ACTIONS = frozenset(
    {
        "archive",
        "defer",
        "supersede",
        "already-implemented",
        "duplicate",
    }
)
REQUIREMENT_RECONCILIATION_ALLOWED_STATES = frozenset(
    {
        OrchestratorState.STORY_INTERVIEW.value,
        OrchestratorState.STORY_PERSISTENCE.value,
        OrchestratorState.SPRINT_SETUP.value,
        OrchestratorState.SPRINT_COMPLETE.value,
    }
)
```

Add helper functions near `get_all_roadmap_requirements`:

```python
def requirement_reconciliation_key(requirement: str) -> str:
    """Return the stable lookup key for one roadmap requirement string."""
    return " ".join(requirement.strip().split()).casefold()


def requirement_reconciliation_for(
    state: dict[str, Any],
    *,
    parent_requirement: str,
) -> dict[str, Any] | None:
    """Return the latest requirement reconciliation decision for a requirement."""
    reconciliations = state.get(REQUIREMENT_RECONCILIATION_STATE_KEY)
    if not isinstance(reconciliations, dict):
        return None
    candidate = reconciliations.get(requirement_reconciliation_key(parent_requirement))
    return candidate if isinstance(candidate, dict) else None


def requirement_reconciliation_satisfies_story_requirement(
    state: dict[str, Any],
    *,
    parent_requirement: str,
) -> bool:
    """Return whether a reconciliation means no Story work is required now."""
    reconciliation = requirement_reconciliation_for(
        state,
        parent_requirement=parent_requirement,
    )
    if reconciliation is None:
        return False
    action = str(reconciliation.get("action") or "").strip().lower()
    return action in REQUIREMENT_RECONCILIATION_SATISFIED_ACTIONS


def _roadmap_requirement_matches(
    state: dict[str, Any],
    *,
    requirement: str,
) -> list[str]:
    target_key = requirement_reconciliation_key(requirement)
    return [
        item
        for item in get_all_roadmap_requirements(state)
        if isinstance(item, str)
        and requirement_reconciliation_key(item) == target_key
    ]
```

- [ ] **Step 2: Add service mutation**

Add this async function near other public Story phase service commands:

```python
async def reconcile_requirement(
    *,
    project_id: int,
    requirement: str,
    action: str,
    reason: str,
    idempotency_key: str,
    changed_by: str = "cli-agent",
    evidence_links: list[str] | None = None,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
) -> dict[str, Any]:
    """Record a requirement-level reconciliation decision."""
    normalized_requirement = requirement.strip()
    normalized_action = action.strip().lower()
    normalized_reason = reason.strip()
    normalized_idempotency_key = idempotency_key.strip()
    if not normalized_requirement:
        raise StoryPhaseError("requirement reconcile requires --requirement", status_code=400)
    if normalized_action not in REQUIREMENT_RECONCILIATION_ACTIONS:
        raise StoryPhaseError(
            "Unsupported requirement reconciliation action.",
            status_code=400,
        )
    if not normalized_reason:
        raise StoryPhaseError("requirement reconcile requires --reason", status_code=400)
    if not normalized_idempotency_key:
        raise StoryPhaseError(
            "requirement reconcile requires --idempotency-key",
            status_code=400,
        )

    state = await load_state()
    current_state = _normalize_fsm_state(state.get("fsm_state"))
    if current_state not in REQUIREMENT_RECONCILIATION_ALLOWED_STATES:
        raise StoryPhaseError(
            "requirement reconcile is only available during Story/Sprint planning states.",
            status_code=409,
        )

    idempotency_registry = state.get(REQUIREMENT_RECONCILIATION_IDEMPOTENCY_KEY)
    if isinstance(idempotency_registry, dict):
        existing = idempotency_registry.get(normalized_idempotency_key)
        if isinstance(existing, dict):
            return dict(existing)

    matches = _roadmap_requirement_matches(state, requirement=normalized_requirement)
    if not matches:
        raise StoryPhaseError(
            "Requirement reconciliation target was not found in saved roadmap releases.",
            status_code=400,
        )
    if len(matches) > 1:
        raise StoryPhaseError(
            "Requirement reconciliation target is ambiguous in saved roadmap releases.",
            status_code=400,
        )

    matched_requirement = matches[0]
    payload: dict[str, Any] = {
        "schema_version": REQUIREMENT_RECONCILIATION_SCHEMA_VERSION,
        "project_id": project_id,
        "requirement": matched_requirement,
        "action": normalized_action,
        "reason": normalized_reason,
        "evidence_links": evidence_links or [],
        "changed_by": changed_by,
        "reconciled_at": now_iso(),
        "idempotency_key": normalized_idempotency_key,
        "terminal": normalized_action in REQUIREMENT_RECONCILIATION_SATISFIED_ACTIONS,
    }

    reconciliations = state.get(REQUIREMENT_RECONCILIATION_STATE_KEY)
    if not isinstance(reconciliations, dict):
        reconciliations = {}
        state[REQUIREMENT_RECONCILIATION_STATE_KEY] = reconciliations
    reconciliations[requirement_reconciliation_key(matched_requirement)] = payload

    history = state.get(REQUIREMENT_RECONCILIATION_HISTORY_KEY)
    if not isinstance(history, list):
        history = []
        state[REQUIREMENT_RECONCILIATION_HISTORY_KEY] = history
    history.append(payload)

    if not isinstance(idempotency_registry, dict):
        idempotency_registry = {}
        state[REQUIREMENT_RECONCILIATION_IDEMPOTENCY_KEY] = idempotency_registry
    idempotency_registry[normalized_idempotency_key] = payload
    save_state(state)
    return payload
```

- [ ] **Step 3: Update pending and complete accounting**

In `_story_pending_items`, before checking saved/merged/attempted, get:

```python
reconciliation = requirement_reconciliation_for(state, parent_requirement=req)
reconciled = requirement_reconciliation_satisfies_story_requirement(
    state,
    parent_requirement=req,
)
```

Use this status branch before saved/merged:

```python
merged_count = 0
reconciled_count = 0

if reconciled:
    status = "Reconciled"
    reconciled_count += 1
elif _story_saved_for_scope(...):
    status = "Saved"
    saved_count += 1
elif _story_resolution_for_scope(...):
    status = "Merged"
    merged_count += 1
...
```

When appending the requirement row, include reconciliation details only when present:

```python
item = {
    "requirement": req,
    "status": status,
    "attempt_count": attempt_count,
    **(extension_metadata or {}),
}
if reconciliation is not None:
    item["reconciliation"] = {
        "action": reconciliation.get("action"),
        "reason": reconciliation.get("reason"),
        "evidence_links": reconciliation.get("evidence_links") or [],
        "changed_by": reconciliation.get("changed_by"),
        "reconciled_at": reconciliation.get("reconciled_at"),
        "terminal": bool(reconciliation.get("terminal")),
    }
milestone_group["requirements"].append(item)
```

Return these counts:

```python
return {
    "grouped_items": grouped_items,
    "total_count": total_count,
    "saved_count": saved_count,
    "reconciled_count": reconciled_count,
    "handled_count": saved_count + merged_count + reconciled_count,
}
```

In `complete_story_phase`, add `reconciled_count = 0`, count terminal reconciliations after saved/merged checks, and return:

```python
"coverage": {
    "saved": saved_count,
    "merged": merged_count,
    "reconciled": reconciled_count,
    "total": total_count,
}
```

Change the failure message to say `saved, merged, or terminal-reconciled`.

- [ ] **Step 4: Run green service tests**

Run:

```bash
uv run --frozen pytest tests/test_story_phase_service.py \
  -k 'requirement_reconciliation or complete_story_phase_counts_terminal_requirement_reconciliation or get_story_pending_groups_requirements_by_status' -q
```

Expected: pass.

## Task 3: Add Runner, CLI, And Command Registry

**Files:**
- Modify: `services/agent_workbench/story_phase.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `cli/main.py`
- Modify: `services/agent_workbench/command_registry.py`
- Test: `tests/test_agent_workbench_story_phase.py`
- Test: `tests/test_agent_workbench_cli.py`

- [ ] **Step 1: Add runner test**

Add to `tests/test_agent_workbench_story_phase.py` near Story reconcile tests:

```python
def test_requirement_reconcile_records_decision_and_audit_event(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement reconcile writes session state and a WorkflowEvent audit record."""
    monkeypatch.setattr(
        "services.agent_workbench.story_phase.get_engine",
        session.get_bind,
    )
    session.add(Product(product_id=PROJECT_ID, name="Cartola"))
    session.commit()
    workflow = _FakeWorkflowService()
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=workflow,
    )

    result = runner.requirement_reconcile(
        project_id=PROJECT_ID,
        requirement="Review match result",
        action="already-implemented",
        reason="Delivered in Sprint 7.",
        idempotency_key="req-rec-runner-1",
        changed_by="agent",
        evidence_links=["sprint-7-closeout.md"],
    )

    assert result["ok"] is True
    assert result["data"]["requirement"] == "Review match result"
    assert result["data"]["terminal"] is True
    stored = workflow.state["requirement_reconciliations"]["review match result"]
    assert stored["reason"] == "Delivered in Sprint 7."
    event = session.exec(select(WorkflowEvent)).one()
    assert event.event_type == WorkflowEventType.STORIES_SAVED
    assert '"action": "requirement_reconcile"' in (event.event_metadata or "")
```

- [ ] **Step 2: Implement runner method**

In `services/agent_workbench/story_phase.py`, import `reconcile_requirement` from `services.phases.story_service`.

Add an event replay helper near `_reconcile_replay`:

```python
def _requirement_reconcile_replay(
    session: Session,
    *,
    project_id: int,
    idempotency_key: str,
) -> dict[str, Any] | None:
    """Return a prior requirement reconciliation result for the same key."""
    events = session.exec(
        select(WorkflowEvent).where(
            WorkflowEvent.product_id == project_id,
            WorkflowEvent.event_type == WorkflowEventType.STORIES_SAVED,
        )
    ).all()
    for event in reversed(events):
        if not event.event_metadata:
            continue
        try:
            metadata = json.loads(event.event_metadata)
        except json.JSONDecodeError:
            continue
        if (
            metadata.get("action") == "requirement_reconcile"
            and metadata.get("idempotency_key") == idempotency_key
            and isinstance(metadata.get("result"), dict)
        ):
            return cast("dict[str, Any]", metadata["result"])
    return None
```

Add public wrapper near `reconcile(...)`:

```python
def requirement_reconcile(  # noqa: PLR0913
    self,
    *,
    project_id: int,
    requirement: str,
    action: str,
    reason: str,
    idempotency_key: str,
    changed_by: str = "cli-agent",
    evidence_links: list[str] | None = None,
) -> dict[str, Any]:
    """Record a roadmap requirement reconciliation decision."""
    return anyio.run(
        self._requirement_reconcile,
        project_id,
        requirement,
        action,
        reason,
        idempotency_key,
        changed_by,
        evidence_links,
    )
```

Add async implementation:

```python
async def _requirement_reconcile(  # noqa: PLR0913
    self,
    project_id: int,
    requirement: str,
    action: str,
    reason: str,
    idempotency_key: str,
    changed_by: str,
    evidence_links: list[str] | None,
) -> dict[str, Any]:
    product = self._load_project(project_id)
    if isinstance(product, dict):
        return product
    try:
        with Session(get_engine()) as session:
            replay = _requirement_reconcile_replay(
                session,
                project_id=project_id,
                idempotency_key=idempotency_key,
            )
            if replay is not None:
                return _data_envelope(replay)

        payload = await reconcile_requirement(
            project_id=project_id,
            requirement=requirement,
            action=action,
            reason=reason,
            idempotency_key=idempotency_key,
            changed_by=changed_by,
            evidence_links=evidence_links,
            load_state=lambda: self._load_story_state(str(project_id), project_id, product),
            save_state=lambda state: self._save_story_mutation_state(
                str(project_id),
                state,
                reason="requirement_reconciled",
                initial_fsm_state=None,
            ),
            now_iso=_now_iso,
        )
        with Session(get_engine()) as session:
            session.add(
                WorkflowEvent(
                    event_type=WorkflowEventType.STORIES_SAVED,
                    product_id=project_id,
                    session_id=str(project_id),
                    event_metadata=json.dumps(
                        {
                            "action": "requirement_reconcile",
                            "idempotency_key": idempotency_key,
                            "result": payload,
                        }
                    ),
                )
            )
            session.commit()
    except StoryPhaseError as exc:
        return _phase_error(exc)
    except RuntimeError as exc:
        return _workflow_error(exc)
    return _data_envelope(payload)
```

If `_save_story_mutation_state(... initial_fsm_state=None)` restores a bad state in practice, use `_save_session_state(...)` directly for this mutation and leave sprint working-set invalidation to a focused follow-up. Do not add a second generic mutation helper unless tests force it.

- [ ] **Step 3: Add facade and protocol method**

In both `cli/main.py` `_Application` and `services/agent_workbench/application.py` `_StoryPhaseRunner`, add:

```python
def requirement_reconcile(  # noqa: PLR0913
    self,
    *,
    project_id: int,
    requirement: str,
    action: str,
    reason: str,
    idempotency_key: str,
    changed_by: str = "cli-agent",
    evidence_links: list[str] | None = None,
) -> JsonObject:
    """Record a requirement reconciliation decision."""
    ...
```

In `AgentWorkbenchApplication`, add:

```python
def requirement_reconcile(  # noqa: PLR0913
    self,
    *,
    project_id: int,
    requirement: str,
    action: str,
    reason: str,
    idempotency_key: str,
    changed_by: str = "cli-agent",
    evidence_links: list[str] | None = None,
) -> dict[str, Any]:
    """Record a requirement reconciliation decision."""
    return self._get_story_runner().requirement_reconcile(
        project_id=project_id,
        requirement=requirement,
        action=action,
        reason=reason,
        idempotency_key=idempotency_key,
        changed_by=changed_by,
        evidence_links=evidence_links,
    )
```

- [ ] **Step 4: Add CLI parser and routing test**

In `tests/test_agent_workbench_cli.py`, add fake app method mirroring `story_reconcile`:

```python
def requirement_reconcile(  # noqa: PLR0913
    self,
    *,
    project_id: int,
    requirement: str,
    action: str,
    reason: str,
    idempotency_key: str,
    changed_by: str = "cli-agent",
    evidence_links: list[str] | None = None,
) -> JsonObject:
    """Return a requirement reconcile payload."""
    self.calls.append(
        (
            "requirement_reconcile",
            {
                "project_id": project_id,
                "requirement": requirement,
                "action": action,
                "reason": reason,
                "idempotency_key": idempotency_key,
                "changed_by": changed_by,
                "evidence_links": evidence_links,
            },
        )
    )
    return {
        "ok": True,
        "data": {"project_id": project_id, "requirement": requirement},
        "warnings": [],
        "errors": [],
    }
```

Add to the existing CLI route parameter table:

```python
(
    [
        "requirement",
        "reconcile",
        "--project-id",
        str(PROJECT_ID),
        "--requirement",
        "Review Storage Durability Hardening",
        "--action",
        "already-implemented",
        "--reason",
        "Delivered in Sprint 7.",
        "--idempotency-key",
        "req-rec-cli-1",
        "--changed-by",
        "agent",
        "--evidence-link",
        "sprint-7-closeout.md",
    ],
    (
        "requirement_reconcile",
        {
            "project_id": PROJECT_ID,
            "requirement": "Review Storage Durability Hardening",
            "action": "already-implemented",
            "reason": "Delivered in Sprint 7.",
            "idempotency_key": "req-rec-cli-1",
            "changed_by": "agent",
            "evidence_links": ["sprint-7-closeout.md"],
        },
    ),
    "agileforge requirement reconcile",
),
```

In `cli/main.py`, add handler:

```python
def _requirement_reconcile(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route requirement reconcile to the application facade."""
    return "agileforge requirement reconcile", application.requirement_reconcile(
        project_id=args.project_id,
        requirement=args.requirement,
        action=args.action,
        reason=args.reason,
        idempotency_key=args.idempotency_key,
        changed_by=args.changed_by,
        evidence_links=args.evidence_links,
    )
```

Add parser beside top-level `story` parser setup:

```python
requirement_sub = subparsers.add_parser(
    "requirement",
    help="Reconcile roadmap requirements before Story generation.",
)
requirement_actions = requirement_sub.add_subparsers(
    dest="requirement_action",
    required=True,
    parser_class=_WorkbenchArgumentParser,
)
requirement_reconcile = requirement_actions.add_parser(
    "reconcile",
    help="Record how a pending roadmap requirement should be handled.",
)
requirement_reconcile.add_argument("--project-id", type=int, required=True)
requirement_reconcile.add_argument("--requirement", required=True)
requirement_reconcile.add_argument(
    "--action",
    choices=[
        "keep",
        "archive",
        "defer",
        "supersede",
        "already-implemented",
        "duplicate",
        "rewrite-needed",
    ],
    required=True,
)
requirement_reconcile.add_argument("--reason", required=True)
requirement_reconcile.add_argument("--idempotency-key", required=True)
requirement_reconcile.add_argument("--changed-by", default="cli-agent")
requirement_reconcile.add_argument(
    "--evidence-link",
    action="append",
    dest="evidence_links",
    help="Evidence path or URL for this requirement decision.",
)
requirement_reconcile.set_defaults(command_handler=_requirement_reconcile)
```

- [ ] **Step 5: Register command metadata**

In `services/agent_workbench/command_registry.py`, add to `_PHASE_2D_COMMANDS` near Story commands:

```python
CommandMetadata(
    name="agileforge requirement reconcile",
    mutates=True,
    phase="phase_2d",
    requires_idempotency_key=True,
    input_required=(
        "project_id",
        "requirement",
        "action",
        "reason",
        "idempotency_key",
    ),
    input_optional=("changed_by", "evidence_link"),
    errors=(
        ErrorCode.PROJECT_NOT_FOUND.value,
        ErrorCode.INVALID_COMMAND.value,
        ErrorCode.WORKFLOW_SESSION_FAILED.value,
        ErrorCode.MUTATION_FAILED.value,
    ),
),
```

- [ ] **Step 6: Run focused CLI/runner tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_story_phase.py \
  -k 'requirement_reconcile or story_reconcile' -q
```

Expected: pass.

## Task 4: Wire Workflow Routing To Satisfied Requirements

**Files:**
- Modify: `services/agent_workbench/application.py`
- Test: `tests/test_agent_workbench_application.py`

- [ ] **Step 1: Add workflow-next tests**

Add near Story workflow-next tests:

```python
def test_workflow_next_treats_terminal_requirement_reconciliation_as_complete() -> None:
    """Terminal requirement decisions allow Story phase completion."""
    app = AgentWorkbenchApplication(
        read_projection=_WorkflowStateReader(
            {
                "fsm_state": "STORY_PERSISTENCE",
                "roadmap_releases": [{"items": ["Requirement A", "Requirement B"]}],
                "story_saved": {"Requirement A": True},
                "requirement_reconciliations": {
                    "requirement b": {
                        "schema_version": "agileforge.requirement_reconciliation.v1",
                        "requirement": "Requirement B",
                        "action": "already-implemented",
                        "reason": "Delivered earlier.",
                        "evidence_links": ["sprint-7-closeout.md"],
                        "changed_by": "agent",
                        "reconciled_at": "2026-06-25T10:00:00Z",
                        "idempotency_key": "req-rec-app-1",
                        "terminal": True,
                    }
                },
            }
        ),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    commands = result["data"]["next_valid_commands"]
    assert (
        "agileforge story complete --project-id 7 "
        "--expected-state STORY_PERSISTENCE "
        "--idempotency-key <idempotency_key>"
    ) in commands
    assert not any(
        command
        == (
            "agileforge story generate --project-id 7 "
            "--parent-requirement <parent_requirement>"
        )
        for command in commands
    )
```

Add keep/rewrite-needed guard:

```python
@pytest.mark.parametrize("action", ["keep", "rewrite-needed"])
def test_workflow_next_keeps_non_terminal_requirement_reconciliation_pending(
    action: str,
) -> None:
    """Keep/rewrite-needed decisions do not satisfy Story coverage."""
    app = AgentWorkbenchApplication(
        read_projection=_WorkflowStateReader(
            {
                "fsm_state": "STORY_PERSISTENCE",
                "roadmap_releases": [{"items": ["Requirement A"]}],
                "requirement_reconciliations": {
                    "requirement a": {
                        "schema_version": "agileforge.requirement_reconciliation.v1",
                        "requirement": "Requirement A",
                        "action": action,
                        "reason": "Still needs PO work.",
                        "evidence_links": [],
                        "changed_by": "agent",
                        "reconciled_at": "2026-06-25T10:00:00Z",
                        "idempotency_key": f"req-rec-{action}",
                        "terminal": False,
                    }
                },
            }
        ),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert (
        "agileforge story generate --project-id 7 "
        "--parent-requirement <parent_requirement>"
    ) in result["data"]["next_valid_commands"]
```

- [ ] **Step 2: Add application helper**

In `services/agent_workbench/application.py`, import:

```python
from services.phases.story_service import (
    requirement_reconciliation_satisfies_story_requirement,
)
```

Add helper near `_story_requirement_is_covered`:

```python
def _story_requirement_is_satisfied(
    state_data: dict[str, Any],
    *,
    parent_requirement: str,
) -> bool:
    """Return whether a requirement needs no more Story work now."""
    return _story_requirement_is_covered(
        state_data,
        parent_requirement=parent_requirement,
    ) or requirement_reconciliation_satisfies_story_requirement(
        state_data,
        parent_requirement=parent_requirement,
    )
```

- [ ] **Step 3: Replace only gate semantics**

Use `_story_requirement_is_satisfied(...)` in these functions:

- `_saveable_story_review_candidate`
- `_covered_existing_story_scope`
- `_covered_story_milestone_complete_commands`
- `_covered_story_selection_complete_command`
- `_story_coverage_is_complete`
- `_uncovered_story_requirements`

Do not replace calls that truly require an actual saved/merged story row.

- [ ] **Step 4: Keep Sprint setup from looping on terminal-reconciled requirements**

Change `_sprint_setup_story_refinement_blocker` signature:

```python
def _sprint_setup_story_refinement_blocker(
    candidates: dict[str, Any] | None,
    *,
    workflow: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
```

After the candidate count check and before returning a blocker:

```python
if workflow is not None and not _uncovered_story_requirements(workflow):
    return None
```

Update call sites in `workflow_next` and `_sprint_workflow_next` to pass `workflow=workflow`.

- [ ] **Step 5: Run focused application tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py \
  -k 'requirement_reconciliation or zero_candidates_to_pending_story_generation or story_persistence_to_complete_when_covered' -q
```

Expected: pass. Existing zero-candidates test must still route to Story generation when at least one uncovered requirement remains.

## Task 5: Full Verification And Manual CLI Smoke

**Files:**
- No new files.

- [ ] **Step 1: Run focused suite**

Run:

```bash
uv run --frozen pytest \
  tests/test_story_phase_service.py \
  tests/test_agent_workbench_story_phase.py \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_cli.py \
  -k 'requirement_reconcile or requirement_reconciliation or story_reconcile or story_pending or workflow_next_routes_zero_candidates' -q
```

Expected: pass.

- [ ] **Step 2: Run CLI help smoke**

Run:

```bash
uv run --frozen agileforge requirement reconcile --help
```

Expected: help shows `--project-id`, `--requirement`, `--action`, `--reason`, `--idempotency-key`, `--changed-by`, and `--evidence-link`.

- [ ] **Step 3: Run quality gates**

Run:

```bash
uv run --frozen ruff check services/phases/story_service.py services/agent_workbench/story_phase.py services/agent_workbench/application.py cli/main.py tests/test_story_phase_service.py tests/test_agent_workbench_story_phase.py tests/test_agent_workbench_application.py tests/test_agent_workbench_cli.py
uv run --frozen pytest tests/test_story_phase_service.py tests/test_agent_workbench_story_phase.py tests/test_agent_workbench_application.py tests/test_agent_workbench_cli.py -q
git diff --check
```

Expected: all pass.

- [ ] **Step 4: Optional full gate before commit**

Run if time allows and the tree is otherwise clean:

```bash
uv run --frozen pyrepo-check --all
```

Expected: pass.

## Out Of Scope For This Fix

- No new SQL table or migration unless session-state storage proves insufficient in tests.
- No fuzzy requirement matching. Exact normalized string match only; duplicate roadmap requirement names fail closed as ambiguous.
- No backlog row deletion.
- No broad redesign of `sprint candidates`; only prevent workflow routing from treating terminal-reconciled requirements as still needing Story generation.
