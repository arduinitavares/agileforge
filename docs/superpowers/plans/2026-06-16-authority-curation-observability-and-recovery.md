# Authority Curation Observability And Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable local trace artifacts, trace inspection, projection metadata, and explicit recovery for `authority curate`.

**Architecture:** Keep the source of truth local and deterministic: JSONL trace artifacts under `logs/traces/authority_curation/`, existing mutation ledger rows, existing curation attempt rows, and one new `mutation_event_id` link on `AuthorityCurationAttempt`. Normal curation writes host-owned trace steps around the ADK workflow; recovery mode never reruns ADK and only reconciles already-published candidates.

**Tech Stack:** Python 3.12, SQLModel, SQLite migrations, stdlib `logging`, local JSONL artifacts, existing ADK 2 curation workflow, pytest.

---

## File Structure

- Create `utils/authority_curation_trace.py`
  - Own trace constants, JSONL append/read/summarize helpers, bounded error schema, and a small context manager.
- Create `tests/test_authority_curation_trace.py`
  - Unit tests for trace write/read, redaction bounds, summary fields, and context manager failures.
- Modify `models/authority_curation.py`
  - Add `mutation_event_id` to `AuthorityCurationAttempt`.
- Modify `db/migrations.py`
  - Add `mutation_event_id` to new and existing `authority_curation_attempts` tables.
  - Add `ix_authority_curation_mutation_event_id`.
- Modify `services/agent_workbench/schema_readiness.py`
  - Require the new column and index.
- Modify `tests/test_db_migrations.py`
  - Assert the new column and index exist after repeated migrations.
- Modify `tests/test_agent_workbench_schema_readiness.py`
  - Assert readiness requires the new curation column.
- Modify `services/agent_workbench/authority_curation.py`
  - Record trace events during normal curation.
  - Persist attempt `mutation_event_id`.
  - Add recovery request and recovery runner path.
  - Include trace metadata in success, failure, recovery, replay, and stale responses.
- Modify `services/agent_workbench/mutation_ledger.py`
  - Add one helper to reconcile recovery-required rows into `domain_failed_no_side_effects` when trace/DB evidence proves no candidate was published.
- Modify `services/agent_workbench/authority_projection.py`
  - Add latest curation trace metadata to `authority status`.
- Modify `services/agent_workbench/application.py`
  - Route normal curation, curation recovery, and read-only curation trace inspection.
- Modify `cli/main.py`
  - Add recovery args to `authority curate`.
  - Add `authority curation trace`.
- Modify `api.py`
  - Add recovery fields to `AuthorityCurateApiRequest`.
  - Route recovery requests through the same application method.
- Modify `services/agent_workbench/command_registry.py`
  - Update `agileforge authority curate` contract and add read-only `agileforge authority curation trace`.
- Modify tests:
  - `tests/test_agent_workbench_authority_curation.py`
  - `tests/test_agent_workbench_authority_projection.py`
  - `tests/test_agent_workbench_application.py`
  - `tests/test_agent_workbench_cli.py`
  - `tests/test_agent_workbench_command_schema.py`
  - `tests/test_api_dashboard.py`
  - `tests/test_agent_workbench_mutation_ledger.py`

---

### Task 1: Trace Artifact Utility

**Files:**
- Create: `utils/authority_curation_trace.py`
- Create: `tests/test_authority_curation_trace.py`

- [ ] **Step 1: Write failing trace utility tests**

Create `tests/test_authority_curation_trace.py`:

```python
"""Tests for durable authority curation trace artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import utils.authority_curation_trace as trace_mod
from utils.authority_curation_trace import (
    TRACE_SCHEMA_VERSION,
    append_trace_event,
    summarize_trace,
    trace_artifact_id,
    trace_artifact_path,
    trace_step,
)


def test_append_and_summarize_trace_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trace events are JSONL and produce a bounded summary."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")

    append_trace_event(
        mutation_event_id=647,
        project_id=3,
        step="mutation_lease_acquired",
        status="completed",
        curation_attempt_id=None,
        correlation_id="corr-1",
        attributes={"source_authority_id": 6},
    )
    append_trace_event(
        mutation_event_id=647,
        project_id=3,
        step="candidate_publication_completed",
        status="completed",
        curation_attempt_id="curation-1",
        correlation_id="corr-1",
        attributes={
            "candidate_authority_id": 7,
            "candidate_authority_fingerprint": "sha256:" + ("a" * 64),
        },
    )

    path = trace_artifact_path(647)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["schema_version"] == TRACE_SCHEMA_VERSION
    assert first["trace_artifact_id"] == "authority_curation_trace-647"

    summary = summarize_trace(mutation_event_id=647)
    assert summary["trace_artifact_id"] == trace_artifact_id(647)
    assert summary["event_count"] == 2
    assert summary["last_trace_step"] == "candidate_publication_completed"
    assert summary["last_trace_status"] == "completed"
    assert summary["candidate_published"] is True
    assert summary["candidate_authority_id"] == 7


def test_trace_rejects_unknown_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Trace steps are constrained to the approved enum."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")

    with pytest.raises(ValueError, match="unknown authority curation trace step"):
        append_trace_event(
            mutation_event_id=1,
            project_id=1,
            step="made_up_step",
            status="completed",
        )


def test_trace_redacts_unallowlisted_and_oversized_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trace attributes keep ids/counts/hashes but remove raw payloads."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")

    append_trace_event(
        mutation_event_id=2,
        project_id=1,
        step="adk_invocation_failed",
        status="failed",
        attributes={
            "requested_model_id": "openrouter/deepseek/deepseek-v4-pro",
            "source_authority_json": {"raw": "must-not-appear"},
            "long_text": "x" * 2000,
        },
        error={
            "code": "SPEC_COMPILE_FAILED",
            "message": "x" * 2000,
            "retryable": False,
            "failure_artifact_id": "authority_curation-failed",
            "details": {"raw_output": "must-not-appear", "validation_error_count": 2},
        },
    )

    payload = trace_artifact_path(2).read_text(encoding="utf-8")
    assert "openrouter/deepseek/deepseek-v4-pro" in payload
    assert "source_authority_json" not in payload
    assert "must-not-appear" not in payload
    event = json.loads(payload)
    assert len(event["error"]["message"]) <= trace_mod.MAX_TRACE_STRING_CHARS
    assert event["error"]["details"] == {"validation_error_count": 2}


def test_trace_step_records_completed_and_failed_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Context manager writes start plus terminal event."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")

    with trace_step(
        mutation_event_id=3,
        project_id=1,
        step="input_load_started",
        completed_step="input_load_completed",
        curation_attempt_id="curation-3",
    ):
        pass

    with pytest.raises(RuntimeError, match="boom"):
        with trace_step(
            mutation_event_id=4,
            project_id=1,
            step="adk_invocation_started",
            completed_step="adk_invocation_completed",
            failed_step="adk_invocation_failed",
        ):
            raise RuntimeError("boom")

    assert summarize_trace(mutation_event_id=3)["last_trace_status"] == "completed"
    failed_summary = summarize_trace(mutation_event_id=4)
    assert failed_summary["last_trace_step"] == "adk_invocation_failed"
    assert failed_summary["last_trace_status"] == "failed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_authority_curation_trace.py -q
```

Expected: FAIL during import with `ModuleNotFoundError: No module named 'utils.authority_curation_trace'`.

- [ ] **Step 3: Add trace utility implementation**

Create `utils/authority_curation_trace.py`:

```python
"""Durable JSONL traces for authority curation mutations."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, TypedDict, cast

from utils.failure_artifacts import LOGS_DIR

TRACE_SCHEMA_VERSION = "agileforge.authority_curation_trace.v1"
TRACE_DIR = LOGS_DIR / "traces" / "authority_curation"
MAX_TRACE_STRING_CHARS = 500

TraceStatus = Literal["started", "completed", "failed", "skipped"]

TRACE_STEPS = frozenset(
    {
        "mutation_lease_acquired",
        "guard_validation_started",
        "guard_validation_completed",
        "guard_validation_failed",
        "curation_attempt_create_started",
        "curation_attempt_create_completed",
        "curation_attempt_create_failed",
        "workflow_curating_status_started",
        "workflow_curating_status_completed",
        "workflow_curating_status_failed",
        "input_load_started",
        "input_load_completed",
        "input_load_failed",
        "adk_invocation_started",
        "adk_invocation_completed",
        "adk_invocation_failed",
        "adk_gate_parse_started",
        "adk_gate_parse_completed",
        "adk_gate_parse_failed",
        "diff_validation_started",
        "diff_validation_completed",
        "diff_validation_failed",
        "candidate_publication_started",
        "candidate_publication_completed",
        "candidate_publication_failed",
        "workflow_pending_review_started",
        "workflow_pending_review_completed",
        "workflow_pending_review_failed",
        "mutation_finalize_started",
        "mutation_finalize_completed",
        "mutation_finalize_failed",
        "recovery_classification_started",
        "recovery_classification_completed",
        "recovery_classification_failed",
    }
)

TRACE_STATUSES = frozenset({"started", "completed", "failed", "skipped"})

ALLOWED_ATTRIBUTE_KEYS = frozenset(
    {
        "spec_version_id",
        "source_authority_id",
        "source_authority_fingerprint",
        "feedback_attempt_id",
        "requested_model_id",
        "compiler_version",
        "prompt_hash",
        "event_count",
        "candidate_authority_id",
        "candidate_authority_fingerprint",
        "failure_stage",
        "validation_error_count",
        "untargeted_change_count",
        "curation_attempt_id",
    }
)

ALLOWED_ERROR_DETAIL_KEYS = frozenset(
    {
        "failure_stage",
        "validation_error_count",
        "untargeted_change_count",
        "current_step",
        "candidate_authority_id",
        "candidate_authority_fingerprint",
    }
)


class TraceError(TypedDict, total=False):
    """Bounded error object stored in a trace event."""

    code: str
    message: str
    retryable: bool
    failure_artifact_id: str | None
    details: dict[str, object]


def trace_artifact_id(mutation_event_id: int) -> str:
    """Return the deterministic trace artifact id for a mutation."""
    return f"authority_curation_trace-{mutation_event_id}"


def trace_artifact_path(mutation_event_id: int) -> Path:
    """Return the deterministic JSONL path for a mutation trace."""
    return TRACE_DIR / f"{trace_artifact_id(mutation_event_id)}.jsonl"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _bounded_str(value: object) -> str:
    text = str(value)
    return text[:MAX_TRACE_STRING_CHARS]


def _bounded_json_value(value: object) -> object:
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, str):
        return _bounded_str(value)
    return _bounded_str(value)


def _bounded_mapping(
    value: Mapping[str, object] | None,
    *,
    allowed_keys: frozenset[str],
) -> dict[str, object]:
    if not value:
        return {}
    return {
        key: _bounded_json_value(item)
        for key, item in value.items()
        if key in allowed_keys
    }


def _bounded_error(value: Mapping[str, object] | None) -> TraceError | None:
    if not value:
        return None
    code = _bounded_str(value.get("code", "MUTATION_FAILED"))
    message = _bounded_str(value.get("message", "Authority curation failed."))
    retryable = bool(value.get("retryable", False))
    failure_artifact_id_raw = value.get("failure_artifact_id")
    failure_artifact_id = (
        None if failure_artifact_id_raw is None else _bounded_str(failure_artifact_id_raw)
    )
    details_raw = value.get("details")
    details = (
        _bounded_mapping(
            cast("Mapping[str, object]", details_raw),
            allowed_keys=ALLOWED_ERROR_DETAIL_KEYS,
        )
        if isinstance(details_raw, Mapping)
        else {}
    )
    error: TraceError = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "failure_artifact_id": failure_artifact_id,
    }
    if details:
        error["details"] = details
    return error


def append_trace_event(
    *,
    mutation_event_id: int,
    project_id: int,
    step: str,
    status: TraceStatus,
    curation_attempt_id: str | None = None,
    correlation_id: str | None = None,
    duration_ms: int | None = None,
    attributes: Mapping[str, object] | None = None,
    error: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Append one bounded authority curation trace event."""
    if step not in TRACE_STEPS:
        raise ValueError(f"unknown authority curation trace step: {step}")
    if status not in TRACE_STATUSES:
        raise ValueError(f"unknown authority curation trace status: {status}")

    event: dict[str, object] = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "trace_artifact_id": trace_artifact_id(mutation_event_id),
        "mutation_event_id": mutation_event_id,
        "curation_attempt_id": curation_attempt_id,
        "project_id": project_id,
        "step": step,
        "status": status,
        "recorded_at": _now_iso(),
        "duration_ms": duration_ms,
        "correlation_id": correlation_id,
        "attributes": _bounded_mapping(
            attributes,
            allowed_keys=ALLOWED_ATTRIBUTE_KEYS,
        ),
        "error": _bounded_error(error),
    }
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    with trace_artifact_path(mutation_event_id).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")))
        handle.write("\n")
    return event


def read_trace_events(*, mutation_event_id: int) -> list[dict[str, object]]:
    """Read valid JSON object events for a mutation trace."""
    path = trace_artifact_path(mutation_event_id)
    if not path.exists():
        return []
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            events.append(cast("dict[str, object]", parsed))
    return events


def summarize_trace(*, mutation_event_id: int) -> dict[str, object]:
    """Return bounded trace summary for CLI/API/projection output."""
    events = read_trace_events(mutation_event_id=mutation_event_id)
    last = events[-1] if events else {}
    candidate_event = next(
        (
            event
            for event in reversed(events)
            if event.get("step") == "candidate_publication_completed"
        ),
        None,
    )
    candidate_attrs = (
        cast("dict[str, object]", candidate_event.get("attributes", {}))
        if candidate_event is not None
        else {}
    )
    failure_artifact_id = None
    for event in reversed(events):
        error = event.get("error")
        if isinstance(error, dict) and error.get("failure_artifact_id"):
            failure_artifact_id = error["failure_artifact_id"]
            break
    return {
        "trace_artifact_id": trace_artifact_id(mutation_event_id),
        "trace_artifact_present": bool(events),
        "event_count": len(events),
        "last_trace_step": last.get("step"),
        "last_trace_status": last.get("status"),
        "curation_attempt_id": last.get("curation_attempt_id"),
        "candidate_published": candidate_event is not None,
        "candidate_authority_id": candidate_attrs.get("candidate_authority_id"),
        "candidate_authority_fingerprint": candidate_attrs.get(
            "candidate_authority_fingerprint"
        ),
        "failure_artifact_id": failure_artifact_id,
    }


@contextmanager
def trace_step(
    *,
    mutation_event_id: int,
    project_id: int,
    step: str,
    completed_step: str,
    failed_step: str | None = None,
    curation_attempt_id: str | None = None,
    correlation_id: str | None = None,
    attributes: Mapping[str, object] | None = None,
) -> Iterator[None]:
    """Trace a started step plus its terminal completed/failed event."""
    started = perf_counter()
    append_trace_event(
        mutation_event_id=mutation_event_id,
        project_id=project_id,
        step=step,
        status="started",
        curation_attempt_id=curation_attempt_id,
        correlation_id=correlation_id,
        attributes=attributes,
    )
    try:
        yield
    except Exception as exc:
        append_trace_event(
            mutation_event_id=mutation_event_id,
            project_id=project_id,
            step=failed_step or step.replace("_started", "_failed"),
            status="failed",
            curation_attempt_id=curation_attempt_id,
            correlation_id=correlation_id,
            duration_ms=int((perf_counter() - started) * 1000),
            attributes=attributes,
            error={
                "code": type(exc).__name__,
                "message": str(exc),
                "retryable": False,
            },
        )
        raise
    append_trace_event(
        mutation_event_id=mutation_event_id,
        project_id=project_id,
        step=completed_step,
        status="completed",
        curation_attempt_id=curation_attempt_id,
        correlation_id=correlation_id,
        duration_ms=int((perf_counter() - started) * 1000),
        attributes=attributes,
    )
```

- [ ] **Step 4: Run trace tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_authority_curation_trace.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add utils/authority_curation_trace.py tests/test_authority_curation_trace.py
git commit -m "feat: add authority curation trace artifacts"
```

---

### Task 2: Persist Mutation Event Id On Curation Attempts

**Files:**
- Modify: `models/authority_curation.py`
- Modify: `db/migrations.py`
- Modify: `services/agent_workbench/schema_readiness.py`
- Modify: `tests/test_db_migrations.py`
- Modify: `tests/test_agent_workbench_schema_readiness.py`

- [ ] **Step 1: Write failing migration/readiness tests**

In `tests/test_db_migrations.py`, update `test_authority_curation_migration_is_idempotent` curation column assertion:

```python
    assert {
        "curation_row_id",
        "project_id",
        "mutation_event_id",
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
    }.issubset(curation_columns)
```

Then add this assertion after existing curation index assertions:

```python
    assert "ix_authority_curation_mutation_event_id" in curation_indexes
```

In `tests/test_agent_workbench_schema_readiness.py`, add:

```python
def test_authority_curation_readiness_requires_mutation_event_id(
    engine: Engine,
) -> None:
    """Authority curation readiness requires the attempt-to-mutation link."""
    SQLModel.metadata.create_all(engine)

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE authority_curation_attempts"))
        conn.execute(
            text(
                """
                CREATE TABLE authority_curation_attempts (
                    curation_row_id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL,
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
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )

    result = check_schema_readiness(engine, AUTHORITY_CURATION_REQUIREMENTS)

    assert result.ok is False
    assert "authority_curation_attempts.mutation_event_id" in result.missing
```

- [ ] **Step 2: Run migration/readiness tests to verify failure**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_db_migrations.py::test_authority_curation_migration_is_idempotent tests/test_agent_workbench_schema_readiness.py::test_authority_curation_readiness_requires_mutation_event_id -q
```

Expected: FAIL because `mutation_event_id` column/index is missing.

- [ ] **Step 3: Add model and migration support**

In `models/authority_curation.py`, add this field after `project_id`:

```python
    mutation_event_id: int | None = Field(default=None, index=True)
```

In `db/migrations.py`, add `mutation_event_id INTEGER` to `AUTHORITY_CURATION_ATTEMPTS_CREATE_SQL` after `project_id`.

In `migrate_authority_curation_tables`, immediately after ensuring the table exists:

```python
    if _ensure_column_exists(
        engine,
        "authority_curation_attempts",
        "mutation_event_id",
        "INTEGER",
    ):
        actions.append("added column: authority_curation_attempts.mutation_event_id")
```

Then add an index ensure block before source authority index:

```python
    if _ensure_index_exists(
        engine,
        "authority_curation_attempts",
        "ix_authority_curation_mutation_event_id",
        ["mutation_event_id"],
    ):
        actions.append("created index: ix_authority_curation_mutation_event_id")
```

In `services/agent_workbench/schema_readiness.py`, add `"mutation_event_id"` to the `authority_curation_attempts` columns and add `"ix_authority_curation_mutation_event_id"` to indexes.

- [ ] **Step 4: Run migration/readiness tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_db_migrations.py::test_authority_curation_migration_is_idempotent tests/test_agent_workbench_schema_readiness.py::test_authority_curation_readiness_requires_mutation_event_id -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add models/authority_curation.py db/migrations.py services/agent_workbench/schema_readiness.py tests/test_db_migrations.py tests/test_agent_workbench_schema_readiness.py
git commit -m "feat: link curation attempts to mutation events"
```

---

### Task 3: Trace Normal Curation Execution

**Files:**
- Modify: `services/agent_workbench/authority_curation.py`
- Modify: `tests/test_agent_workbench_authority_curation.py`

- [ ] **Step 1: Write failing curation trace tests**

In `tests/test_agent_workbench_authority_curation.py`, import the trace module:

```python
import utils.authority_curation_trace as trace_mod
```

Add tests near existing `authority_curate` runner tests:

```python
def test_authority_curate_success_writes_trace_artifact(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Successful curation writes a durable host-step trace."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {"fsm_state": "SETUP_REQUIRED", "setup_status": "authority_rejected"},
    )
    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        lambda **_: _targeted_repair_curation_result(fixture),
    )

    result = AuthorityCurationRunner(
        engine=engine,
        workflow=fake_workflow,
    ).curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-trace-success",
            compiler_model="openrouter/test/model",
            correlation_id="corr-trace-success",
        )
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["trace_artifact_id"].startswith("authority_curation_trace-")
    with Session(engine) as session:
        attempt = session.exec(select(AuthorityCurationAttempt)).one()
        ledger = session.exec(select(CliMutationLedger)).one()
    assert attempt.mutation_event_id == ledger.mutation_event_id
    summary = trace_mod.summarize_trace(
        mutation_event_id=require_id(ledger.mutation_event_id, "mutation_event_id")
    )
    assert summary["candidate_published"] is True
    steps = [
        event["step"]
        for event in trace_mod.read_trace_events(
            mutation_event_id=require_id(ledger.mutation_event_id, "mutation_event_id")
        )
    ]
    assert "mutation_lease_acquired" in steps
    assert "workflow_curating_status_completed" in steps
    assert "adk_invocation_completed" in steps
    assert "diff_validation_completed" in steps
    assert "candidate_publication_completed" in steps
    assert "mutation_finalize_completed" in steps


def test_authority_curate_diff_failure_writes_trace_failure_event(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Host diff failure is visible in the durable trace."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {"fsm_state": "SETUP_REQUIRED", "setup_status": "authority_rejected"},
    )
    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        lambda **_: _untargeted_change_curation_result(fixture),
    )

    result = AuthorityCurationRunner(
        engine=engine,
        workflow=fake_workflow,
    ).curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-trace-diff-failure",
        )
    )

    assert result["ok"] is False
    details = result["errors"][0]["details"]
    assert details["trace_artifact_id"].startswith("authority_curation_trace-")
    with Session(engine) as session:
        ledger = session.exec(select(CliMutationLedger)).one()
    events = trace_mod.read_trace_events(
        mutation_event_id=require_id(ledger.mutation_event_id, "mutation_event_id")
    )
    assert events[-1]["step"] == "diff_validation_failed"
    assert events[-1]["status"] == "failed"
    assert "candidate_publication_completed" not in [event["step"] for event in events]
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_agent_workbench_authority_curation.py::test_authority_curate_success_writes_trace_artifact tests/test_agent_workbench_authority_curation.py::test_authority_curate_diff_failure_writes_trace_failure_event -q
```

Expected: FAIL because curation does not write traces or persist `mutation_event_id`.

- [ ] **Step 3: Wire trace events into normal curation**

In `services/agent_workbench/authority_curation.py`, import trace helpers:

```python
from utils.authority_curation_trace import (
    append_trace_event,
    summarize_trace,
    trace_artifact_id,
    trace_step,
)
```

Update `_create_running_curation_attempt` signature to accept `mutation_event_id: int`, set `mutation_event_id=mutation_event_id` on the row, and update its call site:

```python
        attempt = self._create_running_curation_attempt(
            request=request,
            request_hash=_curation_request_hash(request),
            mutation_event_id=active_mutation.mutation_event_id,
        )
```

Immediately after `_start_curation_mutation` returns `_ActiveMutation`, append:

```python
        append_trace_event(
            mutation_event_id=active_mutation.mutation_event_id,
            project_id=request.project_id,
            step="mutation_lease_acquired",
            status="completed",
            correlation_id=request.correlation_id,
            attributes=_curation_trace_attributes(request),
        )
```

Add helper near `_curation_request_hash`:

```python
def _curation_trace_attributes(
    request: AuthorityCurationRequest,
    *,
    candidate_authority_id: int | None = None,
    candidate_authority_fingerprint: str | None = None,
    event_count: object = None,
    failure_stage: str | None = None,
) -> dict[str, object]:
    """Return allowlisted trace attributes for authority curation."""
    attrs: dict[str, object] = {
        "spec_version_id": request.spec_version_id,
        "source_authority_id": request.source_authority_id,
        "source_authority_fingerprint": request.expected_source_authority_fingerprint,
        "feedback_attempt_id": request.feedback_attempt_id,
        "requested_model_id": _authority_curation_model_id(request),
        "compiler_version": AUTHORITY_CURATION_COMPILER_VERSION,
        "prompt_hash": AUTHORITY_CURATION_PROMPT_HASH,
    }
    if candidate_authority_id is not None:
        attrs["candidate_authority_id"] = candidate_authority_id
    if candidate_authority_fingerprint is not None:
        attrs["candidate_authority_fingerprint"] = candidate_authority_fingerprint
    if event_count is not None:
        attrs["event_count"] = event_count
    if failure_stage is not None:
        attrs["failure_stage"] = failure_stage
    return attrs
```

Use `append_trace_event(...)` around guard validation, attempt creation, workflow state update, input load, ADK invocation, diff validation, candidate publication, pending review workflow update, and mutation finalization. Use the step names from the spec exactly.

Update success response data:

```python
                    "trace_artifact_id": trace_artifact_id(
                        active_mutation.mutation_event_id
                    ),
```

Update error details in `_failed_curation_workflow_response`, `_invalid_curation_candidate_response`, diff failure responses, and `_published_curation_recovery_response` to include:

```python
"trace_artifact_id": trace_artifact_id(active_mutation.mutation_event_id)
```

When a failure response is produced inside `_run_curation_after_status_update`, append a matching failed trace event before finalizing.

- [ ] **Step 4: Run focused curation trace tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_agent_workbench_authority_curation.py::test_authority_curate_success_writes_trace_artifact tests/test_agent_workbench_authority_curation.py::test_authority_curate_diff_failure_writes_trace_failure_event -q
```

Expected: PASS.

- [ ] **Step 5: Run existing curation tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_agent_workbench_authority_curation.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/agent_workbench/authority_curation.py tests/test_agent_workbench_authority_curation.py
git commit -m "feat: trace authority curation execution"
```

---

### Task 4: Expose Trace Metadata In Status And Mutation Show

**Files:**
- Modify: `services/agent_workbench/mutation_ledger.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/authority_projection.py`
- Modify: `tests/test_agent_workbench_mutation_ledger.py`
- Modify: `tests/test_agent_workbench_authority_projection.py`

- [ ] **Step 1: Write failing projection and mutation tests**

In `tests/test_agent_workbench_mutation_ledger.py`, add a test near show/list tests:

```python
def test_mutation_show_includes_authority_curation_trace_metadata(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation show enriches authority curation rows with trace summary."""
    import utils.authority_curation_trace as trace_mod

    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")
    repo = MutationLedgerRepository(engine=engine)
    loaded = repo.create_or_load(
        command="agileforge authority curate",
        idempotency_key="curate-show-trace",
        request_hash="sha256:trace",
        project_id=3,
        correlation_id="corr",
        changed_by="test",
        lease_owner="lease",
        now=datetime(2026, 6, 16, 12, tzinfo=UTC),
    )
    mutation_event_id = require_id(
        loaded.ledger.mutation_event_id,
        "mutation_event_id",
    )
    trace_mod.append_trace_event(
        mutation_event_id=mutation_event_id,
        project_id=3,
        step="adk_invocation_started",
        status="started",
    )

    result = repo.show_event(mutation_event_id=mutation_event_id)

    assert result["ok"] is True
    data = result["data"]
    assert data["trace_artifact_id"] == f"authority_curation_trace-{mutation_event_id}"
    assert data["trace_artifact_present"] is True
    assert data["last_trace_step"] == "adk_invocation_started"
    assert data["last_trace_status"] == "started"
```

In `tests/test_agent_workbench_authority_projection.py`, add to `_seed_curation_attempt` signature:

```python
    mutation_event_id: int | None = 647,
```

and set it on `AuthorityCurationAttempt(...)`.

Add:

```python
def test_authority_status_reports_latest_curation_trace_metadata(
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authority status links latest curation attempt to trace summary."""
    import utils.authority_curation_trace as trace_mod

    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")
    product = _seed_product(session)
    project_id = require_id(product.product_id, "product_id")
    spec = _seed_spec(session, product_id=project_id, content="# Spec\n")
    authority = _seed_authority(session, spec=spec)
    authority_id = require_id(authority.authority_id, "authority_id")
    feedback = _seed_feedback_attempt(
        session,
        project_id=project_id,
        authority=authority,
        feedback_attempt_id="feedback-trace",
        has_blocking_feedback=True,
    )
    curation = _seed_curation_attempt(
        session,
        project_id=project_id,
        authority=authority,
        feedback_attempt_id=feedback.feedback_attempt_id,
        curation_attempt_id="curation-trace",
        status="failed",
        mutation_event_id=647,
    )
    trace_mod.append_trace_event(
        mutation_event_id=647,
        project_id=project_id,
        step="adk_gate_parse_failed",
        status="failed",
        curation_attempt_id=curation.curation_attempt_id,
        error={"code": "SPEC_COMPILE_FAILED", "message": "gate failed", "retryable": False},
    )
    _seed_rejected_decision(session, project_id=project_id, authority_id=authority_id)

    result = AuthorityProjectionService(
        engine=_engine(session),
        repo_root=tmp_path,
    ).status(project_id=project_id)

    assert result["ok"] is True
    data = result["data"]
    assert data["latest_curation_trace_artifact_id"] == "authority_curation_trace-647"
    assert data["latest_curation_last_step"] == "adk_gate_parse_failed"
    assert data["latest_curation_last_status"] == "failed"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_agent_workbench_mutation_ledger.py::test_mutation_show_includes_authority_curation_trace_metadata tests/test_agent_workbench_authority_projection.py::test_authority_status_reports_latest_curation_trace_metadata -q
```

Expected: FAIL because metadata is not exposed.

- [ ] **Step 3: Enrich mutation show**

In `services/agent_workbench/mutation_ledger.py`, import trace summary inside `_row_payload` to avoid broad import cost:

```python
    if row.command == "agileforge authority curate" and row.mutation_event_id is not None:
        from utils.authority_curation_trace import summarize_trace  # noqa: PLC0415

        payload.update(summarize_trace(mutation_event_id=row.mutation_event_id))
```

- [ ] **Step 4: Enrich authority projection**

In `services/agent_workbench/authority_projection.py`, import `summarize_trace` and `trace_artifact_id`.

Update `_feedback_curation_defaults()` with:

```python
        "latest_curation_trace_artifact_id": None,
        "latest_curation_last_step": None,
        "latest_curation_last_status": None,
```

In `_latest_feedback_and_curation`, after choosing `curation`, compute:

```python
    trace_summary: JsonDict = {}
    mutation_event_id = None if curation is None else curation.mutation_event_id
    if mutation_event_id is not None:
        trace_summary = summarize_trace(mutation_event_id=mutation_event_id)
```

Add returned fields:

```python
        "latest_curation_trace_artifact_id": (
            None
            if mutation_event_id is None
            else trace_summary.get("trace_artifact_id")
        ),
        "latest_curation_last_step": trace_summary.get("last_trace_step"),
        "latest_curation_last_status": trace_summary.get("last_trace_status"),
```

- [ ] **Step 5: Run focused projection tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_agent_workbench_mutation_ledger.py::test_mutation_show_includes_authority_curation_trace_metadata tests/test_agent_workbench_authority_projection.py::test_authority_status_reports_latest_curation_trace_metadata -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/agent_workbench/mutation_ledger.py services/agent_workbench/authority_projection.py tests/test_agent_workbench_mutation_ledger.py tests/test_agent_workbench_authority_projection.py
git commit -m "feat: expose authority curation trace metadata"
```

---

### Task 5: Add Read-Only Trace Inspection Command

**Files:**
- Modify: `services/agent_workbench/application.py`
- Modify: `cli/main.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `tests/test_agent_workbench_application.py`
- Modify: `tests/test_agent_workbench_cli.py`
- Modify: `tests/test_agent_workbench_command_schema.py`

- [ ] **Step 1: Write failing CLI/application/schema tests**

In `tests/test_agent_workbench_application.py`, add:

```python
def test_application_authority_curation_trace_returns_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Application reads bounded curation trace summaries by mutation id."""
    import utils.authority_curation_trace as trace_mod

    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")
    trace_mod.append_trace_event(
        mutation_event_id=647,
        project_id=3,
        step="adk_invocation_started",
        status="started",
    )
    app = AgentWorkbenchApplication()

    result = app.authority_curation_trace(
        mutation_event_id=647,
        project_id=None,
    )

    assert result["ok"] is True
    assert result["data"]["trace_artifact_id"] == "authority_curation_trace-647"
    assert result["data"]["event_count"] == 1
```

In `tests/test_agent_workbench_cli.py`, add to `_FakeApplication`:

```python
    def authority_curation_trace(
        self,
        *,
        mutation_event_id: int,
        project_id: int | None = None,
    ) -> JsonObject:
        """Return an authority curation trace payload."""
        self.calls.append(
            (
                "authority_curation_trace",
                {"mutation_event_id": mutation_event_id, "project_id": project_id},
            )
        )
        return {
            "ok": True,
            "data": {
                "trace_artifact_id": f"authority_curation_trace-{mutation_event_id}",
                "event_count": 1,
            },
            "warnings": [],
            "errors": [],
        }
```

Then add:

```python
def test_authority_curation_trace_cli_routes_to_application(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Read-only trace inspection routes mutation and optional project id."""
    app = _FakeApplication()

    rc = main(
        [
            "authority",
            "curation",
            "trace",
            "--mutation-event-id",
            "647",
            "--project-id",
            "3",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert payload["data"]["trace_artifact_id"] == "authority_curation_trace-647"
    assert app.calls[-1] == (
        "authority_curation_trace",
        {"mutation_event_id": 647, "project_id": 3},
    )
```

In `tests/test_agent_workbench_command_schema.py`, add:

```python
def test_authority_curation_trace_command_is_registered() -> None:
    """Publish read-only authority curation trace inspection contract."""
    schema = command_schema_payload("agileforge authority curation trace")

    assert schema["mutates"] is False
    assert schema["input"]["required"] == ["mutation_event_id"]
    assert schema["input"]["optional"] == ["project_id"]
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_agent_workbench_application.py::test_application_authority_curation_trace_returns_summary tests/test_agent_workbench_cli.py::test_authority_curation_trace_cli_routes_to_application tests/test_agent_workbench_command_schema.py::test_authority_curation_trace_command_is_registered -q
```

Expected: FAIL because command/application method are missing.

- [ ] **Step 3: Implement application method**

In `cli/main.py` `_Application` protocol, add:

```python
    def authority_curation_trace(
        self,
        *,
        mutation_event_id: int,
        project_id: int | None = None,
    ) -> JsonObject:
        """Return bounded authority curation trace summary."""
        raise NotImplementedError
```

In `services/agent_workbench/application.py`, add:

```python
    def authority_curation_trace(
        self,
        *,
        mutation_event_id: int,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        """Return bounded authority curation trace summary."""
        from utils.authority_curation_trace import summarize_trace  # noqa: PLC0415

        repo, error = _mutation_ledger_repository()
        if error is not None:
            return error
        repo = cast("MutationLedgerRepository", repo)
        shown = repo.show_event(mutation_event_id=mutation_event_id)
        if shown.get("ok") is not True:
            return shown
        data = shown.get("data")
        if not isinstance(data, dict):
            return shown
        if data.get("command") != "agileforge authority curate":
            return error_envelope(
                command="agileforge authority curation trace",
                error=workbench_error(
                    ErrorCode.MUTATION_RESUME_CONFLICT,
                    message="Mutation is not an authority curation mutation.",
                    details={"mutation_event_id": mutation_event_id},
                ),
            )
        if project_id is not None and data.get("project_id") != project_id:
            return error_envelope(
                command="agileforge authority curation trace",
                error=workbench_error(
                    ErrorCode.MUTATION_RESUME_CONFLICT,
                    message="Mutation does not belong to project.",
                    details={
                        "mutation_event_id": mutation_event_id,
                        "project_id": project_id,
                        "actual_project_id": data.get("project_id"),
                    },
                ),
            )
        return success_envelope(
            command="agileforge authority curation trace",
            data=summarize_trace(mutation_event_id=mutation_event_id),
        )
```

Adjust imports if `error_envelope`, `success_envelope`, `workbench_error`, or `ErrorCode` are not in scope.

- [ ] **Step 4: Add CLI parser and handler**

In `cli/main.py`, after `authority_curate` parser setup, add:

```python
    authority_curation = authority_sub.add_parser(
        "curation",
        help="Inspect authority curation artifacts.",
    )
    authority_curation_sub = authority_curation.add_subparsers(
        dest="curation_command",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    authority_curation_trace = authority_curation_sub.add_parser(
        "trace",
        help="Show bounded authority curation trace summary.",
    )
    authority_curation_trace.add_argument("--mutation-event-id", type=int, required=True)
    authority_curation_trace.add_argument("--project-id", type=int)
    authority_curation_trace.set_defaults(command_handler=_authority_curation_trace)
```

Add handler:

```python
def _authority_curation_trace(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route authority curation trace inspection to the application facade."""
    return (
        "agileforge authority curation trace",
        application.authority_curation_trace(
            mutation_event_id=args.mutation_event_id,
            project_id=args.project_id,
        ),
    )
```

- [ ] **Step 5: Register command schema**

In `services/agent_workbench/command_registry.py`, add a `CommandMetadata` near authority curation:

```python
    CommandMetadata(
        name="agileforge authority curation trace",
        mutates=False,
        phase="phase_2e",
        input_required=("mutation_event_id",),
        input_optional=("project_id",),
        errors=(
            ErrorCode.SCHEMA_NOT_READY.value,
            ErrorCode.MUTATION_NOT_FOUND.value,
            ErrorCode.MUTATION_RESUME_CONFLICT.value,
        ),
    ),
```

- [ ] **Step 6: Run focused command tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_agent_workbench_application.py::test_application_authority_curation_trace_returns_summary tests/test_agent_workbench_cli.py::test_authority_curation_trace_cli_routes_to_application tests/test_agent_workbench_command_schema.py::test_authority_curation_trace_command_is_registered -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add services/agent_workbench/application.py cli/main.py services/agent_workbench/command_registry.py tests/test_agent_workbench_application.py tests/test_agent_workbench_cli.py tests/test_agent_workbench_command_schema.py
git commit -m "feat: add authority curation trace command"
```

---

### Task 6: Reconcile Expired Curation With No Published Candidate

**Files:**
- Modify: `services/agent_workbench/mutation_ledger.py`
- Modify: `services/agent_workbench/authority_curation.py`
- Modify: `tests/test_agent_workbench_mutation_ledger.py`
- Modify: `tests/test_agent_workbench_authority_curation.py`

- [ ] **Step 1: Write failing no-side-effect recovery tests**

In `tests/test_agent_workbench_mutation_ledger.py`, add:

```python
def test_finalize_recovery_as_no_side_effect_failure(
    engine: Engine,
) -> None:
    """A recovery-required row can be reconciled into replayable no-side-effect failure."""
    repo = MutationLedgerRepository(engine=engine)
    now = datetime(2026, 6, 16, 12, tzinfo=UTC)
    loaded = repo.create_or_load(
        command="agileforge authority curate",
        idempotency_key="curate-stale-no-side-effect",
        request_hash="sha256:stale",
        project_id=3,
        correlation_id="corr",
        changed_by="test",
        lease_owner="lease",
        now=now,
    )
    mutation_event_id = require_id(loaded.ledger.mutation_event_id, "mutation_event_id")
    repo.mark_recovery_required(
        mutation_event_id=mutation_event_id,
        lease_owner="lease",
        recovery_action=RecoveryAction.RECONCILE_THEN_RESUME,
        safe_to_auto_resume=False,
        error={"code": "STALE_PENDING", "message": "Pending mutation lease expired."},
        now=now,
    )
    response = {
        "ok": False,
        "data": {},
        "warnings": [],
        "errors": [{"code": "MUTATION_FAILED", "details": {"trace_artifact_id": "authority_curation_trace-1"}}],
    }

    assert repo.finalize_recovery_as_no_side_effect_failure(
        mutation_event_id=mutation_event_id,
        response=response,
        now=now,
    )

    shown = repo.show_event(mutation_event_id=mutation_event_id)
    assert shown["data"]["status"] == "domain_failed_no_side_effects"
    assert shown["data"]["response"] == response
```

In `tests/test_agent_workbench_authority_curation.py`, add:

```python
def test_authority_curate_reconciles_expired_start_without_candidate(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Expired curation before publication returns replayable no-side-effect failure."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {"fsm_state": "SETUP_REQUIRED", "setup_status": "authority_rejected"},
    )
    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)
    request = AuthorityCurationRequest(
        project_id=fixture.project_id,
        spec_version_id=fixture.spec_version_id,
        source_authority_id=fixture.authority_id,
        expected_source_authority_fingerprint=fixture.authority_fingerprint,
        feedback_attempt_id=fixture.feedback_attempt_id,
        idempotency_key="curate-expired-start",
    )

    def fake_run_curation(**_: object) -> dict[str, object]:
        with Session(engine) as session:
            ledger = session.exec(select(CliMutationLedger)).one()
            ledger.lease_expires_at = datetime(2020, 1, 1)
            session.add(ledger)
            session.commit()
        raise RuntimeError("worker died")

    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        fake_run_curation,
    )

    first = runner.curate(request)
    second = runner.curate(request)

    assert first["ok"] is False
    assert second["ok"] is False
    with Session(engine) as session:
        ledger = session.exec(select(CliMutationLedger)).one()
    assert ledger.status == "domain_failed_no_side_effects"
    assert second["errors"][0]["details"]["trace_artifact_id"].startswith(
        "authority_curation_trace-"
    )
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_agent_workbench_mutation_ledger.py::test_finalize_recovery_as_no_side_effect_failure tests/test_agent_workbench_authority_curation.py::test_authority_curate_reconciles_expired_start_without_candidate -q
```

Expected: FAIL because the ledger helper and runner reconciliation path are missing.

- [ ] **Step 3: Add ledger helper**

In `services/agent_workbench/mutation_ledger.py`, add:

```python
    def finalize_recovery_as_no_side_effect_failure(
        self,
        *,
        mutation_event_id: int,
        response: dict[str, Any],
        now: datetime,
    ) -> bool:
        """Convert recovery-required row to replayable no-side-effect failure."""
        db_now = _db_datetime(now)
        with Session(self._engine) as session:
            result = session.exec(
                update(CliMutationLedger)
                .where(_MUTATION_EVENT_ID == mutation_event_id)
                .where(_STATUS == MutationStatus.RECOVERY_REQUIRED.value)
                .values(
                    status=MutationStatus.DOMAIN_FAILED_NO_SIDE_EFFECTS.value,
                    response_json=_json_dump(response),
                    recovery_action=RecoveryAction.NONE.value,
                    recovery_safe_to_auto_resume=False,
                    lease_owner=None,
                    lease_acquired_at=None,
                    last_heartbeat_at=None,
                    lease_expires_at=None,
                    updated_at=db_now,
                )
            )
            session.commit()
            return result.rowcount == 1
```

- [ ] **Step 4: Reconcile no-candidate curation recovery**

In `AuthorityCurationRunner._start_curation_mutation`, when `loaded.error_code == MUTATION_RECOVERY_REQUIRED`, before returning `_curation_ledger_error_response`, call a new helper:

```python
            reconciled = self._reconcile_no_side_effect_curation_recovery(
                request=request,
                mutation_event_id=_required_mutation_event_id(
                    loaded.ledger.mutation_event_id
                ),
            )
            if reconciled is not None:
                return reconciled
```

Add helper:

```python
    def _reconcile_no_side_effect_curation_recovery(
        self,
        *,
        request: AuthorityCurationRequest,
        mutation_event_id: int,
    ) -> dict[str, Any] | None:
        """Recover expired curation if trace/DB prove no candidate was published."""
        summary = summarize_trace(mutation_event_id=mutation_event_id)
        if bool(summary.get("candidate_published")):
            return None
        with Session(self._engine) as session:
            attempt = session.exec(
                select(AuthorityCurationAttempt).where(
                    AuthorityCurationAttempt.mutation_event_id == mutation_event_id
                )
            ).first()
            if attempt is not None and attempt.candidate_authority_id is not None:
                return None
        response = error_envelope(
            command=AUTHORITY_CURATE_COMMAND,
            error=workbench_error(
                ErrorCode.MUTATION_FAILED,
                message=(
                    "Authority curation mutation expired before candidate publication."
                ),
                details={
                    "project_id": request.project_id,
                    "mutation_event_id": mutation_event_id,
                    "trace_artifact_id": summary["trace_artifact_id"],
                    "last_trace_step": summary["last_trace_step"],
                    "last_trace_status": summary["last_trace_status"],
                },
                remediation=[
                    "Retry authority curation with a fresh idempotency key.",
                    "Inspect the trace with agileforge authority curation trace.",
                ],
            ),
            correlation_id=request.correlation_id,
        )
        if attempt is not None:
            self._update_failed_curation_attempt(
                attempt.curation_attempt_id,
                failure_artifact_id=cast("str | None", summary.get("failure_artifact_id")),
            )
        if MutationLedgerRepository(
            engine=self._engine
        ).finalize_recovery_as_no_side_effect_failure(
            mutation_event_id=mutation_event_id,
            response=response,
            now=datetime.now(UTC),
        ):
            return response
        return None
```

- [ ] **Step 5: Run focused recovery tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_agent_workbench_mutation_ledger.py::test_finalize_recovery_as_no_side_effect_failure tests/test_agent_workbench_authority_curation.py::test_authority_curate_reconciles_expired_start_without_candidate -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/agent_workbench/mutation_ledger.py services/agent_workbench/authority_curation.py tests/test_agent_workbench_mutation_ledger.py tests/test_agent_workbench_authority_curation.py
git commit -m "feat: reconcile expired curation before publish"
```

---

### Task 7: Add Published-Candidate Recovery Mode

**Files:**
- Modify: `services/agent_workbench/authority_curation.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `cli/main.py`
- Modify: `api.py`
- Modify: `tests/test_agent_workbench_authority_curation.py`
- Modify: `tests/test_agent_workbench_application.py`
- Modify: `tests/test_agent_workbench_cli.py`
- Modify: `tests/test_api_dashboard.py`

- [ ] **Step 1: Write failing recovery-mode tests**

In `tests/test_agent_workbench_authority_curation.py`, add:

```python
def test_authority_curate_recovery_restores_pending_review_after_publish(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Recovery mode reconciles an already-published curation candidate without ADK."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)

    class FailsPendingReviewWorkflow(FakeWorkflowPort):
        def update_session_status(
            self,
            session_id: str,
            partial_update: dict[str, object],
        ) -> None:
            if partial_update.get("setup_status") == "authority_pending_review":
                raise RuntimeError("workflow down")
            super().update_session_status(session_id, partial_update)

    workflow = FailsPendingReviewWorkflow()
    workflow.update_session_status(
        str(fixture.project_id),
        {"fsm_state": "SETUP_REQUIRED", "setup_status": "authority_rejected"},
    )
    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        lambda **_: _targeted_repair_curation_result(fixture),
    )
    runner = AuthorityCurationRunner(engine=engine, workflow=workflow)
    first = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-recover-published",
        )
    )
    assert first["ok"] is False
    details = first["errors"][0]["details"]
    original_mutation_event_id = details["mutation_event_id"]
    candidate_authority_id = details["candidate_authority_id"]
    candidate_fingerprint = details["candidate_authority_fingerprint"]

    recovery_workflow = FakeWorkflowPort()
    recovery_workflow.update_session_status(
        str(fixture.project_id),
        {"fsm_state": "SETUP_REQUIRED", "setup_status": "authority_curating"},
    )
    recovery_runner = AuthorityCurationRunner(engine=engine, workflow=recovery_workflow)

    recovered = recovery_runner.recover(
        AuthorityCurationRecoveryRequest(
            project_id=fixture.project_id,
            recovery_mutation_event_id=original_mutation_event_id,
            expected_candidate_authority_id=candidate_authority_id,
            expected_candidate_authority_fingerprint=candidate_fingerprint,
            idempotency_key="recover-curate-published",
        )
    )

    assert recovered["ok"] is True
    assert recovered["data"]["pending_authority_id"] == candidate_authority_id
    assert recovered["data"]["recovered_mutation_event_id"] == original_mutation_event_id
    assert recovery_workflow.get_session_status(str(fixture.project_id))[
        "setup_status"
    ] == "authority_pending_review"
    with Session(engine) as session:
        rows = session.exec(select(CliMutationLedger)).all()
    by_id = {row.mutation_event_id: row for row in rows}
    assert by_id[original_mutation_event_id].status == "superseded"
```

In `tests/test_agent_workbench_application.py`, add recovery delegation test:

```python
def test_application_authority_curate_delegates_recovery_to_runner() -> None:
    """Recovery args build a recovery request instead of normal curation request."""
    runner = _FakeAuthorityCurationRunner()
    app = AgentWorkbenchApplication(authority_curation_runner=runner)

    result = app.authority_curate(
        project_id=PROJECT_ID,
        recovery_mutation_event_id=647,
        expected_candidate_authority_id=7,
        expected_candidate_authority_fingerprint="sha256:" + ("a" * 64),
        idempotency_key="recover-curate-app-001",
    )

    assert result["ok"] is True
    assert runner.calls[0][0] == "recover"
    assert runner.calls[0][1].recovery_mutation_event_id == 647
```

Add CLI/API tests mirroring the command:

```python
agileforge authority curate \
  --project-id 3 \
  --recovery-mutation-event-id 647 \
  --expected-candidate-authority-id 7 \
  --expected-candidate-authority-fingerprint sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --idempotency-key recover-key
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_agent_workbench_authority_curation.py::test_authority_curate_recovery_restores_pending_review_after_publish tests/test_agent_workbench_application.py::test_application_authority_curate_delegates_recovery_to_runner -q
```

Expected: FAIL because recovery request/runner path is missing.

- [ ] **Step 3: Add recovery request model and runner method**

In `services/agent_workbench/authority_curation.py`, add:

```python
class AuthorityCurationRecoveryRequest(_StrictModel):
    """Guarded request for published authority curation recovery."""

    project_id: int
    recovery_mutation_event_id: int
    expected_candidate_authority_id: int
    expected_candidate_authority_fingerprint: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    changed_by: str = "cli-agent"
    correlation_id: str | None = None
```

Add `recover(self, request: AuthorityCurationRecoveryRequest) -> dict[str, Any]` to `AuthorityCurationRunner`.

Implementation outline:

```python
    def recover(self, request: AuthorityCurationRecoveryRequest) -> dict[str, Any]:
        """Recover a curation mutation after candidate publication."""
        now = datetime.now(UTC)
        ledger = MutationLedgerRepository(engine=self._engine)
        original_owner = (
            "authority-curation-recovery:"
            f"{request.idempotency_key}:recovers:{request.recovery_mutation_event_id}"
        )
        original = ledger.show_event(
            mutation_event_id=request.recovery_mutation_event_id
        )
        original_data = _require_recoverable_curate_event(
            original,
            project_id=request.project_id,
            recovery_mutation_event_id=request.recovery_mutation_event_id,
        )
        linked_retry = ledger.start_linked_retry(
            command="agileforge authority curate",
            project_id=request.project_id,
            idempotency_key=request.idempotency_key,
            recovers_mutation_event_id=request.recovery_mutation_event_id,
            command_payload=_authority_curation_recovery_payload(request),
            now=now,
        )
        recovery_lease = ledger.acquire_recovery_lease(
            mutation_event_id=request.recovery_mutation_event_id,
            owner=original_owner,
            now=now,
        )
        candidate = self._authority_repository.get_authority(
            authority_id=request.expected_candidate_authority_id,
        )
        _validate_recovered_candidate(
            candidate,
            expected_fingerprint=request.expected_candidate_authority_fingerprint,
            project_id=request.project_id,
        )
        attempt = _latest_curation_attempt_for_mutation(
            session=session,
            mutation_event_id=request.recovery_mutation_event_id,
        )
        _update_succeeded_curation_attempt(
            attempt,
            candidate_authority_id=request.expected_candidate_authority_id,
            candidate_authority_fingerprint=request.expected_candidate_authority_fingerprint,
            now=now,
        )
        _mark_workflow_pending_review(
            project_id=request.project_id,
            authority_id=request.expected_candidate_authority_id,
            authority_fingerprint=request.expected_candidate_authority_fingerprint,
        )
        response = _authority_curation_recovery_success_response(
            request=request,
            original_data=original_data,
            linked_retry_event_id=linked_retry.mutation_event_id,
        )
        ledger.finalize_linked_retry_success(
            linked_retry_event_id=linked_retry.mutation_event_id,
            original_mutation_event_id=request.recovery_mutation_event_id,
            recovery_lease_id=recovery_lease.lease_id,
            replay_response=response,
            original_replay_response=response,
            now=now,
        )
        return response
```

Use existing helpers `_mark_workflow_pending_review`, `_update_succeeded_curation_attempt`, `_published_curation_candidate`, and `pending_authority_fingerprint`.

The response data must include:

```python
{
    "status": "authority_pending_review",
    "project_id": request.project_id,
    "recovered_mutation_event_id": request.recovery_mutation_event_id,
    "recovery_mutation_event_id": retry_mutation_event_id,
    "pending_authority_id": request.expected_candidate_authority_id,
    "pending_authority_fingerprint": request.expected_candidate_authority_fingerprint,
    "trace_artifact_id": trace_artifact_id(request.recovery_mutation_event_id),
}
```

- [ ] **Step 4: Route recovery from application/CLI/API**

In `services/agent_workbench/application.py` `_AuthorityCurationRunner` protocol, add `recover(...)`.

Update `AgentWorkbenchApplication.authority_curate(...)` signature to make normal curation args optional and add:

```python
        recovery_mutation_event_id: int | None = None,
        expected_candidate_authority_id: int | None = None,
        expected_candidate_authority_fingerprint: str | None = None,
```

If `recovery_mutation_event_id is not None`, build `AuthorityCurationRecoveryRequest` and call `runner.recover(...)`. Otherwise require normal curation args and build `AuthorityCurationRequest`.

In `cli/main.py`, make normal `authority curate` args optional in parser, add recovery args, and in `_authority_curate` pass all fields through. If recovery is absent and any normal required value is missing, raise `_CliParseError("normal authority curate requires --spec-version-id, --source-authority-id, --expected-source-authority-fingerprint, and --feedback-attempt-id")`. If recovery is present and any normal-only args are also present, raise `_CliParseError("authority curate recovery cannot include normal curation inputs")`.

In `api.py`, add optional recovery fields to `AuthorityCurateApiRequest` and a `model_validator(mode="after")` that enforces mutual exclusivity.

- [ ] **Step 5: Run focused recovery tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_agent_workbench_authority_curation.py::test_authority_curate_recovery_restores_pending_review_after_publish tests/test_agent_workbench_application.py::test_application_authority_curate_delegates_recovery_to_runner tests/test_agent_workbench_cli.py::test_authority_curate_recovery_cli_routes_to_application tests/test_api_dashboard.py::test_authority_curate_api_routes_recovery_request -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/agent_workbench/authority_curation.py services/agent_workbench/application.py cli/main.py api.py tests/test_agent_workbench_authority_curation.py tests/test_agent_workbench_application.py tests/test_agent_workbench_cli.py tests/test_api_dashboard.py
git commit -m "feat: recover published authority curation candidates"
```

---

### Task 8: Update Contracts And Next-Action Surfaces

**Files:**
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `tests/test_agent_workbench_command_schema.py`
- Modify: `tests/test_agent_workbench_application.py`

- [ ] **Step 1: Write failing contract tests**

In `tests/test_agent_workbench_command_schema.py`, update `test_authority_curate_command_is_registered`:

```python
    assert schema["input"]["required"] == ["project_id", "idempotency_key"]
    assert schema["input"]["optional"] == [
        "spec_version_id",
        "source_authority_id",
        "expected_source_authority_fingerprint",
        "feedback_attempt_id",
        "max_iterations",
        "compiler_model",
        "recovery_mutation_event_id",
        "expected_candidate_authority_id",
        "expected_candidate_authority_fingerprint",
        "changed_by",
        "correlation_id",
    ]
```

In `tests/test_agent_workbench_application.py`, add:

```python
class _CuratingRecoveryAuthorityProjection(_RejectedAuthorityProjection):
    """Fake rejected authority projection with recoverable curation metadata."""

    def status(self, *, project_id: int) -> dict[str, Any]:
        """Return rejected authority status with a published curation candidate."""
        result = super().status(project_id=project_id)
        result["data"].update(
            {
                "latest_curation_attempt_id": "curation-123",
                "latest_curation_status": "recovery_required",
                "latest_curation_mutation_event_id": 647,
                "latest_curation_candidate_authority_id": 7,
                "latest_curation_candidate_authority_fingerprint": "sha256:" + ("a" * 64),
            }
        )
        return result


def test_workflow_next_after_curation_recovery_includes_recovery_command() -> None:
    """Recovery-required curation points to authority curate recovery mode."""
    app = AgentWorkbenchApplication(
        read_projection=_WorkflowStateReader(
            {
                "fsm_state": "SETUP_REQUIRED",
                "setup_status": "authority_curating",
                "setup_spec_version_id": 4,
            }
        ),
        authority_projection=_CuratingRecoveryAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    command = result["data"]["next_valid_commands"][0]
    assert "--recovery-mutation-event-id 647" in command
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_agent_workbench_command_schema.py::test_authority_curate_command_is_registered tests/test_agent_workbench_application.py::test_workflow_next_after_curation_recovery_includes_recovery_command -q
```

Expected: FAIL until command metadata and next-action formatting are updated.

- [ ] **Step 3: Update command metadata**

In `services/agent_workbench/command_registry.py`, update `agileforge authority curate`:

```python
        input_required=("project_id", "idempotency_key"),
        input_optional=(
            "spec_version_id",
            "source_authority_id",
            "expected_source_authority_fingerprint",
            "feedback_attempt_id",
            "max_iterations",
            "compiler_model",
            "recovery_mutation_event_id",
            "expected_candidate_authority_id",
            "expected_candidate_authority_fingerprint",
            "changed_by",
            "correlation_id",
        ),
```

- [ ] **Step 4: Update next-action formatting**

In `services/agent_workbench/application.py`, update the authority curation next-action builder to include trace/recovery commands when workflow state contains `setup_curation_mutation_event_id`, candidate id, and fingerprint. Keep normal rejected-with-feedback path unchanged.

The recovery command string must include:

```text
agileforge authority curate --project-id <project_id> --recovery-mutation-event-id <id> --expected-candidate-authority-id <id> --expected-candidate-authority-fingerprint <sha> --idempotency-key <idempotency_key>
```

- [ ] **Step 5: Run focused contract tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_agent_workbench_command_schema.py::test_authority_curate_command_is_registered tests/test_agent_workbench_command_schema.py::test_authority_curation_trace_command_is_registered tests/test_agent_workbench_application.py::test_workflow_next_prefers_authority_curate_after_feedback tests/test_agent_workbench_application.py::test_workflow_next_after_curation_recovery_includes_recovery_command -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/agent_workbench/command_registry.py services/agent_workbench/application.py tests/test_agent_workbench_command_schema.py tests/test_agent_workbench_application.py
git commit -m "feat: publish authority curation recovery contracts"
```

---

### Task 9: Final Verification

**Files:**
- Verify all touched files.

- [ ] **Step 1: Run focused authority curation suite**

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_authority_curation_trace.py tests/test_agent_workbench_authority_curation.py tests/test_agent_workbench_authority_projection.py tests/test_agent_workbench_mutation_ledger.py -q
```

Expected: PASS.

- [ ] **Step 2: Run command/API focused suite**

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_agent_workbench_cli.py tests/test_agent_workbench_application.py tests/test_agent_workbench_command_schema.py tests/test_api_dashboard.py -q
```

Expected: PASS.

- [ ] **Step 3: Run migration/readiness focused suite**

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge uv run pytest tests/test_db_migrations.py tests/test_agent_workbench_schema_readiness.py -q
```

Expected: PASS.

- [ ] **Step 4: Run repository gate**

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge pyrepo-check --all
```

Expected: PASS with no lint, type, or test failures.

- [ ] **Step 5: Inspect git status**

```bash
git status --short
```

Expected: no output.

---

## Self-Review Checklist

- Spec coverage:
  - Durable JSONL traces: Task 1, Task 3.
  - `AuthorityCurationAttempt.mutation_event_id`: Task 2.
  - Trace metadata in responses/status/mutation show: Task 3, Task 4.
  - Read-only trace command: Task 5.
  - No-side-effect stale recovery: Task 6.
  - Published-candidate recovery without ADK rerun: Task 7.
  - Command/API contracts: Task 7, Task 8.
  - Security redaction: Task 1, Task 3, Task 9.
- No OpenTelemetry dependency is added.
- No raw prompt, feedback, source authority, candidate authority, or model output is stored in default traces.
- Recovery mode is explicit and idempotent.
- Existing normal `authority curate` behavior remains available.
