# Project Create Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `agileforge project create` survive long authority compilation and report recovery state truthfully when setup is interrupted.

**Architecture:** Layer 1 only. Add a heartbeat and hard deadline around the pure authority compiler invocation, repair expired pending create rows at setup-retry entry, make retry dry-runs validate the same recovery rules as real retry, and make `mutation resume` hand project-create recovery back to `project setup retry` without creating a blocking pending lease.

**Tech Stack:** Python 3, SQLModel/SQLAlchemy, pytest, existing AgileForge mutation ledger, project setup runner, and spec compiler services.

---

## Fixed Contracts

- Authority compile heartbeat interval: `60.0` seconds.
- Mutation lease extension: existing `DEFAULT_LEASE_SECONDS`, currently `300` seconds.
- Authority compile hard deadline: `1800.0` seconds.
- Expiry rule everywhere in this work: a lease is expired when `lease_expires_at <= now` after converting `now` through `_db_datetime(now)`.
- Timeout behavior: return `SPEC_COMPILE_FAILED` through project setup, with compiler `failure_stage="invocation_timeout"`, setup row status `validation_failed`, workflow state still `SETUP_REQUIRED`, no `CompiledSpecAuthority`, and next action `agileforge project setup retry` without `recovery_mutation_event_id`.
- Lease-lost behavior: return recovery only when the stored create row is actually `recovery_required`; otherwise return the stored ledger conflict and remediation without claiming recovery happened.
- `mutation resume` handoff rule: for `agileforge project create` recovery rows, do not acquire a generic pending resume lease. Leave the row `recovery_required` and return a success envelope pointing the caller to `agileforge project setup retry`.
- Dry-run rule: `project setup retry --dry-run` may only repair an expired pending create row to `recovery_required`. It must not write product, spec, compiled authority, authority acceptance, or workflow setup rows.

## File Structure

- Modify `services/specs/compiler_service.py`
  - Add authority compile timing constants.
  - Add a daemon-thread compiler invocation guard with heartbeat and timeout.
  - Pass `lease_guard` through `_invoke_compiler_for_version`.

- Modify `services/agent_workbench/mutation_ledger.py`
  - Add `repair_setup_recovery_target`.
  - Add the project-create `mutation resume` handoff branch.
  - Keep generic `mutation resume` behavior unchanged for non-project-create commands.

- Modify `services/agent_workbench/project_setup.py`
  - Validate and repair original recovery rows before retry dry-run returns.
  - Use the boolean from `_mark_create_recovery_required`.
  - Return truthful state when recovery marking fails.

- Modify `tests/test_specs_compiler_service.py`
  - Add heartbeat, timeout, and long-compile regression tests without live model calls.

- Modify `tests/test_agent_workbench_mutation_ledger.py`
  - Add expired-pending repair, active-pending rejection, and mutation-resume handoff tests.

- Modify `tests/test_agent_workbench_project_setup.py`
  - Add setup-retry repair, retry dry-run parity, timeout behavior, recovery-mark-failure, and event-159 regression tests.
  - Update the existing linked retry dry-run test to use a valid context fingerprint now that dry-run validates guards.

## Task 1: Compiler Heartbeat And Deadline

**Files:**
- Modify: `tests/test_specs_compiler_service.py`
- Modify: `services/specs/compiler_service.py`

- [ ] **Step 1: Write failing tests for heartbeat and timeout**

Add `import time` at the top of `tests/test_specs_compiler_service.py`, then append these tests near the other compiler-service unit tests:

```python
def test_compiler_invocation_guard_heartbeats_until_blocking_call_finishes() -> None:
    from services.specs import compiler_service

    calls: list[str] = []
    result_value = object()

    def invoke() -> object:
        time.sleep(0.03)
        return result_value

    def lease_guard(boundary: str) -> bool:
        calls.append(boundary)
        return True

    result = compiler_service._run_compiler_invocation_with_guards(
        invoke=invoke,
        lease_guard=lease_guard,
        heartbeat_interval_seconds=0.005,
        timeout_seconds=1.0,
        timeout_result=lambda: {"success": False, "error": "timeout"},
    )

    assert result is result_value
    assert calls[0] == "authority_compile_invocation_started"
    assert "authority_compile_invocation_heartbeat" in calls
    assert calls[-1] == "authority_compile_invocation_finished"


def test_compiler_invocation_guard_returns_timeout_without_finish_guard() -> None:
    from services.specs import compiler_service

    calls: list[str] = []

    def invoke() -> object:
        time.sleep(0.05)
        return object()

    result = compiler_service._run_compiler_invocation_with_guards(
        invoke=invoke,
        lease_guard=lambda boundary: calls.append(boundary) or True,
        heartbeat_interval_seconds=0.005,
        timeout_seconds=0.01,
        timeout_result=lambda: {
            "success": False,
            "error": "SPEC_COMPILER_INVOCATION_TIMEOUT",
            "failure_stage": "invocation_timeout",
        },
    )

    assert result == {
        "success": False,
        "error": "SPEC_COMPILER_INVOCATION_TIMEOUT",
        "failure_stage": "invocation_timeout",
    }
    assert "authority_compile_invocation_started" in calls
    assert "authority_compile_invocation_finished" not in calls


def test_compiler_invocation_guard_returns_lease_loss_when_heartbeat_fails() -> None:
    from services.specs import compiler_service

    calls: list[str] = []

    def invoke() -> object:
        time.sleep(0.05)
        return object()

    def lease_guard(boundary: str) -> bool:
        calls.append(boundary)
        return boundary != "authority_compile_invocation_heartbeat"

    result = compiler_service._run_compiler_invocation_with_guards(
        invoke=invoke,
        lease_guard=lease_guard,
        heartbeat_interval_seconds=0.005,
        timeout_seconds=1.0,
        timeout_result=lambda: {"success": False, "error": "timeout"},
    )

    assert result == {
        "success": False,
        "error": "MUTATION_LEASE_LOST",
        "error_code": "MUTATION_IN_PROGRESS",
        "boundary": "authority_compile_invocation_heartbeat",
    }
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py \
  -k "compiler_invocation_guard" -q
```

Expected: fail because `_run_compiler_invocation_with_guards` does not exist.

- [ ] **Step 3: Implement compiler timing constants and guard helper**

In `services/specs/compiler_service.py`, add imports:

```python
import time
from threading import Event, Thread
```

Add constants after `_FOCUSED_ITEM_COMPILER_ATTEMPTS`:

```python
DEFAULT_AUTHORITY_COMPILE_HEARTBEAT_SECONDS: float = 60.0
DEFAULT_AUTHORITY_COMPILE_TIMEOUT_SECONDS: float = 1800.0
```

Replace `_mutation_lease_lost_result` with this compatible version:

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
```

Add the guarded invocation helper below `_mutation_progress_failed_result`:

```python
def _run_compiler_invocation_with_guards(
    *,
    invoke: Callable[[], Any],
    lease_guard: Callable[[str], bool] | None,
    heartbeat_interval_seconds: float,
    timeout_seconds: float,
    timeout_result: Callable[[], dict[str, Any]],
) -> Any:
    """Run the pure compiler invocation with lease heartbeats and a deadline."""
    if timeout_seconds <= 0:
        return timeout_result()
    if lease_guard is not None and not lease_guard("authority_compile_invocation_started"):
        return _mutation_lease_lost_result("authority_compile_invocation_started")

    done = Event()
    stop = Event()
    result_box: dict[str, Any] = {}
    lost_boundary: dict[str, str | None] = {"value": None}

    def worker() -> None:
        try:
            result_box["result"] = invoke()
        except BaseException as exc:  # noqa: BLE001
            result_box["exception"] = exc
        finally:
            done.set()

    def heartbeat_loop() -> None:
        while not stop.wait(heartbeat_interval_seconds):
            if done.is_set() or lease_guard is None:
                return
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

    worker_thread = Thread(
        target=worker,
        name="agileforge-authority-compile",
        daemon=True,
    )
    heartbeat_thread = Thread(
        target=heartbeat_loop,
        name="agileforge-authority-compile-heartbeat",
        daemon=True,
    )
    worker_thread.start()
    heartbeat_thread.start()

    deadline = time.monotonic() + timeout_seconds
    while not done.is_set():
        if lost_boundary["value"] is not None:
            stop.set()
            heartbeat_thread.join(timeout=1.0)
            return _mutation_lease_lost_result(lost_boundary["value"])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stop.set()
            heartbeat_thread.join(timeout=1.0)
            return timeout_result()
        done.wait(timeout=min(0.1, remaining))

    stop.set()
    heartbeat_thread.join(timeout=1.0)
    if lost_boundary["value"] is not None:
        return _mutation_lease_lost_result(lost_boundary["value"])
    if "exception" in result_box:
        raise result_box["exception"]
    if lease_guard is not None and not lease_guard("authority_compile_invocation_finished"):
        return _mutation_lease_lost_result("authority_compile_invocation_finished")
    return result_box["result"]
```

- [ ] **Step 4: Wire the guard into `_invoke_compiler_for_version`**

Change `_invoke_compiler_for_version` to accept guard settings and use the helper around `_compile_spec_authority_output`:

```python
def _invoke_compiler_for_version(
    spec_version: SpecRegistry,
    *,
    spec_content: str,
    lease_guard: Callable[[str], bool] | None = None,
    heartbeat_interval_seconds: float = DEFAULT_AUTHORITY_COMPILE_HEARTBEAT_SECONDS,
    timeout_seconds: float = DEFAULT_AUTHORITY_COMPILE_TIMEOUT_SECONDS,
) -> SpecAuthorityCompilationSuccess | dict[str, Any]:
    """Invoke the compiler and normalize either a success artifact or failure result."""

    def invoke() -> _NormalizedCompilerInvocation:
        return _compile_spec_authority_output(
            spec_content=spec_content,
            content_ref=spec_version.content_ref,
            product_id=spec_version.product_id,
            spec_version_id=spec_version.spec_version_id,
        )

    def timeout_result() -> dict[str, Any]:
        return _compiler_failure_result(
            product_id=spec_version.product_id,
            spec_version_id=spec_version.spec_version_id,
            content_ref=spec_version.content_ref,
            failure_stage="invocation_timeout",
            error="SPEC_COMPILER_INVOCATION_TIMEOUT",
            reason=(
                "Spec authority compiler exceeded "
                f"{timeout_seconds:.0f} seconds."
            ),
        )

    try:
        compiled_or_error = _run_compiler_invocation_with_guards(
            invoke=invoke,
            lease_guard=lease_guard,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            timeout_seconds=timeout_seconds,
            timeout_result=timeout_result,
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

    if isinstance(compiled_or_error, dict):
        return compiled_or_error
    compiled = cast("_NormalizedCompilerInvocation", compiled_or_error)
```

Keep the existing normalization logic after `compiled = ...` unchanged.

In `_compile_spec_authority_for_version_in_session`, change the invocation call to:

```python
    compiled = _invoke_compiler_for_version(
        context.spec_version,
        spec_content=spec_content,
        lease_guard=lease_guard,
    )
```

- [ ] **Step 5: Run focused compiler tests**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py \
  -k "compiler_invocation_guard or invocation_failure_writes_failure_artifact" -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add services/specs/compiler_service.py tests/test_specs_compiler_service.py
git commit -m "fix: guard authority compiler invocation"
```

## Task 2: Ledger Repair And Mutation Resume Handoff

**Files:**
- Modify: `tests/test_agent_workbench_mutation_ledger.py`
- Modify: `services/agent_workbench/mutation_ledger.py`

- [ ] **Step 1: Write failing ledger repair and handoff tests**

Append these tests near the existing stale-pending and resume tests in `tests/test_agent_workbench_mutation_ledger.py`:

```python
def test_repair_setup_recovery_target_repairs_expired_pending_create(
    engine: Engine,
) -> None:
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
        now=now + timedelta(seconds=1),
    )

    assert repaired.error_code is None
    assert repaired.ledger.status == MutationStatus.RECOVERY_REQUIRED.value
    assert repaired.ledger.recovery_action == RecoveryAction.RECONCILE_THEN_RESUME.value
    assert repaired.ledger.recovery_safe_to_auto_resume is False
    assert repaired.ledger.lease_owner is None
    assert repaired.ledger.lease_expires_at is None
    assert repaired.ledger.last_error_json is not None
    assert json.loads(repaired.ledger.last_error_json)["code"] == "STALE_PENDING"


def test_repair_setup_recovery_target_rejects_active_pending_create(
    engine: Engine,
) -> None:
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
        lease_seconds=300,
    ).ledger
    assert row.mutation_event_id is not None

    result = repo.repair_setup_recovery_target(
        mutation_event_id=row.mutation_event_id,
        expected_command="agileforge project create",
        expected_project_id=PROJECT_ID,
        now=now,
    )

    assert result.error_code == MUTATION_IN_PROGRESS
    assert result.ledger.status == MutationStatus.PENDING.value


def test_mutation_resume_hands_project_create_recovery_to_setup_retry(
    engine: Engine,
) -> None:
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
    repo._force_recovery_required_for_test(
        mutation_event_id=row.mutation_event_id,
        recovery_action=RecoveryAction.RESUME_FROM_STEP,
        safe_to_auto_resume=True,
        last_error={"code": "MUTATION_RECOVERY_REQUIRED"},
        now=now + timedelta(seconds=2),
    )

    result = repo.resume_event(
        mutation_event_id=row.mutation_event_id,
        correlation_id="corr-resume",
    )

    assert result["ok"] is True
    assert result["data"]["status"] == MutationStatus.RECOVERY_REQUIRED.value
    assert result["data"]["lease_owner"] is None
    assert result["data"]["recovery"] == {
        "acquired": False,
        "domain_resume_required": False,
        "handoff_command": "agileforge project setup retry",
        "reason": "Project creation recovery is resumed by setup retry.",
    }
    with Session(engine) as session:
        stored = session.get(CliMutationLedger, row.mutation_event_id)
    assert stored is not None
    assert stored.status == MutationStatus.RECOVERY_REQUIRED.value
    assert stored.lease_owner is None
```

- [ ] **Step 2: Run focused tests and verify they fail**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_mutation_ledger.py \
  -k "repair_setup_recovery_target or hands_project_create_recovery" -q
```

Expected: fail because `repair_setup_recovery_target` and the handoff branch do not exist.

- [ ] **Step 3: Add `repair_setup_recovery_target`**

In `services/agent_workbench/mutation_ledger.py`, add this method after `_handle_pending_existing`:

```python
    def repair_setup_recovery_target(
        self,
        *,
        mutation_event_id: int,
        expected_command: str,
        expected_project_id: int,
        now: datetime,
    ) -> LedgerLoadResult:
        """Repair a project setup recovery target before setup retry validation."""
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
            if row.lease_expires_at is None or row.lease_expires_at > db_now:
                return LedgerLoadResult(ledger=row, error_code=MUTATION_IN_PROGRESS)

            result = session.exec(
                update(CliMutationLedger)
                .where(_MUTATION_EVENT_ID == mutation_event_id)
                .where(_STATUS == MutationStatus.PENDING.value)
                .where(_LEASE_EXPIRES_AT <= db_now)
                .values(
                    status=MutationStatus.RECOVERY_REQUIRED.value,
                    recovery_action=RecoveryAction.RECONCILE_THEN_RESUME.value,
                    recovery_safe_to_auto_resume=False,
                    lease_owner=None,
                    lease_acquired_at=None,
                    last_heartbeat_at=None,
                    lease_expires_at=None,
                    last_error_json=_json_dump(_stale_pending_error(row=row, now=now)),
                    updated_at=db_now,
                )
            )
            session.commit()
            repaired = session.get(CliMutationLedger, mutation_event_id)
            if repaired is None:
                message = f"Mutation event {mutation_event_id} not found."
                raise ValueError(message)
            if result.rowcount != 1:
                return LedgerLoadResult(
                    ledger=repaired,
                    error_code=MUTATION_RESUME_CONFLICT,
                )
            return LedgerLoadResult(ledger=repaired)
```

- [ ] **Step 4: Add the project-create resume handoff branch**

In `resume_event`, after `_repair_expired_pending_resume(...)` and before `acquire_resume_lease(...)`, load the row and return the handoff when it is a project-create recovery row:

```python
        with Session(self._engine) as session:
            row = session.get(CliMutationLedger, mutation_event_id)
            if row is None:
                return _error_result(
                    code=MUTATION_NOT_FOUND,
                    details={"mutation_event_id": mutation_event_id},
                    remediation=["Re-read mutation state before retrying recovery."],
                )
            if (
                row.command == "agileforge project create"
                and row.status == MutationStatus.RECOVERY_REQUIRED.value
            ):
                data = _row_payload(row)
                data["recovery"] = {
                    "acquired": False,
                    "domain_resume_required": False,
                    "handoff_command": "agileforge project setup retry",
                    "reason": "Project creation recovery is resumed by setup retry.",
                }
                return _success_result(data)
```

- [ ] **Step 5: Run ledger tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_mutation_ledger.py \
  -k "repair_setup_recovery_target or resume_event" -q
```

Expected: all selected tests pass, including the existing generic resume tests.

- [ ] **Step 6: Commit**

```bash
git add services/agent_workbench/mutation_ledger.py tests/test_agent_workbench_mutation_ledger.py
git commit -m "fix: repair setup recovery ledger rows"
```

## Task 3: Setup Retry Repair And Dry-Run Parity

**Files:**
- Modify: `tests/test_agent_workbench_project_setup.py`
- Modify: `services/agent_workbench/project_setup.py`

- [ ] **Step 1: Add a seed helper for event-159 style rows**

Append this helper near `_create_recovery_row` in `tests/test_agent_workbench_project_setup.py`:

```python
def _seed_expired_pending_create_after_spec_approval(
    *,
    engine: Engine,
    spec_file: Path,
    project_name: str = "Interrupted Project",
) -> tuple[int, int]:
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
        lease_seconds=300,
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
        product.spec_file_path = str(spec_file.resolve())
        product.spec_loaded_at = now
        session.add(product)
        spec = SpecRegistry(
            product_id=project_id,
            spec_hash="sha256:seeded-spec",
            content=spec_file.read_text(encoding="utf-8"),
            content_ref=str(spec_file.resolve()),
            status="approved",
            approved_at=now,
            approved_by="cli-project-create",
            approval_notes="Seeded setup state before pending authority.",
        )
        session.add(spec)
        for step in (
            "product_created",
            "product_spec_linked",
            "spec_registry_written",
            "spec_marked_approved",
        ):
            assert MutationLedgerRepository.mark_step_complete_in_session(
                session,
                mutation_event_id=row.mutation_event_id,
                lease_owner="create-owner",
                step=step,
                next_step=step,
                now=now,
            )
        ledger = session.get(CliMutationLedger, row.mutation_event_id)
        assert ledger is not None
        ledger.lease_expires_at = now
        session.add(ledger)
        session.commit()
    return project_id, row.mutation_event_id
```

- [ ] **Step 2: Write failing retry repair and dry-run tests**

Append these tests in `tests/test_agent_workbench_project_setup.py`:

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


def test_project_setup_retry_dry_run_repairs_only_expired_pending_create(
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

    preview = runner.retry_setup(
        ProjectSetupRetryRequest(
            project_id=project_id,
            spec_file=str(spec_file),
            expected_state="SETUP_REQUIRED",
            expected_context_fingerprint=expected_fingerprint,
            recovery_mutation_event_id=original_event_id,
            dry_run=True,
            dry_run_id="retry-preview-interrupted-001",
            changed_by="agent",
        )
    )

    assert preview["ok"] is True
    assert preview["data"]["preview_available"] is True
    assert preview["data"]["project_id"] == project_id
    assert preview["data"]["recovery_mutation_event_id"] == original_event_id
    assert preview["data"]["recovery_status"] == MutationStatus.RECOVERY_REQUIRED.value
    with Session(engine) as session:
        original = session.get(CliMutationLedger, original_event_id)
        assert original is not None
        assert original.status == MutationStatus.RECOVERY_REQUIRED.value
        assert session.exec(select(CompiledSpecAuthority)).all() == []
        assert workflow.sessions == {}
```

- [ ] **Step 3: Run focused retry tests and verify they fail**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_project_setup.py \
  -k "repairs_expired_pending_create or dry_run_repairs_only_expired_pending_create" -q
```

Expected: fail because retry validation still rejects pending rows and dry-run returns before validation.

- [ ] **Step 4: Repair original recovery rows during validation**

In `_validate_original_recovery_row`, replace the explicit `RECOVERY_REQUIRED` status check with repair-first logic:

```python
        row = self._get_ledger(request.recovery_mutation_event_id)
        if (
            row is None
            or row.command != PROJECT_CREATE_COMMAND
            or row.project_id != request.project_id
        ):
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
        if (
            repaired.error_code is not None
            or repaired.ledger.status != MutationStatus.RECOVERY_REQUIRED.value
        ):
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

- [ ] **Step 5: Move retry dry-run after validation and guard checks**

In `_run_retry`, remove the early dry-run block. After the context fingerprint check succeeds and before `create_or_load(...)`, add:

```python
        if request.dry_run:
            return _success(
                {
                    "preview_available": True,
                    "project_id": request.project_id,
                    "resolved_spec_path": str(resolved_spec_path),
                    "recovery_mutation_event_id": request.recovery_mutation_event_id,
                    "recovery_status": (
                        original.status if original is not None else None
                    ),
                    "next_actions": [
                        _retry_action(request, request.recovery_mutation_event_id)
                    ],
                }
            )
```

Update `test_linked_setup_retry_dry_run_leaves_original_recovery_row_unchanged` so it passes a real expected fingerprint:

```python
    expected_fingerprint = _retry_fingerprint(
        project_id=project_id,
        spec_file=spec_file,
        workflow_state=workflow.sessions.get(str(project_id), {}),
    )
```

Then pass `expected_context_fingerprint=expected_fingerprint` in that request.

- [ ] **Step 6: Run setup retry tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_project_setup.py \
  -k "project_setup_retry or linked_setup_retry" -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add services/agent_workbench/project_setup.py tests/test_agent_workbench_project_setup.py
git commit -m "fix: repair project setup retry targets"
```

## Task 4: Truthful Recovery State And Timeout Behavior

**Files:**
- Modify: `tests/test_agent_workbench_project_setup.py`
- Modify: `services/agent_workbench/project_setup.py`

- [ ] **Step 1: Write failing timeout and recovery-mark-failure tests**

Append these tests in `tests/test_agent_workbench_project_setup.py`:

```python
def test_project_create_compiler_timeout_is_failed_setup_not_recovery(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_failing_compiler(
        monkeypatch,
        error_code="SPEC_COMPILE_FAILED",
        failure_artifact_id="spec-timeout-1",
        blocking_gaps=["Spec authority compiler exceeded 1800 seconds."],
    )
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)

    failed = runner.create_project(
        ProjectCreateRequest(
            name="Compiler Timeout Project",
            spec_file=str(spec_file),
            idempotency_key="create-timeout-001",
            changed_by="agent",
        )
    )

    assert failed["ok"] is False
    assert _error_code(failed) == "SPEC_COMPILE_FAILED"
    assert failed["data"]["setup_status"] == "failed"
    assert failed["data"]["next_actions"][0]["command"] == "agileforge project setup retry"
    assert "recovery_mutation_event_id" not in failed["data"]["next_actions"][0]["args"]
    with Session(engine) as session:
        ledger = session.get(CliMutationLedger, failed["data"]["mutation_event_id"])
        assert ledger is not None
        assert ledger.status == MutationStatus.VALIDATION_FAILED.value
        assert session.exec(select(CompiledSpecAuthority)).all() == []


def test_create_recovery_mark_failure_does_not_claim_recovery_required(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_failing_compiler(monkeypatch, error_code="MUTATION_IN_PROGRESS")
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    monkeypatch.setattr(
        MutationLedgerRepository,
        "mark_recovery_required",
        lambda *args, **kwargs: False,
    )

    result = runner.create_project(
        ProjectCreateRequest(
            name="Recovery Mark Failure Project",
            spec_file=str(spec_file),
            idempotency_key="create-recovery-mark-fails-001",
            changed_by="agent",
        )
    )

    assert result["ok"] is False
    assert _error_code(result) == "MUTATION_IN_PROGRESS"
    assert result["data"]["status"] == MutationStatus.PENDING.value
    with Session(engine) as session:
        ledger = session.get(CliMutationLedger, result["data"]["mutation_event_id"])
        assert ledger is not None
        assert ledger.status == MutationStatus.PENDING.value
```

- [ ] **Step 2: Run focused tests and verify recovery-mark failure fails**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_project_setup.py \
  -k "compiler_timeout_is_failed_setup or recovery_mark_failure" -q
```

Expected: timeout test may already pass through the existing compile-failure path; recovery-mark-failure must fail because `_mark_create_recovery_required` is ignored.

- [ ] **Step 3: Make recovery marking truthful**

Change `_mark_create_recovery_required` to return the persisted row or an error response:

```python
    def _mark_create_recovery_required(
        self,
        *,
        mutation_event_id: int,
        lease_owner: str,
        project_id: int,
        code: str,
        spec_file: str,
        safe_to_auto_resume: bool,
        spec_version_id: int | None = None,
    ) -> CliMutationLedger | dict[str, Any]:
        marked = self._ledger.mark_recovery_required(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            recovery_action=RecoveryAction.RESUME_FROM_STEP,
            safe_to_auto_resume=safe_to_auto_resume,
            last_error={
                "code": code,
                "project_id": project_id,
                "spec_version_id": spec_version_id,
                "spec_file": spec_file,
            },
            now=_now(),
        )
        if marked:
            return self._must_get_ledger(mutation_event_id)
        repaired = self._ledger.repair_setup_recovery_target(
            mutation_event_id=mutation_event_id,
            expected_command=PROJECT_CREATE_COMMAND,
            expected_project_id=project_id,
            now=_now(),
        )
        if (
            repaired.error_code is None
            and repaired.ledger.status == MutationStatus.RECOVERY_REQUIRED.value
        ):
            return repaired.ledger
        error_code = repaired.error_code or MUTATION_IN_PROGRESS
        return _error_for_ledger(error_code, repaired.ledger)
```

In `_run_setup_steps`, replace each ignored call followed by `_recovery_required_response(...)` with:

```python
                marked = self._mark_create_recovery_required(
                    mutation_event_id=mutation_event_id,
                    lease_owner=lease_owner,
                    project_id=project_id,
                    code=error_code,
                    spec_file=requested_spec_file,
                    safe_to_auto_resume=False,
                    spec_version_id=authority_result.get("spec_version_id"),
                )
                if isinstance(marked, dict):
                    return marked
                return _recovery_required_response(marked, requested_spec_file)
```

For the `fail_after_step_for_test == "product_created"` branch, keep the injected exception behavior but still call the method and let it persist when possible:

```python
                self._mark_create_recovery_required(
                    mutation_event_id=mutation_event_id,
                    lease_owner=lease_owner,
                    project_id=project_id,
                    code=WORKFLOW_SESSION_FAILED,
                    spec_file=requested_spec_file,
                    safe_to_auto_resume=True,
                )
```

- [ ] **Step 4: Run truthful recovery tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_project_setup.py \
  -k "recovery_mark_failure or recovery_required or compile_failure_records_failed_setup" -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add services/agent_workbench/project_setup.py tests/test_agent_workbench_project_setup.py
git commit -m "fix: report truthful create recovery state"
```

## Task 5: Event-159 Regression Without Live Model Calls

**Files:**
- Modify: `tests/test_agent_workbench_project_setup.py`
- Modify if needed: `services/agent_workbench/project_setup.py`

- [ ] **Step 1: Write the event-159 regression**

Append this test in `tests/test_agent_workbench_project_setup.py`:

```python
def test_event_159_style_long_compile_survives_past_original_lease(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    workflow = FakeWorkflowPort()

    def compile_with_many_heartbeats(
        *,
        engine: Engine,
        spec_version_id: int,
        force_recompile: bool | None = None,
        tool_context: object | None = None,
        lease_guard: Any | None = None,
        record_progress: Any | None = None,
    ) -> dict[str, Any]:
        del force_recompile, tool_context
        assert lease_guard is not None
        for index in range(6):
            assert lease_guard(f"authority_compile_invocation_heartbeat_{index}")
        with Session(engine) as session:
            authority = CompiledSpecAuthority(
                spec_version_id=spec_version_id,
                compiler_version="test-long-compiler",
                prompt_hash="sha256:test-long",
                compiled_artifact_json='{"ok":true}',
                scope_themes="[]",
                invariants="[]",
                eligible_feature_ids="[]",
                rejected_features="[]",
                spec_gaps="[]",
            )
            session.add(authority)
            session.commit()
            session.refresh(authority)
            authority_id = authority.authority_id
        if record_progress is not None:
            assert record_progress("compiled_authority_persisted")
            assert record_progress("product_authority_cache_persisted")
        return {
            "success": True,
            "authority_id": authority_id,
            "spec_version_id": spec_version_id,
            "compiler_version": "test-long-compiler",
            "prompt_hash": "sha256:test-long",
        }

    from services.agent_workbench import project_setup

    monkeypatch.setattr(
        project_setup,
        "compile_spec_authority_for_version_with_engine",
        compile_with_many_heartbeats,
    )
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)

    result = runner.create_project(
        ProjectCreateRequest(
            name="Event 159 Regression Project",
            spec_file=str(spec_file),
            idempotency_key="create-event-159-regression-001",
            changed_by="agent",
        )
    )

    assert result["ok"] is True
    with Session(engine) as session:
        ledger = session.get(CliMutationLedger, result["data"]["mutation_event_id"])
        assert ledger is not None
        assert ledger.status == MutationStatus.SUCCEEDED.value
        assert "pending_authority_compiled" in _row_payload(ledger)["completed_steps"]
        assert session.exec(select(CompiledSpecAuthority)).first() is not None
```

- [ ] **Step 2: Run the regression**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_project_setup.py \
  -k "event_159_style_long_compile" -q
```

Expected: pass if Tasks 1-4 are correct. If it fails, fix only the lease propagation or progress recording code needed to make this exact regression pass.

- [ ] **Step 3: Commit**

```bash
git add services/agent_workbench/project_setup.py tests/test_agent_workbench_project_setup.py
git commit -m "test: cover long project create authority compile"
```

## Task 6: Verification And Cleanup

**Files:**
- Verify only.

- [ ] **Step 1: Run focused recovery/compiler suite**

Run:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_mutation_ledger.py \
  tests/test_agent_workbench_project_setup.py \
  tests/test_specs_compiler_service.py \
  -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run CLI-adjacent coverage if present**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_cli.py -q
```

Expected: pass. If the file is absent in this checkout, record that it is absent and continue.

- [ ] **Step 3: Search for stale scope leakage**

Run:

```bash
git diff -- docs/superpowers/plans/2026-06-05-project-create-recovery.md \
  docs/superpowers/specs/2026-06-05-project-create-recovery-design.md \
  services tests | rg -n "^\\+.*(project[ ]delete|reset[ ]CLI|auto[-]Vision)"
git diff -- docs/superpowers/plans/2026-06-05-project-create-recovery.md \
  | rg -n "^\\+.*(TB[D]|TO[D]O|implement[ ]later|fill[ ]in[ ]details)"
```

Expected: both `rg` commands return no matches.

- [ ] **Step 4: Run diff hygiene**

Run:

```bash
git diff --check
```

Expected: no whitespace errors.

- [ ] **Step 5: Review final diff**

Run:

```bash
git status --short
git diff --stat
```

Expected: only Layer 1 files changed.

- [ ] **Step 6: Record final status**

Run:

```bash
git status --short
```

Expected: no uncommitted implementation files remain after the task commits above.

## Self-Review Checklist For Implementer

- Section 4 outcome matrix from the design is covered:
  - healthy long compile with heartbeat,
  - semantic compiler failure,
  - timeout as failed setup,
  - lease lost or unknown completion as recovery.
- Section 5 tests are implemented:
  - compiler heartbeat,
  - compiler timeout,
  - lease loss after compile or mark failure,
  - expired pending retry repair,
  - retry dry-run parity,
  - resume/retry alignment,
  - event-159 long structured compile regression.
- Required review notes are pinned down:
  - concrete heartbeat, lease, and deadline values,
  - single resume handoff rule,
  - expiry as `now >= lease_expires_at`,
  - timeout ledger status and response contract,
  - recovery-mark-failure regression is required.
- Layer 2 resumable authority units remain a follow-up investigation.
