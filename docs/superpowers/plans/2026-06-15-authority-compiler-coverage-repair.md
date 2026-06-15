# Authority Compiler Coverage Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make authority compilation recover from `MISSING_ACCEPTED_MUST_AUTHORITY` by running one explicit feedback-backed repair attempt per missing `MUST`/`MUST_NOT` item, while failing closed if repair introduces new validation errors.

**Architecture:** Keep the deterministic validator as source of truth. Full compile and current focused item passes still run first; if merged output omits accepted high-priority items, run a single final focused repair per missing item with explicit coverage feedback. Scope-extension base-authority reuse is added after the repair loop by storing base metadata in the amended spec marker and merging accepted base authority with compiled extension items.

**Tech Stack:** Python 3.13, SQLModel, Google ADK runner, LiteLlm-backed compiler agent, Pydantic authority schemas, `uv run --frozen pytest`.

---

## Design Corrections From Review

- Simple rerun is forbidden. Coverage repair prompt must include explicit feedback: previous output failed to cover `<item_id>`, and repair must emit an invariant with `source_item_id=<item_id>` or an explicit gap mentioning `<item_id>`.
- Repair loops are capped. Each missing item gets at most one coverage repair attempt. If that attempt fails schema validation, source metadata validation, or coverage validation, compilation fails closed. Do not enter metadata-repair then coverage-repair loops.
- Scope extension should avoid recompiling unchanged accepted base scope where possible. Store enough metadata on amended `SpecRegistry.approval_notes` to identify the accepted base spec, then merge accepted base authority with extension-only authority.

## File Map

- Modify `services/specs/compiler_service.py`
  - Add coverage-repair feedback helpers.
  - Add one-pass missing-coverage repair after merged structured coverage validation.
  - Prevent coverage repair failures from entering source-metadata repair.
  - Add scope-extension base-authority merge helpers.
- Modify `services/agent_workbench/scope_extension.py`
  - Persist `base_spec_version_id`, `base_spec_hash`, and `added_source_item_ids` in the amended spec recovery marker.
- Modify `tests/test_specs_compiler_service.py`
  - Add coverage repair success, explicit feedback, hard cap, and fail-closed tests.
  - Add scope-extension base authority reuse tests.
- Modify `tests/test_agent_workbench_phase1_integration.py`
  - Add or update one end-to-end scope-extension compile assertion if existing fixtures make this cheap.
- Modify `docs/agent-cli-manual.md`
  - Add bounded remediation note for `MISSING_ACCEPTED_MUST_AUTHORITY`.

---

### Task 1: Pin Coverage Repair Behavior With Tests

**Files:**
- Modify: `tests/test_specs_compiler_service.py`

- [ ] **Step 1: Add test helper constants**

Add near existing constants at top of `tests/test_specs_compiler_service.py`:

```python
_EXPECTED_COVERAGE_REPAIR_CALLS = 5
_EXPECTED_COVERAGE_REPAIR_FAIL_FAST_CALLS = 4
```

- [ ] **Step 2: Add failing test for explicit feedback-backed coverage repair**

Add after `test_preview_spec_authority_rejects_unaccounted_iterative_must_items`:

```python
def test_preview_spec_authority_repairs_missing_coverage_with_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing MUST/MUST_NOT coverage gets one explicit focused repair pass."""
    from services.specs import compiler_service  # noqa: PLC0415

    calls: list[dict[str, object]] = []

    def fake_compiler(**kwargs: object) -> str:
        spec_content = kwargs["spec_content"]
        assert isinstance(spec_content, str)
        domain_hint = kwargs.get("domain_hint")
        payload = json.loads(spec_content)
        item_ids = [item["id"] for item in payload["items"]]
        calls.append({"item_ids": item_ids, "domain_hint": domain_hint})
        if domain_hint and "failed structured coverage validation" in str(domain_hint):
            item_id = item_ids[0]
            source_level = payload["items"][0]["level"]
            assert f"missing source_item_id: {item_id}" in str(domain_hint)
            assert "single repair attempt" in str(domain_hint)
            return _behavioral_payload_json(
                source_item_id=cast("str", item_id),
                source_level=cast("SpecAuthoritySourceLevel", source_level),
            )
        return _compiled_success_json()

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_compiler,
    )

    result = compiler_service.preview_spec_authority(
        {"content": _accepted_multi_item_spec_profile_json()},
        tool_context=make_tool_context(),
    )

    assert result["success"] is True
    assert len(calls) == _EXPECTED_COVERAGE_REPAIR_CALLS
    repair_hints = [
        str(call["domain_hint"])
        for call in calls
        if call["domain_hint"] is not None
    ]
    assert any("missing source_item_id: REQ.todo-create" in hint for hint in repair_hints)
    assert any("missing source_item_id: REQ.todo-toggle" in hint for hint in repair_hints)
```

- [ ] **Step 3: Add failing test for hard cap and fail-closed behavior**

Add after the previous test:

```python
def test_preview_spec_authority_coverage_repair_fails_closed_on_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coverage repair does not enter a second metadata repair loop."""
    from services.specs import compiler_service  # noqa: PLC0415

    calls: list[str | None] = []

    def fake_compiler(**kwargs: object) -> str:
        domain_hint = cast("str | None", kwargs.get("domain_hint"))
        calls.append(domain_hint)
        if domain_hint and "failed structured coverage validation" in domain_hint:
            return _source_metadata_failure_json(
                source_item_id="REQ.todo-create",
                invariant_id="INV-badbadbadbadbad1",
            )
        return _compiled_success_json()

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_compiler,
    )

    result = compiler_service.preview_spec_authority(
        {"content": _accepted_multi_item_spec_profile_json()},
        tool_context=make_tool_context(),
    )

    assert result["success"] is False
    assert result["details"]["error"] == "STRUCTURED_ITEM_COMPILATION_FAILED"
    assert result["details"]["reason"] == "FOCUSED_ITEM_AUTHORITY_FAILED"
    assert len(calls) == _EXPECTED_COVERAGE_REPAIR_FAIL_FAST_CALLS
    assert sum(
        1
        for hint in calls
        if hint and "failed structured coverage validation" in hint
    ) == 1
```

- [ ] **Step 4: Run failing tests**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py -q -k "coverage_repair"
```

Expected: both new tests fail because no coverage-repair feedback path exists.

- [ ] **Step 5: Commit failing tests**

```bash
git add tests/test_specs_compiler_service.py
git commit -m "test: cover authority coverage repair feedback"
```

---

### Task 2: Implement One-Pass Coverage Repair

**Files:**
- Modify: `services/specs/compiler_service.py`

- [ ] **Step 1: Add coverage repair feedback helper**

Add near `_focused_repair_domain_hint`:

```python
def _coverage_repair_domain_hint(item_id: str) -> str:
    """Build explicit feedback for one missing structured coverage item."""
    lines = [
        "Your previous authority output failed structured coverage validation.",
        "",
        "Repair target:",
        f"- missing source_item_id: {item_id}",
        "",
        "This is the single repair attempt for this source item.",
        "Retry only this source item.",
        (
            "You must either emit at least one invariant with source_item_id "
            f"exactly {item_id}, or emit an explicit gap that mentions "
            f"{item_id} and explains why no runtime/product invariant maps."
        ),
        "Do not cite source item IDs other than the repair target.",
        "Do not invent source references, source levels, or source excerpts.",
        "Return only valid compiled authority JSON.",
    ]
    return "\n".join(lines)
```

- [ ] **Step 2: Add one-pass repair helper**

Add after `_invoke_focused_structured_item_authority`:

```python
def _invoke_coverage_repair_authority(
    artifact: TechnicalSpecArtifact,
    *,
    item_id: str,
    product_id: int | None,
    spec_version_id: int | None,
    compiler_model: str | None = None,
) -> SpecAuthorityCompilationSuccess | _FocusedItemCompilationFailure:
    """Run exactly one explicit repair attempt for missing structured coverage."""
    focused_content = _focused_structured_spec_content(artifact, item_id=item_id)
    invocation = _invoke_and_normalize_spec_authority(
        spec_content=focused_content,
        content_ref=None,
        product_id=product_id,
        spec_version_id=spec_version_id,
        domain_hint=_coverage_repair_domain_hint(item_id),
        compiler_model=compiler_model,
    )
    if isinstance(invocation.output.root, SpecAuthorityCompilationFailure):
        return _FocusedItemCompilationFailure(
            item_id=item_id,
            failure=invocation.output.root,
        )
    return cast("SpecAuthorityCompilationSuccess", invocation.output.root)
```

- [ ] **Step 3: Add merge helper for missing coverage**

Add after `_structured_missing_authority_failure`:

```python
def _repair_missing_iterative_authority(
    *,
    artifact: TechnicalSpecArtifact,
    existing_successes: list[SpecAuthorityCompilationSuccess],
    missing_item_ids: list[str],
    product_id: int | None,
    spec_version_id: int | None,
    compiler_model: str | None,
) -> SpecAuthorityCompilerOutput:
    """Repair missing item coverage once and return success or fail-closed output."""
    repair_successes: list[SpecAuthorityCompilationSuccess] = []
    repair_failures: list[_FocusedItemCompilationFailure] = []
    for item_id in missing_item_ids:
        repaired = _invoke_coverage_repair_authority(
            artifact,
            item_id=item_id,
            product_id=product_id,
            spec_version_id=spec_version_id,
            compiler_model=compiler_model,
        )
        if isinstance(repaired, _FocusedItemCompilationFailure):
            repair_failures.append(repaired)
            return _structured_item_compilation_failure(
                repair_failures,
                missing_item_ids=missing_item_ids,
                total_item_count=len(_iterative_authority_item_ids(artifact)),
            )
        repair_successes.append(repaired)

    merged_output = normalize_compiler_output(
        SpecAuthorityCompilerOutput(
            root=_merge_compilation_successes([*existing_successes, *repair_successes])
        ).model_dump_json(),
        source_text=canonical_spec_json(artifact),
        source_format="agileforge.spec.v1",
    )
    if isinstance(merged_output.root, SpecAuthorityCompilationFailure):
        return merged_output
    remaining_missing = _missing_iterative_authority_item_ids(
        merged_output.root,
        item_ids=_iterative_authority_item_ids(artifact),
    )
    if remaining_missing:
        return _structured_missing_authority_failure(
            missing_item_ids=remaining_missing,
            focused_failures=[],
            total_item_count=len(_iterative_authority_item_ids(artifact)),
        )
    return merged_output
```

- [ ] **Step 4: Integrate after merged structured coverage failure**

In `_compile_spec_authority_output`, replace the current `if missing_item_ids:` return block with:

```python
    if missing_item_ids:
        repaired_output = _repair_missing_iterative_authority(
            artifact=artifact,
            existing_successes=successes,
            missing_item_ids=missing_item_ids,
            product_id=product_id,
            spec_version_id=spec_version_id,
            compiler_model=compiler_model,
        )
        return _NormalizedCompilerInvocation(
            raw_json=full_invocation.raw_json,
            output=repaired_output,
        )
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py -q -k "coverage_repair or unaccounted_iterative"
```

Expected: coverage repair tests pass. Existing unaccounted test may need expectation update because missing coverage is now repairable when fake compiler responds to feedback; keep the unaccounted test failing by making fake compiler ignore coverage feedback.

- [ ] **Step 6: Run broader compiler tests**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py -q
```

Expected: pass.

- [ ] **Step 7: Commit implementation**

```bash
git add services/specs/compiler_service.py tests/test_specs_compiler_service.py
git commit -m "feat: repair missing authority coverage with feedback"
```

---

### Task 3: Add Persisted Compile Diagnostics And Loop Guard Tests

**Files:**
- Modify: `tests/test_specs_compiler_service.py`
- Modify: `services/specs/compiler_service.py`

- [ ] **Step 1: Add persisted compile fail-closed test**

Add near source metadata repair tests:

```python
def test_compile_spec_authority_coverage_repair_does_not_chain_metadata_repair(
    session: Session,
    sample_product: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coverage repair failure is terminal and cannot start metadata repair."""
    from services.specs import compiler_service  # noqa: PLC0415

    spec_row = _create_spec_version(
        session,
        product_id=require_id(sample_product.product_id, "product_id"),
        content=_accepted_multi_item_spec_profile_json(),
    )
    spec_version_id = require_id(spec_row.spec_version_id, "spec_version_id")
    calls: list[str | None] = []

    def fake_invoke(**kwargs: object) -> str:
        domain_hint = cast("str | None", kwargs.get("domain_hint"))
        calls.append(domain_hint)
        if domain_hint and "failed structured coverage validation" in domain_hint:
            return _source_metadata_failure_json(
                source_item_id="REQ.todo-create",
                invariant_id="INV-badbadbadbadbad1",
            )
        return _compiled_success_json()

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_invoke,
    )

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=cast("Engine", session.get_bind()),
        spec_version_id=spec_version_id,
        force_recompile=True,
    )

    assert result["success"] is False
    assert result["error"] == "STRUCTURED_ITEM_COMPILATION_FAILED"
    assert result["reason"] == "FOCUSED_ITEM_AUTHORITY_FAILED"
    assert sum(
        1
        for hint in calls
        if hint and "failed structured coverage validation" in hint
    ) == 1
    assert not any(
        hint and "failed source metadata validation" in hint for hint in calls
    )
```

- [ ] **Step 2: Add persisted compile success test**

Add after the previous test:

```python
def test_compile_spec_authority_repairs_missing_coverage_and_persists(
    session: Session,
    sample_product: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coverage repair can produce persisted authority when feedback succeeds."""
    from services.specs import compiler_service  # noqa: PLC0415

    spec_row = _create_spec_version(
        session,
        product_id=require_id(sample_product.product_id, "product_id"),
        content=_accepted_multi_item_spec_profile_json(),
    )
    spec_version_id = require_id(spec_row.spec_version_id, "spec_version_id")

    def fake_invoke(**kwargs: object) -> str:
        spec_content = cast("str", kwargs["spec_content"])
        domain_hint = cast("str | None", kwargs.get("domain_hint"))
        item_id = json.loads(spec_content)["items"][0]["id"]
        if domain_hint and "failed structured coverage validation" in domain_hint:
            source_level = json.loads(spec_content)["items"][0]["level"]
            return _behavioral_payload_json(
                source_item_id=cast("str", item_id),
                source_level=cast("SpecAuthoritySourceLevel", source_level),
            )
        return _compiled_success_json()

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_invoke,
    )

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=cast("Engine", session.get_bind()),
        spec_version_id=spec_version_id,
        force_recompile=True,
    )

    assert result["success"] is True
    with Session(session.get_bind()) as verify_session:
        rows = verify_session.exec(select(CompiledSpecAuthority)).all()
    assert len(rows) == 1
```

- [ ] **Step 3: Add bounded diagnostics only if existing failure details need it**

If persisted failure does not expose enough action data, extend `_normalized_failure_result` or caller wrapping to include:

```python
"coverage_repair_attempted": True
"coverage_repair_item_ids": ["REQ.todo-create"]
"coverage_repair_result": "failed"
```

Do not add a second repair path. Diagnostics must not change retry behavior.

- [ ] **Step 4: Run persisted compile tests**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py -q -k "coverage_repair or metadata_repair"
```

Expected: pass.

- [ ] **Step 5: Commit diagnostics**

```bash
git add services/specs/compiler_service.py tests/test_specs_compiler_service.py
git commit -m "test: guard authority coverage repair loop"
```

---

### Task 4: Persist Scope-Extension Base Metadata

**Files:**
- Modify: `services/agent_workbench/scope_extension.py`
- Modify: `tests/test_agent_workbench_phase1_integration.py` or `tests/test_agent_workbench_scope_extension.py` if present.

- [ ] **Step 1: Add marker metadata test**

Find the existing scope extension start test that asserts `scope_extension_context`. Add assertions that amended spec row `approval_notes` contains:

```python
marker = _recovery_marker_from_notes(amended_spec.approval_notes)
assert marker["base_spec_version_id"] == base_spec_version_id
assert marker["base_spec_hash"] == base_spec_hash
assert marker["added_source_item_ids"] == ["REQ.phase1-extension"]
```

- [ ] **Step 2: Update marker persistence**

In `ScopeExtensionRunner._persist_recovery_marker`, add parameters:

```python
base_spec_version_id: int
base_spec_hash: str
added_source_item_ids: list[str]
```

Update caller in `start()`:

```python
self._persist_recovery_marker(
    request=request,
    spec_version_id=spec_version_id,
    resolved_spec_path=resolved_spec_path,
    request_fingerprint=request_fingerprint,
    base_spec_version_id=int(validation_data["base_spec_version_id"]),
    base_spec_hash=str(validation_data["base_spec_hash"]),
    added_source_item_ids=[
        str(item_id) for item_id in validation_data["added_source_item_ids"]
    ],
)
```

Update marker payload:

```python
marker = {
    "idempotency_key": request.idempotency_key,
    "request_fingerprint": request_fingerprint,
    "spec_file": str(resolved_spec_path),
    "base_spec_version_id": base_spec_version_id,
    "base_spec_hash": base_spec_hash,
    "added_source_item_ids": list(added_source_item_ids),
}
```

- [ ] **Step 3: Run scope extension tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_phase1_integration.py -q -k "scope_extension"
```

Expected: pass.

- [ ] **Step 4: Commit marker metadata**

```bash
git add services/agent_workbench/scope_extension.py tests/test_agent_workbench_phase1_integration.py
git commit -m "feat: record scope extension base metadata"
```

---

### Task 5: Reuse Accepted Base Authority For Scope Extensions

**Files:**
- Modify: `services/specs/compiler_service.py`
- Modify: `tests/test_specs_compiler_service.py`

- [ ] **Step 1: Add helper test for scope-extension marker detection**

Add test:

```python
def test_scope_extension_marker_from_spec_notes() -> None:
    """Compiler can discover scope-extension metadata from amended spec notes."""
    from services.specs import compiler_service  # noqa: PLC0415

    notes = (
        "Required compiler precondition for pending authority generation\n"
        "scope_extension_start_recovery="
        '{"added_source_item_ids":["REQ.new"],'
        '"base_spec_hash":"sha256:base",'
        '"base_spec_version_id":3,'
        '"idempotency_key":"scope-1",'
        '"request_fingerprint":"sha256:req",'
        '"spec_file":"/tmp/spec.json"}'
    )

    marker = compiler_service._scope_extension_marker_from_notes(notes)

    assert marker is not None
    assert marker.base_spec_version_id == 3
    assert marker.added_source_item_ids == ["REQ.new"]
```

- [ ] **Step 2: Implement marker dataclass and parser**

Add near compiler dataclasses:

```python
@dataclass(frozen=True)
class _ScopeExtensionCompileMarker:
    """Scope-extension metadata stored on amended spec rows."""

    base_spec_version_id: int
    base_spec_hash: str
    added_source_item_ids: list[str]
```

Add parser:

```python
def _scope_extension_marker_from_notes(
    notes: str | None,
) -> _ScopeExtensionCompileMarker | None:
    """Parse amended spec scope-extension marker from approval notes."""
    if not notes:
        return None
    prefix = "scope_extension_start_recovery="
    for line in notes.splitlines():
        if not line.startswith(prefix):
            continue
        try:
            payload = json.loads(line.removeprefix(prefix))
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        base_spec_version_id = payload.get("base_spec_version_id")
        base_spec_hash = payload.get("base_spec_hash")
        added_source_item_ids = payload.get("added_source_item_ids")
        if not isinstance(base_spec_version_id, int):
            return None
        if not isinstance(base_spec_hash, str):
            return None
        if not isinstance(added_source_item_ids, list) or not all(
            isinstance(item_id, str) for item_id in added_source_item_ids
        ):
            return None
        return _ScopeExtensionCompileMarker(
            base_spec_version_id=base_spec_version_id,
            base_spec_hash=base_spec_hash,
            added_source_item_ids=list(added_source_item_ids),
        )
    return None
```

- [ ] **Step 3: Add extension-only artifact helper**

Add:

```python
def _extension_only_artifact(
    artifact: TechnicalSpecArtifact,
    *,
    added_source_item_ids: list[str],
) -> TechnicalSpecArtifact:
    """Return a structured spec containing only added scope-extension items."""
    added = set(added_source_item_ids)
    focused = artifact.model_copy(deep=True)
    focused.items = [item for item in focused.items if item.id in added]
    focused.relations = [
        relation
        for relation in focused.relations
        if relation.from_ in added or relation.to in added
    ]
    return focused
```

- [ ] **Step 4: Add compile test for base authority reuse**

Create a test that:

1. Persists base spec version.
2. Persists accepted base `CompiledSpecAuthority` with source coverage for base item.
3. Persists amended spec version with marker pointing to base version and added item.
4. Fakes compiler so it fails if full amended spec is compiled, but succeeds for extension-only spec.
5. Calls `compile_spec_authority_for_version_with_engine`.
6. Asserts persisted authority contains base invariant plus extension invariant.

Core assertion shape:

```python
assert result["success"] is True
assert full_amended_compile_attempted is False
assert extension_item_compile_attempted is True
compiled = SpecAuthorityCompilerOutput.model_validate_json(
    rows[0].compiled_artifact_json
)
assert isinstance(compiled.root, SpecAuthorityCompilationSuccess)
source_ids = {invariant.source_item_id for invariant in compiled.root.invariants}
assert {"REQ.base", "REQ.extension"} <= source_ids
```

- [ ] **Step 5: Implement scope-extension reuse path**

In `compile_spec_authority_for_version_with_engine`, after loading `spec_version`, detect marker:

```python
marker = _scope_extension_marker_from_notes(spec_version.approval_notes)
```

If marker exists:

1. Load base `SpecRegistry`.
2. Confirm base spec hash equals marker hash.
3. Load latest accepted `CompiledSpecAuthority` for marker base spec.
4. Load `compiled_artifact_json` with `load_stored_compiled_artifact`.
5. Build extension-only artifact from amended spec and marker added item IDs.
6. Compile extension-only artifact through `_compile_spec_authority_output`.
7. Merge base success and extension success with `_merge_compilation_successes`.
8. Normalize merged output against full amended spec content.
9. Run `_missing_iterative_authority_item_ids` against full amended artifact.
10. If any missing, run coverage repair once for missing added items only; base items should already be covered by base authority.

Fail closed if base authority is missing, unsupported, invalid, or hash mismatched. Error should be `SPEC_COMPILE_FAILED` with first blocking gap explaining base authority reuse failure.

- [ ] **Step 6: Run scope-extension compiler tests**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py -q -k "scope_extension or coverage_repair"
```

Expected: pass.

- [ ] **Step 7: Commit scope-extension reuse**

```bash
git add services/specs/compiler_service.py tests/test_specs_compiler_service.py
git commit -m "feat: reuse base authority for scope extensions"
```

---

### Task 6: Docs And Regression Verification

**Files:**
- Modify: `docs/agent-cli-manual.md`

- [ ] **Step 1: Add compiler failure remediation doc**

Add under Agent Feedback Capture or Authority Compile troubleshooting:

```markdown
### Authority Compile Coverage Repair

If `authority compile` fails with
`STRUCTURED_COVERAGE_INCOMPLETE: MISSING_ACCEPTED_MUST_AUTHORITY`, do not edit
the spec blindly. The compiler should run one explicit focused repair attempt
for each missing accepted `MUST`/`MUST_NOT` item. If repair still fails, report:

- compiler model;
- failure artifact id;
- missing item count;
- first 10 missing item ids;
- whether any repair attempt introduced source metadata errors.

For scope extensions, unchanged accepted base authority should be reused where
the amended spec marker proves the base spec version and hash.
```

- [ ] **Step 2: Run focused regression suite**

Run:

```bash
uv run --frozen pytest \
  tests/test_specs_compiler_service.py \
  tests/test_pending_authority_service.py \
  tests/test_agent_workbench_project_setup.py \
  tests/test_agent_workbench_phase1_integration.py \
  -q
```

Expected: pass.

- [ ] **Step 3: Run lint on touched files**

Run:

```bash
uv run --frozen ruff check \
  services/specs/compiler_service.py \
  services/agent_workbench/scope_extension.py \
  tests/test_specs_compiler_service.py \
  tests/test_agent_workbench_phase1_integration.py \
  docs/agent-cli-manual.md
```

Expected: pass.

- [ ] **Step 4: Commit docs**

```bash
git add docs/agent-cli-manual.md
git commit -m "docs: document authority coverage repair"
```

---

## Self-Review

- Spec coverage: explicit feedback and max-one retry are covered in Tasks 1-3. Scope-extension base reuse is covered in Tasks 4-5. Docs and regression are covered in Task 6.
- Placeholder scan: no TBD placeholders. Task 5 uses existing `SpecRelation.from_` and `SpecRelation.to` fields.
- Type consistency: repair functions use existing `SpecAuthorityCompilationSuccess`, `SpecAuthorityCompilationFailure`, `_FocusedItemCompilationFailure`, `TechnicalSpecArtifact`, and `_NormalizedCompilerInvocation` types.
- Risk: Task 5 is larger than the core repair loop. If Task 1-3 solve the ASA failure, Task 5 still remains valuable but should be reviewed as a separate commit because it touches compile architecture.
