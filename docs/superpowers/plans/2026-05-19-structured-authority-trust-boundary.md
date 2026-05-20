# Structured Authority Trust Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `agileforge.spec.v1` JSON the only authority compilation input and remove host-derived semantic coverage as an authority acceptance blocker.

**Architecture:** AgileForge validates structured spec shape, freshness, review tokens, compiled artifact schema, and source-reference item IDs. The LLM compiler owns semantic interpretation, while human/agent review owns accept/reject decisions. Legacy Markdown authority compilation and host candidate coverage gates are removed from the structured authority path.

**Tech Stack:** Python 3.12, SQLModel, Pydantic v2, argparse CLI, pytest, ruff, SQLite-backed integration tests.

---

## File Map

- Modify `services/specs/profile_content.py`: reject non-`agileforge.spec.v1` input instead of preserving legacy Markdown.
- Modify `services/agent_workbench/error_codes.py`: add `SPEC_SOURCE_FORMAT_UNSUPPORTED`.
- Modify `services/agent_workbench/command_registry.py`: expose the new error code for project create/setup retry/spec update paths.
- Modify `services/specs/pending_authority_service.py`: propagate structured spec normalization error codes.
- Modify `services/specs/compiler_service.py`: propagate structured spec normalization error codes and stop invoking compiler for unsupported input.
- Modify `services/agent_workbench/project_setup.py`: use structured spec hash validation before mutation ledger write.
- Modify `services/agent_workbench/project_setup_fingerprints.py`: hash setup specs using canonical `agileforge.spec.v1` JSON.
- Modify `orchestrator_agent/agent_tools/spec_authority_compiler_agent/compiler_contract.py`: add source-independent invariant ID helper.
- Modify `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`: remove compact IR generation and source-map semantic support failures from acceptance-critical normalization.
- Modify `services/agent_workbench/authority_review.py`: remove candidate coverage findings/gaps from structured review, add structural source-ref findings.
- Modify `services/agent_workbench/authority_decision.py`: block only structural review findings; ignore removed candidate/coverage codes defensively.
- Modify `services/agent_workbench/application.py`: return human-simple next actions.
- Modify `cli/main.py`: allow `authority accept --project-id` to auto-load latest fresh review token and stable idempotency key.
- Modify tests in `tests/test_specs_compiler_service.py`, `tests/test_agent_workbench_project_create_cli_integration.py`, `tests/test_spec_authority_compiler_normalizer.py`, `tests/test_agent_workbench_authority_review.py`, `tests/test_agent_workbench_authority_decision.py`, `tests/test_agent_workbench_authority_decision_cli.py`, `tests/test_agent_workbench_application.py`, and `tests/test_agent_workbench_command_schema.py`.
- Modify docs in `docs/agent-cli-manual.md`.

## Implementation Rules

- Do not retain Markdown authority compilation behind a flag.
- Do not generate `requirement_candidates` or `authority_mappings` as public review/acceptance artifacts.
- Do not emit `AUTHORITY_CANDIDATE_*` or `AUTHORITY_COVERAGE_INCOMPLETE` from structured authority review.
- Do not require candidate-specific incomplete-review overrides.
- Keep stale-source, stale-token, invalid compiled artifact, and invalid source-ref protections.
- Commit after each task when tests for that task pass.

---

### Task 1: Structured-Only Spec Content Contract Tests

**Files:**
- Modify: `tests/test_specs_compiler_service.py`
- Modify: `tests/test_agent_workbench_project_create_cli_integration.py`

- [ ] **Step 1: Replace legacy Markdown normalization test with unsupported-input test**

In `tests/test_specs_compiler_service.py`, replace `test_normalize_legacy_markdown_content_keeps_existing_hash_shape` with:

```python
def test_normalize_markdown_spec_content_rejects_authority_input() -> None:
    """Authority compilation requires canonical agileforge.spec.v1 JSON."""
    raw_markdown = "# Spec\n\nThe system must record audit evidence.\n"

    with pytest.raises(SpecContentNormalizationError) as exc_info:
        normalize_spec_content_for_registry(raw_markdown)

    assert exc_info.value.error_code == "SPEC_SOURCE_FORMAT_UNSUPPORTED"
    assert "Expected agileforge.spec.v1 JSON" in str(exc_info.value)
```

- [ ] **Step 2: Add arbitrary JSON unsupported-input test**

Add this test in `tests/test_specs_compiler_service.py` after the Markdown test:

```python
def test_normalize_arbitrary_json_rejects_authority_input() -> None:
    """JSON without the AgileForge profile marker is not compiler input."""
    raw_json = json.dumps({"title": "Loose JSON spec"})

    with pytest.raises(SpecContentNormalizationError) as exc_info:
        normalize_spec_content_for_registry(raw_json)

    assert exc_info.value.error_code == "SPEC_SOURCE_FORMAT_UNSUPPORTED"
    assert "schema_version" in str(exc_info.value)
```

- [ ] **Step 3: Add project create Markdown rejection integration test**

In `tests/test_agent_workbench_project_create_cli_integration.py`, add:

```python
def test_project_create_rejects_markdown_spec_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Project create refuses Markdown authority input before setup mutation."""
    business_db = tmp_path / "business.db"
    engine = _business_engine(business_db)
    workflow = FakeWorkflowPort()
    monkeypatch.setattr(
        "cli.main.AgentWorkbenchApplication",
        lambda **_kwargs: AgentWorkbenchApplication(
            business_engine=engine,
            workflow=workflow,
        ),
    )
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    spec_file = _write_spec(caller_dir)
    monkeypatch.chdir(caller_dir)

    exit_code = main(
        [
            "project",
            "create",
            "--name",
            "Markdown Project",
            "--spec-file",
            str(spec_file),
            "--idempotency-key",
            "markdown-project-create-001",
        ]
    )

    payload = _captured_payload(capsys)
    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "SPEC_SOURCE_FORMAT_UNSUPPORTED"
    assert payload["errors"][0]["remediation"] == [
        "Generate specs/spec.json as agileforge.spec.v1 JSON.",
        "Retry project create with --spec-file specs/spec.json.",
    ]
    with Session(engine) as session:
        assert session.exec(select(Product)).all() == []
```

- [ ] **Step 4: Run tests and confirm failure**

Run:

```bash
uv run --frozen pytest \
  tests/test_specs_compiler_service.py::test_normalize_markdown_spec_content_rejects_authority_input \
  tests/test_specs_compiler_service.py::test_normalize_arbitrary_json_rejects_authority_input \
  tests/test_agent_workbench_project_create_cli_integration.py::test_project_create_rejects_markdown_spec_source \
  -q
```

Expected: FAIL because `SpecContentNormalizationError.error_code` and `SPEC_SOURCE_FORMAT_UNSUPPORTED` do not exist yet, and Markdown still normalizes as legacy.

### Task 2: Structured-Only Spec Content Implementation

**Files:**
- Modify: `services/specs/profile_content.py`
- Modify: `services/agent_workbench/error_codes.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `services/specs/pending_authority_service.py`
- Modify: `services/specs/compiler_service.py`
- Modify: `services/agent_workbench/project_setup.py`
- Modify: `services/agent_workbench/project_setup_fingerprints.py`

- [ ] **Step 1: Add unsupported source error metadata**

In `services/agent_workbench/error_codes.py`, add enum value after `SPEC_FILE_INVALID`:

```python
    SPEC_SOURCE_FORMAT_UNSUPPORTED = "SPEC_SOURCE_FORMAT_UNSUPPORTED"
```

Add registry entry after `SPEC_FILE_INVALID`:

```python
    ErrorCode.SPEC_SOURCE_FORMAT_UNSUPPORTED: ErrorMetadata(
        code=ErrorCode.SPEC_SOURCE_FORMAT_UNSUPPORTED.value,
        default_exit_code=2,
        retryable=False,
        description="The requested spec source format is not supported.",
    ),
```

- [ ] **Step 2: Make profile content normalization structured-only**

Replace `services/specs/profile_content.py` with:

```python
# services/specs/profile_content.py
"""Helpers for normalizing structured spec content before registry storage."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import ValidationError

from utils.agileforge_spec_profile import (
    TechnicalSpecArtifact,
    canonical_spec_hash,
    canonical_spec_json,
)

STRUCTURED_SPEC_FORMAT: str = "agileforge.spec.v1"
UNSUPPORTED_SPEC_SOURCE_FORMAT: str = "SPEC_SOURCE_FORMAT_UNSUPPORTED"
INVALID_SPEC_FILE: str = "SPEC_FILE_INVALID"


class SpecContentNormalizationError(ValueError):
    """Raised when spec content cannot be normalized for authority compilation."""

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code: str = error_code


@dataclass(frozen=True)
class NormalizedSpecContent:
    """Normalized spec content ready for SpecRegistry."""

    content: str
    spec_hash: str
    format: str


def normalize_spec_content_for_registry(raw_content: str) -> NormalizedSpecContent:
    """Canonicalize agileforge.spec.v1 JSON and reject every other format."""
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise SpecContentNormalizationError(
            "Expected agileforge.spec.v1 JSON; received non-JSON spec content.",
            error_code=UNSUPPORTED_SPEC_SOURCE_FORMAT,
        ) from exc

    if not isinstance(parsed, dict):
        raise SpecContentNormalizationError(
            "Expected agileforge.spec.v1 JSON object.",
            error_code=UNSUPPORTED_SPEC_SOURCE_FORMAT,
        )
    if parsed.get("schema_version") != STRUCTURED_SPEC_FORMAT:
        raise SpecContentNormalizationError(
            "Expected schema_version='agileforge.spec.v1'.",
            error_code=UNSUPPORTED_SPEC_SOURCE_FORMAT,
        )

    try:
        artifact = TechnicalSpecArtifact.model_validate(parsed)
    except ValidationError as exc:
        message = f"Invalid agileforge.spec.v1 content: {exc}"
        raise SpecContentNormalizationError(
            message,
            error_code=INVALID_SPEC_FILE,
        ) from exc
    return NormalizedSpecContent(
        content=canonical_spec_json(artifact),
        spec_hash=canonical_spec_hash(artifact),
        format=STRUCTURED_SPEC_FORMAT,
    )
```

- [ ] **Step 3: Propagate normalization error codes in pending authority service**

In `services/specs/pending_authority_service.py`, change the `except SpecContentNormalizationError` block in `compile_pending_authority_for_project` to:

```python
    except SpecContentNormalizationError as exc:
        return _result(
            ok=False,
            product_id=product_id,
            spec_path=resolved_path,
            error_code=exc.error_code,
            spec_hash=spec_hash,
            error=str(exc),
        )
```

- [ ] **Step 4: Propagate normalization error codes in compiler service**

In `services/specs/compiler_service.py`, change the `except SpecContentNormalizationError` block in `update_spec_and_compile_authority` to:

```python
    except SpecContentNormalizationError as exc:
        return {
            "success": False,
            "error_code": exc.error_code,
            "error": str(exc),
        }
```

- [ ] **Step 5: Add setup spec validation helper**

In `services/agent_workbench/project_setup.py`, import:

```python
from services.specs.profile_content import (
    SpecContentNormalizationError,
    normalize_spec_content_for_registry,
)
```

Replace `_spec_hash_or_error` with:

```python
def _spec_hash_or_error(path: Path) -> str | dict[str, Any]:
    """Return canonical structured spec hash or a structured spec-file error."""
    if not path.exists():
        return _error(
            ErrorCode.SPEC_FILE_NOT_FOUND.value,
            details={"spec_file": str(path)},
            remediation=["Create the spec file or pass the correct path."],
        )
    if not path.is_file():
        return _error(
            ErrorCode.SPEC_FILE_INVALID.value,
            details={"spec_file": str(path), "reason": "not_a_file"},
            remediation=["Pass a readable agileforge.spec.v1 JSON file."],
        )
    try:
        raw_content = path.read_text(encoding="utf-8")
        normalized = normalize_spec_content_for_registry(raw_content)
        return normalized.spec_hash
    except SpecContentNormalizationError as exc:
        remediation = [
            "Generate specs/spec.json as agileforge.spec.v1 JSON.",
            "Retry project create with --spec-file specs/spec.json.",
        ]
        return _error(
            exc.error_code,
            details={"spec_file": str(path), "reason": str(exc)},
            remediation=remediation,
        )
    except UnicodeDecodeError as exc:
        return _error(
            ErrorCode.SPEC_FILE_INVALID.value,
            details={"spec_file": str(path), "reason": str(exc)},
            remediation=["Save the specification file as UTF-8 JSON."],
        )
    except OSError as exc:
        return _error(
            ErrorCode.SPEC_FILE_INVALID.value,
            details={"spec_file": str(path), "reason": str(exc)},
            remediation=["Pass a readable agileforge.spec.v1 JSON file."],
        )
```

- [ ] **Step 6: Canonicalize setup fingerprint hash**

In `services/agent_workbench/project_setup_fingerprints.py`, import:

```python
from services.specs.profile_content import normalize_spec_content_for_registry
```

Replace `setup_spec_hash` with:

```python
def setup_spec_hash(path: Path) -> str:
    """Return the setup contract hash for a structured spec file."""
    normalized = normalize_spec_content_for_registry(path.read_text(encoding="utf-8"))
    return canonical_hash(normalized.content)
```

- [ ] **Step 7: Register new error code in command registry**

In `services/agent_workbench/command_registry.py`, add `ErrorCode.SPEC_SOURCE_FORMAT_UNSUPPORTED.value` to the `errors=(...)` tuples for:

- `agileforge project create`
- `agileforge project setup retry`
- `agileforge project spec update` if present in the registry

- [ ] **Step 8: Run Task 1 tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_specs_compiler_service.py::test_normalize_markdown_spec_content_rejects_authority_input \
  tests/test_specs_compiler_service.py::test_normalize_arbitrary_json_rejects_authority_input \
  tests/test_agent_workbench_project_create_cli_integration.py::test_project_create_rejects_markdown_spec_source \
  -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add \
  services/specs/profile_content.py \
  services/agent_workbench/error_codes.py \
  services/agent_workbench/command_registry.py \
  services/specs/pending_authority_service.py \
  services/specs/compiler_service.py \
  services/agent_workbench/project_setup.py \
  services/agent_workbench/project_setup_fingerprints.py \
  tests/test_specs_compiler_service.py \
  tests/test_agent_workbench_project_create_cli_integration.py
git commit -m "fix: require structured specs for authority setup"
```

---

### Task 3: Normalizer Structural-Only Tests

**Files:**
- Modify: `tests/test_spec_authority_compiler_normalizer.py`

- [ ] **Step 1: Add missing source map allowed test**

Add:

```python
def test_structured_profile_allows_missing_source_map() -> None:
    """Structured authority no longer requires source_map for acceptance-critical IDs."""
    raw = json.loads(_raw_compiler_output_json())
    raw["source_map"] = []

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=_structured_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.source_map == []
    assert normalized.root.requirement_candidates == []
    assert normalized.root.authority_mappings == []
    assert normalized.root.invariants[0].id.startswith("INV-")
```

- [ ] **Step 2: Add unrelated excerpt allowed test**

Add:

```python
def test_structured_profile_keeps_unrelated_source_map_as_review_evidence() -> None:
    """Structured mode does not reject semantically weak source excerpts."""
    raw = json.loads(_raw_compiler_output_json())
    raw["source_map"][0]["excerpt"] = "This sentence is review evidence only."
    raw["source_map"][0]["location"] = "REQ.audit-evidence.statement"

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=_structured_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.source_map[0].location == "REQ.audit-evidence.statement"
    assert normalized.root.requirement_candidates == []
    assert normalized.root.authority_mappings == []
```

- [ ] **Step 3: Add invalid source ref survives normalization test**

Add:

```python
def test_structured_profile_invalid_source_ref_is_review_finding_not_compile_failure() -> None:
    """Normalizer preserves invalid source refs so review can block structurally."""
    raw = json.loads(_raw_compiler_output_json())
    raw["source_map"][0]["location"] = "REQ.missing.statement"

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=_structured_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.source_map[0].location == "REQ.missing.statement"
```

- [ ] **Step 4: Run tests and confirm failure**

Run:

```bash
uv run --frozen pytest \
  tests/test_spec_authority_compiler_normalizer.py::test_structured_profile_allows_missing_source_map \
  tests/test_spec_authority_compiler_normalizer.py::test_structured_profile_keeps_unrelated_source_map_as_review_evidence \
  tests/test_spec_authority_compiler_normalizer.py::test_structured_profile_invalid_source_ref_is_review_finding_not_compile_failure \
  -q
```

Expected: FAIL because normalizer still requires source_map and still builds compact IR.

### Task 4: Normalizer Structural-Only Implementation

**Files:**
- Modify: `orchestrator_agent/agent_tools/spec_authority_compiler_agent/compiler_contract.py`
- Modify: `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`
- Modify: `tests/test_spec_authority_compiler_agent.py`

- [ ] **Step 1: Add source-independent invariant ID helper**

In `orchestrator_agent/agent_tools/spec_authority_compiler_agent/compiler_contract.py`, add:

```python
def compute_invariant_id_from_payload(
    invariant_type: InvariantType,
    parameters: InvariantParameters | None = None,
) -> str:
    """Compute deterministic invariant ID from invariant semantics only."""
    parameter_seed = _canonical_parameter_seed(parameters)
    seed = f"{invariant_type.value}|{parameter_seed}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"INV-{digest[:16]}"
```

- [ ] **Step 2: Remove compact IR imports from normalizer**

In `normalizer.py`, remove imports from `utils.spec_authority_ir` except `IrProvenance` if schema fields still require enum values during transition. Remove imports of:

```python
AuthorityTargetKind
MappingProvenance
RequirementCandidate
SourceUnit
SourceUnitDisposition
build_authority_mappings
extract_requirement_candidates
parse_markdown_sections
source_units_from_sections
```

Remove imports of these schema classes if no longer referenced:

```python
SpecAuthorityIrPacketLimits
SpecAuthorityMapping
SpecAuthorityRequirementCandidate
SpecAuthoritySourceUnit
```

- [ ] **Step 3: Replace compact IR setter with clear helper**

In `normalizer.py`, replace `_set_compact_ir(...)` with:

```python
def _clear_compact_ir(success: SpecAuthorityCompilationSuccess) -> None:
    """Clear legacy compact IR fields; structured authority has no host semantic IR."""
    success.ir_schema_version = None
    success.ir_provenance = None
    success.source_units = []
    success.requirement_candidates = []
    success.authority_mappings = []
    success.ir_packet_limits = None
```

Delete helper functions used only by compact IR generation:

- `_profile_source_units_and_candidates`
- `_profile_source_fragments`
- `_profile_evidence_candidates`
- `_profile_item_classification`
- `_profile_item_is_active`
- `_mapping_entries_for_compact_ir`
- `_source_entry_mapping_provenance`
- `_model_to_host_candidate_ids`

- [ ] **Step 4: Allow missing source map and remove semantic support rejection**

In `normalize_compiler_output`, replace the missing-source-map failure block with:

```python
    original_source_map = list(success.source_map)
    if success.source_map:
        _repair_source_map_from_source_text(
            success,
            source_text=source_text,
            source_format=source_format,
        )
```

Replace the invariant ID rewrite loop with:

```python
    for inv in success.invariants:
        inv.id = compute_invariant_id_from_payload(inv.type, inv.parameters)
```

Replace source-map ID rewrite with:

```python
    normalized_ids = {inv.id for inv in success.invariants}
    if success.source_map:
        original_id_to_new_id: dict[str, str] = {}
        for original, normalized in zip(original_invariants, success.invariants, strict=False):
            original_id_to_new_id[original.id] = normalized.id
        for index, entry in enumerate(success.source_map):
            if entry.invariant_id in original_id_to_new_id:
                entry.invariant_id = original_id_to_new_id[entry.invariant_id]
            elif index < len(success.invariants):
                entry.invariant_id = success.invariants[index].id
        success.source_map = [
            entry for entry in success.source_map if entry.invariant_id in normalized_ids
        ]
```

Then call:

```python
    _clear_compact_ir(success)
```

before returning success.

- [ ] **Step 5: Keep duplicate invariant dedupe before ID rewrite**

Keep `_deduplicate_semantic_invariants(success)` before the source-independent ID rewrite. If duplicate rewritten IDs remain after rewrite, return `DUPLICATE_INVARIANT_IDS` as today.

- [ ] **Step 6: Update compiler agent instruction tests**

In `tests/test_spec_authority_compiler_agent.py`, keep assertions that instructions do not mention `requirement_candidates` or `authority_mappings`. Add assertion that source-map text is review evidence, not host semantic proof:

```python
assert "source_map is review evidence" in instructions
```

Update `orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions.txt` in the implementation step that follows the test by adding:

```text
source_map is review evidence. It should point to supporting agileforge.spec.v1 item IDs when possible, but host validation does not use it to prove semantic coverage.
```

- [ ] **Step 7: Run Task 3 tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_spec_authority_compiler_normalizer.py::test_structured_profile_allows_missing_source_map \
  tests/test_spec_authority_compiler_normalizer.py::test_structured_profile_keeps_unrelated_source_map_as_review_evidence \
  tests/test_spec_authority_compiler_normalizer.py::test_structured_profile_invalid_source_ref_is_review_finding_not_compile_failure \
  tests/test_spec_authority_compiler_agent.py \
  -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add \
  orchestrator_agent/agent_tools/spec_authority_compiler_agent/compiler_contract.py \
  orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py \
  orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions.txt \
  tests/test_spec_authority_compiler_normalizer.py \
  tests/test_spec_authority_compiler_agent.py
git commit -m "fix: remove host semantic IR from authority normalization"
```

---

### Task 5: Review Structural Findings Tests

**Files:**
- Modify: `tests/test_agent_workbench_authority_review.py`

- [ ] **Step 1: Replace structured review blocker test**

Replace `test_review_blocks_structured_spec_when_profile_ir_has_uncovered_items` with:

```python
def test_review_does_not_block_structured_spec_on_candidate_coverage(
    session: Session,
    tmp_path: Path,
) -> None:
    """Structured spec review does not expose host candidate coverage blockers."""
    spec_content = _agileforge_spec_profile_payload()
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            spec_filename="spec.json",
            artifact_json=_compiled_success_json(
                source_excerpt="This sentence is review evidence only.",
                source_location="REQ.guard-tokens.statement",
            ),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    assert result["ok"] is True
    data = result["data"]
    pending = data["pending_authority"]
    codes = {finding["code"] for finding in data["review_findings"]}
    assert "requirement_candidates" not in pending
    assert pending["authority_mappings"] == []
    assert "AUTHORITY_CANDIDATE_UNCOVERED" not in codes
    assert "AUTHORITY_COVERAGE_INCOMPLETE" not in codes
    assert data["review_summary"]["acceptance_status"] == "accept_ready"
```

- [ ] **Step 2: Add missing source refs warning test**

Add:

```python
def test_review_warns_when_structured_source_refs_are_missing(
    session: Session,
    tmp_path: Path,
) -> None:
    """Missing source refs are visible but not acceptance blockers."""
    artifact = json.loads(_compiled_success_json(source_excerpt=""))
    artifact["source_map"] = []
    spec_content = _agileforge_spec_profile_payload()
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            spec_filename="spec.json",
            artifact_json=json.dumps(artifact),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(project_id=project_id)

    findings = result["data"]["review_findings"]
    assert any(finding["code"] == "SOURCE_REFS_MISSING" for finding in findings)
    missing = next(f for f in findings if f["code"] == "SOURCE_REFS_MISSING")
    assert missing["severity"] == "warning"
    assert missing["override_allowed"] is True
    assert result["data"]["review_summary"]["acceptance_status"] == "accept_ready"
```

- [ ] **Step 3: Add invalid source ref blocking test**

Add:

```python
def test_review_blocks_structured_invalid_source_ref(
    session: Session,
    tmp_path: Path,
) -> None:
    """Source refs pointing at missing spec item IDs are structural blockers."""
    spec_content = _agileforge_spec_profile_payload()
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            spec_filename="spec.json",
            artifact_json=_compiled_success_json(
                source_excerpt="The review output must include guard tokens.",
                source_location="REQ.missing.statement",
            ),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(project_id=project_id)

    codes = {finding["code"] for finding in result["data"]["review_findings"]}
    assert "SOURCE_REF_INVALID" in codes
    assert result["data"]["review_summary"]["acceptance_status"] == "blocked"
```

- [ ] **Step 4: Run tests and confirm failure**

Run:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_authority_review.py::test_review_does_not_block_structured_spec_on_candidate_coverage \
  tests/test_agent_workbench_authority_review.py::test_review_warns_when_structured_source_refs_are_missing \
  tests/test_agent_workbench_authority_review.py::test_review_blocks_structured_invalid_source_ref \
  -q
```

Expected: FAIL because review still derives candidate findings and has no source-ref structural findings.

### Task 6: Review Structural Findings Implementation

**Files:**
- Modify: `services/agent_workbench/authority_review.py`

- [ ] **Step 1: Add structured spec detection helper**

Add near `_structured_spec_snapshot` helpers:

```python
def _structured_artifact_from_text(text: str) -> TechnicalSpecArtifact | None:
    try:
        return TechnicalSpecArtifact.model_validate_json(text)
    except (ValueError, ValidationError):
        return None
```

`services/agent_workbench/authority_review.py` already imports `ValidationError` from `pydantic`; keep that import.

- [ ] **Step 2: Add source ref item ID parser**

Add:

```python
def _source_ref_item_id(location: object) -> str | None:
    if not isinstance(location, str) or not location.strip():
        return None
    value = location.strip()
    prefix = value.rsplit(".", maxsplit=1)[0]
    if prefix.startswith(
        (
            "GOAL.",
            "NON_GOAL.",
            "REQ.",
            "QUALITY.",
            "CONSTRAINT.",
            "INTERFACE.",
            "DATA.",
            "DECISION.",
            "ASSUMPTION.",
            "RISK.",
            "EXAMPLE.",
            "OPEN_QUESTION.",
        )
    ):
        return prefix
    return value if "." in value else None
```

- [ ] **Step 3: Add structural source-ref findings**

Add:

```python
def _structured_source_ref_findings(
    *,
    artifact: Mapping[str, Any],
    spec_artifact: TechnicalSpecArtifact | None,
) -> list[JsonDict]:
    if spec_artifact is None:
        return []
    source_map = artifact.get("source_map")
    if not isinstance(source_map, Sequence) or isinstance(source_map, (str, bytes, bytearray)):
        return [
            {
                "finding_id": "SOURCE_REFS_MISSING",
                "severity": "warning",
                "code": "SOURCE_REFS_MISSING",
                "message": "Compiled authority has no source_map review evidence.",
                "candidate_ids": [],
                "source_unit_ids": [],
                "override_allowed": True,
            }
        ]
    item_ids = {item.id for item in spec_artifact.items}
    invalid_locations: list[str] = []
    usable_locations = 0
    for entry in source_map:
        if not isinstance(entry, Mapping):
            continue
        item_id = _source_ref_item_id(entry.get("location"))
        if item_id is None:
            continue
        usable_locations += 1
        if item_id not in item_ids:
            invalid_locations.append(str(entry.get("location")))
    if invalid_locations:
        return [
            {
                "finding_id": "SOURCE_REF_INVALID",
                "severity": "blocking",
                "code": "SOURCE_REF_INVALID",
                "message": "Compiled authority source_map references unknown spec item IDs.",
                "candidate_ids": [],
                "source_unit_ids": [],
                "override_allowed": False,
                "details": {"invalid_locations": sorted(set(invalid_locations))},
            }
        ]
    if usable_locations == 0:
        return [
            {
                "finding_id": "SOURCE_REFS_MISSING",
                "severity": "warning",
                "code": "SOURCE_REFS_MISSING",
                "message": "Compiled authority source_map has no structured spec item references.",
                "candidate_ids": [],
                "source_unit_ids": [],
                "override_allowed": True,
            }
        ]
    return []
```

- [ ] **Step 4: Make structured review skip coverage/candidate engine**

In `build_authority_review_snapshot`, after `artifact, authority_evidence, classification_evidence = _authority_artifact_payload(authority)`, add:

```python
    structured_artifact = _structured_artifact_from_text(source.text)
    if structured_artifact is not None:
        outline: list[JsonDict] = []
        coverage_summary = {
            "covered_sections": 0,
            "partial_sections": 0,
            "uncovered_sections": 0,
            "intentionally_classified_sections": 0,
            "unclassified_content_blocks": 0,
            "omission_assessment": "complete",
        }
        diagnostics: list[JsonDict] = []
    else:
        outline, coverage_summary, diagnostics = _coverage_payload(
            text=source.text,
            authority_evidence=authority_evidence,
            classification_evidence=classification_evidence,
        )
```

Remove the old unconditional `_coverage_payload(...)` call.

- [ ] **Step 5: Remove coverage gap injection for structured specs**

Replace:

```python
    artifact = _artifact_with_coverage_gaps(...)
```

with:

```python
    if structured_artifact is None:
        artifact = _artifact_with_coverage_gaps(
            artifact,
            outline=outline,
            coverage_summary=coverage_summary,
            diagnostics=diagnostics,
        )
```

- [ ] **Step 6: Replace `_authority_ir_payload` with structural payload**

Replace `_authority_ir_payload` body with:

```python
def _authority_ir_payload(
    *,
    authority: CompiledSpecAuthority,
    diagnostics: Sequence[Mapping[str, Any]],
    artifact: Mapping[str, Any],
    structured_artifact: TechnicalSpecArtifact | None,
) -> JsonDict:
    """Build public review metadata without host semantic candidate coverage."""
    diagnostic_findings = _diagnostic_review_findings(diagnostics)
    source_ref_findings = _structured_source_ref_findings(
        artifact=artifact,
        spec_artifact=structured_artifact,
    )
    rendered_findings = [
        *[_finding_payload(finding) for finding in diagnostic_findings],
        *source_ref_findings,
    ]
    return {
        "source_units": [],
        "authority_mappings": [],
        "review_findings": rendered_findings,
        "ir_provenance": "not_applicable",
        "coverage_summary": {
            "blocking_finding_count": sum(
                1 for finding in rendered_findings if finding.get("severity") == "blocking"
            ),
            "mapping_count": 0,
            "covered_mapping_count": 0,
            "weak_mapping_count": 0,
            "intentionally_classified_mapping_count": 0,
            "partial_mapping_count": 0,
            "has_incomplete_coverage": False,
        },
        "coverage_diagnostics": diagnostics,
        "ir_packet_limits": {
            "max_findings": authority_ir.MAX_REVIEW_FINDINGS,
            "truncated": False,
        },
    }
```

Update the call site to pass `artifact=artifact` and `structured_artifact=structured_artifact`.

- [ ] **Step 7: Run Task 5 tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_authority_review.py::test_review_does_not_block_structured_spec_on_candidate_coverage \
  tests/test_agent_workbench_authority_review.py::test_review_warns_when_structured_source_refs_are_missing \
  tests/test_agent_workbench_authority_review.py::test_review_blocks_structured_invalid_source_ref \
  -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add services/agent_workbench/authority_review.py tests/test_agent_workbench_authority_review.py
git commit -m "fix: make authority review structural for profile specs"
```

---

### Task 7: Accept Policy Tests

**Files:**
- Modify: `tests/test_agent_workbench_authority_decision.py`
- Modify: `tests/test_agent_workbench_authority_decision_cli.py`

- [ ] **Step 1: Replace candidate current-blocker test**

Replace `test_accept_blocks_current_ir_candidate_findings` with:

```python
def test_accept_ignores_candidate_and_coverage_findings_defensively(
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removed host semantic findings do not block human authority acceptance."""
    _make_schema_v3_ready(_engine(session))
    project_id, _spec_version_id, _authority_id, _path = _seed_pending_review_project(
        session,
        tmp_path=tmp_path,
    )
    snapshot = _snapshot(session, project_id)
    findings = [
        {
            "finding_id": "AUTHORITY_CANDIDATE_UNCOVERED:REQ-1",
            "severity": "blocking",
            "code": "AUTHORITY_CANDIDATE_UNCOVERED",
            "message": "Removed host semantic candidate finding.",
            "candidate_ids": ["REQ-1"],
            "source_unit_ids": [],
            "override_allowed": True,
        },
        {
            "finding_id": "AUTHORITY_COVERAGE_INCOMPLETE:REQ-1",
            "severity": "blocking",
            "code": "AUTHORITY_COVERAGE_INCOMPLETE",
            "message": "Removed host semantic coverage finding.",
            "candidate_ids": ["REQ-1"],
            "source_unit_ids": [],
            "override_allowed": False,
        },
    ]
    monkeypatch.setattr(
        "services.agent_workbench.authority_decision.build_authority_review_snapshot",
        lambda **_kwargs: replace(snapshot, review_findings=findings),
    )

    result = _runner(session, _workflow_for(project_id)).accept(
        _accept_request(project_id=project_id, review_token=snapshot.review_token)
    )

    assert result["ok"] is True
    assert _terminal_rows(session)[0].status == "accepted"
```

- [ ] **Step 2: Add structural finding still blocks test**

Add:

```python
def test_accept_blocks_invalid_source_ref_finding(
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structural source-ref findings still block accept."""
    _make_schema_v3_ready(_engine(session))
    project_id, _spec_version_id, _authority_id, _path = _seed_pending_review_project(
        session,
        tmp_path=tmp_path,
    )
    snapshot = _snapshot(session, project_id)
    invalid_source_ref = {
        "finding_id": "SOURCE_REF_INVALID",
        "severity": "blocking",
        "code": "SOURCE_REF_INVALID",
        "message": "Compiled authority source_map references unknown spec item IDs.",
        "candidate_ids": [],
        "source_unit_ids": [],
        "override_allowed": False,
    }
    monkeypatch.setattr(
        "services.agent_workbench.authority_decision.build_authority_review_snapshot",
        lambda **_kwargs: replace(snapshot, review_findings=[invalid_source_ref]),
    )

    result = _runner(session, _workflow_for(project_id)).accept(
        _accept_request(project_id=project_id, review_token=snapshot.review_token)
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_REVIEW_INCOMPLETE"
    assert result["errors"][0]["details"]["blocking_findings"][0]["code"] == (
        "SOURCE_REF_INVALID"
    )
```

- [ ] **Step 3: Add CLI simple accept test**

In `tests/test_agent_workbench_authority_decision_cli.py`, add:

```python
def test_authority_accept_without_token_uses_latest_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Human CLI can accept with project id only when latest review is fresh."""
    project_id = 42
    captured_requests: list[AuthorityAcceptRequest] = []

    class FakeApplication:
        def authority_review(self, *, project_id: int, include_spec: str, output_format: str) -> dict[str, Any]:
            assert include_spec == "auto"
            assert output_format == "json"
            return {
                "ok": True,
                "data": {
                    "review_token": "review-token-123",
                    "review_summary": {"acceptance_status": "accept_ready"},
                },
                "errors": [],
                "warnings": [],
            }

        def authority_accept(self, request: AuthorityAcceptRequest) -> dict[str, Any]:
            captured_requests.append(request)
            return {"ok": True, "data": {"accepted_decision_id": 7}, "errors": [], "warnings": []}

    monkeypatch.setattr("cli.main.AgentWorkbenchApplication", lambda **_kwargs: FakeApplication())

    exit_code = main(["authority", "accept", "--project-id", str(project_id)])

    payload = _captured_payload(capsys)
    assert exit_code == 0
    assert payload["ok"] is True
    assert captured_requests[0].review_token == "review-token-123"
    assert captured_requests[0].idempotency_key.startswith("authority-accept-42-")
```

- [ ] **Step 4: Run tests and confirm failure**

Run:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_authority_decision.py::test_accept_ignores_candidate_and_coverage_findings_defensively \
  tests/test_agent_workbench_authority_decision.py::test_accept_blocks_invalid_source_ref_finding \
  tests/test_agent_workbench_authority_decision_cli.py::test_authority_accept_without_token_uses_latest_review \
  -q
```

Expected: FAIL because current accept blocks coverage marker and CLI still requires token/idempotency.

### Task 8: Accept Policy And Simple CLI Implementation

**Files:**
- Modify: `services/agent_workbench/authority_decision.py`
- Modify: `cli/main.py`
- Modify: `services/agent_workbench/application.py`

- [ ] **Step 1: Defensively ignore removed semantic coverage findings**

Replace `_blocking_review_findings` in `services/agent_workbench/authority_decision.py` with:

```python
def _blocking_review_findings(findings: list[JsonDict]) -> list[JsonDict]:
    removed_host_semantic_codes = {
        "AUTHORITY_COVERAGE_INCOMPLETE",
        "AUTHORITY_CANDIDATE_UNCOVERED",
        "AUTHORITY_CANDIDATE_WEAK_MAPPING",
        "AUTHORITY_CANDIDATE_INTENTIONALLY_CLASSIFIED",
        "AUTHORITY_CANDIDATE_PARTIAL",
        "AUTHORITY_CANDIDATE_UNCERTAIN",
    }
    return [
        finding
        for finding in findings
        if finding.get("severity") == "blocking"
        and str(finding.get("code") or "") not in removed_host_semantic_codes
    ]
```

- [ ] **Step 2: Add CLI auto idempotency key helper**

In `cli/main.py`, add near authority decision helpers:

```python
def _auto_authority_idempotency_key(
    *,
    action: str,
    project_id: int,
    review_token: str,
) -> str:
    digest = hashlib.sha256(review_token.encode("utf-8")).hexdigest()[:16]
    return f"authority-{action}-{project_id}-{digest}"
```

In `cli/main.py`, add `import hashlib` with the other standard-library imports.

- [ ] **Step 3: Add latest review token helper**

In `cli/main.py`, add:

```python
def _latest_review_token_or_error(
    *,
    application: AgentWorkbenchApplication,
    project_id: int,
    command: str,
) -> str | dict[str, Any]:
    review = application.authority_review(
        project_id=project_id,
        include_spec="auto",
        output_format="json",
    )
    if not review.get("ok"):
        return review
    data = review.get("data")
    if not isinstance(data, dict) or not data.get("review_token"):
        return error_envelope(
            command=command,
            error=workbench_error(
                ErrorCode.AUTHORITY_REVIEW_REQUIRED,
                message="Run authority review before accepting authority.",
                remediation=[f"agileforge authority review --project-id {project_id}"],
            ),
        )
    summary = data.get("review_summary")
    if isinstance(summary, dict) and summary.get("acceptance_status") == "blocked":
        return error_envelope(
            command=command,
            error=workbench_error(
                ErrorCode.AUTHORITY_REVIEW_INCOMPLETE,
                message="Authority review has structural blocking findings.",
                details={"review_summary": summary},
                remediation=[f"agileforge authority review --project-id {project_id} --open"],
            ),
        )
    return str(data["review_token"])
```

- [ ] **Step 4: Route missing token/idempotency through auto path**

In `_route_authority_accept`, before constructing `AuthorityAcceptRequest`, add logic:

```python
    review_token = args.review_token
    if not review_token:
        token_result = _latest_review_token_or_error(
            application=application,
            project_id=args.project_id,
            command=command,
        )
        if isinstance(token_result, dict):
            return command, token_result
        review_token = token_result
    idempotency_key = args.idempotency_key or _auto_authority_idempotency_key(
        action="accept",
        project_id=args.project_id,
        review_token=review_token,
    )
```

Pass `review_token=review_token` and `idempotency_key=idempotency_key` into `AuthorityAcceptRequest`.

- [ ] **Step 5: Update project create and workflow next next-actions**

In `services/agent_workbench/application.py`, change authority pending review action command from:

```text
agileforge authority review --project-id {project_id}
```

to:

```text
agileforge authority review --project-id {project_id} --open
```

Change accept command text in next actions from token/idempotency-heavy form to:

```text
agileforge authority accept --project-id {project_id}
```

Keep token/idempotency fields in structured metadata only if needed by machine consumers.

- [ ] **Step 6: Run Task 7 tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_authority_decision.py::test_accept_ignores_candidate_and_coverage_findings_defensively \
  tests/test_agent_workbench_authority_decision.py::test_accept_blocks_invalid_source_ref_finding \
  tests/test_agent_workbench_authority_decision_cli.py::test_authority_accept_without_token_uses_latest_review \
  -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add \
  services/agent_workbench/authority_decision.py \
  cli/main.py \
  services/agent_workbench/application.py \
  tests/test_agent_workbench_authority_decision.py \
  tests/test_agent_workbench_authority_decision_cli.py
git commit -m "fix: allow human authority acceptance after structural review"
```

---

### Task 9: Command Schema And Docs Cleanup

**Files:**
- Modify: `tests/test_agent_workbench_command_schema.py`
- Modify: `tests/test_agent_workbench_application.py`
- Modify: `docs/agent-cli-manual.md`

- [ ] **Step 1: Update command schema tests**

In `tests/test_agent_workbench_command_schema.py`, update authority accept schema expectations:

```python
assert "project_id" in accept_schema["input_required"]
assert "review_token" not in accept_schema["input_required"]
assert "idempotency_key" not in accept_schema["input_required"]
```

Assert project create errors include:

```python
assert "SPEC_SOURCE_FORMAT_UNSUPPORTED" in project_create_schema["errors"]
```

- [ ] **Step 2: Update application next-action tests**

In `tests/test_agent_workbench_application.py`, update expected next action strings:

```python
assert "agileforge authority review --project-id 1 --open" in command_text
assert "agileforge authority accept --project-id 1" in command_text
assert "--review-token" not in command_text
```

- [ ] **Step 3: Update manual**

In `docs/agent-cli-manual.md`, replace authority setup guidance with:

```markdown
### Structured Spec Authority Flow

Authority compilation accepts only `agileforge.spec.v1` JSON.

Use:

```bash
agileforge project create --name "Project Name" --spec-file specs/spec.json
agileforge authority review --project-id <project_id> --open
agileforge authority accept --project-id <project_id>
```

Markdown specs are render views or source material for a separate structured-spec generation step. They are not accepted by `project create`.

Host validation checks schema, freshness, compiled artifact shape, and source-reference item IDs. It does not block acceptance based on generated requirement candidates or inferred semantic coverage.
```
```

Remove or rewrite sections that say:

- `pending_authority.requirement_candidates`
- `pending_authority.authority_mappings`
- `review_findings` candidate blockers
- candidate-specific override requirements
- "Never recommend acceptance while any blocking review_findings entry exists" when the finding is one of the removed host semantic codes

- [ ] **Step 4: Run schema/docs tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_command_schema.py \
  tests/test_agent_workbench_application.py \
  -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add \
  tests/test_agent_workbench_command_schema.py \
  tests/test_agent_workbench_application.py \
  docs/agent-cli-manual.md
git commit -m "docs: simplify structured authority workflow"
```

---

### Task 10: Full Verification And Smoke

**Files:**
- No implementation files.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
uv run --frozen pytest \
  tests/test_specs_compiler_service.py \
  tests/test_agent_workbench_project_create_cli_integration.py \
  tests/test_spec_authority_compiler_normalizer.py \
  tests/test_spec_authority_compiler_agent.py \
  tests/test_agent_workbench_authority_review.py \
  tests/test_agent_workbench_authority_decision.py \
  tests/test_agent_workbench_authority_decision_cli.py \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_command_schema.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run ruff**

Run:

```bash
uv run --frozen ruff check \
  services/specs/profile_content.py \
  services/agent_workbench/error_codes.py \
  services/agent_workbench/command_registry.py \
  services/specs/pending_authority_service.py \
  services/specs/compiler_service.py \
  services/agent_workbench/project_setup.py \
  services/agent_workbench/project_setup_fingerprints.py \
  orchestrator_agent/agent_tools/spec_authority_compiler_agent/compiler_contract.py \
  orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py \
  services/agent_workbench/authority_review.py \
  services/agent_workbench/authority_decision.py \
  services/agent_workbench/application.py \
  cli/main.py \
  tests/test_specs_compiler_service.py \
  tests/test_agent_workbench_project_create_cli_integration.py \
  tests/test_spec_authority_compiler_normalizer.py \
  tests/test_spec_authority_compiler_agent.py \
  tests/test_agent_workbench_authority_review.py \
  tests/test_agent_workbench_authority_decision.py \
  tests/test_agent_workbench_authority_decision_cli.py \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_command_schema.py
```

Expected: PASS.

- [ ] **Step 3: Run local CLI smoke from caRtola**

Use a fresh key and the structured spec:

```bash
cd /Users/aaat/projects/caRtola
agileforge project create \
  --name "caRtola Trust Boundary Smoke" \
  --spec-file specs/spec.json \
  --idempotency-key cartola-trust-boundary-smoke-20260519
```

Expected:

```text
ok=true
setup_status=authority_pending_review
next action includes agileforge authority review --project-id <id> --open
```

- [ ] **Step 4: Review and accept smoke project**

Run with the returned project id:

```bash
agileforge authority review --project-id 2 --include-spec full
agileforge authority accept --project-id 2
agileforge status --project-id 2
```

Expected:

```text
review_summary.acceptance_status=accept_ready
no AUTHORITY_CANDIDATE_* findings
no AUTHORITY_COVERAGE_INCOMPLETE finding
authority accept ok=true
authority status accepted/current
```

- [ ] **Step 5: Verify Markdown rejection smoke**

Run:

```bash
agileforge project create \
  --name "Markdown Reject Smoke" \
  --spec-file specs/app.md \
  --idempotency-key markdown-reject-smoke-20260519
```

Expected:

```text
ok=false
error code SPEC_SOURCE_FORMAT_UNSUPPORTED
remediation says generate specs/spec.json
```

- [ ] **Step 6: Commit final verification notes if docs changed**

If smoke results require documentation updates, commit them:

```bash
git add docs/agent-cli-manual.md
git commit -m "docs: record structured authority smoke workflow"
```

If no docs changed, do not create an empty commit.

---

## Self-Review Checklist

- Spec coverage:
  - Structured JSON-only input contract: Tasks 1 and 2.
  - No legacy Markdown authority behavior: Tasks 1, 2, 9, and 10.
  - No host semantic candidate blockers: Tasks 3, 4, 5, 6, and 7.
  - Human accept path: Tasks 7, 8, and 10.
  - Structural blockers preserved: Tasks 5, 6, and 7.
  - Documentation updated: Task 9.
- Placeholder scan:
  - No placeholder markers or undefined task references.
- Type consistency:
  - `SPEC_SOURCE_FORMAT_UNSUPPORTED` is added to `ErrorCode`, propagated through services, and asserted in tests.
  - Source-ref findings use `SOURCE_REFS_MISSING` and `SOURCE_REF_INVALID` consistently.
  - Removed semantic coverage codes are ignored defensively but not emitted by structured review.
