# Brownfield Product-Spec Curation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a brownfield setup mode that records raw sources and scans outside `SpecRegistry`, lets a human review or import a curated `agileforge.spec.v1`, and only bridges to `authority_compile_required` through guarded approval.

**Architecture:** Keep the first implementation CLI-first. Add dedicated SQLModel tables for source, scan, draft, and approval history; add a brownfield curation runner behind `AgentWorkbenchApplication`; extend project create for shell-only brownfield setup; and reuse the existing `CliMutationLedger` pattern for idempotency and recovery. Dashboard project creation stays greenfield-only in this slice.

**Tech Stack:** Python 3.13, SQLModel, Pydantic, existing AgileForge CLI envelopes, `CliMutationLedger`, `WorkflowService`, `agileforge.spec.v1` profile validation, existing `uv run --frozen pytest` test path.

---

## File Structure

- Create `models/brownfield.py`: SQLModel rows for `brownfield_source_artifacts`, `brownfield_scan_attempts`, `brownfield_spec_draft_attempts`, and `brownfield_spec_approvals`.
- Modify `models/__init__.py`, `models/db.py`, and `agile_sqlmodel.py`: register brownfield models so `SQLModel.metadata.create_all()` creates the tables.
- Modify `repositories/product.py`: delete brownfield rows before deleting a product.
- Create `services/agent_workbench/brownfield_curation.py`: request models, runner, artifact hashing, source import, repo scan, deterministic draft, human import, approval bridge, and progress projection helpers.
- Modify `services/agent_workbench/project_setup.py`: add `setup_mode`, greenfield/brownfield create validation, shell-only brownfield workflow state, and authority compile brownfield metadata guard.
- Modify `services/agent_workbench/application.py`: expose brownfield runner methods, include derived progress in `status`, and route `workflow next` for `brownfield_curation_required`.
- Modify `services/agent_workbench/error_codes.py`: register brownfield guard error codes.
- Modify `services/agent_workbench/command_registry.py`: register brownfield command contracts and update `project create` inputs.
- Modify `cli/main.py`: add `--setup-mode` to `project create` and add `brownfield source import`, `brownfield scan`, `brownfield spec draft`, `brownfield spec import`, and `brownfield spec approve`.
- Modify `api.py`: keep `/api/projects` greenfield-only and make the contract explicit in tests.
- Modify `docs/agent-cli-manual.md`: document the brownfield CLI path after implementation tests pass.
- Tests:
  - Create `tests/test_brownfield_models.py`
  - Create `tests/test_agent_workbench_brownfield_curation.py`
  - Modify `tests/test_business_db_bootstrap.py`
  - Modify `tests/test_agent_workbench_project_setup.py`
  - Modify `tests/test_agent_workbench_project_create_cli_integration.py`
  - Modify `tests/test_agent_workbench_application.py`
  - Modify `tests/test_agent_workbench_command_schema.py`
  - Modify `tests/test_agent_workbench_error_codes.py`
  - Modify `tests/test_api_dashboard.py`

## Command Contracts

`agileforge project create`

- `--setup-mode greenfield` is the default.
- Greenfield requires `--spec-file` and behaves as it does now.
- Brownfield requires no spec file and rejects `--spec-file`, `--source-file`, and `--repo-path`.
- Brownfield writes workflow state:

```python
{
    "fsm_state": "SETUP_REQUIRED",
    "setup_mode": "brownfield",
    "setup_status": "brownfield_curation_required",
    "setup_error": None,
    "setup_spec_file_path": None,
    "setup_spec_hash": None,
    "setup_spec_version_id": None,
    "setup_next_actions": [
        {
            "command": "agileforge brownfield source import",
            "args": {"project_id": project_id},
            "reason": "Record raw brownfield source before drafting a curated spec.",
        },
        {
            "command": "agileforge brownfield scan",
            "args": {"project_id": project_id},
            "reason": "Record repository facts before drafting a curated spec.",
        },
    ],
}
```

`agileforge brownfield source import`

- Required: `--project-id`, `--source-file`, `--idempotency-key`.
- Optional: `--source-kind`, `--correlation-id`, `--changed-by`.
- Request fingerprint includes command name, project id, resolved source path, file SHA-256, source kind, and changed by.
- Mutation attempt id is `source-{mutation_event_id}`.
- Replay with same idempotency key and same request returns the original response.
- Same key with changed request returns `IDEMPOTENCY_KEY_REUSED`.
- Source rows never write `SpecRegistry` or setup spec fields.

`agileforge brownfield scan`

- Required: `--project-id`, `--repo-path`, `--idempotency-key`.
- Optional: `--source-attempt-id`, `--correlation-id`, `--changed-by`.
- Request fingerprint includes command name, project id, resolved repo path, git commit, dirty flag, scannable file manifest hash, optional source attempt id, and changed by.
- Mutation attempt id is `scan-{mutation_event_id}`.
- If `--source-attempt-id` is supplied, it must exist, belong to the project, and be complete.
- If no source is supplied, use source fingerprint `sha256:no-source`.
- Scan rows never write `SpecRegistry` or setup spec fields.

`agileforge brownfield spec draft`

- Required: `--project-id`, `--scan-attempt-id`, `--idempotency-key`.
- Optional: `--user-input`, `--correlation-id`, `--changed-by`.
- Request fingerprint includes command name, project id, scan fingerprint, source fingerprint, user input hash, drafter version, and changed by.
- Mutation attempt id is `draft-{mutation_event_id}`.
- The deterministic v1 drafter accepts typed source lines such as `REQ:`, `DECISION:`, `NON_GOAL:`, `RISK:`, and `OPEN_QUESTION:`. Untyped source with no product-level items creates an incomplete draft with an `OPEN_QUESTION` item and cannot be approved.
- Draft rows never write `SpecRegistry` or setup spec fields.

`agileforge brownfield spec import`

- Required: `--project-id`, `--curated-spec-file`, `--expected-scan-fingerprint`, `--idempotency-key`.
- Optional: `--parent-draft-attempt-id`, `--correlation-id`, `--changed-by`.
- Request fingerprint includes command name, project id, resolved curated spec path, normalized spec hash, expected scan fingerprint, optional parent draft id, and changed by.
- Mutation attempt id is `draft-import-{mutation_event_id}`.
- Imported rows use `origin="human_import"` and are approvable when profile validation passes and the expected scan fingerprint is current.

`agileforge brownfield spec approve`

- Required: `--project-id`, `--attempt-id`, `--expected-artifact-fingerprint`, `--expected-state`, `--expected-setup-status`, `--idempotency-key`.
- Optional: `--correlation-id`, `--changed-by`.
- Request fingerprint includes all guards, draft fingerprint, source fingerprint, scan fingerprint, normalized spec hash, and changed by.
- Mutation attempt id is `approval-{mutation_event_id}`.
- Approval writes the managed approved spec file, registers `SpecRegistry`, records `brownfield_spec_approvals`, writes workflow setup spec fields, transitions to `authority_compile_required`, and finalizes ledger response.

## Task 1: Brownfield Persistence Models

**Files:**
- Create: `models/brownfield.py`
- Modify: `models/__init__.py`
- Modify: `models/db.py`
- Modify: `agile_sqlmodel.py`
- Modify: `repositories/product.py`
- Modify: `tests/test_business_db_bootstrap.py`
- Create: `tests/test_brownfield_models.py`

- [ ] **Step 1: Write failing bootstrap test**

Add to `tests/test_business_db_bootstrap.py`:

```python
def test_ensure_business_db_ready_creates_brownfield_tables(tmp_path: Path) -> None:
    """Verify business DB bootstrap creates brownfield artifact tables."""
    db_path = tmp_path / "business_bootstrap_brownfield.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    ensure_business_db_ready(engine_override=engine)

    expected = {
        "brownfield_source_artifacts",
        "brownfield_scan_attempts",
        "brownfield_spec_draft_attempts",
        "brownfield_spec_approvals",
    }
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()

    assert expected.issubset({row[0] for row in rows})
```

- [ ] **Step 2: Run failing bootstrap test**

Run:

```bash
uv run --frozen pytest tests/test_business_db_bootstrap.py::test_ensure_business_db_ready_creates_brownfield_tables -q
```

Expected: fails because the brownfield tables do not exist.

- [ ] **Step 3: Add brownfield SQLModel classes**

Create `models/brownfield.py`:

```python
"""Brownfield setup artifact persistence models."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.schema import UniqueConstraint
from sqlalchemy.types import Text
from sqlmodel import Field, SQLModel


def _utc_now() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(UTC)


class BrownfieldSourceArtifact(SQLModel, table=True):
    """Raw brownfield source artifact recorded before spec curation."""

    __tablename__ = "brownfield_source_artifacts"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "attempt_id",
            name="uq_brownfield_source_project_attempt",
        ),
    )

    source_artifact_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id", index=True)
    attempt_id: str = Field(index=True)
    artifact_fingerprint: str = Field(index=True)
    source_kind: str = Field(default="source_file", index=True)
    source_file_path: str | None = Field(default=None, sa_type=Text)
    source_sha256: str | None = Field(default=None, index=True)
    content_preview: str | None = Field(default=None, sa_type=Text)
    status: str = Field(default="complete", index=True)
    request_hash: str = Field(index=True)
    warning_metadata_json: str = Field(default="[]", sa_type=Text)
    error_metadata_json: str = Field(default="[]", sa_type=Text)
    tool_version: str = Field(default="brownfield-curation.v1", index=True)
    created_at: datetime = Field(default_factory=_utc_now, nullable=False)


class BrownfieldScanAttempt(SQLModel, table=True):
    """Repository scan facts recorded before spec curation."""

    __tablename__ = "brownfield_scan_attempts"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "attempt_id",
            name="uq_brownfield_scan_project_attempt",
        ),
    )

    scan_attempt_pk: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id", index=True)
    attempt_id: str = Field(index=True)
    artifact_fingerprint: str = Field(index=True)
    source_attempt_id: str | None = Field(default=None, index=True)
    source_fingerprint: str = Field(index=True)
    repo_path: str = Field(sa_type=Text)
    repo_commit: str | None = Field(default=None, index=True)
    repo_dirty: bool = Field(default=False, index=True)
    file_manifest_json: str = Field(default="[]", sa_type=Text)
    implementation_facts_json: str = Field(default="[]", sa_type=Text)
    status: str = Field(default="complete", index=True)
    request_hash: str = Field(index=True)
    warning_metadata_json: str = Field(default="[]", sa_type=Text)
    error_metadata_json: str = Field(default="[]", sa_type=Text)
    tool_version: str = Field(default="brownfield-curation.v1", index=True)
    created_at: datetime = Field(default_factory=_utc_now, nullable=False)


class BrownfieldSpecDraftAttempt(SQLModel, table=True):
    """Generated or imported curated spec candidate."""

    __tablename__ = "brownfield_spec_draft_attempts"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "attempt_id",
            name="uq_brownfield_draft_project_attempt",
        ),
    )

    draft_attempt_pk: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id", index=True)
    attempt_id: str = Field(index=True)
    artifact_fingerprint: str = Field(index=True)
    origin: str = Field(index=True)
    status: str = Field(index=True)
    source_fingerprint: str = Field(index=True)
    scan_attempt_id: str = Field(index=True)
    scan_fingerprint: str = Field(index=True)
    parent_draft_attempt_id: str | None = Field(default=None, index=True)
    spec_hash: str | None = Field(default=None, index=True)
    curated_spec_json: str | None = Field(default=None, sa_type=Text)
    imported_file_path: str | None = Field(default=None, sa_type=Text)
    request_hash: str = Field(index=True)
    user_input_hash: str | None = Field(default=None, index=True)
    warning_metadata_json: str = Field(default="[]", sa_type=Text)
    error_metadata_json: str = Field(default="[]", sa_type=Text)
    tool_version: str = Field(default="brownfield-curation.v1", index=True)
    created_at: datetime = Field(default_factory=_utc_now, nullable=False)


class BrownfieldSpecApproval(SQLModel, table=True):
    """Approval bridge from brownfield draft to SpecRegistry."""

    __tablename__ = "brownfield_spec_approvals"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint(
            "approval_fingerprint",
            name="uq_brownfield_approval_fingerprint",
        ),
    )

    approval_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id", index=True)
    approval_attempt_id: str = Field(index=True)
    approval_fingerprint: str = Field(index=True)
    draft_attempt_id: str = Field(index=True)
    draft_fingerprint: str = Field(index=True)
    scan_fingerprint: str = Field(index=True)
    source_fingerprint: str = Field(index=True)
    spec_hash: str = Field(index=True)
    spec_version_id: int | None = Field(default=None, index=True)
    managed_spec_file_path: str | None = Field(default=None, sa_type=Text)
    mutation_event_id: int | None = Field(default=None, index=True)
    status: str = Field(default="started", index=True)
    error_metadata_json: str = Field(default="[]", sa_type=Text)
    created_at: datetime = Field(default_factory=_utc_now, nullable=False)
    updated_at: datetime = Field(default_factory=_utc_now, nullable=False)
```

- [ ] **Step 4: Wire models into metadata**

In `models/__init__.py`, extend `__all__`:

```python
__all__ = ["agent_workbench", "brownfield", "core", "db", "enums", "events", "specs"]
```

In `models/db.py`, add the import next to `models.agent_workbench`:

```python
from models import brownfield as _brownfield_models  # noqa: F401
```

In `agile_sqlmodel.py`, import and export the brownfield classes:

```python
from models.brownfield import (
    BrownfieldScanAttempt,
    BrownfieldSourceArtifact,
    BrownfieldSpecApproval,
    BrownfieldSpecDraftAttempt,
)
```

Add their names to `__all__`.

- [ ] **Step 5: Add model uniqueness test**

Create `tests/test_brownfield_models.py`:

```python
"""Tests for brownfield artifact persistence models."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Engine
from sqlmodel import Session

from models.brownfield import BrownfieldSourceArtifact
from models.core import Product


def test_source_attempt_is_unique_per_project(engine: Engine) -> None:
    """Verify duplicate source attempt ids are rejected per project."""
    with Session(engine) as session:
        product = Product(name="Brownfield Product")
        session.add(product)
        session.commit()
        session.refresh(product)
        assert product.product_id is not None

        first = BrownfieldSourceArtifact(
            project_id=product.product_id,
            attempt_id="source-1",
            artifact_fingerprint="sha256:first",
            source_sha256="sha256:first-source",
            request_hash="sha256:request",
        )
        duplicate = BrownfieldSourceArtifact(
            project_id=product.product_id,
            attempt_id="source-1",
            artifact_fingerprint="sha256:second",
            source_sha256="sha256:second-source",
            request_hash="sha256:request-2",
        )
        session.add(first)
        session.commit()

        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()
```

- [ ] **Step 6: Delete brownfield rows with product deletion**

In `repositories/product.py`, import brownfield models:

```python
from models.brownfield import (
    BrownfieldScanAttempt,
    BrownfieldSourceArtifact,
    BrownfieldSpecApproval,
    BrownfieldSpecDraftAttempt,
)
```

Before deleting `SpecRegistry`, add:

```python
            for model in (
                BrownfieldSpecApproval,
                BrownfieldSpecDraftAttempt,
                BrownfieldScanAttempt,
                BrownfieldSourceArtifact,
            ):
                for row in session.exec(
                    select(model).where(model.project_id == product_id)
                ).all():
                    session.delete(row)
```

- [ ] **Step 7: Verify model tests**

Run:

```bash
uv run --frozen pytest tests/test_business_db_bootstrap.py::test_ensure_business_db_ready_creates_brownfield_tables tests/test_brownfield_models.py -q
```

Expected: pass.

- [ ] **Step 8: Commit storage foundation**

Run:

```bash
git add models/brownfield.py models/__init__.py models/db.py agile_sqlmodel.py repositories/product.py tests/test_business_db_bootstrap.py tests/test_brownfield_models.py
git commit -m "feat: add brownfield artifact tables"
```

## Task 2: Brownfield Project Shell

**Files:**
- Modify: `services/agent_workbench/project_setup.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `cli/main.py`
- Modify: `tests/test_agent_workbench_project_setup.py`
- Modify: `tests/test_agent_workbench_project_create_cli_integration.py`

- [ ] **Step 1: Add failing shell-create tests**

Add to `tests/test_agent_workbench_project_setup.py`:

```python
def test_brownfield_project_create_rejects_spec_file(engine: Engine) -> None:
    """Brownfield create is shell-only and rejects spec input."""
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)

    with pytest.raises(ValidationError):
        ProjectCreateRequest(
            name="Brownfield With Spec",
            setup_mode="brownfield",
            spec_file="specs/spec.json",
            idempotency_key="brownfield-with-spec-001",
        )


def test_brownfield_project_create_writes_shell_without_spec_registry(
    engine: Engine,
) -> None:
    """Brownfield create writes workflow shell but no SpecRegistry row."""
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)

    result = runner.create_project(
        ProjectCreateRequest(
            name="Brownfield Shell",
            setup_mode="brownfield",
            spec_file=None,
            idempotency_key="brownfield-shell-001",
            changed_by="agent",
        )
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["setup_mode"] == "brownfield"
    assert data["setup_status"] == "brownfield_curation_required"
    assert data["spec_hash"] is None
    assert data["spec_version_id"] is None
    assert data["next_actions"][0]["command"] == "agileforge brownfield source import"
    project_id = data["project_id"]
    assert workflow.sessions[str(project_id)]["setup_status"] == (
        "brownfield_curation_required"
    )
    assert workflow.sessions[str(project_id)]["setup_spec_file_path"] is None

    with Session(engine) as session:
        assert session.exec(select(Product)).one().name == "Brownfield Shell"
        assert session.exec(select(SpecRegistry)).all() == []
```

- [ ] **Step 2: Run failing shell-create tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_project_setup.py::test_brownfield_project_create_rejects_spec_file tests/test_agent_workbench_project_setup.py::test_brownfield_project_create_writes_shell_without_spec_registry -q
```

Expected: fails because `ProjectCreateRequest.spec_file` is required and no brownfield branch exists.

- [ ] **Step 3: Extend project create request validation**

In `services/agent_workbench/project_setup.py`, add constants near setup status constants:

```python
BROWNFIELD_CURATION_REQUIRED = "brownfield_curation_required"
GREENFIELD_SETUP_MODE = "greenfield"
BROWNFIELD_SETUP_MODE = "brownfield"
```

Update `ProjectCreateRequest`:

```python
class ProjectCreateRequest(BaseModel):
    """Validated request for `agileforge project create`."""

    name: str = Field(min_length=1)
    spec_file: str | None = None
    setup_mode: str = GREENFIELD_SETUP_MODE
    idempotency_key: str | None = None
    dry_run: bool = False
    dry_run_id: str | None = None
    correlation_id: str | None = None
    changed_by: str = "cli-agent"

    @model_validator(mode="after")
    def _validate_mutation_keys(self) -> ProjectCreateRequest:
        _validate_key_mode(
            dry_run=self.dry_run,
            idempotency_key=self.idempotency_key,
            dry_run_id=self.dry_run_id,
        )
        if self.setup_mode not in {GREENFIELD_SETUP_MODE, BROWNFIELD_SETUP_MODE}:
            raise ValueError("setup_mode must be greenfield or brownfield")
        if self.setup_mode == GREENFIELD_SETUP_MODE and not self.spec_file:
            raise ValueError("spec_file is required for greenfield project create")
        if self.setup_mode == BROWNFIELD_SETUP_MODE and self.spec_file is not None:
            raise ValueError("spec_file is forbidden for brownfield project create")
        return self
```

- [ ] **Step 4: Add brownfield shell workflow helper**

In `services/agent_workbench/project_setup.py`, add helper functions near `_authority_compile_action`:

```python
def _brownfield_source_import_action(project_id: int) -> dict[str, Any]:
    return {
        "command": "agileforge brownfield source import",
        "args": {"project_id": project_id},
        "reason": "Record raw brownfield source before drafting a curated spec.",
    }


def _brownfield_scan_action(project_id: int) -> dict[str, Any]:
    return {
        "command": "agileforge brownfield scan",
        "args": {"project_id": project_id},
        "reason": "Record repository facts before drafting a curated spec.",
    }
```

Add a runner method:

```python
    def _run_brownfield_create(self, request: ProjectCreateRequest) -> dict[str, Any]:
        existing_key_row = self._find_ledger(
            command=PROJECT_CREATE_COMMAND,
            idempotency_key=_required(request.idempotency_key),
        )
        if existing_key_row is None and self._product_name_exists(request.name):
            return _error(
                PROJECT_ALREADY_EXISTS,
                details={"name": request.name},
                remediation=["Choose a different project name."],
            )
        request_hash = canonical_hash(
            {
                "command": PROJECT_CREATE_COMMAND,
                "name": request.name,
                "setup_mode": BROWNFIELD_SETUP_MODE,
                "changed_by": request.changed_by,
            }
        )
        if request.dry_run:
            return _success(
                {
                    "preview_available": True,
                    "name": request.name,
                    "setup_mode": BROWNFIELD_SETUP_MODE,
                    "setup_status": BROWNFIELD_CURATION_REQUIRED,
                }
            )
        loaded = self._ledger.create_or_load(
            command=PROJECT_CREATE_COMMAND,
            idempotency_key=_required(request.idempotency_key),
            request_hash=request_hash,
            project_id=None,
            correlation_id=_correlation_id(request.correlation_id),
            changed_by=request.changed_by,
            lease_owner=_lease_owner(
                command=PROJECT_CREATE_COMMAND,
                idempotency_key=_required(request.idempotency_key),
                correlation_id=request.correlation_id,
            ),
            now=_now(),
            lease_seconds=self._lease_seconds,
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code == MUTATION_RECOVERY_REQUIRED:
            return _recovery_required_response(loaded.ledger, "<brownfield>")
        if loaded.error_code is not None:
            return _error_for_ledger(loaded.error_code, loaded.ledger)

        mutation_event_id = _event_id(loaded.ledger)
        lease_owner = _required(loaded.ledger.lease_owner)
        project_id = self._create_product_and_record_progress(
            name=request.name,
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
        )
        workflow_result = self._ensure_brownfield_shell_workflow(
            project_id=project_id,
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
        )
        if not workflow_result.get("ok"):
            marked = self._mark_create_recovery_required(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                project_id=project_id,
                code=WORKFLOW_SESSION_FAILED,
                spec_file="<brownfield>",
                safe_to_auto_resume=True,
            )
            if isinstance(marked, dict):
                return marked
            return _recovery_required_response(marked, "<brownfield>")

        data = {
            "project_id": project_id,
            "name": self._project_name(project_id),
            "setup_mode": BROWNFIELD_SETUP_MODE,
            "setup_status": BROWNFIELD_CURATION_REQUIRED,
            "fsm_state": "SETUP_REQUIRED",
            "spec_hash": None,
            "spec_version_id": None,
            "mutation_event_id": mutation_event_id,
            "next_actions": [
                _brownfield_source_import_action(project_id),
                _brownfield_scan_action(project_id),
            ],
        }
        response = _success(data)
        if not self._ledger.finalize_success(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            after=data,
            response=response,
            now=_now(),
        ):
            return _error(
                ErrorCode.MUTATION_RESUME_CONFLICT.value,
                details={"mutation_event_id": mutation_event_id},
                remediation=["Re-read mutation state before retrying recovery."],
            )
        return response
```

Add `_ensure_brownfield_shell_workflow`:

```python
    def _ensure_brownfield_shell_workflow(
        self,
        *,
        project_id: int,
        mutation_event_id: int,
        lease_owner: str,
    ) -> dict[str, Any]:
        session_id = str(project_id)
        current = self._workflow.get_session_status(session_id)
        if current == {}:
            if not self._ledger.require_active_owner(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                now=_now(),
                lease_seconds=self._lease_seconds,
            ):
                return {"ok": False, "error_code": MUTATION_IN_PROGRESS}
            self._workflow.initialize_session(session_id=session_id)
        if not self._ledger.mark_step_complete(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            step="workflow_session_created",
            next_step="workflow_session_created",
            now=_now(),
        ):
            return {"ok": False, "error_code": MUTATION_RECOVERY_REQUIRED}

        required_state = {
            "fsm_state": "SETUP_REQUIRED",
            "setup_mode": BROWNFIELD_SETUP_MODE,
            "setup_status": BROWNFIELD_CURATION_REQUIRED,
            "setup_error": None,
            "setup_spec_file_path": None,
            "setup_spec_hash": None,
            "setup_spec_version_id": None,
            "setup_next_actions": [
                _brownfield_source_import_action(project_id),
                _brownfield_scan_action(project_id),
            ],
        }
        self._workflow.update_session_status(session_id, required_state)
        if not self._ledger.mark_step_complete(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            step="workflow_session_status_written",
            next_step="workflow_session_status_written",
            now=_now(),
        ):
            return {"ok": False, "error_code": MUTATION_RECOVERY_REQUIRED}
        return {"ok": True, "session_id": session_id, "state": required_state}
```

- [ ] **Step 5: Route create by setup mode**

In `_run_create`, branch before resolving `spec_file`:

```python
        if request.setup_mode == BROWNFIELD_SETUP_MODE:
            return self._run_brownfield_create(request)
        assert request.spec_file is not None
        resolved_spec_path = Path(request.spec_file).expanduser().resolve()
```

Replace subsequent `request.spec_file` usages in the greenfield branch only after the assertion.

- [ ] **Step 6: Update application facade**

In `services/agent_workbench/application.py`, update `project_create` signature and request construction:

```python
    def project_create(
        self,
        *,
        name: str,
        spec_file: str | None = None,
        setup_mode: str = "greenfield",
        idempotency_key: str | None = None,
        dry_run: bool = False,
        dry_run_id: str | None = None,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        request = ProjectCreateRequest(
            name=name,
            spec_file=spec_file,
            setup_mode=setup_mode,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
            dry_run_id=dry_run_id,
            correlation_id=correlation_id,
            changed_by=changed_by,
        )
        return self._get_project_setup_runner().create_project(request)
```

- [ ] **Step 7: Update CLI parser**

In `cli/main.py`, change `project create` args:

```python
    project_create.add_argument("--setup-mode", choices=("greenfield", "brownfield"), default="greenfield")
    project_create.add_argument("--spec-file")
```

In `_project_create`, pass:

```python
        spec_file=args.spec_file,
        setup_mode=args.setup_mode,
```

- [ ] **Step 8: Add CLI integration test**

Add to `tests/test_agent_workbench_project_create_cli_integration.py`:

```python
def test_project_create_brownfield_cli_creates_shell(
    engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    app = AgentWorkbenchApplication(project_setup_runner=runner)

    exit_code = main(
        [
            "project",
            "create",
            "--setup-mode",
            "brownfield",
            "--name",
            "CLI Brownfield Shell",
            "--idempotency-key",
            "cli-brownfield-shell-001",
        ],
        application=app,
    )
    payload = _captured_payload(capsys)

    assert exit_code == 0
    assert payload["ok"] is True
    data = payload["data"]
    assert data["setup_status"] == "brownfield_curation_required"
    assert data["spec_hash"] is None
    assert data["next_actions"][0]["command"] == "agileforge brownfield source import"
```

- [ ] **Step 9: Verify shell create**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_project_setup.py::test_brownfield_project_create_rejects_spec_file tests/test_agent_workbench_project_setup.py::test_brownfield_project_create_writes_shell_without_spec_registry tests/test_agent_workbench_project_create_cli_integration.py::test_project_create_brownfield_cli_creates_shell -q
```

Expected: pass.

- [ ] **Step 10: Commit shell create**

Run:

```bash
git add services/agent_workbench/project_setup.py services/agent_workbench/application.py cli/main.py tests/test_agent_workbench_project_setup.py tests/test_agent_workbench_project_create_cli_integration.py
git commit -m "feat: add brownfield project shell"
```

## Task 3: Source Import And Repository Scan

**Files:**
- Create: `services/agent_workbench/brownfield_curation.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `cli/main.py`
- Create: `tests/test_agent_workbench_brownfield_curation.py`

- [ ] **Step 1: Write failing source/scan runner tests**

Create `tests/test_agent_workbench_brownfield_curation.py`:

```python
"""Tests for brownfield curation runner."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from models.brownfield import BrownfieldScanAttempt, BrownfieldSourceArtifact
from models.core import Product
from services.agent_workbench.brownfield_curation import BrownfieldCurationRunner


def _project(engine: Engine, name: str = "Brownfield App") -> int:
    with Session(engine) as session:
        product = Product(name=name)
        session.add(product)
        session.commit()
        session.refresh(product)
        assert product.product_id is not None
        return product.product_id


def test_source_import_records_non_authoritative_artifact(
    engine: Engine,
    tmp_path: Path,
) -> None:
    project_id = _project(engine)
    source = tmp_path / "notes.md"
    source.write_text("REQ: The system MUST reconcile invoices.\n", encoding="utf-8")
    runner = BrownfieldCurationRunner(engine=engine)

    result = runner.source_import(
        project_id=project_id,
        source_file=str(source),
        source_kind="notes",
        idempotency_key="source-import-001",
        changed_by="agent",
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["attempt_id"].startswith("source-")
    assert data["artifact_fingerprint"].startswith("sha256:")
    with Session(engine) as session:
        rows = session.exec(select(BrownfieldSourceArtifact)).all()
        assert len(rows) == 1
        assert rows[0].project_id == project_id
        assert rows[0].source_file_path == str(source.resolve())


def test_scan_records_repo_snapshot_with_source_chain(
    engine: Engine,
    tmp_path: Path,
) -> None:
    project_id = _project(engine)
    source = tmp_path / "notes.md"
    source.write_text("REQ: The system MUST reconcile invoices.\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def reconcile():\n    return True\n", encoding="utf-8")
    runner = BrownfieldCurationRunner(engine=engine)
    imported = runner.source_import(
        project_id=project_id,
        source_file=str(source),
        source_kind="notes",
        idempotency_key="source-import-002",
        changed_by="agent",
    )

    result = runner.scan(
        project_id=project_id,
        repo_path=str(repo),
        source_attempt_id=imported["data"]["attempt_id"],
        idempotency_key="scan-001",
        changed_by="agent",
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["attempt_id"].startswith("scan-")
    assert data["source_attempt_id"] == imported["data"]["attempt_id"]
    with Session(engine) as session:
        rows = session.exec(select(BrownfieldScanAttempt)).all()
        assert len(rows) == 1
        assert rows[0].repo_path == str(repo.resolve())
        assert rows[0].source_fingerprint == imported["data"]["artifact_fingerprint"]
```

- [ ] **Step 2: Run failing source/scan tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_brownfield_curation.py::test_source_import_records_non_authoritative_artifact tests/test_agent_workbench_brownfield_curation.py::test_scan_records_repo_snapshot_with_source_chain -q
```

Expected: import fails because `services.agent_workbench.brownfield_curation` does not exist.

- [ ] **Step 3: Add runner skeleton and shared helpers**

Create `services/agent_workbench/brownfield_curation.py`:

```python
"""Brownfield product-spec curation commands."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from models.brownfield import (
    BrownfieldScanAttempt,
    BrownfieldSourceArtifact,
)
from models.core import Product
from models.db import get_engine
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash
from services.agent_workbench.mutation_ledger import MutationLedgerRepository

GIT_BINARY = "git"
BROWNFIELD_COMMAND_VERSION = "brownfield-curation.v1"
NO_SOURCE_FINGERPRINT = "sha256:no-source"


def _now() -> datetime:
    return datetime.now(UTC)


def _success(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": data, "warnings": [], "errors": []}


def _error(code: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "data": None,
        "warnings": [],
        "errors": [workbench_error(code, details=details).to_dict()],
    }


def _file_sha256(file_path: Path) -> str:
    digest = canonical_hash({"bytes": file_path.read_bytes().hex()})
    return digest


def _preview_text(file_path: Path, limit: int = 1000) -> str:
    return file_path.read_text(encoding="utf-8", errors="replace")[:limit]


def _repo_metadata(repo_path: Path) -> dict[str, Any]:
    git_commit: str | None = None
    dirty = False
    commit = subprocess.run(
        [GIT_BINARY, "-C", str(repo_path), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if commit.returncode == 0:
        git_commit = commit.stdout.strip() or None
        status = subprocess.run(
            [GIT_BINARY, "-C", str(repo_path), "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
        )
        dirty = bool(status.stdout.strip()) if status.returncode == 0 else False
    return {"repo_commit": git_commit, "repo_dirty": dirty}


def _file_manifest(repo_path: Path) -> list[dict[str, str]]:
    manifest: list[dict[str, str]] = []
    for file_path in sorted(repo_path.rglob("*")):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(repo_path).as_posix()
        if relative.startswith((".git/", ".env")) or "/.git/" in relative:
            continue
        if file_path.stat().st_size > 200_000:
            continue
        manifest.append({"path": relative, "sha256": _file_sha256(file_path)})
    return manifest


class BrownfieldCurationRunner:
    """Run brownfield curation commands against durable artifact rows."""

    def __init__(self, *, engine: Engine | None = None) -> None:
        self._engine = engine or get_engine()
        self._ledger = MutationLedgerRepository(engine=self._engine)

    def _project_exists(self, project_id: int) -> bool:
        with Session(self._engine) as session:
            return session.get(Product, project_id) is not None
```

- [ ] **Step 4: Implement source import**

Add to `BrownfieldCurationRunner`:

```python
    def source_import(
        self,
        *,
        project_id: int,
        source_file: str,
        source_kind: str = "source_file",
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        if not self._project_exists(project_id):
            return _error(ErrorCode.PROJECT_NOT_FOUND.value, details={"project_id": project_id})
        resolved = Path(source_file).expanduser().resolve()
        if not resolved.exists() or not resolved.is_file():
            return _error(ErrorCode.SPEC_FILE_NOT_FOUND.value, details={"source_file": str(resolved)})
        source_sha256 = _file_sha256(resolved)
        request_hash = canonical_hash(
            {
                "command": "agileforge brownfield source import",
                "project_id": project_id,
                "source_file": str(resolved),
                "source_sha256": source_sha256,
                "source_kind": source_kind,
                "changed_by": changed_by,
            }
        )
        loaded = self._ledger.create_or_load(
            command="agileforge brownfield source import",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            project_id=project_id,
            correlation_id=correlation_id or idempotency_key,
            changed_by=changed_by,
            lease_owner=f"brownfield-source:{idempotency_key}",
            now=_now(),
            lease_seconds=300,
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code is not None:
            return _error(loaded.error_code, details={"idempotency_key": idempotency_key})
        mutation_event_id = loaded.ledger.mutation_event_id
        assert mutation_event_id is not None
        attempt_id = f"source-{mutation_event_id}"
        artifact_fingerprint = canonical_hash(
            {
                "project_id": project_id,
                "attempt_id": attempt_id,
                "source_sha256": source_sha256,
                "source_kind": source_kind,
            }
        )
        with Session(self._engine) as session:
            row = BrownfieldSourceArtifact(
                project_id=project_id,
                attempt_id=attempt_id,
                artifact_fingerprint=artifact_fingerprint,
                source_kind=source_kind,
                source_file_path=str(resolved),
                source_sha256=source_sha256,
                content_preview=_preview_text(resolved),
                request_hash=request_hash,
            )
            session.add(row)
            session.commit()
        data = {
            "project_id": project_id,
            "attempt_id": attempt_id,
            "artifact_fingerprint": artifact_fingerprint,
            "source_file": str(resolved),
            "status": "complete",
            "mutation_event_id": mutation_event_id,
        }
        response = _success(data)
        self._ledger.finalize_success(
            mutation_event_id=mutation_event_id,
            lease_owner=f"brownfield-source:{idempotency_key}",
            after=data,
            response=response,
            now=_now(),
        )
        return response
```

- [ ] **Step 5: Implement scan**

Add to `BrownfieldCurationRunner`:

```python
    def scan(
        self,
        *,
        project_id: int,
        repo_path: str,
        source_attempt_id: str | None = None,
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        if not self._project_exists(project_id):
            return _error(ErrorCode.PROJECT_NOT_FOUND.value, details={"project_id": project_id})
        resolved_repo = Path(repo_path).expanduser().resolve()
        if not resolved_repo.exists() or not resolved_repo.is_dir():
            return _error(ErrorCode.SPEC_FILE_NOT_FOUND.value, details={"repo_path": str(resolved_repo)})
        with Session(self._engine) as session:
            source = None
            if source_attempt_id is not None:
                source = session.exec(
                    select(BrownfieldSourceArtifact).where(
                        BrownfieldSourceArtifact.project_id == project_id,
                        BrownfieldSourceArtifact.attempt_id == source_attempt_id,
                        BrownfieldSourceArtifact.status == "complete",
                    )
                ).first()
                if source is None:
                    return _error("BROWNFIELD_SOURCE_NOT_FOUND", details={"source_attempt_id": source_attempt_id})
            source_fingerprint = (
                source.artifact_fingerprint if source is not None else NO_SOURCE_FINGERPRINT
            )
        metadata = _repo_metadata(resolved_repo)
        manifest = _file_manifest(resolved_repo)
        manifest_hash = canonical_hash({"files": manifest})
        request_hash = canonical_hash(
            {
                "command": "agileforge brownfield scan",
                "project_id": project_id,
                "repo_path": str(resolved_repo),
                "repo_commit": metadata["repo_commit"],
                "repo_dirty": metadata["repo_dirty"],
                "manifest_hash": manifest_hash,
                "source_attempt_id": source_attempt_id,
                "source_fingerprint": source_fingerprint,
                "changed_by": changed_by,
            }
        )
        loaded = self._ledger.create_or_load(
            command="agileforge brownfield scan",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            project_id=project_id,
            correlation_id=correlation_id or idempotency_key,
            changed_by=changed_by,
            lease_owner=f"brownfield-scan:{idempotency_key}",
            now=_now(),
            lease_seconds=300,
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code is not None:
            return _error(loaded.error_code, details={"idempotency_key": idempotency_key})
        mutation_event_id = loaded.ledger.mutation_event_id
        assert mutation_event_id is not None
        attempt_id = f"scan-{mutation_event_id}"
        artifact_fingerprint = canonical_hash(
            {
                "project_id": project_id,
                "attempt_id": attempt_id,
                "source_fingerprint": source_fingerprint,
                "repo": metadata,
                "manifest_hash": manifest_hash,
            }
        )
        facts = [{"kind": "file", "path": item["path"]} for item in manifest[:200]]
        with Session(self._engine) as session:
            row = BrownfieldScanAttempt(
                project_id=project_id,
                attempt_id=attempt_id,
                artifact_fingerprint=artifact_fingerprint,
                source_attempt_id=source_attempt_id,
                source_fingerprint=source_fingerprint,
                repo_path=str(resolved_repo),
                repo_commit=metadata["repo_commit"],
                repo_dirty=bool(metadata["repo_dirty"]),
                file_manifest_json=json.dumps(manifest, sort_keys=True),
                implementation_facts_json=json.dumps(facts, sort_keys=True),
                request_hash=request_hash,
            )
            session.add(row)
            session.commit()
        data = {
            "project_id": project_id,
            "attempt_id": attempt_id,
            "artifact_fingerprint": artifact_fingerprint,
            "source_attempt_id": source_attempt_id,
            "source_fingerprint": source_fingerprint,
            "repo_path": str(resolved_repo),
            "repo_commit": metadata["repo_commit"],
            "repo_dirty": metadata["repo_dirty"],
            "status": "complete",
            "mutation_event_id": mutation_event_id,
        }
        response = _success(data)
        self._ledger.finalize_success(
            mutation_event_id=mutation_event_id,
            lease_owner=f"brownfield-scan:{idempotency_key}",
            after=data,
            response=response,
            now=_now(),
        )
        return response
```

- [ ] **Step 6: Verify source/scan tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_brownfield_curation.py::test_source_import_records_non_authoritative_artifact tests/test_agent_workbench_brownfield_curation.py::test_scan_records_repo_snapshot_with_source_chain -q
```

Expected: pass.

- [ ] **Step 7: Commit source and scan**

Run:

```bash
git add services/agent_workbench/brownfield_curation.py tests/test_agent_workbench_brownfield_curation.py
git commit -m "feat: record brownfield sources and scans"
```

## Task 4: Draft And Human Import Attempts

**Files:**
- Modify: `services/agent_workbench/brownfield_curation.py`
- Modify: `tests/test_agent_workbench_brownfield_curation.py`

- [ ] **Step 1: Add failing draft/import tests**

Add to `tests/test_agent_workbench_brownfield_curation.py`:

```python
def test_spec_draft_from_typed_source_creates_reusable_candidate(
    engine: Engine,
    tmp_path: Path,
) -> None:
    project_id = _project(engine)
    source = tmp_path / "notes.md"
    source.write_text(
        "REQ: The system MUST reconcile invoices.\n"
        "DECISION: Operators approve imported invoices before posting.\n",
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "routes.py").write_text("POST /invoices/import\n", encoding="utf-8")
    runner = BrownfieldCurationRunner(engine=engine)
    imported = runner.source_import(
        project_id=project_id,
        source_file=str(source),
        source_kind="notes",
        idempotency_key="draft-source-001",
        changed_by="agent",
    )
    scan = runner.scan(
        project_id=project_id,
        repo_path=str(repo),
        source_attempt_id=imported["data"]["attempt_id"],
        idempotency_key="draft-scan-001",
        changed_by="agent",
    )

    result = runner.spec_draft(
        project_id=project_id,
        scan_attempt_id=scan["data"]["attempt_id"],
        user_input="prioritize operator-visible behavior",
        idempotency_key="draft-001",
        changed_by="agent",
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["attempt_id"].startswith("draft-")
    assert data["status"] == "complete"
    assert data["origin"] == "generated"
    assert data["spec_hash"].startswith("sha256:")
    assert data["artifact_fingerprint"].startswith("sha256:")


def test_spec_import_records_human_imported_candidate(
    engine: Engine,
    tmp_path: Path,
) -> None:
    project_id = _project(engine, name="Human Import Product")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    runner = BrownfieldCurationRunner(engine=engine)
    scan = runner.scan(
        project_id=project_id,
        repo_path=str(repo),
        source_attempt_id=None,
        idempotency_key="import-scan-001",
        changed_by="agent",
    )
    curated = tmp_path / "curated.json"
    curated.write_text(
        json.dumps(_structured_spec_payload(title="Human Curated Spec")),
        encoding="utf-8",
    )

    result = runner.spec_import(
        project_id=project_id,
        curated_spec_file=str(curated),
        expected_scan_fingerprint=scan["data"]["artifact_fingerprint"],
        parent_draft_attempt_id=None,
        idempotency_key="import-001",
        changed_by="agent",
    )

    assert result["ok"] is True
    assert result["data"]["origin"] == "human_import"
    assert result["data"]["status"] == "complete"
```

Also add this fixture helper near `_project`:

```python
def _structured_spec_payload(*, title: str = "Curated Spec") -> dict[str, object]:
    return {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": "SPEC.curated",
        "title": title,
        "status": "draft",
        "version": "0.1",
        "created_at": "2026-06-15",
        "updated_at": "2026-06-15",
        "summary": "Curated brownfield product specification.",
        "problem_statement": "Operators need a reviewed product spec before authority compilation.",
        "items": [
            {
                "id": "REQ.curated.001",
                "type": "REQ",
                "status": "proposed",
                "level": "MUST",
                "title": "Reviewed curated spec",
                "statement": "The system MUST compile authority only from reviewed curated specs.",
                "verification": "system-test",
                "acceptance": ["Authority compile uses the managed approved spec path."],
            }
        ],
        "relations": [],
        "controlled_terms": [],
        "external_references": [],
        "rendering": {
            "markdown_profile": "agileforge.spec_markdown.v1",
            "rendered_markdown_sha256": None,
        },
    }
```

- [ ] **Step 2: Run failing draft/import tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_brownfield_curation.py::test_spec_draft_from_typed_source_creates_reusable_candidate tests/test_agent_workbench_brownfield_curation.py::test_spec_import_records_human_imported_candidate -q
```

Expected: fails because `spec_draft` and `spec_import` do not exist.

- [ ] **Step 3: Add deterministic draft helpers**

In `services/agent_workbench/brownfield_curation.py`, add imports:

```python
from models.brownfield import BrownfieldSpecDraftAttempt
from services.specs.profile_content import normalize_spec_content_for_registry
```

Add constants and helpers:

```python
PRODUCT_ITEM_TYPES = {
    "REQ",
    "QUALITY",
    "CONSTRAINT",
    "INTERFACE",
    "DATA",
    "DECISION",
    "NON_GOAL",
    "RISK",
    "OPEN_QUESTION",
}
IMPLEMENTATION_TERMS = {
    "route",
    "endpoint",
    "table",
    "column",
    "model",
    "serializer",
    "controller",
    "worker",
    "queue",
    "framework",
}


def _typed_item(line: str, index: int) -> dict[str, Any] | None:
    prefix, separator, body = line.partition(":")
    item_type = prefix.strip().upper()
    statement = body.strip()
    if separator != ":" or item_type not in PRODUCT_ITEM_TYPES or not statement:
        return None
    item_id = f"{item_type}.brownfield.{index:03d}"
    return {
        "id": item_id,
        "type": item_type,
        "status": "proposed",
        "level": "MUST" if item_type in {"REQ", "QUALITY", "CONSTRAINT", "INTERFACE", "DATA"} else None,
        "title": statement[:80],
        "statement": statement,
        "verification": "review",
        "acceptance": [statement],
    }


def _candidate_spec_from_source(
    *,
    project_id: int,
    source_text: str,
    user_input: str | None,
) -> tuple[dict[str, Any], str, list[str]]:
    warnings: list[str] = []
    items = [
        item
        for index, line in enumerate(source_text.splitlines(), start=1)
        if (item := _typed_item(line, index)) is not None
    ]
    implementation_hits = sum(
        source_text.lower().count(term) for term in IMPLEMENTATION_TERMS
    )
    if implementation_hits >= max(3, len(items) * 2):
        warnings.append("BROWNFIELD_SPEC_IMPLEMENTATION_HEAVY")
    if not items:
        items = [
            {
                "id": "OPEN_QUESTION.brownfield.001",
                "type": "OPEN_QUESTION",
                "status": "proposed",
                "title": "Curated product requirements needed",
                "statement": "Human review must provide product-level requirements before approval.",
                "verification": "review",
                "acceptance": ["A human imports a curated agileforge.spec.v1 file."],
            }
        ]
    spec = {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": f"SPEC.brownfield.{project_id}",
        "title": f"Brownfield Curated Spec {project_id}",
        "status": "draft",
        "version": "0.1",
        "created_at": "2026-06-15",
        "updated_at": "2026-06-15",
        "summary": user_input or "Curated brownfield product specification.",
        "problem_statement": "Brownfield setup needs reviewed product requirements before authority compilation.",
        "items": items,
        "relations": [],
        "controlled_terms": [],
        "external_references": [],
        "rendering": {
            "markdown_profile": "agileforge.spec_markdown.v1",
            "rendered_markdown_sha256": None,
        },
    }
    status = "complete" if any(item["type"] != "OPEN_QUESTION" for item in items) else "incomplete"
    return spec, status, warnings
```

If Ruff reports line length or complexity, split the long dictionary expressions without changing behavior.

- [ ] **Step 4: Implement `spec_draft`**

Add method to `BrownfieldCurationRunner`:

```python
    def spec_draft(
        self,
        *,
        project_id: int,
        scan_attempt_id: str,
        user_input: str | None = None,
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        if not self._project_exists(project_id):
            return _error(ErrorCode.PROJECT_NOT_FOUND.value, details={"project_id": project_id})
        with Session(self._engine) as session:
            scan = session.exec(
                select(BrownfieldScanAttempt).where(
                    BrownfieldScanAttempt.project_id == project_id,
                    BrownfieldScanAttempt.attempt_id == scan_attempt_id,
                    BrownfieldScanAttempt.status == "complete",
                )
            ).first()
            if scan is None:
                return _error("BROWNFIELD_SCAN_NOT_FOUND", details={"scan_attempt_id": scan_attempt_id})
            source_text = ""
            if scan.source_attempt_id:
                source = session.exec(
                    select(BrownfieldSourceArtifact).where(
                        BrownfieldSourceArtifact.project_id == project_id,
                        BrownfieldSourceArtifact.attempt_id == scan.source_attempt_id,
                    )
                ).first()
                source_text = source.content_preview if source is not None and source.content_preview else ""
        spec, draft_status, warnings = _candidate_spec_from_source(
            project_id=project_id,
            source_text=source_text,
            user_input=user_input,
        )
        normalized = normalize_spec_content_for_registry(json.dumps(spec, sort_keys=True))
        request_hash = canonical_hash(
            {
                "command": "agileforge brownfield spec draft",
                "project_id": project_id,
                "scan_attempt_id": scan_attempt_id,
                "scan_fingerprint": scan.artifact_fingerprint,
                "source_fingerprint": scan.source_fingerprint,
                "user_input": user_input,
                "changed_by": changed_by,
                "drafter_version": BROWNFIELD_COMMAND_VERSION,
            }
        )
        loaded = self._ledger.create_or_load(
            command="agileforge brownfield spec draft",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            project_id=project_id,
            correlation_id=correlation_id or idempotency_key,
            changed_by=changed_by,
            lease_owner=f"brownfield-draft:{idempotency_key}",
            now=_now(),
            lease_seconds=300,
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code is not None:
            return _error(loaded.error_code, details={"idempotency_key": idempotency_key})
        mutation_event_id = loaded.ledger.mutation_event_id
        assert mutation_event_id is not None
        attempt_id = f"draft-{mutation_event_id}"
        artifact_fingerprint = canonical_hash(
            {
                "attempt_id": attempt_id,
                "scan_fingerprint": scan.artifact_fingerprint,
                "source_fingerprint": scan.source_fingerprint,
                "spec_hash": normalized.spec_hash,
                "status": draft_status,
            }
        )
        with Session(self._engine) as session:
            row = BrownfieldSpecDraftAttempt(
                project_id=project_id,
                attempt_id=attempt_id,
                artifact_fingerprint=artifact_fingerprint,
                origin="generated",
                status=draft_status,
                source_fingerprint=scan.source_fingerprint,
                scan_attempt_id=scan_attempt_id,
                scan_fingerprint=scan.artifact_fingerprint,
                spec_hash=normalized.spec_hash,
                curated_spec_json=normalized.content,
                request_hash=request_hash,
                user_input_hash=canonical_hash({"user_input": user_input}),
                warning_metadata_json=json.dumps(warnings, sort_keys=True),
            )
            session.add(row)
            session.commit()
        data = {
            "project_id": project_id,
            "attempt_id": attempt_id,
            "artifact_fingerprint": artifact_fingerprint,
            "origin": "generated",
            "status": draft_status,
            "scan_fingerprint": scan.artifact_fingerprint,
            "source_fingerprint": scan.source_fingerprint,
            "spec_hash": normalized.spec_hash,
            "warnings": warnings,
            "mutation_event_id": mutation_event_id,
        }
        response = _success(data)
        response["warnings"] = [{"code": code} for code in warnings]
        self._ledger.finalize_success(
            mutation_event_id=mutation_event_id,
            lease_owner=f"brownfield-draft:{idempotency_key}",
            after=data,
            response=response,
            now=_now(),
        )
        return response
```

- [ ] **Step 5: Implement `spec_import`**

Add method to `BrownfieldCurationRunner`:

```python
    def spec_import(
        self,
        *,
        project_id: int,
        curated_spec_file: str,
        expected_scan_fingerprint: str,
        parent_draft_attempt_id: str | None = None,
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        if not self._project_exists(project_id):
            return _error(ErrorCode.PROJECT_NOT_FOUND.value, details={"project_id": project_id})
        resolved = Path(curated_spec_file).expanduser().resolve()
        if not resolved.exists() or not resolved.is_file():
            return _error(ErrorCode.SPEC_FILE_NOT_FOUND.value, details={"curated_spec_file": str(resolved)})
        normalized = normalize_spec_content_for_registry(resolved.read_text(encoding="utf-8"))
        with Session(self._engine) as session:
            scan = session.exec(
                select(BrownfieldScanAttempt).where(
                    BrownfieldScanAttempt.project_id == project_id,
                    BrownfieldScanAttempt.artifact_fingerprint == expected_scan_fingerprint,
                    BrownfieldScanAttempt.status == "complete",
                )
            ).first()
            if scan is None:
                return _error("BROWNFIELD_APPROVAL_CHAIN_MISMATCH", details={"expected_scan_fingerprint": expected_scan_fingerprint})
            if parent_draft_attempt_id is not None:
                parent = session.exec(
                    select(BrownfieldSpecDraftAttempt).where(
                        BrownfieldSpecDraftAttempt.project_id == project_id,
                        BrownfieldSpecDraftAttempt.attempt_id == parent_draft_attempt_id,
                    )
                ).first()
                if parent is None:
                    return _error("BROWNFIELD_DRAFT_NOT_FOUND", details={"parent_draft_attempt_id": parent_draft_attempt_id})
        request_hash = canonical_hash(
            {
                "command": "agileforge brownfield spec import",
                "project_id": project_id,
                "curated_spec_file": str(resolved),
                "spec_hash": normalized.spec_hash,
                "expected_scan_fingerprint": expected_scan_fingerprint,
                "parent_draft_attempt_id": parent_draft_attempt_id,
                "changed_by": changed_by,
            }
        )
        loaded = self._ledger.create_or_load(
            command="agileforge brownfield spec import",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            project_id=project_id,
            correlation_id=correlation_id or idempotency_key,
            changed_by=changed_by,
            lease_owner=f"brownfield-import:{idempotency_key}",
            now=_now(),
            lease_seconds=300,
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code is not None:
            return _error(loaded.error_code, details={"idempotency_key": idempotency_key})
        mutation_event_id = loaded.ledger.mutation_event_id
        assert mutation_event_id is not None
        attempt_id = f"draft-import-{mutation_event_id}"
        artifact_fingerprint = canonical_hash(
            {
                "attempt_id": attempt_id,
                "scan_fingerprint": expected_scan_fingerprint,
                "source_fingerprint": scan.source_fingerprint,
                "spec_hash": normalized.spec_hash,
                "origin": "human_import",
            }
        )
        with Session(self._engine) as session:
            row = BrownfieldSpecDraftAttempt(
                project_id=project_id,
                attempt_id=attempt_id,
                artifact_fingerprint=artifact_fingerprint,
                origin="human_import",
                status="complete",
                source_fingerprint=scan.source_fingerprint,
                scan_attempt_id=scan.attempt_id,
                scan_fingerprint=scan.artifact_fingerprint,
                parent_draft_attempt_id=parent_draft_attempt_id,
                spec_hash=normalized.spec_hash,
                curated_spec_json=normalized.content,
                imported_file_path=str(resolved),
                request_hash=request_hash,
            )
            session.add(row)
            session.commit()
        data = {
            "project_id": project_id,
            "attempt_id": attempt_id,
            "artifact_fingerprint": artifact_fingerprint,
            "origin": "human_import",
            "status": "complete",
            "scan_fingerprint": scan.artifact_fingerprint,
            "source_fingerprint": scan.source_fingerprint,
            "spec_hash": normalized.spec_hash,
            "mutation_event_id": mutation_event_id,
        }
        response = _success(data)
        self._ledger.finalize_success(
            mutation_event_id=mutation_event_id,
            lease_owner=f"brownfield-import:{idempotency_key}",
            after=data,
            response=response,
            now=_now(),
        )
        return response
```

- [ ] **Step 6: Verify draft/import tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_brownfield_curation.py::test_spec_draft_from_typed_source_creates_reusable_candidate tests/test_agent_workbench_brownfield_curation.py::test_spec_import_records_human_imported_candidate -q
```

Expected: pass.

- [ ] **Step 7: Commit draft/import**

Run:

```bash
git add services/agent_workbench/brownfield_curation.py tests/test_agent_workbench_brownfield_curation.py
git commit -m "feat: draft and import brownfield specs"
```

## Task 5: Approval Bridge And Recovery

**Files:**
- Modify: `services/agent_workbench/brownfield_curation.py`
- Modify: `tests/test_agent_workbench_brownfield_curation.py`

- [ ] **Step 1: Add failing approval test**

Add to `tests/test_agent_workbench_brownfield_curation.py`:

```python
def test_spec_approve_registers_managed_spec_and_workflow_state(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "config-root"
    config_root.mkdir()
    monkeypatch.setenv("AGILEFORGE_CONFIG_ROOT", str(config_root))
    project_id = _project(engine, name="Approval Product")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    curated = tmp_path / "curated.json"
    curated.write_text(json.dumps(_structured_spec_payload()), encoding="utf-8")
    workflow = FakeBrownfieldWorkflow()
    workflow.sessions[str(project_id)] = {
        "fsm_state": "SETUP_REQUIRED",
        "setup_mode": "brownfield",
        "setup_status": "brownfield_curation_required",
    }
    runner = BrownfieldCurationRunner(engine=engine, workflow=workflow)
    scan = runner.scan(
        project_id=project_id,
        repo_path=str(repo),
        source_attempt_id=None,
        idempotency_key="approve-scan-001",
        changed_by="agent",
    )
    imported = runner.spec_import(
        project_id=project_id,
        curated_spec_file=str(curated),
        expected_scan_fingerprint=scan["data"]["artifact_fingerprint"],
        parent_draft_attempt_id=None,
        idempotency_key="approve-import-001",
        changed_by="agent",
    )

    result = runner.spec_approve(
        project_id=project_id,
        attempt_id=imported["data"]["attempt_id"],
        expected_artifact_fingerprint=imported["data"]["artifact_fingerprint"],
        expected_state="SETUP_REQUIRED",
        expected_setup_status="brownfield_curation_required",
        idempotency_key="approve-001",
        changed_by="agent",
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["setup_status"] == "authority_compile_required"
    assert data["spec_version_id"] is not None
    managed_path = Path(data["setup_spec_file_path"])
    assert managed_path.exists()
    assert managed_path.is_relative_to(config_root / "artifacts" / "brownfield")
    assert workflow.sessions[str(project_id)]["setup_spec_file_path"] == str(managed_path)
    with Session(engine) as session:
        spec = session.get(SpecRegistry, data["spec_version_id"])
        assert spec is not None
        assert spec.content_ref == str(managed_path)
        approvals = session.exec(select(BrownfieldSpecApproval)).all()
        assert len(approvals) == 1
        assert approvals[0].spec_version_id == data["spec_version_id"]
```

Add fake workflow in the same test file:

```python
class FakeBrownfieldWorkflow:
    """In-memory workflow port for brownfield approval tests."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, object]] = {}

    def get_session_status(self, session_id: str) -> dict[str, object]:
        return dict(self.sessions.get(session_id, {}))

    def update_session_status(self, session_id: str, partial_update: dict[str, object]) -> None:
        current = self.sessions.setdefault(session_id, {})
        current.update(partial_update)
```

Add imports:

```python
from models.brownfield import BrownfieldSpecApproval
from models.specs import SpecRegistry
```

- [ ] **Step 2: Run failing approval test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_brownfield_curation.py::test_spec_approve_registers_managed_spec_and_workflow_state -q
```

Expected: fails because `spec_approve` and workflow support do not exist.

- [ ] **Step 3: Add workflow port and managed path helper**

In `services/agent_workbench/brownfield_curation.py`, add imports:

```python
from models.brownfield import BrownfieldSpecApproval, BrownfieldSpecDraftAttempt
from services.specs.pending_authority_service import ensure_pending_spec_version_for_project
from utils.runtime_config import get_config_root
```

Add protocol and default adapter:

```python
class BrownfieldWorkflowPort:
    """Workflow state operations used by brownfield approval."""

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def update_session_status(self, session_id: str, partial_update: dict[str, Any]) -> None:
        raise NotImplementedError


class SyncBrownfieldWorkflowAdapter(BrownfieldWorkflowPort):
    """Synchronous adapter over WorkflowService."""

    def __init__(self) -> None:
        from services.workflow import WorkflowService

        self._workflow = WorkflowService()

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        return self._workflow.get_session_status(session_id)

    def update_session_status(self, session_id: str, partial_update: dict[str, Any]) -> None:
        self._workflow.update_session_status(session_id, partial_update)
```

Update runner constructor:

```python
    def __init__(
        self,
        *,
        engine: Engine | None = None,
        workflow: BrownfieldWorkflowPort | None = None,
    ) -> None:
        self._engine = engine or get_engine()
        self._ledger = MutationLedgerRepository(engine=self._engine)
        self._workflow = workflow or SyncBrownfieldWorkflowAdapter()
```

Add managed path helper:

```python
def _managed_approved_spec_path(*, project_id: int, approval_attempt_id: str) -> Path:
    return (
        get_config_root()
        / "artifacts"
        / "brownfield"
        / str(project_id)
        / "approvals"
        / approval_attempt_id
        / "spec.json"
    )
```

- [ ] **Step 4: Implement approval validation and side effects**

Add to `BrownfieldCurationRunner`:

```python
    def spec_approve(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        expected_setup_status: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        if not self._project_exists(project_id):
            return _error(ErrorCode.PROJECT_NOT_FOUND.value, details={"project_id": project_id})
        with Session(self._engine) as session:
            draft = session.exec(
                select(BrownfieldSpecDraftAttempt).where(
                    BrownfieldSpecDraftAttempt.project_id == project_id,
                    BrownfieldSpecDraftAttempt.attempt_id == attempt_id,
                )
            ).first()
            if draft is None:
                return _error("BROWNFIELD_DRAFT_NOT_FOUND", details={"attempt_id": attempt_id})
            if draft.status != "complete" or not draft.curated_spec_json or not draft.spec_hash:
                return _error("BROWNFIELD_DRAFT_INCOMPLETE", details={"attempt_id": attempt_id})
            if draft.artifact_fingerprint != expected_artifact_fingerprint:
                return _error("BROWNFIELD_DRAFT_STALE", details={"attempt_id": attempt_id})
            current_scan = session.exec(
                select(BrownfieldScanAttempt)
                .where(
                    BrownfieldScanAttempt.project_id == project_id,
                    BrownfieldScanAttempt.status == "complete",
                )
                .order_by(BrownfieldScanAttempt.created_at.desc())
            ).first()
            if current_scan is None or current_scan.artifact_fingerprint != draft.scan_fingerprint:
                return _error("BROWNFIELD_APPROVAL_CHAIN_MISMATCH", details={"attempt_id": attempt_id})
        request_hash = canonical_hash(
            {
                "command": "agileforge brownfield spec approve",
                "project_id": project_id,
                "attempt_id": attempt_id,
                "expected_artifact_fingerprint": expected_artifact_fingerprint,
                "expected_state": expected_state,
                "expected_setup_status": expected_setup_status,
                "spec_hash": draft.spec_hash,
                "scan_fingerprint": draft.scan_fingerprint,
                "source_fingerprint": draft.source_fingerprint,
                "changed_by": changed_by,
            }
        )
        loaded = self._ledger.create_or_load(
            command="agileforge brownfield spec approve",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            project_id=project_id,
            correlation_id=correlation_id or idempotency_key,
            changed_by=changed_by,
            lease_owner=f"brownfield-approve:{idempotency_key}",
            now=_now(),
            lease_seconds=300,
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code is not None:
            return _error(loaded.error_code, details={"idempotency_key": idempotency_key})
        workflow_state = self._workflow.get_session_status(str(project_id))
        if workflow_state.get("fsm_state") != expected_state:
            return _error(ErrorCode.STALE_STATE.value, details={"expected_state": expected_state})
        if workflow_state.get("setup_status") != expected_setup_status:
            return _error(ErrorCode.STALE_SETUP_STATUS.value, details={"expected_setup_status": expected_setup_status})
        if expected_setup_status != "brownfield_curation_required":
            return _error("BROWNFIELD_APPROVAL_STALE_GUARD", details={"expected_setup_status": expected_setup_status})
        with Session(self._engine) as session:
            existing_approval = session.exec(
                select(BrownfieldSpecApproval).where(
                    BrownfieldSpecApproval.project_id == project_id,
                    BrownfieldSpecApproval.draft_attempt_id == attempt_id,
                    BrownfieldSpecApproval.status == "complete",
                )
            ).first()
            if existing_approval is not None:
                return _error("BROWNFIELD_CURATED_SPEC_ALREADY_REGISTERED", details={"attempt_id": attempt_id})
        mutation_event_id = loaded.ledger.mutation_event_id
        assert mutation_event_id is not None
        approval_attempt_id = f"approval-{mutation_event_id}"
        approval_fingerprint = canonical_hash(
            {
                "project_id": project_id,
                "approval_attempt_id": approval_attempt_id,
                "draft_fingerprint": draft.artifact_fingerprint,
                "scan_fingerprint": draft.scan_fingerprint,
                "source_fingerprint": draft.source_fingerprint,
                "spec_hash": draft.spec_hash,
            }
        )
        managed_path = _managed_approved_spec_path(
            project_id=project_id,
            approval_attempt_id=approval_attempt_id,
        )
        managed_path.parent.mkdir(parents=True, exist_ok=True)
        managed_path.write_text(draft.curated_spec_json, encoding="utf-8")
        with Session(self._engine) as session:
            approval = BrownfieldSpecApproval(
                project_id=project_id,
                approval_attempt_id=approval_attempt_id,
                approval_fingerprint=approval_fingerprint,
                draft_attempt_id=attempt_id,
                draft_fingerprint=draft.artifact_fingerprint,
                scan_fingerprint=draft.scan_fingerprint,
                source_fingerprint=draft.source_fingerprint,
                spec_hash=draft.spec_hash,
                managed_spec_file_path=str(managed_path),
                mutation_event_id=mutation_event_id,
                status="started",
            )
            session.add(approval)
            session.commit()
            result = ensure_pending_spec_version_for_project(
                session=session,
                product_id=project_id,
                spec_path=managed_path,
                approved_by="brownfield-spec-approve",
                lease_guard=lambda _boundary: self._ledger.require_active_owner(
                    mutation_event_id=mutation_event_id,
                    lease_owner=f"brownfield-approve:{idempotency_key}",
                    now=_now(),
                    lease_seconds=300,
                ),
                record_progress=lambda boundary: self._ledger.mark_step_complete(
                    mutation_event_id=mutation_event_id,
                    lease_owner=f"brownfield-approve:{idempotency_key}",
                    step=boundary,
                    next_step=boundary,
                    now=_now(),
                ),
            )
            if not result.ok or result.spec_version_id is None:
                approval.status = "recovery_required"
                session.add(approval)
                session.commit()
                return _error(result.error_code or ErrorCode.MUTATION_RECOVERY_REQUIRED.value)
            approval.spec_version_id = result.spec_version_id
            approval.status = "spec_registered"
            approval.updated_at = _now()
            session.add(approval)
            session.commit()
        required_state = {
            "fsm_state": "SETUP_REQUIRED",
            "setup_mode": "brownfield",
            "setup_status": "authority_compile_required",
            "setup_error": None,
            "setup_spec_file_path": str(managed_path),
            "setup_spec_hash": draft.spec_hash,
            "setup_spec_version_id": int(result.spec_version_id),
            "setup_next_actions": [
                {
                    "command": "agileforge authority compile",
                    "args": {
                        "project_id": project_id,
                        "spec_version_id": int(result.spec_version_id),
                        "expected_spec_hash": draft.spec_hash,
                        "expected_state": "SETUP_REQUIRED",
                        "expected_setup_status": "authority_compile_required",
                    },
                    "reason": "Compile approved brownfield spec before authority review.",
                }
            ],
        }
        self._workflow.update_session_status(str(project_id), required_state)
        with Session(self._engine) as session:
            approval = session.exec(
                select(BrownfieldSpecApproval).where(
                    BrownfieldSpecApproval.approval_fingerprint == approval_fingerprint
                )
            ).one()
            approval.status = "complete"
            approval.updated_at = _now()
            session.add(approval)
            session.commit()
        data = {
            "project_id": project_id,
            "approval_attempt_id": approval_attempt_id,
            "approval_fingerprint": approval_fingerprint,
            "setup_status": "authority_compile_required",
            "setup_spec_file_path": str(managed_path),
            "spec_hash": draft.spec_hash,
            "spec_version_id": int(result.spec_version_id),
            "mutation_event_id": mutation_event_id,
            "next_actions": required_state["setup_next_actions"],
        }
        response = _success(data)
        self._ledger.finalize_success(
            mutation_event_id=mutation_event_id,
            lease_owner=f"brownfield-approve:{idempotency_key}",
            after=data,
            response=response,
            now=_now(),
        )
        return response
```

- [ ] **Step 5: Add duplicate approval recovery test**

Add:

```python
def test_spec_approve_replay_does_not_duplicate_spec_registry(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "config-root"
    config_root.mkdir()
    monkeypatch.setenv("AGILEFORGE_CONFIG_ROOT", str(config_root))
    project_id = _project(engine, name="Replay Approval Product")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    curated = tmp_path / "curated.json"
    curated.write_text(json.dumps(_structured_spec_payload()), encoding="utf-8")
    workflow = FakeBrownfieldWorkflow()
    workflow.sessions[str(project_id)] = {
        "fsm_state": "SETUP_REQUIRED",
        "setup_mode": "brownfield",
        "setup_status": "brownfield_curation_required",
    }
    runner = BrownfieldCurationRunner(engine=engine, workflow=workflow)
    scan = runner.scan(
        project_id=project_id,
        repo_path=str(repo),
        source_attempt_id=None,
        idempotency_key="replay-scan-001",
        changed_by="agent",
    )
    imported = runner.spec_import(
        project_id=project_id,
        curated_spec_file=str(curated),
        expected_scan_fingerprint=scan["data"]["artifact_fingerprint"],
        parent_draft_attempt_id=None,
        idempotency_key="replay-import-001",
        changed_by="agent",
    )
    args = {
        "project_id": project_id,
        "attempt_id": imported["data"]["attempt_id"],
        "expected_artifact_fingerprint": imported["data"]["artifact_fingerprint"],
        "expected_state": "SETUP_REQUIRED",
        "expected_setup_status": "brownfield_curation_required",
        "idempotency_key": "replay-approve-001",
        "changed_by": "agent",
    }

    first = runner.spec_approve(**args)
    workflow.sessions[str(project_id)]["setup_status"] = "brownfield_curation_required"
    replay = runner.spec_approve(**args)

    assert replay["ok"] is True
    assert replay["data"]["spec_version_id"] == first["data"]["spec_version_id"]
    with Session(engine) as session:
        assert len(session.exec(select(SpecRegistry)).all()) == 1
```

- [ ] **Step 6: Verify approval tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_brownfield_curation.py::test_spec_approve_registers_managed_spec_and_workflow_state tests/test_agent_workbench_brownfield_curation.py::test_spec_approve_replay_does_not_duplicate_spec_registry -q
```

Expected: pass. If replay returns the saved ledger response before checking current workflow state, that is acceptable because same-key replay is idempotent.

- [ ] **Step 7: Commit approval bridge**

Run:

```bash
git add services/agent_workbench/brownfield_curation.py tests/test_agent_workbench_brownfield_curation.py
git commit -m "feat: approve brownfield curated specs"
```

## Task 6: CLI, Application Facade, And Command Schema

**Files:**
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `services/agent_workbench/error_codes.py`
- Modify: `cli/main.py`
- Modify: `tests/test_agent_workbench_command_schema.py`
- Modify: `tests/test_agent_workbench_error_codes.py`
- Modify: `tests/test_agent_workbench_project_create_cli_integration.py`

- [ ] **Step 1: Add failing registry tests**

In `tests/test_agent_workbench_command_schema.py`, add expected command names:

```python
EXPECTED_BROWNFIELD_COMMAND_NAMES = {
    "agileforge brownfield source import",
    "agileforge brownfield scan",
    "agileforge brownfield spec draft",
    "agileforge brownfield spec import",
    "agileforge brownfield spec approve",
}
```

Include it in `expected_command_names`:

```python
        | EXPECTED_BROWNFIELD_COMMAND_NAMES
```

Add:

```python
def test_brownfield_command_contracts_are_guarded() -> None:
    approve = command_schema_payload("agileforge brownfield spec approve")

    assert approve["mutates"] is True
    assert approve["idempotency_required"] is True
    assert approve["input"]["required"] == [
        "project_id",
        "attempt_id",
        "expected_artifact_fingerprint",
        "expected_state",
        "expected_setup_status",
    ]
    assert "BROWNFIELD_CURATED_SPEC_ALREADY_REGISTERED" in approve["errors"]
```

In `tests/test_agent_workbench_error_codes.py`, extend `test_registry_covers_representative_phase_2a_error_codes` with:

```python
        "BROWNFIELD_DRAFT_NOT_FOUND",
        "BROWNFIELD_APPROVAL_CHAIN_MISMATCH",
        "BROWNFIELD_CURATED_SPEC_ALREADY_REGISTERED",
```

- [ ] **Step 2: Run failing schema tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_command_schema.py::test_command_schema_payloads_are_available tests/test_agent_workbench_command_schema.py::test_brownfield_command_contracts_are_guarded tests/test_agent_workbench_error_codes.py::test_registry_covers_representative_phase_2a_error_codes -q
```

Expected: fails because commands and errors are not registered.

- [ ] **Step 3: Register error codes**

In `services/agent_workbench/error_codes.py`, add enum members:

```python
    BROWNFIELD_SOURCE_NOT_FOUND = "BROWNFIELD_SOURCE_NOT_FOUND"
    BROWNFIELD_SCAN_NOT_FOUND = "BROWNFIELD_SCAN_NOT_FOUND"
    BROWNFIELD_DRAFT_NOT_FOUND = "BROWNFIELD_DRAFT_NOT_FOUND"
    BROWNFIELD_DRAFT_STALE = "BROWNFIELD_DRAFT_STALE"
    BROWNFIELD_DRAFT_INCOMPLETE = "BROWNFIELD_DRAFT_INCOMPLETE"
    BROWNFIELD_SOURCE_SUPERSEDED = "BROWNFIELD_SOURCE_SUPERSEDED"
    BROWNFIELD_APPROVAL_CHAIN_MISMATCH = "BROWNFIELD_APPROVAL_CHAIN_MISMATCH"
    BROWNFIELD_CURATED_SPEC_ALREADY_REGISTERED = (
        "BROWNFIELD_CURATED_SPEC_ALREADY_REGISTERED"
    )
    BROWNFIELD_APPROVAL_STALE_GUARD = "BROWNFIELD_APPROVAL_STALE_GUARD"
```

Add registry entries with exit codes:

```python
    ErrorCode.BROWNFIELD_SOURCE_NOT_FOUND: ErrorMetadata(
        code=ErrorCode.BROWNFIELD_SOURCE_NOT_FOUND.value,
        default_exit_code=4,
        retryable=False,
        description="Brownfield source attempt was not found.",
    ),
    ErrorCode.BROWNFIELD_SCAN_NOT_FOUND: ErrorMetadata(
        code=ErrorCode.BROWNFIELD_SCAN_NOT_FOUND.value,
        default_exit_code=4,
        retryable=False,
        description="Brownfield scan attempt was not found.",
    ),
    ErrorCode.BROWNFIELD_DRAFT_NOT_FOUND: ErrorMetadata(
        code=ErrorCode.BROWNFIELD_DRAFT_NOT_FOUND.value,
        default_exit_code=4,
        retryable=False,
        description="Brownfield draft attempt was not found.",
    ),
    ErrorCode.BROWNFIELD_DRAFT_STALE: ErrorMetadata(
        code=ErrorCode.BROWNFIELD_DRAFT_STALE.value,
        default_exit_code=3,
        retryable=True,
        description="Brownfield draft fingerprint or freshness guard is stale.",
    ),
    ErrorCode.BROWNFIELD_DRAFT_INCOMPLETE: ErrorMetadata(
        code=ErrorCode.BROWNFIELD_DRAFT_INCOMPLETE.value,
        default_exit_code=4,
        retryable=False,
        description="Brownfield draft is incomplete or not reusable.",
    ),
    ErrorCode.BROWNFIELD_SOURCE_SUPERSEDED: ErrorMetadata(
        code=ErrorCode.BROWNFIELD_SOURCE_SUPERSEDED.value,
        default_exit_code=3,
        retryable=True,
        description="A newer brownfield source or scan superseded the draft chain.",
    ),
    ErrorCode.BROWNFIELD_APPROVAL_CHAIN_MISMATCH: ErrorMetadata(
        code=ErrorCode.BROWNFIELD_APPROVAL_CHAIN_MISMATCH.value,
        default_exit_code=3,
        retryable=True,
        description="Brownfield approval chain does not match current source and scan.",
    ),
    ErrorCode.BROWNFIELD_CURATED_SPEC_ALREADY_REGISTERED: ErrorMetadata(
        code=ErrorCode.BROWNFIELD_CURATED_SPEC_ALREADY_REGISTERED.value,
        default_exit_code=10,
        retryable=False,
        description="Curated brownfield spec is already registered.",
    ),
    ErrorCode.BROWNFIELD_APPROVAL_STALE_GUARD: ErrorMetadata(
        code=ErrorCode.BROWNFIELD_APPROVAL_STALE_GUARD.value,
        default_exit_code=3,
        retryable=True,
        description="Brownfield approval stale guards do not match workflow state.",
    ),
```

Replace string literal brownfield codes in `brownfield_curation.py` with `ErrorCode.<name>.value`.

- [ ] **Step 4: Register command metadata**

In `services/agent_workbench/command_registry.py`, add a `_BROWNFIELD_COMMANDS` tuple:

```python
_BROWNFIELD_COMMANDS: tuple[CommandMetadata, ...] = (
    CommandMetadata(
        name="agileforge brownfield source import",
        mutates=True,
        phase="phase_2b",
        requires_idempotency_key=True,
        idempotency_policy=_REQUIRED_IDEMPOTENCY_POLICY,
        input_required=("project_id", "source_file", "idempotency_key"),
        input_optional=("source_kind", "correlation_id", "changed_by"),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.SPEC_FILE_NOT_FOUND.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
            ErrorCode.MUTATION_IN_PROGRESS.value,
            ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge brownfield scan",
        mutates=True,
        phase="phase_2b",
        requires_idempotency_key=True,
        idempotency_policy=_REQUIRED_IDEMPOTENCY_POLICY,
        input_required=("project_id", "repo_path", "idempotency_key"),
        input_optional=("source_attempt_id", "correlation_id", "changed_by"),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.SPEC_FILE_NOT_FOUND.value,
            ErrorCode.BROWNFIELD_SOURCE_NOT_FOUND.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge brownfield spec draft",
        mutates=True,
        phase="phase_2b",
        requires_idempotency_key=True,
        idempotency_policy=_REQUIRED_IDEMPOTENCY_POLICY,
        input_required=("project_id", "scan_attempt_id", "idempotency_key"),
        input_optional=("user_input", "correlation_id", "changed_by"),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.BROWNFIELD_SCAN_NOT_FOUND.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
        ),
    ),
    CommandMetadata(
        name="agileforge brownfield spec import",
        mutates=True,
        phase="phase_2b",
        requires_idempotency_key=True,
        idempotency_policy=_REQUIRED_IDEMPOTENCY_POLICY,
        input_required=(
            "project_id",
            "curated_spec_file",
            "expected_scan_fingerprint",
            "idempotency_key",
        ),
        input_optional=("parent_draft_attempt_id", "correlation_id", "changed_by"),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.SPEC_FILE_NOT_FOUND.value,
            ErrorCode.SPEC_FILE_INVALID.value,
            ErrorCode.BROWNFIELD_APPROVAL_CHAIN_MISMATCH.value,
            ErrorCode.BROWNFIELD_DRAFT_NOT_FOUND.value,
        ),
    ),
    CommandMetadata(
        name="agileforge brownfield spec approve",
        mutates=True,
        phase="phase_2b",
        requires_idempotency_key=True,
        accepts_expected_state=True,
        accepts_expected_artifact_fingerprint=True,
        idempotency_policy=_REQUIRED_IDEMPOTENCY_POLICY,
        input_required=(
            "project_id",
            "attempt_id",
            "expected_artifact_fingerprint",
            "expected_state",
            "expected_setup_status",
        ),
        input_optional=("idempotency_key", "correlation_id", "changed_by"),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.STALE_STATE.value,
            ErrorCode.STALE_SETUP_STATUS.value,
            ErrorCode.BROWNFIELD_DRAFT_NOT_FOUND.value,
            ErrorCode.BROWNFIELD_DRAFT_STALE.value,
            ErrorCode.BROWNFIELD_DRAFT_INCOMPLETE.value,
            ErrorCode.BROWNFIELD_SOURCE_SUPERSEDED.value,
            ErrorCode.BROWNFIELD_APPROVAL_CHAIN_MISMATCH.value,
            ErrorCode.BROWNFIELD_CURATED_SPEC_ALREADY_REGISTERED.value,
            ErrorCode.BROWNFIELD_APPROVAL_STALE_GUARD.value,
            ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
        ),
    ),
)
```

Append `_BROWNFIELD_COMMANDS` in `command_contracts()`.

- [ ] **Step 5: Add application facade methods**

In `services/agent_workbench/application.py`, add a runner protocol near the other runner protocols:

```python
class _BrownfieldCurationRunner(Protocol):
    """Brownfield setup curation commands exposed through the facade."""

    def source_import(self, **kwargs: Any) -> dict[str, Any]:
        ...

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        ...

    def spec_draft(self, **kwargs: Any) -> dict[str, Any]:
        ...

    def spec_import(self, **kwargs: Any) -> dict[str, Any]:
        ...

    def spec_approve(self, **kwargs: Any) -> dict[str, Any]:
        ...
```

Extend `AgentWorkbenchApplication.__init__`:

```python
        brownfield_runner: _BrownfieldCurationRunner | None = None,
```

Store it with the other injected runners:

```python
        self._brownfield_runner = brownfield_runner
```

Import `BrownfieldCurationRunner` lazily through a `_get_brownfield_runner()` method:

```python
    def _get_brownfield_runner(self) -> _BrownfieldCurationRunner:
        if self._brownfield_runner is None:
            from services.agent_workbench.brownfield_curation import BrownfieldCurationRunner

            self._brownfield_runner = BrownfieldCurationRunner()
        return self._brownfield_runner
```

Add methods:

```python
    def brownfield_source_import(self, **kwargs: Any) -> dict[str, Any]:
        return self._get_brownfield_runner().source_import(**kwargs)

    def brownfield_scan(self, **kwargs: Any) -> dict[str, Any]:
        return self._get_brownfield_runner().scan(**kwargs)

    def brownfield_spec_draft(self, **kwargs: Any) -> dict[str, Any]:
        return self._get_brownfield_runner().spec_draft(**kwargs)

    def brownfield_spec_import(self, **kwargs: Any) -> dict[str, Any]:
        return self._get_brownfield_runner().spec_import(**kwargs)

    def brownfield_spec_approve(self, **kwargs: Any) -> dict[str, Any]:
        return self._get_brownfield_runner().spec_approve(**kwargs)
```

- [ ] **Step 6: Add CLI parser and handlers**

In `cli/main.py`, add protocol methods for the five brownfield facade calls. Add parser structure:

```python
    brownfield = subparsers.add_parser("brownfield", help="Run brownfield setup curation.")
    brownfield_sub = brownfield.add_subparsers(dest="brownfield_command", required=True)
    source = brownfield_sub.add_parser("source", help="Record brownfield sources.")
    source_sub = source.add_subparsers(dest="brownfield_source_command", required=True)
    source_import = source_sub.add_parser("import", help="Import a brownfield source file.")
    source_import.add_argument("--project-id", type=int, required=True)
    source_import.add_argument("--source-file", required=True)
    source_import.add_argument("--source-kind", default="source_file")
    source_import.add_argument("--idempotency-key", required=True)
    source_import.add_argument("--correlation-id")
    source_import.add_argument("--changed-by", default="cli-agent")
    source_import.set_defaults(command_handler=_brownfield_source_import)
```

Add the scan and spec subcommands:

```python
    scan = brownfield_sub.add_parser("scan", help="Scan a brownfield repository.")
    scan.add_argument("--project-id", type=int, required=True)
    scan.add_argument("--repo-path", required=True)
    scan.add_argument("--source-attempt-id")
    scan.add_argument("--idempotency-key", required=True)
    scan.add_argument("--correlation-id")
    scan.add_argument("--changed-by", default="cli-agent")
    scan.set_defaults(command_handler=_brownfield_scan)

    spec = brownfield_sub.add_parser("spec", help="Draft, import, or approve curated specs.")
    spec_sub = spec.add_subparsers(dest="brownfield_spec_command", required=True)
```

Add `draft`, `import`, and `approve` parsers with flags from the command contracts. Add handlers that return `(command_name, application.<method>(...))`.

- [ ] **Step 7: Add CLI smoke integration test**

Add to `tests/test_agent_workbench_project_create_cli_integration.py`:

```python
def test_brownfield_cli_source_scan_import_approve_flow(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_root = tmp_path / "config-root"
    config_root.mkdir()
    monkeypatch.setenv("AGILEFORGE_CONFIG_ROOT", str(config_root))
    workflow = FakeWorkflowPort()
    setup_runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    from services.agent_workbench.brownfield_curation import BrownfieldCurationRunner

    brownfield_runner = BrownfieldCurationRunner(engine=engine, workflow=workflow)
    app = AgentWorkbenchApplication(
        project_setup_runner=setup_runner,
        brownfield_runner=brownfield_runner,
    )

    create_rc = main(
        [
            "project",
            "create",
            "--setup-mode",
            "brownfield",
            "--name",
            "CLI Brownfield Flow",
            "--idempotency-key",
            "cli-brownfield-flow-create",
        ],
        application=app,
    )
    create_payload = _captured_payload(capsys)
    assert create_rc == 0
    project_id = create_payload["data"]["project_id"]

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
    curated = tmp_path / "curated.json"
    curated.write_text(json.dumps(_structured_spec_payload()), encoding="utf-8")

    scan_rc = main(
        [
            "brownfield",
            "scan",
            "--project-id",
            str(project_id),
            "--repo-path",
            str(repo),
            "--idempotency-key",
            "cli-brownfield-flow-scan",
        ],
        application=app,
    )
    scan_payload = _captured_payload(capsys)
    assert scan_rc == 0

    import_rc = main(
        [
            "brownfield",
            "spec",
            "import",
            "--project-id",
            str(project_id),
            "--curated-spec-file",
            str(curated),
            "--expected-scan-fingerprint",
            scan_payload["data"]["artifact_fingerprint"],
            "--idempotency-key",
            "cli-brownfield-flow-import",
        ],
        application=app,
    )
    import_payload = _captured_payload(capsys)
    assert import_rc == 0

    approve_rc = main(
        [
            "brownfield",
            "spec",
            "approve",
            "--project-id",
            str(project_id),
            "--attempt-id",
            import_payload["data"]["attempt_id"],
            "--expected-artifact-fingerprint",
            import_payload["data"]["artifact_fingerprint"],
            "--expected-state",
            "SETUP_REQUIRED",
            "--expected-setup-status",
            "brownfield_curation_required",
            "--idempotency-key",
            "cli-brownfield-flow-approve",
        ],
        application=app,
    )
    approve_payload = _captured_payload(capsys)
    assert approve_rc == 0
    assert approve_payload["data"]["setup_status"] == "authority_compile_required"
```

- [ ] **Step 8: Verify CLI and schema tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_command_schema.py::test_command_schema_payloads_are_available tests/test_agent_workbench_command_schema.py::test_brownfield_command_contracts_are_guarded tests/test_agent_workbench_error_codes.py::test_registry_covers_representative_phase_2a_error_codes tests/test_agent_workbench_project_create_cli_integration.py::test_brownfield_cli_source_scan_import_approve_flow -q
```

Expected: pass.

- [ ] **Step 9: Commit CLI contracts**

Run:

```bash
git add services/agent_workbench/application.py services/agent_workbench/command_registry.py services/agent_workbench/error_codes.py cli/main.py tests/test_agent_workbench_command_schema.py tests/test_agent_workbench_error_codes.py tests/test_agent_workbench_project_create_cli_integration.py
git commit -m "feat: expose brownfield curation CLI"
```

## Task 7: Workflow Progress And Authority Compile Guard

**Files:**
- Modify: `services/agent_workbench/brownfield_curation.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/project_setup.py`
- Modify: `tests/test_agent_workbench_application.py`
- Modify: `tests/test_agent_workbench_project_setup.py`

- [ ] **Step 1: Add failing workflow-next progress test**

In `tests/test_agent_workbench_application.py`, add a fake projection object that returns brownfield setup state:

```python
class BrownfieldReadProjection:
    def project_show(self, *, project_id: int) -> dict[str, object]:
        return {"ok": True, "data": {"project_id": project_id, "name": "Brownfield"}}

    def workflow_state(self, *, project_id: int) -> dict[str, object]:
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "state": {
                    "fsm_state": "SETUP_REQUIRED",
                    "setup_mode": "brownfield",
                    "setup_status": "brownfield_curation_required",
                },
            },
        }
```

Add:

```python
def test_workflow_next_routes_brownfield_curation_required() -> None:
    app = AgentWorkbenchApplication(read_projection=BrownfieldReadProjection())

    result = app.workflow_next(project_id=10)

    assert result["ok"] is True
    commands = [command["command"] for command in result["data"]["next_valid_commands"]]
    assert "agileforge brownfield source import" in commands
    assert "agileforge authority compile" not in commands
```

- [ ] **Step 2: Run failing workflow-next test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py::test_workflow_next_routes_brownfield_curation_required -q
```

Expected: fails because `workflow_next` does not route brownfield status.

- [ ] **Step 3: Add derived progress helper**

In `services/agent_workbench/brownfield_curation.py`, add:

```python
def brownfield_progress(*, engine: Engine, project_id: int) -> dict[str, Any]:
    """Return derived brownfield progress from artifact rows."""
    with Session(engine) as session:
        source = session.exec(
            select(BrownfieldSourceArtifact)
            .where(
                BrownfieldSourceArtifact.project_id == project_id,
                BrownfieldSourceArtifact.status == "complete",
            )
            .order_by(BrownfieldSourceArtifact.created_at.desc())
        ).first()
        scan = session.exec(
            select(BrownfieldScanAttempt)
            .where(
                BrownfieldScanAttempt.project_id == project_id,
                BrownfieldScanAttempt.status == "complete",
            )
            .order_by(BrownfieldScanAttempt.created_at.desc())
        ).first()
        draft = session.exec(
            select(BrownfieldSpecDraftAttempt)
            .where(
                BrownfieldSpecDraftAttempt.project_id == project_id,
                BrownfieldSpecDraftAttempt.status == "complete",
            )
            .order_by(BrownfieldSpecDraftAttempt.created_at.desc())
        ).first()
    return {
        "source": "current" if source is not None else "missing",
        "scan": "current" if scan is not None else "missing",
        "draft": "ready" if draft is not None else "missing",
        "approval": "required" if draft is not None else "blocked",
        "recommended_draft_attempt_id": draft.attempt_id if draft is not None else None,
    }
```

- [ ] **Step 4: Route brownfield workflow next**

In `services/agent_workbench/application.py`, before authority setup routing, add:

```python
        if _fsm_state_from_envelope(workflow) == "SETUP_REQUIRED" and setup_status == "brownfield_curation_required":
            return _brownfield_workflow_next(project_id=project_id, workflow=workflow)
```

Add helper:

```python
def _brownfield_workflow_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
) -> dict[str, Any]:
    commands = [
        {
            "command": "agileforge brownfield source import",
            "args": {"project_id": project_id},
            "reason": "Record raw brownfield source before drafting a curated spec.",
        },
        {
            "command": "agileforge brownfield scan",
            "args": {"project_id": project_id},
            "reason": "Record repository facts before drafting a curated spec.",
        },
        {
            "command": "agileforge brownfield spec draft",
            "args": {"project_id": project_id},
            "reason": "Create a reviewed product-spec candidate from current brownfield artifacts.",
        },
        {
            "command": "agileforge brownfield spec import",
            "args": {"project_id": project_id},
            "reason": "Import a human-edited agileforge.spec.v1 candidate for approval.",
        },
    ]
    data = {
        "project_id": project_id,
        "workflow_state": "SETUP_REQUIRED",
        "setup_status": "brownfield_curation_required",
        "next_valid_commands": commands,
        "blocked_commands": [
            {
                "command": "agileforge authority compile",
                "reason": "Brownfield setup has no approved curated spec yet.",
            }
        ],
        "blocked_future_commands": [],
    }
    data["source_fingerprint"] = canonical_hash(
        {
            "command": WORKFLOW_NEXT_COMMAND,
            "project_id": project_id,
            "workflow": _fingerprint_input(_envelope_data(workflow)),
            "next_valid_commands": commands,
        }
    )
    return {"ok": True, "data": data, "warnings": [], "errors": []}
```

- [ ] **Step 5: Add authority compile guard test**

In `tests/test_agent_workbench_project_setup.py`, add:

```python
def test_authority_compile_rejects_brownfield_before_approval(
    engine: Engine,
) -> None:
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    create = runner.create_project(
        ProjectCreateRequest(
            name="Brownfield Compile Block",
            setup_mode="brownfield",
            spec_file=None,
            idempotency_key="brownfield-compile-block-create",
        )
    )
    project_id = create["data"]["project_id"]

    result = runner.compile_authority(
        AuthorityCompileRequest(
            project_id=project_id,
            spec_version_id=1,
            expected_spec_hash="sha256:missing",
            expected_state="SETUP_REQUIRED",
            expected_setup_status="brownfield_curation_required",
            idempotency_key="brownfield-compile-block",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "BROWNFIELD_APPROVAL_STALE_GUARD"
```

- [ ] **Step 6: Implement compile guard**

In `_authority_compile_workflow_state_or_error` or immediately after it returns in `_run_authority_compile`, add:

```python
        if workflow_state.get("setup_mode") == "brownfield" and workflow_state.get("setup_status") != AUTHORITY_COMPILE_REQUIRED:
            return _error(
                ErrorCode.BROWNFIELD_APPROVAL_STALE_GUARD.value,
                details={
                    "project_id": request.project_id,
                    "setup_status": workflow_state.get("setup_status"),
                },
                remediation=["Approve a brownfield curated spec before authority compile."],
            )
```

Also guard missing approved setup fields:

```python
        if workflow_state.get("setup_mode") == "brownfield":
            required_fields = (
                "setup_spec_file_path",
                "setup_spec_hash",
                "setup_spec_version_id",
            )
            missing = [field for field in required_fields if not workflow_state.get(field)]
            if missing:
                return _error(
                    ErrorCode.BROWNFIELD_APPROVAL_STALE_GUARD.value,
                    details={"missing_fields": missing},
                    remediation=["Replay or recover brownfield spec approval."],
                )
```

- [ ] **Step 7: Verify progress and guard tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py::test_workflow_next_routes_brownfield_curation_required tests/test_agent_workbench_project_setup.py::test_authority_compile_rejects_brownfield_before_approval -q
```

Expected: pass.

- [ ] **Step 8: Commit projections and guards**

Run:

```bash
git add services/agent_workbench/brownfield_curation.py services/agent_workbench/application.py services/agent_workbench/project_setup.py tests/test_agent_workbench_application.py tests/test_agent_workbench_project_setup.py
git commit -m "feat: route brownfield setup progress"
```

## Task 8: API Boundary And Dashboard Greenfield Safety

**Files:**
- Modify: `api.py`
- Modify: `tests/test_api_dashboard.py`

- [ ] **Step 1: Add dashboard greenfield-only test**

In `tests/test_api_dashboard.py`, add a focused test near project create tests:

```python
def test_dashboard_project_create_remains_greenfield_only() -> None:
    schema = CreateProjectRequest.model_json_schema()

    assert "spec_file_path" in schema["properties"]
    assert "setup_mode" not in schema["properties"]
    assert "source_file" not in schema["properties"]
    assert "repo_path" not in schema["properties"]
```

- [ ] **Step 2: Run dashboard boundary test**

Run:

```bash
uv run --frozen pytest tests/test_api_dashboard.py::test_dashboard_project_create_remains_greenfield_only -q
```

Expected: pass if API model is already greenfield-only. If it fails because a prior task added `setup_mode` to the dashboard request, remove it from `CreateProjectRequest` and keep brownfield CLI-only.

- [ ] **Step 3: Keep API create call explicit**

In `api.py`, make the existing greenfield path explicit:

```python
        result = _workbench_application().project_create(
            name=req.name,
            setup_mode="greenfield",
            spec_file=req.spec_file_path,
            idempotency_key=idempotency_key,
            changed_by="dashboard-ui",
        )
```

- [ ] **Step 4: Verify API boundary**

Run:

```bash
uv run --frozen pytest tests/test_api_dashboard.py::test_dashboard_project_create_remains_greenfield_only -q
```

Expected: pass.

- [ ] **Step 5: Commit API boundary**

Run:

```bash
git add api.py tests/test_api_dashboard.py
git commit -m "test: pin dashboard project create boundary"
```

## Task 9: End-To-End Regression And Docs

**Files:**
- Modify: `docs/agent-cli-manual.md`

- [ ] **Step 1: Run focused brownfield suite**

Run:

```bash
uv run --frozen pytest \
  tests/test_brownfield_models.py \
  tests/test_agent_workbench_brownfield_curation.py \
  tests/test_agent_workbench_project_setup.py \
  tests/test_agent_workbench_project_create_cli_integration.py \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_command_schema.py \
  tests/test_agent_workbench_error_codes.py \
  tests/test_business_db_bootstrap.py \
  tests/test_api_dashboard.py \
  -q
```

Expected: pass.

- [ ] **Step 2: Run greenfield regression tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_project_setup.py \
  tests/test_agent_workbench_project_create_cli_integration.py \
  tests/test_spec_authority_compiler_agent.py \
  tests/test_agileforge_spec_profile.py \
  -q
```

Expected: pass.

- [ ] **Step 3: Document brownfield CLI path**

Add to `docs/agent-cli-manual.md` under project setup guidance:

````markdown
### Brownfield product-spec curation

Use brownfield setup only when the available input is raw source, repository
facts, notes, route dumps, or another non-authoritative artifact that must be
curated before authority compilation.

```bash
agileforge project create \
  --setup-mode brownfield \
  --name "InvoicePortal" \
  --idempotency-key "create-invoiceportal-brownfield-001"

agileforge brownfield source import \
  --project-id 42 \
  --source-file notes.md \
  --source-kind notes \
  --idempotency-key "invoiceportal-source-001"

agileforge brownfield scan \
  --project-id 42 \
  --repo-path /workspace/invoice-portal \
  --source-attempt-id source-123 \
  --idempotency-key "invoiceportal-scan-001"

agileforge brownfield spec import \
  --project-id 42 \
  --curated-spec-file curated/spec.json \
  --expected-scan-fingerprint sha256:... \
  --idempotency-key "invoiceportal-import-001"

agileforge brownfield spec approve \
  --project-id 42 \
  --attempt-id draft-import-456 \
  --expected-artifact-fingerprint sha256:... \
  --expected-state SETUP_REQUIRED \
  --expected-setup-status brownfield_curation_required \
  --idempotency-key "invoiceportal-approve-001"
```

Do not pass raw brownfield notes, Markdown, route dumps, or repository paths to
`project create --spec-file`. `authority compile` is available only after
approval writes a managed curated spec path and setup spec fields.
````

- [ ] **Step 4: Verify documentation formatting**

Run:

```bash
rg -n "Brownfield product-spec curation|brownfield spec approve|project create --spec-file" docs/agent-cli-manual.md
```

Expected: all three search terms are present.

- [ ] **Step 5: Run final focused suite**

Run:

```bash
uv run --frozen pytest \
  tests/test_brownfield_models.py \
  tests/test_agent_workbench_brownfield_curation.py \
  tests/test_agent_workbench_project_setup.py \
  tests/test_agent_workbench_project_create_cli_integration.py \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_command_schema.py \
  tests/test_agent_workbench_error_codes.py \
  tests/test_business_db_bootstrap.py \
  tests/test_api_dashboard.py \
  -q
```

Expected: pass.

- [ ] **Step 6: Commit docs and final verification**

Run:

```bash
git add docs/agent-cli-manual.md
git commit -m "docs: document brownfield setup curation"
```

## Self-Review Checklist

- [ ] Raw brownfield source and scan rows never create `SpecRegistry`.
- [ ] `project create --setup-mode brownfield` rejects `--spec-file`.
- [ ] `project create --setup-mode greenfield --spec-file specs/spec.json` still works.
- [ ] `workflow next` does not advertise `authority compile` during `brownfield_curation_required`.
- [ ] `brownfield spec import` validates normalized `agileforge.spec.v1`.
- [ ] `brownfield spec approve` writes managed approved spec path under `get_config_root()/artifacts/brownfield/...`.
- [ ] `setup_spec_file_path` never points at raw source or editor-local import file.
- [ ] Approval replay with same idempotency key does not create duplicate `SpecRegistry` rows.
- [ ] Different idempotency key for an already registered curated draft returns `BROWNFIELD_CURATED_SPEC_ALREADY_REGISTERED`.
- [ ] Dashboard project create remains greenfield-only.

## Execution Handoff

Plan complete when this file exists and red-flag scan passes. Use one of these execution modes:

1. Subagent-Driven: dispatch one fresh subagent per task and review each task diff before continuing.
2. Inline Execution: use `superpowers:executing-plans` and run task batches with explicit checkpoints.
