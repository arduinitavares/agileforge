# Issue 174 Discovery Schema Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Scope Discovery schema readiness truthful so `doctor`, `schema check`, and exhausted-project `workflow next` return `SCHEMA_NOT_READY` instead of raw SQLite `OperationalError` when discovery storage is absent.

**Architecture:** Add shared Scope Discovery `SchemaRequirement` contracts in `schema_readiness.py`, reuse them from diagnostics, and add a targeted guard before `workflow next` queries discovery tables. Do not add discovery requirements to generic `workflow state`; that command does not read discovery tables.

**Tech Stack:** Python 3.13, SQLModel/SQLAlchemy, pytest, AgileForge JSON envelopes.

---

## File Map

- Modify `services/agent_workbench/schema_readiness.py`
  - Own shared schema contracts for existing-project discovery tables, greenfield discovery tables, and the combined Scope Discovery storage surface.
- Modify `services/agent_workbench/diagnostics.py`
  - Make `doctor` and `schema check` report missing Scope Discovery tables/columns using the shared contracts.
- Modify `services/agent_workbench/application.py`
  - Guard `_scope_discovery_next_response()` before it queries discovery tables.
- Modify `tests/test_agent_workbench_schema_readiness.py`
  - Prove the shared requirements include the tables and uniqueness contracts needed by migrations and runtime queries.
- Modify `tests/test_agent_workbench_diagnostics.py`
  - Prove diagnostics report missing discovery storage even when schema version and mutation ledger look current.
- Modify `tests/test_agent_workbench_application.py`
  - Prove exhausted-project `workflow next` returns `SCHEMA_NOT_READY` when discovery tables are missing.

---

### Task 1: Add Failing Schema Readiness Contract Tests

**Files:**
- Modify: `tests/test_agent_workbench_schema_readiness.py`

- [ ] **Step 1: Add imports for new constants before they exist**

Change the import block from `services.agent_workbench.schema_readiness` to include:

```python
from services.agent_workbench.schema_readiness import (
    AUTHORITY_CURATION_REQUIREMENTS,
    DISCOVERY_REQUIREMENTS,
    GREENFIELD_DISCOVERY_REQUIREMENTS,
    SCOPE_DISCOVERY_REQUIREMENTS,
    SchemaRequirement,
    check_schema_readiness,
)
```

- [ ] **Step 2: Add tests for existing-project discovery contracts**

Append these tests after `test_authority_curation_readiness_requires_v2_columns`:

```python
def test_scope_discovery_readiness_requires_existing_project_tables() -> None:
    """Existing-project discovery requirements must cover every runtime table."""
    requirements_by_table = {
        requirement.table: requirement for requirement in DISCOVERY_REQUIREMENTS
    }

    assert set(requirements_by_table) == {
        "discovery_challenge_artifacts",
        "discovery_prds",
        "discovery_spec_amendment_drafts",
    }
    assert (
        "project_id",
        "idempotency_key",
    ) in requirements_by_table[
        "discovery_challenge_artifacts"
    ].unique_columns
    assert (
        "project_id",
        "idempotency_key",
    ) in requirements_by_table["discovery_prds"].unique_columns
    assert (
        "project_id",
        "idempotency_key",
    ) in requirements_by_table[
        "discovery_spec_amendment_drafts"
    ].unique_columns


def test_scope_discovery_readiness_requires_greenfield_tables() -> None:
    """Greenfield discovery requirements must cover every provisional table."""
    requirements_by_table = {
        requirement.table: requirement for requirement in GREENFIELD_DISCOVERY_REQUIREMENTS
    }

    assert set(requirements_by_table) == {
        "greenfield_discovery_contexts",
        "greenfield_discovery_challenge_artifacts",
        "greenfield_discovery_prds",
        "greenfield_discovery_spec_amendment_drafts",
    }
    assert ("context_key",) in requirements_by_table[
        "greenfield_discovery_contexts"
    ].unique_columns
    assert ("idempotency_key",) in requirements_by_table[
        "greenfield_discovery_contexts"
    ].unique_columns
    assert (
        "greenfield_context_id",
        "idempotency_key",
    ) in requirements_by_table[
        "greenfield_discovery_challenge_artifacts"
    ].unique_columns


def test_scope_discovery_readiness_combines_existing_and_greenfield() -> None:
    """Combined discovery requirements are available for broad diagnostics."""
    combined_tables = {requirement.table for requirement in SCOPE_DISCOVERY_REQUIREMENTS}

    assert combined_tables == {
        requirement.table
        for requirement in (
            *DISCOVERY_REQUIREMENTS,
            *GREENFIELD_DISCOVERY_REQUIREMENTS,
        )
    }
```

- [ ] **Step 3: Run the focused tests and verify failure**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_schema_readiness.py -q
```

Expected: fail with `ImportError` or `NameError` for `DISCOVERY_REQUIREMENTS`.

- [ ] **Step 4: Do not commit yet**

This is the red test task only.

---

### Task 2: Add Shared Scope Discovery Schema Requirements

**Files:**
- Modify: `services/agent_workbench/schema_readiness.py`
- Test: `tests/test_agent_workbench_schema_readiness.py`

- [ ] **Step 1: Add existing-project discovery requirements**

In `services/agent_workbench/schema_readiness.py`, after `AUTHORITY_CURATION_REQUIREMENTS`, add:

```python
DISCOVERY_REQUIREMENTS: tuple[SchemaRequirement, ...] = (
    SchemaRequirement(
        table="discovery_challenge_artifacts",
        columns=(
            "challenge_artifact_id",
            "project_id",
            "producer",
            "readiness",
            "original_idea",
            "content_json",
            "artifact_fingerprint",
            "request_hash",
            "idempotency_key",
            "changed_by",
            "created_at",
            "updated_at",
        ),
        indexes=(
            "ix_discovery_challenge_artifacts_project_id",
            "ix_discovery_challenge_artifacts_producer",
            "ix_discovery_challenge_artifacts_readiness",
            "ix_discovery_challenge_artifacts_artifact_fingerprint",
            "ix_discovery_challenge_artifacts_request_hash",
            "ix_discovery_challenge_artifacts_idempotency_key",
            "ix_discovery_challenge_artifacts_changed_by",
        ),
        unique_columns=(("project_id", "idempotency_key"),),
    ),
    SchemaRequirement(
        table="discovery_prds",
        columns=(
            "prd_id",
            "project_id",
            "challenge_artifact_id",
            "producer",
            "status",
            "version",
            "title",
            "content_json",
            "supersedes_prd_id",
            "artifact_fingerprint",
            "request_hash",
            "idempotency_key",
            "reviewed_by",
            "review_notes",
            "reviewed_at",
            "review_request_hash",
            "review_idempotency_key",
            "changed_by",
            "created_at",
            "updated_at",
        ),
        indexes=(
            "ix_discovery_prds_project_id",
            "ix_discovery_prds_challenge_artifact_id",
            "ix_discovery_prds_producer",
            "ix_discovery_prds_status",
            "ix_discovery_prds_version",
            "ix_discovery_prds_title",
            "ix_discovery_prds_supersedes_prd_id",
            "ix_discovery_prds_artifact_fingerprint",
            "ix_discovery_prds_request_hash",
            "ix_discovery_prds_idempotency_key",
            "ix_discovery_prds_reviewed_by",
            "ix_discovery_prds_review_request_hash",
            "ix_discovery_prds_review_idempotency_key",
            "ix_discovery_prds_changed_by",
        ),
        unique_columns=(("project_id", "idempotency_key"),),
    ),
    SchemaRequirement(
        table="discovery_spec_amendment_drafts",
        columns=(
            "spec_amendment_draft_id",
            "project_id",
            "prd_id",
            "challenge_artifact_id",
            "status",
            "amendment_file",
            "content_json",
            "validation_json",
            "artifact_fingerprint",
            "request_hash",
            "idempotency_key",
            "base_spec_version_id",
            "base_spec_hash",
            "amended_spec_hash",
            "reviewed_by",
            "review_notes",
            "reviewed_at",
            "review_request_hash",
            "review_idempotency_key",
            "changed_by",
            "created_at",
            "updated_at",
        ),
        indexes=(
            "ix_discovery_spec_amendment_drafts_project_id",
            "ix_discovery_spec_amendment_drafts_prd_id",
            "ix_discovery_spec_amendment_drafts_challenge_artifact_id",
            "ix_discovery_spec_amendment_drafts_status",
            "ix_discovery_spec_amendment_drafts_artifact_fingerprint",
            "ix_discovery_spec_amendment_drafts_request_hash",
            "ix_discovery_spec_amendment_drafts_idempotency_key",
            "ix_discovery_spec_amendment_drafts_base_spec_version_id",
            "ix_discovery_spec_amendment_drafts_base_spec_hash",
            "ix_discovery_spec_amendment_drafts_amended_spec_hash",
            "ix_discovery_spec_amendment_drafts_reviewed_by",
            "ix_discovery_spec_amendment_drafts_review_request_hash",
            "ix_discovery_spec_amendment_drafts_review_idempotency_key",
            "ix_discovery_spec_amendment_drafts_changed_by",
        ),
        unique_columns=(("project_id", "idempotency_key"),),
    ),
)
```

- [ ] **Step 2: Add greenfield discovery requirements**

Immediately after `DISCOVERY_REQUIREMENTS`, add:

```python
GREENFIELD_DISCOVERY_REQUIREMENTS: tuple[SchemaRequirement, ...] = (
    SchemaRequirement(
        table="greenfield_discovery_contexts",
        columns=(
            "greenfield_context_id",
            "context_key",
            "project_id",
            "status",
            "request_hash",
            "idempotency_key",
            "changed_by",
            "created_at",
            "updated_at",
        ),
        indexes=(
            "ix_greenfield_discovery_contexts_context_key",
            "ix_greenfield_discovery_contexts_project_id",
            "ix_greenfield_discovery_contexts_status",
            "ix_greenfield_discovery_contexts_request_hash",
            "ix_greenfield_discovery_contexts_idempotency_key",
            "ix_greenfield_discovery_contexts_changed_by",
        ),
        unique_columns=(("context_key",), ("idempotency_key",)),
    ),
    SchemaRequirement(
        table="greenfield_discovery_challenge_artifacts",
        columns=(
            "challenge_artifact_id",
            "greenfield_context_id",
            "producer",
            "readiness",
            "original_idea",
            "content_json",
            "artifact_fingerprint",
            "request_hash",
            "idempotency_key",
            "changed_by",
            "created_at",
            "updated_at",
        ),
        indexes=(
            "ix_greenfield_discovery_challenge_artifacts_greenfield_context_id",
            "ix_greenfield_discovery_challenge_artifacts_producer",
            "ix_greenfield_discovery_challenge_artifacts_readiness",
            "ix_greenfield_discovery_challenge_artifacts_artifact_fingerprint",
            "ix_greenfield_discovery_challenge_artifacts_request_hash",
            "ix_greenfield_discovery_challenge_artifacts_idempotency_key",
            "ix_greenfield_discovery_challenge_artifacts_changed_by",
        ),
        unique_columns=(("greenfield_context_id", "idempotency_key"),),
    ),
    SchemaRequirement(
        table="greenfield_discovery_prds",
        columns=(
            "prd_id",
            "greenfield_context_id",
            "challenge_artifact_id",
            "producer",
            "status",
            "version",
            "title",
            "content_json",
            "artifact_fingerprint",
            "request_hash",
            "idempotency_key",
            "reviewed_by",
            "review_notes",
            "reviewed_at",
            "review_request_hash",
            "review_idempotency_key",
            "changed_by",
            "created_at",
            "updated_at",
        ),
        indexes=(
            "ix_greenfield_discovery_prds_greenfield_context_id",
            "ix_greenfield_discovery_prds_challenge_artifact_id",
            "ix_greenfield_discovery_prds_producer",
            "ix_greenfield_discovery_prds_status",
            "ix_greenfield_discovery_prds_version",
            "ix_greenfield_discovery_prds_title",
            "ix_greenfield_discovery_prds_artifact_fingerprint",
            "ix_greenfield_discovery_prds_request_hash",
            "ix_greenfield_discovery_prds_idempotency_key",
            "ix_greenfield_discovery_prds_reviewed_by",
            "ix_greenfield_discovery_prds_review_request_hash",
            "ix_greenfield_discovery_prds_review_idempotency_key",
            "ix_greenfield_discovery_prds_changed_by",
        ),
        unique_columns=(("greenfield_context_id", "idempotency_key"),),
    ),
    SchemaRequirement(
        table="greenfield_discovery_spec_amendment_drafts",
        columns=(
            "spec_amendment_draft_id",
            "greenfield_context_id",
            "prd_id",
            "challenge_artifact_id",
            "status",
            "amendment_file",
            "content_json",
            "validation_json",
            "artifact_fingerprint",
            "request_hash",
            "idempotency_key",
            "amended_spec_hash",
            "reviewed_by",
            "review_notes",
            "reviewed_at",
            "review_request_hash",
            "review_idempotency_key",
            "changed_by",
            "created_at",
            "updated_at",
        ),
        indexes=(
            "ix_greenfield_discovery_spec_amendment_drafts_greenfield_context_id",
            "ix_greenfield_discovery_spec_amendment_drafts_prd_id",
            "ix_greenfield_discovery_spec_amendment_drafts_challenge_artifact_id",
            "ix_greenfield_discovery_spec_amendment_drafts_status",
            "ix_greenfield_discovery_spec_amendment_drafts_artifact_fingerprint",
            "ix_greenfield_discovery_spec_amendment_drafts_request_hash",
            "ix_greenfield_discovery_spec_amendment_drafts_idempotency_key",
            "ix_greenfield_discovery_spec_amendment_drafts_amended_spec_hash",
            "ix_greenfield_discovery_spec_amendment_drafts_reviewed_by",
            "ix_greenfield_discovery_spec_amendment_drafts_review_request_hash",
            "ix_greenfield_discovery_spec_amendment_drafts_review_idempotency_key",
            "ix_greenfield_discovery_spec_amendment_drafts_changed_by",
        ),
        unique_columns=(("greenfield_context_id", "idempotency_key"),),
    ),
)

SCOPE_DISCOVERY_REQUIREMENTS: tuple[SchemaRequirement, ...] = (
    *DISCOVERY_REQUIREMENTS,
    *GREENFIELD_DISCOVERY_REQUIREMENTS,
)
```

- [ ] **Step 3: Run schema readiness tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_schema_readiness.py -q
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add services/agent_workbench/schema_readiness.py tests/test_agent_workbench_schema_readiness.py
git commit -m "test: cover scope discovery schema requirements"
```

---

### Task 3: Make Diagnostics Report Missing Discovery Storage

**Files:**
- Modify: `services/agent_workbench/diagnostics.py`
- Modify: `tests/test_agent_workbench_diagnostics.py`

- [ ] **Step 1: Add a helper to drop discovery tables in diagnostics tests**

In `tests/test_agent_workbench_diagnostics.py`, after `AGENT_WORKBENCH_SCHEMA_VERSIONS_CREATE_SQL`, add:

```python
SCOPE_DISCOVERY_TABLES: tuple[str, ...] = (
    "discovery_spec_amendment_drafts",
    "discovery_prds",
    "discovery_challenge_artifacts",
    "greenfield_discovery_spec_amendment_drafts",
    "greenfield_discovery_prds",
    "greenfield_discovery_challenge_artifacts",
    "greenfield_discovery_contexts",
)


def _drop_scope_discovery_tables(engine: Engine) -> None:
    """Remove Scope Discovery tables from an otherwise migrated database."""
    with engine.begin() as conn:
        for table_name in SCOPE_DISCOVERY_TABLES:
            conn.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
```

- [ ] **Step 2: Update the ready diagnostics test**

In `test_schema_check_reports_ready_business_db`, update the expected `checks` dict to include:

```python
"scope_discovery_tables": True,
"scope_discovery_columns": True,
```

The full expected `business_db` dict should become:

```python
assert payload["business_db"] == {
    "ok": True,
    "status": "ok",
    "required_version": STORAGE_SCHEMA_VERSION,
    "version_source": "agent_workbench_schema_versions",
    "checks": {
        "schema_versions_table": True,
        "cli_mutation_ledger_table": True,
        "cli_mutation_ledger_columns": True,
        "scope_discovery_tables": True,
        "scope_discovery_columns": True,
    },
    "missing": [],
}
```

- [ ] **Step 3: Add a failing regression test for issue #174 diagnostics**

Append this test after `test_schema_check_reports_ready_business_db`:

```python
def test_schema_check_blocks_missing_scope_discovery_storage(
    engine: Engine,
) -> None:
    """Report missing discovery tables even when version and ledger look current."""
    ensure_schema_current(engine)
    _drop_scope_discovery_tables(engine)

    payload = schema_check_payload(
        business_engine=engine,
        session_db_url="sqlite:///:memory:",
    )

    assert payload["business_db"]["ok"] is False
    assert payload["business_db"]["status"] == "blocked"
    assert payload["business_db"]["checks"]["schema_versions_table"] is True
    assert payload["business_db"]["checks"]["cli_mutation_ledger_table"] is True
    assert payload["business_db"]["checks"]["cli_mutation_ledger_columns"] is True
    assert payload["business_db"]["checks"]["scope_discovery_tables"] is False
    assert payload["business_db"]["checks"]["scope_discovery_columns"] is False
    assert "discovery_challenge_artifacts" in payload["business_db"]["missing"]
    assert "discovery_prds" in payload["business_db"]["missing"]
    assert "discovery_spec_amendment_drafts" in payload["business_db"]["missing"]
    assert "greenfield_discovery_contexts" in payload["business_db"]["missing"]
```

- [ ] **Step 4: Update existing diagnostics tests that assert the full checks dict**

For these tests, add the two new check keys with expected `False` because the test DB is intentionally incomplete:

- `test_schema_check_reports_missing_business_contract_tables`
- `test_schema_check_reports_missing_business_sqlite_file_without_creating_it`
- `test_schema_check_reports_missing_mutation_ledger_columns`
- `test_schema_check_reports_malformed_mutation_ledger_columns`

Use:

```python
"scope_discovery_tables": False,
"scope_discovery_columns": False,
```

- [ ] **Step 5: Run diagnostics tests and verify failure before implementation**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_diagnostics.py -q
```

Expected: fail because diagnostics does not yet inspect `SCOPE_DISCOVERY_REQUIREMENTS`.

- [ ] **Step 6: Import shared requirements in diagnostics**

In `services/agent_workbench/diagnostics.py`, change the schema readiness import to:

```python
from services.agent_workbench.schema_readiness import (
    MUTATION_LEDGER_REQUIRED_COLUMNS,
    MUTATION_LEDGER_TABLE,
    SCOPE_DISCOVERY_REQUIREMENTS,
    check_schema_readiness,
)
```

- [ ] **Step 7: Add a helper to flatten readiness misses**

In `services/agent_workbench/diagnostics.py`, before `_business_db_payload`, add:

```python
def _flatten_missing(
    missing: dict[str, list[str]],
    *,
    table_names: set[str],
) -> list[str]:
    """Return table names for absent tables and table.element for missing elements."""
    flattened: list[str] = []
    for table_name, elements in missing.items():
        if table_name not in table_names:
            flattened.append(table_name)
            continue
        flattened.extend(f"{table_name}.{element}" for element in elements)
    return flattened
```

Then simplify after implementation if the line length or semantics are awkward. The important behavior is:

- missing table becomes `"discovery_challenge_artifacts"`;
- missing column becomes `"discovery_challenge_artifacts.producer"`.

- [ ] **Step 8: Update `_business_db_payload` to check Scope Discovery readiness**

Inside `_business_db_payload`, after mutation ledger column checks, add:

```python
    discovery_readiness = check_schema_readiness(engine, SCOPE_DISCOVERY_REQUIREMENTS)
    discovery_missing = _flatten_missing(
        discovery_readiness.missing,
        table_names=table_names,
    )
```

Update the `checks` dict returned for existing DBs to include:

```python
"scope_discovery_tables": not any(
    table_name in discovery_readiness.missing
    for table_name in (requirement.table for requirement in SCOPE_DISCOVERY_REQUIREMENTS)
),
"scope_discovery_columns": discovery_readiness.ok,
```

Extend `missing` with:

```python
missing.extend(discovery_missing)
```

For early return paths where the SQLite file is absent or inspection fails, include:

```python
"scope_discovery_tables": False,
"scope_discovery_columns": False,
```

- [ ] **Step 9: Run diagnostics tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_diagnostics.py -q
```

Expected: pass.

- [ ] **Step 10: Commit**

```bash
git add services/agent_workbench/diagnostics.py tests/test_agent_workbench_diagnostics.py
git commit -m "fix: report missing scope discovery storage"
```

---

### Task 4: Guard Exhausted-Project `workflow next`

**Files:**
- Modify: `services/agent_workbench/application.py`
- Modify: `tests/test_agent_workbench_application.py`

- [ ] **Step 1: Add a regression test for issue #174 workflow-next crash**

In `tests/test_agent_workbench_application.py`, after `test_workflow_next_routes_exhausted_default_app_to_scope_discovery`, add:

```python
def test_workflow_next_returns_schema_not_ready_when_discovery_tables_missing(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not raise OperationalError when exhausted-project discovery tables are absent."""
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE discovery_spec_amendment_drafts"))
        conn.execute(text("DROP TABLE discovery_prds"))
        conn.execute(text("DROP TABLE discovery_challenge_artifacts"))
    monkeypatch.setattr(application_mod, "get_engine", lambda: engine, raising=False)
    monkeypatch.setattr(
        post_sprint_triage_module,
        "canonical_hash",
        lambda _payload: "sha256:triage",
    )
    app = AgentWorkbenchApplication(
        read_projection=_SprintCompleteTriagedNoneNoRefinedCandidatesReadProjection(
            impact="none",
        ),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is False
    assert result["data"] is None
    assert result["errors"][0]["code"] == "SCHEMA_NOT_READY"
    assert result["errors"][0]["retryable"] is True
    assert "discovery_challenge_artifacts" in result["errors"][0]["details"]["missing"]
```

If `text` is not already imported in this test file, add:

```python
from sqlalchemy import text
```

- [ ] **Step 2: Run the regression test and verify failure**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py::test_workflow_next_returns_schema_not_ready_when_discovery_tables_missing -q
```

Expected: fail with `COMMAND_EXCEPTION` or `OperationalError`.

- [ ] **Step 3: Import discovery requirements**

In `services/agent_workbench/application.py`, update the schema readiness import:

```python
from services.agent_workbench.schema_readiness import (
    DISCOVERY_REQUIREMENTS,
    MUTATION_LEDGER_REQUIREMENTS,
    SchemaReadiness,
    check_schema_readiness,
)
```

- [ ] **Step 4: Add a workflow-next schema error helper**

Near `_mutation_ledger_repository`, add:

```python
def _schema_not_ready_response(
    *,
    command: str,
    readiness: SchemaReadiness,
) -> dict[str, Any]:
    """Return a schema-not-ready envelope for application-level guards."""
    return error_envelope(
        command=command,
        error=workbench_error(
            ErrorCode.SCHEMA_NOT_READY,
            message=(
                "Database schema is missing required tables or columns for this "
                "command."
            ),
            details={"missing": readiness.missing},
            remediation=[
                "Run the application startup or migration command before using the CLI.",
                "Then rerun agileforge schema check.",
            ],
        ),
    )
```

- [ ] **Step 5: Guard `_scope_discovery_next_response` before querying**

At the top of `_scope_discovery_next_response`, before `with Session(get_engine()) as session:`, replace direct engine usage with:

```python
    engine = get_engine()
    readiness = check_schema_readiness(engine, DISCOVERY_REQUIREMENTS)
    if not readiness.ok:
        return _schema_not_ready_response(
            command=WORKFLOW_NEXT_COMMAND,
            readiness=readiness,
        )

    with Session(engine) as session:
```

Do not add `DISCOVERY_REQUIREMENTS` to `_WORKFLOW_STATE_REQUIREMENTS` in `read_projection.py`.

- [ ] **Step 6: Run the focused regression test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py::test_workflow_next_returns_schema_not_ready_when_discovery_tables_missing -q
```

Expected: pass.

- [ ] **Step 7: Run nearby workflow-next scope discovery tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "scope_discovery or workflow_next_routes_exhausted"
```

Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add services/agent_workbench/application.py tests/test_agent_workbench_application.py
git commit -m "fix: guard scope discovery workflow routing"
```

---

### Task 5: Optional Direct Discovery Command Guard

**Files:**
- Modify only if Task 4 still leaves direct `discovery ...` commands returning raw `OperationalError`.

This task is optional because issue #174 acceptance criteria focus on `workflow next`, `doctor`, and `schema check`. Execute it only if focused manual probes show direct discovery commands still surface raw database exceptions after diagnostics are fixed.

- [ ] **Step 1: Probe direct command behavior with missing discovery tables**

Use a temporary DB or test harness. Do not mutate the user database.

Expected safe behavior after Tasks 2-4: agents should stop after `schema check` reports blocked. If direct commands are still reachable and return `COMMAND_EXCEPTION`, continue this task.

- [ ] **Step 2: Add direct command guard tests**

Add tests in `tests/test_agent_workbench_scope_discovery.py` that construct a `ScopeDiscoveryRunner(session=session)` against an engine missing discovery tables and assert `SCHEMA_NOT_READY`.

- [ ] **Step 3: Add runner-level readiness checks**

In `services/agent_workbench/scope_discovery.py`, import:

```python
from sqlalchemy.engine import Engine
from services.agent_workbench.schema_readiness import (
    DISCOVERY_REQUIREMENTS,
    GREENFIELD_DISCOVERY_REQUIREMENTS,
    check_schema_readiness,
)
```

Add a helper:

```python
def _schema_not_ready(
    session: Session,
    requirements: tuple[SchemaRequirement, ...],
) -> dict[str, Any] | None:
    """Return schema-not-ready when discovery storage is absent."""
    engine = cast("Engine", session.get_bind())
    readiness = check_schema_readiness(engine, requirements)
    if readiness.ok:
        return None
    return _error(
        ErrorCode.SCHEMA_NOT_READY,
        details={"missing": readiness.missing},
        remediation=[
            "Run the application startup or migration command before using the CLI.",
            "Then rerun agileforge schema check.",
        ],
    )
```

Use `DISCOVERY_REQUIREMENTS` for existing-project methods and `GREENFIELD_DISCOVERY_REQUIREMENTS` for greenfield methods.

- [ ] **Step 4: Run scope discovery tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_scope_discovery.py -q
```

Expected: pass.

- [ ] **Step 5: Commit only if implemented**

```bash
git add services/agent_workbench/scope_discovery.py tests/test_agent_workbench_scope_discovery.py
git commit -m "fix: guard discovery commands against missing storage"
```

---

### Task 6: Final Verification

**Files:**
- No planned edits.

- [ ] **Step 1: Run focused suites**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_schema_readiness.py tests/test_agent_workbench_diagnostics.py -q
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "scope_discovery or workflow_next_routes_exhausted or schema_not_ready"
```

Expected: all pass.

- [ ] **Step 2: Run changed-path suite**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_schema_readiness.py tests/test_agent_workbench_diagnostics.py tests/test_agent_workbench_application.py -q
```

Expected: pass.

- [ ] **Step 3: Run static checks**

Run:

```bash
uv run --frozen ruff check services/agent_workbench/schema_readiness.py services/agent_workbench/diagnostics.py services/agent_workbench/application.py tests/test_agent_workbench_schema_readiness.py tests/test_agent_workbench_diagnostics.py tests/test_agent_workbench_application.py
git diff --check
```

Expected: pass.

- [ ] **Step 4: Run full project gate if time allows**

Run:

```bash
uv run --frozen pyrepo-check --all
```

Expected: Ruff, annotations, ty, Bandit, and pytest pass.

- [ ] **Step 5: Manual acceptance probe**

Use a temporary or copied DB, not the user database:

1. Ensure the DB has version table and mutation ledger.
2. Drop `discovery_challenge_artifacts`.
3. Run `agileforge schema check`.
4. Run `agileforge workflow next --project-id <project_id>`.

Expected:

- `schema check` returns `ok: true` at the envelope level but `data.business_db.ok=false`, or the established diagnostics envelope shape for blocked readiness.
- `workflow next` returns `ok=false` with `errors[0].code == "SCHEMA_NOT_READY"`.
- No raw `sqlite3.OperationalError` appears.

- [ ] **Step 6: Final commit if any verification-only fixes were needed**

```bash
git status --short
git log --oneline -5
```

Commit any final cleanup with:

```bash
git add <changed_files>
git commit -m "test: verify scope discovery schema readiness"
```
