# Project Create Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `agileforge project create` recover cleanly when the process dies or stalls after durable setup writes but before `pending_authority_compiled`.

**Architecture:** Keep the existing mutation-ledger design and `project setup retry` recovery flow, but close the current gaps at the long authority-compile boundary and retry entry. Authority compilation must keep the create lease alive while it blocks, `project setup retry` must repair expired pending create rows before validating recovery status, `mutation resume` must not trap project setup recovery in a pending owner state, and project deletion must not orphan unresolved create ledger rows.

**Tech Stack:** Python 3, SQLModel/SQLAlchemy, pytest, existing AgileForge mutation ledger and agent workbench services.

---

## File Structure

- Modify `services/specs/compiler_service.py`
  - Add a small heartbeat wrapper around `_compile_spec_authority_output` so long authority compiler calls refresh the active mutation lease.
  - Preserve existing compiler failure envelopes and cached-compile behavior.

- Modify `services/agent_workbench/mutation_ledger.py`
  - Add a public repair helper for setup recovery targets.
  - Repair expired pending rows to `RECOVERY_REQUIRED`.
  - Repair stale `mutation resume` handoff rows for project setup retry.

- Modify `services/agent_workbench/project_setup.py`
  - Call the repair helper from `_validate_original_recovery_row` before rejecting a recovery event.
  - Keep normal active pending create mutations blocked as `MUTATION_IN_PROGRESS`.

- Modify `api.py`
  - Refuse project deletion while unresolved project-create or setup-retry mutation rows exist for that project.
  - Return structured failure instead of deleting domain rows and leaving orphan ledger rows.

- Modify `services/agent_workbench/command_registry.py`
  - Add the delete error code if project delete is represented in the registry. If delete is not registered, leave registry unchanged.

- Modify `tests/test_agent_workbench_project_setup.py`
  - Add regression tests for expired pending create after `spec_marked_approved`.
  - Add regression tests for `mutation resume` followed by `project setup retry`.

- Modify `tests/test_agent_workbench_mutation_ledger.py`
  - Add focused repository tests for the new repair helper.

- Modify `tests/test_specs_compiler_service.py`
  - Add heartbeat coverage around the blocking compiler invocation.

- Modify `tests/test_api_dashboard.py`
  - Add API delete refusal coverage for unresolved setup mutations.

---

### Task 1: Ledger Repair Helper Tests

**Files:**
- Modify: `tests/test_agent_workbench_mutation_ledger.py`
- Modify later: `services/agent_workbench/mutation_ledger.py`

- [ ] **Step 1: Add failing tests for expired pending repair**

Append these tests near the existing stale pending tests in `tests/test_agent_workbench_mutation_ledger.py`:

```python
def test_repair_expired_pending_for_recovery_moves_row_to_recovery_required(
    engine: Engine,
) -> None:
    """Expired pending setup rows can be repaired at retry entry."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge project create",
        idempotency_key="create-project-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=1,
    ).ledger
    assert row.mutation_event_id is not None

    repaired = repo.repair_setup_recovery_target(
        mutation_event_id=row.mutation_event_id,
        expected_command="agileforge project create",
        expected_project_id=PROJECT_ID,
        now=now + timedelta(seconds=2),
    )

    assert repaired.error_code is None
    assert repaired.ledger.status == MutationStatus.RECOVERY_REQUIRED.value
    assert repaired.ledger.recovery_action == RecoveryAction.RECONCILE_THEN_RESUME.value
    assert repaired.ledger.recovery_safe_to_auto_resume is False
    assert repaired.ledger.lease_owner is None
    assert repaired.ledger.last_error_json is not None
    assert json.loads(repaired.ledger.last_error_json)["code"] == "STALE_PENDING"
```

Add the resume handoff case:

```python
def test_repair_setup_recovery_target_releases_resume_handoff_pending_row(
    engine: Engine,
) -> None:
    """A generic mutation-resume lease must not block project setup retry."""
    repo = _repo(engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    row = repo.create_or_load(
        command="agileforge project create",
        idempotency_key="create-project-001",
        request_hash="sha256:req",
        project_id=PROJECT_ID,
        correlation_id="corr-1",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=30,
    ).ledger
    assert row.mutation_event_id is not None
    repo._force_recovery_required_for_test(
        mutation_event_id=row.mutation_event_id,
        recovery_action=RecoveryAction.RESUME_FROM_STEP,
        safe_to_auto_resume=True,
        last_error={"code": "CRASHED"},
        now=now + timedelta(seconds=1),
    )
    resumed = repo.resume_event(
        mutation_event_id=row.mutation_event_id,
        correlation_id="corr-resume",
    )
    assert resumed["ok"] is True

    repaired = repo.repair_setup_recovery_target(
        mutation_event_id=row.mutation_event_id,
        expected_command="agileforge project create",
        expected_project_id=PROJECT_ID,
        now=now + timedelta(seconds=2),
    )

    assert repaired.error_code is None
    assert repaired.ledger.status == MutationStatus.RECOVERY_REQUIRED.value
    assert repaired.ledger.lease_owner is None
    assert repaired.ledger.last_error_json is not None
    assert json.loads(repaired.ledger.last_error_json)["code"] == (
        "RESUME_HANDOFF_TO_SETUP_RETRY"
    )
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_mutation_ledger.py \
  -k "repair_expired_pending_for_recovery or repair_setup_recovery_target" -q
```

Expected: fail with `AttributeError: 'MutationLedgerRepository' object has no attribute 'repair_setup_recovery_target'`.

- [ ] **Step 3: Implement `repair_setup_recovery_target`**

Add the method in `services/agent_workbench/mutation_ledger.py` after `_handle_pending_existing`:

```python
    def repair_setup_recovery_target(
        self,
        *,
        mutation_event_id: int,
        expected_command: str,
        expected_project_id: int,
        now: datetime,
    ) -> LedgerLoadResult:
        """Repair project-setup recovery target rows before setup retry validation."""
        db_now = _db_datetime(now)
        with Session(self._engine) as session:
            row = session.get(CliMutationLedger, mutation_event_id)
            if row is None:
                message = f"Mutation event {mutation_event_id} not found."
                raise ValueError(message)
            if row.command != expected_command or row.project_id != expected_project_id:
                return LedgerLoadResult(ledger=row, error_code=MUTATION_RESUME_CONFLICT)
            if row.status == MutationStatus.RECOVERY_REQUIRED.value:
                return LedgerLoadResult(ledger=row)
            if row.status != MutationStatus.PENDING.value:
                return LedgerLoadResult(ledger=row, error_code=MUTATION_RESUME_CONFLICT)

            resume_handoff = (
                row.lease_owner is not None
                and row.lease_owner.startswith("agileforge-cli:mutation-resume:")
            )
            expired = row.lease_expires_at is not None and row.lease_expires_at <= db_now
            if not expired and not resume_handoff:
                return LedgerLoadResult(ledger=row, error_code=MUTATION_IN_PROGRESS)

            last_error = (
                {
                    "code": "RESUME_HANDOFF_TO_SETUP_RETRY",
                    "message": "Mutation resume handed recovery back to project setup retry.",
                    "details": {"current_step": row.current_step},
                    "retryable": True,
                    "recorded_at": now.astimezone(UTC).isoformat(),
                }
                if resume_handoff
                else _stale_pending_error(row=row, now=now)
            )
            result = session.exec(
                update(CliMutationLedger)
                .where(_MUTATION_EVENT_ID == mutation_event_id)
                .where(_STATUS == MutationStatus.PENDING.value)
                .values(
                    status=MutationStatus.RECOVERY_REQUIRED.value,
                    recovery_action=RecoveryAction.RECONCILE_THEN_RESUME.value,
                    recovery_safe_to_auto_resume=False,
                    lease_owner=None,
                    lease_acquired_at=None,
                    last_heartbeat_at=None,
                    lease_expires_at=None,
                    last_error_json=_json_dump(last_error),
                    updated_at=db_now,
                )
            )
            session.commit()
            repaired = session.get(CliMutationLedger, mutation_event_id)
            if repaired is None:
                message = f"Mutation event {mutation_event_id} not found."
                raise ValueError(message)
            if result.rowcount != 1:
                return LedgerLoadResult(ledger=repaired, error_code=MUTATION_RESUME_CONFLICT)
            return LedgerLoadResult(ledger=repaired)
```

- [ ] **Step 4: Run the focused tests and verify they pass**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_mutation_ledger.py \
  -k "repair_expired_pending_for_recovery or repair_setup_recovery_target" -q
```

Expected: both tests pass.

- [ ] **Step 5: Commit**

```bash
git add services/agent_workbench/mutation_ledger.py tests/test_agent_workbench_mutation_ledger.py
git commit -m "fix: repair setup recovery ledger targets"
```

---

### Task 2: Setup Retry Accepts Repaired Expired Pending Create Rows

**Files:**
- Modify: `tests/test_agent_workbench_project_setup.py`
- Modify: `services/agent_workbench/project_setup.py`

- [ ] **Step 1: Add failing integration tests for retry entry**

Append this helper near the existing project setup test helpers:

```python
def _seed_expired_pending_create_after_spec_approval(
    *,
    engine: Engine,
    spec_file: Path,
    project_name: str = "Interrupted Project",
) -> tuple[int, int]:
    """Create the durable state left by a process death before pending authority."""
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    repo = MutationLedgerRepository(engine=engine)
    row = repo.create_or_load(
        command="agileforge project create",
        idempotency_key="create-interrupted-001",
        request_hash="sha256:create-interrupted",
        project_id=None,
        correlation_id="corr-create",
        changed_by="agent",
        lease_owner="create-owner",
        now=now,
        lease_seconds=1,
    ).ledger
    assert row.mutation_event_id is not None
    with Session(engine) as session:
        product = Product(name=project_name)
        session.add(product)
        session.flush()
        project_id = product.product_id
        assert project_id is not None
        assert MutationLedgerRepository.set_project_id_in_session(
            session,
            mutation_event_id=row.mutation_event_id,
            lease_owner="create-owner",
            project_id=project_id,
            now=now,
        )
        assert MutationLedgerRepository.mark_step_complete_in_session(
            session,
            mutation_event_id=row.mutation_event_id,
            lease_owner="create-owner",
            step="product_created",
            next_step="pending_authority_compiled",
            now=now,
        )
        product.spec_file_path = str(spec_file.resolve())
        product.spec_loaded_at = now
        session.add(product)
        assert MutationLedgerRepository.mark_step_complete_in_session(
            session,
            mutation_event_id=row.mutation_event_id,
            lease_owner="create-owner",
            step="product_spec_linked",
            next_step="product_spec_linked",
            now=now,
        )
        spec = SpecRegistry(
            product_id=project_id,
            spec_hash="sha256:seeded-spec",
            content=spec_file.read_text(encoding="utf-8"),
            content_ref=str(spec_file.resolve()),
            status="approved",
            approved_at=now,
            approved_by="cli-project-create",
            approval_notes="Required compiler precondition for pending authority generation",
        )
        session.add(spec)
        session.flush()
        assert MutationLedgerRepository.mark_step_complete_in_session(
            session,
            mutation_event_id=row.mutation_event_id,
            lease_owner="create-owner",
            step="spec_registry_written",
            next_step="spec_registry_written",
            now=now,
        )
        assert MutationLedgerRepository.mark_step_complete_in_session(
            session,
            mutation_event_id=row.mutation_event_id,
            lease_owner="create-owner",
            step="spec_marked_approved",
            next_step="spec_marked_approved",
            now=now,
        )
        ledger = session.get(CliMutationLedger, row.mutation_event_id)
        assert ledger is not None
        ledger.lease_expires_at = now.replace(second=1)
        session.add(ledger)
        session.commit()
    return project_id, row.mutation_event_id
```

Append the regression test:

```python
def test_project_setup_retry_repairs_expired_pending_create_recovery_event(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_fast_compiler(monkeypatch)
    workflow = FakeWorkflowPort()
    project_id, original_event_id = _seed_expired_pending_create_after_spec_approval(
        engine=engine,
        spec_file=spec_file,
    )
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    expected_fingerprint = _retry_fingerprint(
        project_id=project_id,
        spec_file=spec_file,
        workflow_state={},
    )

    retried = runner.retry_setup(
        ProjectSetupRetryRequest(
            project_id=project_id,
            spec_file=str(spec_file),
            expected_state="SETUP_REQUIRED",
            expected_context_fingerprint=expected_fingerprint,
            recovery_mutation_event_id=original_event_id,
            idempotency_key="retry-interrupted-001",
            changed_by="agent",
        )
    )

    assert retried["ok"] is True
    assert retried["data"]["project_id"] == project_id
    assert retried["data"]["recovery_mutation_event_id"] == original_event_id
    with Session(engine) as session:
        original = session.get(CliMutationLedger, original_event_id)
        retry = session.get(CliMutationLedger, retried["data"]["mutation_event_id"])
        assert original is not None
        assert retry is not None
        assert original.status == MutationStatus.SUPERSEDED.value
        assert retry.status == MutationStatus.SUCCEEDED.value
        assert session.exec(select(CompiledSpecAuthority)).first() is not None
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_project_setup.py \
  -k "repairs_expired_pending_create_recovery_event" -q
```

Expected: fail with `MUTATION_RECOVERY_INVALID` or `MUTATION_IN_PROGRESS`.

- [ ] **Step 3: Call the repair helper from `_validate_original_recovery_row`**

In `services/agent_workbench/project_setup.py`, replace the recovery-row validation body after `row = self._get_ledger(...)` with:

```python
        if row is None or row.command != PROJECT_CREATE_COMMAND or row.project_id != request.project_id:
            return _error(
                MUTATION_RECOVERY_INVALID,
                details={
                    "project_id": request.project_id,
                    "recovery_mutation_event_id": request.recovery_mutation_event_id,
                },
                remediation=["Re-read mutation state before retrying recovery."],
            )
        repaired = self._ledger.repair_setup_recovery_target(
            mutation_event_id=request.recovery_mutation_event_id,
            expected_command=PROJECT_CREATE_COMMAND,
            expected_project_id=request.project_id,
            now=_now(),
        )
        if repaired.error_code == MUTATION_IN_PROGRESS:
            return _error_for_ledger(MUTATION_IN_PROGRESS, repaired.ledger)
        if repaired.error_code is not None:
            return _error(
                MUTATION_RECOVERY_INVALID,
                details={
                    "project_id": request.project_id,
                    "recovery_mutation_event_id": request.recovery_mutation_event_id,
                },
                remediation=["Re-read mutation state before retrying recovery."],
            )
        if repaired.ledger.status != MutationStatus.RECOVERY_REQUIRED.value:
            return _error(
                MUTATION_RECOVERY_INVALID,
                details={
                    "project_id": request.project_id,
                    "recovery_mutation_event_id": request.recovery_mutation_event_id,
                },
                remediation=["Re-read mutation state before retrying recovery."],
            )
        return repaired.ledger
```

- [ ] **Step 4: Run the focused test and existing retry tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_project_setup.py \
  -k "project_setup_retry or linked_setup_retry or retry_without_recovery_link" -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add services/agent_workbench/project_setup.py tests/test_agent_workbench_project_setup.py
git commit -m "fix: allow setup retry to repair interrupted create"
```

---

### Task 3: Compiler Lease Heartbeat During Blocking Authority Invocation

**Files:**
- Modify: `tests/test_specs_compiler_service.py`
- Modify: `services/specs/compiler_service.py`

- [ ] **Step 1: Add a failing heartbeat test**

Append this focused helper test near other compiler service tests:

```python
def test_run_with_compiler_heartbeat_refreshes_during_blocking_call() -> None:
    """The authority compiler invocation keeps the mutation lease fresh."""
    from services.specs import compiler_service

    heartbeat_calls: list[str] = []

    def blocking_invoke() -> dict[str, object]:
        time.sleep(0.05)
        return {"success": True, "compiled": True}

    def lease_guard(boundary: str) -> bool:
        heartbeat_calls.append(boundary)
        return True

    result = compiler_service._run_with_compiler_heartbeat(
        lease_guard=lease_guard,
        heartbeat_interval_seconds=0.01,
        invoke=blocking_invoke,
    )

    assert result == {"success": True, "compiled": True}
    assert "authority_compile_invocation_started" in heartbeat_calls
    assert "authority_compile_invocation_heartbeat" in heartbeat_calls
    assert "authority_compile_invocation_finished" in heartbeat_calls
```

Add imports at the top of the test file if missing:

```python
import time
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py \
  -k "heartbeats_during_blocking_call" -q
```

Expected: fail because `_run_with_compiler_heartbeat` does not exist yet.

- [ ] **Step 3: Implement the heartbeat wrapper**

In `services/specs/compiler_service.py`, add imports:

```python
from threading import Event, Thread
```

Add this helper near `_mutation_lease_lost_result`:

```python
def _mutation_lease_lost_result(boundary: str | None = None) -> dict[str, Any]:
    """Return the canonical mutation lease-loss envelope."""
    result: dict[str, Any] = {
        "success": False,
        "error": "MUTATION_LEASE_LOST",
        "error_code": "MUTATION_IN_PROGRESS",
    }
    if boundary is not None:
        result["boundary"] = boundary
    return result


def _run_with_compiler_heartbeat(
    *,
    lease_guard: Callable[[str], bool] | None,
    heartbeat_interval_seconds: float,
    invoke: Callable[[], SpecAuthorityCompilationSuccess | dict[str, Any]],
) -> SpecAuthorityCompilationSuccess | dict[str, Any]:
    """Run the blocking compiler call while refreshing the mutation lease."""
    if lease_guard is None:
        return invoke()
    if not lease_guard("authority_compile_invocation_started"):
        return _mutation_lease_lost_result("authority_compile_invocation_started")

    stop = Event()
    lost_boundary: dict[str, str | None] = {"value": None}

    def heartbeat_loop() -> None:
        while not stop.wait(heartbeat_interval_seconds):
            boundary = "authority_compile_invocation_heartbeat"
            try:
                if not lease_guard(boundary):
                    lost_boundary["value"] = boundary
                    stop.set()
                    return
            except Exception:  # noqa: BLE001
                lost_boundary["value"] = boundary
                stop.set()
                return

    thread = Thread(
        target=heartbeat_loop,
        name="agileforge-authority-compile-heartbeat",
        daemon=True,
    )
    thread.start()
    try:
        result = invoke()
    finally:
        stop.set()
        thread.join(timeout=1.0)

    if lost_boundary["value"] is not None:
        return _mutation_lease_lost_result(lost_boundary["value"])
    if not lease_guard("authority_compile_invocation_finished"):
        return _mutation_lease_lost_result("authority_compile_invocation_finished")
    return result
```

Update `_invoke_compiler_for_version` signature and body:

```python
def _invoke_compiler_for_version(
    spec_version: SpecRegistry,
    *,
    spec_content: str,
    lease_guard: Callable[[str], bool] | None = None,
    heartbeat_interval_seconds: float = 60.0,
) -> SpecAuthorityCompilationSuccess | dict[str, Any]:
    """Invoke the compiler and normalize either a success artifact or failure result."""

    def invoke() -> SpecAuthorityCompilationSuccess | dict[str, Any]:
        try:
            compiled = _compile_spec_authority_output(
                spec_content=spec_content,
                content_ref=spec_version.content_ref,
                product_id=spec_version.product_id,
                spec_version_id=spec_version.spec_version_id,
            )
        except AgentInvocationError as exc:
            return _compiler_failure_result(
                product_id=spec_version.product_id,
                spec_version_id=spec_version.spec_version_id,
                content_ref=spec_version.content_ref,
                failure_stage="invocation_exception",
                error="SPEC_COMPILER_INVOCATION_FAILED",
                reason=str(exc),
                raw_output=exc.partial_output,
                exception=exc,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            return _compiler_failure_result(
                product_id=spec_version.product_id,
                spec_version_id=spec_version.spec_version_id,
                content_ref=spec_version.content_ref,
                failure_stage="invocation_exception",
                error="SPEC_COMPILER_INVOCATION_FAILED",
                reason=str(exc),
                exception=exc,
            )
        raw_json = compiled.raw_json
        normalized = compiled.output
        if isinstance(normalized.root, SpecAuthorityCompilationFailure):
            failure_stage = (
                "invalid_json"
                if normalized.root.reason == "INVALID_JSON"
                else "output_validation"
            )
            return _compiler_failure_result(
                product_id=spec_version.product_id,
                spec_version_id=spec_version.spec_version_id,
                content_ref=spec_version.content_ref,
                failure_stage=failure_stage,
                error=normalized.root.error,
                reason=normalized.root.reason,
                raw_output=raw_json,
                blocking_gaps=normalized.root.blocking_gaps,
            )
        return normalized.root

    return _run_with_compiler_heartbeat(
        lease_guard=lease_guard,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        invoke=invoke,
    )
```

Update the call site in `_compile_spec_authority_for_version_in_session`:

```python
    compiled = _invoke_compiler_for_version(
        context.spec_version,
        spec_content=spec_content,
        lease_guard=lease_guard,
    )
```

- [ ] **Step 4: Run heartbeat and compiler tests**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py \
  -k "heartbeats_during_blocking_call or compile_spec_authority" -q
```

Expected: selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add services/specs/compiler_service.py tests/test_specs_compiler_service.py
git commit -m "fix: heartbeat authority compile leases"
```

---

### Task 4: Project Delete Refuses Unresolved Setup Mutations

**Files:**
- Modify: `tests/test_api_dashboard.py`
- Modify: `api.py`

- [ ] **Step 1: Add failing API test**

Append this test near dashboard project delete tests:

```python
def test_delete_project_refuses_unresolved_setup_mutation(
    client: TestClient,
    engine: Engine,
) -> None:
    """Do not delete project rows while setup recovery still owns ledger state."""
    product = _create_product(engine, name="Interrupted Project")
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    MutationLedgerRepository(engine=engine).create_or_load(
        command="agileforge project create",
        idempotency_key="create-interrupted-001",
        request_hash="sha256:req",
        project_id=product.product_id,
        correlation_id="corr",
        changed_by="cli-agent",
        lease_owner="worker-1",
        now=now,
        lease_seconds=1,
    )

    response = client.delete(f"/api/projects/{product.product_id}")

    assert response.status_code == 409
    payload = response.json()
    assert payload["detail"]["errors"][0]["code"] == "PROJECT_DELETE_BLOCKED_BY_SETUP_RECOVERY"
    with Session(engine) as session:
        assert session.get(Product, product.product_id) is not None
```

If `_create_product` or `engine` fixtures are named differently in this file, use the existing helper style in `tests/test_api_dashboard.py`.

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
uv run --frozen pytest tests/test_api_dashboard.py \
  -k "delete_project_refuses_unresolved_setup_mutation" -q
```

Expected: fail because delete succeeds or returns a non-409 error.

- [ ] **Step 3: Implement delete guard**

In `api.py`, add a helper near `delete_project`:

```python
def _unresolved_setup_mutation_for_project(project_id: int) -> CliMutationLedger | None:
    unresolved_statuses = {
        MutationStatus.PENDING.value,
        MutationStatus.RECOVERY_REQUIRED.value,
    }
    setup_commands = {
        "agileforge project create",
        "agileforge project setup retry",
    }
    with Session(get_engine()) as session:
        return session.exec(
            select(CliMutationLedger)
            .where(CliMutationLedger.project_id == project_id)
            .where(CliMutationLedger.command.in_(setup_commands))
            .where(CliMutationLedger.status.in_(unresolved_statuses))
            .order_by(CliMutationLedger.mutation_event_id.desc())
        ).first()
```

Then add this guard after the product existence check in `delete_project`:

```python
    unresolved = _unresolved_setup_mutation_for_project(project_id)
    if unresolved is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "status": "error",
                "message": "Project has unresolved setup recovery state.",
                "errors": [
                    {
                        "code": "PROJECT_DELETE_BLOCKED_BY_SETUP_RECOVERY",
                        "message": "Resolve or abandon setup recovery before deleting this project.",
                        "details": {
                            "project_id": project_id,
                            "mutation_event_id": unresolved.mutation_event_id,
                            "mutation_status": unresolved.status,
                        },
                        "retryable": False,
                    }
                ],
            },
        )
```

- [ ] **Step 4: Run the focused test**

Run:

```bash
uv run --frozen pytest tests/test_api_dashboard.py \
  -k "delete_project_refuses_unresolved_setup_mutation" -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add api.py tests/test_api_dashboard.py
git commit -m "fix: block delete during setup recovery"
```

---

### Task 5: CLI/API Recovery Smoke Tests

**Files:**
- Modify: `tests/test_agent_workbench_project_setup.py`
- Modify: `tests/test_agent_workbench_cli.py`
- Modify: `tests/test_api_dashboard.py`

- [ ] **Step 1: Add CLI behavior test for expired pending retry**

In `tests/test_agent_workbench_cli.py`, add a facade-level test that verifies CLI argument routing accepts `--recovery-mutation-event-id` and does not require the old create replay path:

```python
def test_cli_routes_setup_retry_with_recovery_mutation_event_id() -> None:
    app = FakeApplication()
    payload = _run_cli_json(
        [
            "project",
            "setup",
            "retry",
            "--project-id",
            "7",
            "--spec-file",
            "specs/spec.json",
            "--expected-state",
            "SETUP_REQUIRED",
            "--expected-context-fingerprint",
            "sha256:" + "a" * 64,
            "--recovery-mutation-event-id",
            "42",
            "--idempotency-key",
            "retry-project-001",
        ],
        application=app,
    )

    assert payload["ok"] is True
    assert app.calls[-1][0] == "project_setup_retry"
    assert app.calls[-1][1]["recovery_mutation_event_id"] == 42
```

Use the actual helper names already present in `tests/test_agent_workbench_cli.py`; do not create duplicate runner helpers.

- [ ] **Step 2: Run CLI tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_cli.py \
  -k "setup_retry_with_recovery_mutation_event_id or project_setup_retry" -q
```

Expected: pass.

- [ ] **Step 3: Run setup recovery regression set**

Run:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_mutation_ledger.py \
  tests/test_agent_workbench_project_setup.py \
  tests/test_specs_compiler_service.py \
  tests/test_api_dashboard.py \
  tests/test_agent_workbench_cli.py \
  -k "setup or recovery or pending or heartbeat or delete_project_refuses" -q
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_agent_workbench_cli.py
git commit -m "test: cover setup retry recovery CLI routing"
```

If Step 1 required no code changes because coverage already exists, skip this commit and record that in the task note before moving on.

---

### Task 6: Final Verification

**Files:**
- No source edits unless a verification failure identifies a bug in this slice.

- [ ] **Step 1: Run full repository verification**

Run:

```bash
uv run --frozen pyrepo-check --all
```

Expected: pass.

- [ ] **Step 2: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 3: Inspect final diff**

Run:

```bash
git diff --stat HEAD~5..HEAD
git status --short
```

Expected:
- Only files listed in this plan are changed by the branch.
- Worktree is clean after commits, or only intentional untracked local artifacts remain.

- [ ] **Step 4: Do not run live `project create` against the user's real DB**

Do not run mutating live CLI commands as part of this implementation. If a manual smoke is required later, run it against a cloned SQLite DB or a fresh throwaway DB with `AGILEFORGE_CONFIG_ROOT` and `AGILEFORGE_DB_URL` explicitly set.

---

## Self-Review

**Spec coverage:**
- Crash/hang after `spec_marked_approved` before `pending_authority_compiled`: covered by Task 2.
- Lease expiry during long compiler call: covered by Task 3.
- Retry entry repairs expired pending rows: covered by Tasks 1 and 2.
- `mutation resume` no longer blocks setup retry: covered by Task 1 and Task 2 through the repair helper.
- Re-running create still idempotent: existing tests remain in `tests/test_agent_workbench_project_setup.py`; Task 2 runs the relevant set.
- Delete orphan risk: covered by Task 4.

**Chosen delete policy:** normal project delete refuses unresolved setup recovery. This is intentionally safer than silently tombstoning ledger rows because the setup mutation has already written durable domain state.

**Known non-goal:** This plan does not add a separate `project setup abandon` command. If operators need to intentionally discard unrecoverable half-created projects, write a separate spec and command for that path.
