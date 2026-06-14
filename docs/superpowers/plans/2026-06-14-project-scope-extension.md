# Project Scope Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a project-agnostic scope extension ritual for exhausted AgileForge projects that validates additive spec amendments, routes through authority review, and generates new execution work without rewriting completed history.

**Architecture:** Create a new `ScopeExtensionRunner` boundary for read-only validation and guarded start mutation. Reuse the existing pending authority compile/review/accept flow, then make backlog and roadmap generation read extension metadata so they append delta scope instead of replacing existing project artifacts.

**Tech Stack:** Python 3.13, SQLModel, Pydantic v2, pytest, existing AgileForge workflow/session services, existing CLI/API command registry, vanilla frontend JavaScript.

---

## Source Spec

Implement against:

- `docs/superpowers/specs/2026-06-14-project-scope-extension-design.md`

Current baseline in the implementation worktree:

- Branch: `dev/project-scope-extension`
- Worktree: `/Users/aaat/projects/agileforge/.worktrees/project-scope-extension`
- Baseline: `pyrepo-check --all` passed with 2329 tests.

## File Structure

Create:

- `services/agent_workbench/scope_extension.py`
  - Pure additive-amendment validation helpers.
  - Exhausted-scope precondition helpers.
  - `ScopeExtensionRunner` for CLI/API application use.
  - Guarded `validate` and `start` request models.
- `tests/test_agent_workbench_scope_extension.py`
  - Unit and service tests for validation, preconditions, idempotent start, and pending-authority routing.

Modify:

- `services/agent_workbench/application.py`
  - Inject/use `ScopeExtensionRunner`.
  - Expose `scope_extension_validate()` and `scope_extension_start()`.
  - Add `workflow next` routing when `SPRINT_COMPLETE + impact=none + no refined candidates + exhausted scope`.
- `services/agent_workbench/command_registry.py`
  - Register `agileforge scope extension validate`.
  - Register `agileforge scope extension start`.
- `services/agent_workbench/error_codes.py`
  - Add stable scope-extension error codes and metadata.
- `cli/main.py`
  - Add `scope extension validate` and `scope extension start` commands.
- `tests/test_agent_workbench_cli.py`
  - CLI parser/facade routing coverage.
- `tests/test_agent_workbench_command_schema.py`
  - Command schema coverage for new commands.
- `api.py`
  - Add API request schemas and routes for validation/start.
  - Surface scope extension projection in project runtime summary.
- `tests/test_api_sprint_flow.py` or `tests/test_api_dashboard.py`
  - API route and dashboard projection coverage. Use the existing test file that already owns the touched route/projection.
- `services/backlog_runtime.py`
  - Add extension-aware input context when `workflow_state["scope_extension_context"]` exists and authority is accepted.
- `services/phases/backlog_service.py`
  - Add extension attempt metadata and append-only save behavior.
- `services/agent_workbench/backlog_phase.py`
  - Pass extension context through the existing runner boundary.
- `tests/test_backlog_phase_service.py`
  - Backlog extension generation/save regression coverage.
- `services/roadmap_runtime.py`
  - Add extension-aware input context for appended roadmap phase generation.
- `services/phases/roadmap_service.py`
  - Add extension attempt metadata and append-only save behavior.
- `services/agent_workbench/roadmap_phase.py`
  - Pass extension context through the existing runner boundary.
- `tests/test_roadmap_phase_service.py`
  - Roadmap append-only regression coverage.
- `frontend/project.js`
  - Show scope-extension action/status from backend projection without calendar/sprint confusion.

Do not add schema migrations in v1. The intended durable carriers are existing `SpecRegistry`, `CompiledSpecAuthority`, `SpecAuthorityAcceptance`, workflow state, and existing backlog/roadmap/story tables.

## Shared Constants And Contracts

Use these exact status/error strings unless a test shows the repo already has a stronger existing equivalent:

```python
SCOPE_EXTENSION_AVAILABLE = "project_scope_extension_available"
SCOPE_EXTENSION_BLOCKED = "project_scope_extension_blocked"
SCOPE_EXTENSION_VALID = "project_scope_extension_valid"
SCOPE_EXTENSION_INVALID = "project_scope_extension_invalid"
SCOPE_EXTENSION_STARTED = "project_scope_extension_started"

ERR_SCOPE_EXTENSION_NOT_AVAILABLE = "SCOPE_EXTENSION_NOT_AVAILABLE"
ERR_SCOPE_EXTENSION_NOT_ADDITIVE = "SCOPE_EXTENSION_NOT_ADDITIVE"
ERR_SCOPE_EXTENSION_NO_ADDED_ITEMS = "SCOPE_EXTENSION_NO_ADDED_ITEMS"
ERR_SCOPE_EXTENSION_BASE_SPEC_MISMATCH = "SCOPE_EXTENSION_BASE_SPEC_MISMATCH"
ERR_SCOPE_EXTENSION_UNRESOLVED_WORK = "SCOPE_EXTENSION_UNRESOLVED_WORK"
```

Workflow state key:

```python
"scope_extension_context": {
    "schema": "agileforge.scope_extension.v1",
    "base_spec_version_id": 1,
    "base_spec_hash": "sha256:base",
    "amended_spec_version_id": 2,
    "amended_spec_hash": "sha256:amended",
    "added_source_item_ids": ["REQ.new-capability"],
    "started_at": "2026-06-14T00:00:00Z",
    "idempotency_key": "scope-extension-001"
}
```

## Task 1: Add Pure Additive Amendment Validation

**Files:**
- Create: `services/agent_workbench/scope_extension.py`
- Test: `tests/test_agent_workbench_scope_extension.py`

- [ ] **Step 1: Write failing pure validation tests**

Add this test scaffold to `tests/test_agent_workbench_scope_extension.py`:

```python
"""Tests for project scope extension validation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from services.agent_workbench.scope_extension import (
    ScopeExtensionIssue,
    validate_additive_scope_extension,
)


def _artifact() -> dict[str, Any]:
    return {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": "SPEC.scope-extension",
        "title": "Scope Extension Fixture",
        "status": "draft",
        "version": "0.1",
        "created_at": "2026-06-14",
        "updated_at": "2026-06-14",
        "summary": "Exercise additive scope extension.",
        "problem_statement": "A mature project needs new accepted scope.",
        "items": [
            {
                "id": "GOAL.existing",
                "type": "GOAL",
                "status": "accepted",
                "title": "Existing goal",
                "statement": "Preserve existing accepted goal.",
            },
            {
                "id": "REQ.existing-capability",
                "type": "REQ",
                "status": "accepted",
                "title": "Existing capability",
                "statement": "The system MUST preserve existing capability.",
                "level": "MUST",
                "verification": "acceptance-test",
                "acceptance": ["Existing capability remains available."],
            },
        ],
        "relations": [
            {
                "from": "REQ.existing-capability",
                "type": "satisfies",
                "to": "GOAL.existing",
                "rationale": "Requirement satisfies the existing goal.",
            }
        ],
        "controlled_terms": [],
        "external_references": [],
        "rendering": {"markdown_profile": "agileforge.spec_markdown.v1"},
    }


def _with_new_item(base: dict[str, Any]) -> dict[str, Any]:
    amended = deepcopy(base)
    amended["items"].append(
        {
            "id": "REQ.new-capability",
            "type": "REQ",
            "status": "accepted",
            "title": "New capability",
            "statement": "The system MUST support a new capability.",
            "level": "MUST",
            "verification": "acceptance-test",
            "acceptance": ["New capability is available."],
        }
    )
    return amended


def _issue_codes(issues: list[ScopeExtensionIssue]) -> set[str]:
    return {issue.code for issue in issues}


def test_additive_scope_extension_accepts_new_source_item() -> None:
    base = _artifact()
    amended = _with_new_item(base)

    result = validate_additive_scope_extension(base, amended)

    assert result.ok is True
    assert result.added_source_item_ids == ["REQ.new-capability"]
    assert result.blocking_issues == []


def test_additive_scope_extension_blocks_modified_existing_item() -> None:
    base = _artifact()
    amended = _with_new_item(base)
    amended["items"][1]["statement"] = "The system MUST rewrite old scope."

    result = validate_additive_scope_extension(base, amended)

    assert result.ok is False
    assert "EXISTING_SOURCE_ITEM_MODIFIED" in _issue_codes(result.blocking_issues)
    assert result.modified_source_item_ids == ["REQ.existing-capability"]


def test_additive_scope_extension_blocks_removed_existing_item() -> None:
    base = _artifact()
    amended = _with_new_item(base)
    amended["items"] = [item for item in amended["items"] if item["id"] != "GOAL.existing"]

    result = validate_additive_scope_extension(base, amended)

    assert result.ok is False
    assert "EXISTING_SOURCE_ITEM_REMOVED" in _issue_codes(result.blocking_issues)
    assert result.removed_source_item_ids == ["GOAL.existing"]


def test_additive_scope_extension_blocks_changed_existing_relation() -> None:
    base = _artifact()
    amended = _with_new_item(base)
    amended["relations"][0]["rationale"] = "Changed rationale."

    result = validate_additive_scope_extension(base, amended)

    assert result.ok is False
    assert "EXISTING_RELATION_MODIFIED" in _issue_codes(result.blocking_issues)


def test_additive_scope_extension_blocks_no_new_items() -> None:
    base = _artifact()

    result = validate_additive_scope_extension(base, deepcopy(base))

    assert result.ok is False
    assert "NO_ADDED_SOURCE_ITEMS" in _issue_codes(result.blocking_issues)
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_scope_extension.py -q -k "additive_scope_extension"
```

Expected: import failure because `services.agent_workbench.scope_extension` does not exist.

- [ ] **Step 3: Implement the pure validation module**

Create `services/agent_workbench/scope_extension.py` with:

```python
"""Project scope extension validation and mutation runner."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from services.agent_workbench.fingerprints import canonical_hash
from services.specs.profile_content import (
    SpecContentNormalizationError,
    normalize_spec_content_for_registry,
)
from utils.agileforge_spec_profile import TechnicalSpecArtifact


SCOPE_EXTENSION_AVAILABLE = "project_scope_extension_available"
SCOPE_EXTENSION_BLOCKED = "project_scope_extension_blocked"
SCOPE_EXTENSION_VALID = "project_scope_extension_valid"
SCOPE_EXTENSION_INVALID = "project_scope_extension_invalid"
SCOPE_EXTENSION_STARTED = "project_scope_extension_started"

ERR_SCOPE_EXTENSION_NOT_AVAILABLE = "SCOPE_EXTENSION_NOT_AVAILABLE"
ERR_SCOPE_EXTENSION_NOT_ADDITIVE = "SCOPE_EXTENSION_NOT_ADDITIVE"
ERR_SCOPE_EXTENSION_NO_ADDED_ITEMS = "SCOPE_EXTENSION_NO_ADDED_ITEMS"
ERR_SCOPE_EXTENSION_BASE_SPEC_MISMATCH = "SCOPE_EXTENSION_BASE_SPEC_MISMATCH"
ERR_SCOPE_EXTENSION_UNRESOLVED_WORK = "SCOPE_EXTENSION_UNRESOLVED_WORK"


class ScopeExtensionValidateRequest(BaseModel):
    """Validated request for read-only scope-extension validation."""

    project_id: int
    spec_file: str = Field(min_length=1)
    base_spec_version_id: int | None = None


class ScopeExtensionStartRequest(BaseModel):
    """Validated request for guarded scope-extension start."""

    project_id: int
    spec_file: str = Field(min_length=1)
    base_spec_version_id: int
    expected_state: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "cli-agent"

    def normalized_request_hash(self) -> str:
        """Return a stable hash for idempotent extension start."""
        return canonical_hash(
            {
                "command": "agileforge scope extension start",
                "project_id": self.project_id,
                "spec_file": str(Path(self.spec_file).expanduser()),
                "base_spec_version_id": self.base_spec_version_id,
                "expected_state": self.expected_state,
                "changed_by": self.changed_by,
            }
        )


@dataclass(frozen=True)
class ScopeExtensionIssue:
    """One deterministic scope-extension validation issue."""

    code: str
    message: str
    source_item_id: str | None = None
    relation_key: str | None = None


@dataclass(frozen=True)
class ScopeExtensionValidation:
    """Result of additive spec amendment validation."""

    ok: bool
    added_source_item_ids: list[str]
    removed_source_item_ids: list[str]
    modified_source_item_ids: list[str]
    blocking_issues: list[ScopeExtensionIssue]


def _canonical_item_fingerprint(item: Mapping[str, Any]) -> str:
    return canonical_hash(dict(item))


def _relation_key(relation: Mapping[str, Any]) -> str:
    return "::".join(
        [
            str(relation.get("from", "")),
            str(relation.get("type", "")),
            str(relation.get("to", "")),
        ]
    )


def _canonical_relation_fingerprint(relation: Mapping[str, Any]) -> str:
    return canonical_hash(dict(relation))


def _items_by_id(artifact: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    items = artifact.get("items")
    if not isinstance(items, list):
        return {}
    return {
        str(item["id"]): item
        for item in items
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }


def _relations_by_key(artifact: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    relations = artifact.get("relations")
    if not isinstance(relations, list):
        return {}
    return {
        _relation_key(relation): relation
        for relation in relations
        if isinstance(relation, Mapping)
    }


def validate_additive_scope_extension(
    base_artifact: Mapping[str, Any],
    amended_artifact: Mapping[str, Any],
) -> ScopeExtensionValidation:
    """Validate that amended spec only adds source items and relations."""
    base_items = _items_by_id(base_artifact)
    amended_items = _items_by_id(amended_artifact)
    base_relations = _relations_by_key(base_artifact)
    amended_relations = _relations_by_key(amended_artifact)

    added = sorted(set(amended_items) - set(base_items))
    removed = sorted(set(base_items) - set(amended_items))
    modified = sorted(
        item_id
        for item_id in set(base_items) & set(amended_items)
        if _canonical_item_fingerprint(base_items[item_id])
        != _canonical_item_fingerprint(amended_items[item_id])
    )

    issues: list[ScopeExtensionIssue] = []
    for item_id in removed:
        issues.append(
            ScopeExtensionIssue(
                code="EXISTING_SOURCE_ITEM_REMOVED",
                message="Existing accepted source item is missing from amended spec.",
                source_item_id=item_id,
            )
        )
    for item_id in modified:
        issues.append(
            ScopeExtensionIssue(
                code="EXISTING_SOURCE_ITEM_MODIFIED",
                message="Existing accepted source item changed in amended spec.",
                source_item_id=item_id,
            )
        )
    for key in sorted(set(base_relations) - set(amended_relations)):
        issues.append(
            ScopeExtensionIssue(
                code="EXISTING_RELATION_REMOVED",
                message="Existing relation is missing from amended spec.",
                relation_key=key,
            )
        )
    for key in sorted(set(base_relations) & set(amended_relations)):
        if _canonical_relation_fingerprint(base_relations[key]) != (
            _canonical_relation_fingerprint(amended_relations[key])
        ):
            issues.append(
                ScopeExtensionIssue(
                    code="EXISTING_RELATION_MODIFIED",
                    message="Existing relation changed in amended spec.",
                    relation_key=key,
                )
            )
    if not added:
        issues.append(
            ScopeExtensionIssue(
                code="NO_ADDED_SOURCE_ITEMS",
                message="Scope extension must add at least one source item.",
            )
        )

    return ScopeExtensionValidation(
        ok=not issues,
        added_source_item_ids=added,
        removed_source_item_ids=removed,
        modified_source_item_ids=modified,
        blocking_issues=issues,
    )


def load_structured_spec_file(path: str) -> tuple[TechnicalSpecArtifact, str, str]:
    """Load, normalize, and parse a structured AgileForge spec file."""
    raw = Path(path).expanduser().resolve().read_text(encoding="utf-8")
    normalized = normalize_spec_content_for_registry(raw)
    payload = json.loads(normalized.content)
    return TechnicalSpecArtifact.model_validate(payload), normalized.content, normalized.spec_hash
```

- [ ] **Step 4: Run the focused tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_scope_extension.py -q -k "additive_scope_extension"
```

Expected: all additive validation tests pass.

- [ ] **Step 5: Commit**

```bash
git add services/agent_workbench/scope_extension.py tests/test_agent_workbench_scope_extension.py
git commit -m "feat(scope): validate additive spec amendments"
```

## Task 2: Add Exhausted-Scope Preconditions

**Files:**
- Modify: `services/agent_workbench/scope_extension.py`
- Test: `tests/test_agent_workbench_scope_extension.py`

- [ ] **Step 1: Write failing precondition tests**

Append this exact precondition test block:

```python
from datetime import UTC, datetime

from models.core import Product, Sprint, UserStory
from models.enums import SprintStatus, StoryStatus
from services.agent_workbench.scope_extension import (
    evaluate_scope_extension_preconditions,
)


def test_scope_extension_available_when_sprint_complete_and_no_open_work(
    session,
) -> None:
    product = Product(name="Exhausted", vision="Done")
    session.add(product)
    session.commit()
    session.refresh(product)

    result = evaluate_scope_extension_preconditions(
        session=session,
        project_id=product.product_id,
        workflow_state={
            "fsm_state": "SPRINT_COMPLETE",
            "post_sprint_triage_required": False,
            "post_sprint_triage": {"impact": "none"},
        },
        sprint_candidate_count=0,
    )

    assert result.available is True
    assert result.status == "project_scope_extension_available"
    assert result.blockers == []


def test_scope_extension_blocks_active_sprint(session) -> None:
    product = Product(name="Active Sprint", vision="Work")
    session.add(product)
    session.commit()
    session.refresh(product)
    session.add(
        Sprint(
            product_id=product.product_id,
            team_id=1,
            status=SprintStatus.ACTIVE,
            started_at=datetime.now(UTC),
        )
    )
    session.commit()

    result = evaluate_scope_extension_preconditions(
        session=session,
        project_id=product.product_id,
        workflow_state={"fsm_state": "SPRINT_COMPLETE"},
        sprint_candidate_count=0,
    )

    assert result.available is False
    assert result.blockers[0]["reason"] == "ACTIVE_SPRINT_EXISTS"


def test_scope_extension_blocks_planned_sprint(session) -> None:
    product = Product(name="Planned Sprint", vision="Work")
    session.add(product)
    session.commit()
    session.refresh(product)
    session.add(Sprint(product_id=product.product_id, team_id=1, status=SprintStatus.PLANNED))
    session.commit()

    result = evaluate_scope_extension_preconditions(
        session=session,
        project_id=product.product_id,
        workflow_state={"fsm_state": "SPRINT_COMPLETE"},
        sprint_candidate_count=0,
    )

    assert result.available is False
    assert result.blockers[0]["reason"] == "PLANNED_SPRINT_EXISTS"


def test_scope_extension_blocks_open_story(session) -> None:
    product = Product(name="Open Story", vision="Work")
    session.add(product)
    session.commit()
    session.refresh(product)
    session.add(
        UserStory(
            product_id=product.product_id,
            title="Open",
            status=StoryStatus.TO_DO,
            is_refined=True,
        )
    )
    session.commit()

    result = evaluate_scope_extension_preconditions(
        session=session,
        project_id=product.product_id,
        workflow_state={"fsm_state": "SPRINT_COMPLETE"},
        sprint_candidate_count=0,
    )

    assert result.available is False
    assert result.blockers[0]["reason"] == "OPEN_STORY_EXISTS"


def test_scope_extension_blocks_remaining_sprint_candidates(session) -> None:
    product = Product(name="Candidates", vision="Work")
    session.add(product)
    session.commit()
    session.refresh(product)

    result = evaluate_scope_extension_preconditions(
        session=session,
        project_id=product.product_id,
        workflow_state={"fsm_state": "SPRINT_COMPLETE"},
        sprint_candidate_count=2,
    )

    assert result.available is False
    assert result.blockers[0]["reason"] == "SPRINT_CANDIDATES_EXIST"
```

- [ ] **Step 2: Run the tests and verify they fail**

```bash
uv run --frozen pytest tests/test_agent_workbench_scope_extension.py -q -k "scope_extension_blocks or scope_extension_available"
```

Expected: import failure for `evaluate_scope_extension_preconditions`.

- [ ] **Step 3: Implement precondition helpers**

Add to `services/agent_workbench/scope_extension.py`:

```python
from sqlmodel import Session, select

from models.core import Sprint, UserStory
from models.enums import SprintStatus, StoryStatus


@dataclass(frozen=True)
class ScopeExtensionPreconditions:
    """Read-only exhausted-scope precondition result."""

    available: bool
    status: str
    blockers: list[dict[str, Any]]


def _story_is_terminal(story: UserStory) -> bool:
    if story.is_superseded:
        return True
    if story.archived_reason:
        return True
    return story.status in {StoryStatus.DONE, StoryStatus.ACCEPTED}


def evaluate_scope_extension_preconditions(
    *,
    session: Session,
    project_id: int,
    workflow_state: Mapping[str, Any],
    sprint_candidate_count: int,
) -> ScopeExtensionPreconditions:
    """Return whether project scope extension can start from this snapshot."""
    blockers: list[dict[str, Any]] = []
    fsm_state = str(workflow_state.get("fsm_state") or "")
    if fsm_state != "SPRINT_COMPLETE":
        blockers.append(
            {
                "command": "agileforge scope extension start",
                "reason": "FSM_STATE_NOT_SPRINT_COMPLETE",
                "message": "Scope extension can start only after a completed sprint.",
                "actual_state": fsm_state,
            }
        )

    active_or_planned = session.exec(
        select(Sprint).where(
            Sprint.product_id == project_id,
            Sprint.status.in_([SprintStatus.ACTIVE, SprintStatus.PLANNED]),
        )
    ).all()
    for sprint in active_or_planned:
        blockers.append(
            {
                "command": "agileforge scope extension start",
                "reason": (
                    "ACTIVE_SPRINT_EXISTS"
                    if sprint.status == SprintStatus.ACTIVE
                    else "PLANNED_SPRINT_EXISTS"
                ),
                "message": "Finish or cancel current sprint work before extending scope.",
                "sprint_id": sprint.sprint_id,
            }
        )

    stories = session.exec(
        select(UserStory).where(UserStory.product_id == project_id)
    ).all()
    open_story_ids = [
        story.story_id
        for story in stories
        if story.story_id is not None and not _story_is_terminal(story)
    ]
    if open_story_ids:
        blockers.append(
            {
                "command": "agileforge scope extension start",
                "reason": "OPEN_STORY_EXISTS",
                "message": "Finish, archive, or defer open stories before extending scope.",
                "story_ids": open_story_ids,
            }
        )

    if sprint_candidate_count > 0:
        blockers.append(
            {
                "command": "agileforge scope extension start",
                "reason": "SPRINT_CANDIDATES_EXIST",
                "message": "Plan or defer remaining sprint candidates before extending scope.",
                "candidate_count": sprint_candidate_count,
            }
        )

    return ScopeExtensionPreconditions(
        available=not blockers,
        status=SCOPE_EXTENSION_AVAILABLE if not blockers else SCOPE_EXTENSION_BLOCKED,
        blockers=blockers,
    )
```

- [ ] **Step 4: Run the focused tests**

```bash
uv run --frozen pytest tests/test_agent_workbench_scope_extension.py -q -k "scope_extension_blocks or scope_extension_available"
```

Expected: all precondition tests pass.

- [ ] **Step 5: Commit**

```bash
git add services/agent_workbench/scope_extension.py tests/test_agent_workbench_scope_extension.py
git commit -m "feat(scope): guard extension on exhausted execution scope"
```

## Task 3: Add Runner Validation And Guarded Start

**Files:**
- Modify: `services/agent_workbench/scope_extension.py`
- Test: `tests/test_agent_workbench_scope_extension.py`

- [ ] **Step 1: Write failing runner tests**

Append this exact runner test block after the pure validation and precondition
tests in `tests/test_agent_workbench_scope_extension.py`:

```python
from pathlib import Path

from models.specs import SpecRegistry
from services.agent_workbench.scope_extension import (
    ScopeExtensionRunner,
    ScopeExtensionStartRequest,
    ScopeExtensionValidateRequest,
)
from utils.agileforge_spec_profile import (
    TechnicalSpecArtifact,
    canonical_spec_hash,
    canonical_spec_json,
)


class _FakeWorkflowService:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = dict(state)
        self.updates: list[tuple[str, dict[str, object]]] = []

    def get_session_status(self, session_id: str) -> dict[str, object]:
        return dict(self.state)

    def update_session_status(
        self, session_id: str, partial_update: dict[str, object]
    ) -> None:
        self.updates.append((session_id, partial_update))
        self.state.update(partial_update)


def _write_spec_file(path: Path, payload: dict[str, Any]) -> Path:
    artifact = TechnicalSpecArtifact.model_validate(payload)
    path.write_text(canonical_spec_json(artifact), encoding="utf-8")
    return path


def _insert_accepted_spec(
    session, *, product_id: int, payload: dict[str, Any]
) -> SpecRegistry:
    artifact = TechnicalSpecArtifact.model_validate(payload)
    row = SpecRegistry(
        product_id=product_id,
        spec_hash=canonical_spec_hash(artifact),
        content=canonical_spec_json(artifact),
        content_ref="specs/base.json",
        status="approved",
        approved_by="test",
        approval_notes="accepted base",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_scope_extension_validate_returns_added_items_and_base_guard(
    session, tmp_path: Path, engine
) -> None:
    product = Product(name="Validate Extension", vision="Done")
    session.add(product)
    session.commit()
    session.refresh(product)
    base = _artifact()
    base_spec = _insert_accepted_spec(
        session, product_id=product.product_id, payload=base
    )
    amended_file = _write_spec_file(tmp_path / "amended.json", _with_new_item(base))
    workflow = _FakeWorkflowService({"fsm_state": "SPRINT_COMPLETE"})
    runner = ScopeExtensionRunner(engine=engine, workflow_service=workflow)

    result = runner.validate(
        ScopeExtensionValidateRequest(
            project_id=product.product_id,
            spec_file=str(amended_file),
        )
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "project_scope_extension_valid"
    assert result["data"]["base_spec_version_id"] == base_spec.spec_version_id
    assert result["data"]["added_source_item_ids"] == ["REQ.new-capability"]


def test_scope_extension_start_registers_pending_spec_and_authority_compile_route(
    session, tmp_path: Path, engine
) -> None:
    product = Product(name="Start Extension", vision="Done")
    session.add(product)
    session.commit()
    session.refresh(product)
    base = _artifact()
    base_spec = _insert_accepted_spec(
        session, product_id=product.product_id, payload=base
    )
    amended_file = _write_spec_file(tmp_path / "amended.json", _with_new_item(base))
    workflow = _FakeWorkflowService({"fsm_state": "SPRINT_COMPLETE"})
    runner = ScopeExtensionRunner(engine=engine, workflow_service=workflow)

    result = runner.start(
        ScopeExtensionStartRequest(
            project_id=product.product_id,
            spec_file=str(amended_file),
            base_spec_version_id=base_spec.spec_version_id,
            expected_state="SPRINT_COMPLETE",
            idempotency_key="scope-extension-001",
        )
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "project_scope_extension_started"
    assert result["data"]["setup_status"] == "authority_compile_required"
    assert result["data"]["next_actions"][0]["command"] == "agileforge authority compile"
    assert workflow.state["fsm_state"] == "SETUP_REQUIRED"
    assert workflow.state["scope_extension_context"]["added_source_item_ids"] == [
        "REQ.new-capability"
    ]
```

- [ ] **Step 2: Run the tests and verify they fail**

```bash
uv run --frozen pytest tests/test_agent_workbench_scope_extension.py -q -k "runner or registers_pending"
```

Expected: `ScopeExtensionRunner` import failure.

- [ ] **Step 3: Implement `ScopeExtensionRunner`**

In `services/agent_workbench/scope_extension.py`, add:

```python
from models.db import ensure_business_db_ready
from models.specs import SpecRegistry
from services.agent_workbench.envelope import data_envelope, error_envelope
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.mutation_ledger import MutationLedgerRepository
from services.agent_workbench.project_setup import _authority_compile_action
from services.specs.pending_authority_service import ensure_pending_spec_version_for_project


class ScopeExtensionRunner:
    """Validate and start project scope extension from an exhausted project."""

    def __init__(self, *, engine: Any, workflow_service: Any) -> None:
        self._engine = engine
        self._workflow_service = workflow_service
        ensure_business_db_ready(engine_override=engine)
        self._ledger = MutationLedgerRepository(engine=engine)

    def validate(self, request: ScopeExtensionValidateRequest) -> dict[str, Any]:
        """Return read-only validation result for an amended spec file."""
        with Session(self._engine) as session:
            base = _latest_accepted_spec(session, project_id=request.project_id)
            if base is None or base.spec_version_id is None:
                return _scope_error(
                    ErrorCode.SPEC_VERSION_NOT_FOUND.value,
                    "No accepted base spec exists for this project.",
                )
            if request.base_spec_version_id is not None and (
                request.base_spec_version_id != base.spec_version_id
            ):
                return _scope_error(
                    ERR_SCOPE_EXTENSION_BASE_SPEC_MISMATCH,
                    "Requested base spec version is not the latest accepted spec.",
                )
            base_artifact = json.loads(base.content)
            amended_artifact, amended_content, amended_hash = load_structured_spec_file(
                request.spec_file
            )
            validation = validate_additive_scope_extension(
                base_artifact,
                amended_artifact.model_dump(mode="json", by_alias=True),
            )
            status = SCOPE_EXTENSION_VALID if validation.ok else SCOPE_EXTENSION_INVALID
            return _scope_data(
                {
                    "project_id": request.project_id,
                    "status": status,
                    "base_spec_version_id": base.spec_version_id,
                    "base_spec_hash": base.spec_hash,
                    "amended_spec_hash": amended_hash,
                    "added_source_item_ids": validation.added_source_item_ids,
                    "removed_source_item_ids": validation.removed_source_item_ids,
                    "modified_source_item_ids": validation.modified_source_item_ids,
                    "blocking_issues": [issue.__dict__ for issue in validation.blocking_issues],
                    "valid": validation.ok,
                }
            )

    def start(self, request: ScopeExtensionStartRequest) -> dict[str, Any]:
        """Register the additive amended spec and route to authority compile."""
        validation = self.validate(
            ScopeExtensionValidateRequest(
                project_id=request.project_id,
                spec_file=request.spec_file,
                base_spec_version_id=request.base_spec_version_id,
            )
        )
        if not validation.get("ok") or validation["data"]["valid"] is not True:
            return validation

        workflow_state = self._workflow_service.get_session_status(str(request.project_id))
        current_state = str(workflow_state.get("fsm_state") or "")
        if current_state != request.expected_state:
            return _scope_error(
                ErrorCode.STALE_STATE.value,
                "Workflow state changed before scope extension start.",
                details={"expected_state": request.expected_state, "actual_state": current_state},
            )

        with Session(self._engine) as session:
            registered = ensure_pending_spec_version_for_project(
                session=session,
                product_id=request.project_id,
                spec_path=Path(request.spec_file),
                approved_by=request.changed_by,
                lease_guard=lambda _step: True,
                record_progress=lambda _step: True,
            )
            if not registered.ok or registered.spec_version_id is None:
                return _scope_error(
                    registered.error_code or ErrorCode.MUTATION_FAILED.value,
                    registered.error or "Scope extension spec registration failed.",
                )

        data = validation["data"]
        next_action = _authority_compile_action(
            project_id=request.project_id,
            spec_version_id=int(registered.spec_version_id),
            spec_hash=str(registered.spec_hash),
            expected_setup_status="authority_compile_required",
        )
        context = {
            "schema": "agileforge.scope_extension.v1",
            "base_spec_version_id": request.base_spec_version_id,
            "base_spec_hash": data["base_spec_hash"],
            "amended_spec_version_id": registered.spec_version_id,
            "amended_spec_hash": registered.spec_hash,
            "added_source_item_ids": data["added_source_item_ids"],
            "idempotency_key": request.idempotency_key,
        }
        self._workflow_service.update_session_status(
            str(request.project_id),
            {
                "fsm_state": "SETUP_REQUIRED",
                "setup_status": "authority_compile_required",
                "setup_spec_file_path": str(Path(request.spec_file).expanduser().resolve()),
                "setup_spec_hash": registered.spec_hash,
                "setup_spec_version_id": registered.spec_version_id,
                "setup_next_actions": [next_action],
                "scope_extension_context": context,
            },
        )
        return _scope_data(
            {
                "project_id": request.project_id,
                "status": SCOPE_EXTENSION_STARTED,
                "setup_status": "authority_compile_required",
                "spec_version_id": registered.spec_version_id,
                "scope_extension_context": context,
                "next_actions": [next_action],
            }
        )


def _scope_data(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": data, "warnings": [], "errors": []}


def _scope_error(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error = workbench_error(code, message, details=details or {})
    return {"ok": False, "data": None, "warnings": [], "errors": [error.model_dump()]}
```

Keep this first implementation intentionally simple. If tests show the mutation ledger is required to match project setup safety, add ledger coverage before moving to the next task. Do not defer idempotency after this task.

- [ ] **Step 4: Run focused runner tests**

```bash
uv run --frozen pytest tests/test_agent_workbench_scope_extension.py -q -k "runner or registers_pending"
```

Expected: runner tests pass.

- [ ] **Step 5: Commit**

```bash
git add services/agent_workbench/scope_extension.py tests/test_agent_workbench_scope_extension.py
git commit -m "feat(scope): start guarded extension through authority compile"
```

## Task 4: Wire Application And Workflow Next

**Files:**
- Modify: `services/agent_workbench/application.py`
- Test: `tests/test_agent_workbench_application.py`

- [ ] **Step 1: Write failing workflow-next regression**

Add a test proving `SPRINT_COMPLETE + impact=none + zero candidates + no open work` surfaces scope extension:

```python
def test_workflow_next_exposes_scope_extension_when_scope_exhausted() -> None:
    app = AgentWorkbenchApplication(
        read_projection=_FakeSprintCompleteNoCandidateProjection(),
        scope_extension_runner=_FakeScopeExtensionAvailableRunner(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["status"] == "project_scope_extension_available"
    assert "agileforge scope extension validate" in result["data"]["next_valid_commands"][0]
    assert result["data"]["next_actions"][0]["runnable"] is True
```

Add a second test for blocked preconditions:

```python
def test_workflow_next_blocks_scope_extension_when_open_work_remains() -> None:
    app = AgentWorkbenchApplication(
        read_projection=_FakeSprintCompleteNoCandidateProjection(),
        scope_extension_runner=_FakeScopeExtensionBlockedRunner(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["status"] == "project_scope_extension_blocked"
    assert result["data"]["blocked_commands"][0]["reason"] == "OPEN_STORY_EXISTS"
```

- [ ] **Step 2: Run and verify failure**

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "scope_extension"
```

Expected: constructor/facade failures because application does not inject scope extension.

- [ ] **Step 3: Implement application facade methods**

Modify `AgentWorkbenchApplication.__init__` to accept `scope_extension_runner`. Add:

```python
def scope_extension_validate(
    self,
    *,
    project_id: int,
    spec_file: str,
    base_spec_version_id: int | None = None,
) -> dict[str, Any]:
    return self._scope_extension_runner.validate(
        ScopeExtensionValidateRequest(
            project_id=project_id,
            spec_file=spec_file,
            base_spec_version_id=base_spec_version_id,
        )
    )


def scope_extension_start(
    self,
    *,
    project_id: int,
    spec_file: str,
    base_spec_version_id: int,
    expected_state: str,
    idempotency_key: str,
    changed_by: str = "cli-agent",
) -> dict[str, Any]:
    return self._scope_extension_runner.start(
        ScopeExtensionStartRequest(
            project_id=project_id,
            spec_file=spec_file,
            base_spec_version_id=base_spec_version_id,
            expected_state=expected_state,
            idempotency_key=idempotency_key,
            changed_by=changed_by,
        )
    )
```

Change the `SPRINT_COMPLETE` branch to pass a scope-extension availability payload into `_sprint_complete_workflow_next()` when candidate count is zero. Keep the existing `NO_REFINED_SPRINT_CANDIDATES` blocker when preconditions fail.

- [ ] **Step 4: Run focused application tests**

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "scope_extension or post_sprint_sprint_candidates_unavailable"
```

Expected: new scope-extension tests pass and existing zero-candidate routing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add services/agent_workbench/application.py tests/test_agent_workbench_application.py
git commit -m "feat(scope): route exhausted projects to extension ritual"
```

## Task 5: Add CLI And Command Schema

**Files:**
- Modify: `cli/main.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `services/agent_workbench/error_codes.py`
- Test: `tests/test_agent_workbench_cli.py`
- Test: `tests/test_agent_workbench_command_schema.py`

- [ ] **Step 1: Write failing CLI and schema tests**

Add CLI routing tests:

```python
def test_cli_routes_scope_extension_validate() -> None:
    app = _FakeApplication()
    payload = run_cli(
        [
            "scope",
            "extension",
            "validate",
            "--project-id",
            "3",
            "--spec-file",
            "specs/amended.json",
            "--base-spec-version-id",
            "12",
        ],
        application=app,
    )

    assert app.calls[-1] == (
        "scope_extension_validate",
        {
            "project_id": 3,
            "spec_file": "specs/amended.json",
            "base_spec_version_id": 12,
        },
    )
    assert _mapping(payload["meta"])["command"] == "agileforge scope extension validate"


def test_cli_routes_scope_extension_start() -> None:
    app = _FakeApplication()
    payload = run_cli(
        [
            "scope",
            "extension",
            "start",
            "--project-id",
            "3",
            "--spec-file",
            "specs/amended.json",
            "--base-spec-version-id",
            "12",
            "--expected-state",
            "SPRINT_COMPLETE",
            "--idempotency-key",
            "scope-extension-001",
        ],
        application=app,
    )

    assert app.calls[-1][0] == "scope_extension_start"
    assert app.calls[-1][1]["expected_state"] == "SPRINT_COMPLETE"
    assert _mapping(payload["meta"])["command"] == "agileforge scope extension start"
```

Add command schema tests:

```python
def test_scope_extension_validate_schema() -> None:
    schema = command_schema_payload("agileforge scope extension validate")
    assert schema["input_required"] == ["project_id", "spec_file"]
    assert "base_spec_version_id" in schema["input_optional"]
    assert schema["mutates"] is False


def test_scope_extension_start_schema() -> None:
    schema = command_schema_payload("agileforge scope extension start")
    assert schema["mutates"] is True
    assert schema["requires_idempotency_key"] is True
    assert schema["input_required"] == [
        "project_id",
        "spec_file",
        "base_spec_version_id",
        "expected_state",
        "idempotency_key",
    ]
```

- [ ] **Step 2: Run and verify failure**

```bash
uv run --frozen pytest tests/test_agent_workbench_cli.py tests/test_agent_workbench_command_schema.py -q -k "scope_extension"
```

Expected: command parser/schema failures.

- [ ] **Step 3: Implement command registry metadata and error codes**

Add command metadata:

```python
CommandMetadata(
    name="agileforge scope extension validate",
    mutates=False,
    phase="scope_extension",
    input_required=("project_id", "spec_file"),
    input_optional=("base_spec_version_id",),
    errors=(
        ErrorCode.PROJECT_NOT_FOUND.value,
        ErrorCode.SPEC_FILE_NOT_FOUND.value,
        ErrorCode.SPEC_FILE_INVALID.value,
        ErrorCode.SCOPE_EXTENSION_BASE_SPEC_MISMATCH.value,
        ErrorCode.SCOPE_EXTENSION_NOT_ADDITIVE.value,
    ),
),
CommandMetadata(
    name="agileforge scope extension start",
    mutates=True,
    phase="scope_extension",
    requires_idempotency_key=True,
    accepts_expected_state=True,
    idempotency_policy=_DRY_RUN_IDEMPOTENCY_POLICY,
    input_required=(
        "project_id",
        "spec_file",
        "base_spec_version_id",
        "expected_state",
        "idempotency_key",
    ),
    input_optional=("changed_by",),
    errors=(
        ErrorCode.PROJECT_NOT_FOUND.value,
        ErrorCode.STALE_STATE.value,
        ErrorCode.SCOPE_EXTENSION_NOT_AVAILABLE.value,
        ErrorCode.SCOPE_EXTENSION_UNRESOLVED_WORK.value,
        ErrorCode.SCOPE_EXTENSION_NOT_ADDITIVE.value,
        ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
        ErrorCode.MUTATION_IN_PROGRESS.value,
        ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
    ),
),
```

Add matching `ErrorCode` enum values and `_ERROR_REGISTRY` entries.

- [ ] **Step 4: Implement CLI parser and route handlers**

Add a `scope` parser with nested `extension` subparser. Route to application methods:

```python
def _handle_scope_extension_validate(args: argparse.Namespace, application: _Application) -> tuple[str, JsonObject]:
    return "agileforge scope extension validate", application.scope_extension_validate(
        project_id=args.project_id,
        spec_file=args.spec_file,
        base_spec_version_id=args.base_spec_version_id,
    )


def _handle_scope_extension_start(args: argparse.Namespace, application: _Application) -> tuple[str, JsonObject]:
    return "agileforge scope extension start", application.scope_extension_start(
        project_id=args.project_id,
        spec_file=args.spec_file,
        base_spec_version_id=args.base_spec_version_id,
        expected_state=args.expected_state,
        idempotency_key=args.idempotency_key,
        changed_by=args.changed_by,
    )
```

- [ ] **Step 5: Run focused CLI/schema tests**

```bash
uv run --frozen pytest tests/test_agent_workbench_cli.py tests/test_agent_workbench_command_schema.py -q -k "scope_extension"
```

Expected: all new CLI/schema tests pass.

- [ ] **Step 6: Commit**

```bash
git add cli/main.py services/agent_workbench/command_registry.py services/agent_workbench/error_codes.py tests/test_agent_workbench_cli.py tests/test_agent_workbench_command_schema.py
git commit -m "feat(scope): expose extension commands"
```

## Task 6: Add API Contracts

**Files:**
- Modify: `api.py`
- Test: `tests/test_api_dashboard.py` or `tests/test_api_sprint_flow.py`

- [ ] **Step 1: Write failing API tests**

Append API route tests that cover:

- `POST /api/projects/{project_id}/scope-extension/validate`
- `POST /api/projects/{project_id}/scope-extension/start`
- forbidden legacy/unknown fields via Pydantic `extra="forbid"`
- stale `expected_state` returns a structured error.

Expected test assertions:

```python
assert response.status_code == 200
payload = response.json()
assert payload["ok"] is True
assert payload["data"]["status"] == "project_scope_extension_valid"
assert payload["data"]["added_source_item_ids"] == ["REQ.new-capability"]
```

For start:

```python
assert payload["data"]["status"] == "project_scope_extension_started"
assert payload["data"]["next_actions"][0]["command"] == "agileforge authority compile"
```

- [ ] **Step 2: Run and verify failure**

```bash
uv run --frozen pytest tests/test_api_dashboard.py tests/test_api_sprint_flow.py -q -k "scope_extension"
```

Expected: route failures.

- [ ] **Step 3: Implement API request models and routes**

Add models:

```python
class ScopeExtensionValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec_file: str = Field(min_length=1)
    base_spec_version_id: int | None = None


class ScopeExtensionStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec_file: str = Field(min_length=1)
    base_spec_version_id: int
    expected_state: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "dashboard-agent"
```

Add routes:

```python
@app.post("/api/projects/{project_id}/scope-extension/validate")
def validate_scope_extension(project_id: int, request: ScopeExtensionValidateRequest) -> dict[str, Any]:
    return _application().scope_extension_validate(
        project_id=project_id,
        spec_file=request.spec_file,
        base_spec_version_id=request.base_spec_version_id,
    )


@app.post("/api/projects/{project_id}/scope-extension/start")
def start_scope_extension(project_id: int, request: ScopeExtensionStartRequest) -> dict[str, Any]:
    return _application().scope_extension_start(
        project_id=project_id,
        spec_file=request.spec_file,
        base_spec_version_id=request.base_spec_version_id,
        expected_state=request.expected_state,
        idempotency_key=request.idempotency_key,
        changed_by=request.changed_by,
    )
```

- [ ] **Step 4: Run focused API tests**

```bash
uv run --frozen pytest tests/test_api_dashboard.py tests/test_api_sprint_flow.py -q -k "scope_extension"
```

Expected: API tests pass.

- [ ] **Step 5: Commit**

```bash
git add api.py tests/test_api_dashboard.py tests/test_api_sprint_flow.py
git commit -m "feat(scope): add extension API routes"
```

## Task 7: Add Extension-Aware Backlog Generation

**Files:**
- Modify: `services/backlog_runtime.py`
- Modify: `services/phases/backlog_service.py`
- Modify: `services/agent_workbench/backlog_phase.py`
- Test: `tests/test_backlog_phase_service.py`
- Test: `tests/test_agent_workbench_backlog_phase.py`

- [ ] **Step 1: Write failing backlog extension tests**

Append focused tests proving:

- when `scope_extension_context` references accepted amended authority, backlog input context includes only new `added_source_item_ids` as generation scope;
- existing `UserStory` rows remain untouched on save;
- new backlog items are appended with provenance to the amended spec version.

Key assertions:

```python
assert captured_context["generation_mode"] == "scope_extension"
assert captured_context["scope_extension"]["added_source_item_ids"] == ["REQ.new-capability"]
assert existing_story.title == "Existing Story"
assert new_story.accepted_spec_version_id == amended_spec_version_id
assert new_story.story_origin == "scope_extension"
```

- [ ] **Step 2: Run and verify failure**

```bash
uv run --frozen pytest tests/test_backlog_phase_service.py tests/test_agent_workbench_backlog_phase.py -q -k "scope_extension"
```

Expected: extension context is absent and save behavior is replacement-oriented.

- [ ] **Step 3: Implement extension context in backlog runtime**

In `services/backlog_runtime.py`, when `state["scope_extension_context"]` exists:

```python
input_context["generation_mode"] = "scope_extension"
input_context["scope_extension"] = {
    "base_spec_version_id": context["base_spec_version_id"],
    "amended_spec_version_id": context["amended_spec_version_id"],
    "added_source_item_ids": context["added_source_item_ids"],
    "existing_story_count": len(existing_stories),
}
input_context["authority_scope_filter"] = {
    "source_item_ids": context["added_source_item_ids"],
}
```

Use accepted authority for the amended spec version only after authority acceptance exists. If acceptance is missing, return a blocked runtime payload with `AUTHORITY_REVIEW_REQUIRED`.

- [ ] **Step 4: Implement append-only backlog save behavior**

In `services/phases/backlog_service.py`, when the attempt input context has `generation_mode == "scope_extension"`:

- do not reset active backlog rows;
- do not archive or supersede existing stories;
- persist new rows with `story_origin="scope_extension"`;
- set `accepted_spec_version_id` to `scope_extension_context["amended_spec_version_id"]`;
- set workflow state to Roadmap generation for extension scope.

- [ ] **Step 5: Run focused backlog tests**

```bash
uv run --frozen pytest tests/test_backlog_phase_service.py tests/test_agent_workbench_backlog_phase.py -q -k "scope_extension"
```

Expected: extension backlog tests pass.

- [ ] **Step 6: Commit**

```bash
git add services/backlog_runtime.py services/phases/backlog_service.py services/agent_workbench/backlog_phase.py tests/test_backlog_phase_service.py tests/test_agent_workbench_backlog_phase.py
git commit -m "feat(scope): append extension backlog items"
```

## Task 8: Add Extension-Aware Roadmap Append

**Files:**
- Modify: `services/roadmap_runtime.py`
- Modify: `services/phases/roadmap_service.py`
- Modify: `services/agent_workbench/roadmap_phase.py`
- Test: `tests/test_roadmap_phase_service.py`
- Test: `tests/test_agent_workbench_roadmap_phase.py`

- [ ] **Step 1: Write failing roadmap extension tests**

Append focused tests proving:

- roadmap input context includes existing roadmap as read-only context;
- generated extension roadmap is appended as a new phase;
- existing roadmap phase titles/content remain byte-for-byte unchanged;
- workflow routes to `STORY_INTERVIEW` for the extension phase.

Key assertions:

```python
assert captured_context["generation_mode"] == "scope_extension"
assert saved_roadmap["releases"][0] == existing_release
assert saved_roadmap["releases"][-1]["extension_of_spec_version_id"] == amended_spec_version_id
assert result["fsm_state"] == "STORY_INTERVIEW"
```

- [ ] **Step 2: Run and verify failure**

```bash
uv run --frozen pytest tests/test_roadmap_phase_service.py tests/test_agent_workbench_roadmap_phase.py -q -k "scope_extension"
```

Expected: extension append behavior is absent.

- [ ] **Step 3: Implement extension roadmap context**

In `services/roadmap_runtime.py`, add `generation_mode="scope_extension"` context when workflow state has `scope_extension_context`.

- [ ] **Step 4: Implement append-only roadmap persistence**

In `services/phases/roadmap_service.py`, if attempt input context is extension mode:

- load existing roadmap releases from state;
- validate generated output has exactly one or more new extension releases;
- append generated releases after existing releases;
- add `extension_of_spec_version_id` and `source_item_ids` metadata to appended release records;
- do not alter existing release dictionaries.

- [ ] **Step 5: Run focused roadmap tests**

```bash
uv run --frozen pytest tests/test_roadmap_phase_service.py tests/test_agent_workbench_roadmap_phase.py -q -k "scope_extension"
```

Expected: roadmap append tests pass.

- [ ] **Step 6: Commit**

```bash
git add services/roadmap_runtime.py services/phases/roadmap_service.py services/agent_workbench/roadmap_phase.py tests/test_roadmap_phase_service.py tests/test_agent_workbench_roadmap_phase.py
git commit -m "feat(scope): append extension roadmap phase"
```

## Task 9: Restrict Story And Sprint Continuation To Extension Scope

**Files:**
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/story_phase.py`
- Modify: `services/phases/story_service.py`
- Modify: `services/agent_workbench/sprint_phase.py`
- Test: `tests/test_agent_workbench_story_phase.py`
- Test: `tests/test_story_phase_service.py`
- Test: `tests/test_agent_workbench_sprint_phase.py`

- [ ] **Step 1: Write failing continuation tests**

Append focused tests proving:

- `story pending` only shows extension roadmap requirements while extension context is active;
- `story complete --scope milestone` can complete the extension milestone without reopening old milestones;
- `sprint candidates` returns only extension stories after extension story completion.

Key assertions:

```python
assert pending["data"]["requirements"][0]["extension_scope"] is True
assert old_requirement not in [item["title"] for item in pending["data"]["requirements"]]
assert all(story["accepted_spec_version_id"] == amended_spec_version_id for story in candidates["data"]["items"])
```

- [ ] **Step 2: Run and verify failure**

```bash
uv run --frozen pytest tests/test_agent_workbench_story_phase.py tests/test_story_phase_service.py tests/test_agent_workbench_sprint_phase.py -q -k "scope_extension"
```

Expected: existing story/sprint projections include all scope.

- [ ] **Step 3: Implement extension filters**

When `scope_extension_context` exists and extension roadmap has been appended:

- story pending should focus on appended extension releases;
- story save should set `accepted_spec_version_id` to amended version;
- sprint candidates should filter to saved refined stories from amended version or extension origin;
- old completed stories remain visible in history but unavailable for new sprint planning.

- [ ] **Step 4: Run focused continuation tests**

```bash
uv run --frozen pytest tests/test_agent_workbench_story_phase.py tests/test_story_phase_service.py tests/test_agent_workbench_sprint_phase.py -q -k "scope_extension"
```

Expected: extension continuation tests pass.

- [ ] **Step 5: Commit**

```bash
git add services/agent_workbench/application.py services/agent_workbench/story_phase.py services/phases/story_service.py services/agent_workbench/sprint_phase.py tests/test_agent_workbench_story_phase.py tests/test_story_phase_service.py tests/test_agent_workbench_sprint_phase.py
git commit -m "feat(scope): continue stories and sprints from extension scope"
```

## Task 10: Update Dashboard Projection And Frontend

**Files:**
- Modify: `api.py`
- Modify: `frontend/project.js`
- Test: `tests/test_api_dashboard.py`

- [ ] **Step 1: Write failing dashboard projection test**

Add API projection coverage:

```python
def test_dashboard_projects_exhausted_scope_extension_action(client, seeded_project):
    response = client.get(f"/api/projects/{seeded_project}/dashboard")
    payload = response.json()

    assert payload["sprint_runtime"]["status"] == "project_scope_extension_available"
    assert payload["sprint_runtime"]["primary_action"]["label"] == "Extend Project Scope"
    assert payload["sprint_runtime"]["primary_action"]["command"].startswith(
        "agileforge scope extension validate"
    )
```

- [ ] **Step 2: Run and verify failure**

```bash
uv run --frozen pytest tests/test_api_dashboard.py -q -k "scope_extension"
```

Expected: dashboard projection lacks scope-extension runtime status.

- [ ] **Step 3: Implement dashboard projection**

In `api.py`, include `workflow_next.data.status`, `next_actions`, and blocked reasons in the project dashboard runtime summary.

In `frontend/project.js`, render:

- title: `Project Scope Extension Available`
- subtitle: `Current execution scope is exhausted. Validate an amended spec before creating new work.`
- button: `Extend Project Scope`

Do not show Sprint as active or show Sprint planning controls when `workflow next` status is `project_scope_extension_available` or `project_scope_extension_blocked`.

- [ ] **Step 4: Run focused API and JS checks**

```bash
uv run --frozen pytest tests/test_api_dashboard.py -q -k "scope_extension"
node --check frontend/project.js
```

Expected: dashboard test and JS syntax check pass.

- [ ] **Step 5: Commit**

```bash
git add api.py frontend/project.js tests/test_api_dashboard.py
git commit -m "feat(scope): show extension action in dashboard"
```

## Task 11: End-To-End Workflow Regression

**Files:**
- Test: `tests/test_agent_workbench_phase1_integration.py`
- Test: `tests/test_agent_workbench_application.py`

- [ ] **Step 1: Write failing end-to-end test**

Add an integration test that performs this sequence with stubbed agents:

```text
completed project with no candidates
-> workflow next exposes scope extension
-> scope extension validate passes
-> scope extension start registers pending spec
-> authority compile routes to review
-> authority accept records accepted amended authority
-> backlog generate/save appends extension work
-> roadmap generate/save appends extension phase
-> story pending shows extension phase
-> sprint candidates include only extension stories
```

Assert existing completed sprint/story counts are unchanged before and after extension.

- [ ] **Step 2: Run and verify failure**

```bash
uv run --frozen pytest tests/test_agent_workbench_phase1_integration.py tests/test_agent_workbench_application.py -q -k "scope_extension"
```

Expected: integration path fails until previous tasks are complete.

- [ ] **Step 3: Reconcile integration payload drift**

Do not add new product behavior in this task. Use it only to reconcile payload names, state keys, or command strings that drifted across earlier tasks.

- [ ] **Step 4: Run focused integration tests**

```bash
uv run --frozen pytest tests/test_agent_workbench_phase1_integration.py tests/test_agent_workbench_application.py -q -k "scope_extension"
```

Expected: integration tests pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_agent_workbench_phase1_integration.py tests/test_agent_workbench_application.py services frontend api.py cli/main.py
git commit -m "test(scope): cover extension workflow end to end"
```

## Task 12: Final Verification And Feedback Update

**Files:**
- Modify: `docs/feedback/asa-milestone1-agileforge-feedback.md`

- [ ] **Step 1: Update feedback document**

Add a short entry under the relevant AgileForge feedback section:

```markdown
### Project Scope Extension After Exhausted Scope

- Status: accepted/fixed by `dev/project-scope-extension`.
- Reason: Mature projects can exhaust all refined sprint candidates while still needing new product scope. The fix adds a project-agnostic scope-extension ritual instead of forcing backlog refinement, project recreation, or manual spec bypass.
- Contract: amended scope must pass additive spec validation, authority compile/review/accept, delta backlog generation, appended roadmap phase, and normal Story/Sprint rituals.
- Not included in v1: removal/deprecation of old accepted scope and smart roadmap reordering.
```

- [ ] **Step 2: Run focused suites**

```bash
uv run --frozen pytest tests/test_agent_workbench_scope_extension.py -q
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "scope_extension or post_sprint"
uv run --frozen pytest tests/test_agent_workbench_cli.py -q -k "scope_extension"
uv run --frozen pytest tests/test_agent_workbench_command_schema.py -q -k "scope_extension"
uv run --frozen pytest tests/test_api_dashboard.py tests/test_api_sprint_flow.py -q -k "scope_extension"
uv run --frozen pytest tests/test_backlog_phase_service.py tests/test_agent_workbench_backlog_phase.py -q -k "scope_extension"
uv run --frozen pytest tests/test_roadmap_phase_service.py tests/test_agent_workbench_roadmap_phase.py -q -k "scope_extension"
uv run --frozen pytest tests/test_agent_workbench_story_phase.py tests/test_story_phase_service.py tests/test_agent_workbench_sprint_phase.py -q -k "scope_extension"
node --check frontend/project.js
```

Expected: all focused suites pass.

- [ ] **Step 3: Run full gate**

```bash
pyrepo-check --all
```

Expected: full repository gate passes.

- [ ] **Step 4: Read-only ASA verification**

Run only read-only commands against project 3:

```bash
agileforge workflow next --project-id 3
agileforge sprint candidates --project-id 3
agileforge sprint history --project-id 3
```

Expected if ASA is exhausted:

- workflow next status is `project_scope_extension_available` or `project_scope_extension_blocked`;
- if blocked, blockers explain remaining executable scope;
- no mutation is run against ASA.

- [ ] **Step 5: Commit final docs/verification adjustment**

```bash
git add docs/feedback/asa-milestone1-agileforge-feedback.md
git commit -m "docs(feedback): record scope extension resolution"
```

## Self-Review Checklist

- Spec coverage:
  - Add-only validation: Tasks 1 and 3.
  - Exhausted-scope preconditions: Tasks 2 and 4.
  - Authority gate: Tasks 3, 4, and 11.
  - Delta backlog: Task 7.
  - Roadmap append: Task 8.
  - Story/Sprint continuation: Task 9.
  - CLI/API/UI: Tasks 5, 6, and 10.
  - History preservation: Tasks 7, 8, 9, and 11.
- Placeholder scan:
  - No unresolved placeholder tokens or angle-bracket placeholders are intentional in this plan.
- Type consistency:
  - Request models use `project_id`, `spec_file`, `base_spec_version_id`, `expected_state`, and `idempotency_key`.
  - Workflow state uses `scope_extension_context`.
  - Status strings use the constants listed in Shared Constants And Contracts.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-14-project-scope-extension.md`. Two execution options:

**1. Subagent-Driven (recommended)** - Dispatch a fresh subagent per task, review between tasks, faster and better isolation.

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
