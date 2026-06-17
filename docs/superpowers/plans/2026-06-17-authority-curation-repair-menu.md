# Authority Curation Repair Menu Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace model-authored authority curation patches with a v2 host repair menu where the model selects host-minted handles and host code applies bounded deterministic text repairs.

**Architecture:** ADK remains the curation workflow substrate, but the repair node emits only `RepairSelectionPayload`. AgileForge host services build the repair menu, validate selections, apply exact-field text repairs, reject legacy patch/full-candidate outputs, validate diffs across invariants/assumptions/gaps, persist safe trace/idempotency metadata, and stop at human review.

**Tech Stack:** Python, Pydantic v2, SQLModel, SQLite migrations, Google ADK 2.x, pytest, existing mutation ledger, existing authority curation trace JSONL.

---

## Scope Check

This plan implements the accepted design:

- `docs/superpowers/specs/2026-06-17-authority-curation-repair-menu-design.md`

This plan intentionally does not retry the ASA project. ASA retry happens only after all replay fixtures and `pyrepo-check --all` pass.

## File Structure

- `orchestrator_agent/agent_tools/authority_curation/schemes.py`
  - Owns v2 Pydantic schemas: `AuthorityCurationRepairMenuEntry`, `AuthorityCurationRepairSelection`, `AuthorityCurationRepairSelectionPayload`, and updated repair output wrapper.
- `orchestrator_agent/agent_tools/authority_curation/agent.py`
  - Updates repair compiler prompt to select host handles, not patches or full candidate JSON.
- `services/agent_workbench/error_codes.py`
  - Adds `AUTHORITY_REPAIR_INTENT_INVALID` and `AUTHORITY_REPAIR_TARGET_NOT_FOUND`.
- `models/authority_curation.py`
  - Adds attempt metadata fields: `contract_version`, `menu_fingerprint`, `selection_fingerprint`, `rejected_selection_json`, and `overlay_json`.
- `db/migrations.py`
  - Adds additive migration columns for the new fields.
- `services/agent_workbench/schema_readiness.py`
  - Adds readiness checks for new curation columns.
- `services/specs/authority_curation_diff.py`
  - Generalizes diff from invariant-only to `invariants`, `assumptions`, and `gaps` with per-collection target allowlists.
- `services/agent_workbench/authority_curation.py`
  - Builds repair menus, invokes v2 workflow, validates selection payloads, applies exact-field text repairs, hard-rejects legacy outputs, persists selection fingerprints and trace data.
- `utils/authority_curation_trace.py`
  - Extends safe trace event attributes for per-selection summaries if the existing writer needs schema/field tests.
- Tests:
  - `tests/test_authority_curation_agent.py`
  - `tests/test_authority_curation_models.py`
  - `tests/test_agent_workbench_schema_readiness.py`
  - `tests/test_db_migrations.py`
  - `tests/test_agent_workbench_authority_curation.py`
  - `tests/test_authority_curation_trace.py`

## Task 1: Error Codes And Persistence Columns

**Files:**
- Modify: `services/agent_workbench/error_codes.py`
- Modify: `models/authority_curation.py`
- Modify: `db/migrations.py`
- Modify: `services/agent_workbench/schema_readiness.py`
- Test: `tests/test_agent_workbench_error_codes.py`
- Test: `tests/test_authority_curation_models.py`
- Test: `tests/test_agent_workbench_schema_readiness.py`
- Test: `tests/test_db_migrations.py`

- [ ] **Step 1: Add failing error-code test**

Add to `tests/test_agent_workbench_error_codes.py`:

```python
def test_authority_repair_v2_error_codes_are_registered() -> None:
    invalid = error_metadata(ErrorCode.AUTHORITY_REPAIR_INTENT_INVALID)
    missing = error_metadata(ErrorCode.AUTHORITY_REPAIR_TARGET_NOT_FOUND)

    assert invalid.code == "AUTHORITY_REPAIR_INTENT_INVALID"
    assert invalid.exit_code == 1
    assert invalid.retryable is False
    assert missing.code == "AUTHORITY_REPAIR_TARGET_NOT_FOUND"
    assert missing.exit_code == 1
    assert missing.retryable is False
```

- [ ] **Step 2: Run error-code test and verify it fails**

Run:

```bash
uv run pytest tests/test_agent_workbench_error_codes.py::test_authority_repair_v2_error_codes_are_registered -q
```

Expected: FAIL because `ErrorCode.AUTHORITY_REPAIR_INTENT_INVALID` does not exist.

- [ ] **Step 3: Implement error codes**

In `services/agent_workbench/error_codes.py`, add enum values near existing authority curation codes:

```python
AUTHORITY_REPAIR_INTENT_INVALID = "AUTHORITY_REPAIR_INTENT_INVALID"
AUTHORITY_REPAIR_TARGET_NOT_FOUND = "AUTHORITY_REPAIR_TARGET_NOT_FOUND"
```

Add registry entries:

```python
ErrorCode.AUTHORITY_REPAIR_INTENT_INVALID: ErrorMetadata(
    code=ErrorCode.AUTHORITY_REPAIR_INTENT_INVALID.value,
    exit_code=1,
    retryable=False,
    description="Authority repair selection violates the repair menu contract.",
),
ErrorCode.AUTHORITY_REPAIR_TARGET_NOT_FOUND: ErrorMetadata(
    code=ErrorCode.AUTHORITY_REPAIR_TARGET_NOT_FOUND.value,
    exit_code=1,
    retryable=False,
    description="Authority repair menu handle resolves to a missing source target.",
),
```

- [ ] **Step 4: Add failing model/migration tests**

In `tests/test_authority_curation_models.py`, extend the existing column/default test:

```python
def test_authority_curation_attempt_v2_columns_exist(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)

    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("authority_curation_attempts")}

    assert "contract_version" in columns
    assert "menu_fingerprint" in columns
    assert "selection_fingerprint" in columns
    assert "rejected_selection_json" in columns
    assert "overlay_json" in columns
```

In `tests/test_agent_workbench_schema_readiness.py`, add requirements assertion:

```python
def test_schema_readiness_requires_authority_curation_v2_columns() -> None:
    requirements = _authority_curation_requirements()
    columns = {
        column
        for requirement in requirements
        if requirement.table == "authority_curation_attempts"
        for column in requirement.columns
    }

    assert "contract_version" in columns
    assert "menu_fingerprint" in columns
    assert "selection_fingerprint" in columns
    assert "rejected_selection_json" in columns
    assert "overlay_json" in columns
```

If `_authority_curation_requirements` is private/unavailable, follow the existing schema-readiness test style in that file and assert via the public requirements collection.

- [ ] **Step 5: Run new model/readiness tests and verify failure**

Run:

```bash
uv run pytest tests/test_authority_curation_models.py::test_authority_curation_attempt_v2_columns_exist tests/test_agent_workbench_schema_readiness.py -q
```

Expected: FAIL on missing columns/readiness requirements.

- [ ] **Step 6: Add model fields**

In `models/authority_curation.py`, add to `AuthorityCurationAttempt`:

```python
contract_version: str = Field(
    default="authority_curation.v1",
    sa_column_kwargs={"server_default": text("'authority_curation.v1'")},
)
menu_fingerprint: str | None = Field(default=None)
selection_fingerprint: str | None = Field(default=None)
rejected_selection_json: str = Field(
    default="{}",
    sa_type=Text,
    sa_column_kwargs={"server_default": text("'{}'")},
)
overlay_json: str = Field(
    default="{}",
    sa_type=Text,
    sa_column_kwargs={"server_default": text("'{}'")},
)
```

- [ ] **Step 7: Add additive migrations/readiness**

In `db/migrations.py`, add `_ensure_column_exists` calls for:

```python
("authority_curation_attempts", "contract_version", "VARCHAR DEFAULT 'authority_curation.v1' NOT NULL")
("authority_curation_attempts", "menu_fingerprint", "VARCHAR")
("authority_curation_attempts", "selection_fingerprint", "VARCHAR")
("authority_curation_attempts", "rejected_selection_json", "TEXT DEFAULT '{}' NOT NULL")
("authority_curation_attempts", "overlay_json", "TEXT DEFAULT '{}' NOT NULL")
```

In `services/agent_workbench/schema_readiness.py`, add the same columns to the `authority_curation_attempts` readiness requirement.

- [ ] **Step 8: Run Task 1 tests**

Run:

```bash
uv run pytest tests/test_agent_workbench_error_codes.py::test_authority_repair_v2_error_codes_are_registered tests/test_authority_curation_models.py tests/test_agent_workbench_schema_readiness.py tests/test_db_migrations.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit Task 1**

```bash
git add services/agent_workbench/error_codes.py models/authority_curation.py db/migrations.py services/agent_workbench/schema_readiness.py tests/test_agent_workbench_error_codes.py tests/test_authority_curation_models.py tests/test_agent_workbench_schema_readiness.py tests/test_db_migrations.py
git commit -m "feat: add authority curation v2 persistence contract"
```

## Task 2: V2 Repair Selection Schemas And Prompt Contract

**Files:**
- Modify: `orchestrator_agent/agent_tools/authority_curation/schemes.py`
- Modify: `orchestrator_agent/agent_tools/authority_curation/agent.py`
- Test: `tests/test_authority_curation_agent.py`

- [ ] **Step 1: Add failing schema tests**

Add to `tests/test_authority_curation_agent.py`:

```python
def test_repair_selection_payload_rejects_model_authored_targets() -> None:
    payload = {
        "repairs": [
            {
                "feedback_id": "AFB-1",
                "target_handle": "R1",
                "target_id": "INV-1",
                "repair_kind": "replace_text",
                "replacement_text": "Safer wording.",
            }
        ]
    }

    with pytest.raises(ValidationError):
        AuthorityCurationRepairSelectionPayload.model_validate(payload)


def test_repair_selection_payload_accepts_replace_text() -> None:
    payload = AuthorityCurationRepairSelectionPayload.model_validate(
        {
            "repairs": [
                {
                    "feedback_id": "AFB-1",
                    "target_handle": "R1",
                    "repair_kind": "replace_text",
                    "replacement_text": "Safer wording.",
                }
            ]
        }
    )

    assert payload.repairs[0].target_handle == "R1"
    assert payload.repairs[0].replacement_text == "Safer wording."


def test_repair_selection_payload_rejects_replace_text_without_text() -> None:
    with pytest.raises(ValidationError):
        AuthorityCurationRepairSelectionPayload.model_validate(
            {
                "repairs": [
                    {
                        "feedback_id": "AFB-1",
                        "target_handle": "R1",
                        "repair_kind": "replace_text",
                    }
                ]
            }
        )
```

- [ ] **Step 2: Run schema tests and verify failure**

Run:

```bash
uv run pytest tests/test_authority_curation_agent.py -q -k 'repair_selection_payload'
```

Expected: FAIL because v2 schema classes do not exist.

- [ ] **Step 3: Add v2 schemas**

In `orchestrator_agent/agent_tools/authority_curation/schemes.py`, add:

```python
class AuthorityCurationRepairMenuEntry(_StrictModel):
    """One host-minted repair option for a blocking feedback item."""

    handle: str = Field(min_length=1)
    feedback_id: str = Field(min_length=1)
    target_kind: Literal["invariant", "assumption", "gap"]
    target_id: str = Field(min_length=1)
    target_field: str = Field(min_length=1)
    target_review_label: str = Field(min_length=1)
    overlay_target_key: str = Field(min_length=1)
    allowed_repair_kinds: list[Literal["replace_text", "mark_unresolvable"]]
    target_content_hash: str | None = Field(default=None, min_length=1)


class AuthorityCurationRepairSelection(_StrictModel):
    """One model selection from the host repair menu."""

    feedback_id: str = Field(min_length=1)
    target_handle: str = Field(min_length=1)
    repair_kind: Literal["replace_text", "mark_unresolvable"]
    replacement_text: str | None = Field(default=None, min_length=1)
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _require_payload_for_repair_kind(self) -> Self:
        if self.repair_kind == "replace_text" and not self.replacement_text:
            msg = "replacement_text is required when repair_kind is replace_text"
            raise ValueError(msg)
        if self.repair_kind == "mark_unresolvable" and not self.reason:
            msg = "reason is required when repair_kind is mark_unresolvable"
            raise ValueError(msg)
        return self


class AuthorityCurationRepairSelectionPayload(_StrictModel):
    """Repair selections emitted by the ADK repair node."""

    repairs: list[AuthorityCurationRepairSelection] = Field(default_factory=list)
```

Extend `AuthorityCurationWorkflowInput`:

```python
repair_menu: list[AuthorityCurationRepairMenuEntry] = Field(default_factory=list)
contract_version: Literal["authority_curation.v1", "authority_curation.v2"] = "authority_curation.v1"
```

For v2 repair output, either replace `AuthorityCurationRepairOutput` fields or add:

```python
selection_payload: AuthorityCurationRepairSelectionPayload | None = None
```

Keep v1 patch fields temporarily for read-only compatibility until later tasks hard-reject them under v2.

- [ ] **Step 4: Update agent prompt contract**

In `orchestrator_agent/agent_tools/authority_curation/agent.py`, update repair compiler instruction with this exact contract language:

```text
For authority_curation.v2 inputs, return only RepairSelectionPayload selections.
Pick target_handle values exactly from the repair_menu.
Do not emit target_id, target_kind, op, path, value, patches, or candidate_authority_json.
Use replace_text only when a menu entry allows replace_text.
Use mark_unresolvable with a reason when feedback cannot be safely repaired from the menu.
```

Add/adjust prompt tests to assert those strings are present.

- [ ] **Step 5: Run Task 2 tests**

Run:

```bash
uv run pytest tests/test_authority_curation_agent.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add orchestrator_agent/agent_tools/authority_curation/schemes.py orchestrator_agent/agent_tools/authority_curation/agent.py tests/test_authority_curation_agent.py
git commit -m "feat: add authority repair selection schema"
```

## Task 3: Repair Menu Builder

**Files:**
- Modify: `services/agent_workbench/authority_curation.py`
- Test: `tests/test_agent_workbench_authority_curation.py`

- [ ] **Step 1: Add failing menu builder tests**

Add tests:

```python
def test_authority_curate_builds_text_repair_menu_from_feedback(engine: Engine) -> None:
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)

    with Session(engine) as session:
        loaded = curation_mod._load_curation_inputs(
            session=session,
            request=AuthorityCurationRequest(
                project_id=fixture.project_id,
                spec_version_id=fixture.spec_version_id,
                source_authority_id=fixture.authority_id,
                expected_source_authority_fingerprint=fixture.authority_fingerprint,
                feedback_attempt_id=fixture.feedback_attempt_id,
                idempotency_key="menu-build",
            ),
        )

    menu = curation_mod._build_repair_menu(
        source_authority_json=loaded.source_authority_json,
        feedback_json=loaded.feedback_json,
    )

    assert [item["handle"] for item in menu] == ["R1"]
    assert menu[0]["feedback_id"] == "AFB-curation-1"
    assert menu[0]["target_kind"] == "invariant"
    assert menu[0]["target_id"] == "INV-curation-1"
    assert menu[0]["target_field"] == "text"
    assert menu[0]["allowed_repair_kinds"] == ["replace_text", "mark_unresolvable"]


def test_repair_menu_marks_parameter_feedback_not_repairable(engine: Engine) -> None:
    source_authority_json = {
        "invariants": [
            {
                "id": "INV-1",
                "source_item_id": "REQ-1",
                "text": "Old text.",
                "parameters": {"rule": "old"},
            }
        ],
        "assumptions": [],
        "gaps": [],
    }
    feedback_json = {
        "feedback_items": [
            {
                "feedback_id": "AFB-param",
                "target_kind": "invariant",
                "target_id": "INV-1",
                "issue_type": "parameter_correction",
                "severity": "blocking",
                "instruction": "Change /parameters/rule.",
            }
        ]
    }

    menu = curation_mod._build_repair_menu(
        source_authority_json=source_authority_json,
        feedback_json=feedback_json,
    )

    assert menu == [
        {
            "handle": "R1",
            "feedback_id": "AFB-param",
            "target_kind": "invariant",
            "target_id": "INV-1",
            "target_field": "text",
            "target_review_label": "INV-1",
            "overlay_target_key": "REQ-1:invariant:text:0",
            "allowed_repair_kinds": ["mark_unresolvable"],
            "target_content_hash": curation_mod._content_hash("Old text."),
            "not_repairable_reason": "structural_repair_deferred",
        }
    ]
```

- [ ] **Step 2: Run menu tests and verify failure**

Run:

```bash
uv run pytest tests/test_agent_workbench_authority_curation.py -q -k 'repair_menu'
```

Expected: FAIL because `_build_repair_menu` does not exist.

- [ ] **Step 3: Implement menu helper functions**

In `services/agent_workbench/authority_curation.py`, add helpers near existing patch helpers:

```python
def _build_repair_menu(
    *,
    source_authority_json: dict[str, Any],
    feedback_json: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build host-minted repair handles for blocking feedback."""
    items = feedback_json.get("feedback_items")
    if not isinstance(items, list):
        return []
    menu: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("severity") != "blocking":
            continue
        target_kind = _string_or_none(item.get("target_kind"))
        target_id = _string_or_none(item.get("target_id"))
        feedback_id = _string_or_none(item.get("feedback_id"))
        if feedback_id is None or target_kind is None or target_id is None:
            continue
        target = _find_patch_target(
            source_authority_json,
            target_kind=target_kind,
            target_id=target_id,
        )
        if target is None:
            continue
        _, index, target_item = target
        target_field = _repairable_text_field(target_item)
        if target_field is None:
            continue
        target_text = _target_text_value(target_item, target_field=target_field)
        repair_kinds = _allowed_repair_kinds_for_feedback(item)
        menu.append(
            {
                "handle": f"R{len(menu) + 1}",
                "feedback_id": feedback_id,
                "target_kind": target_kind,
                "target_id": target_id,
                "target_field": target_field,
                "target_review_label": target_id,
                "overlay_target_key": _overlay_target_key(
                    target_kind=target_kind,
                    target_id=target_id,
                    target_field=target_field,
                    index=index,
                    target_item=target_item,
                ),
                "allowed_repair_kinds": repair_kinds,
                "target_content_hash": _content_hash(target_text),
            }
        )
    return menu
```

Add deterministic text-field and hash helpers:

```python
def _repairable_text_field(target_item: object) -> str | None:
    if isinstance(target_item, str):
        return "text"
    if not isinstance(target_item, dict):
        return None
    for field_name in ("text", "statement", "description"):
        if isinstance(target_item.get(field_name), str):
            return field_name
    return None


def _target_text_value(target_item: object, *, target_field: str) -> str:
    if isinstance(target_item, str):
        return target_item
    if isinstance(target_item, dict) and isinstance(target_item.get(target_field), str):
        return cast("str", target_item[target_field])
    return ""


def _content_hash(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()
```

Use existing import style. If `sha256` is not imported, add `from hashlib import sha256`.

Add `_allowed_repair_kinds_for_feedback`:

```python
def _allowed_repair_kinds_for_feedback(item: dict[Any, Any]) -> list[str]:
    issue_type = _string_or_none(item.get("issue_type")) or ""
    structural_markers = ("parameter", "missing", "coverage", "duplicate", "split", "remove")
    if any(marker in issue_type for marker in structural_markers):
        return ["mark_unresolvable"]
    return ["replace_text", "mark_unresolvable"]
```

- [ ] **Step 4: Run Task 3 tests**

Run:

```bash
uv run pytest tests/test_agent_workbench_authority_curation.py -q -k 'repair_menu'
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add services/agent_workbench/authority_curation.py tests/test_agent_workbench_authority_curation.py
git commit -m "feat: build authority repair menus"
```

## Task 4: V2 Repair Applier And Legacy Output Rejection

**Files:**
- Modify: `services/agent_workbench/authority_curation.py`
- Test: `tests/test_agent_workbench_authority_curation.py`

- [ ] **Step 1: Add failing v2 applier tests**

Add tests:

```python
def test_authority_curate_v2_applies_selected_text_handle(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {"fsm_state": "SETUP_REQUIRED", "setup_status": "authority_rejected"},
    )
    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        lambda **_: {
            "ok": True,
            "curation_attempt_id": "curation-v2-result",
            "project_id": fixture.project_id,
            "contract_version": "authority_curation.v2",
            "selection_payload": {
                "repairs": [
                    {
                        "feedback_id": "AFB-curation-1",
                        "target_handle": "R1",
                        "repair_kind": "replace_text",
                        "replacement_text": "Review packets include qualified guard evidence.",
                    }
                ]
            },
            "quality_report": {"status": "passed"},
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
            idempotency_key="curate-v2-selected-text",
        )
    )

    assert result["ok"] is True
    candidate = _latest_authority_artifact(engine)
    invariants = {item["id"]: item for item in candidate["invariants"]}
    assert invariants["INV-curation-1"]["text"] == (
        "Review packets include qualified guard evidence."
    )
    assert invariants["INV-curation-untargeted"]["text"] == (
        "Unrelated review packets remain stable."
    )


def test_authority_curate_v2_rejects_full_candidate_output(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {"fsm_state": "SETUP_REQUIRED", "setup_status": "authority_rejected"},
    )
    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        lambda **_: {
            "ok": True,
            "contract_version": "authority_curation.v2",
            "candidate_authority_json": json.loads(_compiled_artifact_json()),
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
            idempotency_key="curate-v2-rejects-full-candidate",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_REPAIR_INTENT_INVALID"
    assert result["errors"][0]["details"]["reason"] == "full_candidate_forbidden"
```

- [ ] **Step 2: Add failing malformed selection tests**

Add:

```python
def test_authority_curate_v2_rejects_unknown_handle(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {"fsm_state": "SETUP_REQUIRED", "setup_status": "authority_rejected"},
    )
    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        lambda **_: {
            "ok": True,
            "contract_version": "authority_curation.v2",
            "selection_payload": {
                "repairs": [
                    {
                        "feedback_id": "AFB-curation-1",
                        "target_handle": "R999",
                        "repair_kind": "replace_text",
                        "replacement_text": "Unsafe.",
                    }
                ]
            },
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
            idempotency_key="curate-v2-unknown-handle",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_REPAIR_INTENT_INVALID"
    assert result["errors"][0]["details"]["reason"] == "target_handle_unknown"
```

- [ ] **Step 3: Run v2 applier tests and verify failure**

Run:

```bash
uv run pytest tests/test_agent_workbench_authority_curation.py -q -k 'v2'
```

Expected: FAIL because v2 output is not handled yet.

- [ ] **Step 4: Implement v2 output routing**

In `services/agent_workbench/authority_curation.py`, add:

```python
AUTHORITY_CURATION_CONTRACT_V2 = "authority_curation.v2"
```

Add helper:

```python
def _candidate_authority_from_v2_selection(
    *,
    context: _PatchApplicationContext,
    loaded: _LoadedCurationInputs,
    workflow_result: dict[str, Any],
) -> dict[str, Any]:
    if workflow_result.get("candidate_authority_json") is not None:
        return _authority_repair_intent_invalid_response(
            context=context,
            reason="full_candidate_forbidden",
        )
    if workflow_result.get("patches") is not None:
        return _authority_repair_intent_invalid_response(
            context=context,
            reason="legacy_patch_forbidden",
        )
    menu = _build_repair_menu(
        source_authority_json=loaded.source_authority_json,
        feedback_json=loaded.feedback_json,
    )
    payload = _json_object_from_value(workflow_result.get("selection_payload"))
    if payload is None:
        return _authority_repair_intent_invalid_response(
            context=context,
            reason="selection_payload_missing",
        )
    return _apply_repair_selections(
        context=context,
        source_authority_json=loaded.source_authority_json,
        repair_menu=menu,
        selection_payload=payload,
    )
```

Modify `_candidate_authority_from_workflow_result`:

```python
if workflow_result.get("contract_version") == AUTHORITY_CURATION_CONTRACT_V2:
    return _candidate_authority_from_v2_selection(
        context=context,
        loaded=loaded,
        workflow_result=workflow_result,
    )
```

Keep current v1 path after that branch for existing tests and compatibility.

- [ ] **Step 5: Implement selection applier**

Add:

```python
def _apply_repair_selections(
    *,
    context: _PatchApplicationContext,
    source_authority_json: dict[str, Any],
    repair_menu: list[dict[str, Any]],
    selection_payload: dict[str, Any],
) -> dict[str, Any]:
    candidate = deepcopy(source_authority_json)
    menu_by_handle = {str(item["handle"]): item for item in repair_menu}
    repairs = selection_payload.get("repairs")
    if not isinstance(repairs, list):
        return _authority_repair_intent_invalid_response(
            context=context,
            reason="repairs_not_list",
        )
    for index, repair in enumerate(repairs):
        if not isinstance(repair, dict):
            return _authority_repair_intent_invalid_response(
                context=context,
                reason="invalid_repair",
                repair_index=index,
            )
        error = _apply_one_repair_selection(
            candidate=candidate,
            menu_by_handle=menu_by_handle,
            repair=repair,
            repair_index=index,
        )
        if error is not None:
            return _authority_repair_intent_invalid_response(
                context=context,
                reason=error["reason"],
                repair_index=index,
                patch_details=error.get("details"),
            )
    return candidate
```

Add `_apply_one_repair_selection` to validate:

- `target_handle` exists in menu.
- `feedback_id` matches menu entry.
- `repair_kind` is allowed.
- `replace_text` has string `replacement_text`.
- target exists in candidate.
- only menu entry `target_field` is changed.

- [ ] **Step 6: Run Task 4 tests**

Run:

```bash
uv run pytest tests/test_agent_workbench_authority_curation.py -q -k 'v2 or repair_menu'
```

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```bash
git add services/agent_workbench/authority_curation.py tests/test_agent_workbench_authority_curation.py
git commit -m "feat: apply authority repair menu selections"
```

## Task 5: Diff Invariants, Assumptions, And Gaps

**Files:**
- Modify: `services/specs/authority_curation_diff.py`
- Modify: `services/agent_workbench/authority_curation.py`
- Test: `tests/test_agent_workbench_authority_curation.py`

- [ ] **Step 1: Add failing assumption/gap diff tests**

Add:

```python
def test_authority_diff_detects_untargeted_assumption_change() -> None:
    source = json.loads(_compiled_artifact_json())
    candidate = json.loads(_compiled_artifact_json())
    candidate["assumptions"][1]["text"] = "Unrelated assumption changed."

    diff = build_authority_diff(
        source_authority_json=source,
        candidate_authority_json=candidate,
        targeted_source_item_ids=set(),
        targeted_collection_keys={
            "invariants": set(),
            "assumptions": {"ASM-curation-1"},
            "gaps": set(),
        },
    )

    assert diff["summary"]["untargeted_change_count"] == 1
    assert diff["untargeted_changes"][0]["collection"] == "assumptions"


def test_authority_diff_allows_targeted_assumption_change() -> None:
    source = json.loads(_compiled_artifact_json())
    candidate = json.loads(_compiled_artifact_json())
    candidate["assumptions"][0]["text"] = "Report contexts are examples."

    diff = build_authority_diff(
        source_authority_json=source,
        candidate_authority_json=candidate,
        targeted_source_item_ids=set(),
        targeted_collection_keys={
            "invariants": set(),
            "assumptions": {"ASM-curation-1"},
            "gaps": set(),
        },
    )

    assert diff["summary"]["untargeted_change_count"] == 0
    assert diff["collections"]["assumptions"]["changed_ids"] == ["ASM-curation-1"]
```

- [ ] **Step 2: Run diff tests and verify failure**

Run:

```bash
uv run pytest tests/test_agent_workbench_authority_curation.py -q -k 'authority_diff'
```

Expected: FAIL because `build_authority_diff` lacks `targeted_collection_keys` and assumptions/gaps handling.

- [ ] **Step 3: Generalize diff helpers**

In `services/specs/authority_curation_diff.py`, keep current invariant behavior but add a new optional parameter:

```python
def build_authority_diff(
    *,
    source_authority_json: JsonDict,
    candidate_authority_json: JsonDict,
    targeted_source_item_ids: set[str],
    targeted_collection_keys: dict[str, set[str]] | None = None,
) -> JsonDict:
```

Add collection keying:

```python
def _collection_by_key(
    authority_json: JsonDict,
    *,
    authority: str,
    collection: str,
) -> dict[str, JsonDict]:
    value = authority_json.get(collection)
    if not isinstance(value, list):
        raise AuthorityDiffValidationError(
            [{"authority": authority, "collection": collection, "reason": "collection_not_list"}]
        )
    result: dict[str, JsonDict] = {}
    for index, item in enumerate(value):
        key, payload = _collection_item_key_and_payload(
            item,
            collection=collection,
            index=index,
        )
        if key in result:
            raise AuthorityDiffValidationError(
                [{"authority": authority, "collection": collection, "duplicate_id": key, "reason": "duplicate_id"}]
            )
        result[key] = payload
    return result
```

Key rules:

- `invariants`: dict `id`
- `assumptions`: dict `assumption_id` or `id`; string fallback `ASM-{index + 1}`
- `gaps`: dict `gap_id` or `id`; string fallback `GAP-{index + 1}`

Return existing top-level invariant fields for compatibility and add:

```python
"collections": {
    "invariants": {
        "changed_ids": [],
        "added_ids": [],
        "removed_ids": [],
    },
    "assumptions": {
        "changed_ids": [],
        "added_ids": [],
        "removed_ids": [],
    },
    "gaps": {
        "changed_ids": [],
        "added_ids": [],
        "removed_ids": [],
    },
}
```

- [ ] **Step 4: Update curation validation to pass collection allowlists**

In `services/agent_workbench/authority_curation.py`, derive targeted collection keys from selected menu handles:

```python
def _targeted_collection_keys_from_menu(
    *,
    repair_menu: list[dict[str, Any]],
    selection_payload: dict[str, Any],
) -> dict[str, set[str]]:
    keys = {"invariants": set(), "assumptions": set(), "gaps": set()}
    by_handle = {str(item["handle"]): item for item in repair_menu}
    for repair in selection_payload.get("repairs", []):
        if not isinstance(repair, dict):
            continue
        menu_item = by_handle.get(str(repair.get("target_handle")))
        if menu_item is None:
            continue
        collection = _collection_name_for_target_kind(str(menu_item["target_kind"]))
        keys[collection].add(str(menu_item["target_id"]))
    return keys
```

- [ ] **Step 5: Run Task 5 tests**

Run:

```bash
uv run pytest tests/test_agent_workbench_authority_curation.py -q -k 'authority_diff or v2'
```

Expected: PASS.

- [ ] **Step 6: Commit Task 5**

```bash
git add services/specs/authority_curation_diff.py services/agent_workbench/authority_curation.py tests/test_agent_workbench_authority_curation.py
git commit -m "feat: bound authority curation diffs across collections"
```

## Task 6: Safe Per-Selection Trace And Idempotency Metadata

**Files:**
- Modify: `services/agent_workbench/authority_curation.py`
- Modify: `utils/authority_curation_trace.py`
- Test: `tests/test_authority_curation_trace.py`
- Test: `tests/test_agent_workbench_authority_curation.py`

- [ ] **Step 1: Add failing rejected-selection trace test**

In `tests/test_authority_curation_trace.py`, add:

```python
def test_authority_curation_trace_records_rejected_selection_vector(tmp_path: Path) -> None:
    writer = AuthorityCurationTraceWriter(root_dir=tmp_path)
    writer.record(
        mutation_event_id=660,
        project_id=3,
        step="repair_selection_rejected",
        status="failed",
        curation_attempt_id="curation-1",
        attributes={
            "feedback_id": "AFB-1",
            "target_handle": "R1",
            "target_kind": "assumption",
            "target_id": "ASM-11",
            "target_field": "text",
            "repair_kind": "replace_text",
            "reject_reason": "target_handle_unknown",
            "requested_model_id": "openrouter/deepseek/deepseek-v4-pro",
            "selection_fingerprint": "sha256:" + "a" * 64,
        },
    )

    rows = list(_read_trace_rows(tmp_path, mutation_event_id=660))
    assert rows[0]["attributes"]["reject_reason"] == "target_handle_unknown"
    assert "replacement_text" not in rows[0]["attributes"]
```

- [ ] **Step 2: Add failing idempotency metadata test**

In `tests/test_agent_workbench_authority_curation.py`, add:

```python
def test_authority_curate_v2_persists_menu_and_selection_fingerprints(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {"fsm_state": "SETUP_REQUIRED", "setup_status": "authority_rejected"},
    )
    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        lambda **_: {
            "ok": True,
            "contract_version": "authority_curation.v2",
            "selection_payload": {
                "repairs": [
                    {
                        "feedback_id": "AFB-curation-1",
                        "target_handle": "R1",
                        "repair_kind": "replace_text",
                        "replacement_text": "Review packets include qualified guard evidence.",
                    }
                ]
            },
            "quality_report": {"status": "passed"},
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
            idempotency_key="curate-v2-fingerprints",
        )
    )

    assert result["ok"] is True
    with Session(engine) as session:
        attempt = session.exec(select(AuthorityCurationAttempt)).one()
    assert attempt.contract_version == "authority_curation.v2"
    assert attempt.menu_fingerprint.startswith("sha256:")
    assert attempt.selection_fingerprint.startswith("sha256:")
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
uv run pytest tests/test_authority_curation_trace.py tests/test_agent_workbench_authority_curation.py -q -k 'rejected_selection_vector or fingerprints'
```

Expected: FAIL until fields are persisted.

- [ ] **Step 4: Implement fingerprint helpers**

In `services/agent_workbench/authority_curation.py`, add:

```python
def _fingerprint_json(value: object) -> str:
    return "sha256:" + sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
```

During v2 flow:

- compute `menu_fingerprint = _fingerprint_json(repair_menu)` before ADK invocation;
- pass repair menu and contract version to workflow input;
- compute `selection_fingerprint = _fingerprint_json(selection_payload)` after model output;
- store both on `AuthorityCurationAttempt`;
- store rejected selection vector when validation fails.

- [ ] **Step 5: Implement safe trace fields**

Where v2 selection validation rejects, emit a trace event:

```python
_record_trace_event(
    mutation_event_id=mutation_event_id,
    project_id=request.project_id,
    curation_attempt_id=attempt.curation_attempt_id,
    step="repair_selection_rejected",
    status="failed",
    attributes={
        "feedback_id": feedback_id,
        "target_handle": target_handle,
        "target_kind": target_kind,
        "target_id": target_id,
        "target_field": target_field,
        "repair_kind": repair_kind,
        "reject_reason": reason,
        "requested_model_id": request.compiler_model,
        "selection_fingerprint": selection_fingerprint,
    },
)
```

Do not include `replacement_text` in trace attributes.

- [ ] **Step 6: Run Task 6 tests**

Run:

```bash
uv run pytest tests/test_authority_curation_trace.py tests/test_agent_workbench_authority_curation.py -q -k 'rejected_selection_vector or fingerprints or v2'
```

Expected: PASS.

- [ ] **Step 7: Commit Task 6**

```bash
git add services/agent_workbench/authority_curation.py utils/authority_curation_trace.py tests/test_authority_curation_trace.py tests/test_agent_workbench_authority_curation.py
git commit -m "feat: trace authority repair selections"
```

## Task 7: ADK Workflow Integration And Kill Switch

**Files:**
- Modify: `services/agent_workbench/authority_curation.py`
- Modify: `orchestrator_agent/agent_tools/authority_curation/agent.py`
- Test: `tests/test_agent_workbench_authority_curation.py`
- Test: `tests/test_authority_curation_agent.py`

- [ ] **Step 1: Add failing workflow-input test**

Add to `tests/test_agent_workbench_authority_curation.py`:

```python
def test_authority_curate_v2_passes_repair_menu_to_workflow(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    captured: dict[str, object] = {}

    def fake_workflow(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "ok": True,
            "contract_version": "authority_curation.v2",
            "selection_payload": {
                "repairs": [
                    {
                        "feedback_id": "AFB-curation-1",
                        "target_handle": "R1",
                        "repair_kind": "replace_text",
                        "replacement_text": "Review packets include qualified guard evidence.",
                    }
                ]
            },
            "quality_report": {"status": "passed"},
        }

    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        fake_workflow,
    )
    fake_workflow_port = FakeWorkflowPort()
    fake_workflow_port.update_session_status(
        str(fixture.project_id),
        {"fsm_state": "SETUP_REQUIRED", "setup_status": "authority_rejected"},
    )
    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow_port)

    result = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-v2-menu-input",
        )
    )

    assert result["ok"] is True
    assert captured["contract_version"] == "authority_curation.v2"
    assert captured["repair_menu"][0]["handle"] == "R1"
```

- [ ] **Step 2: Add failing kill-switch test**

Add:

```python
def test_authority_curate_kill_switch_returns_fail_no_candidate(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    monkeypatch.setenv("AGILEFORGE_AUTHORITY_CURATION_MODE", "fail_no_candidate")
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {"fsm_state": "SETUP_REQUIRED", "setup_status": "authority_rejected"},
    )
    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)

    result = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-v2-kill-switch",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "SPEC_COMPILE_FAILED"
    assert result["errors"][0]["details"]["failure_reason"] == "fail_no_candidate"
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
uv run pytest tests/test_agent_workbench_authority_curation.py tests/test_authority_curation_agent.py -q -k 'menu_input or kill_switch or repair_selection'
```

Expected: FAIL until workflow call and kill switch are wired.

- [ ] **Step 4: Pass repair menu into workflow**

In `AuthorityCurationRunner.curate`, after loading inputs and before ADK invocation:

```python
repair_menu = _build_repair_menu(
    source_authority_json=loaded.source_authority_json,
    feedback_json=loaded.feedback_json,
)
menu_fingerprint = _fingerprint_json(repair_menu)
```

Pass to `run_authority_curation_workflow`:

```python
contract_version=AUTHORITY_CURATION_CONTRACT_V2,
repair_menu=repair_menu,
menu_fingerprint=menu_fingerprint,
```

Persist `attempt.contract_version` and `attempt.menu_fingerprint` before invocation when possible.

- [ ] **Step 5: Implement kill switch**

Add config helper:

```python
def _authority_curation_mode() -> str:
    return os.getenv("AGILEFORGE_AUTHORITY_CURATION_MODE", "repair_menu")
```

If mode is `fail_no_candidate`, return a curation failure envelope before ADK invocation:

```python
return _curation_failed_response(
    request=request,
    attempt=attempt,
    failure_reason="fail_no_candidate",
    mutation_event_id=mutation_event_id,
)
```

Use existing failure response helper if present; keep output consistent with curation gate failures.

- [ ] **Step 6: Run Task 7 tests**

Run:

```bash
uv run pytest tests/test_agent_workbench_authority_curation.py tests/test_authority_curation_agent.py -q -k 'menu_input or kill_switch or repair_selection or v2'
```

Expected: PASS.

- [ ] **Step 7: Commit Task 7**

```bash
git add services/agent_workbench/authority_curation.py orchestrator_agent/agent_tools/authority_curation/agent.py tests/test_agent_workbench_authority_curation.py tests/test_authority_curation_agent.py
git commit -m "feat: route authority curation through repair menu"
```

## Task 8: Replay Fixture Coverage For ASA Failures

**Files:**
- Modify: `tests/test_agent_workbench_authority_curation.py`

- [ ] **Step 1: Add replay tests for all observed legacy shapes**

Add a test table or separate tests covering:

```python
@pytest.mark.parametrize(
    ("workflow_result", "reason"),
    [
        (
            {
                "ok": True,
                "contract_version": "authority_curation.v2",
                "patches": [
                    {
                        "target_kind": "assumption",
                        "target_id": "authority:7",
                        "op": "replace_text",
                        "new_text": "bad",
                    }
                ],
            },
            "legacy_patch_forbidden",
        ),
        (
            {
                "ok": True,
                "contract_version": "authority_curation.v2",
                "patches": [
                    {
                        "target_kind": "assumption",
                        "target_id": "assumptions[10]",
                        "op": "replace_text",
                        "new_text": "bad",
                    }
                ],
            },
            "legacy_patch_forbidden",
        ),
        (
            {
                "ok": True,
                "contract_version": "authority_curation.v2",
                "patches": [
                    {
                        "target_kind": "assumption",
                        "target_id": "ASM-11",
                        "op": "replace_value",
                        "path": "$.source_authority_json.assumptions[10]",
                        "value": "bad",
                    }
                ],
            },
            "legacy_patch_forbidden",
        ),
        (
            {
                "ok": True,
                "contract_version": "authority_curation.v2",
                "candidate_authority_json": {"invariants": [], "assumptions": [], "gaps": []},
            },
            "full_candidate_forbidden",
        ),
    ],
)
def test_authority_curate_v2_replays_legacy_failure_shapes(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    workflow_result: dict[str, object],
    reason: str,
) -> None:
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {"fsm_state": "SETUP_REQUIRED", "setup_status": "authority_rejected"},
    )
    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        lambda **_: workflow_result,
    )
    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)

    result = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key=f"curate-v2-replay-{reason}",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["details"]["reason"] == reason
```

- [ ] **Step 2: Add unresolved feedback fixture**

Add:

```python
def test_authority_curate_v2_fails_gate_when_blocking_feedback_omitted(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {"fsm_state": "SETUP_REQUIRED", "setup_status": "authority_rejected"},
    )
    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        lambda **_: {
            "ok": True,
            "contract_version": "authority_curation.v2",
            "selection_payload": {"repairs": []},
            "quality_report": {"status": "passed"},
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
            idempotency_key="curate-v2-unresolved-feedback",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "SPEC_COMPILE_FAILED"
```

- [ ] **Step 3: Run replay fixture tests**

Run:

```bash
uv run pytest tests/test_agent_workbench_authority_curation.py -q -k 'v2_replays or unresolved_feedback or untargeted_assumption'
```

Expected: PASS.

- [ ] **Step 4: Commit Task 8**

```bash
git add tests/test_agent_workbench_authority_curation.py
git commit -m "test: cover authority curation repair menu replay cases"
```

## Task 9: Full Verification

**Files:**
- Verify all touched files.

- [ ] **Step 1: Run focused curation suites**

Run:

```bash
uv run pytest tests/test_authority_curation_agent.py tests/test_agent_workbench_authority_curation.py tests/test_authority_curation_models.py tests/test_authority_curation_trace.py -q
```

Expected: PASS.

- [ ] **Step 2: Run migration/readiness tests**

Run:

```bash
uv run pytest tests/test_db_migrations.py tests/test_agent_workbench_schema_readiness.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full gate**

Run:

```bash
pyrepo-check --all
```

Expected: all checks pass. No suppression.

- [ ] **Step 4: Review diff**

Run:

```bash
git diff --stat
git diff -- services/agent_workbench/authority_curation.py | sed -n '1,260p'
git diff -- services/specs/authority_curation_diff.py | sed -n '1,220p'
```

Expected: only authority curation v2 implementation and tests changed.

- [ ] **Step 5: Final commit if needed**

If any verification-only fixes were required:

```bash
git add .
git commit -m "fix: complete authority repair menu verification"
```

## Task 10: Post-Merge ASA Retry Instructions

**Files:**
- No code changes.

- [ ] **Step 1: Confirm AgileForge master is clean and pushed**

Run from `/Users/aaat/projects/agileforge`:

```bash
git status --short --branch
git rev-parse --short HEAD
```

Expected: `master...origin/master`, no changes.

- [ ] **Step 2: Ask ASA agent to run only one fresh curation attempt**

Use a fresh idempotency key. Do not run both default and GPT-5-mini unless the
first attempt fails before publishing a candidate.

Default model command:

```bash
cd /Users/aaat/projects/asa-deep-process-control-experiments

UV_CACHE_DIR=/private/tmp/uv-cache-agileforge agileforge authority curate \
  --project-id 3 \
  --spec-version-id 4 \
  --source-authority-id 7 \
  --expected-source-authority-fingerprint sha256:fd955ef3c60e59399e498acc4eb18d88fdc029c20958a0bfd7798c163a14137d \
  --feedback-attempt-id feedback-775be364-23ef-469f-a854-3ffb6e1d35dd \
  --idempotency-key asa-authority-7-curate-014-repair-menu-default
```

Expected if successful: `ok=true`, candidate authority id/fingerprint returned, setup status moves to `authority_pending_review`.

- [ ] **Step 3: Stop at human review**

If curation publishes a candidate, run only:

```bash
agileforge authority review --project-id 3 --open
```

Do not accept/reject until human review completes.

## Plan Self-Review

- Spec coverage:
  - Host-minted repair menu: Tasks 2, 3, 7.
  - No model target/path/full candidate: Tasks 2, 4, 8.
  - Diff invariants/assumptions/gaps: Task 5.
  - Safe trace/idempotency: Task 6.
  - Overlay metadata and lineage reason vocabulary: Tasks 1, 4, 6.
  - Replay fixtures before ASA retry: Task 8.
  - Human review preserved: Task 10.
- Placeholder scan:
  - No task contains open placeholders. Deferred v2.1 scope is explicit.
- Type consistency:
  - `RepairMenuEntry` uses `target_field`.
  - `RepairSelectionPayload` uses only `repairs`.
  - `selection_fingerprint` is post-model; `menu_fingerprint` is pre-model.
