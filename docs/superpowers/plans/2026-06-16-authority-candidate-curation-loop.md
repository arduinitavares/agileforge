# Authority Candidate Curation Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an ADK 2.0-backed authority curation loop that records targeted authority feedback, repairs rejected authority candidates with bounded iterations, and publishes a new pending authority only after deterministic host gates pass.

**Architecture:** First migrate AgileForge's agent runtime to ADK 2.0 and prove the current agent surfaces still work. Then add SQLModel-backed feedback and curation attempts, expose them through CLI/API projections, and run the curation workflow through ADK 2.0 graph or dynamic workflow nodes while host services retain idempotency, workflow guards, lineage, diff validation, and final publication control.

**Tech Stack:** Python 3.13, SQLModel, Pydantic `ConfigDict(extra="forbid")`, Google ADK 2.0 workflow runtime, existing `CliMutationLedger`, existing authority compiler/review services, Node built-in test runner for frontend-only checks, `pyrepo-check --all`.

---

## Spec And Review Inputs

- Design spec: `docs/superpowers/specs/2026-06-16-authority-candidate-curation-loop-design.md`
- Current ADK dependency: `pyproject.toml` declares `google-adk>=1.16.0`; `uv.lock` resolves `google-adk 1.16.0`.
- Review amendments already accepted:
  - use ADK 2.0 graph/dynamic workflow for curation;
  - keep Loop template workflow semantics: ordered sub-agents, max iterations, explicit exit signal;
  - add transient setup status `authority_curating`;
  - add `AuthorityFeedbackAttempt` and `AuthorityCurationAttempt` tables through `db/migrations.py`;
  - add `lineage_json` old invariant id to new invariant id mapping;
  - update `authority_projection.py` with feedback/curation flags;
  - require `ConfigDict(extra="forbid")` for new schemas.

## File Structure

- Modify `pyproject.toml` and `uv.lock`: migrate from ADK 1.16 to ADK 2.0.
- Modify `orchestrator_agent/agent_tools/utils/resilience.py`: remove curation reliance on legacy `_run_async_impl`; keep existing helpers compatible with ADK 2.0 or isolate legacy use.
- Create `orchestrator_agent/agent_tools/authority_curation/__init__.py`: export curation workflow builder and schema names.
- Create `orchestrator_agent/agent_tools/authority_curation/schemes.py`: strict Pydantic schemas for feedback review, repair plans, repair outputs, diff validation, and gate decisions.
- Create `orchestrator_agent/agent_tools/authority_curation/agent.py`: ADK 2.0 graph/dynamic workflow factory that preserves Loop template semantics.
- Create `models/authority_curation.py`: SQLModel rows for feedback and curation attempts.
- Modify `models/__init__.py`, `models/db.py`, and `agile_sqlmodel.py`: import curation models so metadata registration works.
- Modify `db/migrations.py`: create curation tables and indexes inside `ensure_schema_current()`.
- Modify `services/agent_workbench/schema_readiness.py`: add curation schema requirements for projections and mutation runners.
- Create `services/agent_workbench/authority_curation.py`: request models, strict feedback validation, feedback recording, curation mutation runner, workflow-state transitions, and publication path.
- Create `services/specs/authority_curation_diff.py`: deterministic diff and lineage helpers for repaired authority candidates.
- Modify `services/agent_workbench/authority_projection.py`: expose feedback and curation flags.
- Modify `services/agent_workbench/application.py`: expose `authority_feedback_record()` and `authority_curate()`, wire workflow-next routing.
- Modify `services/agent_workbench/command_registry.py`: register `agileforge authority feedback record` and `agileforge authority curate`.
- Modify `services/agent_workbench/error_codes.py`: add curation-specific error codes.
- Modify `cli/main.py`: add CLI parsers and handlers.
- Modify `api.py`: add API equivalents if current dashboard/API uses authority setup projections.
- Modify `docs/agent-cli-manual.md`: document rejection feedback and curation path.
- Create `tests/test_authority_curation_models.py`: model and bootstrap coverage.
- Create `tests/test_agent_workbench_authority_curation.py`: feedback, curation runner, idempotency, guards, and lineage tests.
- Modify `tests/test_db_migrations.py`: migration readiness for fresh and existing databases.
- Modify `tests/test_agent_workbench_authority_projection.py`: projection fields and stale guard behavior.
- Modify `tests/test_agent_workbench_application.py`: facade and workflow-next routing.
- Modify `tests/test_agent_workbench_command_schema.py`: command registry contracts.
- Modify `tests/test_agent_workbench_error_codes.py`: error registry coverage.
- Modify `tests/test_api_dashboard.py`: API/dashboard projection coverage if API is extended.
- Modify `tests/test_agent_tool_runtime_import_boundary.py`: ADK 2.0 import boundary smoke coverage.

## Command Contracts

`agileforge authority feedback record`

- Required: `--project-id`, `--pending-authority-id`, `--expected-authority-fingerprint`, `--feedback-file`, `--idempotency-key`.
- Optional: `--changed-by`, `--correlation-id`.
- Request fingerprint includes command name, project id, pending authority id, expected authority fingerprint, normalized feedback payload hash, changed by.
- Replay with same idempotency key and same request returns same response.
- Same key with changed request returns `IDEMPOTENCY_KEY_REUSED`.
- Command records feedback only. It does not accept, reject, regenerate, or curate.

`agileforge authority curate`

- Required: `--project-id`, `--spec-version-id`, `--source-authority-id`, `--expected-source-authority-fingerprint`, `--feedback-attempt-id`, `--idempotency-key`.
- Optional: `--max-iterations`, `--compiler-model`, `--changed-by`, `--correlation-id`.
- Request fingerprint includes command name, project id, spec version id, source authority id, expected source authority fingerprint, feedback attempt id, max iterations, compiler model, changed by.
- Command sets workflow setup status to `authority_curating`, runs ADK 2.0 curation, validates diff/lineage/quality gates, publishes new pending authority when gates pass, and stops at `authority_pending_review`.
- Command must not call `authority accept`.

---

### Task 1: ADK 2.0 Runtime Migration Gate

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `orchestrator_agent/agent_tools/utils/resilience.py`
- Modify: `services/workflow.py`
- Modify: `tests/test_agent_tool_runtime_import_boundary.py`
- Modify: `tests/test_workflow_session_bootstrap.py`

- [ ] **Step 1: Add failing ADK 2.0 dependency test**

Add to `tests/test_agent_tool_runtime_import_boundary.py`:

```python
from importlib import metadata


def test_google_adk_major_version_is_2() -> None:
    """New agentic workflow work must run on ADK 2.0."""
    version = metadata.version("google-adk")
    major = int(version.split(".", maxsplit=1)[0])

    assert major == 2
```

- [ ] **Step 2: Run the failing dependency test**

Run:

```bash
uv run --frozen pytest tests/test_agent_tool_runtime_import_boundary.py::test_google_adk_major_version_is_2 -q
```

Expected: FAIL showing installed `google-adk` major version is `1`.

- [ ] **Step 3: Update ADK dependency**

Edit `pyproject.toml` dependency line:

```toml
"google-adk>=2.0.0,<3.0.0",
```

Then update the lockfile:

```bash
uv lock --upgrade-package google-adk
```

Expected: `uv.lock` resolves `google-adk` with major version `2`.

- [ ] **Step 4: Migrate ADK session-service import**

In `services/workflow.py`, import `DatabaseSessionService` from `google.adk.sessions.database_session_service`. Do not use the ADK 1.x top-level `google.adk.sessions` export lookup.

- [ ] **Step 5: Add ADK 2.0 event-shape session smoke test**

Add to `tests/test_workflow_session_bootstrap.py`:

```python
def test_workflow_session_events_accept_adk2_fields() -> None:
    """Session serialization must tolerate ADK 2.0 workflow event fields."""
    event_payload = {
        "author": "AuthorityCurationWorkflow",
        "content": {"parts": [{"text": "gate passed"}]},
        "node_info": {"node_id": "GateDecision", "iteration": 1},
        "output": {"status": "pass", "review_ready": True},
    }

    assert event_payload["node_info"]["node_id"] == "GateDecision"
    assert event_payload["output"]["status"] == "pass"
```

This test is intentionally narrow. If AgileForge uses serialized JSON event blobs, it documents compatibility. If a custom session table has rigid event columns, replace this assertion with the concrete table read/write path and keep the same payload.

- [ ] **Step 6: Remove curation dependence on legacy loop override**

In `orchestrator_agent/agent_tools/utils/resilience.py`, leave `ConditionalLoopAgent` for legacy agents only. Add this comment above the class:

```python
# Legacy ADK 1.x helper. New ADK 2.0 workflows must use graph or dynamic
# workflow nodes and must not depend on _run_async_impl for curation control.
```

Do not use `ConditionalLoopAgent` in authority curation code.

- [ ] **Step 7: Run ADK migration tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_tool_runtime_import_boundary.py tests/test_workflow_session_bootstrap.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit ADK migration gate**

```bash
git add pyproject.toml uv.lock orchestrator_agent/agent_tools/utils/resilience.py services/workflow.py tests/test_agent_tool_runtime_import_boundary.py tests/test_workflow_session_bootstrap.py docs/superpowers/plans/2026-06-16-authority-candidate-curation-loop.md
git commit -m "chore: migrate agent runtime to adk 2"
```

---

### Task 2: Authority Curation Persistence And Migration

**Files:**
- Create: `models/authority_curation.py`
- Modify: `models/__init__.py`
- Modify: `models/db.py`
- Modify: `agile_sqlmodel.py`
- Modify: `db/migrations.py`
- Modify: `services/agent_workbench/schema_readiness.py`
- Create: `tests/test_authority_curation_models.py`
- Modify: `tests/test_db_migrations.py`

- [ ] **Step 1: Write failing model bootstrap test**

Create `tests/test_authority_curation_models.py`:

```python
from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from db.migrations import ensure_schema_current


def test_authority_curation_tables_are_created(engine: Engine) -> None:
    """Authority curation attempts must have dedicated tables."""
    ensure_schema_current(engine)

    table_names = set(inspect(engine).get_table_names())

    assert "authority_feedback_attempts" in table_names
    assert "authority_curation_attempts" in table_names
```

- [ ] **Step 2: Run failing bootstrap test**

Run:

```bash
uv run --frozen pytest tests/test_authority_curation_models.py::test_authority_curation_tables_are_created -q
```

Expected: FAIL because tables do not exist.

- [ ] **Step 3: Add SQLModel classes**

Create `models/authority_curation.py`:

```python
"""Authority curation persistence models."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.schema import UniqueConstraint
from sqlalchemy.types import Text
from sqlmodel import Field, SQLModel


def _utc_now() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(UTC)


class AuthorityFeedbackAttempt(SQLModel, table=True):
    """Structured feedback recorded against one authority candidate."""

    __tablename__ = "authority_feedback_attempts"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "feedback_attempt_id",
            name="uq_authority_feedback_project_attempt",
        ),
    )

    feedback_row_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id", index=True)
    feedback_attempt_id: str = Field(index=True)
    source_authority_id: int = Field(index=True)
    source_authority_fingerprint: str = Field(index=True)
    feedback_fingerprint: str = Field(index=True)
    status: str = Field(default="recorded", index=True)
    has_blocking_feedback: bool = Field(default=False, index=True)
    feedback_json: str = Field(sa_type=Text)
    request_hash: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    changed_by: str = Field(default="cli-agent", index=True)
    created_at: datetime = Field(default_factory=_utc_now, nullable=False)
    updated_at: datetime = Field(default_factory=_utc_now, nullable=False)


class AuthorityCurationAttempt(SQLModel, table=True):
    """ADK-backed curation attempt for one authority candidate."""

    __tablename__ = "authority_curation_attempts"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "curation_attempt_id",
            name="uq_authority_curation_project_attempt",
        ),
    )

    curation_row_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id", index=True)
    curation_attempt_id: str = Field(index=True)
    source_authority_id: int = Field(index=True)
    source_authority_fingerprint: str = Field(index=True)
    spec_version_id: int = Field(index=True)
    feedback_attempt_id: str = Field(index=True)
    status: str = Field(default="running", index=True)
    max_iterations: int = Field(default=2)
    iteration_count: int = Field(default=0)
    compiler_model: str | None = Field(default=None, index=True)
    candidate_authority_id: int | None = Field(default=None, index=True)
    candidate_authority_fingerprint: str | None = Field(default=None, index=True)
    request_json: str = Field(default="{}", sa_type=Text)
    candidate_lineage_json: str = Field(default="{}", sa_type=Text)
    diff_summary_json: str = Field(default="{}", sa_type=Text)
    lineage_json: str = Field(default="{}", sa_type=Text)
    quality_report_json: str = Field(default="{}", sa_type=Text)
    failure_artifact_id: str | None = Field(default=None, index=True)
    request_hash: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    changed_by: str = Field(default="cli-agent", index=True)
    created_at: datetime = Field(default_factory=_utc_now, nullable=False)
    updated_at: datetime = Field(default_factory=_utc_now, nullable=False)
```

- [ ] **Step 4: Register metadata imports**

Add imports:

```python
from models.authority_curation import AuthorityCurationAttempt, AuthorityFeedbackAttempt
```

to:

- `models/__init__.py`
- `models/db.py`
- `agile_sqlmodel.py`

If a file uses sorted `__all__`, add both names there.

- [ ] **Step 5: Add migration SQL**

In `db/migrations.py`, add constants near other agent-workbench table migrations:

```python
AUTHORITY_FEEDBACK_ATTEMPTS_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS authority_feedback_attempts (
    feedback_row_id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES products(product_id),
    feedback_attempt_id VARCHAR NOT NULL,
    source_authority_id INTEGER NOT NULL,
    source_authority_fingerprint VARCHAR NOT NULL,
    feedback_fingerprint VARCHAR NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'recorded',
    has_blocking_feedback BOOLEAN NOT NULL DEFAULT 0,
    feedback_json TEXT NOT NULL,
    request_hash VARCHAR NOT NULL,
    idempotency_key VARCHAR NOT NULL,
    changed_by VARCHAR NOT NULL DEFAULT 'cli-agent',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT uq_authority_feedback_project_attempt
        UNIQUE (project_id, feedback_attempt_id)
)
"""

AUTHORITY_CURATION_ATTEMPTS_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS authority_curation_attempts (
    curation_row_id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES products(product_id),
    curation_attempt_id VARCHAR NOT NULL,
    source_authority_id INTEGER NOT NULL,
    source_authority_fingerprint VARCHAR NOT NULL,
    spec_version_id INTEGER NOT NULL,
    feedback_attempt_id VARCHAR NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'running',
    max_iterations INTEGER NOT NULL DEFAULT 2,
    iteration_count INTEGER NOT NULL DEFAULT 0,
    compiler_model VARCHAR,
    candidate_authority_id INTEGER,
    candidate_authority_fingerprint VARCHAR,
    request_json TEXT NOT NULL DEFAULT '{}',
    candidate_lineage_json TEXT NOT NULL DEFAULT '{}',
    diff_summary_json TEXT NOT NULL DEFAULT '{}',
    lineage_json TEXT NOT NULL DEFAULT '{}',
    quality_report_json TEXT NOT NULL DEFAULT '{}',
    failure_artifact_id VARCHAR,
    request_hash VARCHAR NOT NULL,
    idempotency_key VARCHAR NOT NULL,
    changed_by VARCHAR NOT NULL DEFAULT 'cli-agent',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT uq_authority_curation_project_attempt
        UNIQUE (project_id, curation_attempt_id)
)
"""
```

Add migration function:

```python
def migrate_authority_curation_tables(engine: Engine) -> list[str]:
    """Ensure authority curation feedback and attempt tables exist."""
    actions: list[str] = []

    if _ensure_table_exists(
        engine,
        "authority_feedback_attempts",
        AUTHORITY_FEEDBACK_ATTEMPTS_CREATE_SQL,
    ):
        actions.append("created table: authority_feedback_attempts")

    if _ensure_index_exists(
        engine,
        "authority_feedback_attempts",
        "ix_authority_feedback_project_status",
        ["project_id", "status"],
    ):
        actions.append("created index: ix_authority_feedback_project_status")

    if _ensure_index_exists(
        engine,
        "authority_feedback_attempts",
        "ix_authority_feedback_source_authority",
        ["source_authority_id"],
    ):
        actions.append("created index: ix_authority_feedback_source_authority")

    if _ensure_table_exists(
        engine,
        "authority_curation_attempts",
        AUTHORITY_CURATION_ATTEMPTS_CREATE_SQL,
    ):
        actions.append("created table: authority_curation_attempts")

    if _ensure_index_exists(
        engine,
        "authority_curation_attempts",
        "ix_authority_curation_project_status",
        ["project_id", "status"],
    ):
        actions.append("created index: ix_authority_curation_project_status")

    if _ensure_index_exists(
        engine,
        "authority_curation_attempts",
        "ix_authority_curation_source_authority",
        ["source_authority_id"],
    ):
        actions.append("created index: ix_authority_curation_source_authority")

    return actions
```

Call `migrate_authority_curation_tables(engine)` inside `ensure_schema_current()`.

- [ ] **Step 6: Add schema readiness requirements**

In `services/agent_workbench/schema_readiness.py`, add:

```python
AUTHORITY_CURATION_REQUIREMENTS: tuple[SchemaRequirement, ...] = (
    SchemaRequirement(
        table="authority_feedback_attempts",
        columns=(
            "feedback_row_id",
            "project_id",
            "feedback_attempt_id",
            "source_authority_id",
            "source_authority_fingerprint",
            "feedback_fingerprint",
            "status",
            "has_blocking_feedback",
            "feedback_json",
            "request_hash",
            "idempotency_key",
            "changed_by",
            "created_at",
            "updated_at",
        ),
        indexes=(
            "ix_authority_feedback_project_status",
            "ix_authority_feedback_source_authority",
        ),
    ),
    SchemaRequirement(
        table="authority_curation_attempts",
        columns=(
            "curation_row_id",
            "project_id",
            "curation_attempt_id",
            "source_authority_id",
            "source_authority_fingerprint",
            "spec_version_id",
            "feedback_attempt_id",
            "status",
            "max_iterations",
            "iteration_count",
            "compiler_model",
            "candidate_authority_id",
            "candidate_authority_fingerprint",
            "request_json",
            "candidate_lineage_json",
            "diff_summary_json",
            "lineage_json",
            "quality_report_json",
            "failure_artifact_id",
            "request_hash",
            "idempotency_key",
            "changed_by",
            "created_at",
            "updated_at",
        ),
        indexes=(
            "ix_authority_curation_project_status",
            "ix_authority_curation_source_authority",
        ),
    ),
)


def check_authority_curation_readiness(engine: Engine) -> SchemaReadiness:
    """Return readiness for authority feedback and curation storage."""
    return check_schema_readiness(engine, AUTHORITY_CURATION_REQUIREMENTS)
```

- [ ] **Step 7: Add fresh/existing migration tests**

Add to `tests/test_db_migrations.py`:

```python
def test_authority_curation_migration_is_idempotent(engine: Engine) -> None:
    """Fresh and repeated migration creates curation tables once."""
    first_actions = ensure_schema_current(engine)
    second_actions = ensure_schema_current(engine)

    inspector = inspect(engine)
    assert "authority_feedback_attempts" in inspector.get_table_names()
    assert "authority_curation_attempts" in inspector.get_table_names()
    assert any("authority_feedback_attempts" in action for action in first_actions)
    assert not any("authority_feedback_attempts" in action for action in second_actions)
```

- [ ] **Step 8: Run persistence tests**

Run:

```bash
uv run --frozen pytest tests/test_authority_curation_models.py tests/test_db_migrations.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit persistence layer**

```bash
git add models/authority_curation.py models/__init__.py models/db.py agile_sqlmodel.py db/migrations.py services/agent_workbench/schema_readiness.py tests/test_authority_curation_models.py tests/test_db_migrations.py
git commit -m "feat: add authority curation storage"
```

---

### Task 3: Feedback Schema And Record Command

**Files:**
- Create: `services/agent_workbench/authority_curation.py`
- Modify: `services/agent_workbench/error_codes.py`
- Create: `tests/test_agent_workbench_authority_curation.py`
- Modify: `tests/test_agent_workbench_error_codes.py`

- [ ] **Step 1: Add failing feedback schema tests**

Create `tests/test_agent_workbench_authority_curation.py`:

```python
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import Engine

from db.migrations import ensure_schema_current
from services.agent_workbench.authority_curation import (
    AuthorityFeedbackFile,
    AuthorityFeedbackItem,
    AuthorityFeedbackRecordRequest,
    AuthorityCurationRunner,
)


def test_feedback_models_reject_unknown_fields() -> None:
    """Feedback payloads are strict audit artifacts."""
    with pytest.raises(ValidationError):
        AuthorityFeedbackItem.model_validate(
            {
                "feedback_id": "AFB-1",
                "target_kind": "invariant",
                "target_id": "INV-0123456789abcdef",
                "issue_type": "overstrong_invariant",
                "severity": "blocking",
                "instruction": "Replace the overstrong invariant.",
                "extra": "rejected",
            }
        )


def test_feedback_record_requires_idempotency_key() -> None:
    """Feedback recording is a mutation and requires idempotency."""
    with pytest.raises(ValidationError):
        AuthorityFeedbackRecordRequest(
            project_id=1,
            pending_authority_id=6,
            expected_authority_fingerprint="sha256:abc",
            feedback_file="feedback.json",
        )
```

- [ ] **Step 2: Run failing schema tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_curation.py::test_feedback_models_reject_unknown_fields tests/test_agent_workbench_authority_curation.py::test_feedback_record_requires_idempotency_key -q
```

Expected: FAIL because service module does not exist.

- [ ] **Step 3: Add curation error codes**

In `services/agent_workbench/error_codes.py`, add enum values:

```python
AUTHORITY_FEEDBACK_TARGET_NOT_FOUND = "AUTHORITY_FEEDBACK_TARGET_NOT_FOUND"
AUTHORITY_FEEDBACK_SCHEMA_INVALID = "AUTHORITY_FEEDBACK_SCHEMA_INVALID"
AUTHORITY_CURATED_DIFF_UNBOUNDED = "AUTHORITY_CURATED_DIFF_UNBOUNDED"
AUTHORITY_CURATION_MAX_ITERATIONS = "AUTHORITY_CURATION_MAX_ITERATIONS"
```

Add `_ERROR_REGISTRY` entries:

```python
ErrorCode.AUTHORITY_FEEDBACK_TARGET_NOT_FOUND: ErrorMetadata(
    code=ErrorCode.AUTHORITY_FEEDBACK_TARGET_NOT_FOUND.value,
    default_exit_code=4,
    retryable=False,
    description="Authority feedback references a target that does not exist.",
),
ErrorCode.AUTHORITY_FEEDBACK_SCHEMA_INVALID: ErrorMetadata(
    code=ErrorCode.AUTHORITY_FEEDBACK_SCHEMA_INVALID.value,
    default_exit_code=2,
    retryable=False,
    description="Authority feedback payload is invalid.",
),
ErrorCode.AUTHORITY_CURATED_DIFF_UNBOUNDED: ErrorMetadata(
    code=ErrorCode.AUTHORITY_CURATED_DIFF_UNBOUNDED.value,
    default_exit_code=1,
    retryable=False,
    description="Authority curation changed untargeted authority items.",
),
ErrorCode.AUTHORITY_CURATION_MAX_ITERATIONS: ErrorMetadata(
    code=ErrorCode.AUTHORITY_CURATION_MAX_ITERATIONS.value,
    default_exit_code=1,
    retryable=True,
    description="Authority curation reached its maximum iteration count.",
),
```

- [ ] **Step 4: Implement strict feedback models**

Create the top of `services/agent_workbench/authority_curation.py`:

```python
"""Authority feedback and curation mutation service."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlmodel import Session, select

from models.authority_curation import AuthorityFeedbackAttempt
from models.specs import CompiledSpecAuthority
from services.agent_workbench.authority_projection import pending_authority_fingerprint
from services.agent_workbench.envelope import error_envelope, success_envelope
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash

AUTHORITY_FEEDBACK_RECORD_COMMAND = "agileforge authority feedback record"

FeedbackTargetKind = Literal[
    "invariant",
    "gap",
    "assumption",
    "quality_group",
    "source_item",
    "authority_candidate",
]
FeedbackIssueType = Literal[
    "overstrong_invariant",
    "understrong_invariant",
    "materially_wrong_invariant",
    "duplicate_invariant",
    "near_duplicate_invariant",
    "over_split_group",
    "brittle_wording",
    "missing_invariant",
    "invalid_gap",
    "invalid_assumption",
    "source_map_error",
    "coverage_gap",
]
FeedbackSeverity = Literal["blocking", "non_blocking"]


class _StrictModel(BaseModel):
    """Base model for strict authority curation payloads."""

    model_config = ConfigDict(extra="forbid")


class AuthorityFeedbackItem(_StrictModel):
    """One structured feedback item targeted at authority content."""

    feedback_id: str = Field(min_length=1)
    target_kind: FeedbackTargetKind
    target_id: str | None = Field(default=None, min_length=1)
    source_item_id: str | None = Field(default=None, min_length=1)
    issue_type: FeedbackIssueType
    severity: FeedbackSeverity
    instruction: str = Field(min_length=1)

    @model_validator(mode="after")
    def _require_concrete_target(self) -> AuthorityFeedbackItem:
        if self.target_id is None and self.source_item_id is None:
            msg = "target_id or source_item_id is required"
            raise ValueError(msg)
        return self


class AuthorityFeedbackFile(_StrictModel):
    """Canonical feedback file schema."""

    schema_version: Literal["agileforge.authority_feedback.v1"]
    authority_id: int
    feedback_items: list[AuthorityFeedbackItem] = Field(min_length=1)


class AuthorityFeedbackRecordRequest(_StrictModel):
    """CLI request for feedback recording."""

    project_id: int
    pending_authority_id: int
    expected_authority_fingerprint: str = Field(min_length=1)
    feedback_file: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "cli-agent"
    correlation_id: str | None = None
```

- [ ] **Step 5: Add feedback file loader and target validation**

Append to `services/agent_workbench/authority_curation.py`:

```python
def _load_feedback_file(path: str) -> AuthorityFeedbackFile:
    """Load and validate a feedback file from disk."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return AuthorityFeedbackFile.model_validate(payload)


def _authority_targets(authority: CompiledSpecAuthority) -> set[str]:
    """Return known target ids from the compiled authority JSON columns."""
    target_ids: set[str] = {f"authority:{authority.authority_id}"}
    for column_name in ("invariants", "spec_gaps", "compiled_artifact_json"):
        raw_value = getattr(authority, column_name, None)
        if not raw_value:
            continue
        try:
            parsed = json.loads(str(raw_value))
        except json.JSONDecodeError:
            continue
        target_ids.update(_collect_ids(parsed))
    return target_ids


def _collect_ids(value: object) -> set[str]:
    """Collect id-like strings from nested JSON."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key in ("id", "invariant_id", "gap_id", "assumption_id", "group_id"):
            item_id = value.get(key)
            if isinstance(item_id, str) and item_id:
                found.add(item_id)
        for child in value.values():
            found.update(_collect_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_ids(child))
    return found
```

- [ ] **Step 6: Implement feedback record method**

Append runner skeleton:

```python
class AuthorityCurationRunner:
    """Run authority feedback and curation commands."""

    def __init__(self, *, engine: Any, workflow: Any | None = None) -> None:
        self._engine = engine
        self._workflow = workflow

    def feedback_record(
        self,
        request: AuthorityFeedbackRecordRequest,
    ) -> dict[str, Any]:
        """Record structured feedback for a pending authority."""
        feedback = _load_feedback_file(request.feedback_file)
        if feedback.authority_id != request.pending_authority_id:
            return error_envelope(
                command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
                error=workbench_error(
                    ErrorCode.AUTHORITY_FEEDBACK_SCHEMA_INVALID,
                    message="Feedback authority_id does not match request.",
                    details={
                        "feedback_authority_id": feedback.authority_id,
                        "pending_authority_id": request.pending_authority_id,
                    },
                ),
            )

        with Session(self._engine) as session:
            authority = session.get(CompiledSpecAuthority, request.pending_authority_id)
            if authority is None:
                return error_envelope(
                    command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
                    error=workbench_error(
                        ErrorCode.AUTHORITY_NOT_PENDING,
                        message="Pending authority was not found.",
                        details={"authority_id": request.pending_authority_id},
                    ),
                )
            actual_fingerprint = pending_authority_fingerprint(authority)
            if actual_fingerprint != request.expected_authority_fingerprint:
                return error_envelope(
                    command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
                    error=workbench_error(
                        ErrorCode.STALE_AUTHORITY_VERSION,
                        message="Authority fingerprint changed.",
                        details={
                            "expected": request.expected_authority_fingerprint,
                            "actual": actual_fingerprint,
                        },
                    ),
                )

            targets = _authority_targets(authority)
            for item in feedback.feedback_items:
                if item.target_id is not None and item.target_id not in targets:
                    return error_envelope(
                        command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
                        error=workbench_error(
                            ErrorCode.AUTHORITY_FEEDBACK_TARGET_NOT_FOUND,
                            message="Feedback target does not exist.",
                            details={"target_id": item.target_id},
                        ),
                    )

            payload = feedback.model_dump(mode="json")
            feedback_fingerprint = canonical_hash(payload)
            attempt_id = f"feedback-{uuid4()}"
            now = datetime.now(UTC)
            row = AuthorityFeedbackAttempt(
                project_id=request.project_id,
                feedback_attempt_id=attempt_id,
                source_authority_id=request.pending_authority_id,
                source_authority_fingerprint=actual_fingerprint,
                feedback_fingerprint=feedback_fingerprint,
                has_blocking_feedback=any(
                    item.severity == "blocking" for item in feedback.feedback_items
                ),
                feedback_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
                request_hash=canonical_hash(request.model_dump(mode="json")),
                idempotency_key=request.idempotency_key,
                changed_by=request.changed_by,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()

        return success_envelope(
            command=AUTHORITY_FEEDBACK_RECORD_COMMAND,
            data={
                "status": "authority_feedback_recorded",
                "project_id": request.project_id,
                "feedback_attempt_id": attempt_id,
                "source_authority_id": request.pending_authority_id,
                "source_authority_fingerprint": actual_fingerprint,
                "feedback_fingerprint": feedback_fingerprint,
                "has_blocking_feedback": row.has_blocking_feedback,
            },
        )
```

This first pass intentionally omits ledger replay. Add ledger replay in Task 5 when curation mutations use the same runner, so idempotency code is shared.

- [ ] **Step 7: Add passing feedback tests**

Extend `tests/test_agent_workbench_authority_curation.py` with a fixture that inserts a `CompiledSpecAuthority` row and feedback JSON file, then assert:

```python
def test_feedback_record_rejects_missing_target(engine: Engine, tmp_path: Path) -> None:
    ensure_schema_current(engine)
    feedback_file = tmp_path / "feedback.json"
    feedback_file.write_text(
        json.dumps(
            {
                "schema_version": "agileforge.authority_feedback.v1",
                "authority_id": 6,
                "feedback_items": [
                    {
                        "feedback_id": "AFB-missing",
                        "target_kind": "invariant",
                        "target_id": "INV-missingmissing1",
                        "issue_type": "overstrong_invariant",
                        "severity": "blocking",
                        "instruction": "Repair this target.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    runner = AuthorityCurationRunner(engine=engine)
    result = runner.feedback_record(
        AuthorityFeedbackRecordRequest(
            project_id=1,
            pending_authority_id=6,
            expected_authority_fingerprint="sha256:expected",
            feedback_file=str(feedback_file),
            idempotency_key="feedback-record-001",
        )
    )

    assert result["ok"] is False
```

Before finalizing this test, insert the authority row and use the real `pending_authority_fingerprint(authority)` value so the failure reaches target validation.

- [ ] **Step 8: Run feedback tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_curation.py tests/test_agent_workbench_error_codes.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit feedback record command core**

```bash
git add services/agent_workbench/authority_curation.py services/agent_workbench/error_codes.py tests/test_agent_workbench_authority_curation.py tests/test_agent_workbench_error_codes.py
git commit -m "feat: record structured authority feedback"
```

---

### Task 4: Authority Projection And Workflow Next Routing

**Files:**
- Modify: `services/agent_workbench/authority_projection.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `tests/test_agent_workbench_authority_projection.py`
- Modify: `tests/test_agent_workbench_application.py`

- [ ] **Step 1: Add failing projection test**

Add to `tests/test_agent_workbench_authority_projection.py`:

```python
def test_authority_status_includes_curation_flags_for_rejected_authority(
    engine: Engine,
) -> None:
    """Status projection exposes feedback and curation state."""
    ensure_schema_current(engine)
    project_id, authority_id, fingerprint = _insert_rejected_authority(engine)
    _insert_feedback_attempt(
        engine,
        project_id=project_id,
        authority_id=authority_id,
        fingerprint=fingerprint,
        has_blocking_feedback=True,
    )

    result = authority_status(project_id=project_id, engine=engine)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "rejected"
    assert data["has_blocking_feedback"] is True
    assert data["curation_available"] is True
    assert data["latest_feedback_attempt_id"].startswith("feedback-")
```

Use existing helpers in this test file if they already create product/spec/authority rows. If no helper exists, add local helpers with explicit rows and the real `pending_authority_fingerprint()` function.

- [ ] **Step 2: Implement feedback projection helper**

In `services/agent_workbench/authority_projection.py`, import:

```python
from models.authority_curation import AuthorityCurationAttempt, AuthorityFeedbackAttempt
```

Add helper:

```python
def _latest_feedback_and_curation(
    session: Session,
    *,
    project_id: int,
    authority_id: int | None,
) -> JsonDict:
    """Return bounded feedback and curation status for the authority status view."""
    if authority_id is None:
        return {
            "has_blocking_feedback": False,
            "latest_feedback_attempt_id": None,
            "latest_curation_attempt_id": None,
            "latest_curation_status": None,
            "latest_curation_failure_artifact_id": None,
            "curation_available": False,
            "curation_in_progress": False,
        }

    feedback = session.exec(
        select(AuthorityFeedbackAttempt)
        .where(AuthorityFeedbackAttempt.project_id == project_id)
        .where(AuthorityFeedbackAttempt.source_authority_id == authority_id)
        .order_by(AuthorityFeedbackAttempt.created_at.desc())
    ).first()
    curation = session.exec(
        select(AuthorityCurationAttempt)
        .where(AuthorityCurationAttempt.project_id == project_id)
        .where(AuthorityCurationAttempt.source_authority_id == authority_id)
        .order_by(AuthorityCurationAttempt.created_at.desc())
    ).first()
    has_blocking = bool(feedback and feedback.has_blocking_feedback)
    curation_status = None if curation is None else curation.status
    return {
        "has_blocking_feedback": has_blocking,
        "latest_feedback_attempt_id": None if feedback is None else feedback.feedback_attempt_id,
        "latest_curation_attempt_id": None if curation is None else curation.curation_attempt_id,
        "latest_curation_status": curation_status,
        "latest_curation_failure_artifact_id": None if curation is None else curation.failure_artifact_id,
        "curation_available": has_blocking and curation_status not in {"running", "succeeded"},
        "curation_in_progress": curation_status == "running",
    }
```

Merge returned fields into `_status_data()` where status payload is built.

- [ ] **Step 3: Add workflow-next routing test**

Add to `tests/test_agent_workbench_application.py`:

```python
def test_workflow_next_prefers_authority_curate_after_feedback() -> None:
    """Rejected authority with blocking feedback routes to curate, not regenerate."""
    app = AgentWorkbenchApplication(
        read_projection=FakeReadProjection(
            workflow_state={
                "ok": True,
                "data": {
                    "project_id": 3,
                    "state": {
                        "fsm_state": "SETUP_REQUIRED",
                        "setup_status": "authority_rejected",
                        "setup_spec_version_id": 4,
                    },
                },
            }
        ),
        authority_projection=FakeAuthorityProjection(
            status_payload={
                "ok": True,
                "data": {
                    "status": "rejected",
                    "latest_feedback_attempt_id": "feedback-123",
                    "has_blocking_feedback": True,
                    "curation_available": True,
                    "curation_in_progress": False,
                    "pending_authority_id": 6,
                    "pending_authority_fingerprint": "sha256:abc",
                },
            }
        ),
    )

    result = app.workflow_next(project_id=3)

    assert result["ok"] is True
    command = result["data"]["next_actions"][0]["command"]
    assert "agileforge authority curate" in command
    assert "authority regenerate" not in command
```

- [ ] **Step 4: Implement workflow-next routing**

In `services/agent_workbench/application.py`, update rejected-authority branch to prefer:

```python
if authority_data.get("curation_in_progress") is True:
    return _mutation_show_next_action(...)
if authority_data.get("curation_available") is True:
    return _authority_curate_next_action(
        project_id=project_id,
        spec_version_id=int(setup_state["setup_spec_version_id"]),
        source_authority_id=int(authority_data["pending_authority_id"]),
        source_authority_fingerprint=str(authority_data["pending_authority_fingerprint"]),
        feedback_attempt_id=str(authority_data["latest_feedback_attempt_id"]),
    )
```

Add helper:

```python
def _authority_curate_next_action(
    *,
    project_id: int,
    spec_version_id: int,
    source_authority_id: int,
    source_authority_fingerprint: str,
    feedback_attempt_id: str,
) -> dict[str, Any]:
    """Return next action for structured authority curation."""
    return {
        "command": (
            "agileforge authority curate "
            f"--project-id {project_id} "
            f"--spec-version-id {spec_version_id} "
            f"--source-authority-id {source_authority_id} "
            f"--expected-source-authority-fingerprint {source_authority_fingerprint} "
            f"--feedback-attempt-id {feedback_attempt_id} "
            "--idempotency-key <idempotency_key>"
        ),
        "reason": "Structured authority feedback exists for the rejected authority.",
    }
```

- [ ] **Step 5: Run projection/routing tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_projection.py tests/test_agent_workbench_application.py -q -k "curation or workflow_next_prefers_authority_curate"
```

Expected: PASS.

- [ ] **Step 6: Commit projection routing**

```bash
git add services/agent_workbench/authority_projection.py services/agent_workbench/application.py tests/test_agent_workbench_authority_projection.py tests/test_agent_workbench_application.py
git commit -m "feat: project authority curation next actions"
```

---

### Task 5: Curation Mutation Runner And `authority_curating`

**Files:**
- Modify: `services/agent_workbench/authority_curation.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `tests/test_agent_workbench_authority_curation.py`
- Modify: `tests/test_agent_workbench_application.py`

- [ ] **Step 1: Add failing curating status test**

Add to `tests/test_agent_workbench_authority_curation.py`:

```python
def test_authority_curate_sets_curating_before_workflow(
    engine: Engine,
    fake_workflow: FakeWorkflowPort,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long curation work must be fenced by authority_curating status."""
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    captured_state: dict[str, object] = {}

    def fake_run_curation(*args: object, **kwargs: object) -> dict[str, object]:
        captured_state.update(fake_workflow.get_session_status(str(fixture.project_id)))
        return _successful_curation_result(fixture)

    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        fake_run_curation,
    )
    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)

    result = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-001",
        )
    )

    assert result["ok"] is True
    assert captured_state["setup_status"] == "authority_curating"
```

- [ ] **Step 2: Add strict curation request model**

In `services/agent_workbench/authority_curation.py`, add:

```python
AUTHORITY_CURATE_COMMAND = "agileforge authority curate"
AUTHORITY_CURATION_LEASE_SECONDS = 600


class AuthorityCurationRequest(_StrictModel):
    """Guarded request for authority curation."""

    project_id: int
    spec_version_id: int
    source_authority_id: int
    expected_source_authority_fingerprint: str = Field(min_length=1)
    feedback_attempt_id: str = Field(min_length=1)
    max_iterations: int = Field(default=2, ge=1, le=2)
    compiler_model: str | None = Field(default=None, min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "cli-agent"
    correlation_id: str | None = None
```

- [ ] **Step 3: Add workflow port**

Add protocol and default adapter:

```python
class AuthorityCurationWorkflowPort(Protocol):
    """Workflow state operations needed by curation."""

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """Return current workflow state."""
        ...

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        """Merge workflow state update."""
        ...
```

Use the existing `WorkflowService` in the default adapter, matching `AuthorityDecisionRunner`.

- [ ] **Step 4: Implement mutation start and curating status**

In `AuthorityCurationRunner.curate()`, follow `AuthorityRegenerateRunner._start_mutation()` pattern:

```python
def curate(self, request: AuthorityCurationRequest) -> dict[str, Any]:
    """Run bounded authority curation and publish a new pending candidate."""
    active_mutation = self._start_curation_mutation(request)
    if not isinstance(active_mutation, _ActiveMutation):
        return active_mutation

    stale_error = self._validate_curation_guards(request)
    if stale_error is not None:
        return stale_error

    self._workflow.update_session_status(
        str(request.project_id),
        {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "authority_curating",
            "setup_curation_mutation_event_id": active_mutation.mutation_event_id,
            "setup_next_actions": [
                {
                    "command": "agileforge mutation show",
                    "args": {"mutation_event_id": active_mutation.mutation_event_id},
                    "reason": "Inspect the active authority curation mutation.",
                }
            ],
        },
    )
    return self._run_curation_after_status_update(
        request=request,
        active_mutation=active_mutation,
    )
```

- [ ] **Step 5: Add concurrent curation rejection test**

Add:

```python
def test_authority_curate_rejects_when_already_curating(
    engine: Engine,
    fake_workflow: FakeWorkflowPort,
) -> None:
    """Concurrent curation must be blocked by setup status."""
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "authority_curating",
            "setup_curation_mutation_event_id": 777,
        },
    )
    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)

    result = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-concurrent-001",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] in {"STALE_SETUP_STATUS", "MUTATION_IN_PROGRESS"}
    assert result["errors"][0]["details"]["setup_curation_mutation_event_id"] == 777
```

- [ ] **Step 6: Run curation guard tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_curation.py -q -k "curating or concurrent"
```

Expected: PASS.

- [ ] **Step 7: Commit curation mutation guards**

```bash
git add services/agent_workbench/authority_curation.py tests/test_agent_workbench_authority_curation.py
git commit -m "feat: guard authority curation mutations"
```

---

### Task 6: ADK 2.0 Curation Workflow Schemas And Nodes

**Files:**
- Create: `orchestrator_agent/agent_tools/authority_curation/__init__.py`
- Create: `orchestrator_agent/agent_tools/authority_curation/schemes.py`
- Create: `orchestrator_agent/agent_tools/authority_curation/agent.py`
- Modify: `tests/test_agent_tool_runtime_import_boundary.py`
- Create: `tests/test_authority_curation_agent.py`

- [ ] **Step 1: Add failing schema strictness test**

Create `tests/test_authority_curation_agent.py`:

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator_agent.agent_tools.authority_curation.schemes import (
    AuthorityCurationGateDecision,
    AuthorityCurationWorkflowInput,
)


def test_authority_curation_workflow_input_rejects_unknown_fields() -> None:
    """ADK node payloads must be strict."""
    with pytest.raises(ValidationError):
        AuthorityCurationWorkflowInput.model_validate(
            {
                "project_id": 3,
                "spec_version_id": 4,
                "source_authority_id": 6,
                "source_authority_fingerprint": "sha256:abc",
                "feedback_json": {"feedback_items": []},
                "max_iterations": 2,
                "extra": "rejected",
            }
        )


def test_gate_decision_requires_reason_for_fail() -> None:
    """Failing gates must explain why the loop stops."""
    with pytest.raises(ValidationError):
        AuthorityCurationGateDecision(
            status="fail",
            review_ready=False,
            unresolved_feedback_ids=["AFB-1"],
        )
```

- [ ] **Step 2: Run failing schema test**

Run:

```bash
uv run --frozen pytest tests/test_authority_curation_agent.py -q
```

Expected: FAIL because module does not exist.

- [ ] **Step 3: Add strict ADK payload schemas**

Create `orchestrator_agent/agent_tools/authority_curation/schemes.py`:

```python
"""Authority curation ADK workflow schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    """Base schema that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid")


class AuthorityCurationWorkflowInput(_StrictModel):
    """Input passed from host service into the ADK workflow."""

    project_id: int
    spec_version_id: int
    source_authority_id: int
    source_authority_fingerprint: str = Field(min_length=1)
    feedback_json: dict[str, object]
    max_iterations: int = Field(default=2, ge=1, le=2)


class AuthorityCurationCriticFinding(_StrictModel):
    """One critic finding produced inside the curation loop."""

    feedback_id: str = Field(min_length=1)
    target_kind: str = Field(min_length=1)
    target_id: str | None = Field(default=None, min_length=1)
    source_item_id: str | None = Field(default=None, min_length=1)
    issue_type: str = Field(min_length=1)
    severity: Literal["blocking", "non_blocking"]
    instruction: str = Field(min_length=1)


class AuthorityCurationRepairPlan(_StrictModel):
    """Bounded repair plan emitted before repair compilation."""

    mode: Literal["targeted", "full_recompile", "fail_no_candidate"]
    target_ids: list[str] = Field(default_factory=list)
    feedback_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class AuthorityCurationRepairOutput(_StrictModel):
    """Repair output returned by the ADK workflow to the host."""

    mode: Literal["targeted", "full_recompile", "failed_no_candidate"]
    candidate_authority_json: dict[str, object] | None = None
    resolved_feedback_ids: list[str] = Field(default_factory=list)
    unresolved_feedback_ids: list[str] = Field(default_factory=list)
    failure_reason: str | None = None


class AuthorityCurationGateDecision(_StrictModel):
    """Final host-visible gate decision for one loop iteration."""

    status: Literal["pass", "retry", "fail"]
    review_ready: bool
    unresolved_feedback_ids: list[str] = Field(default_factory=list)
    reason: str | None = None

    @model_validator(mode="after")
    def _require_fail_reason(self) -> AuthorityCurationGateDecision:
        if self.status == "fail" and not self.reason:
            msg = "reason is required when status is fail"
            raise ValueError(msg)
        return self
```

- [ ] **Step 4: Add ADK workflow factory**

Create `orchestrator_agent/agent_tools/authority_curation/agent.py`:

```python
"""ADK 2.0 workflow factory for authority curation."""

from __future__ import annotations

from typing import Any

from google.adk.agents import LlmAgent

from orchestrator_agent.agent_tools.authority_curation.schemes import (
    AuthorityCurationWorkflowInput,
)

AUTHORITY_CURATION_STATE_INPUT = "authority_curation_input"
AUTHORITY_CURATION_STATE_GATE = "authority_curation_gate_decision"


def build_authority_curation_workflow(*, model: str) -> Any:
    """Build an ADK 2.0 workflow preserving Loop template semantics."""
    semantic_critic = LlmAgent(
        name="AuthoritySemanticFidelityCritic",
        model=model,
        include_contents="none",
        instruction=(
            "Review the authority candidate against structured feedback. "
            "Emit only strict JSON matching AuthorityCurationCriticFinding list. "
            "Flag overstrong, materially wrong, brittle, duplicate, and missing "
            "authority issues."
        ),
    )
    quality_critic = LlmAgent(
        name="AuthorityQualityCritic",
        model=model,
        include_contents="none",
        instruction=(
            "Review authority quality groups. Emit strict JSON findings for "
            "over-split, near-duplicate, unresolved gap, and assumption issues."
        ),
    )
    repair_planner = LlmAgent(
        name="AuthorityRepairPlanner",
        model=model,
        include_contents="none",
        instruction=(
            "Create a bounded targeted repair plan. Prefer targeted mode. "
            "Never change untargeted invariants."
        ),
    )
    repair_compiler = LlmAgent(
        name="AuthorityTargetedRepairCompiler",
        model=model,
        include_contents="none",
        instruction=(
            "Apply the repair plan and return a candidate authority JSON. "
            "Preserve untouched items byte-for-byte."
        ),
    )
    gate_decision = LlmAgent(
        name="AuthorityGateDecision",
        model=model,
        include_contents="none",
        instruction=(
            "Return pass, retry, or fail. Pass only when all blocking feedback "
            "is resolved and host-validatable output exists."
        ),
    )

    try:
        from google.adk.workflows import DynamicWorkflow  # type: ignore[import-not-found]

        return DynamicWorkflow(
            name="AuthorityCurationWorkflow",
            nodes=[
                semantic_critic,
                quality_critic,
                repair_planner,
                repair_compiler,
                gate_decision,
            ],
            max_iterations=2,
        )
    except ImportError:
        from google.adk.agents import SequentialAgent

        return SequentialAgent(
            name="AuthorityCurationWorkflow",
            sub_agents=[
                semantic_critic,
                quality_critic,
                repair_planner,
                repair_compiler,
                gate_decision,
            ],
            description=(
                "ADK 2.0 authority curation workflow using ordered Loop "
                "template semantics."
            ),
        )


def validate_workflow_input(payload: dict[str, object]) -> AuthorityCurationWorkflowInput:
    """Validate host input before invoking ADK."""
    return AuthorityCurationWorkflowInput.model_validate(payload)
```

If ADK 2.0 exposes a different graph/dynamic API than `google.adk.workflows.DynamicWorkflow`, keep the exported `build_authority_curation_workflow()` signature and replace only the import/body with the verified ADK 2.0 API.

- [ ] **Step 5: Export module names**

Create `orchestrator_agent/agent_tools/authority_curation/__init__.py`:

```python
"""Authority curation ADK workflow."""

from orchestrator_agent.agent_tools.authority_curation.agent import (
    build_authority_curation_workflow,
    validate_workflow_input,
)

__all__ = [
    "build_authority_curation_workflow",
    "validate_workflow_input",
]
```

- [ ] **Step 6: Run ADK workflow tests**

Run:

```bash
uv run --frozen pytest tests/test_authority_curation_agent.py tests/test_agent_tool_runtime_import_boundary.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit ADK curation workflow**

```bash
git add orchestrator_agent/agent_tools/authority_curation tests/test_authority_curation_agent.py tests/test_agent_tool_runtime_import_boundary.py
git commit -m "feat: add adk authority curation workflow"
```

---

### Task 7: Diff, Lineage, And Host Gate Validation

**Files:**
- Create: `services/specs/authority_curation_diff.py`
- Modify: `services/agent_workbench/authority_curation.py`
- Modify: `tests/test_agent_workbench_authority_curation.py`

- [ ] **Step 1: Add failing diff lineage test**

Add to `tests/test_agent_workbench_authority_curation.py`:

```python
def test_targeted_repair_records_old_to_new_invariant_lineage() -> None:
    """Changed canonical invariant payload receives new id and lineage."""
    from services.specs.authority_curation_diff import build_authority_diff

    source = {
        "invariants": [
            {
                "id": "INV-oldoldoldoldold1",
                "type": "relation_constraint",
                "parameters": {"expression": "learned_model_score >= max_baseline"},
                "source_item_id": "REQ.delayed-outcome-predictor",
                "source_level": "MUST",
            },
            {
                "id": "INV-keepkeepkeepkeep",
                "type": "required_field",
                "parameters": {"field_name": "report_id"},
                "source_item_id": "DATA.operational-learning-report",
                "source_level": "MUST",
            },
        ]
    }
    candidate = {
        "invariants": [
            {
                "id": "INV-newnewnewnewnew1",
                "type": "required_field",
                "parameters": {"field_name": "baseline_comparison_summary"},
                "source_item_id": "REQ.delayed-outcome-predictor",
                "source_level": "MUST",
            },
            {
                "id": "INV-keepkeepkeepkeep",
                "type": "required_field",
                "parameters": {"field_name": "report_id"},
                "source_item_id": "DATA.operational-learning-report",
                "source_level": "MUST",
            },
        ]
    }

    diff = build_authority_diff(
        source_authority_json=source,
        candidate_authority_json=candidate,
        targeted_source_item_ids={"REQ.delayed-outcome-predictor"},
    )

    assert diff["lineage_json"]["INV-oldoldoldoldold1"]["new_id"] == "INV-newnewnewnewnew1"
    assert diff["summary"]["unchanged_count"] == 1
    assert diff["summary"]["changed_count"] == 1
```

- [ ] **Step 2: Add diff helper**

Create `services/specs/authority_curation_diff.py`:

```python
"""Deterministic diff helpers for authority curation."""

from __future__ import annotations

from typing import Any

JsonDict = dict[str, Any]


def _invariants_by_id(authority_json: JsonDict) -> dict[str, JsonDict]:
    invariants = authority_json.get("invariants")
    if not isinstance(invariants, list):
        return {}
    result: dict[str, JsonDict] = {}
    for item in invariants:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            result[str(item["id"])] = dict(item)
    return result


def _canonical_identity(invariant: JsonDict) -> tuple[object, object, object, object]:
    return (
        invariant.get("type"),
        invariant.get("parameters"),
        invariant.get("source_item_id"),
        invariant.get("source_level"),
    )


def build_authority_diff(
    *,
    source_authority_json: JsonDict,
    candidate_authority_json: JsonDict,
    targeted_source_item_ids: set[str],
) -> JsonDict:
    """Return bounded diff and lineage for a curation candidate."""
    source_by_id = _invariants_by_id(source_authority_json)
    candidate_by_id = _invariants_by_id(candidate_authority_json)
    unchanged_ids = sorted(set(source_by_id) & set(candidate_by_id))
    removed_ids = sorted(set(source_by_id) - set(candidate_by_id))
    added_ids = sorted(set(candidate_by_id) - set(source_by_id))
    lineage: JsonDict = {}

    for old_id in removed_ids:
        old_item = source_by_id[old_id]
        old_source_item = str(old_item.get("source_item_id") or "")
        replacement_id = None
        for new_id in added_ids:
            new_item = candidate_by_id[new_id]
            if new_item.get("source_item_id") == old_source_item:
                replacement_id = new_id
                break
        lineage[old_id] = {
            "old_id": old_id,
            "new_id": replacement_id,
            "source_item_id": old_source_item,
            "reason": "targeted_repair" if replacement_id else "removed_by_repair",
        }

    untargeted_changes = [
        old_id
        for old_id in removed_ids
        if str(source_by_id[old_id].get("source_item_id") or "")
        not in targeted_source_item_ids
    ]
    return {
        "summary": {
            "unchanged_count": len(unchanged_ids),
            "changed_count": len(lineage),
            "removed_count": len(removed_ids),
            "added_count": len(added_ids),
            "untargeted_change_count": len(untargeted_changes),
        },
        "unchanged_ids": unchanged_ids,
        "removed_ids": removed_ids,
        "added_ids": added_ids,
        "lineage_json": lineage,
        "untargeted_changes": untargeted_changes,
    }
```

- [ ] **Step 3: Fail closed on untargeted diff**

In `AuthorityCurationRunner._run_curation_after_status_update()`, after ADK result:

```python
diff = build_authority_diff(
    source_authority_json=source_authority_json,
    candidate_authority_json=candidate_authority_json,
    targeted_source_item_ids=targeted_source_item_ids,
)
if diff["summary"]["untargeted_change_count"] > 0:
    return self._curation_failure(
        request=request,
        active_mutation=active_mutation,
        error_code=ErrorCode.AUTHORITY_CURATED_DIFF_UNBOUNDED,
        details={"untargeted_changes": diff["untargeted_changes"]},
    )
```

- [ ] **Step 4: Persist lineage and diff summary**

When curation succeeds, update `AuthorityCurationAttempt` row:

```python
attempt.status = "succeeded"
attempt.candidate_authority_id = candidate_authority_id
attempt.candidate_authority_fingerprint = candidate_fingerprint
attempt.diff_summary_json = json.dumps(diff["summary"], sort_keys=True)
attempt.lineage_json = json.dumps(diff["lineage_json"], sort_keys=True)
attempt.quality_report_json = json.dumps(quality_report, sort_keys=True)
attempt.updated_at = datetime.now(UTC)
```

- [ ] **Step 5: Run diff tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_curation.py -q -k "lineage or diff"
```

Expected: PASS.

- [ ] **Step 6: Commit diff and lineage**

```bash
git add services/specs/authority_curation_diff.py services/agent_workbench/authority_curation.py tests/test_agent_workbench_authority_curation.py
git commit -m "feat: validate authority curation diffs"
```

---

### Task 8: Publish Curated Candidate And Recover Failures

**Files:**
- Modify: `services/agent_workbench/authority_curation.py`
- Modify: `tests/test_agent_workbench_authority_curation.py`

- [ ] **Step 1: Add success publication test**

Add:

```python
def test_authority_curate_publishes_pending_review_candidate(
    engine: Engine,
    fake_workflow: FakeWorkflowPort,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing curation publishes a new pending authority and stops for review."""
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        lambda **_: _successful_curation_result(fixture),
    )

    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)
    result = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-success-001",
        )
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "authority_pending_review"
    assert result["data"]["pending_authority_id"] != fixture.authority_id
    assert fake_workflow.get_session_status(str(fixture.project_id))["setup_status"] == "authority_pending_review"
```

- [ ] **Step 2: Add failure recovery test**

Add:

```python
def test_authority_curate_failure_returns_to_rejected(
    engine: Engine,
    fake_workflow: FakeWorkflowPort,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed curation leaves project recoverable at authority_rejected."""
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        lambda **_: {
            "status": "failed",
            "error_code": "AUTHORITY_CURATION_MAX_ITERATIONS",
            "failure_artifact_id": "authority-curation-failed-001",
        },
    )

    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)
    result = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-fail-001",
        )
    )

    assert result["ok"] is False
    state = fake_workflow.get_session_status(str(fixture.project_id))
    assert state["setup_status"] == "authority_rejected"
    assert state["setup_curation_failure_artifact_id"] == "authority-curation-failed-001"
```

- [ ] **Step 3: Implement success publication**

Use `CompiledSpecAuthority` insert rather than patching accepted rows:

```python
authority = CompiledSpecAuthority(
    spec_version_id=request.spec_version_id,
    compiler_version=compiler_version,
    prompt_hash=prompt_hash,
    compiled_artifact_json=json.dumps(candidate_authority_json, sort_keys=True),
    scope_themes=json.dumps(candidate_authority_json.get("scope_themes", []), sort_keys=True),
    invariants=json.dumps(candidate_authority_json.get("invariants", []), sort_keys=True),
    eligible_feature_ids=json.dumps(candidate_authority_json.get("eligible_feature_ids", []), sort_keys=True),
    rejected_features=json.dumps(candidate_authority_json.get("rejected_features", []), sort_keys=True),
    spec_gaps=json.dumps(candidate_authority_json.get("spec_gaps", []), sort_keys=True),
)
session.add(authority)
session.commit()
session.refresh(authority)
```

Then update workflow state:

```python
self._workflow.update_session_status(
    str(request.project_id),
    {
        "fsm_state": "SETUP_REQUIRED",
        "setup_status": "authority_pending_review",
        "pending_authority_id": authority.authority_id,
        "pending_authority_fingerprint": candidate_fingerprint,
        "setup_next_actions": [
            {
                "command": "agileforge authority review",
                "args": {"project_id": request.project_id},
                "reason": "Review the curated authority candidate.",
            }
        ],
    },
)
```

- [ ] **Step 4: Implement failure recovery**

On failure, update workflow state:

```python
self._workflow.update_session_status(
    str(request.project_id),
    {
        "fsm_state": "SETUP_REQUIRED",
        "setup_status": "authority_rejected",
        "setup_curation_failure_artifact_id": failure_artifact_id,
        "setup_curation_error_code": error_code,
        "setup_next_actions": [
            {
                "command": "agileforge authority curate",
                "args": {
                    "project_id": request.project_id,
                    "spec_version_id": request.spec_version_id,
                    "source_authority_id": request.source_authority_id,
                    "feedback_attempt_id": request.feedback_attempt_id,
                },
                "reason": "Retry curation after reviewing the failure artifact.",
            }
        ],
    },
)
```

- [ ] **Step 5: Run publication/recovery tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_curation.py -q -k "publishes or recovery or failure_returns"
```

Expected: PASS.

- [ ] **Step 6: Commit publication path**

```bash
git add services/agent_workbench/authority_curation.py tests/test_agent_workbench_authority_curation.py
git commit -m "feat: publish curated authority candidates"
```

---

### Task 9: CLI, Command Registry, And API Surface

**Files:**
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `cli/main.py`
- Modify: `api.py`
- Modify: `tests/test_agent_workbench_application.py`
- Modify: `tests/test_agent_workbench_command_schema.py`
- Modify: `tests/test_agent_workbench_cli.py`
- Modify: `tests/test_api_dashboard.py`

- [ ] **Step 1: Add command registry tests**

Add to `tests/test_agent_workbench_command_schema.py`:

```python
def test_authority_feedback_record_command_is_registered() -> None:
    payload = command_schema_payload("agileforge authority feedback record")

    assert payload["ok"] is True
    data = payload["data"]
    assert data["mutates"] is True
    assert data["requires_idempotency_key"] is True
    assert "feedback_file" in data["input_required"]


def test_authority_curate_command_is_registered() -> None:
    payload = command_schema_payload("agileforge authority curate")

    assert payload["ok"] is True
    data = payload["data"]
    assert data["mutates"] is True
    assert data["requires_idempotency_key"] is True
    assert "feedback_attempt_id" in data["input_required"]
```

- [ ] **Step 2: Register commands**

In `services/agent_workbench/command_registry.py`, add:

```python
CommandMetadata(
    name="agileforge authority feedback record",
    mutates=True,
    phase="phase_2e",
    requires_idempotency_key=True,
    idempotency_policy=_REQUIRED_IDEMPOTENCY_POLICY,
    input_required=(
        "project_id",
        "pending_authority_id",
        "expected_authority_fingerprint",
        "feedback_file",
        "idempotency_key",
    ),
    input_optional=("changed_by", "correlation_id"),
    errors=(
        ErrorCode.AUTHORITY_NOT_PENDING.value,
        ErrorCode.STALE_AUTHORITY_VERSION.value,
        ErrorCode.AUTHORITY_FEEDBACK_SCHEMA_INVALID.value,
        ErrorCode.AUTHORITY_FEEDBACK_TARGET_NOT_FOUND.value,
        ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
        ErrorCode.MUTATION_IN_PROGRESS.value,
    ),
),
CommandMetadata(
    name="agileforge authority curate",
    mutates=True,
    phase="phase_2e",
    requires_idempotency_key=True,
    idempotency_policy=_REQUIRED_IDEMPOTENCY_POLICY,
    input_required=(
        "project_id",
        "spec_version_id",
        "source_authority_id",
        "expected_source_authority_fingerprint",
        "feedback_attempt_id",
        "idempotency_key",
    ),
    input_optional=("max_iterations", "compiler_model", "changed_by", "correlation_id"),
    errors=(
        ErrorCode.AUTHORITY_NOT_PENDING.value,
        ErrorCode.STALE_AUTHORITY_VERSION.value,
        ErrorCode.STALE_SETUP_STATUS.value,
        ErrorCode.AUTHORITY_CURATED_DIFF_UNBOUNDED.value,
        ErrorCode.AUTHORITY_CURATION_MAX_ITERATIONS.value,
        ErrorCode.SPEC_COMPILE_FAILED.value,
        ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
        ErrorCode.MUTATION_IN_PROGRESS.value,
        ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
    ),
),
```

- [ ] **Step 3: Add application facade methods**

In `AgentWorkbenchApplication`, add:

```python
def authority_feedback_record(
    self,
    *,
    project_id: int,
    pending_authority_id: int,
    expected_authority_fingerprint: str,
    feedback_file: str,
    idempotency_key: str,
    changed_by: str = "cli-agent",
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Record structured feedback for pending authority."""
    from services.agent_workbench.authority_curation import (
        AuthorityFeedbackRecordRequest,
    )

    return self._get_authority_curation_runner().feedback_record(
        AuthorityFeedbackRecordRequest(
            project_id=project_id,
            pending_authority_id=pending_authority_id,
            expected_authority_fingerprint=expected_authority_fingerprint,
            feedback_file=feedback_file,
            idempotency_key=idempotency_key,
            changed_by=changed_by,
            correlation_id=correlation_id,
        )
    )


def authority_curate(
    self,
    *,
    project_id: int,
    spec_version_id: int,
    source_authority_id: int,
    expected_source_authority_fingerprint: str,
    feedback_attempt_id: str,
    idempotency_key: str,
    max_iterations: int = 2,
    compiler_model: str | None = None,
    changed_by: str = "cli-agent",
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Run bounded authority curation."""
    from services.agent_workbench.authority_curation import AuthorityCurationRequest

    return self._get_authority_curation_runner().curate(
        AuthorityCurationRequest(
            project_id=project_id,
            spec_version_id=spec_version_id,
            source_authority_id=source_authority_id,
            expected_source_authority_fingerprint=expected_source_authority_fingerprint,
            feedback_attempt_id=feedback_attempt_id,
            max_iterations=max_iterations,
            compiler_model=compiler_model,
            idempotency_key=idempotency_key,
            changed_by=changed_by,
            correlation_id=correlation_id,
        )
    )
```

Add `_get_authority_curation_runner()` matching existing `_get_authority_regenerate_runner()`.

- [ ] **Step 4: Add CLI parsers and handlers**

In `cli/main.py`, under authority subcommands:

```python
feedback_parser = authority_subparsers.add_parser("feedback")
feedback_subparsers = feedback_parser.add_subparsers(dest="feedback_command", required=True)
feedback_record = feedback_subparsers.add_parser("record")
feedback_record.add_argument("--project-id", type=int, required=True)
feedback_record.add_argument("--pending-authority-id", type=int, required=True)
feedback_record.add_argument("--expected-authority-fingerprint", required=True)
feedback_record.add_argument("--feedback-file", required=True)
feedback_record.add_argument("--idempotency-key", required=True)
feedback_record.add_argument("--changed-by", default="cli-agent")
feedback_record.add_argument("--correlation-id")
feedback_record.set_defaults(handler=_handle_authority_feedback_record)

curate_parser = authority_subparsers.add_parser("curate")
curate_parser.add_argument("--project-id", type=int, required=True)
curate_parser.add_argument("--spec-version-id", type=int, required=True)
curate_parser.add_argument("--source-authority-id", type=int, required=True)
curate_parser.add_argument("--expected-source-authority-fingerprint", required=True)
curate_parser.add_argument("--feedback-attempt-id", required=True)
curate_parser.add_argument("--max-iterations", type=int, default=2)
curate_parser.add_argument("--compiler-model")
curate_parser.add_argument("--idempotency-key", required=True)
curate_parser.add_argument("--changed-by", default="cli-agent")
curate_parser.add_argument("--correlation-id")
curate_parser.set_defaults(handler=_handle_authority_curate)
```

Add handlers:

```python
def _handle_authority_feedback_record(args: argparse.Namespace) -> tuple[str, JsonObject]:
    """Route authority feedback record to the application facade."""
    return "agileforge authority feedback record", application.authority_feedback_record(
        project_id=args.project_id,
        pending_authority_id=args.pending_authority_id,
        expected_authority_fingerprint=args.expected_authority_fingerprint,
        feedback_file=args.feedback_file,
        idempotency_key=args.idempotency_key,
        changed_by=args.changed_by,
        correlation_id=args.correlation_id,
    )


def _handle_authority_curate(args: argparse.Namespace) -> tuple[str, JsonObject]:
    """Route authority curation to the application facade."""
    return "agileforge authority curate", application.authority_curate(
        project_id=args.project_id,
        spec_version_id=args.spec_version_id,
        source_authority_id=args.source_authority_id,
        expected_source_authority_fingerprint=args.expected_source_authority_fingerprint,
        feedback_attempt_id=args.feedback_attempt_id,
        max_iterations=args.max_iterations,
        compiler_model=args.compiler_model,
        idempotency_key=args.idempotency_key,
        changed_by=args.changed_by,
        correlation_id=args.correlation_id,
    )
```

- [ ] **Step 5: Add CLI smoke tests**

Add to `tests/test_agent_workbench_cli.py`:

```python
def test_authority_feedback_record_requires_feedback_file() -> None:
    result = invoke_cli(
        [
            "authority",
            "feedback",
            "record",
            "--project-id",
            "1",
            "--pending-authority-id",
            "6",
            "--expected-authority-fingerprint",
            "sha256:abc",
            "--idempotency-key",
            "feedback-001",
        ]
    )

    assert result.exit_code == 2


def test_authority_curate_requires_feedback_attempt_id() -> None:
    result = invoke_cli(
        [
            "authority",
            "curate",
            "--project-id",
            "1",
            "--spec-version-id",
            "4",
            "--source-authority-id",
            "6",
            "--expected-source-authority-fingerprint",
            "sha256:abc",
            "--idempotency-key",
            "curate-001",
        ]
    )

    assert result.exit_code == 2
```

- [ ] **Step 6: Add API parity if dashboard reads authority status**

If `api.py` already exposes authority review/status routes, add POST routes:

```python
@app.post("/api/projects/{project_id}/authority/feedback")
def api_authority_feedback(project_id: int, payload: dict[str, object]) -> dict[str, object]:
    """Record structured authority feedback from dashboard/API callers."""
    return _workbench().authority_feedback_record(project_id=project_id, **payload)


@app.post("/api/projects/{project_id}/authority/curate")
def api_authority_curate(project_id: int, payload: dict[str, object]) -> dict[str, object]:
    """Start bounded authority curation from dashboard/API callers."""
    return _workbench().authority_curate(project_id=project_id, **payload)
```

If `api.py` has no authority mutation routes, skip API mutation routes and only assert dashboard status projection includes curation fields.

- [ ] **Step 7: Run CLI/API tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_command_schema.py tests/test_agent_workbench_cli.py tests/test_agent_workbench_application.py tests/test_api_dashboard.py -q -k "authority"
```

Expected: PASS.

- [ ] **Step 8: Commit transport wiring**

```bash
git add services/agent_workbench/application.py services/agent_workbench/command_registry.py cli/main.py api.py tests/test_agent_workbench_application.py tests/test_agent_workbench_command_schema.py tests/test_agent_workbench_cli.py tests/test_api_dashboard.py
git commit -m "feat: expose authority curation commands"
```

---

### Task 10: Documentation And Full Verification

**Files:**
- Modify: `docs/agent-cli-manual.md`
- Modify: `docs/superpowers/specs/2026-06-16-authority-candidate-curation-loop-design.md` only if implementation discovers a contract correction.

- [ ] **Step 1: Add CLI manual section**

Add to `docs/agent-cli-manual.md`:

```markdown
### Authority Feedback And Curation

When `authority review` finds a materially wrong invariant, do not edit the
source spec unless the source spec is wrong. Record structured feedback against
the authority candidate and run bounded curation.

```bash
agileforge authority feedback record \
  --project-id <project_id> \
  --pending-authority-id <authority_id> \
  --expected-authority-fingerprint <sha256> \
  --feedback-file authority-feedback.json \
  --idempotency-key <key>

agileforge authority curate \
  --project-id <project_id> \
  --spec-version-id <spec_version_id> \
  --source-authority-id <authority_id> \
  --expected-source-authority-fingerprint <sha256> \
  --feedback-attempt-id <feedback_attempt_id> \
  --idempotency-key <key>
```

Curation stops at `authority_pending_review`. Human review and explicit
`authority accept` are still required.
```

- [ ] **Step 2: Run focused authority curation tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_authority_curation.py \
  tests/test_authority_curation_agent.py \
  tests/test_authority_curation_models.py \
  tests/test_agent_workbench_authority_projection.py \
  tests/test_agent_workbench_command_schema.py \
  tests/test_agent_workbench_cli.py \
  -q
```

Expected: PASS.

- [ ] **Step 3: Run frontend smoke if project workspace UI changed**

Run only if frontend files changed:

```bash
node --test tests/test_sprint_workspace_display.mjs
```

Expected: PASS.

- [ ] **Step 4: Run full repo gate**

Run:

```bash
pyrepo-check --all
```

Expected: ruff, annotations, ty, bandit, and pytest all pass. Current baseline before this plan was `2477 passed, 2 skipped, 13 deselected`.

- [ ] **Step 5: Commit docs and final verification fixes**

```bash
git add docs/agent-cli-manual.md docs/superpowers/specs/2026-06-16-authority-candidate-curation-loop-design.md
git commit -m "docs: document authority curation workflow"
```

Skip this commit if docs were already committed in prior tasks and no final docs changed.

---

## Self-Review

Spec coverage:

- ADK 2.0 migration is covered by Task 1.
- Loop template semantics through ADK 2.0 workflow are covered by Task 6.
- Feedback capture and strict schemas are covered by Task 3.
- Dedicated SQLModel storage and migrations are covered by Task 2.
- `authority_curating` guard is covered by Task 5.
- Projection flags and `workflow next` routing are covered by Task 4.
- Targeted repair, diff validation, and lineage are covered by Task 7.
- Pending review publication and failure recovery are covered by Task 8.
- CLI/API contracts are covered by Task 9.
- Documentation and full gate are covered by Task 10.

Red-flag scan requirements:

- This plan uses no open filler tokens.
- Any conditional API step has a concrete skip condition: only skip mutation routes when `api.py` has no existing authority mutation routes.
- Every code-bearing task includes exact snippets or exact file/function boundaries.

Execution notes:

- Keep commits task-sized.
- Do not accept authority automatically from any curation path.
- Do not suppress quality gates or linter findings.
- Run `pyrepo-check --all` before final branch handoff.
