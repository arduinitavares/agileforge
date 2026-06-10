# Post-Sprint Learning Triage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement accepted post-sprint learning triage so `SPRINT_COMPLETE` becomes a durable next-cycle decision point instead of defaulting to backlog refinement.

**Architecture:** Store triage metadata in workflow state, with validation and fingerprints isolated in a small helper module. `SprintPhaseRunner` owns `sprint review` and `sprint triage`; `AgentWorkbenchApplication.workflow_next` projects the triage-gated routing contract; CLI/API/dashboard surfaces expose the read-only status and command guidance. Bridge commands perform workflow transitions; `sprint triage` never changes `fsm_state`.

**Tech Stack:** Python 3.13, SQLModel, FastAPI dashboard API, AgileForge CLI argparse, pytest, pyrepo-check.

---

## Preconditions

- Worktree: `/Users/aaat/projects/agileforge-post-sprint-learning-triage`
- Branch: `dev/post-sprint-learning-triage`
- Baseline verification already run in this worktree:
  - `pyrepo-check --all`
  - Result: `2105 passed, 2 skipped, 13 deselected, 2 warnings`
- Accepted design: `docs/superpowers/specs/2026-06-10-post-sprint-learning-triage-design.md`

## File Structure

- Create `services/agent_workbench/post_sprint_triage.py`
  - Owns triage schema constants, normalization, validation, request fingerprinting, current-triage projection, and workflow-state mutation helpers.
- Modify `services/agent_workbench/sprint_phase.py`
  - Adds `SprintPhaseRunner.review()` and `SprintPhaseRunner.triage()`.
  - Records workflow events and persists triage state.
- Modify `services/agent_workbench/application.py`
  - Adds facade methods for sprint review/triage.
  - Replaces `SPRINT_COMPLETE` backlog-default routing with triage-gated `next_actions`.
- Modify `cli/main.py`
  - Adds `agileforge sprint review` and `agileforge sprint triage` parsers and handlers.
- Modify `services/agent_workbench/command_registry.py`
  - Registers sprint review and sprint triage command metadata.
- Modify `services/agent_workbench/error_codes.py`
  - Adds stable triage and backlog-source error codes.
- Modify `models/enums.py`
  - Adds `WorkflowEventType.POST_SPRINT_TRIAGE_RECORDED`.
- Modify `services/agent_workbench/read_projection.py` and `api.py`
  - Projects `post_sprint_triage`, `post_sprint_triage_required`, and sprint runtime triage fields.
- Modify `frontend/project.js`
  - Renders `SPRINT_COMPLETE` as a post-sprint triage decision state in existing dashboard panels.
- Tests:
  - `tests/test_post_sprint_triage.py`
  - `tests/test_agent_workbench_sprint_phase.py`
  - `tests/test_agent_workbench_application.py`
  - `tests/test_agent_workbench_command_schema.py`
  - `tests/test_agent_workbench_cli.py`
  - `tests/test_agent_workbench_read_projection.py`
  - `tests/test_api_sprint_flow.py`

## Task 1: Triage Validation And Fingerprints

**Files:**
- Create: `services/agent_workbench/post_sprint_triage.py`
- Create: `tests/test_post_sprint_triage.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_post_sprint_triage.py` with these tests:

```python
from __future__ import annotations

import pytest

from services.agent_workbench.post_sprint_triage import (
    TRIAGE_SCHEMA_VERSION,
    PostSprintTriageValidationError,
    build_triage_payload,
    current_triage_for_latest_sprint,
    post_sprint_triage_required,
)


def test_build_triage_payload_normalizes_and_fingerprints_story_impact() -> None:
    payload = build_triage_payload(
        project_id=7,
        sprint_id=13,
        impact="story",
        affected_requirements=["  Quality Gate  ", "Quality Gate"],
        affected_task_ids=[],
        affected_story_ids=[4, "4", "5"],
        affected_backlog_item_ids=[],
        affected_roadmap_item_ids=[],
        affected_layers=[],
        learning_summary="  Spike confirmed the next Story. ",
        decision_reason=" Continue Story work. ",
        idempotency_key="triage-001",
        replace_existing=False,
        recorded_at="2026-06-10T00:00:00Z",
        recorded_by="cli-agent",
    )

    assert payload["schema_version"] == TRIAGE_SCHEMA_VERSION
    assert payload["sprint_id"] == 13
    assert payload["impact"] == "story"
    assert payload["affected_requirements"] == ["Quality Gate"]
    assert payload["affected_story_ids"] == [4, 5]
    assert payload["learning_summary"] == "Spike confirmed the next Story."
    assert payload["decision_reason"] == "Continue Story work."
    assert payload["request_fingerprint"].startswith("sha256:")
    assert payload["triage_fingerprint"].startswith("sha256:")


def test_build_triage_payload_rejects_multiple_without_structured_layers() -> None:
    with pytest.raises(PostSprintTriageValidationError) as excinfo:
        build_triage_payload(
            project_id=7,
            sprint_id=13,
            impact="multiple",
            affected_requirements=[],
            affected_task_ids=[],
            affected_story_ids=[],
            affected_backlog_item_ids=[],
            affected_roadmap_item_ids=[],
            affected_layers=["story"],
            learning_summary="Several things changed.",
            decision_reason="Story and backlog are mentioned in prose.",
            idempotency_key="triage-002",
            replace_existing=False,
            recorded_at="2026-06-10T00:00:00Z",
            recorded_by="cli-agent",
        )

    assert excinfo.value.code == "TRIAGE_IMPACT_FIELDS_INVALID"


def test_current_triage_for_latest_sprint_requires_matching_sprint_id() -> None:
    state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": 14,
        "post_sprint_triage": {"sprint_id": 13, "impact": "none"},
    }

    assert current_triage_for_latest_sprint(state) is None
    assert post_sprint_triage_required(state) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --frozen python -m pytest tests/test_post_sprint_triage.py -q
```

Expected: FAIL because `services.agent_workbench.post_sprint_triage` does not exist.

- [ ] **Step 3: Implement the helper module**

Create `services/agent_workbench/post_sprint_triage.py` with:

```python
"""Post-sprint triage validation and workflow-state helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from services.agent_workbench.fingerprints import canonical_hash

TRIAGE_SCHEMA_VERSION: Final[str] = "agileforge.post_sprint_triage.v1"
VALID_IMPACTS: Final[frozenset[str]] = frozenset(
    {"none", "task", "story", "roadmap", "backlog", "multiple"}
)
VALID_AFFECTED_LAYERS: Final[frozenset[str]] = frozenset(
    {"task", "story", "roadmap", "backlog"}
)
STANDARD_AFFECTED_FIELDS: Final[tuple[str, ...]] = (
    "affected_requirements",
    "affected_task_ids",
    "affected_story_ids",
    "affected_backlog_item_ids",
    "affected_roadmap_item_ids",
)


@dataclass(frozen=True)
class PostSprintTriageValidationError(ValueError):
    """Raised when a post-sprint triage request violates the contract."""

    code: str
    message: str
    details: dict[str, Any]
    remediation: list[str]


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _clean_text_list(values: list[object] | None) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for value in values or []:
        text = _clean_text(value)
        if text and text not in seen:
            seen.add(text)
            normalized.append(text)
    return normalized


def _clean_positive_int_list(values: list[object] | None) -> list[int]:
    seen: set[int] = set()
    normalized: list[int] = []
    for value in values or []:
        if isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in seen:
            seen.add(number)
            normalized.append(number)
    return normalized


def _raise_invalid_fields(message: str, details: dict[str, Any]) -> None:
    raise PostSprintTriageValidationError(
        code="TRIAGE_IMPACT_FIELDS_INVALID",
        message=message,
        details=details,
        remediation=["Retry with the affected fields required by the chosen impact."],
    )


def _validate_impact_fields(payload: dict[str, Any]) -> None:
    impact = str(payload["impact"])
    affected_requirements = payload["affected_requirements"]
    affected_task_ids = payload["affected_task_ids"]
    affected_story_ids = payload["affected_story_ids"]
    affected_backlog_item_ids = payload["affected_backlog_item_ids"]
    affected_roadmap_item_ids = payload["affected_roadmap_item_ids"]
    affected_layers = payload["affected_layers"]
    has_any_affected_item = any(payload[field] for field in STANDARD_AFFECTED_FIELDS)

    if impact == "none":
        if has_any_affected_item or affected_layers:
            _raise_invalid_fields(
                "impact=none requires empty affected fields.",
                {"impact": impact},
            )
    elif impact == "task":
        if not (affected_task_ids or affected_story_ids or affected_requirements):
            _raise_invalid_fields(
                "impact=task requires a task id, story id, or affected requirement.",
                {"impact": impact},
            )
    elif impact == "story":
        if not (affected_story_ids or affected_requirements):
            _raise_invalid_fields(
                "impact=story requires a story id or affected requirement.",
                {"impact": impact},
            )
    elif impact == "roadmap":
        if not (affected_roadmap_item_ids or affected_requirements):
            _raise_invalid_fields(
                "impact=roadmap requires an affected requirement or roadmap item id.",
                {"impact": impact},
            )
    elif impact == "backlog":
        if not affected_backlog_item_ids and not payload["decision_reason"]:
            _raise_invalid_fields(
                "impact=backlog requires a backlog item id or decision reason.",
                {"impact": impact},
            )
    elif impact == "multiple":
        distinct_layers = set(affected_layers)
        if len(distinct_layers) < 2:
            _raise_invalid_fields(
                "impact=multiple requires at least two structured affected layers.",
                {"impact": impact, "affected_layers": affected_layers},
            )


def build_triage_payload(
    *,
    project_id: int,
    sprint_id: int,
    impact: str,
    affected_requirements: list[object] | None,
    affected_task_ids: list[object] | None,
    affected_story_ids: list[object] | None,
    affected_backlog_item_ids: list[object] | None,
    affected_roadmap_item_ids: list[object] | None,
    affected_layers: list[object] | None,
    learning_summary: str,
    decision_reason: str,
    idempotency_key: str,
    replace_existing: bool,
    recorded_at: str,
    recorded_by: str,
) -> dict[str, Any]:
    """Build a normalized triage payload and deterministic fingerprints."""
    normalized_impact = _clean_text(impact).lower()
    if normalized_impact not in VALID_IMPACTS:
        _raise_invalid_fields("Unknown post-sprint triage impact.", {"impact": impact})

    normalized_layers = _clean_text_list(affected_layers)
    invalid_layers = sorted(set(normalized_layers) - VALID_AFFECTED_LAYERS)
    if invalid_layers:
        _raise_invalid_fields(
            "affected_layers contains unsupported values.",
            {"affected_layers": normalized_layers, "invalid_layers": invalid_layers},
        )

    payload: dict[str, Any] = {
        "schema_version": TRIAGE_SCHEMA_VERSION,
        "sprint_id": int(sprint_id),
        "impact": normalized_impact,
        "affected_requirements": _clean_text_list(affected_requirements),
        "affected_task_ids": _clean_positive_int_list(affected_task_ids),
        "affected_story_ids": _clean_positive_int_list(affected_story_ids),
        "affected_backlog_item_ids": _clean_text_list(affected_backlog_item_ids),
        "affected_roadmap_item_ids": _clean_text_list(affected_roadmap_item_ids),
        "affected_layers": normalized_layers,
        "learning_summary": _clean_text(learning_summary),
        "decision_reason": _clean_text(decision_reason),
        "recorded_at": recorded_at,
        "recorded_by": recorded_by,
    }
    if not payload["learning_summary"]:
        _raise_invalid_fields("learning_summary is required.", {"field": "learning_summary"})
    if not payload["decision_reason"]:
        _raise_invalid_fields("decision_reason is required.", {"field": "decision_reason"})
    _validate_impact_fields(payload)

    request_fingerprint = canonical_hash(
        {
            "project_id": project_id,
            "sprint_id": payload["sprint_id"],
            "impact": payload["impact"],
            "affected_requirements": payload["affected_requirements"],
            "affected_task_ids": payload["affected_task_ids"],
            "affected_story_ids": payload["affected_story_ids"],
            "affected_backlog_item_ids": payload["affected_backlog_item_ids"],
            "affected_roadmap_item_ids": payload["affected_roadmap_item_ids"],
            "affected_layers": payload["affected_layers"],
            "learning_summary": payload["learning_summary"],
            "decision_reason": payload["decision_reason"],
            "idempotency_key": idempotency_key,
            "replace_existing": replace_existing,
        }
    )
    payload["request_fingerprint"] = request_fingerprint
    payload["triage_fingerprint"] = canonical_hash(payload)
    return payload


def current_triage_for_latest_sprint(state: dict[str, Any]) -> dict[str, Any] | None:
    """Return current triage only when it matches latest_completed_sprint_id."""
    triage = state.get("post_sprint_triage")
    if not isinstance(triage, dict):
        return None
    latest = state.get("latest_completed_sprint_id")
    if latest is None or triage.get("sprint_id") != latest:
        return None
    return triage


def post_sprint_triage_required(state: dict[str, Any]) -> bool:
    """Return whether SPRINT_COMPLETE is waiting for triage."""
    return (
        state.get("fsm_state") == "SPRINT_COMPLETE"
        and state.get("latest_completed_sprint_id") is not None
        and current_triage_for_latest_sprint(state) is None
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run --frozen python -m pytest tests/test_post_sprint_triage.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add services/agent_workbench/post_sprint_triage.py tests/test_post_sprint_triage.py
git commit -m "feat(sprint): add post-sprint triage payload helpers"
```

## Task 2: Sprint Review And Triage Runner

**Files:**
- Modify: `models/enums.py`
- Modify: `services/agent_workbench/error_codes.py`
- Modify: `services/agent_workbench/sprint_phase.py`
- Modify: `tests/test_agent_workbench_sprint_phase.py`

- [ ] **Step 1: Write the failing tests**

Append tests to `tests/test_agent_workbench_sprint_phase.py`:

```python
def test_sprint_review_returns_latest_completed_sprint_without_mutation(
    session: Session,
) -> None:
    product = Product(name="Triage Review Product")
    sprint = Sprint(
        product=product,
        goal="Review completed sprint",
        status=SprintStatus.COMPLETED,
    )
    session.add_all([product, sprint])
    session.commit()
    assert product.product_id is not None
    assert sprint.sprint_id is not None

    workflow = _FakeWorkflowService()
    workflow.state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": sprint.sprint_id,
    }
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow),
    )

    result = runner.review(project_id=product.product_id)

    assert result["ok"] is True
    assert result["data"]["fsm_state"] == "SPRINT_COMPLETE"
    assert result["data"]["latest_completed_sprint_id"] == sprint.sprint_id
    assert result["data"]["post_sprint_triage_required"] is True
    assert workflow.state["fsm_state"] == "SPRINT_COMPLETE"
    assert "post_sprint_triage" not in workflow.state


def test_sprint_triage_records_metadata_without_changing_fsm_state(
    session: Session,
) -> None:
    product = Product(name="Triage Product")
    sprint = Sprint(product=product, goal="Closed sprint", status=SprintStatus.COMPLETED)
    session.add_all([product, sprint])
    session.commit()
    assert product.product_id is not None
    assert sprint.sprint_id is not None

    workflow = _FakeWorkflowService()
    workflow.state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": sprint.sprint_id,
    }
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow),
    )

    result = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="story",
        learning_summary="Spike clarified the next story.",
        decision_reason="Only the pending story needs updated context.",
        idempotency_key="triage-story-001",
        affected_requirements=["Quality Gate"],
        affected_task_ids=[],
        affected_story_ids=[],
        affected_backlog_item_ids=[],
        affected_roadmap_item_ids=[],
        affected_layers=[],
        sprint_id=None,
        replace_existing=False,
        expected_triage_fingerprint=None,
        changed_by="cli-agent",
    )

    assert result["ok"] is True
    assert result["data"]["fsm_state"] == "SPRINT_COMPLETE"
    assert result["data"]["post_sprint_triage"]["impact"] == "story"
    assert result["data"]["post_sprint_triage"]["affected_requirements"] == [
        "Quality Gate"
    ]
    assert workflow.state["fsm_state"] == "SPRINT_COMPLETE"
    assert workflow.state["post_sprint_triage"]["impact"] == "story"
    assert workflow.state["post_sprint_triage_history"][-1]["history_action"] == (
        "recorded"
    )
    event = session.exec(
        select(WorkflowEvent).where(
            WorkflowEvent.event_type == WorkflowEventType.POST_SPRINT_TRIAGE_RECORDED
        )
    ).first()
    assert event is not None


def test_sprint_triage_guarded_correction_supersedes_previous_payload(
    session: Session,
) -> None:
    product = Product(name="Correction Product")
    sprint = Sprint(product=product, goal="Closed sprint", status=SprintStatus.COMPLETED)
    session.add_all([product, sprint])
    session.commit()
    assert product.product_id is not None
    assert sprint.sprint_id is not None

    workflow = _FakeWorkflowService()
    workflow.state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": sprint.sprint_id,
    }
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow),
    )
    first = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="none",
        learning_summary="Plan confirmed.",
        decision_reason="No downstream planning change.",
        idempotency_key="triage-none-001",
        affected_requirements=[],
        affected_task_ids=[],
        affected_story_ids=[],
        affected_backlog_item_ids=[],
        affected_roadmap_item_ids=[],
        affected_layers=[],
        sprint_id=None,
        replace_existing=False,
        expected_triage_fingerprint=None,
        changed_by="cli-agent",
    )

    corrected = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="story",
        learning_summary="One pending story needs the spike context.",
        decision_reason="The next requirement changed at Story level.",
        idempotency_key="triage-story-002",
        affected_requirements=["Quality Gate"],
        affected_task_ids=[],
        affected_story_ids=[],
        affected_backlog_item_ids=[],
        affected_roadmap_item_ids=[],
        affected_layers=[],
        sprint_id=None,
        replace_existing=True,
        expected_triage_fingerprint=first["data"]["post_sprint_triage"][
            "triage_fingerprint"
        ],
        changed_by="cli-agent",
    )

    assert corrected["ok"] is True
    assert workflow.state["post_sprint_triage"]["impact"] == "story"
    assert [entry["history_action"] for entry in workflow.state["post_sprint_triage_history"]] == [
        "recorded",
        "superseded",
        "corrected",
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --frozen python -m pytest tests/test_agent_workbench_sprint_phase.py -q -k "sprint_review or sprint_triage"
```

Expected: FAIL because `SprintPhaseRunner.review()` and `triage()` do not exist and the enum/error codes are missing.

- [ ] **Step 3: Add enum and error codes**

Modify `models/enums.py`:

```python
    POST_SPRINT_TRIAGE_RECORDED = "post_sprint_triage_recorded"
```

Add to `services/agent_workbench/error_codes.py`:

```python
    TRIAGE_ALREADY_RECORDED = "TRIAGE_ALREADY_RECORDED"
    TRIAGE_FINGERPRINT_MISMATCH = "TRIAGE_FINGERPRINT_MISMATCH"
    TRIAGE_EXPECTED_STATE_MISMATCH = "TRIAGE_EXPECTED_STATE_MISMATCH"
    TRIAGE_IMPACT_FIELDS_INVALID = "TRIAGE_IMPACT_FIELDS_INVALID"
    BACKLOG_SOURCE_UNAVAILABLE = "BACKLOG_SOURCE_UNAVAILABLE"
```

Register each new `ErrorCode` in `_ERROR_REGISTRY` with exit code `2` and `retryable=False`.

- [ ] **Step 4: Implement runner methods**

In `services/agent_workbench/sprint_phase.py`:

- Import helpers from `services.agent_workbench.post_sprint_triage`.
- Add command constants:

```python
_SPRINT_REVIEW_COMMAND = "agileforge sprint review"
_SPRINT_TRIAGE_COMMAND = "agileforge sprint triage"
_SPRINT_TRIAGE_LEASE_OWNER = "agileforge-cli:sprint-triage"
```

- Add public methods on `SprintPhaseRunner`:

```python
    def review(self, *, project_id: int, sprint_id: int | None = None) -> dict[str, Any]:
        """Return read-only post-sprint review context."""

    def triage(
        self,
        *,
        project_id: int,
        expected_state: str,
        impact: str,
        learning_summary: str,
        decision_reason: str,
        idempotency_key: str,
        affected_requirements: list[str] | None = None,
        affected_task_ids: list[int] | None = None,
        affected_story_ids: list[int] | None = None,
        affected_backlog_item_ids: list[str] | None = None,
        affected_roadmap_item_ids: list[str] | None = None,
        affected_layers: list[str] | None = None,
        sprint_id: int | None = None,
        replace_existing: bool = False,
        expected_triage_fingerprint: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record or correct post-sprint triage metadata."""
```

Implementation details:

- Resolve `sprint_id` from `latest_completed_sprint_id` when absent.
- Require current `fsm_state == expected_state == "SPRINT_COMPLETE"`.
- Verify the sprint row belongs to the project and has `SprintStatus.COMPLETED`.
- Use `MutationLedgerRepository.create_or_load()` for idempotency.
- Use `build_triage_payload()` to normalize and fingerprint the payload.
- Without `replace_existing`, reject existing current triage for the same sprint with `TRIAGE_ALREADY_RECORDED`.
- With `replace_existing`, require `expected_triage_fingerprint` to match current `post_sprint_triage_fingerprint`; otherwise return `TRIAGE_FINGERPRINT_MISMATCH`.
- Preserve `fsm_state`, all planning artifacts, stale markers, `latest_completed_sprint_id`, and `planned_sprint_id`.
- Append history entries with `history_action` values `recorded`, `superseded`, and `corrected`.
- Insert `WorkflowEvent(event_type=WorkflowEventType.POST_SPRINT_TRIAGE_RECORDED, product_id=project_id, sprint_id=resolved_sprint_id, session_id=str(project_id), event_metadata=json.dumps(event_metadata))`.

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
uv run --frozen python -m pytest tests/test_post_sprint_triage.py tests/test_agent_workbench_sprint_phase.py -q -k "post_sprint or sprint_review or sprint_triage"
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add models/enums.py services/agent_workbench/error_codes.py services/agent_workbench/sprint_phase.py tests/test_agent_workbench_sprint_phase.py
git commit -m "feat(sprint): record post-sprint triage decisions"
```

## Task 3: CLI And Command Registry

**Files:**
- Modify: `cli/main.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `tests/test_agent_workbench_command_schema.py`
- Modify: `tests/test_agent_workbench_cli.py`

- [ ] **Step 1: Write failing command-surface tests**

Add tests that assert:

```python
def test_command_schema_includes_sprint_review_and_triage() -> None:
    result = command_schema_payload("agileforge sprint triage")
    assert result["ok"] is True
    fields = result["data"]["input_required"]
    assert fields == [
        "project_id",
        "expected_state",
        "impact",
        "learning_summary",
        "decision_reason",
        "idempotency_key",
    ]


def test_cli_routes_sprint_triage_arguments_to_application() -> None:
    # Follow the existing CLI facade test pattern in tests/test_agent_workbench_cli.py:
    # use the fake application object, call main with sprint triage args, and assert
    # the fake captured affected_task_ids and affected_layers.
```

The CLI test must pass concrete args:

```bash
agileforge sprint triage --project-id 7 --expected-state SPRINT_COMPLETE --impact multiple --affected-layer story --affected-layer backlog --learning-summary Learned --decision-reason Routing --idempotency-key triage-001
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --frozen python -m pytest tests/test_agent_workbench_command_schema.py tests/test_agent_workbench_cli.py -q -k "sprint_triage or sprint_review"
```

Expected: FAIL because commands are not registered or parsed.

- [ ] **Step 3: Implement facade, registry, and CLI**

In `services/agent_workbench/application.py`, add:

```python
    def sprint_review(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return post-sprint review context."""
        return self._get_sprint_runner().review(
            project_id=project_id,
            sprint_id=sprint_id,
        )

    def sprint_triage(
        self,
        *,
        project_id: int,
        expected_state: str,
        impact: str,
        learning_summary: str,
        decision_reason: str,
        idempotency_key: str,
        affected_requirements: list[str] | None = None,
        affected_task_ids: list[int] | None = None,
        affected_story_ids: list[int] | None = None,
        affected_backlog_item_ids: list[str] | None = None,
        affected_roadmap_item_ids: list[str] | None = None,
        affected_layers: list[str] | None = None,
        sprint_id: int | None = None,
        replace_existing: bool = False,
        expected_triage_fingerprint: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record post-sprint triage metadata."""
        return self._get_sprint_runner().triage(
            project_id=project_id,
            expected_state=expected_state,
            impact=impact,
            learning_summary=learning_summary,
            decision_reason=decision_reason,
            idempotency_key=idempotency_key,
            affected_requirements=affected_requirements,
            affected_task_ids=affected_task_ids,
            affected_story_ids=affected_story_ids,
            affected_backlog_item_ids=affected_backlog_item_ids,
            affected_roadmap_item_ids=affected_roadmap_item_ids,
            affected_layers=affected_layers,
            sprint_id=sprint_id,
            replace_existing=replace_existing,
            expected_triage_fingerprint=expected_triage_fingerprint,
            changed_by=changed_by,
        )
```

In `services/agent_workbench/command_registry.py`, register:

```python
    CommandMetadata(
        name="agileforge sprint review",
        mutates=False,
        phase="phase_2d",
        input_required=("project_id",),
        input_optional=("sprint_id",),
    ),
    CommandMetadata(
        name="agileforge sprint triage",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=(
            "project_id",
            "expected_state",
            "impact",
            "learning_summary",
            "decision_reason",
            "idempotency_key",
        ),
        input_optional=(
            "sprint_id",
            "affected_requirement",
            "affected_task_id",
            "affected_story_id",
            "affected_backlog_item_id",
            "affected_roadmap_item_id",
            "affected_layer",
            "replace_existing",
            "expected_triage_fingerprint",
            "changed_by",
        ),
    ),
```

In `cli/main.py`, add parser entries under `sprint_sub`:

```python
    sprint_review = sprint_sub.add_parser(
        "review",
        help="Review completed Sprint learning before routing the next cycle.",
    )
    sprint_review.add_argument("--project-id", type=int, required=True)
    sprint_review.add_argument("--sprint-id", type=int)
    sprint_review.set_defaults(command_handler=_sprint_review)

    sprint_triage = sprint_sub.add_parser(
        "triage",
        help="Record post-sprint learning impact routing.",
    )
    sprint_triage.add_argument("--project-id", type=int, required=True)
    sprint_triage.add_argument("--sprint-id", type=int)
    sprint_triage.add_argument("--expected-state", required=True)
    sprint_triage.add_argument(
        "--impact",
        choices=["none", "task", "story", "roadmap", "backlog", "multiple"],
        required=True,
    )
    sprint_triage.add_argument("--affected-requirement", action="append", default=[])
    sprint_triage.add_argument("--affected-task-id", action="append", type=int, default=[])
    sprint_triage.add_argument("--affected-story-id", action="append", type=int, default=[])
    sprint_triage.add_argument("--affected-backlog-item-id", action="append", default=[])
    sprint_triage.add_argument("--affected-roadmap-item-id", action="append", default=[])
    sprint_triage.add_argument(
        "--affected-layer",
        action="append",
        choices=["task", "story", "roadmap", "backlog"],
        default=[],
    )
    sprint_triage.add_argument("--learning-summary", required=True)
    sprint_triage.add_argument("--decision-reason", required=True)
    sprint_triage.add_argument("--idempotency-key", required=True)
    sprint_triage.add_argument("--replace-existing", action="store_true")
    sprint_triage.add_argument("--expected-triage-fingerprint")
    sprint_triage.add_argument("--changed-by", default="cli-agent")
    sprint_triage.set_defaults(command_handler=_sprint_triage)
```

Add `_sprint_review()` and `_sprint_triage()` handlers mirroring existing sprint handlers.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run --frozen python -m pytest tests/test_agent_workbench_command_schema.py tests/test_agent_workbench_cli.py -q -k "sprint_triage or sprint_review"
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add cli/main.py services/agent_workbench/application.py services/agent_workbench/command_registry.py tests/test_agent_workbench_command_schema.py tests/test_agent_workbench_cli.py
git commit -m "feat(cli): expose post-sprint review and triage commands"
```

## Task 4: Workflow Next Triage Gate

**Files:**
- Modify: `services/agent_workbench/application.py`
- Modify: `tests/test_agent_workbench_application.py`

- [ ] **Step 1: Write failing workflow-next tests**

Add tests to `tests/test_agent_workbench_application.py`:

```python
def test_workflow_next_requires_post_sprint_triage_before_backlog_refinement() -> None:
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "post_sprint_triage_required"
    assert data["next_valid_commands"] == [
        "agileforge sprint review --project-id 7",
        (
            "agileforge sprint triage --project-id 7 "
            "--expected-state SPRINT_COMPLETE --impact <impact> "
            "--learning-summary <summary> --decision-reason <reason> "
            "--idempotency-key <idempotency_key>"
        ),
        "agileforge sprint history --project-id 7",
    ]
    assert not any("backlog refine" in command for command in data["next_valid_commands"])
    assert data["next_actions"][0]["status"] == "post_sprint_triage_required"
```

The fake read projection must return workflow state:

```python
{
    "fsm_state": "SPRINT_COMPLETE",
    "latest_completed_sprint_id": 13,
    "backlog_attempts": [
        {"attempt_id": "backlog-attempt-1", "artifact_fingerprint": "sha256:source"}
    ],
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --frozen python -m pytest tests/test_agent_workbench_application.py -q -k "post_sprint_triage_required"
```

Expected: FAIL because current `SPRINT_COMPLETE` routing advertises backlog refinement.

- [ ] **Step 3: Implement missing-triage routing**

In `services/agent_workbench/application.py`:

- Import `current_triage_for_latest_sprint` and `post_sprint_triage_required`.
- In `_sprint_workflow_next`, replace the `SPRINT_COMPLETE` branch with:
  - stale-guard handling first when `downstream_backlog_stale=true`
  - missing triage branch with status `post_sprint_triage_required`
  - recorded triage branch delegated to per-impact helpers
- Add `_post_sprint_triage_required_next_action()` returning structured action:

```python
{
    "command": "agileforge sprint triage",
    "status": "post_sprint_triage_required",
    "reason": "A completed Sprint needs learning triage before next-cycle routing.",
    "runnable": True,
    "requires": [
        "expected_state",
        "impact",
        "learning_summary",
        "decision_reason",
        "idempotency_key",
    ],
}
```

Do not include backlog refinement commands in `next_valid_commands` for this branch.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run --frozen python -m pytest tests/test_agent_workbench_application.py -q -k "post_sprint_triage_required"
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add services/agent_workbench/application.py tests/test_agent_workbench_application.py
git commit -m "feat(workflow): require triage after sprint completion"
```

## Task 5: Impact Routing For None, Story, Task, And Multiple

**Files:**
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/sprint_phase.py`
- Modify: `services/phases/sprint_service.py`
- Modify: `tests/test_agent_workbench_application.py`
- Modify: `tests/test_agent_workbench_sprint_phase.py`

- [ ] **Step 1: Write failing routing and bridge tests**

Add tests that prove:

```python
def test_workflow_next_routes_impact_none_to_story_and_sprint_continuation() -> None:
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteTriagedNoneReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "post_sprint_story_continuation_available"
    assert "agileforge story pending --project-id 7" in data["next_valid_commands"]
    assert "agileforge sprint candidates --project-id 7" in data["next_valid_commands"]
    assert not any("backlog refine" in command for command in data["next_valid_commands"])


def test_workflow_next_routes_impact_multiple_to_guarded_correction_only() -> None:
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteTriagedMultipleReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "post_sprint_multiple_impacts_need_decision"
    assert data["next_valid_commands"] == [
        "agileforge sprint review --project-id 7",
        (
            "agileforge sprint triage --project-id 7 "
            "--expected-state SPRINT_COMPLETE --replace-existing "
            "--expected-triage-fingerprint sha256:triage"
        ),
    ]
```

Add a runner test proving `sprint generate` can run from `SPRINT_COMPLETE` only when current triage impact is `none`.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --frozen python -m pytest tests/test_agent_workbench_application.py tests/test_agent_workbench_sprint_phase.py -q -k "impact_none or impact_multiple or SPRINT_COMPLETE"
```

Expected: FAIL because impact routing and `SPRINT_COMPLETE` sprint generation bridge are absent.

- [ ] **Step 3: Implement routing**

In `services/agent_workbench/application.py`, add helpers:

- `_post_sprint_none_next()`
- `_post_sprint_story_next()`
- `_post_sprint_task_next()`
- `_post_sprint_multiple_next()`

Rules:

- `none`: runnable `story pending`, `sprint candidates`, and `sprint generate` when no stale guard is active.
- `story`: runnable `story pending`, affected `story generate` commands for affected requirements, no Sprint generation until Story work is reconciled.
- `task`: runnable `sprint review`, `sprint status --sprint-id <latest_completed_sprint_id>`, `sprint history`; blocked task carryover action with reason `TASK_CARRYOVER_NOT_IMPLEMENTED`.
- `multiple`: runnable `sprint review` and guarded correction command only; layer bridges blocked with `POST_SPRINT_MULTIPLE_IMPACTS_NEED_DECISION`.

In `services/phases/sprint_service.py`, allow `SPRINT_COMPLETE` as a candidate generation state only when the runner has already validated triage impact `none`. Keep direct service behavior guarded by passing the current workflow state through the existing state checks.

In `services/agent_workbench/sprint_phase.py`, before `generate()` from `SPRINT_COMPLETE`, require current `post_sprint_triage.impact == "none"` and no stale guard. Return `INVALID_COMMAND` when absent or not `none`.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run --frozen python -m pytest tests/test_agent_workbench_application.py tests/test_agent_workbench_sprint_phase.py -q -k "impact_none or impact_story or impact_task or impact_multiple or SPRINT_COMPLETE"
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add services/agent_workbench/application.py services/agent_workbench/sprint_phase.py services/phases/sprint_service.py tests/test_agent_workbench_application.py tests/test_agent_workbench_sprint_phase.py
git commit -m "feat(workflow): route post-sprint triage impacts"
```

## Task 6: Backlog Impact, Source Availability, Planned Sprint, And Stale Guards

**Files:**
- Modify: `services/agent_workbench/application.py`
- Modify: `tests/test_agent_workbench_application.py`

- [ ] **Step 1: Write failing tests**

Add tests proving:

```python
def test_backlog_impact_records_but_blocks_refine_record_without_source() -> None:
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteTriagedBacklogNoSourceReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "post_sprint_backlog_refinement_available"
    assert not any("refine-record" in command for command in data["next_valid_commands"])
    assert data["blocked_commands"][0]["reason"] == "BACKLOG_SOURCE_UNAVAILABLE"


def test_active_reset_stale_guard_overrides_triage_none() -> None:
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteTriagedNoneActiveResetReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["status"] == "post_sprint_blocked_by_stale_backlog"
    assert result["data"]["blocked_commands"][0]["reason"] == (
        "DOWNSTREAM_BACKLOG_STALE_AFTER_ACTIVE_RESET"
    )
```

Add a planned Sprint test:

```python
def test_planned_sprint_start_is_blocked_until_triage_confirms_none() -> None:
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompletePlannedSprintMissingTriageReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["data"]["status"] == "post_sprint_triage_required"
    assert result["data"]["blocked_commands"][0]["reason"] == "POST_SPRINT_TRIAGE_REQUIRED"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --frozen python -m pytest tests/test_agent_workbench_application.py -q -k "backlog_impact or stale_guard or planned_sprint"
```

Expected: FAIL because these bridge details are not implemented.

- [ ] **Step 3: Implement backlog and stale routing**

In `services/agent_workbench/application.py`:

- Reuse `_latest_backlog_attempt()` for source attempt detection.
- Add `_post_sprint_backlog_next()`.
- If source attempt id and fingerprint exist, advertise `refine-preview`, `refine-record`, and `refine-import`.
- If no source exists, keep `impact=backlog` triage valid but return backlog bridge actions as blocked with reason `BACKLOG_SOURCE_UNAVAILABLE`.
- If `downstream_backlog_stale=true` and `stale_backlog_reason="refined_backlog_recorded"`, surface backlog review/save/reset guidance before per-impact routing.
- If `stale_backlog_reason="active_backlog_reset"`, reuse the existing active-reset roadmap partial-unblock status and block Story/Sprint commands.
- If `planned_sprint_id` exists before triage, return planned Sprint start as blocked with `POST_SPRINT_TRIAGE_REQUIRED`.
- If triage impact is `none`, planned Sprint start is runnable only when no stale guard is active.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run --frozen python -m pytest tests/test_agent_workbench_application.py -q -k "backlog_impact or stale_guard or planned_sprint"
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add services/agent_workbench/application.py tests/test_agent_workbench_application.py
git commit -m "feat(workflow): guard post-sprint backlog and stale routing"
```

## Task 7: Read Projection, API, And Dashboard Semantics

**Files:**
- Modify: `services/agent_workbench/read_projection.py`
- Modify: `api.py`
- Modify: `frontend/project.js`
- Modify: `tests/test_agent_workbench_read_projection.py`
- Modify: `tests/test_api_sprint_flow.py`

- [ ] **Step 1: Write failing projection tests**

Add tests proving:

```python
def test_read_projection_projects_post_sprint_triage_required() -> None:
    # Use the existing read projection fixture style.
    # Seed workflow state with SPRINT_COMPLETE and latest_completed_sprint_id.
    # Assert data.state.post_sprint_triage_required is True.


def test_sprint_runtime_summary_exposes_post_sprint_triage_context(session, monkeypatch):
    client, repo, workflow = _build_client(monkeypatch)
    project_id, completed_sprint_id = _seed_completed_sprint(session, repo)
    workflow.states[str(project_id)] = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": completed_sprint_id,
        "post_sprint_triage": {
            "schema_version": "agileforge.post_sprint_triage.v1",
            "sprint_id": completed_sprint_id,
            "impact": "none",
            "affected_requirements": [],
            "affected_task_ids": [],
            "affected_story_ids": [],
            "affected_backlog_item_ids": [],
            "affected_roadmap_item_ids": [],
            "affected_layers": [],
            "learning_summary": "Plan confirmed.",
            "decision_reason": "No planning impact.",
            "request_fingerprint": "sha256:request",
            "triage_fingerprint": "sha256:triage",
            "recorded_at": "2026-06-10T00:00:00Z",
            "recorded_by": "cli-agent",
        },
    }

    response = client.get(f"/api/projects/{project_id}/sprints")

    assert response.status_code == 200
    runtime = response.json()["data"]["runtime_summary"]
    assert runtime["post_sprint_triage_required"] is False
    assert runtime["post_sprint_triage"]["impact"] == "none"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --frozen python -m pytest tests/test_agent_workbench_read_projection.py tests/test_api_sprint_flow.py -q -k "post_sprint_triage"
```

Expected: FAIL because projections do not compute triage fields.

- [ ] **Step 3: Implement projections and dashboard wording**

In `services/agent_workbench/read_projection.py` and `api.py`:

- Import `current_triage_for_latest_sprint` and `post_sprint_triage_required`.
- Add `post_sprint_triage` and `post_sprint_triage_required` to effective project state and sprint runtime summary.

In `frontend/project.js`:

- When `sprintRuntimeSummary.post_sprint_triage_required` is true:
  - Runtime title: `Post-Sprint Triage Required`
  - Runtime text: `The last sprint is closed. Record what the sprint changed before routing backlog, roadmap, Story, or Sprint work.`
- When `post_sprint_triage.impact === "none"`:
  - Runtime title: `Sprint Learning Confirmed Plan`
  - Runtime text: `The completed sprint did not require backlog, roadmap, or Story reconciliation.`
- Do not render `Backlog Review` as the sole default destination for `SPRINT_COMPLETE`.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run --frozen python -m pytest tests/test_agent_workbench_read_projection.py tests/test_api_sprint_flow.py -q -k "post_sprint_triage"
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add services/agent_workbench/read_projection.py api.py frontend/project.js tests/test_agent_workbench_read_projection.py tests/test_api_sprint_flow.py
git commit -m "feat(ui): project post-sprint triage status"
```

## Task 8: End-To-End Regression And Cleanup

**Files:**
- Modify tests only if failures reveal missing coverage:
  - `tests/test_agent_workbench_application.py`
  - `tests/test_agent_workbench_sprint_phase.py`
  - `tests/test_agent_workbench_cli.py`
  - `tests/test_api_sprint_flow.py`

- [ ] **Step 1: Run targeted regression suite**

Run:

```bash
uv run --frozen python -m pytest \
  tests/test_post_sprint_triage.py \
  tests/test_agent_workbench_sprint_phase.py \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_command_schema.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_read_projection.py \
  tests/test_api_sprint_flow.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run full quality gate**

Run:

```bash
pyrepo-check --all
```

Expected: PASS with no failures. Existing third-party Pydantic warnings may remain.

- [ ] **Step 3: Inspect final diff**

Run:

```bash
git status --short
git diff --check
git log --oneline --max-count=8
```

Expected:

- `git diff --check` prints nothing and exits 0.
- The branch contains one commit per completed task.
- No untracked implementation artifacts remain.

- [ ] **Step 4: Commit any final cleanup**

If Step 1 or Step 2 required small fixes, stage only the files printed by
`git status --short` and commit them with:

```bash
git commit -m "test(sprint): cover post-sprint triage regressions"
```

## Self-Review

- Spec coverage:
  - Durable triage payload and fingerprints: Task 1.
  - `sprint review` and `sprint triage`: Tasks 2 and 3.
  - Missing-triage `workflow next` gate: Task 4.
  - Per-impact bridge postconditions: Tasks 5 and 6.
  - Stale-guard-aware routing: Task 6.
  - API/read projection/dashboard semantics: Task 7.
  - Runnable advertised commands and full regression: Task 8.
- Placeholder scan:
  - The plan intentionally uses CLI template placeholders only where the product itself advertises command templates, such as `<impact>` and `<idempotency_key>`.
  - Implementation steps name concrete files, concrete tests, and concrete commands.
- Type consistency:
  - Triage payload fields match the accepted spec: `affected_task_ids`, `affected_story_ids`, `affected_backlog_item_ids`, `affected_roadmap_item_ids`, and `affected_layers`.
  - Error/status names match the accepted spec: `TRIAGE_ALREADY_RECORDED`, `TRIAGE_FINGERPRINT_MISMATCH`, `TRIAGE_EXPECTED_STATE_MISMATCH`, `TRIAGE_IMPACT_FIELDS_INVALID`, and `BACKLOG_SOURCE_UNAVAILABLE`.
