# Active Backlog Reset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `agileforge backlog reset-active` so an approved refined backlog attempt can become the new active backlog baseline by soft-archiving old active stories, preserving history, and keeping downstream artifacts stale.

**Architecture:** Add nullable archive metadata to `UserStory`, then add a reset-specific service that consumes the existing approved refined attempt and writes both new seed rows and a `WorkflowEventType.BACKLOG_SAVED` audit event with `metadata.action = "active_backlog_reset"`. Reset is a guarded override of the existing replacement guard, not a second unapproved persistence path. Roadmap generation gets the one stale-marker exception needed after reset; story and sprint generation remain blocked until a later stale-clearing slice.

**Tech Stack:** Python 3.13, SQLModel/SQLAlchemy, Pydantic, pytest, existing AgileForge CLI/facade/phase-service architecture.

---

## Source Spec

- `/Users/aaat/projects/agileforge/docs/superpowers/specs/2026-06-02-active-backlog-reset-design.md`

## Files To Modify

- `/Users/aaat/projects/agileforge/models/core.py`  
  Add nullable archive columns to `UserStory`.
- `/Users/aaat/projects/agileforge/db/migrations.py`  
  Add additive migration for archive columns and call it from `ensure_schema_current`.
- `/Users/aaat/projects/agileforge/services/agent_workbench/read_projection.py`  
  Add archive columns to schema readiness and story payload projection where story data is exposed.
- `/Users/aaat/projects/agileforge/orchestrator_agent/agent_tools/backlog_primer/tools.py`  
  Tighten backlog-save idempotency replay to only replay `action == "backlog_saved"`.
- `/Users/aaat/projects/agileforge/services/agent_workbench/backlog_reconciliation.py`  
  Tighten `_latest_saved_count` to count only `action == "backlog_saved"` and keep reconcile replay action-specific.
- `/Users/aaat/projects/agileforge/services/agent_workbench/backlog_active_reset.py`  
  Create reset DB mutation service with request fingerprint, replay/conflict, archive, create, and history-preservation checks.
- `/Users/aaat/projects/agileforge/services/phases/backlog_service.py`  
  Add phase-level reset guard that validates workflow state, attempt/fingerprint/approval, completeness, and replacement-blocked precondition.
- `/Users/aaat/projects/agileforge/services/agent_workbench/backlog_phase.py`  
  Add `BacklogPhaseRunner.reset_active`.
- `/Users/aaat/projects/agileforge/services/agent_workbench/application.py`  
  Add facade method and `workflow next` reset-aware guidance.
- `/Users/aaat/projects/agileforge/cli/main.py`  
  Add `agileforge backlog reset-active` parser and handler.
- `/Users/aaat/projects/agileforge/services/agent_workbench/command_registry.py`  
  Register command contract.
- `/Users/aaat/projects/agileforge/services/phases/workflow_state.py`  
  Add reset-aware roadmap stale exception helper.
- `/Users/aaat/projects/agileforge/services/phases/roadmap_service.py`  
  Use roadmap-only stale exception.

## Files To Create

- `/Users/aaat/projects/agileforge/tests/test_db_migrations_active_backlog_reset.py`
- `/Users/aaat/projects/agileforge/tests/test_backlog_active_reset.py`

---

### Task 1: Add Archive Columns And Migration

**Files:**
- Modify: `/Users/aaat/projects/agileforge/models/core.py`
- Modify: `/Users/aaat/projects/agileforge/db/migrations.py`
- Modify: `/Users/aaat/projects/agileforge/services/agent_workbench/read_projection.py`
- Create: `/Users/aaat/projects/agileforge/tests/test_db_migrations_active_backlog_reset.py`

- [ ] **Step 1: Write failing migration tests**

Add `/Users/aaat/projects/agileforge/tests/test_db_migrations_active_backlog_reset.py`:

```python
"""Tests for active backlog reset archive-column migrations."""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel, create_engine

from db.migrations import ensure_schema_current
from models.core import UserStory


ARCHIVE_COLUMNS = {
    "archived_reason",
    "archived_at",
    "archived_by",
    "archive_reset_attempt_id",
    "archive_previous_status",
}


def _column_names(engine: Engine) -> set[str]:
    return {col["name"] for col in inspect(engine).get_columns("user_stories")}


def test_active_backlog_reset_migration_adds_nullable_archive_columns() -> None:
    """Archive metadata columns are additive and nullable."""
    engine = create_engine("sqlite://", echo=False)
    SQLModel.metadata.create_all(engine)

    ensure_schema_current(engine)

    columns = _column_names(engine)
    assert ARCHIVE_COLUMNS.issubset(columns)
    column_map = {col["name"]: col for col in inspect(engine).get_columns("user_stories")}
    for column_name in ARCHIVE_COLUMNS:
        assert column_map[column_name]["nullable"] is True


def test_active_backlog_reset_migration_backfills_no_existing_rows() -> None:
    """Existing rows stay unarchived after additive migration."""
    engine = create_engine("sqlite://", echo=False)
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO products (name, created_at, updated_at)
                VALUES ('Cartola', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO user_stories
                    (title, status, product_id, is_refined, is_superseded, created_at, updated_at)
                VALUES
                    ('Old story', 'To Do', 1, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )

    ensure_schema_current(engine)

    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT archived_reason, archived_at, archived_by,
                       archive_reset_attempt_id, archive_previous_status
                FROM user_stories
                WHERE title = 'Old story'
                """
            )
        ).mappings().one()
    assert dict(row) == {
        "archived_reason": None,
        "archived_at": None,
        "archived_by": None,
        "archive_reset_attempt_id": None,
        "archive_previous_status": None,
    }


def test_user_story_model_exposes_archive_columns() -> None:
    """SQLModel metadata includes reset archive fields."""
    model_columns = set(UserStory.model_fields)
    assert ARCHIVE_COLUMNS.issubset(model_columns)
```

- [ ] **Step 2: Run failing migration tests**

Run:

```bash
uv run --frozen pytest tests/test_db_migrations_active_backlog_reset.py -q
```

Expected now: fail because `UserStory` and migration do not expose archive columns.

- [ ] **Step 3: Add `UserStory` archive fields**

In `/Users/aaat/projects/agileforge/models/core.py`, add imports only if needed. `datetime` is already used in the file. Add fields after `superseded_by_story_id`:

```python
    archived_reason: str | None = Field(
        default=None,
        index=True,
        description="Archive reason for reset-archived story rows.",
    )
    archived_at: datetime | None = Field(default=None)
    archived_by: str | None = Field(
        default=None,
        max_length=100,
        description="Host-boundary actor that archived this story.",
    )
    archive_reset_attempt_id: str | None = Field(
        default=None,
        index=True,
        description="Backlog attempt id that caused active backlog reset archive.",
    )
    archive_previous_status: str | None = Field(
        default=None,
        description="Story status snapshot at reset archive time.",
    )
```

- [ ] **Step 4: Add migration helper and call it**

In `/Users/aaat/projects/agileforge/db/migrations.py`, add after `migrate_user_story_refinement_linkage`:

```python
def migrate_user_story_archive_metadata(engine: Engine) -> list[str]:
    """Ensure active-backlog reset archive metadata columns exist."""
    actions: list[str] = []

    if _ensure_column_exists(engine, "user_stories", "archived_reason", "VARCHAR"):
        actions.append("added column: user_stories.archived_reason")
    if _ensure_column_exists(engine, "user_stories", "archived_at", "DATETIME"):
        actions.append("added column: user_stories.archived_at")
    if _ensure_column_exists(engine, "user_stories", "archived_by", "VARCHAR"):
        actions.append("added column: user_stories.archived_by")
    if _ensure_column_exists(
        engine,
        "user_stories",
        "archive_reset_attempt_id",
        "VARCHAR",
    ):
        actions.append("added column: user_stories.archive_reset_attempt_id")
    if _ensure_column_exists(
        engine,
        "user_stories",
        "archive_previous_status",
        "VARCHAR",
    ):
        actions.append("added column: user_stories.archive_previous_status")

    return actions
```

Then in `ensure_schema_current`, immediately after `migrate_user_story_refinement_linkage(engine)`:

```python
        actions.extend(migrate_user_story_archive_metadata(engine))
```

- [ ] **Step 5: Update read projection schema readiness and story payload**

In `/Users/aaat/projects/agileforge/services/agent_workbench/read_projection.py`, extend `_USER_STORY_REQUIREMENT` with:

```python
        "archived_reason",
        "archived_at",
        "archived_by",
        "archive_reset_attempt_id",
        "archive_previous_status",
```

Where story payloads include `is_superseded`, include:

```python
            "archived_reason": story.archived_reason,
            "archived_at": _iso_z(story.archived_at),
            "archived_by": story.archived_by,
            "archive_reset_attempt_id": story.archive_reset_attempt_id,
            "archive_previous_status": story.archive_previous_status,
```

- [ ] **Step 6: Run migration tests**

Run:

```bash
uv run --frozen pytest tests/test_db_migrations_active_backlog_reset.py -q
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add models/core.py db/migrations.py services/agent_workbench/read_projection.py tests/test_db_migrations_active_backlog_reset.py
git commit -m "feat: add backlog reset archive metadata"
```

---

### Task 2: Make Existing Backlog Event Readers Action-Specific

**Files:**
- Modify: `/Users/aaat/projects/agileforge/orchestrator_agent/agent_tools/backlog_primer/tools.py`
- Modify: `/Users/aaat/projects/agileforge/services/agent_workbench/backlog_reconciliation.py`
- Modify: `/Users/aaat/projects/agileforge/tests/test_backlog_primer_agent.py`
- Modify: `/Users/aaat/projects/agileforge/tests/test_agent_workbench_backlog_phase.py`

- [ ] **Step 1: Add failing save replay regression test**

In `/Users/aaat/projects/agileforge/tests/test_backlog_primer_agent.py`, add near existing save idempotency tests:

```python
    @pytest.mark.asyncio
    async def test_backlog_save_idempotency_ignores_active_reset_events(self) -> None:
        """Reset events share BACKLOG_SAVED type but must not replay save keys."""
        mock_context = MagicMock()
        mock_context.state = {}
        test_engine = create_engine("sqlite://", echo=False)
        SQLModel.metadata.create_all(test_engine)
        with SqlSession(test_engine) as session:
            session.add(Product(name="Test Product"))
            session.commit()
            session.add(
                WorkflowEvent(
                    event_type=WorkflowEventType.BACKLOG_SAVED,
                    product_id=1,
                    event_metadata=json.dumps(
                        {
                            "action": "active_backlog_reset",
                            "idempotency_key": "same-key",
                            "request_fingerprint": "sha256:reset",
                            "created_count": 2,
                        }
                    ),
                )
            )
            session.commit()

        save_input = SaveBacklogInput(
            product_id=1,
            idempotency_key="same-key",
            backlog_items=[
                {
                    "priority": 1,
                    "requirement": "New baseline",
                    "value_driver": "Strategic",
                    "justification": "Use reviewed backlog.",
                    "estimated_effort": "M",
                }
            ],
        )

        with patch(
            "orchestrator_agent.agent_tools.backlog_primer.tools.get_engine",
            return_value=test_engine,
        ):
            result = await save_backlog_tool(save_input, tool_context=mock_context)

        assert result["success"] is True
        assert result.get("idempotent_replay") is not True
        assert result["saved_count"] == 1
```

- [ ] **Step 2: Add failing reconcile saved-count regression test**

In `/Users/aaat/projects/agileforge/tests/test_agent_workbench_backlog_phase.py`, add after `test_backlog_reconcile_supersedes_legacy_duplicate_active_seed_rows`:

```python
def test_backlog_reconcile_latest_saved_count_ignores_active_reset_events(
    session: Session,
) -> None:
    """Reset event created_count must not corrupt canonical cohort selection."""
    product = Product(name="Cartola")
    session.add(product)
    session.commit()
    session.refresh(product)
    assert product.product_id is not None
    product_id = product.product_id
    base = datetime(2026, 6, 2, 12, tzinfo=UTC)
    for offset, title, rank in [
        (0, "Old import", "1"),
        (1, "Old projection", "2"),
        (10, "New import", "1"),
        (11, "New projection", "2"),
    ]:
        session.add(
            UserStory(
                product_id=product_id,
                title=title,
                status=StoryStatus.TO_DO,
                rank=rank,
                story_origin="backlog_seed",
                is_refined=False,
                is_superseded=False,
                created_at=base + timedelta(minutes=offset),
                updated_at=base + timedelta(minutes=offset),
            )
        )
    session.add(
        WorkflowEvent(
            event_type=WorkflowEventType.BACKLOG_SAVED,
            product_id=product_id,
            timestamp=base + timedelta(minutes=12),
            event_metadata=json.dumps(
                {
                    "action": "backlog_saved",
                    "processed_count": 2,
                    "created_count": 2,
                }
            ),
        )
    )
    session.add(
        WorkflowEvent(
            event_type=WorkflowEventType.BACKLOG_SAVED,
            product_id=product_id,
            timestamp=base + timedelta(minutes=20),
            event_metadata=json.dumps(
                {
                    "action": "active_backlog_reset",
                    "created_count": 13,
                    "idempotency_key": "reset-active",
                }
            ),
        )
    )
    session.commit()

    runner = BacklogPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.reconcile(
        project_id=product_id,
        idempotency_key="reconcile-ignore-reset",
    )

    assert result["ok"] is True
    assert result["data"]["active_after"] == 2
```

- [ ] **Step 3: Run failing event-reader tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_backlog_primer_agent.py::TestBacklogPrimerAgent::test_backlog_save_idempotency_ignores_active_reset_events \
  tests/test_agent_workbench_backlog_phase.py::test_backlog_reconcile_latest_saved_count_ignores_active_reset_events \
  -q
```

Expected now: at least one test fails because existing readers do not require `action == "backlog_saved"`.

- [ ] **Step 4: Fix backlog save replay**

In `/Users/aaat/projects/agileforge/orchestrator_agent/agent_tools/backlog_primer/tools.py`, change `_idempotent_backlog_save_replay` action check from skip-reconcile to save-only:

```python
        if metadata.get("action") != "backlog_saved":
            continue
```

- [ ] **Step 5: Fix reconcile saved-count inference**

In `/Users/aaat/projects/agileforge/services/agent_workbench/backlog_reconciliation.py`, change `_latest_saved_count` loop:

```python
        if metadata.get("action") != "backlog_saved":
            continue
```

Keep `_idempotent_replay` unchanged if it already requires `action == "backlog_reconciled"`.

- [ ] **Step 6: Run event-reader tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_backlog_primer_agent.py::TestBacklogPrimerAgent::test_backlog_save_idempotency_ignores_active_reset_events \
  tests/test_agent_workbench_backlog_phase.py::test_backlog_reconcile_latest_saved_count_ignores_active_reset_events \
  -q
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add orchestrator_agent/agent_tools/backlog_primer/tools.py services/agent_workbench/backlog_reconciliation.py tests/test_backlog_primer_agent.py tests/test_agent_workbench_backlog_phase.py
git commit -m "fix: isolate backlog saved event actions"
```

---

### Task 3: Add Reset DB Mutation Service

**Files:**
- Create: `/Users/aaat/projects/agileforge/services/agent_workbench/backlog_active_reset.py`
- Create: `/Users/aaat/projects/agileforge/tests/test_backlog_active_reset.py`

- [ ] **Step 1: Write failing DB mutation tests**

Create `/Users/aaat/projects/agileforge/tests/test_backlog_active_reset.py`:

```python
"""Tests for active backlog reset DB mutation."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from models.core import Product, Sprint, SprintStory, Task, UserStory
from models.enums import SprintStatus, StoryStatus, TaskStatus, WorkflowEventType
from models.events import StoryCompletionLog, TaskExecutionLog, WorkflowEvent
from services.agent_workbench.backlog_active_reset import (
    ActiveBacklogResetRequest,
    ActiveBacklogResetReusedKeyError,
    reset_active_backlog_rows,
)


def _engine():
    engine = create_engine("sqlite://", echo=False)
    SQLModel.metadata.create_all(engine)
    return engine


def _approved_item(priority: int, requirement: str) -> dict[str, Any]:
    return {
        "priority": priority,
        "requirement": requirement,
        "authority_ref": f"REQ.{priority}",
        "capability_hint": requirement,
        "value_driver": "Strategic",
        "justification": f"Reviewed item {priority}",
        "estimated_effort": "M",
        "technical_note": "Imported from approved refined backlog.",
        "as_built_annotation": {"schema_version": "agileforge.brownfield_annotation.v1"},
    }


def _request(**overrides: object) -> ActiveBacklogResetRequest:
    payload = {
        "project_id": 1,
        "attempt_id": "backlog-attempt-12",
        "expected_artifact_fingerprint": "sha256:artifact",
        "expected_state": "BACKLOG_REVIEW",
        "reset_reason": "pre-brownfield backlog reset",
        "archive_all_active_stories": True,
        "idempotency_key": "reset-active-1",
        "approved_artifact_fingerprint": "sha256:artifact",
        "artifact": {
            "is_complete": True,
            "clarifying_questions": [],
            "backlog_items": [
                _approved_item(1, "Validate captain-aware contract"),
                _approved_item(2, "Build post-round review artifact"),
            ],
        },
        "now": datetime(2026, 6, 2, 12, tzinfo=UTC),
    }
    payload.update(overrides)
    return ActiveBacklogResetRequest.model_validate(payload)


def test_reset_active_archives_all_active_rows_and_preserves_history() -> None:
    """Reset soft-archives active rows and leaves historical links queryable."""
    engine = _engine()
    with Session(engine) as session:
        product = Product(name="Cartola")
        session.add(product)
        session.commit()
        session.refresh(product)
        assert product.product_id == 1

        old_done = UserStory(
            product_id=1,
            title="Old delivered story",
            status=StoryStatus.DONE,
            story_origin="backlog_seed",
            is_refined=True,
            is_superseded=False,
            acceptance_criteria="- Done",
            completed_at=datetime(2026, 5, 28, 12, tzinfo=UTC),
        )
        old_todo = UserStory(
            product_id=1,
            title="Old residue story",
            status=StoryStatus.TO_DO,
            story_origin="backlog_seed",
            is_refined=False,
            is_superseded=False,
        )
        session.add(old_done)
        session.add(old_todo)
        session.commit()
        session.refresh(old_done)
        session.refresh(old_todo)
        assert old_done.story_id is not None

        sprint = Sprint(
            product_id=1,
            goal="First sprint",
            status=SprintStatus.COMPLETED,
            close_snapshot_json='{"closed": true}',
        )
        session.add(sprint)
        session.commit()
        session.refresh(sprint)
        assert sprint.sprint_id is not None
        session.add(SprintStory(sprint_id=sprint.sprint_id, story_id=old_done.story_id))
        task = Task(
            story_id=old_done.story_id,
            title="Implement old delivered story",
            status=TaskStatus.DONE,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        assert task.task_id is not None
        session.add(
            StoryCompletionLog(
                story_id=old_done.story_id,
                old_status=StoryStatus.IN_PROGRESS,
                new_status=StoryStatus.DONE,
                changed_by="po",
            )
        )
        session.add(
            TaskExecutionLog(
                task_id=task.task_id,
                sprint_id=sprint.sprint_id,
                old_status=TaskStatus.IN_PROGRESS,
                new_status=TaskStatus.DONE,
                changed_by="agent",
            )
        )
        session.commit()

    result = reset_active_backlog_rows(engine, _request())

    assert result["success"] is True
    assert result["archived_count"] == 2
    assert result["created_count"] == 2
    with Session(engine) as session:
        rows = session.exec(
            select(UserStory).where(UserStory.product_id == 1).order_by(UserStory.story_id)
        ).all()
        archived = [row for row in rows if row.archived_reason == "active_backlog_reset"]
        active = [row for row in rows if not row.is_superseded]
        assert [row.title for row in active] == [
            "Validate captain-aware contract",
            "Build post-round review artifact",
        ]
        assert {row.title for row in archived} == {
            "Old delivered story",
            "Old residue story",
        }
        done = next(row for row in archived if row.title == "Old delivered story")
        assert done.status == StoryStatus.DONE
        assert done.archive_previous_status == StoryStatus.DONE.value
        assert done.archive_reset_attempt_id == "backlog-attempt-12"
        assert done.archived_by == "po"
        assert session.exec(select(SprintStory)).first() is not None
        assert session.exec(select(StoryCompletionLog)).first() is not None
        assert session.exec(select(Task)).first() is not None
        assert session.exec(select(TaskExecutionLog)).first() is not None
        event = session.exec(select(WorkflowEvent)).one()
        assert event.event_type == WorkflowEventType.BACKLOG_SAVED
        metadata = json.loads(event.event_metadata or "{}")
        assert metadata["action"] == "active_backlog_reset"
        assert metadata["request_fingerprint"].startswith("sha256:")


def test_reset_active_idempotency_replays_same_request() -> None:
    """Same key and same fingerprint replays without duplicate rows."""
    engine = _engine()
    with Session(engine) as session:
        session.add(Product(name="Cartola"))
        session.commit()
        session.add(
            UserStory(
                product_id=1,
                title="Old story",
                status=StoryStatus.TO_DO,
                story_origin="backlog_seed",
                is_superseded=False,
            )
        )
        session.commit()

    request = _request()
    first = reset_active_backlog_rows(engine, request)
    second = reset_active_backlog_rows(engine, request)

    assert first["success"] is True
    assert second["success"] is True
    assert second["idempotent_replay"] is True
    with Session(engine) as session:
        rows = session.exec(select(UserStory)).all()
        assert len(rows) == 3


def test_reset_active_idempotency_conflict_fails() -> None:
    """Same key with different request fingerprint is rejected."""
    engine = _engine()
    with Session(engine) as session:
        session.add(Product(name="Cartola"))
        session.commit()
        session.add(
            UserStory(
                product_id=1,
                title="Old story",
                status=StoryStatus.TO_DO,
                story_origin="backlog_seed",
                is_superseded=False,
            )
        )
        session.commit()

    reset_active_backlog_rows(engine, _request())
    with pytest.raises(ActiveBacklogResetReusedKeyError):
        reset_active_backlog_rows(
            engine,
            _request(reset_reason="different PO reset reason"),
        )
```

- [ ] **Step 2: Run failing DB mutation tests**

Run:

```bash
uv run --frozen pytest tests/test_backlog_active_reset.py -q
```

Expected now: fail because module does not exist.

- [ ] **Step 3: Implement reset service**

Create `/Users/aaat/projects/agileforge/services/agent_workbench/backlog_active_reset.py`:

```python
"""DB mutation for explicit active backlog reset."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from models.core import UserStory
from models.enums import StoryStatus, WorkflowEventType
from models.events import WorkflowEvent
from orchestrator_agent.agent_tools.backlog_primer.schemes import BacklogItem
from services.agent_workbench.fingerprints import canonical_hash
from services.phases.backlog_refinement import project_savable_backlog_items


class ActiveBacklogResetError(RuntimeError):
    """Base reset-active mutation error."""


class ActiveBacklogResetReusedKeyError(ActiveBacklogResetError):
    """Raised when an idempotency key is reused with different inputs."""


class ActiveBacklogResetRequest(BaseModel):
    """Host-validated active backlog reset request."""

    model_config = ConfigDict(extra="forbid")

    project_id: int
    attempt_id: str = Field(min_length=1)
    expected_artifact_fingerprint: str = Field(min_length=1)
    expected_state: str = Field(min_length=1)
    reset_reason: str = Field(min_length=1)
    archive_all_active_stories: bool
    idempotency_key: str = Field(min_length=1)
    approved_artifact_fingerprint: str = Field(min_length=1)
    artifact: dict[str, Any]
    now: datetime
    archived_by: str = "po"


def reset_request_fingerprint(request: ActiveBacklogResetRequest) -> str:
    """Return deterministic reset request fingerprint stored in event metadata."""
    return canonical_hash(
        {
            "command": "agileforge backlog reset-active",
            "project_id": request.project_id,
            "attempt_id": request.attempt_id,
            "expected_artifact_fingerprint": request.expected_artifact_fingerprint,
            "expected_state": request.expected_state,
            "reset_reason": request.reset_reason,
            "archive_all_active_stories": request.archive_all_active_stories,
            "approved_artifact_fingerprint": request.approved_artifact_fingerprint,
        }
    )


def reset_active_backlog_rows(
    engine: Engine,
    request: ActiveBacklogResetRequest,
) -> dict[str, Any]:
    """Soft-archive active stories and create seed rows from approved artifact."""
    request_fingerprint = reset_request_fingerprint(request)
    with Session(engine) as session:
        replay = _reset_replay(
            session,
            project_id=request.project_id,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return replay

        active_stories = session.exec(
            select(UserStory)
            .where(UserStory.product_id == request.project_id)
            .where(UserStory.is_superseded == False)  # noqa: E712
            .order_by(UserStory.story_id)
        ).all()
        if not active_stories:
            raise ActiveBacklogResetError("RESET_NOT_REQUIRED")

        archived_story_ids: list[int] = []
        for story in active_stories:
            story.is_superseded = True
            story.superseded_by_story_id = None
            story.archived_reason = "active_backlog_reset"
            story.archived_at = request.now
            story.archived_by = request.archived_by
            story.archive_reset_attempt_id = request.attempt_id
            story.archive_previous_status = _story_status_value(story.status)
            if story.story_id is not None:
                archived_story_ids.append(story.story_id)
            session.add(story)

        created_story_ids: list[int] = []
        projected_items = project_savable_backlog_items(request.artifact)
        for raw_item in projected_items:
            item = BacklogItem.model_validate(raw_item)
            story = _story_from_backlog_item(request.project_id, item)
            session.add(story)
            session.flush()
            if story.story_id is not None:
                created_story_ids.append(story.story_id)

        metadata = {
            "action": "active_backlog_reset",
            "project_id": request.project_id,
            "attempt_id": request.attempt_id,
            "artifact_fingerprint": request.expected_artifact_fingerprint,
            "approved_artifact_fingerprint": request.approved_artifact_fingerprint,
            "reset_reason": request.reset_reason,
            "archived_story_ids": archived_story_ids,
            "created_story_ids": created_story_ids,
            "archived_count": len(archived_story_ids),
            "created_count": len(created_story_ids),
            "idempotency_key": request.idempotency_key,
            "request_fingerprint": request_fingerprint,
        }
        session.add(
            WorkflowEvent(
                event_type=WorkflowEventType.BACKLOG_SAVED,
                product_id=request.project_id,
                timestamp=request.now,
                event_metadata=json.dumps(metadata, sort_keys=True),
            )
        )
        session.commit()
        return {"success": True, **metadata}


def _story_from_backlog_item(product_id: int, item: BacklogItem) -> UserStory:
    effort = str(item.estimated_effort).strip().upper()
    points = {"S": 1, "M": 3, "L": 5, "XL": 8}.get(effort)
    return UserStory(
        title=item.requirement,
        product_id=product_id,
        status=StoryStatus.TO_DO,
        rank=str(item.priority),
        story_points=points,
        story_description=item.justification,
        acceptance_criteria=None,
        source_requirement=item.requirement.lower().strip().replace(" ", "-"),
        refinement_slot=item.priority,
        story_origin="backlog_seed",
        is_refined=False,
        is_superseded=False,
    )


def _reset_replay(
    session: Session,
    *,
    project_id: int,
    idempotency_key: str,
    request_fingerprint: str,
) -> dict[str, Any] | None:
    events = session.exec(
        select(WorkflowEvent)
        .where(WorkflowEvent.product_id == project_id)
        .where(WorkflowEvent.event_type == WorkflowEventType.BACKLOG_SAVED)
    ).all()
    for event in events:
        metadata = _json_object(event.event_metadata)
        if metadata.get("action") != "active_backlog_reset":
            continue
        if metadata.get("idempotency_key") != idempotency_key:
            continue
        if metadata.get("request_fingerprint") != request_fingerprint:
            raise ActiveBacklogResetReusedKeyError("RESET_IDEMPOTENCY_CONFLICT")
        replay = dict(metadata)
        replay["success"] = True
        replay["idempotent_replay"] = True
        return replay
    return None


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _story_status_value(status: object) -> str:
    return str(status.value if hasattr(status, "value") else status)
```

- [ ] **Step 4: Run DB mutation tests**

Run:

```bash
uv run --frozen pytest tests/test_backlog_active_reset.py -q
```

Expected: pass. If `source_requirement` normalization differs from save tool behavior, import `normalize_requirement_key` from `orchestrator_agent.agent_tools.backlog_primer.tools` and use it in `_story_from_backlog_item`.

- [ ] **Step 5: Commit**

```bash
git add services/agent_workbench/backlog_active_reset.py tests/test_backlog_active_reset.py
git commit -m "feat: add active backlog reset mutation"
```

---

### Task 4: Add Phase Guard, Runner, Facade, CLI, And Command Schema

**Files:**
- Modify: `/Users/aaat/projects/agileforge/services/phases/backlog_service.py`
- Modify: `/Users/aaat/projects/agileforge/services/agent_workbench/backlog_phase.py`
- Modify: `/Users/aaat/projects/agileforge/services/agent_workbench/application.py`
- Modify: `/Users/aaat/projects/agileforge/cli/main.py`
- Modify: `/Users/aaat/projects/agileforge/services/agent_workbench/command_registry.py`
- Modify: `/Users/aaat/projects/agileforge/tests/test_backlog_phase_service.py`
- Modify: `/Users/aaat/projects/agileforge/tests/test_agent_workbench_application.py`
- Modify: `/Users/aaat/projects/agileforge/tests/test_agent_workbench_cli.py`
- Modify: `/Users/aaat/projects/agileforge/tests/test_agent_workbench_command_schema.py`

- [ ] **Step 1: Add failing phase-service guard tests**

In `/Users/aaat/projects/agileforge/tests/test_backlog_phase_service.py`, add tests for:

```python
@pytest.mark.asyncio
async def test_reset_active_backlog_requires_next_cycle_origin() -> None:
    """Reset-active is only valid for next-cycle refinement review."""
    state = _review_state_for_artifact(
        {
            "backlog_items": [_savable_backlog_item()],
            "is_complete": True,
            "clarifying_questions": [],
        }
    )
    state["backlog_review_origin"] = "initial_backlog"
    state["backlog_attempts"][0].update(
        {
            "attempt_kind": "import_refinement",
            "refinement_saveable": True,
            "refinement_approval": {
                "approval_id": "approval:reset",
                "approved_artifact_fingerprint": state["product_backlog_assessment"][
                    "artifact_fingerprint"
                ],
            },
        }
    )

    async def hydrate_context() -> object:
        return SimpleNamespace(state=dict(state), session_id="7")

    with pytest.raises(BacklogPhaseError) as exc_info:
        await reset_active_backlog(
            project_id=7,
            attempt_id="backlog-attempt-1",
            expected_artifact_fingerprint=state["product_backlog_assessment"][
                "artifact_fingerprint"
            ],
            expected_state="BACKLOG_REVIEW",
            reset_reason="pre-brownfield reset",
            archive_all_active_stories=True,
            idempotency_key="reset-active-1",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-06-02T12:00:00Z",
            hydrate_context=hydrate_context,
            reset_rows=lambda _request: {"success": True},
            replacement_blocked=lambda _project_id: True,
        )

    assert "RESET_WRONG_REVIEW_ORIGIN" in exc_info.value.detail
```

Add a passing test using `backlog_review_origin = "next_cycle_refinement"` that asserts:

```python
assert saved["state"]["fsm_state"] == "BACKLOG_PERSISTENCE"
assert saved["state"]["downstream_backlog_stale"] is True
assert saved["state"]["stale_backlog_reason"] == "active_backlog_reset"
assert saved["state"]["stale_since_backlog_attempt_id"] == "backlog-attempt-1"
assert payload["reset_result"]["success"] is True
```

- [ ] **Step 2: Run failing phase tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_backlog_phase_service.py::test_reset_active_backlog_requires_next_cycle_origin \
  -q
```

Expected now: fail because `reset_active_backlog` does not exist.

- [ ] **Step 3: Implement phase-service reset guard**

In `/Users/aaat/projects/agileforge/services/phases/backlog_service.py`, import:

```python
from datetime import UTC, datetime
from services.agent_workbench.backlog_active_reset import ActiveBacklogResetRequest
```

Add an async function near `save_backlog_draft`:

```python
async def reset_active_backlog(
    *,
    project_id: int,
    attempt_id: str,
    expected_artifact_fingerprint: str,
    expected_state: str,
    reset_reason: str,
    archive_all_active_stories: bool,
    idempotency_key: str,
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
    hydrate_context: Callable[[], Awaitable[Any]],
    reset_rows: Callable[[ActiveBacklogResetRequest], dict[str, Any]],
    replacement_blocked: Callable[[int], bool],
) -> dict[str, Any]:
    context = await hydrate_context()
    state = context.state
    _assert_save_expected_state(state, expected_state)
    if state.get("backlog_review_origin") != "next_cycle_refinement":
        raise BacklogPhaseError("RESET_WRONG_REVIEW_ORIGIN")
    if not reset_reason.strip():
        raise BacklogPhaseError("RESET_REASON_REQUIRED")
    if archive_all_active_stories is not True:
        raise BacklogPhaseError("RESET_ARCHIVE_FLAG_REQUIRED")
    if not idempotency_key.strip():
        raise BacklogPhaseError("RESET_IDEMPOTENCY_CONFLICT")
    if not replacement_blocked(project_id):
        raise BacklogPhaseError("RESET_NOT_REQUIRED")

    assessment = state.get("product_backlog_assessment")
    if not isinstance(assessment, dict):
        raise BacklogPhaseError("RESET_ATTEMPT_NOT_FOUND")
    _assert_save_guards(
        state=state,
        assessment=assessment,
        attempt_id=attempt_id,
        expected_artifact_fingerprint=expected_artifact_fingerprint,
    )
    _assert_refined_attempt_saveable(
        state=state,
        attempt_id=attempt_id,
        expected_artifact_fingerprint=expected_artifact_fingerprint,
    )
    if not bool(assessment.get("is_complete", False)):
        raise BacklogPhaseError("RESET_ATTEMPT_INCOMPLETE")
    if _has_clarifying_questions(assessment):
        raise BacklogPhaseError("RESET_ATTEMPT_INCOMPLETE")

    approval = assessment.get("refinement_approval")
    approved_fingerprint = ""
    if isinstance(approval, dict):
        approved_fingerprint = str(approval.get("approved_artifact_fingerprint") or "")
    request = ActiveBacklogResetRequest(
        project_id=project_id,
        attempt_id=attempt_id,
        expected_artifact_fingerprint=expected_artifact_fingerprint,
        expected_state=expected_state,
        reset_reason=reset_reason.strip(),
        archive_all_active_stories=archive_all_active_stories,
        idempotency_key=idempotency_key.strip(),
        approved_artifact_fingerprint=approved_fingerprint,
        artifact=assessment,
        now=_parse_iso(now_iso()),
    )
    reset_result = reset_rows(request)

    now = now_iso()
    state["fsm_state"] = OrchestratorState.BACKLOG_PERSISTENCE.value
    state["fsm_state_entered_at"] = now
    state["backlog_saved_at"] = now
    state["downstream_backlog_stale"] = True
    state["stale_backlog_reason"] = "active_backlog_reset"
    state["stale_since_backlog_attempt_id"] = attempt_id
    state["active_backlog_reset_at"] = now
    state["active_backlog_reset_attempt_id"] = attempt_id
    save_state(state)
    return {
        "fsm_state": OrchestratorState.BACKLOG_PERSISTENCE.value,
        "attempt_id": attempt_id,
        "artifact_fingerprint": expected_artifact_fingerprint,
        "reset_result": reset_result,
        "idempotency_key": idempotency_key,
    }


def _parse_iso(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
```

- [ ] **Step 4: Add runner/facade/CLI command**

Add `reset_active` to:

- `BacklogPhaseRunner` protocol and class in `/Users/aaat/projects/agileforge/services/agent_workbench/backlog_phase.py`;
- application facade protocol and method in `/Users/aaat/projects/agileforge/services/agent_workbench/application.py`;
- CLI parser and handler in `/Users/aaat/projects/agileforge/cli/main.py`;
- command registry metadata in `/Users/aaat/projects/agileforge/services/agent_workbench/command_registry.py`.

CLI arguments:

```python
    backlog_reset_active = backlog_sub.add_parser(
        "reset-active",
        help="Soft-archive active backlog rows and install an approved refined attempt.",
    )
    backlog_reset_active.add_argument("--project-id", type=int, required=True)
    backlog_reset_active.add_argument("--attempt-id", required=True)
    backlog_reset_active.add_argument("--expected-artifact-fingerprint", required=True)
    backlog_reset_active.add_argument("--expected-state", required=True)
    backlog_reset_active.add_argument("--reset-reason", required=True)
    backlog_reset_active.add_argument("--archive-all-active-stories", action="store_true")
    backlog_reset_active.add_argument("--idempotency-key", required=True)
    backlog_reset_active.set_defaults(command_handler=_backlog_reset_active)
```

Command registry metadata:

```python
    CommandMetadata(
        name="agileforge backlog reset-active",
        mutates=True,
        phase="phase_2d",
        destructive=False,
        requires_idempotency_key=True,
        accepts_expected_state=True,
        accepts_expected_artifact_fingerprint=True,
        input_required=(
            "project_id",
            "attempt_id",
            "expected_artifact_fingerprint",
            "expected_state",
            "reset_reason",
            "archive_all_active_stories",
            "idempotency_key",
        ),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.MUTATION_FAILED.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
        ),
    ),
```

- [ ] **Step 5: Add command tests**

Add/extend tests so these pass:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_command_schema.py::test_command_schema_payloads_are_available \
  tests/test_agent_workbench_cli.py::test_backlog_reset_active_routes_to_application \
  tests/test_agent_workbench_application.py::test_backlog_reset_active_facade_routes_to_runner \
  -q
```

Create these exact tests when absent:

- `test_command_schema_payloads_are_available` should include `"agileforge backlog reset-active"` in its known command list;
- `test_backlog_reset_active_routes_to_application` should live beside other CLI backlog route tests and assert parsed args route to `backlog_reset_active`;
- `test_backlog_reset_active_facade_routes_to_runner` should live beside other application facade backlog route tests and assert the facade calls `BacklogPhaseRunner.reset_active`.

- [ ] **Step 6: Run focused command/phase tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_backlog_phase_service.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_command_schema.py \
  -q
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add services/phases/backlog_service.py services/agent_workbench/backlog_phase.py services/agent_workbench/application.py cli/main.py services/agent_workbench/command_registry.py tests/test_backlog_phase_service.py tests/test_agent_workbench_application.py tests/test_agent_workbench_cli.py tests/test_agent_workbench_command_schema.py
git commit -m "feat: expose active backlog reset command"
```

---

### Task 5: Add Reset-Aware Stale Routing

**Files:**
- Modify: `/Users/aaat/projects/agileforge/services/phases/workflow_state.py`
- Modify: `/Users/aaat/projects/agileforge/services/phases/roadmap_service.py`
- Modify: `/Users/aaat/projects/agileforge/services/agent_workbench/application.py`
- Modify: `/Users/aaat/projects/agileforge/tests/test_roadmap_phase_service.py`
- Modify: `/Users/aaat/projects/agileforge/tests/test_agent_workbench_application.py`
- Modify: `/Users/aaat/projects/agileforge/tests/test_agent_workbench_story_phase.py`
- Modify: `/Users/aaat/projects/agileforge/tests/test_agent_workbench_sprint_phase.py`

- [ ] **Step 1: Add failing roadmap stale exception test**

In `/Users/aaat/projects/agileforge/tests/test_roadmap_phase_service.py`, add:

```python
@pytest.mark.asyncio
async def test_generate_roadmap_allows_active_reset_stale_marker() -> None:
    """Roadmap generation is the reset stale-exit path."""
    state: JsonDict = {
        "fsm_state": "BACKLOG_PERSISTENCE",
        "downstream_backlog_stale": True,
        "stale_backlog_reason": "active_backlog_reset",
        "stale_since_backlog_attempt_id": "backlog-attempt-12",
        "active_backlog_reset_attempt_id": "backlog-attempt-12",
    }
    saved: JsonDict = {}

    async def load_state() -> JsonDict:
        return state

    async def fake_run_roadmap_agent_from_state(
        state: object, *, project_id: int, user_input: str | None
    ) -> JsonDict:
        del state, project_id, user_input
        return {
            "success": True,
            "input_context": {},
            "output_artifact": _complete_roadmap_artifact(is_complete=True),
            "is_complete": True,
            "error": None,
        }

    payload = await generate_roadmap_draft(
        project_id=7,
        load_state=load_state,
        save_state=lambda updated: saved.update({"state": dict(updated)}),
        now_iso=lambda: "2026-06-02T12:00:00Z",
        run_roadmap_agent=fake_run_roadmap_agent_from_state,
        user_input=None,
    )

    assert payload["fsm_state"] == "ROADMAP_REVIEW"
    assert saved["state"]["stale_backlog_reason"] == "active_backlog_reset"
```

- [ ] **Step 2: Add story/sprint blocked tests**

Add one focused test each in story and sprint phase tests using existing stale tests as template:

```python
state = {
    "fsm_state": "STORY_INTERVIEW",
    "downstream_backlog_stale": True,
    "stale_backlog_reason": "active_backlog_reset",
    "stale_since_backlog_attempt_id": "backlog-attempt-12",
    "active_backlog_reset_attempt_id": "backlog-attempt-12",
}
```

Expected: story generation and sprint planning still raise stale-backlog error.

- [ ] **Step 3: Run failing stale tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_roadmap_phase_service.py::test_generate_roadmap_allows_active_reset_stale_marker \
  -q
```

Expected now: fail because `assert_downstream_backlog_not_stale` blocks roadmap.

- [ ] **Step 4: Add reset-aware helper**

In `/Users/aaat/projects/agileforge/services/phases/workflow_state.py`, add:

```python
def assert_downstream_backlog_not_stale_for_roadmap(state: dict[str, Any]) -> None:
    """Block stale backlog except reset marker whose exit path is roadmap."""
    if state.get("downstream_backlog_stale") is not True:
        return
    if (
        state.get("fsm_state") == OrchestratorState.BACKLOG_PERSISTENCE.value
        and state.get("stale_backlog_reason") == "active_backlog_reset"
        and state.get("stale_since_backlog_attempt_id")
        == state.get("active_backlog_reset_attempt_id")
    ):
        return
    assert_downstream_backlog_not_stale(state)
```

- [ ] **Step 5: Use helper in roadmap service only**

In `/Users/aaat/projects/agileforge/services/phases/roadmap_service.py`, replace:

```python
        workflow_state.assert_downstream_backlog_not_stale(state)
```

with:

```python
        workflow_state.assert_downstream_backlog_not_stale_for_roadmap(state)
```

Do not change story or sprint services.

- [ ] **Step 6: Update `workflow next` to expose reset phase clearly**

In `/Users/aaat/projects/agileforge/services/agent_workbench/application.py`, inside `_backlog_workflow_next`, when `fsm_state == "BACKLOG_PERSISTENCE"` and reset stale marker is present, return:

```python
status = "active_backlog_reset_requires_roadmap_regeneration"
```

and keep only roadmap generation as next phase command. Add no sprint commands. Add a `blocked_commands` entry explaining sprint/story dead-end:

```python
{
    "command": "agileforge sprint save",
    "reason": "DOWNSTREAM_BACKLOG_STALE_AFTER_ACTIVE_RESET",
    "message": "Sprint generation remains blocked until downstream reset-stale clearing exists.",
}
```

- [ ] **Step 7: Run stale routing tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_roadmap_phase_service.py \
  tests/test_agent_workbench_story_phase.py \
  tests/test_agent_workbench_sprint_phase.py \
  tests/test_agent_workbench_application.py \
  -q
```

Expected: pass. This documents acknowledged dead-end: roadmap can regenerate after reset, story/sprint remain blocked until deferred stale-clearing slice.

- [ ] **Step 8: Commit**

```bash
git add services/phases/workflow_state.py services/phases/roadmap_service.py services/agent_workbench/application.py tests/test_roadmap_phase_service.py tests/test_agent_workbench_story_phase.py tests/test_agent_workbench_sprint_phase.py tests/test_agent_workbench_application.py
git commit -m "feat: route reset backlog to roadmap regeneration"
```

---

### Task 6: Add End-To-End Reset Acceptance Coverage

**Files:**
- Modify: `/Users/aaat/projects/agileforge/tests/test_agent_workbench_backlog_phase.py`
- Modify: `/Users/aaat/projects/agileforge/tests/test_agent_workbench_cli.py`

- [ ] **Step 1: Add runner-level reset acceptance test**

In `/Users/aaat/projects/agileforge/tests/test_agent_workbench_backlog_phase.py`, add a test that builds:

- product;
- one active `Done` story with sprint link and completion log;
- one active `To Do` story;
- workflow state with `BACKLOG_REVIEW`, `backlog_review_origin = "next_cycle_refinement"`, approved `backlog-attempt-12`, complete artifact, and matching fingerprint;
- call `BacklogPhaseRunner.reset_active(...)`;
- assert `ok is True`, workflow moves to `BACKLOG_PERSISTENCE`, old rows archived, new rows active, event metadata has `action = "active_backlog_reset"`.

Use exact command values:

```python
result = runner.reset_active(
    project_id=product_id,
    attempt_id="backlog-attempt-12",
    expected_artifact_fingerprint=artifact_fingerprint,
    expected_state="BACKLOG_REVIEW",
    reset_reason="pre-brownfield backlog reset",
    archive_all_active_stories=True,
    idempotency_key="reset-active-acceptance-1",
)
```

- [ ] **Step 2: Add CLI JSON smoke test**

In `/Users/aaat/projects/agileforge/tests/test_agent_workbench_cli.py`, add test mirroring existing CLI route tests:

```python
def test_backlog_reset_active_cli_routes_expected_arguments() -> None:
    """CLI forwards reset-active guarded arguments to application."""
```

Assert routed method is `backlog_reset_active`, command is `agileforge backlog reset-active`, and kwargs include:

```python
{
    "project_id": 7,
    "attempt_id": "backlog-attempt-12",
    "expected_artifact_fingerprint": "sha256:artifact",
    "expected_state": "BACKLOG_REVIEW",
    "reset_reason": "pre-brownfield backlog reset",
    "archive_all_active_stories": True,
    "idempotency_key": "reset-active-cli-1",
}
```

- [ ] **Step 3: Run acceptance tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_backlog_phase.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_command_schema.py \
  -q
```

Expected: pass.

- [ ] **Step 4: Run full targeted phase set**

Run:

```bash
uv run --frozen pytest \
  tests/test_db_migrations_active_backlog_reset.py \
  tests/test_backlog_active_reset.py \
  tests/test_backlog_phase_service.py \
  tests/test_agent_workbench_backlog_phase.py \
  tests/test_backlog_primer_agent.py \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_command_schema.py \
  tests/test_roadmap_phase_service.py \
  tests/test_agent_workbench_story_phase.py \
  tests/test_agent_workbench_sprint_phase.py \
  -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_agent_workbench_backlog_phase.py tests/test_agent_workbench_cli.py
git commit -m "test: cover active backlog reset flow"
```

---

### Task 7: Verification And Manual caRtola Smoke

**Files:**
- No source edits expected.

- [ ] **Step 1: Run repository checks**

Run:

```bash
uv run --frozen pyrepo-check --all
```

Expected: all checks pass.

- [ ] **Step 2: Inspect command schema**

Run:

```bash
agileforge command schema "agileforge backlog reset-active"
```

Expected:

```text
ok: true
mutates: true
destructive: false
idempotency_required: true
required options include project_id, attempt_id, expected_artifact_fingerprint, expected_state, reset_reason, archive_all_active_stories, idempotency_key
```

- [ ] **Step 3: caRtola preflight only**

Run read-only commands:

```bash
agileforge status --project-id 2
agileforge backlog history --project-id 2
agileforge workflow next --project-id 2
```

Expected before reset:

```text
fsm_state: BACKLOG_REVIEW
product_backlog_assessment.attempt_id: backlog-attempt-12
product_backlog_assessment.is_complete: true
product_backlog_assessment.refinement_saveable: true
```

- [ ] **Step 4: caRtola reset smoke**

Stop here unless the user explicitly approves this caRtola DB mutation.

```bash
agileforge backlog reset-active \
  --project-id 2 \
  --attempt-id backlog-attempt-12 \
  --expected-artifact-fingerprint sha256:6ed088be18f559c5ca583cee647f296df64f114ef2794c2ee2de55da2067a8a5 \
  --expected-state BACKLOG_REVIEW \
  --reset-reason "pre-brownfield backlog reset" \
  --archive-all-active-stories \
  --idempotency-key reset-active-cartola-20260602
```

Expected:

```text
ok: true
fsm_state: BACKLOG_PERSISTENCE
archived_count equals old active story count
created_count equals approved refined backlog item count
downstream_backlog_stale: true
stale_backlog_reason: active_backlog_reset
stale_since_backlog_attempt_id: backlog-attempt-12
```

- [ ] **Step 5: caRtola post-reset read-only smoke**

Run:

```bash
agileforge workflow next --project-id 2
agileforge roadmap generate --project-id 2
agileforge sprint candidates --project-id 2
```

Expected:

```text
workflow next recommends roadmap generation
roadmap generate is not blocked by active_backlog_reset stale marker
sprint candidates or sprint flow remains blocked/stale until deferred stale-clearing slice
```

This dead-end is intentional for this slice. Do not report it as a regression.

- [ ] **Step 6: Final status check**

```bash
git status --short
```

Expected: clean working tree after all planned commits.

---

## Self-Review Checklist

- Spec coverage: all accepted requirements map to tasks:
  - archive columns: Task 1
  - event action filtering and reset fingerprint storage in `event_metadata`: Tasks 2 and 3
  - guarded reset mutation: Tasks 3 and 4
  - CLI/facade/schema: Task 4
  - roadmap-only stale exception and story/sprint block: Task 5
  - history preservation and idempotency: Tasks 3 and 6
- Reviewer additions captured:
  - reset request fingerprint stored in `WorkflowEvent.event_metadata`: Task 3
  - dedicated reset replay/conflict function: Task 3
  - `_latest_saved_count` counts only `action == "backlog_saved"`: Task 2
  - acknowledged post-reset dead-end for story/sprint: Tasks 5 and 7
- No plan step starts sprint planning, sprint execution, backlog save, staging, or commit against caRtola without explicit user approval.
- `reset-active` is framed as explicit replacement-guard override consuming an approved attempt, not direct edited-file persistence.
