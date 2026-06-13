# Project Create Authority Compile Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split project creation from authority compilation so `agileforge project create` returns a project/spec shell immediately and `agileforge authority compile` becomes the explicit guarded long-running setup command.

**Architecture:** Keep `ProjectSetupMutationRunner` as the setup orchestration boundary, but split it into two public mutations: `create_project()` for product/spec registration and `compile_authority()` for compiler invocation. Extract a spec-registration helper from `pending_authority_service`, wire a new CLI/API/command-registry contract, and extend `workflow next` setup routing so agents see compile, compiling, compile-failed, and pending-review states as distinct executable steps.

**Tech Stack:** Python 3.12, SQLModel/SQLite, Pydantic, FastAPI, pytest, AgileForge mutation ledger, existing frontend JavaScript dashboard.

---

## File Structure

- Modify `services/specs/pending_authority_service.py`
  - Add a reusable `ensure_pending_spec_version_for_project()` helper that links the project to a structured spec file and creates or reuses the `SpecRegistry` row without invoking the authority compiler.
  - Keep `compile_pending_authority_for_project()` as the compile helper; have it call the new spec-registration helper so registration semantics remain shared.

- Modify `services/agent_workbench/project_setup.py`
  - Add `PROJECT_AUTHORITY_COMPILE_COMMAND`.
  - Add `AuthorityCompileRequest`.
  - Change `create_project()` to persist product/spec metadata and write `setup_status="authority_compile_required"`.
  - Add `compile_authority()` to validate guards, write `authority_compiling`, invoke the compiler, and transition to `authority_pending_review` or `authority_compile_failed`.
  - Add action builders for `authority compile`, mutation inspection, and failed compile retry.

- Modify `services/agent_workbench/application.py`
  - Extend the `_ProjectSetupRunner` protocol and application facade with `authority_compile()`.
  - Extend setup-state routing in `workflow_next()` for `authority_compile_required`, `authority_compiling`, and `authority_compile_failed`.

- Modify `cli/main.py`
  - Add parser support for `agileforge authority compile`.
  - Add application protocol and handler wiring for all authority compile flags.

- Modify `services/agent_workbench/command_registry.py`
  - Register `agileforge authority compile` as a guarded idempotent mutation.
  - Remove authority compiler errors from the normal `project create` contract.

- Modify `api.py`
  - Make `POST /api/projects` return the fast create payload with compile-required state.
  - Add `AuthorityCompileApiRequest` with forbidden extras.
  - Add `POST /api/projects/{project_id}/authority/compile` with CLI-equivalent guard semantics.

- Modify `frontend/project.js`
  - Show an explicit authority compile action for `authority_compile_required` and `authority_compile_failed`.
  - Show mutation-inspection guidance for `authority_compiling`.
  - Do not show authority review, Vision, Backlog, Roadmap, Story, or Sprint controls until authority is pending review or accepted.

- Modify tests:
  - `tests/test_pending_authority_service.py`
  - `tests/test_agent_workbench_project_setup.py`
  - `tests/test_agent_workbench_application.py`
  - `tests/test_agent_workbench_cli.py`
  - `tests/test_agent_workbench_command_schema.py`
  - `tests/test_api_dashboard.py`
  - `tests/test_agent_workbench_project_create_cli_integration.py`

## Task 1: Extract Spec Registration Helper

**Files:**
- Modify: `services/specs/pending_authority_service.py`
- Test: `tests/test_pending_authority_service.py`

- [ ] **Step 1: Write failing spec-registration tests**

Add these tests to `tests/test_pending_authority_service.py` after `test_pending_authority_public_contract_is_keyword_only`:

```python
def test_ensure_pending_spec_version_links_product_without_compiling(
    session: Session, tmp_path: Path
) -> None:
    """Spec registration should persist project/spec metadata only."""
    service = _pending_service()
    product = _create_product(session)
    product_id = require_id(product.product_id, "product_id")
    spec_path = _write_spec(tmp_path)
    compile_calls: list[dict[str, object]] = []

    result = service.ensure_pending_spec_version_for_project(
        session=session,
        product_id=product_id,
        spec_path=spec_path,
        approved_by="cli-project-create",
        lease_guard=lambda boundary: boundary != "never-called",
        record_progress=lambda boundary: boundary != "never-called",
    )

    assert result.ok is True
    assert result.spec_hash is not None
    assert result.spec_version_id is not None
    assert result.authority_id is None
    assert compile_calls == []

    session.expire_all()
    saved_product = session.get(Product, product_id)
    assert saved_product is not None
    assert saved_product.spec_file_path == str(spec_path.resolve())
    assert saved_product.spec_loaded_at is not None

    specs = session.exec(
        select(SpecRegistry).where(SpecRegistry.product_id == product_id)
    ).all()
    assert len(specs) == 1
    assert specs[0].status == "approved"
    assert specs[0].approved_by == "cli-project-create"
    assert specs[0].approval_notes == EXPECTED_APPROVAL_NOTES

    authorities = session.exec(select(CompiledSpecAuthority)).all()
    assert authorities == []
    acceptances = session.exec(select(SpecAuthorityAcceptance)).all()
    assert acceptances == []


def test_ensure_pending_spec_version_reuses_same_hash_registry_row(
    session: Session, tmp_path: Path
) -> None:
    """Re-registering the same spec hash should reuse the latest registry row."""
    service = _pending_service()
    product = _create_product(session)
    product_id = require_id(product.product_id, "product_id")
    spec_path = _write_spec(tmp_path)

    first = service.ensure_pending_spec_version_for_project(
        session=session,
        product_id=product_id,
        spec_path=spec_path,
        approved_by="cli-project-create",
        lease_guard=lambda _boundary: True,
        record_progress=lambda _boundary: True,
    )
    second = service.ensure_pending_spec_version_for_project(
        session=session,
        product_id=product_id,
        spec_path=spec_path,
        approved_by="cli-project-create",
        lease_guard=lambda _boundary: True,
        record_progress=lambda _boundary: True,
    )

    assert first.ok is True
    assert second.ok is True
    assert second.spec_version_id == first.spec_version_id
    specs = session.exec(
        select(SpecRegistry).where(SpecRegistry.product_id == product_id)
    ).all()
    assert len(specs) == 1
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
uv run --frozen pytest tests/test_pending_authority_service.py -q -k "ensure_pending_spec_version"
```

Expected: both tests fail with `AttributeError: module 'services.specs.pending_authority_service' has no attribute 'ensure_pending_spec_version_for_project'`.

- [ ] **Step 3: Implement the helper**

In `services/specs/pending_authority_service.py`, extract the spec-loading, product-linking, spec-registry, and spec-approval block from `compile_pending_authority_for_project()` into:

```python
def ensure_pending_spec_version_for_project(
    *,
    session: Session,
    product_id: int,
    spec_path: Path,
    approved_by: str,
    lease_guard: Callable[[str], bool],
    record_progress: Callable[[str], bool],
) -> PendingAuthorityResult:
    """Register the pending authority source spec without compiling authority."""
    loaded = _load_spec_file(spec_path)
    if isinstance(loaded, PendingAuthorityResult):
        return _result(
            ok=False,
            product_id=product_id,
            spec_path=loaded.spec_path,
            error_code=loaded.error_code,
            error=loaded.error,
        )
    resolved_path, spec_content, spec_hash = loaded
    try:
        normalized_spec = normalize_spec_content_for_registry(spec_content)
    except SpecContentNormalizationError as exc:
        return _result(
            ok=False,
            product_id=product_id,
            spec_path=resolved_path,
            error_code=exc.error_code,
            spec_hash=spec_hash,
            error=str(exc),
        )
    spec_content = normalized_spec.content
    spec_hash = normalized_spec.spec_hash

    product = session.get(Product, product_id)
    if product is None:
        return _result(
            ok=False,
            product_id=product_id,
            spec_path=resolved_path,
            error_code="PRODUCT_NOT_FOUND",
            spec_hash=spec_hash,
            error=f"Product {product_id} not found",
        )

    product.spec_file_path = str(resolved_path)
    product.spec_loaded_at = datetime.now(UTC)
    if not lease_guard("product_spec_linked"):
        session.rollback()
        return _lease_lost(
            product_id=product_id,
            spec_path=resolved_path,
            spec_hash=spec_hash,
            boundary="product_spec_linked",
        )
    session.add(product)
    session.commit()
    progress_error = _record_progress_or_error(
        record_progress=record_progress,
        product_id=product_id,
        spec_path=resolved_path,
        spec_hash=spec_hash,
        spec_version_id=None,
        boundary="product_spec_linked",
    )
    if progress_error is not None:
        return progress_error

    latest_spec = _latest_spec_for_product(session, product_id=product_id)
    if latest_spec and latest_spec.spec_hash == spec_hash:
        spec_version = latest_spec
    else:
        spec_version = SpecRegistry(
            product_id=product_id,
            spec_hash=spec_hash,
            content=spec_content,
            content_ref=str(resolved_path),
            status="draft",
        )
    if not lease_guard("spec_registry_written"):
        session.rollback()
        return _lease_lost(
            product_id=product_id,
            spec_path=resolved_path,
            spec_hash=spec_hash,
            boundary="spec_registry_written",
        )
    session.add(spec_version)
    session.commit()
    session.refresh(spec_version)
    spec_version_id = spec_version.spec_version_id
    if spec_version_id is None:
        return _result(
            ok=False,
            product_id=product_id,
            spec_path=resolved_path,
            error_code="MUTATION_FAILED",
            spec_hash=spec_hash,
            error="Spec registry row did not receive a primary key",
        )
    progress_error = _record_progress_or_error(
        record_progress=record_progress,
        product_id=product_id,
        spec_path=resolved_path,
        spec_hash=spec_hash,
        spec_version_id=spec_version_id,
        boundary="spec_registry_written",
    )
    if progress_error is not None:
        return progress_error

    spec_version.status = "approved"
    spec_version.approved_at = datetime.now(UTC)
    spec_version.approved_by = approved_by
    spec_version.approval_notes = _PENDING_APPROVAL_NOTES
    if not lease_guard("spec_marked_approved"):
        session.rollback()
        return _lease_lost(
            product_id=product_id,
            spec_path=resolved_path,
            spec_hash=spec_hash,
            boundary="spec_marked_approved",
        )
    session.add(spec_version)
    session.commit()
    progress_error = _record_progress_or_error(
        record_progress=record_progress,
        product_id=product_id,
        spec_path=resolved_path,
        spec_hash=spec_hash,
        spec_version_id=spec_version_id,
        boundary="spec_marked_approved",
    )
    if progress_error is not None:
        return progress_error

    return _result(
        ok=True,
        product_id=product_id,
        spec_path=resolved_path,
        spec_hash=spec_hash,
        spec_version_id=spec_version_id,
    )
```

Then replace the duplicated setup block in `compile_pending_authority_for_project()` with:

```python
registered = ensure_pending_spec_version_for_project(
    session=session,
    product_id=product_id,
    spec_path=spec_path,
    approved_by=approved_by,
    lease_guard=lease_guard,
    record_progress=record_progress,
)
if not registered.ok:
    return registered

resolved_path = Path(registered.spec_path)
spec_hash = registered.spec_hash
spec_version_id = registered.spec_version_id
if spec_version_id is None:
    return _result(
        ok=False,
        product_id=product_id,
        spec_path=resolved_path,
        error_code="MUTATION_FAILED",
        spec_hash=spec_hash,
        error="Spec registry row did not receive a primary key",
    )
```

- [ ] **Step 4: Verify helper and existing compile behavior**

Run:

```bash
uv run --frozen pytest tests/test_pending_authority_service.py -q -k "pending_authority or ensure_pending_spec_version"
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add services/specs/pending_authority_service.py tests/test_pending_authority_service.py
git commit -m "refactor(setup): split spec registration from authority compile"
```

## Task 2: Change Project Create To Fast Spec Registration

**Files:**
- Modify: `services/agent_workbench/project_setup.py`
- Test: `tests/test_agent_workbench_project_setup.py`

- [ ] **Step 1: Replace the create-success test with compile-required expectations**

Rename `test_project_create_success_creates_authority_without_acceptance` to `test_project_create_success_registers_spec_without_compiling_authority` and change the assertions to:

```python
def test_project_create_success_registers_spec_without_compiling_authority(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_fast_compiler(monkeypatch)
    fake_workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=fake_workflow)

    result = runner.create_project(
        ProjectCreateRequest(
            name="CLI Project",
            spec_file=str(spec_file),
            idempotency_key="create-project-001",
            changed_by="agent",
        )
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["name"] == "CLI Project"
    assert data["resolved_spec_path"] == str(spec_file.resolve())
    assert data["setup_status"] == "authority_compile_required"
    assert data["fsm_state"] == "SETUP_REQUIRED"
    assert "pending_authority_id" not in data
    assert "compiled_authority_id" not in data
    assert isinstance(data["spec_version_id"], int)
    assert data["next_actions"] == [
        {
            "command": "agileforge authority compile",
            "args": {
                "project_id": data["project_id"],
                "spec_version_id": data["spec_version_id"],
                "expected_spec_hash": data["spec_hash"],
                "expected_state": "SETUP_REQUIRED",
                "expected_setup_status": "authority_compile_required",
            },
            "reason": "Compile pending authority before authority review.",
        }
    ]

    with Session(engine) as session:
        assert len(session.exec(select(Product)).all()) == 1
        assert len(session.exec(select(SpecRegistry)).all()) == 1
        assert session.exec(select(CompiledSpecAuthority)).all() == []
        assert session.exec(select(SpecAuthorityAcceptance)).all() == []
        ledger = session.get(CliMutationLedger, data["mutation_event_id"])
        assert ledger is not None
        assert ledger.status == MutationStatus.SUCCEEDED.value
        assert ledger.project_id == data["project_id"]
        assert "pending_authority_compiled" not in _row_payload(ledger)[
            "completed_steps"
        ]

    assert fake_workflow.created_sessions == [str(data["project_id"])]
    assert fake_workflow.sessions[str(data["project_id"])]["setup_status"] == (
        "authority_compile_required"
    )
    assert fake_workflow.sessions[str(data["project_id"])]["setup_spec_hash"] == (
        data["spec_hash"]
    )
    assert fake_workflow.sessions[str(data["project_id"])]["setup_spec_version_id"] == (
        data["spec_version_id"]
    )
```

- [ ] **Step 2: Run the failing create test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_project_setup.py -q -k "registers_spec_without_compiling"
```

Expected: FAIL because current create compiles authority and returns `authority_pending_review`.

- [ ] **Step 3: Implement compile-required setup state**

In `services/agent_workbench/project_setup.py`:

1. Import `ensure_pending_spec_version_for_project`.
2. Add constants:

```python
PROJECT_AUTHORITY_COMPILE_COMMAND = "agileforge authority compile"
AUTHORITY_COMPILE_FAILED = "authority_compile_failed"
AUTHORITY_COMPILE_REQUIRED = "authority_compile_required"
AUTHORITY_PENDING_REVIEW = "authority_pending_review"
```

3. Change `SyncProjectSetupWorkflowAdapter.ensure_setup_state()` to write:

```python
required_state = {
    "fsm_state": "SETUP_REQUIRED",
    "setup_status": AUTHORITY_COMPILE_REQUIRED,
    "setup_error": None,
    "setup_spec_file_path": str(resolved_spec_path),
    "setup_spec_hash": str(spec_hash),
    "setup_spec_version_id": int(spec_version_id),
    "setup_next_actions": [
        _authority_compile_action(
            project_id=project_id,
            spec_version_id=int(spec_version_id),
            spec_hash=str(spec_hash),
            expected_setup_status=AUTHORITY_COMPILE_REQUIRED,
        )
    ],
}
```

The adapter method signature must become:

```python
def ensure_setup_state(
    self,
    *,
    project_id: int,
    resolved_spec_path: Path,
    spec_hash: str,
    spec_version_id: int,
    lease_guard: Callable[[str], bool],
    record_progress: Callable[[str], bool],
) -> dict[str, Any]:
```

4. Update `ProjectSetupWorkflowPort` and `FakeWorkflowPort` to match the new signature.

5. Replace the create path’s `_ensure_pending_authority()` call with `_ensure_pending_spec_version()`, implemented as:

```python
def _ensure_pending_spec_version(
    self,
    *,
    project_id: int,
    resolved_spec_path: Path,
    mutation_event_id: int,
    lease_owner: str,
) -> dict[str, Any]:
    completed_steps = self._completed_steps(mutation_event_id)
    if "spec_registry_written" in completed_steps:
        existing = self._existing_spec_version(project_id)
        if existing is not None:
            return {"ok": True, **existing}

    def lease_guard(boundary: str) -> bool:
        del boundary
        return self._ledger.require_active_owner(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            now=_now(),
            lease_seconds=self._lease_seconds,
        )

    def record_progress(boundary: str) -> bool:
        return self._ledger.mark_step_complete(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            step=boundary,
            next_step=boundary,
            now=_now(),
        )

    with Session(self._engine) as session:
        result = ensure_pending_spec_version_for_project(
            session=session,
            product_id=project_id,
            spec_path=resolved_spec_path,
            approved_by="cli-project-create",
            lease_guard=lease_guard,
            record_progress=record_progress,
        )
    if not result.ok:
        return {
            "ok": False,
            "error_code": result.error_code or "SPEC_FILE_INVALID",
            "error": result.error,
            "spec_hash": result.spec_hash,
            "spec_version_id": result.spec_version_id,
            "reason": result.reason,
        }
    return {
        "ok": True,
        "spec_hash": result.spec_hash,
        "spec_version_id": result.spec_version_id,
    }
```

6. Change product creation progress to set `next_step="product_spec_linked"` instead of `next_step="pending_authority_compiled"`.

7. Build successful create data without authority ids:

```python
data = {
    "project_id": project_id,
    "name": self._project_name(project_id),
    "resolved_spec_path": str(resolved_spec_path),
    "spec_hash": spec_result["spec_hash"],
    "spec_version_id": spec_result["spec_version_id"],
    "setup_status": AUTHORITY_COMPILE_REQUIRED,
    "fsm_state": "SETUP_REQUIRED",
    "mutation_event_id": mutation_event_id,
    "next_actions": [
        _authority_compile_action(
            project_id=project_id,
            spec_version_id=int(spec_result["spec_version_id"]),
            spec_hash=str(spec_result["spec_hash"]),
            expected_setup_status=AUTHORITY_COMPILE_REQUIRED,
        )
    ],
}
```

- [ ] **Step 4: Verify create now registers only spec metadata**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_project_setup.py -q -k "registers_spec_without_compiling or dry_run or missing_spec or request_validation"
```

Expected: selected tests pass. Existing compile-failure tests fail outside this `-k` because authority compile has not been moved yet.

- [ ] **Step 5: Commit Task 2**

```bash
git add services/agent_workbench/project_setup.py tests/test_agent_workbench_project_setup.py
git commit -m "feat(setup): create projects before authority compile"
```

## Task 3: Add Authority Compile Runner

**Files:**
- Modify: `services/agent_workbench/project_setup.py`
- Test: `tests/test_agent_workbench_project_setup.py`

- [ ] **Step 1: Add authority compile request validation tests**

Add tests near `test_project_create_request_validation_rules`:

```python
def test_authority_compile_request_validation_rules() -> None:
    """Authority compile requires stale guards and correct key mode."""
    request = AuthorityCompileRequest(
        project_id=1,
        spec_version_id=2,
        expected_spec_hash="a" * 64,
        expected_state="SETUP_REQUIRED",
        expected_setup_status="authority_compile_required",
        idempotency_key="authority-compile-001",
        changed_by="agent",
    )

    assert request.project_id == 1
    assert request.spec_version_id == 2

    with pytest.raises(ValidationError):
        AuthorityCompileRequest(
            project_id=1,
            spec_version_id=2,
            expected_spec_hash="a" * 64,
            expected_state="SETUP_REQUIRED",
            expected_setup_status="authority_compile_required",
            dry_run=True,
            idempotency_key="authority-compile-001",
        )

    dry_run = AuthorityCompileRequest(
        project_id=1,
        spec_version_id=2,
        expected_spec_hash="a" * 64,
        expected_state="SETUP_REQUIRED",
        expected_setup_status="authority_compile_required",
        dry_run=True,
        dry_run_id="authority-compile-preview-001",
    )
    assert dry_run.dry_run is True
```

- [ ] **Step 2: Add authority compile success and state-transition tests**

Add the stale guard error metadata test to `tests/test_agent_workbench_error_codes.py`:

```python
def test_authority_compile_stale_guard_error_codes_are_registered() -> None:
    """Authority compile stale guard errors should be registered and retryable."""
    assert ErrorCode.STALE_SETUP_STATUS.value == "STALE_SETUP_STATUS"
    assert ErrorCode.STALE_SPEC_HASH.value == "STALE_SPEC_HASH"
    assert ErrorCode.STALE_SPEC_VERSION.value == "STALE_SPEC_VERSION"

    assert error_metadata(ErrorCode.STALE_SETUP_STATUS).default_exit_code == 3
    assert error_metadata(ErrorCode.STALE_SPEC_HASH).default_exit_code == 3
    assert error_metadata(ErrorCode.STALE_SPEC_VERSION).default_exit_code == 3

    assert error_metadata(ErrorCode.STALE_SETUP_STATUS).retryable is True
    assert error_metadata(ErrorCode.STALE_SPEC_HASH).retryable is True
    assert error_metadata(ErrorCode.STALE_SPEC_VERSION).retryable is True
```

Add these enum values to `services/agent_workbench/error_codes.py` after `STALE_STATE` during the implementation step:

```python
STALE_SETUP_STATUS = "STALE_SETUP_STATUS"
STALE_SPEC_HASH = "STALE_SPEC_HASH"
STALE_SPEC_VERSION = "STALE_SPEC_VERSION"
```

Add these metadata entries after `ErrorCode.STALE_STATE`:

```python
ErrorCode.STALE_SETUP_STATUS: ErrorMetadata(
    code=ErrorCode.STALE_SETUP_STATUS.value,
    default_exit_code=3,
    retryable=True,
    description="Expected setup status did not match.",
),
ErrorCode.STALE_SPEC_HASH: ErrorMetadata(
    code=ErrorCode.STALE_SPEC_HASH.value,
    default_exit_code=3,
    retryable=True,
    description="Expected setup spec hash did not match.",
),
ErrorCode.STALE_SPEC_VERSION: ErrorMetadata(
    code=ErrorCode.STALE_SPEC_VERSION.value,
    default_exit_code=3,
    retryable=True,
    description="Expected setup spec version did not match.",
),
```

- [ ] **Step 3: Run the failing error-code test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_error_codes.py -q -k "authority_compile_stale_guard"
```

Expected: FAIL because `STALE_SETUP_STATUS`, `STALE_SPEC_HASH`, and `STALE_SPEC_VERSION` are not registered.

- [ ] **Step 4: Add stale guard error codes**

Modify `services/agent_workbench/error_codes.py` with the enum and metadata entries shown in Step 2. Also add these entries to `EXPECTED_ERROR_METADATA` in `tests/test_agent_workbench_error_codes.py`:

```python
ErrorCode.STALE_SETUP_STATUS: (3, True),
ErrorCode.STALE_SPEC_HASH: (3, True),
ErrorCode.STALE_SPEC_VERSION: (3, True),
```

- [ ] **Step 5: Verify error-code registration**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_error_codes.py -q -k "authority_compile_stale_guard or registry"
```

Expected: selected tests pass.

- [ ] **Step 6: Add authority compile success and state-transition tests**

Add tests after the create-success test:

```python
def test_authority_compile_succeeds_from_compile_required(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authority compile should be the only normal setup compiler path."""
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_fast_compiler(monkeypatch)
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    created = runner.create_project(
        ProjectCreateRequest(
            name="Compile Later Project",
            spec_file=str(spec_file),
            idempotency_key="create-compile-later-001",
            changed_by="agent",
        )
    )
    created_data = created["data"]

    result = runner.compile_authority(
        AuthorityCompileRequest(
            project_id=created_data["project_id"],
            spec_version_id=created_data["spec_version_id"],
            expected_spec_hash=created_data["spec_hash"],
            expected_state="SETUP_REQUIRED",
            expected_setup_status="authority_compile_required",
            idempotency_key="authority-compile-001",
            changed_by="agent",
        )
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["project_id"] == created_data["project_id"]
    assert data["spec_version_id"] == created_data["spec_version_id"]
    assert data["spec_hash"] == created_data["spec_hash"]
    assert data["setup_status"] == "authority_pending_review"
    assert data["fsm_state"] == "SETUP_REQUIRED"
    assert isinstance(data["pending_authority_id"], int)
    assert data["compiled_authority_id"] == data["pending_authority_id"]
    assert data["next_actions"] == [
        {
            "command": "agileforge authority review",
            "args": {"project_id": created_data["project_id"]},
            "reason": "Review pending compiled authority before acceptance.",
        }
    ]

    with Session(engine) as session:
        authorities = session.exec(select(CompiledSpecAuthority)).all()
        assert len(authorities) == 1
        ledger = session.get(CliMutationLedger, data["mutation_event_id"])
        assert ledger is not None
        assert ledger.command == "agileforge authority compile"
        assert ledger.status == MutationStatus.SUCCEEDED.value
        assert "pending_authority_compiled" in _row_payload(ledger)["completed_steps"]

    state = workflow.sessions[str(created_data["project_id"])]
    assert state["setup_status"] == "authority_pending_review"
    assert state["setup_compile_mutation_event_id"] == data["mutation_event_id"]


def test_authority_compile_marks_compiling_before_invocation(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workflow state should expose the long-running compile before the LLM call."""
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    workflow = FakeWorkflowPort()
    observed_states: list[dict[str, Any]] = []

    def observing_compiler(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        observed_states.append(workflow.sessions["1"].copy())
        return {
            "success": True,
            "authority_id": 1,
            "spec_version_id": 1,
            "compiler_version": "fake",
            "prompt_hash": "a" * 64,
        }

    monkeypatch.setattr(
        "services.agent_workbench.project_setup.compile_spec_authority_for_version_with_engine",
        lambda **kwargs: observing_compiler(**kwargs),
    )
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    created = runner.create_project(
        ProjectCreateRequest(
            name="Observed Compile Project",
            spec_file=str(spec_file),
            idempotency_key="create-observed-compile-001",
            changed_by="agent",
        )
    )

    runner.compile_authority(
        AuthorityCompileRequest(
            project_id=created["data"]["project_id"],
            spec_version_id=created["data"]["spec_version_id"],
            expected_spec_hash=created["data"]["spec_hash"],
            expected_state="SETUP_REQUIRED",
            expected_setup_status="authority_compile_required",
            idempotency_key="authority-compile-observed-001",
            changed_by="agent",
        )
    )

    assert observed_states
    assert observed_states[0]["setup_status"] == "authority_compiling"
    assert observed_states[0]["setup_compile_mutation_event_id"] is not None
```

- [ ] **Step 7: Add authority compile failure and stale-guard tests**

Add:

```python
def test_authority_compile_failure_records_retryable_compile_failed(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compiler failures should not route through project setup retry."""
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    _install_fast_compiler(monkeypatch)
    created = runner.create_project(
        ProjectCreateRequest(
            name="Compile Failure Later Project",
            spec_file=str(spec_file),
            idempotency_key="create-compile-fail-later-001",
            changed_by="agent",
        )
    )
    _install_failing_compiler(
        monkeypatch,
        failure_artifact_id="spec-authority-failure-compile-command",
        blocking_gaps=["source_map excerpt does not mention required field"],
    )

    failed = runner.compile_authority(
        AuthorityCompileRequest(
            project_id=created["data"]["project_id"],
            spec_version_id=created["data"]["spec_version_id"],
            expected_spec_hash=created["data"]["spec_hash"],
            expected_state="SETUP_REQUIRED",
            expected_setup_status="authority_compile_required",
            idempotency_key="authority-compile-fails-001",
            changed_by="agent",
        )
    )

    assert failed["ok"] is False
    assert _error_code(failed) == "SPEC_COMPILE_FAILED"
    data = failed["data"]
    assert data["setup_status"] == "authority_compile_failed"
    assert data["setup_failure_stage"] == "authority_compile"
    assert data["setup_failure_artifact_id"] == "spec-authority-failure-compile-command"
    assert data["next_actions"][0]["command"] == "agileforge authority compile"
    assert data["next_actions"][0]["args"]["expected_setup_status"] == (
        "authority_compile_failed"
    )
    assert workflow.sessions[str(data["project_id"])]["setup_status"] == (
        "authority_compile_failed"
    )


@pytest.mark.parametrize(
    ("field", "replacement", "code"),
    [
        ("expected_state", "VISION_INTERVIEW", ErrorCode.STALE_STATE.value),
        (
            "expected_setup_status",
            "authority_pending_review",
            ErrorCode.STALE_SETUP_STATUS.value,
        ),
        ("expected_spec_hash", "b" * 64, ErrorCode.STALE_SPEC_HASH.value),
        ("spec_version_id", 999999, ErrorCode.STALE_SPEC_VERSION.value),
    ],
)
def test_authority_compile_rejects_stale_guards(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: object,
    code: str,
) -> None:
    """Authority compile should reject stale state and spec guards."""
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_fast_compiler(monkeypatch)
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    created = runner.create_project(
        ProjectCreateRequest(
            name=f"Stale Guard Project {field}",
            spec_file=str(spec_file),
            idempotency_key=f"create-stale-guard-{field}",
            changed_by="agent",
        )
    )
    request_data = {
        "project_id": created["data"]["project_id"],
        "spec_version_id": created["data"]["spec_version_id"],
        "expected_spec_hash": created["data"]["spec_hash"],
        "expected_state": "SETUP_REQUIRED",
        "expected_setup_status": "authority_compile_required",
        "idempotency_key": f"authority-compile-stale-{field}",
        "changed_by": "agent",
    }
    request_data[field] = replacement

    result = runner.compile_authority(AuthorityCompileRequest(**request_data))

    assert result["ok"] is False
    assert _error_code(result) == code
```

- [ ] **Step 8: Run the failing authority compile tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_project_setup.py -q -k "authority_compile"
```

Expected: FAIL because `AuthorityCompileRequest` and `compile_authority()` do not exist.

- [ ] **Step 9: Implement request model and runner method**

In `services/agent_workbench/project_setup.py`, add:

```python
class AuthorityCompileRequest(BaseModel):
    """Validated request for `agileforge authority compile`."""

    project_id: int
    spec_version_id: int
    expected_spec_hash: str = Field(min_length=1)
    expected_state: str = Field(min_length=1)
    expected_setup_status: str = Field(min_length=1)
    idempotency_key: str | None = None
    dry_run: bool = False
    dry_run_id: str | None = None
    correlation_id: str | None = None
    changed_by: str = "cli-agent"

    @model_validator(mode="after")
    def _validate_mutation_keys(self) -> AuthorityCompileRequest:
        _validate_key_mode(
            dry_run=self.dry_run,
            idempotency_key=self.idempotency_key,
            dry_run_id=self.dry_run_id,
        )
        return self

    def normalized_request_hash(self) -> str:
        """Return a stable hash including authority compile guards."""
        return canonical_hash(
            {
                "command": PROJECT_AUTHORITY_COMPILE_COMMAND,
                "project_id": self.project_id,
                "spec_version_id": self.spec_version_id,
                "expected_spec_hash": self.expected_spec_hash,
                "expected_state": self.expected_state,
                "expected_setup_status": self.expected_setup_status,
                "changed_by": self.changed_by,
            }
        )
```

Add public method:

```python
def compile_authority(self, request: AuthorityCompileRequest) -> dict[str, Any]:
    """Compile pending authority for an already-created project/spec shell."""
    return self._run_authority_compile(request)
```

Implement `_run_authority_compile()` using the same mutation-ledger pattern as `_run_retry()`:

- validate the project exists;
- validate workflow `fsm_state`, `setup_status`, `setup_spec_hash`, and `setup_spec_version_id`;
- support dry-run by returning `preview_available=True` and the compile action;
- create/load ledger command `PROJECT_AUTHORITY_COMPILE_COMMAND`;
- write workflow status `authority_compiling` before `_ensure_pending_authority()`;
- call `_ensure_pending_authority()`;
- on success write `authority_pending_review` and finalize success;
- on compiler failure write `authority_compile_failed`, mark validation failed, and return retryable compile action.

Use this status writer:

```python
def _write_authority_compile_workflow_state(
    self,
    *,
    project_id: int,
    status: str,
    resolved_spec_path: Path,
    spec_hash: str,
    spec_version_id: int,
    mutation_event_id: int,
    failure_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "fsm_state": "SETUP_REQUIRED",
        "setup_status": status,
        "setup_error": None if failure_data is None else failure_data.get("setup_error"),
        "setup_spec_file_path": str(resolved_spec_path),
        "setup_spec_hash": spec_hash,
        "setup_spec_version_id": spec_version_id,
        "setup_compile_mutation_event_id": mutation_event_id,
    }
    if status == "authority_compiling":
        state["setup_compile_started_at"] = _now().isoformat()
        state["setup_next_actions"] = [
            {
                "command": "agileforge mutation show",
                "args": {"mutation_event_id": mutation_event_id},
                "reason": "Inspect the active authority compile mutation.",
            }
        ]
    if status == "authority_pending_review":
        state["setup_next_actions"] = [_authority_status_action(project_id)]
    if status == "authority_compile_failed" and failure_data is not None:
        state.update(failure_data)
        state["setup_next_actions"] = [
            _authority_compile_action(
                project_id=project_id,
                spec_version_id=spec_version_id,
                spec_hash=spec_hash,
                expected_setup_status="authority_compile_failed",
            )
        ]
    self._workflow.update_session_status(str(project_id), state)
    return {"ok": True, "state": self._workflow.get_session_status(str(project_id))}
```

- [ ] **Step 10: Verify authority compile runner**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_project_setup.py -q -k "authority_compile or registers_spec_without_compiling"
```

Expected: selected tests pass.

- [ ] **Step 11: Commit Task 3**

```bash
git add services/agent_workbench/project_setup.py services/agent_workbench/error_codes.py tests/test_agent_workbench_project_setup.py
git commit -m "feat(setup): add guarded authority compile mutation"
```

## Task 4: Route Authority Compile Through Application And CLI

**Files:**
- Modify: `services/agent_workbench/application.py`
- Modify: `cli/main.py`
- Test: `tests/test_agent_workbench_application.py`
- Test: `tests/test_agent_workbench_cli.py`

- [ ] **Step 1: Add application facade test**

In `tests/test_agent_workbench_application.py`, extend `_FakeProjectSetupRunner` with:

```python
def compile_authority(self, request: object) -> dict[str, Any]:
    """Return an authority compile payload."""
    self.calls.append(("compile_authority", request))
    return {"ok": True, "data": {"project_id": PROJECT_ID}, "warnings": [], "errors": []}
```

Add:

```python
def test_application_routes_authority_compile_to_setup_runner() -> None:
    """Verify authority compile facade builds the guarded request model."""
    runner = _FakeProjectSetupRunner()
    app = AgentWorkbenchApplication(project_setup_runner=runner)

    result = app.authority_compile(
        project_id=PROJECT_ID,
        spec_version_id=SPEC_VERSION_ID,
        expected_spec_hash="a" * 64,
        expected_state="SETUP_REQUIRED",
        expected_setup_status="authority_compile_required",
        idempotency_key="authority-compile-cli-001",
        dry_run=False,
        dry_run_id=None,
        correlation_id="corr-1",
        changed_by="test-agent",
    )

    assert result["ok"] is True
    assert runner.calls[0][0] == "compile_authority"
    request = cast("AuthorityCompileRequest", runner.calls[0][1])
    assert request.project_id == PROJECT_ID
    assert request.spec_version_id == SPEC_VERSION_ID
    assert request.expected_spec_hash == "a" * 64
    assert request.expected_state == "SETUP_REQUIRED"
    assert request.expected_setup_status == "authority_compile_required"
    assert request.idempotency_key == "authority-compile-cli-001"
    assert request.correlation_id == "corr-1"
    assert request.changed_by == "test-agent"
```

- [ ] **Step 2: Add CLI routing tests**

In `tests/test_agent_workbench_cli.py`, add `_FakeApplication.authority_compile()`:

```python
def authority_compile(  # noqa: PLR0913
    self,
    *,
    project_id: int,
    spec_version_id: int,
    expected_spec_hash: str,
    expected_state: str,
    expected_setup_status: str,
    idempotency_key: str | None = None,
    dry_run: bool = False,
    dry_run_id: str | None = None,
    correlation_id: str | None = None,
    changed_by: str = "cli-agent",
) -> JsonObject:
    """Return an authority compile payload."""
    self.calls.append(
        (
            "authority_compile",
            {
                "project_id": project_id,
                "spec_version_id": spec_version_id,
                "expected_spec_hash": expected_spec_hash,
                "expected_state": expected_state,
                "expected_setup_status": expected_setup_status,
                "idempotency_key": idempotency_key,
                "dry_run": dry_run,
                "dry_run_id": dry_run_id,
                "correlation_id": correlation_id,
                "changed_by": changed_by,
            },
        )
    )
    return {"ok": True, "data": {"project_id": project_id}, "warnings": [], "errors": []}
```

Add:

```python
def test_cli_routes_authority_compile_to_application(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify authority compile routes stale guards to the application facade."""
    app = _FakeApplication()

    rc = main(
        [
            "authority",
            "compile",
            "--project-id",
            str(PROJECT_ID),
            "--spec-version-id",
            str(SPEC_VERSION_ID),
            "--expected-spec-hash",
            "a" * 64,
            "--expected-state",
            "SETUP_REQUIRED",
            "--expected-setup-status",
            "authority_compile_required",
            "--idempotency-key",
            "authority-compile-cli-001",
            "--changed-by",
            "test-agent",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == "agileforge authority compile"
    assert app.calls == [
        (
            "authority_compile",
            {
                "project_id": PROJECT_ID,
                "spec_version_id": SPEC_VERSION_ID,
                "expected_spec_hash": "a" * 64,
                "expected_state": "SETUP_REQUIRED",
                "expected_setup_status": "authority_compile_required",
                "idempotency_key": "authority-compile-cli-001",
                "dry_run": False,
                "dry_run_id": None,
                "correlation_id": None,
                "changed_by": "test-agent",
            },
        )
    ]


def test_cli_routes_authority_compile_dry_run_without_idempotency_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify authority compile dry-run routes without consuming idempotency."""
    app = _FakeApplication()

    rc = main(
        [
            "authority",
            "compile",
            "--project-id",
            str(PROJECT_ID),
            "--spec-version-id",
            str(SPEC_VERSION_ID),
            "--expected-spec-hash",
            "a" * 64,
            "--expected-state",
            "SETUP_REQUIRED",
            "--expected-setup-status",
            "authority_compile_required",
            "--dry-run",
            "--dry-run-id",
            "authority-compile-preview-001",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == "agileforge authority compile"
    assert app.calls[0][0] == "authority_compile"
    assert app.calls[0][1]["dry_run"] is True
    assert app.calls[0][1]["idempotency_key"] is None
    assert app.calls[0][1]["dry_run_id"] == "authority-compile-preview-001"
```

- [ ] **Step 3: Run failing routing tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "authority_compile_to_setup_runner"
uv run --frozen pytest tests/test_agent_workbench_cli.py -q -k "authority_compile"
```

Expected: FAIL because facade and parser wiring do not exist.

- [ ] **Step 4: Implement application and CLI routing**

In `services/agent_workbench/application.py`:

- Import `AuthorityCompileRequest`.
- Add `compile_authority()` to `_ProjectSetupRunner`.
- Add `authority_compile()` to `AgentWorkbenchApplication` with the same signature used in the tests.

In `cli/main.py`:

- Add `authority_compile()` to `_Application`.
- Add parser:

```python
authority_compile = authority_sub.add_parser(
    "compile",
    help="Compile pending Spec Authority for a created project.",
)
authority_compile.add_argument("--project-id", type=int, required=True)
authority_compile.add_argument("--spec-version-id", type=int, required=True)
authority_compile.add_argument("--expected-spec-hash", required=True)
authority_compile.add_argument("--expected-state", required=True)
authority_compile.add_argument("--expected-setup-status", required=True)
authority_compile.add_argument("--idempotency-key")
authority_compile.add_argument("--dry-run", action="store_true")
authority_compile.add_argument("--dry-run-id")
authority_compile.add_argument("--correlation-id")
authority_compile.add_argument("--changed-by", default="cli-agent")
authority_compile.set_defaults(command_handler=_authority_compile)
```

- Add handler:

```python
def _authority_compile(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route authority compile to the application facade."""
    command = "agileforge authority compile"
    validation_error = _validate_mutation_idempotency_args(args)
    if validation_error is not None:
        return _mutation_arg_error(command, validation_error)
    return command, application.authority_compile(
        project_id=args.project_id,
        spec_version_id=args.spec_version_id,
        expected_spec_hash=args.expected_spec_hash,
        expected_state=args.expected_state,
        expected_setup_status=args.expected_setup_status,
        idempotency_key=args.idempotency_key,
        dry_run=args.dry_run,
        dry_run_id=args.dry_run_id,
        correlation_id=args.correlation_id,
        changed_by=args.changed_by,
    )
```

- [ ] **Step 5: Verify routing**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "project_create_to_setup_runner or project_setup_retry_to_setup_runner or authority_compile_to_setup_runner"
uv run --frozen pytest tests/test_agent_workbench_cli.py -q -k "project_create or project_setup_retry or authority_compile"
```

Expected: selected tests pass.

- [ ] **Step 6: Commit Task 4**

```bash
git add services/agent_workbench/application.py cli/main.py tests/test_agent_workbench_application.py tests/test_agent_workbench_cli.py
git commit -m "feat(cli): route authority compile command"
```

## Task 5: Register Authority Compile Command Schema

**Files:**
- Modify: `services/agent_workbench/command_registry.py`
- Test: `tests/test_agent_workbench_command_schema.py`

- [ ] **Step 1: Add command-registry test**

In `tests/test_agent_workbench_command_schema.py`, add:

```python
def test_authority_compile_is_registered_as_guarded_mutation() -> None:
    """Publish the authority compile mutation contract for agents."""
    schema = command_schema_payload("agileforge authority compile")

    assert schema["mutates"] is True
    assert schema["idempotency_required"] is True
    assert schema["idempotency_policy"] == DRY_RUN_IDEMPOTENCY_POLICY
    assert schema["guard_policy"] == [
        "expected_state",
        "expected_setup_status",
        "expected_spec_hash",
        "spec_version_id",
    ]
    assert schema["input"]["required"] == [
        "project_id",
        "spec_version_id",
        "expected_spec_hash",
        "expected_state",
        "expected_setup_status",
    ]
    assert "idempotency_key" in schema["input"]["optional"]
    assert "dry_run" in schema["input"]["optional"]
    assert "dry_run_id" in schema["input"]["optional"]
    assert ErrorCode.SPEC_COMPILE_FAILED.value in schema["errors"]
    assert ErrorCode.STALE_STATE.value in schema["errors"]
    assert ErrorCode.MUTATION_IN_PROGRESS.value in schema["errors"]
```

Update `EXPECTED_PHASE_2B_COMMAND_NAMES` or `EXPECTED_PHASE_2C_COMMAND_NAMES` to include `"agileforge authority compile"`. Prefer Phase 2C because it is authority-specific, even though it is part of setup.

- [ ] **Step 2: Run failing command schema test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_command_schema.py -q -k "authority_compile or project_create_is_registered"
```

Expected: FAIL because `authority compile` is not registered and project create still exposes compile errors.

- [ ] **Step 3: Implement command metadata**

In `services/agent_workbench/command_registry.py`:

- Add `guard_policy` support for `expected_setup_status`, `expected_spec_hash`, and `spec_version_id` if `_guard_policy()` already filters metadata.
- Add this `CommandMetadata` to `_PHASE_2C_COMMANDS` before `authority review`:

```python
CommandMetadata(
    name="agileforge authority compile",
    mutates=True,
    phase="phase_2c",
    requires_idempotency_key=True,
    accepts_expected_state=True,
    guard_policy=(
        "expected_state",
        "expected_setup_status",
        "expected_spec_hash",
        "spec_version_id",
    ),
    idempotency_policy=_DRY_RUN_IDEMPOTENCY_POLICY,
    input_required=(
        "project_id",
        "spec_version_id",
        "expected_spec_hash",
        "expected_state",
        "expected_setup_status",
    ),
    input_optional=(
        "idempotency_key",
        "dry_run",
        "dry_run_id",
        "correlation_id",
        "changed_by",
    ),
    errors=(
        ErrorCode.SCHEMA_NOT_READY.value,
        ErrorCode.PROJECT_NOT_FOUND.value,
        ErrorCode.SPEC_COMPILE_FAILED.value,
        ErrorCode.STALE_STATE.value,
        ErrorCode.STALE_SETUP_STATUS.value,
        ErrorCode.STALE_SPEC_HASH.value,
        ErrorCode.STALE_SPEC_VERSION.value,
        ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
        ErrorCode.MUTATION_IN_PROGRESS.value,
        ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
        ErrorCode.MUTATION_RESUME_CONFLICT.value,
    ),
)
```

- Remove `ErrorCode.SPEC_COMPILE_FAILED.value` from the `agileforge project create` errors tuple.

- [ ] **Step 4: Verify command registry**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_command_schema.py -q -k "authority_compile or project_create_is_registered or phase_2"
```

Expected: selected tests pass.

- [ ] **Step 5: Commit Task 5**

```bash
git add services/agent_workbench/command_registry.py tests/test_agent_workbench_command_schema.py
git commit -m "feat(commands): publish authority compile contract"
```

## Task 6: Add Setup Workflow Next Routing

**Files:**
- Modify: `services/agent_workbench/application.py`
- Test: `tests/test_agent_workbench_application.py`

- [ ] **Step 1: Add workflow-next routing tests**

Add near existing setup workflow tests:

```python
def test_workflow_next_routes_compile_required_to_authority_compile() -> None:
    """Setup projects without pending authority should route to compile."""
    app = AgentWorkbenchApplication(
        workflow_reader=_FakeWorkflowReader(
            {
                "project_id": PROJECT_ID,
                "state": {
                    "fsm_state": "SETUP_REQUIRED",
                    "setup_status": "authority_compile_required",
                    "setup_spec_file_path": "/tmp/agileforge/spec.json",
                    "setup_spec_hash": "a" * 64,
                    "setup_spec_version_id": SPEC_VERSION_ID,
                },
            }
        )
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "authority_compile_required"
    assert data["next_valid_commands"] == [
        "agileforge authority compile "
        f"--project-id {PROJECT_ID} "
        f"--spec-version-id {SPEC_VERSION_ID} "
        f"--expected-spec-hash {'a' * 64} "
        "--expected-state SETUP_REQUIRED "
        "--expected-setup-status authority_compile_required"
    ]
    assert data["next_actions"][0]["command"] == data["next_valid_commands"][0]


def test_workflow_next_routes_compile_failed_to_authority_compile_retry() -> None:
    """Failed compiler setup should retry compile, not project setup retry."""
    app = AgentWorkbenchApplication(
        workflow_reader=_FakeWorkflowReader(
            {
                "project_id": PROJECT_ID,
                "state": {
                    "fsm_state": "SETUP_REQUIRED",
                    "setup_status": "authority_compile_failed",
                    "setup_spec_file_path": "/tmp/agileforge/spec.json",
                    "setup_spec_hash": "b" * 64,
                    "setup_spec_version_id": SPEC_VERSION_ID,
                    "setup_failure_stage": "authority_compile",
                    "setup_failure_summary": "Compiler output failed validation.",
                },
            }
        )
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "authority_compile_failed"
    assert data["next_valid_commands"][0].endswith(
        "--expected-setup-status authority_compile_failed"
    )
    assert "project setup retry" not in data["next_valid_commands"][0]


def test_workflow_next_routes_compiling_to_mutation_inspection() -> None:
    """Active compile state should expose mutation inspection commands."""
    app = AgentWorkbenchApplication(
        workflow_reader=_FakeWorkflowReader(
            {
                "project_id": PROJECT_ID,
                "state": {
                    "fsm_state": "SETUP_REQUIRED",
                    "setup_status": "authority_compiling",
                    "setup_compile_mutation_event_id": 123,
                    "setup_spec_hash": "c" * 64,
                    "setup_spec_version_id": SPEC_VERSION_ID,
                },
            }
        )
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "authority_compiling"
    assert data["next_valid_commands"] == [
        "agileforge mutation show --mutation-event-id 123",
        f"agileforge mutation list --project-id {PROJECT_ID} --status pending",
        f"agileforge authority status --project-id {PROJECT_ID}",
    ]
```

- [ ] **Step 2: Run failing workflow tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "compile_required or compile_failed or compiling_to_mutation"
```

Expected: FAIL because setup routing ignores these statuses.

- [ ] **Step 3: Implement setup routing**

In `services/agent_workbench/application.py`:

- Include `authority_compile_required`, `authority_compiling`, and `authority_compile_failed` in the `SETUP_REQUIRED` branch.
- Avoid calling `authority_status()` for compile-required, compiling, and compile-failed states.
- Add helper:

```python
def _authority_compile_command_from_workflow(
    *,
    project_id: int,
    workflow: dict[str, Any],
    expected_setup_status: str,
) -> dict[str, Any]:
    """Build a guarded authority compile command from workflow setup fields."""
    state = _envelope_data(workflow).get("state")
    workflow_state = state if isinstance(state, dict) else {}
    spec_hash = workflow_state.get("setup_spec_hash")
    spec_version_id = workflow_state.get("setup_spec_version_id")
    if not isinstance(spec_hash, str) or not spec_hash.strip():
        return {
            "command": "agileforge authority compile",
            "runnable": False,
            "reason": "Authority compile requires setup_spec_hash in workflow state.",
        }
    if not isinstance(spec_version_id, int):
        return {
            "command": "agileforge authority compile",
            "runnable": False,
            "reason": "Authority compile requires setup_spec_version_id in workflow state.",
        }
    return {
        "command": (
            "agileforge authority compile "
            f"--project-id {project_id} "
            f"--spec-version-id {spec_version_id} "
            f"--expected-spec-hash {quote(spec_hash)} "
            "--expected-state SETUP_REQUIRED "
            f"--expected-setup-status {expected_setup_status}"
        ),
        "runnable": True,
    }
```

- In `_setup_workflow_next()`, add branches:

```python
if setup_status in {"authority_compile_required", "authority_compile_failed"}:
    compile_command = _authority_compile_command_from_workflow(
        project_id=project_id,
        workflow=workflow,
        expected_setup_status=setup_status,
    )
    data["status"] = setup_status
    if compile_command.get("runnable"):
        data["next_valid_commands"] = [str(compile_command["command"])]
        data["next_actions"] = [
            {
                "command": compile_command["command"],
                "installed": True,
                "requires_cli_installation": False,
                "reason": "Compile pending authority before authority review.",
            },
            {
                "command": f"agileforge authority status --project-id {project_id}",
                "installed": True,
                "requires_cli_installation": False,
                "reason": "Inspect current authority/setup state.",
            },
        ]
    else:
        data["blocked_commands"] = [
            {
                "command": compile_command["command"],
                "installed": True,
                "reason": compile_command["reason"],
            }
        ]
```

For `authority_compiling`, return mutation commands using `setup_compile_mutation_event_id`.

- [ ] **Step 4: Verify setup routing**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "workflow_next_routes_compile or workflow_next_routes_failed_setup or authority_pending_review"
```

Expected: selected tests pass.

- [ ] **Step 5: Commit Task 6**

```bash
git add services/agent_workbench/application.py tests/test_agent_workbench_application.py
git commit -m "feat(workflow): route setup through authority compile"
```

## Task 7: Add API Authority Compile Endpoint

**Files:**
- Modify: `api.py`
- Test: `tests/test_api_dashboard.py`

- [ ] **Step 1: Add API create and compile tests**

In `tests/test_api_dashboard.py`, update the fake application with `authority_compile()` mirroring the real facade arguments.

Add:

```python
def test_create_project_returns_compile_required_without_pending_authority(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dashboard create should return immediately before authority compilation."""
    app = _FakeWorkbenchApplication()
    app.results["project_create"] = {
        "ok": True,
        "data": {
            "project_id": 10,
            "name": "API Project",
            "setup_status": "authority_compile_required",
            "fsm_state": "SETUP_REQUIRED",
            "spec_hash": "a" * 64,
            "spec_version_id": 3,
            "next_actions": [
                {
                    "command": "agileforge authority compile",
                    "args": {
                        "project_id": 10,
                        "spec_version_id": 3,
                        "expected_spec_hash": "a" * 64,
                        "expected_state": "SETUP_REQUIRED",
                        "expected_setup_status": "authority_compile_required",
                    },
                    "reason": "Compile pending authority before authority review.",
                }
            ],
        },
        "warnings": [],
        "errors": [],
    }
    monkeypatch.setattr(api, "_workbench_application", lambda: app)

    response = client.post(
        "/api/projects",
        json={"name": "API Project", "spec_file_path": "specs/spec.json"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["id"] == 10
    assert payload["data"]["setup_status"] == "authority_compile_required"
    assert payload["data"]["fsm_state"] == "SETUP_REQUIRED"
    assert payload["data"]["spec_hash"] == "a" * 64
    assert payload["data"]["spec_version_id"] == 3
    assert payload["data"]["next_actions"][0]["command"] == (
        "agileforge authority compile"
    )


def test_authority_compile_api_routes_guarded_request(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authority compile API should mirror the CLI guarded mutation."""
    app = _FakeWorkbenchApplication()
    app.results["authority_compile"] = {
        "ok": True,
        "data": {
            "project_id": 10,
            "spec_version_id": 3,
            "spec_hash": "a" * 64,
            "pending_authority_id": 99,
            "compiled_authority_id": 99,
            "setup_status": "authority_pending_review",
            "fsm_state": "SETUP_REQUIRED",
            "mutation_event_id": 123,
            "next_actions": [
                {
                    "command": "agileforge authority review",
                    "args": {"project_id": 10},
                    "reason": "Review pending compiled authority before acceptance.",
                }
            ],
        },
        "warnings": [],
        "errors": [],
    }
    monkeypatch.setattr(api, "_workbench_application", lambda: app)

    response = client.post(
        "/api/projects/10/authority/compile",
        json={
            "spec_version_id": 3,
            "expected_spec_hash": "a" * 64,
            "expected_state": "SETUP_REQUIRED",
            "expected_setup_status": "authority_compile_required",
            "idempotency_key": "authority-compile-api-001",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["setup_status"] == "authority_pending_review"
    assert app.calls[-1] == (
        "authority_compile",
        {
            "project_id": 10,
            "spec_version_id": 3,
            "expected_spec_hash": "a" * 64,
            "expected_state": "SETUP_REQUIRED",
            "expected_setup_status": "authority_compile_required",
            "idempotency_key": "authority-compile-api-001",
            "changed_by": "dashboard-ui",
        },
    )


def test_authority_compile_api_forbids_extra_fields(client: TestClient) -> None:
    """Removed or unrelated fields should fail validation instead of being ignored."""
    response = client.post(
        "/api/projects/10/authority/compile",
        json={
            "spec_version_id": 3,
            "expected_spec_hash": "a" * 64,
            "expected_state": "SETUP_REQUIRED",
            "expected_setup_status": "authority_compile_required",
            "idempotency_key": "authority-compile-api-001",
            "spec_file_path": "specs/spec.json",
        },
    )

    assert response.status_code == 422
```

- [ ] **Step 2: Run failing API tests**

Run:

```bash
uv run --frozen pytest tests/test_api_dashboard.py -q -k "compile_required or authority_compile_api"
```

Expected: FAIL because the API endpoint and response fields do not exist.

- [ ] **Step 3: Implement API models and route**

In `api.py`:

- Import `ConfigDict` from Pydantic if not already imported.
- Add:

```python
class AuthorityCompileApiRequest(BaseModel):
    """Request body for guarded authority compilation."""

    model_config = ConfigDict(extra="forbid")

    spec_version_id: int
    expected_spec_hash: str = Field(min_length=1)
    expected_state: str = Field(min_length=1)
    expected_setup_status: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=8, max_length=128)
```

- Update `create_project()` success payload to include `spec_hash`, `spec_version_id`, and `next_actions`, and default setup status to `authority_compile_required`.
- Add:

```python
@app.post("/api/projects/{project_id}/authority/compile")
async def compile_project_authority(
    project_id: int,
    req: AuthorityCompileApiRequest,
) -> dict[str, object]:
    """Compile pending authority for a created project/spec shell."""
    product = product_repo.get_by_id(project_id)
    if not product:
        raise HTTPException(status_code=404, detail="Project not found")

    result = _workbench_application().authority_compile(
        project_id=project_id,
        spec_version_id=req.spec_version_id,
        expected_spec_hash=req.expected_spec_hash,
        expected_state=req.expected_state,
        expected_setup_status=req.expected_setup_status,
        idempotency_key=req.idempotency_key,
        changed_by="dashboard-ui",
    )

    if not result.get("ok"):
        data = result.get("data") or {}
        errors = result.get("errors") or []
        warnings = result.get("warnings") or []
        return {
            "status": "error",
            "data": data,
            "errors": errors,
            "warnings": warnings,
        }

    return {
        "status": "success",
        "data": result.get("data") or {},
        "warnings": result.get("warnings") or [],
    }
```

- [ ] **Step 4: Verify API**

Run:

```bash
uv run --frozen pytest tests/test_api_dashboard.py -q -k "create_project or setup or authority_compile_api"
```

Expected: selected tests pass.

- [ ] **Step 5: Commit Task 7**

```bash
git add api.py tests/test_api_dashboard.py
git commit -m "feat(api): expose authority compile endpoint"
```

## Task 8: Dashboard Setup State UI

**Files:**
- Modify: `frontend/project.js`
- Test: `frontend/project.js`

- [ ] **Step 1: Inspect current setup status rendering**

Run:

```bash
rg -n "authority_pending_review|setup_status|setup failed|retry setup|Vision|Backlog" frontend/project.js
```

Expected: identify the existing rendering branch that handles setup failure and pending authority review.

- [ ] **Step 2: Add compile-required and compile-failed rendering**

In `frontend/project.js`, update the setup/dashboard rendering branch so:

- `authority_compile_required` shows title `Authority compile required`, text `Project and spec are saved. Compile pending authority before review.`, and a compile button.
- `authority_compiling` shows title `Authority compile running`, text `Authority compilation is in progress. Inspect the mutation before retrying.`, and mutation show/list guidance.
- `authority_compile_failed` shows title `Authority compile failed`, failure metadata, and a compile retry button.
- Later workflow controls remain hidden until `setup_status` is `authority_pending_review` or `passed`.

Use the `next_actions` from API/workflow payload when present. Do not synthesize a compile command without `project_id`, `spec_version_id`, `expected_spec_hash`, `expected_state`, and `expected_setup_status`.

- [ ] **Step 3: Syntax-check the frontend**

Run:

```bash
node --check frontend/project.js
```

Expected: no syntax errors.

- [ ] **Step 4: Commit Task 8**

```bash
git add frontend/project.js
git commit -m "feat(ui): show authority compile setup states"
```

## Task 9: CLI Integration And Full Setup Regression

**Files:**
- Modify: `tests/test_agent_workbench_project_create_cli_integration.py`
- Test: `tests/test_agent_workbench_project_create_cli_integration.py`
- Test: all modified focused suites

- [ ] **Step 1: Update project-create CLI integration expectations**

In `tests/test_agent_workbench_project_create_cli_integration.py`, update the happy-path create test to assert:

```python
assert payload["ok"] is True
data = payload["data"]
assert data["setup_status"] == "authority_compile_required"
assert data["fsm_state"] == "SETUP_REQUIRED"
assert "pending_authority_id" not in data
assert data["next_actions"][0]["command"] == "agileforge authority compile"
```

Add an integration test that runs `authority compile` after `project create` using the emitted guard fields:

```python
def test_project_create_then_authority_compile_cli_flow(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI setup flow should require explicit authority compile after create."""
    spec_file = _write_structured_spec(tmp_path)
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    app = AgentWorkbenchApplication(project_setup_runner=runner)
    _install_compiler(monkeypatch, success=True)

    create_rc = main(
        [
            "project",
            "create",
            "--name",
            "Split Setup CLI Project",
            "--spec-file",
            str(spec_file),
            "--idempotency-key",
            "split-setup-create-001",
        ],
        application=app,
    )
    create = _captured_payload(capsys)
    assert create_rc == 0
    create_data = create["data"]

    compile_rc = main(
        [
            "authority",
            "compile",
            "--project-id",
            str(create_data["project_id"]),
            "--spec-version-id",
            str(create_data["spec_version_id"]),
            "--expected-spec-hash",
            create_data["spec_hash"],
            "--expected-state",
            "SETUP_REQUIRED",
            "--expected-setup-status",
            "authority_compile_required",
            "--idempotency-key",
            "split-setup-compile-001",
        ],
        application=app,
    )
    compiled = _captured_payload(capsys)

    assert compile_rc == 0
    assert compiled["ok"] is True
    assert compiled["data"]["setup_status"] == "authority_pending_review"
    assert compiled["data"]["pending_authority_id"] is not None
```

- [ ] **Step 2: Run CLI integration test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_project_create_cli_integration.py -q -k "project_create or authority_compile"
```

Expected: selected tests pass.

- [ ] **Step 3: Run focused implementation suites**

Run:

```bash
uv run --frozen pytest tests/test_pending_authority_service.py -q -k "pending_authority or ensure_pending_spec_version"
uv run --frozen pytest tests/test_agent_workbench_project_setup.py -q -k "project_create or authority_compile or project_setup_retry"
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "authority_compile or workflow_next_routes_compile or workflow_next_routes_failed_setup or authority_pending_review"
uv run --frozen pytest tests/test_agent_workbench_cli.py -q -k "project_create or project_setup_retry or authority_compile"
uv run --frozen pytest tests/test_agent_workbench_command_schema.py -q -k "project_create or project_setup_retry or authority_compile or phase_2"
uv run --frozen pytest tests/test_api_dashboard.py -q -k "create_project or setup or authority_compile_api"
uv run --frozen pytest tests/test_agent_workbench_project_create_cli_integration.py -q -k "project_create or authority_compile"
node --check frontend/project.js
```

Expected: all focused checks pass.

- [ ] **Step 4: Run final repository gate**

Run:

```bash
pyrepo-check --all
```

Expected: pass.

- [ ] **Step 5: Commit final integration adjustments**

If Step 1 changed the integration test after Task 8, commit it:

```bash
git add tests/test_agent_workbench_project_create_cli_integration.py
git commit -m "test(setup): cover explicit create compile cli flow"
```

If `git status --short` is clean because previous tasks already included the integration changes, skip this commit and record that no integration-only commit was needed.

## Task 10: Feedback Doc And Issue Closure Evidence

**Files:**
- Modify: `docs/feedback/asa-milestone1-agileforge-feedback.md`

- [ ] **Step 1: Update feedback doc**

Append a dated note under the setup/bootstrap feedback section:

```markdown
### 2026-06-13: Project create / authority compile split accepted for #128

Decision: accepted and implemented as a hard split.

What changed:
- `agileforge project create` now persists project/spec metadata and returns `authority_compile_required`.
- Authority compilation is now the explicit guarded mutation `agileforge authority compile`.
- `workflow next` routes setup projects through compile-required, compiling, compile-failed, and pending-review states.
- Dashboard/API creation no longer hides a long-running authority compile operation.

Why:
- Project creation should be observable and fast.
- Compiler failure recovery belongs to the compiler command, not project metadata creation.
- Future scope-extension and brownfield setup workflows need the same spec-registration-to-compile boundary.

Out of scope:
- #129 brownfield setup.
- #130 authority compiler source-map/model repair.
- Product Goal / scope-extension workflow.
```

- [ ] **Step 2: Run doc whitespace check**

Run:

```bash
git diff --check
```

Expected: no whitespace errors.

- [ ] **Step 3: Commit docs**

```bash
git add docs/feedback/asa-milestone1-agileforge-feedback.md
git commit -m "docs(feedback): record setup compile split"
```

## Final Verification

- [ ] **Run all focused checks again**

```bash
uv run --frozen pytest tests/test_pending_authority_service.py -q -k "pending_authority or ensure_pending_spec_version"
uv run --frozen pytest tests/test_agent_workbench_project_setup.py -q -k "project_create or authority_compile or project_setup_retry"
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "authority_compile or workflow_next_routes_compile or workflow_next_routes_failed_setup or authority_pending_review"
uv run --frozen pytest tests/test_agent_workbench_cli.py -q -k "project_create or project_setup_retry or authority_compile"
uv run --frozen pytest tests/test_agent_workbench_command_schema.py -q -k "project_create or project_setup_retry or authority_compile or phase_2"
uv run --frozen pytest tests/test_api_dashboard.py -q -k "create_project or setup or authority_compile_api"
uv run --frozen pytest tests/test_agent_workbench_project_create_cli_integration.py -q -k "project_create or authority_compile"
node --check frontend/project.js
```

- [ ] **Run final gate**

```bash
pyrepo-check --all
```

- [ ] **Manual CLI smoke on a temporary spec**

Use a temporary test project name that does not collide with existing projects:

```bash
tmp_dir="$(mktemp -d -t agileforge-setup-split.XXXXXX)"
cp specs/spec.json "$tmp_dir/spec.json"
agileforge project create \
  --name "Setup Split Smoke $(date +%s)" \
  --spec-file "$tmp_dir/spec.json" \
  --idempotency-key "setup-split-smoke-create-$(date +%s)"
```

Expected:

- `ok=true`
- `data.setup_status="authority_compile_required"`
- `data.next_actions[0].command="agileforge authority compile"`
- no `pending_authority_id` in response data

Do not run the smoke against ASA project 3. This is a setup-flow product fix, not an ASA data mutation.
