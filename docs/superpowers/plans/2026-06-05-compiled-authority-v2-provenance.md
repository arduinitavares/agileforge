# Compiled Authority V2 Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement v2 compiled authority provenance schema, fail-closed unsupported artifact loading, and explicit authority regeneration without legacy fallback.

**Architecture:** Make `utils.spec_schemas` the source of truth for v2 compiled authority. All stored compiled-authority reads go through one raw-sniffing loader in `services/specs/compiler_service.py`, which returns a typed result instead of collapsing unsupported and invalid artifacts into `None`. Fresh compiler output may receive pre-validation drift repair, but stored artifacts never migrate silently. Regeneration is a first-class workbench mutation that recompiles an approved spec version and stops at pending authority review.

**Tech Stack:** Python 3.13, Pydantic v2, SQLModel, argparse CLI, workbench JSON envelopes, uv, pytest, Ruff.

---

## Contract Decisions

- `SpecAuthorityCompilationSuccess.schema_version` must equal `agileforge.compiled_authority.v2`.
- `SPEC_AUTHORITY_COMPILER_VERSION` becomes `2.0.0`.
- Stored artifacts with missing or non-v2 `schema_version` return `COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED` before Pydantic validation.
- Stored artifacts receive no compatibility migration, no fallback accessors, and no legacy param-level provenance support.
- Fresh compiler output may move `parameters.source_item_id` and `parameters.source_level` to top-level `Invariant.source_item_id` and `Invariant.source_level` before strict validation.
- `parameters` remain type-specific semantic payloads only and keep `extra="forbid"`.
- `SourceMapEntry.location` remains the item-reference location field. Do not add `source_item_id` to `SourceMapEntry` in this pass.
- `source_item_id` and `source_level` are provenance hints, not evidence. `source_map.excerpt` must resolve to real `agileforge.spec.v1` item text when structured source proof is required.
- Deterministic invariant IDs ignore provenance and source-map evidence; hash only invariant `type` and semantic `parameters`.
- `authority regenerate --dry-run` ships in this pass. It validates guards and returns a would-regenerate envelope without compiling or mutating domain rows.
- Existing accepted authority is considered non-current through projection-level compiler/prompt/spec fingerprint mismatch. Do not add a DB status migration for accepted rows.
- Dashboard/API unsupported-artifact responses use structured error objects; HTTP endpoints return `409 Conflict` with `COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED`.
- No real `agileforge project create` runtime smoke belongs in this implementation plan. Use saved fixtures and unit/integration tests.

## File Structure

- Modify `utils/spec_schemas.py`: v2 schema fields, top-level invariant provenance, semantic-only parameter schemas.
- Modify `orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions_source.py`: compiler version `2.0.0`.
- Modify `orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions.txt`: v2 output contract and provenance placement.
- Modify `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`: fresh-output drift repair, deterministic ID hashing, v2 source validators, schema-only retry decision hooks if colocated.
- Modify `services/specs/compiler_service.py`: central raw-sniff loader, typed loader result, unsupported-artifact envelope helper, compiler persistence of v2 artifacts, schema-only retry.
- Modify `services/agent_workbench/error_codes.py`: register `COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED`.
- Create `services/agent_workbench/authority_regenerate.py`: mutation-ledger guarded regenerate runner and request model.
- Modify `services/agent_workbench/application.py`: application facade protocol and method for regenerate.
- Modify `services/agent_workbench/command_registry.py`: register `agileforge authority regenerate`.
- Modify `cli/main.py`: parser and handler for `agileforge authority regenerate`.
- Modify `services/agent_workbench/authority_review.py`: use central loader result and fail closed for unsupported stored artifacts.
- Modify `services/agent_workbench/authority_projection.py`: expose unsupported-artifact status and regeneration remediation.
- Modify `api.py`: return structured `409` unsupported-artifact responses for dashboard/API authority readers.
- Modify `services/orchestrator_context_service.py`: do not cache or backfill unsupported old artifacts.
- Modify `services/setup_service.py`: surface unsupported-artifact regenerate guidance without crashing.
- Modify `services/specs/story_validation_service.py`: fail closed when only unsupported compiled authority exists.
- Modify `services/agent_workbench/as_built_assessment.py`: read top-level invariant provenance and `SourceMapEntry.location`, not raw `parameters.source_item_id`.
- Modify `services/agent_workbench/evidence_collect.py`: read top-level invariant provenance and `SourceMapEntry.location`, not raw `parameters.source_item_id`.
- Modify `docs/agent-cli-manual.md`: document v2 provenance and `authority regenerate`.

## Shared Test Helpers

Use this fixture shape in tests that need compiled authority JSON:

```python
def v2_compiled_authority_payload() -> dict[str, object]:
    return {
        "schema_version": "agileforge.compiled_authority.v2",
        "scope_themes": ["intake"],
        "domain": "operations",
        "invariants": [
            {
                "id": "INV-1111111111111111",
                "type": "USER_INTERACTION",
                "source_item_id": "REQ.intake-form",
                "source_level": "MUST",
                "parameters": {
                    "trigger": "user opens intake",
                    "target": "intake form",
                    "expected_response": "show required intake fields",
                },
            }
        ],
        "eligible_feature_rules": [],
        "rejected_features": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-1111111111111111",
                "excerpt": "MUST provide an intake form with required fields.",
                "location": "REQ.intake-form",
            }
        ],
        "compiler_version": "2.0.0",
        "prompt_hash": "a" * 64,
        "ir_schema_version": None,
        "ir_provenance": None,
    }
```

Use this fixture shape for legacy stored artifacts:

```python
def legacy_compiled_authority_payload() -> dict[str, object]:
    payload = v2_compiled_authority_payload()
    payload.pop("schema_version")
    invariant = payload["invariants"][0]  # type: ignore[index]
    assert isinstance(invariant, dict)
    parameters = invariant["parameters"]
    assert isinstance(parameters, dict)
    parameters["source_item_id"] = invariant.pop("source_item_id")
    parameters["source_level"] = invariant.pop("source_level")
    payload["compiler_version"] = "1.0.0"
    return payload
```

## Task 1: Register Unsupported Artifact Error And Loader Result Contract

**Files:**
- Modify `services/agent_workbench/error_codes.py`
- Modify `services/specs/compiler_service.py`
- Test `tests/test_agent_workbench_error_codes.py`
- Test `tests/test_specs_compiler_service.py`

- [ ] **Step 1: Write failing error-code test**

Add this test to `tests/test_agent_workbench_error_codes.py`:

```python
def test_compiled_authority_schema_unsupported_error_is_registered() -> None:
    from services.agent_workbench.error_codes import ErrorCode, error_metadata

    metadata = error_metadata(ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED)

    assert metadata.code == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
    assert metadata.default_exit_code == 4
    assert metadata.retryable is False
    assert metadata.description == "Compiled authority artifact schema is unsupported."
```

- [ ] **Step 2: Verify the error-code test fails**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_error_codes.py::test_compiled_authority_schema_unsupported_error_is_registered -q
```

Expected: fail because `ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED` does not exist.

- [ ] **Step 3: Register the error code**

In `services/agent_workbench/error_codes.py`, add the enum member next to the authority errors:

```python
COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED = "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
```

Add registry metadata:

```python
ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED: ErrorMetadata(
    code=ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED.value,
    default_exit_code=4,
    retryable=False,
    description="Compiled authority artifact schema is unsupported.",
),
```

- [ ] **Step 4: Write failing loader-result tests**

In `tests/test_specs_compiler_service.py`, replace the current `load_compiled_artifact` `None`-only assertions with explicit result assertions:

```python
def test_load_compiled_artifact_returns_success_result_for_v2_payload() -> None:
    from types import SimpleNamespace

    from services.specs.compiler_service import load_compiled_artifact

    authority = SimpleNamespace(
        compiled_artifact_json=json.dumps(v2_compiled_authority_payload())
    )

    result = load_compiled_artifact(authority)

    assert result.status == "success"
    assert result.artifact is not None
    assert result.error_code is None
    assert result.observed_schema_version == "agileforge.compiled_authority.v2"


def test_load_compiled_artifact_raw_sniffs_missing_schema_version() -> None:
    from types import SimpleNamespace

    from services.specs.compiler_service import load_compiled_artifact

    authority = SimpleNamespace(
        compiled_artifact_json=json.dumps(legacy_compiled_authority_payload())
    )

    result = load_compiled_artifact(authority)

    assert result.status == "schema_unsupported"
    assert result.artifact is None
    assert result.error_code == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
    assert result.observed_schema_version is None


def test_load_compiled_artifact_raw_sniffs_wrong_schema_version() -> None:
    from types import SimpleNamespace

    from services.specs.compiler_service import load_compiled_artifact

    payload = v2_compiled_authority_payload()
    payload["schema_version"] = "agileforge.compiled_authority.v1"
    authority = SimpleNamespace(compiled_artifact_json=json.dumps(payload))

    result = load_compiled_artifact(authority)

    assert result.status == "schema_unsupported"
    assert result.error_code == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
    assert result.observed_schema_version == "agileforge.compiled_authority.v1"
```

- [ ] **Step 5: Verify loader tests fail**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py::test_load_compiled_artifact_returns_success_result_for_v2_payload tests/test_specs_compiler_service.py::test_load_compiled_artifact_raw_sniffs_missing_schema_version tests/test_specs_compiler_service.py::test_load_compiled_artifact_raw_sniffs_wrong_schema_version -q
```

Expected: fail because `load_compiled_artifact` still returns `SpecAuthorityCompilationSuccess | None`.

- [ ] **Step 6: Implement typed loader result**

In `services/specs/compiler_service.py`, add this dataclass near the current loader:

```python
COMPILED_AUTHORITY_SCHEMA_VERSION: str = "agileforge.compiled_authority.v2"


@dataclass(frozen=True)
class CompiledArtifactLoadResult:
    """Result of loading a stored compiled-authority artifact."""

    status: Literal[
        "success",
        "missing",
        "invalid_json",
        "schema_invalid",
        "schema_unsupported",
        "compiler_failure",
    ]
    artifact: SpecAuthorityCompilationSuccess | None = None
    error_code: str | None = None
    message: str | None = None
    observed_schema_version: str | None = None
    validation_error: str | None = None

    @property
    def ok(self) -> bool:
        """Return whether the stored artifact is a supported success object."""
        return self.status == "success" and self.artifact is not None

    @property
    def unsupported(self) -> bool:
        """Return whether the stored artifact must be regenerated."""
        return self.status == "schema_unsupported"
```

Replace `load_compiled_artifact` with this raw-sniffing implementation:

```python
def load_compiled_artifact(authority: object) -> CompiledArtifactLoadResult:
    """Load stored compiled authority with raw schema-version sniffing."""
    artifact_json = getattr(authority, "compiled_artifact_json", None)
    if not artifact_json:
        return CompiledArtifactLoadResult(
            status="missing",
            message="compiled_artifact_json is missing.",
        )

    try:
        payload = json.loads(str(artifact_json))
    except (TypeError, json.JSONDecodeError) as exc:
        return CompiledArtifactLoadResult(
            status="invalid_json",
            message="compiled_artifact_json is not valid JSON.",
            validation_error=str(exc),
        )

    if not isinstance(payload, dict):
        return CompiledArtifactLoadResult(
            status="schema_invalid",
            message="compiled_artifact_json must be a JSON object.",
        )

    observed_schema_version = payload.get("schema_version")
    if observed_schema_version != COMPILED_AUTHORITY_SCHEMA_VERSION:
        observed = (
            observed_schema_version if isinstance(observed_schema_version, str) else None
        )
        return CompiledArtifactLoadResult(
            status="schema_unsupported",
            error_code=ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED.value,
            message=(
                "Compiled authority artifact schema is unsupported; regenerate "
                "compiled authority from the approved spec."
            ),
            observed_schema_version=observed,
        )

    try:
        parsed = SpecAuthorityCompilerOutput.model_validate(payload)
    except (ValidationError, ValueError) as exc:
        return CompiledArtifactLoadResult(
            status="schema_invalid",
            message="compiled_artifact_json failed v2 schema validation.",
            observed_schema_version=COMPILED_AUTHORITY_SCHEMA_VERSION,
            validation_error=str(exc),
        )

    if isinstance(parsed.root, SpecAuthorityCompilationFailure):
        return CompiledArtifactLoadResult(
            status="compiler_failure",
            message="compiled_artifact_json contains a compiler failure object.",
            observed_schema_version=COMPILED_AUTHORITY_SCHEMA_VERSION,
        )

    return CompiledArtifactLoadResult(
        status="success",
        artifact=parsed.root,
        observed_schema_version=COMPILED_AUTHORITY_SCHEMA_VERSION,
    )
```

Add `ErrorCode` to the imports:

```python
from services.agent_workbench.error_codes import ErrorCode
```

- [ ] **Step 7: Update direct loader call sites in `compiler_service.py`**

Change truthy checks from:

```python
artifact = load_compiled_artifact(authority)
if not artifact:
    raise SpecAuthorityAcceptanceError.invalid_artifact(spec_version_id)
```

to:

```python
load_result = load_compiled_artifact(authority)
if not load_result.ok:
    raise SpecAuthorityAcceptanceError.invalid_artifact(spec_version_id)
artifact = load_result.artifact
assert artifact is not None
```

Apply this pattern only inside `services/specs/compiler_service.py` in this task.

- [ ] **Step 8: Verify Task 1 tests pass**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_error_codes.py tests/test_specs_compiler_service.py::test_load_compiled_artifact_returns_success_result_for_v2_payload tests/test_specs_compiler_service.py::test_load_compiled_artifact_raw_sniffs_missing_schema_version tests/test_specs_compiler_service.py::test_load_compiled_artifact_raw_sniffs_wrong_schema_version -q
```

Expected: pass.

- [ ] **Step 9: Commit Task 1**

Run:

```bash
git add services/agent_workbench/error_codes.py services/specs/compiler_service.py tests/test_agent_workbench_error_codes.py tests/test_specs_compiler_service.py
git commit -m "feat: raw-sniff compiled authority schema versions"
```

## Task 2: V2 Schema Shape And Semantic-Only Parameters

**Files:**
- Modify `utils/spec_schemas.py`
- Modify `orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions_source.py`
- Test `tests/test_spec_authority.py`
- Test `tests/test_spec_schema_modules.py`

- [ ] **Step 1: Write failing schema tests**

Add these tests to `tests/test_spec_authority.py`:

```python
def test_compiled_authority_v2_requires_schema_version() -> None:
    from pydantic import ValidationError

    from utils.spec_schemas import SpecAuthorityCompilationSuccess

    payload = v2_compiled_authority_payload()
    payload.pop("schema_version")

    with pytest.raises(ValidationError):
        SpecAuthorityCompilationSuccess.model_validate(payload)


def test_invariant_accepts_top_level_source_metadata() -> None:
    from utils.spec_schemas import SpecAuthorityCompilationSuccess

    artifact = SpecAuthorityCompilationSuccess.model_validate(
        v2_compiled_authority_payload()
    )

    invariant = artifact.invariants[0]
    assert invariant.source_item_id == "REQ.intake-form"
    assert invariant.source_level == "MUST"


def test_behavioral_parameters_reject_source_metadata() -> None:
    from pydantic import ValidationError

    from utils.spec_schemas import SpecAuthorityCompilationSuccess

    payload = v2_compiled_authority_payload()
    invariant = payload["invariants"][0]
    assert isinstance(invariant, dict)
    parameters = invariant["parameters"]
    assert isinstance(parameters, dict)
    parameters["source_item_id"] = "REQ.intake-form"
    parameters["source_level"] = "MUST"

    with pytest.raises(ValidationError) as exc_info:
        SpecAuthorityCompilationSuccess.model_validate(payload)

    assert "Extra inputs are not permitted" in str(exc_info.value)
```

- [ ] **Step 2: Verify schema tests fail**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority.py::test_compiled_authority_v2_requires_schema_version tests/test_spec_authority.py::test_invariant_accepts_top_level_source_metadata tests/test_spec_authority.py::test_behavioral_parameters_reject_source_metadata -q
```

Expected: fail because v2 fields are not present and behavioral params still include source metadata.

- [ ] **Step 3: Update schema models**

In `utils/spec_schemas.py`, remove `source_item_id` and `source_level` fields from `BehavioralAuthorityParams`, leaving the base class as:

```python
class BehavioralAuthorityParams(BaseModel):
    """Base class for behavioral authority parameters."""

    model_config = ConfigDict(extra="forbid")
```

Add top-level provenance to `Invariant`:

```python
class Invariant(BaseModel):
    """Structured invariant extracted from the specification."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[
        str,
        Field(pattern=r"^INV-[0-9a-f]{16}$", description="Stable invariant ID."),
    ]
    type: InvariantType
    source_item_id: Annotated[
        str | None,
        Field(default=None, min_length=1, description="Structured spec item ID."),
    ] = None
    source_level: Annotated[
        SpecAuthoritySourceLevel | None,
        Field(default=None, description="Normative level of the source item."),
    ] = None
    parameters: InvariantParameters
```

Add schema version to `SpecAuthorityCompilationSuccess` before `scope_themes`:

```python
schema_version: Annotated[
    Literal["agileforge.compiled_authority.v2"],
    Field(description="Compiled authority artifact schema version."),
] = "agileforge.compiled_authority.v2"
```

- [ ] **Step 4: Bump compiler version**

In `orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions_source.py`, set:

```python
SPEC_AUTHORITY_COMPILER_VERSION: str = "2.0.0"
```

- [ ] **Step 5: Preserve schema re-export tests**

Run:

```bash
uv run --frozen pytest tests/test_spec_schema_modules.py tests/test_spec_authority.py::test_compiled_authority_v2_requires_schema_version tests/test_spec_authority.py::test_invariant_accepts_top_level_source_metadata tests/test_spec_authority.py::test_behavioral_parameters_reject_source_metadata -q
```

Expected: pass.

- [ ] **Step 6: Commit Task 2**

Run:

```bash
git add utils/spec_schemas.py orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions_source.py tests/test_spec_authority.py tests/test_spec_schema_modules.py
git commit -m "feat: define compiled authority v2 provenance schema"
```

## Task 3: Fresh Compiler Output Provenance Repair And Deterministic IDs

**Files:**
- Modify `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`
- Test `tests/test_spec_authority_compiler_normalizer.py`

- [ ] **Step 1: Write failing normalizer drift-repair test**

Add this test to `tests/test_spec_authority_compiler_normalizer.py`:

```python
def test_normalizer_moves_misplaced_source_metadata_to_invariant_top_level() -> None:
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (
        normalize_compiler_output,
    )

    payload = v2_compiled_authority_payload()
    invariant = payload["invariants"][0]
    assert isinstance(invariant, dict)
    parameters = invariant["parameters"]
    assert isinstance(parameters, dict)
    parameters["source_item_id"] = invariant.pop("source_item_id")
    parameters["source_level"] = invariant.pop("source_level")

    result = normalize_compiler_output(
        json.dumps(payload),
        source_items_by_id={
            "REQ.intake-form": {
                "id": "REQ.intake-form",
                "level": "MUST",
                "text": "MUST provide an intake form with required fields.",
            }
        },
    )

    invariant_result = result.invariants[0]
    assert invariant_result.source_item_id == "REQ.intake-form"
    assert invariant_result.source_level == "MUST"
    assert not hasattr(invariant_result.parameters, "source_item_id")
    assert not hasattr(invariant_result.parameters, "source_level")
```

- [ ] **Step 2: Write failing ID-stability test**

Add this test to the same file:

```python
def test_deterministic_invariant_id_ignores_provenance() -> None:
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (
        normalize_compiler_output,
    )

    first = v2_compiled_authority_payload()
    second = v2_compiled_authority_payload()
    first["invariants"][0]["source_item_id"] = "REQ.intake-form"  # type: ignore[index]
    second["invariants"][0]["source_item_id"] = "REQ.other"  # type: ignore[index]
    first["source_map"][0]["location"] = "REQ.intake-form"  # type: ignore[index]
    second["source_map"][0]["location"] = "REQ.other"  # type: ignore[index]

    first_result = normalize_compiler_output(json.dumps(first))
    second_result = normalize_compiler_output(json.dumps(second))

    assert first_result.invariants[0].id == second_result.invariants[0].id
```

- [ ] **Step 3: Verify tests fail**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py::test_normalizer_moves_misplaced_source_metadata_to_invariant_top_level tests/test_spec_authority_compiler_normalizer.py::test_deterministic_invariant_id_ignores_provenance -q
```

Expected: fail because strict validation happens before provenance repair or because ID logic still observes legacy params.

- [ ] **Step 4: Add fresh-output repair helper**

In `normalizer.py`, add a pre-validation helper before the call to `SpecAuthorityCompilerOutput.model_validate`:

```python
_PROVENANCE_KEYS: frozenset[str] = frozenset({"source_item_id", "source_level"})


def _repair_fresh_invariant_provenance(payload: object) -> None:
    """Move misplaced provenance out of fresh invariant parameters before validation."""
    if not isinstance(payload, dict):
        return
    invariants = payload.get("invariants")
    if not isinstance(invariants, list):
        return
    for invariant in invariants:
        if not isinstance(invariant, dict):
            continue
        parameters = invariant.get("parameters")
        if not isinstance(parameters, dict):
            continue
        for key in _PROVENANCE_KEYS:
            value = parameters.pop(key, None)
            if value is not None and invariant.get(key) is None:
                invariant[key] = value
```

Call it only on the freshly parsed compiler payload before strict validation:

```python
payload = _parse_compiler_json(raw_output)
_repair_prompt_hash(payload)
_repair_placeholder_invariant_ids(payload)
_repair_fresh_invariant_provenance(payload)
parsed = SpecAuthorityCompilerOutput.model_validate(payload)
```

Use the current local parse/repair function names if they already exist. Keep the call order before strict Pydantic validation.

- [ ] **Step 5: Update deterministic signature**

In `_semantic_signature_from_invariant_payload`, remove legacy parameter skipping and build the hash from `type` and semantic `parameters` only:

```python
signature_payload = {
    "type": invariant_payload.get("type"),
    "parameters": invariant_payload.get("parameters") or {},
}
```

Do not include `source_item_id`, `source_level`, `source_map`, `excerpt`, or `location`.

- [ ] **Step 6: Verify normalizer tests pass**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py::test_normalizer_moves_misplaced_source_metadata_to_invariant_top_level tests/test_spec_authority_compiler_normalizer.py::test_deterministic_invariant_id_ignores_provenance -q
```

Expected: pass.

- [ ] **Step 7: Commit Task 3**

Run:

```bash
git add orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py tests/test_spec_authority_compiler_normalizer.py
git commit -m "fix: repair fresh compiler provenance before validation"
```

## Task 4: Semantic Source Validation Uses Top-Level Provenance And Real Source Text

**Files:**
- Modify `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`
- Test `tests/test_spec_authority_compiler_normalizer.py`
- Test `tests/test_specs_compiler_service.py`

- [ ] **Step 1: Write failing source-resolution tests**

Add these tests to `tests/test_spec_authority_compiler_normalizer.py`:

```python
def test_top_level_source_item_id_must_resolve_when_structured_proof_required() -> None:
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (
        SpecAuthorityNormalizationError,
        normalize_compiler_output,
    )

    payload = v2_compiled_authority_payload()
    payload["source_map"][0]["location"] = "REQ.unknown"  # type: ignore[index]

    with pytest.raises(SpecAuthorityNormalizationError) as exc_info:
        normalize_compiler_output(
            json.dumps(payload),
            source_items_by_id={
                "REQ.intake-form": {
                    "id": "REQ.intake-form",
                    "level": "MUST",
                    "text": "MUST provide an intake form with required fields.",
                }
            },
        )

    assert "SOURCE_METADATA_MISMATCH" in str(exc_info.value)


def test_source_map_excerpt_must_match_real_source_item_text() -> None:
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (
        SpecAuthorityNormalizationError,
        normalize_compiler_output,
    )

    payload = v2_compiled_authority_payload()
    payload["source_map"][0]["excerpt"] = "fake excerpt"  # type: ignore[index]

    with pytest.raises(SpecAuthorityNormalizationError) as exc_info:
        normalize_compiler_output(
            json.dumps(payload),
            source_items_by_id={
                "REQ.intake-form": {
                    "id": "REQ.intake-form",
                    "level": "MUST",
                    "text": "MUST provide an intake form with required fields.",
                }
            },
        )

    assert "SOURCE_METADATA_MISMATCH" in str(exc_info.value)
```

- [ ] **Step 2: Verify source tests fail**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py::test_top_level_source_item_id_must_resolve_when_structured_proof_required tests/test_spec_authority_compiler_normalizer.py::test_source_map_excerpt_must_match_real_source_item_text -q
```

Expected: fail because validators still read `parameters.source_item_id` or do not bind source-map evidence to real source text strongly enough.

- [ ] **Step 3: Update validator reads**

In `normalizer.py`, replace reads like:

```python
source_item = source_items.get(parameters.source_item_id)
actual_level = source_item.get("level")
if actual_level != parameters.source_level:
    ...
```

with:

```python
source_item_id = invariant.source_item_id
source_level = invariant.source_level
if source_item_id is None or source_level is None:
    errors.append(
        f"{invariant.id} requires top-level source_item_id and source_level."
    )
    return
source_item = source_items.get(source_item_id)
if source_item is None:
    errors.append(f"{invariant.id} references unknown source_item_id {source_item_id}.")
    return
actual_level = source_item.get("level")
if actual_level != source_level:
    errors.append(
        f"{invariant.id} source_item_id {source_item_id} source_level "
        f"{source_level} does not match source item level {actual_level}."
    )
```

Update source-map matching so evidence item IDs come from `SourceMapEntry.location` first, and excerpts must be substrings of the resolved source item text:

```python
def _source_map_entry_item_id(entry: SourceMapEntry) -> str | None:
    return _structured_item_id_from_reference(entry.location)


def _source_map_entry_matches_item(entry: SourceMapEntry, source_item: dict[str, Any]) -> bool:
    source_text = str(source_item.get("text") or "")
    excerpt = entry.excerpt.strip()
    return bool(excerpt) and excerpt in source_text
```

- [ ] **Step 4: Preserve semantic failure no-retry behavior**

Add or update a compiler-service test so semantic source mismatch returns `SPEC_COMPILE_FAILED` with failure stage `output_validation` and does not record a schema retry attempt:

```python
def test_semantic_source_mismatch_does_not_trigger_schema_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def fake_invoke(*args: object, **kwargs: object) -> str:
        attempts.append("attempt")
        payload = v2_compiled_authority_payload()
        payload["source_map"][0]["excerpt"] = "fake excerpt"  # type: ignore[index]
        return json.dumps(payload)

    monkeypatch.setattr(
        "services.specs.compiler_service.invoke_agent_to_text",
        fake_invoke,
    )

    result = compile_spec_authority_for_version(
        {"spec_version_id": 1, "spec_content": valid_structured_spec_json()},
    )

    assert result["ok"] is False
    assert result["failure_artifact_stage"] == "output_validation"
    assert len(attempts) == 1
```

If `compile_spec_authority_for_version` test fixtures require a DB session, place this assertion inside the existing compiler-service fixture style rather than creating a new DB harness.

- [ ] **Step 5: Verify source validation suite passes**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py tests/test_specs_compiler_service.py -q
```

Expected: pass.

- [ ] **Step 6: Commit Task 4**

Run:

```bash
git add orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py tests/test_spec_authority_compiler_normalizer.py tests/test_specs_compiler_service.py
git commit -m "fix: validate authority provenance against real source text"
```

## Task 5: Compiler Instructions And Schema-Only Retry

**Files:**
- Modify `orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions.txt`
- Modify `services/specs/compiler_service.py`
- Test `tests/test_spec_authority_compile_tool.py`
- Test `tests/test_specs_compiler_service.py`

- [ ] **Step 1: Update compiler instructions**

In `instructions.txt`, replace behavioral parameter examples that include provenance:

```text
USER_INTERACTION => parameters: {"source_item_id": "...", "source_level": "...", ...}
```

with v2 examples:

```text
USER_INTERACTION => invariant: {"type": "USER_INTERACTION", "source_item_id": "<typed item id>", "source_level": "MUST|SHOULD|MAY|MUST_NOT", "parameters": {"trigger": "<user event>", "target": "<ui target>", "expected_response": "<required response>"}}
STATE_TRANSITION => invariant: {"type": "STATE_TRANSITION", "source_item_id": "<typed item id>", "source_level": "MUST|SHOULD|MAY|MUST_NOT", "parameters": {"state": "<state>", "trigger": "<event or condition>", "outcome": "<resulting state or side effect>"}}
DATA_CONTRACT => invariant: {"type": "DATA_CONTRACT", "source_item_id": "<typed item id>", "source_level": "MUST|SHOULD|MAY|MUST_NOT", "parameters": {"subject": "<record/key/payload>", "fields": ["<field>"], "rule": "<shape, naming, or persistence rule>"}}
ROUTE_CONTRACT => invariant: {"type": "ROUTE_CONTRACT", "source_item_id": "<typed item id>", "source_level": "MUST|SHOULD|MAY|MUST_NOT", "parameters": {"route": "<route pattern>", "route_name": "<purpose>", "behavior": "<route behavior>"}}
VISIBILITY_RULE => invariant: {"type": "VISIBILITY_RULE", "source_item_id": "<typed item id>", "source_level": "MUST|SHOULD|MAY|MUST_NOT", "parameters": {"target": "<ui element>", "condition": "<condition>", "visibility": "visible|hidden|shown|removed"}}
```

Add this contract text:

```text
Output schema_version MUST be "agileforge.compiled_authority.v2".
Invariant parameters MUST contain semantic fields only.
Do not put source_item_id or source_level inside parameters.
source_map.location MUST be the structured source item id.
source_map.excerpt MUST be copied from the real source item text.
```

- [ ] **Step 2: Write schema-retry tests**

In `tests/test_specs_compiler_service.py`, add tests using the existing fake invocation helpers:

```python
def test_json_validation_failed_gets_one_schema_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def fake_invoke(*args: object, **kwargs: object) -> str:
        attempts.append("attempt")
        if len(attempts) == 1:
            payload = v2_compiled_authority_payload()
            payload["invariants"][0]["parameters"]["source_item_id"] = "REQ.intake-form"  # type: ignore[index]
            return json.dumps(payload)
        return json.dumps(v2_compiled_authority_payload())

    monkeypatch.setattr(
        "services.specs.compiler_service.invoke_agent_to_text",
        fake_invoke,
    )

    result = compile_spec_authority_for_version(
        {"spec_version_id": 1, "spec_content": valid_structured_spec_json()},
    )

    assert result["ok"] is True
    assert len(attempts) == 2
    assert result["schema_retry_attempted"] is True
```

Add a second test:

```python
def test_schema_retry_stops_after_one_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def fake_invoke(*args: object, **kwargs: object) -> str:
        attempts.append("attempt")
        payload = v2_compiled_authority_payload()
        payload["prompt_hash"] = "not-a-sha"
        return json.dumps(payload)

    monkeypatch.setattr(
        "services.specs.compiler_service.invoke_agent_to_text",
        fake_invoke,
    )

    result = compile_spec_authority_for_version(
        {"spec_version_id": 1, "spec_content": valid_structured_spec_json()},
    )

    assert result["ok"] is False
    assert result["failure_artifact_stage"] == "output_validation"
    assert len(attempts) == 2
```

- [ ] **Step 3: Implement retry matrix**

In `compiler_service.py`, wrap normalizer validation so only these stages retry once:

```python
_SCHEMA_RETRY_FAILURES: frozenset[str] = frozenset(
    {"INVALID_JSON", "JSON_VALIDATION_FAILED"}
)
```

On first failure, inspect the failure category emitted by normalizer/failure artifact. If it is in `_SCHEMA_RETRY_FAILURES`, make exactly one retry with a feedback prompt that includes:

```text
Your previous output failed the compiled authority v2 schema.
Return only valid JSON.
Do not put source_item_id or source_level inside parameters.
Place provenance at invariant.source_item_id and invariant.source_level.
schema_version must be "agileforge.compiled_authority.v2".
```

Record these output fields in the compile result:

```python
"schema_retry_attempted": True,
"schema_retry_reason": failure_code,
"schema_retry_attempts": 1,
```

For semantic/source failures, set `schema_retry_attempted` to `False`.

- [ ] **Step 4: Verify retry tests pass**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compile_tool.py tests/test_specs_compiler_service.py::test_json_validation_failed_gets_one_schema_retry tests/test_specs_compiler_service.py::test_schema_retry_stops_after_one_retry tests/test_specs_compiler_service.py::test_semantic_source_mismatch_does_not_trigger_schema_retry -q
```

Expected: pass.

- [ ] **Step 5: Commit Task 5**

Run:

```bash
git add orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions.txt services/specs/compiler_service.py tests/test_spec_authority_compile_tool.py tests/test_specs_compiler_service.py
git commit -m "feat: add bounded schema feedback retry"
```

## Task 6: Unsupported Stored Artifact Read Paths Fail Closed

**Files:**
- Modify `services/specs/compiler_service.py`
- Modify `services/agent_workbench/authority_review.py`
- Modify `services/agent_workbench/authority_projection.py`
- Modify `services/specs/story_validation_service.py`
- Test `tests/test_specs_compiler_service.py`
- Test `tests/test_agent_workbench_authority_review.py`
- Test `tests/test_agent_workbench_authority_projection.py`
- Test `tests/test_agent_workbench_authority_decision_cli.py`

- [ ] **Step 1: Write unsupported-artifact reader tests**

Add tests that create a `CompiledSpecAuthority` with `legacy_compiled_authority_payload()` and assert each reader returns or raises the unsupported error:

```python
def test_authority_review_rejects_unsupported_compiled_authority_schema(
    authority_review_service: AuthorityReviewService,
    project_with_legacy_authority: int,
) -> None:
    result = authority_review_service.review(
        project_id=project_with_legacy_authority,
        include_spec="summary",
        output_format="json",
    )

    assert result["ok"] is False
    error = result["errors"][0]
    assert error["code"] == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
    assert "agileforge authority regenerate" in " ".join(error["remediation"])
```

```python
def test_authority_status_reports_regenerate_for_unsupported_schema(
    authority_projection_service: AuthorityProjectionService,
    project_with_legacy_authority: int,
) -> None:
    result = authority_projection_service.status(project_id=project_with_legacy_authority)

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
    assert result["data"]["authority_status"] == "unsupported_schema"
```

```python
def test_authority_invariants_reports_regenerate_for_unsupported_schema(
    authority_projection_service: AuthorityProjectionService,
    project_with_legacy_authority: int,
) -> None:
    result = authority_projection_service.invariants(project_id=project_with_legacy_authority)

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
```

- [ ] **Step 2: Verify reader tests fail**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_review.py::test_authority_review_rejects_unsupported_compiled_authority_schema tests/test_agent_workbench_authority_projection.py::test_authority_status_reports_regenerate_for_unsupported_schema tests/test_agent_workbench_authority_projection.py::test_authority_invariants_reports_regenerate_for_unsupported_schema -q
```

Expected: fail because unsupported stored artifacts still look invalid or fall back.

- [ ] **Step 3: Add shared remediation helper**

In `services/specs/compiler_service.py`, add:

```python
def compiled_authority_schema_unsupported_details(
    *,
    project_id: int,
    spec_version_id: int | None,
    observed_schema_version: str | None,
) -> dict[str, Any]:
    """Return standard unsupported compiled-authority remediation details."""
    return {
        "project_id": project_id,
        "spec_version_id": spec_version_id,
        "observed_schema_version": observed_schema_version,
        "required_schema_version": COMPILED_AUTHORITY_SCHEMA_VERSION,
    }


def compiled_authority_schema_unsupported_remediation(
    *,
    project_id: int,
    spec_version_id: int | None,
) -> list[str]:
    """Return standard regenerate remediation for unsupported authority artifacts."""
    if spec_version_id is None:
        return ["Find the approved spec version, then run agileforge authority regenerate."]
    return [
        (
            "Run agileforge authority regenerate "
            f"--project-id {project_id} "
            f"--spec-version-id {spec_version_id} "
            "--idempotency-key <new-key>."
        )
    ]
```

- [ ] **Step 4: Replace local fallback loaders**

In `authority_review.py`, replace local `_load_compiled_artifact` with the central loader from `compiler_service.py`. In `_compiled_artifact_shape_findings`, when `load_result.unsupported` is true, return a blocking finding:

```python
{
    "finding_id": "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED",
    "severity": "blocking",
    "code": "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED",
    "message": "Compiled authority artifact schema is unsupported.",
    "candidate_ids": [],
    "source_unit_ids": [],
    "override_allowed": False,
    "details": {
        "observed_schema_version": load_result.observed_schema_version,
        "required_schema_version": COMPILED_AUTHORITY_SCHEMA_VERSION,
    },
}
```

For review command envelopes, return `ok=false` with `workbench_error(ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED, ...)`; do not use `_fallback_authority_artifact` for unsupported schemas.

- [ ] **Step 5: Update projection and story-validation readers**

In `authority_projection.py`, if `load_compiled_artifact(...).unsupported` is true, return the standard unsupported error and include:

```python
"authority_status": "unsupported_schema",
"current": False,
"accepted_current": False,
```

In `story_validation_service.py`, convert unsupported loader results into `SpecAuthorityGateError` with this message:

```python
raise SpecAuthorityGateError(
    "Compiled authority artifact schema is unsupported. Run "
    f"agileforge authority regenerate --project-id {product_id} "
    f"--spec-version-id {spec_version_id} --idempotency-key <new-key>."
)
```

If the caller returns a workbench envelope instead of raising `SpecAuthorityGateError`, return `workbench_error(ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED, ...)` with the same remediation string.

- [ ] **Step 6: Verify Task 6 tests pass**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py tests/test_agent_workbench_authority_review.py tests/test_agent_workbench_authority_projection.py tests/test_agent_workbench_authority_decision_cli.py -q
```

Expected: pass.

- [ ] **Step 7: Commit Task 6**

Run:

```bash
git add services/specs/compiler_service.py services/agent_workbench/authority_review.py services/agent_workbench/authority_projection.py services/specs/story_validation_service.py tests/test_specs_compiler_service.py tests/test_agent_workbench_authority_review.py tests/test_agent_workbench_authority_projection.py tests/test_agent_workbench_authority_decision_cli.py
git commit -m "fix: fail closed on unsupported compiled authority artifacts"
```

## Task 7: Authority Regenerate Runner

**Files:**
- Create `services/agent_workbench/authority_regenerate.py`
- Modify `services/agent_workbench/application.py`
- Test `tests/test_agent_workbench_application.py`
- Create `tests/test_agent_workbench_authority_regenerate.py`

- [ ] **Step 1: Write regenerate runner tests**

Create `tests/test_agent_workbench_authority_regenerate.py` with these contract tests using existing workbench DB fixtures:

```python
def test_regenerate_requires_approved_spec_version(authority_regenerate_runner: AuthorityRegenerateRunner) -> None:
    result = authority_regenerate_runner.regenerate(
        AuthorityRegenerateRequest(
            project_id=1,
            spec_version_id=100,
            idempotency_key="regen-unapproved-001",
            changed_by="test",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] in {
        "SPEC_VERSION_NOT_FOUND",
        "AUTHORITY_REVIEW_REQUIRED",
    }


def test_regenerate_dry_run_validates_guards_without_mutation(
    authority_regenerate_runner: AuthorityRegenerateRunner,
    approved_spec_version_id: int,
) -> None:
    result = authority_regenerate_runner.regenerate(
        AuthorityRegenerateRequest(
            project_id=1,
            spec_version_id=approved_spec_version_id,
            dry_run=True,
            changed_by="test",
        )
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "dry_run"
    assert result["data"]["would_regenerate"] is True
    assert result["data"].get("mutation_event_id") is None


def test_regenerate_persists_pending_v2_authority_and_does_not_accept(
    authority_regenerate_runner: AuthorityRegenerateRunner,
    approved_spec_version_id: int,
) -> None:
    result = authority_regenerate_runner.regenerate(
        AuthorityRegenerateRequest(
            project_id=1,
            spec_version_id=approved_spec_version_id,
            idempotency_key="regen-approved-001",
            changed_by="test",
        )
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "authority_pending_review"
    assert result["data"]["compiled_authority_schema_version"] == (
        "agileforge.compiled_authority.v2"
    )
    assert result["data"]["accepted_authority_id"] is None
    assert result["data"]["next_actions"][0]["command"] == "agileforge authority review"


def test_regenerate_idempotency_replays_completed_mutation(
    authority_regenerate_runner: AuthorityRegenerateRunner,
    approved_spec_version_id: int,
) -> None:
    request = AuthorityRegenerateRequest(
        project_id=1,
        spec_version_id=approved_spec_version_id,
        idempotency_key="regen-replay-001",
        changed_by="test",
    )

    first = authority_regenerate_runner.regenerate(request)
    second = authority_regenerate_runner.regenerate(request)

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["data"]["mutation_event_id"] == first["data"]["mutation_event_id"]
```

Add these local fixtures in `tests/test_agent_workbench_authority_regenerate.py` when no shared fixture already exists:

```python
@pytest.fixture
def approved_spec_version_id(session: Session, product_id: int) -> int:
    spec = SpecRegistry(
        product_id=product_id,
        spec_content='{"format":"agileforge.spec.v1","items":[]}',
        content_hash="sha256:approved",
        status="approved",
        approved_at=datetime.now(UTC),
        approved_by="test",
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    assert spec.spec_version_id is not None
    return spec.spec_version_id


@pytest.fixture
def authority_regenerate_runner(engine: Engine) -> AuthorityRegenerateRunner:
    return AuthorityRegenerateRunner(engine=engine)
```

Use the repository fixture names already present in the test module for `engine`, `session`, and `product_id`. If this test module creates its own SQLite engine, name the fixtures exactly `engine`, `session`, and `product_id` and keep them local to the file.

- [ ] **Step 2: Verify regenerate tests fail**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_regenerate.py -q
```

Expected: fail because runner module does not exist.

- [ ] **Step 3: Implement request model and runner**

Create `services/agent_workbench/authority_regenerate.py`:

```python
"""Regenerate compiled authority for an approved spec version."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from typing import Any
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import update
from sqlmodel import Session

from models.db import get_engine
from models.agent_workbench import CliMutationLedger
from models.specs import SpecRegistry
from services.agent_workbench.envelope import error_envelope, success_envelope
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash
from services.agent_workbench.mutation_ledger import (
    IDEMPOTENCY_KEY_REUSED,
    MUTATION_IN_PROGRESS,
    MUTATION_RECOVERY_REQUIRED,
    MutationLedgerRepository,
    MutationStatus,
)
from services.specs.compiler_service import (
    COMPILED_AUTHORITY_SCHEMA_VERSION,
    compile_spec_authority_for_version,
)

AUTHORITY_REGENERATE_COMMAND: str = "agileforge authority regenerate"


class AuthorityRegenerateRequest(BaseModel):
    """CLI request for authority regeneration."""

    project_id: int
    spec_version_id: int
    idempotency_key: str | None = None
    changed_by: str = "cli-agent"
    dry_run: bool = False


@dataclass
class AuthorityRegenerateRunner:
    """Mutation runner for approved-spec authority regeneration."""

    engine: Any

    def regenerate(self, request: AuthorityRegenerateRequest) -> dict[str, Any]:
        """Regenerate authority and stop at pending review."""
        with Session(self.engine) as session:
            spec_version = session.get(SpecRegistry, request.spec_version_id)
            if spec_version is None or spec_version.product_id != request.project_id:
                return error_envelope(
                    command=AUTHORITY_REGENERATE_COMMAND,
                    error=workbench_error(
                        ErrorCode.SPEC_VERSION_NOT_FOUND,
                        message=(
                            f"Spec version {request.spec_version_id} was not found "
                            f"for project {request.project_id}."
                        ),
                        details={
                            "project_id": request.project_id,
                            "spec_version_id": request.spec_version_id,
                        },
                    ),
                )
            if spec_version.status != "approved":
                return error_envelope(
                    command=AUTHORITY_REGENERATE_COMMAND,
                    error=workbench_error(
                        ErrorCode.AUTHORITY_REVIEW_REQUIRED,
                        message="Authority can only be regenerated for an approved spec.",
                        details={
                            "project_id": request.project_id,
                            "spec_version_id": request.spec_version_id,
                            "spec_status": spec_version.status,
                        },
                        remediation=["Approve the spec before regenerating authority."],
                    ),
                )

        if request.dry_run:
            return success_envelope(
                command=AUTHORITY_REGENERATE_COMMAND,
                data={
                    "status": "dry_run",
                    "would_regenerate": True,
                    "project_id": request.project_id,
                    "spec_version_id": request.spec_version_id,
                    "compiled_authority_schema_version": COMPILED_AUTHORITY_SCHEMA_VERSION,
                },
            )

        if not request.idempotency_key:
            return error_envelope(
                command=AUTHORITY_REGENERATE_COMMAND,
                error=workbench_error(
                    ErrorCode.INVALID_COMMAND,
                    message="idempotency_key is required for authority regeneration.",
                    details={"project_id": request.project_id},
                ),
            )

        now = datetime.now(UTC)
        ledger = MutationLedgerRepository(engine=self.engine)
        request_hash = canonical_hash(
            {
                "command": AUTHORITY_REGENERATE_COMMAND,
                "project_id": request.project_id,
                "spec_version_id": request.spec_version_id,
            }
        )
        lease_owner = (
            f"agileforge-cli:authority-regenerate:{request.idempotency_key}:"
            f"{uuid4()}"
        )
        loaded = ledger.create_or_load(
            command=AUTHORITY_REGENERATE_COMMAND,
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            project_id=request.project_id,
            correlation_id=str(uuid4()),
            changed_by=request.changed_by,
            lease_owner=lease_owner,
            now=now,
            lease_seconds=300,
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code is not None:
            return _ledger_error_response(loaded.error_code, loaded.ledger.mutation_event_id)

        compile_result = compile_spec_authority_for_version(
            {"spec_version_id": request.spec_version_id, "force_recompile": True}
        )
        if not compile_result.get("ok"):
            response = error_envelope(
                command=AUTHORITY_REGENERATE_COMMAND,
                error=workbench_error(
                    ErrorCode.SPEC_COMPILE_FAILED,
                    message="Authority regeneration failed during compilation.",
                    details={
                        "project_id": request.project_id,
                        "spec_version_id": request.spec_version_id,
                        "compile_result": compile_result,
                    },
                    remediation=["Fix the compile failure, then rerun regenerate."],
                ),
            )
            _finalize_mutation_status(
                engine=self.engine,
                mutation_event_id=loaded.ledger.mutation_event_id,
                lease_owner=lease_owner,
                status=MutationStatus.VALIDATION_FAILED,
                response=response,
            )
            return response

        response = success_envelope(
            command=AUTHORITY_REGENERATE_COMMAND,
            data={
                "status": "authority_pending_review",
                "project_id": request.project_id,
                "spec_version_id": request.spec_version_id,
                "mutation_event_id": loaded.ledger.mutation_event_id,
                "compiled_authority_schema_version": COMPILED_AUTHORITY_SCHEMA_VERSION,
                "pending_authority_id": compile_result.get("authority_id"),
                "accepted_authority_id": None,
                "next_actions": [
                    {
                        "command": "agileforge authority review",
                        "args": {"project_id": request.project_id, "open": True},
                        "reason": "Review regenerated compiled authority before acceptance.",
                    }
                ],
            },
        )
        finalized = ledger.finalize_success(
            mutation_event_id=loaded.ledger.mutation_event_id,
            lease_owner=lease_owner,
            after={
                "project_id": request.project_id,
                "spec_version_id": request.spec_version_id,
                "compiled_authority_schema_version": COMPILED_AUTHORITY_SCHEMA_VERSION,
            },
            response=response,
            now=datetime.now(UTC),
        )
        if not finalized:
            return _ledger_error_response(
                "MUTATION_RESUME_CONFLICT",
                loaded.ledger.mutation_event_id,
            )
        return response


def _ledger_error_response(error_code: str, mutation_event_id: int | None) -> dict[str, Any]:
    code = {
        IDEMPOTENCY_KEY_REUSED: ErrorCode.IDEMPOTENCY_KEY_REUSED,
        MUTATION_IN_PROGRESS: ErrorCode.MUTATION_IN_PROGRESS,
        MUTATION_RECOVERY_REQUIRED: ErrorCode.MUTATION_RECOVERY_REQUIRED,
    }.get(error_code, ErrorCode.MUTATION_FAILED)
    return error_envelope(
        command=AUTHORITY_REGENERATE_COMMAND,
        error=workbench_error(
            code,
            message="Authority regeneration mutation cannot start.",
            details={"mutation_event_id": mutation_event_id},
            remediation=["Inspect mutation state before retrying regenerate."],
        ),
    )


def _finalize_mutation_status(
    *,
    engine: Any,
    mutation_event_id: int | None,
    lease_owner: str,
    status: MutationStatus,
    response: dict[str, Any],
) -> None:
    if mutation_event_id is None:
        return
    now = datetime.now(UTC).replace(tzinfo=None)
    with Session(engine) as session:
        session.exec(
            update(CliMutationLedger)
            .where(CliMutationLedger.mutation_event_id == mutation_event_id)
            .where(CliMutationLedger.status == MutationStatus.PENDING.value)
            .where(CliMutationLedger.lease_owner == lease_owner)
            .where(CliMutationLedger.lease_expires_at > now)
            .values(
                status=status.value,
                response_json=json.dumps(
                    response,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ),
                lease_owner=None,
                lease_acquired_at=None,
                last_heartbeat_at=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        session.commit()


def default_authority_regenerate_runner() -> AuthorityRegenerateRunner:
    """Build the default authority regenerate runner."""
    return AuthorityRegenerateRunner(engine=get_engine())
```

Keep this runner on the existing `MutationLedgerRepository.create_or_load(...)` and `finalize_success(...)` APIs. Do not add new generic ledger APIs for this pass.

- [ ] **Step 4: Add application facade method**

In `services/agent_workbench/application.py`, add imports:

```python
from services.agent_workbench.authority_regenerate import (
    AuthorityRegenerateRequest,
    AuthorityRegenerateRunner,
    default_authority_regenerate_runner,
)
```

Add a protocol:

```python
class _AuthorityRegenerateRunner(Protocol):
    """Authority regenerate methods exposed through the facade."""

    def regenerate(self, request: AuthorityRegenerateRequest) -> dict[str, Any]:
        """Regenerate authority for an approved spec version."""
        ...
```

Add constructor dependency and method:

```python
def authority_regenerate(
    self,
    *,
    project_id: int,
    spec_version_id: int,
    idempotency_key: str | None = None,
    changed_by: str = "cli-agent",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Regenerate compiled authority through the workbench facade."""
    return self._authority_regenerate_runner.regenerate(
        AuthorityRegenerateRequest(
            project_id=project_id,
            spec_version_id=spec_version_id,
            idempotency_key=idempotency_key,
            changed_by=changed_by,
            dry_run=dry_run,
        )
    )
```

- [ ] **Step 5: Verify regenerate runner and application tests pass**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_regenerate.py tests/test_agent_workbench_application.py -q
```

Expected: pass.

- [ ] **Step 6: Commit Task 7**

Run:

```bash
git add services/agent_workbench/authority_regenerate.py services/agent_workbench/application.py tests/test_agent_workbench_authority_regenerate.py tests/test_agent_workbench_application.py
git commit -m "feat: add authority regenerate runner"
```

## Task 8: CLI And Command Registry For Authority Regenerate

**Files:**
- Modify `cli/main.py`
- Modify `services/agent_workbench/command_registry.py`
- Modify `docs/agent-cli-manual.md`
- Test `tests/test_agent_workbench_authority_decision_cli.py`
- Test `tests/test_agent_workbench_application.py`

- [ ] **Step 1: Write failing CLI parser test**

In `tests/test_agent_workbench_authority_decision_cli.py`, add:

```python
def test_authority_regenerate_cli_invokes_application(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeApplication:
        def authority_regenerate(self, **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {
                "ok": True,
                "data": {
                    "status": "authority_pending_review",
                    "project_id": kwargs["project_id"],
                    "spec_version_id": kwargs["spec_version_id"],
                },
                "errors": [],
                "warnings": [],
            }

    result = run_cli(
        [
            "authority",
            "regenerate",
            "--project-id",
            "1",
            "--spec-version-id",
            "2",
            "--idempotency-key",
            "regen-cli-001",
            "--changed-by",
            "tester",
        ],
        application=FakeApplication(),
    )

    assert result.exit_code == 0
    assert calls == [
        {
            "project_id": 1,
            "spec_version_id": 2,
            "idempotency_key": "regen-cli-001",
            "changed_by": "tester",
            "dry_run": False,
        }
    ]
```

- [ ] **Step 2: Write failing registry test**

In `tests/test_agent_workbench_application.py` or the existing registry test file, add:

```python
def test_authority_regenerate_is_registered_command() -> None:
    from services.agent_workbench.command_registry import command_metadata

    metadata = command_metadata("agileforge authority regenerate")

    assert metadata is not None
    assert metadata.mutates is True
    assert metadata.requires_idempotency_key is True
    assert metadata.input_required == ("project_id", "spec_version_id")
    assert "idempotency_key" in metadata.input_optional
    assert "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED" in metadata.errors
```

- [ ] **Step 3: Verify CLI and registry tests fail**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_decision_cli.py::test_authority_regenerate_cli_invokes_application tests/test_agent_workbench_application.py::test_authority_regenerate_is_registered_command -q
```

Expected: fail because command is not registered.

- [ ] **Step 4: Add CLI parser and handler**

In `cli/main.py`, under `authority_sub`, add:

```python
authority_regenerate = authority_sub.add_parser(
    "regenerate",
    help="Regenerate compiled authority for an approved spec version.",
)
authority_regenerate.add_argument("--project-id", type=int, required=True)
authority_regenerate.add_argument("--spec-version-id", type=int, required=True)
authority_regenerate.add_argument("--idempotency-key")
authority_regenerate.add_argument("--changed-by", default="cli-agent")
authority_regenerate.add_argument("--dry-run", action="store_true")
authority_regenerate.set_defaults(command_handler=_authority_regenerate)
```

Add handler:

```python
def _authority_regenerate(
    application: AgentWorkbenchApplication,
    args: argparse.Namespace,
) -> CommandResult:
    return "agileforge authority regenerate", application.authority_regenerate(
        project_id=args.project_id,
        spec_version_id=args.spec_version_id,
        idempotency_key=args.idempotency_key,
        changed_by=args.changed_by,
        dry_run=args.dry_run,
    )
```

- [ ] **Step 5: Register command metadata**

In `_PHASE_2C_COMMANDS` in `command_registry.py`, add:

```python
CommandMetadata(
    name="agileforge authority regenerate",
    mutates=True,
    phase="phase_2c",
    requires_idempotency_key=True,
    idempotency_policy={
        "non_dry_run": "required",
        "dry_run": "forbidden",
        "dry_run_trace_field": "none",
    },
    input_required=("project_id", "spec_version_id"),
    input_optional=("idempotency_key", "changed_by", "dry_run"),
    errors=(
        ErrorCode.SCHEMA_NOT_READY.value,
        ErrorCode.PROJECT_NOT_FOUND.value,
        ErrorCode.SPEC_VERSION_NOT_FOUND.value,
        ErrorCode.AUTHORITY_REVIEW_REQUIRED.value,
        ErrorCode.SPEC_COMPILE_FAILED.value,
        ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED.value,
        ErrorCode.MUTATION_FAILED.value,
        ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
        ErrorCode.MUTATION_IN_PROGRESS.value,
        ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
    ),
)
```

- [ ] **Step 6: Update CLI manual**

In `docs/agent-cli-manual.md`, add a short command section:

```markdown
### Regenerate Compiled Authority

Use `agileforge authority regenerate` only when an approved spec version needs a fresh compiled authority artifact, including after `COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED`.

```bash
agileforge authority regenerate \
  --project-id 1 \
  --spec-version-id 1 \
  --idempotency-key regenerate-authority-001
```

The command recompiles the approved spec, saves v2 compiled authority, and stops at pending authority review. It does not accept/reject authority and does not advance Vision, Backlog, Roadmap, Story, or Sprint.
```

- [ ] **Step 7: Verify CLI and registry tests pass**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_decision_cli.py::test_authority_regenerate_cli_invokes_application tests/test_agent_workbench_application.py::test_authority_regenerate_is_registered_command -q
```

Expected: pass.

- [ ] **Step 8: Commit Task 8**

Run:

```bash
git add cli/main.py services/agent_workbench/command_registry.py docs/agent-cli-manual.md tests/test_agent_workbench_authority_decision_cli.py tests/test_agent_workbench_application.py
git commit -m "feat: expose authority regenerate command"
```

## Task 9: API, Context, Setup, And Phase Gates

**Files:**
- Modify `api.py`
- Modify `services/orchestrator_context_service.py`
- Modify `services/setup_service.py`
- Modify `services/agent_workbench/read_projection.py`
- Modify phase gate files that read compiled authority through projections.
- Test `tests/test_api_dashboard.py`
- Test `tests/test_orchestrator_context_service.py`
- Test `tests/test_setup_service.py`
- Test `tests/test_phase_workflow_state.py`

- [ ] **Step 1: Write API/dashboard unsupported-artifact test**

In `tests/test_api_dashboard.py`, add:

```python
def test_authority_api_returns_409_for_unsupported_compiled_authority_schema(
    client: TestClient,
    project_with_legacy_authority: int,
) -> None:
    response = client.get(f"/api/projects/{project_with_legacy_authority}/authority")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
    assert "agileforge authority regenerate" in " ".join(detail["remediation"])
```

- [ ] **Step 2: Write context/setup failure-closed tests**

In `tests/test_orchestrator_context_service.py`, add:

```python
def test_context_pack_does_not_cache_unsupported_compiled_authority(
    context_service: OrchestratorContextService,
    project_with_legacy_authority: int,
) -> None:
    result = context_service.build_context(project_id=project_with_legacy_authority)

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
```

In `tests/test_setup_service.py`, add:

```python
def test_setup_projection_surfaces_unsupported_authority_regenerate_action(
    setup_service: SetupService,
    project_with_legacy_authority: int,
) -> None:
    result = setup_service.status(project_id=project_with_legacy_authority)

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
    assert any(
        action["command"] == "agileforge authority regenerate"
        for action in result["data"]["next_actions"]
    )
```

- [ ] **Step 3: Verify tests fail**

Run:

```bash
uv run --frozen pytest tests/test_api_dashboard.py::test_authority_api_returns_409_for_unsupported_compiled_authority_schema tests/test_orchestrator_context_service.py::test_context_pack_does_not_cache_unsupported_compiled_authority tests/test_setup_service.py::test_setup_projection_surfaces_unsupported_authority_regenerate_action -q
```

Expected: fail because read paths still treat unsupported artifacts as generic invalid/missing.

- [ ] **Step 4: Add API error helper**

In `api.py`, add:

```python
def _raise_compiled_authority_schema_unsupported(
    *,
    project_id: int,
    spec_version_id: int | None,
    observed_schema_version: str | None,
) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "code": "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED",
            "message": "Compiled authority artifact schema is unsupported.",
            "details": {
                "project_id": project_id,
                "spec_version_id": spec_version_id,
                "observed_schema_version": observed_schema_version,
                "required_schema_version": "agileforge.compiled_authority.v2",
            },
            "remediation": [
                (
                    "Run agileforge authority regenerate "
                    f"--project-id {project_id} "
                    f"--spec-version-id {spec_version_id} "
                    "--idempotency-key <new-key>."
                )
            ],
        },
    )
```

Use this helper after `load_compiled_artifact` calls when `result.unsupported` is true.

- [ ] **Step 5: Update context/setup/read projections**

In `orchestrator_context_service.py`, `setup_service.py`, and `read_projection.py`, use `load_compiled_artifact` and branch on `load_result.unsupported`. Return the standard unsupported error code and a next action:

```python
{
    "command": "agileforge authority regenerate",
    "args": {
        "project_id": project_id,
        "spec_version_id": spec_version_id,
        "idempotency_key": "<new-key>",
    },
    "reason": "Regenerate unsupported compiled authority artifact before continuing.",
}
```

Do not backfill `product.compiled_authority_json` from unsupported artifacts.

- [ ] **Step 6: Update phase gates**

Search for compiled-authority reads in the exact files below:

```bash
rg -n "compiled_authority|load_compiled_artifact|compiled_artifact_json" \
  api.py \
  services/agent_workbench/backlog_phase.py \
  services/agent_workbench/roadmap_phase.py \
  services/agent_workbench/story_phase.py \
  services/agent_workbench/sprint_phase.py \
  services/agent_workbench/context_pack.py \
  services/agent_workbench/read_projection.py \
  services/specs/story_validation_service.py \
  services/orchestrator_context_service.py \
  services/setup_service.py
```

For every match that reads a stored artifact, branch on `load_result.unsupported` and return the standard error. Vision, Backlog, Roadmap, Story, Sprint, As-Built, and evidence collection must not start when only a v1 compiled authority artifact exists.

- [ ] **Step 7: Verify API/context/setup/phase tests pass**

Run:

```bash
uv run --frozen pytest tests/test_api_dashboard.py tests/test_orchestrator_context_service.py tests/test_setup_service.py tests/test_phase_workflow_state.py -q
```

Expected: pass.

- [ ] **Step 8: Commit Task 9**

Run:

```bash
git add api.py services/orchestrator_context_service.py services/setup_service.py services/agent_workbench/read_projection.py tests/test_api_dashboard.py tests/test_orchestrator_context_service.py tests/test_setup_service.py tests/test_phase_workflow_state.py
git commit -m "fix: surface unsupported authority artifacts in read paths"
```

## Task 10: As-Built And Evidence Provenance Readers

**Files:**
- Modify `services/agent_workbench/as_built_assessment.py`
- Modify `services/agent_workbench/evidence_collect.py`
- Test `tests/test_as_built_assessment.py`
- Test `tests/test_evidence_collect.py`

- [ ] **Step 1: Write failing v2 provenance tests for as-built**

In `tests/test_as_built_assessment.py`, update `CARTOLA_AUTHORITY` fixture or add a v2 fixture:

```python
V2_CARTOLA_AUTHORITY = {
    "schema_version": "agileforge.compiled_authority.v2",
    "invariants": [
        {
            "id": "INV-1111111111111111",
            "type": "USER_INTERACTION",
            "source_item_id": "REQ.live-squad-recommendation",
            "source_level": "MUST",
            "parameters": {
                "trigger": "manager opens squad recommendations",
                "target": "recommendation panel",
                "expected_response": "show eligible squad recommendations",
            },
        }
    ],
    "source_map": [
        {
            "invariant_id": "INV-1111111111111111",
            "excerpt": "MUST recommend eligible squads.",
            "location": "REQ.live-squad-recommendation",
        }
    ],
    "compiler_version": "2.0.0",
    "prompt_hash": "a" * 64,
    "scope_themes": ["squad"],
    "domain": "sports",
    "eligible_feature_rules": [],
    "rejected_features": [],
    "gaps": [],
    "assumptions": [],
}
```

Add:

```python
def test_as_built_targets_use_top_level_invariant_source_item_id() -> None:
    targets = as_built_assessment_module.targets_from_compiled_authority(
        V2_CARTOLA_AUTHORITY
    )

    assert targets[0].authority_ref == "REQ.live-squad-recommendation"
```

- [ ] **Step 2: Write failing v2 provenance tests for evidence collection**

In `tests/test_evidence_collect.py`, add:

```python
def test_evidence_targets_use_top_level_source_item_and_source_map_location() -> None:
    targets, warnings = evidence_collect_module.targets_from_compiled_authority(
        v2_compiled_authority_payload()
    )

    assert warnings == []
    assert targets[0].authority_ref == "REQ.intake-form"
    assert targets[0].invariant_id == "INV-1111111111111111"
```

- [ ] **Step 3: Verify tests fail**

Run:

```bash
uv run --frozen pytest tests/test_as_built_assessment.py::test_as_built_targets_use_top_level_invariant_source_item_id tests/test_evidence_collect.py::test_evidence_targets_use_top_level_source_item_and_source_map_location -q
```

Expected: fail because readers still inspect `parameters.source_item_id`.

- [ ] **Step 4: Update target extraction**

In `as_built_assessment.py` and `evidence_collect.py`, replace raw reads:

```python
source_item_id = invariant.get("parameters", {}).get("source_item_id")
```

with:

```python
source_item_id = invariant.get("source_item_id")
if not isinstance(source_item_id, str) or not source_item_id.strip():
    source_item_id = _source_item_id_from_source_map(
        invariant_id=str(invariant.get("id") or ""),
        source_map=compiled_authority.get("source_map") or [],
    )
```

Add helper in each module or a shared local helper if both files already share utilities:

```python
def _source_item_id_from_source_map(
    *,
    invariant_id: str,
    source_map: object,
) -> str | None:
    if not isinstance(source_map, list):
        return None
    for entry in source_map:
        if not isinstance(entry, dict):
            continue
        if entry.get("invariant_id") != invariant_id:
            continue
        location = entry.get("location")
        if isinstance(location, str) and location.strip():
            return location.strip()
    return None
```

Do not read `source_map[].source_item_id`.

- [ ] **Step 5: Verify as-built/evidence tests pass**

Run:

```bash
uv run --frozen pytest tests/test_as_built_assessment.py tests/test_evidence_collect.py -q
```

Expected: pass.

- [ ] **Step 6: Commit Task 10**

Run:

```bash
git add services/agent_workbench/as_built_assessment.py services/agent_workbench/evidence_collect.py tests/test_as_built_assessment.py tests/test_evidence_collect.py
git commit -m "fix: read v2 authority provenance in assessment flows"
```

## Task 11: Project Create Regression With Saved Failure Artifacts

**Files:**
- Modify `tests/test_spec_authority_compiler_normalizer.py`
- Modify `tests/test_specs_compiler_service.py`
- Use saved artifact paths if present under `logs/failures/spec_authority/`

- [ ] **Step 1: Add invalid-ID regression**

In `tests/test_spec_authority_compiler_normalizer.py`, add a regression that loads or reconstructs a compiler payload with `id: "INV-xxxxxxxxxxxxxxxx"` and verifies deterministic IDs are repaired before strict validation:

```python
def test_saved_failure_placeholder_invariant_ids_repair_to_v2() -> None:
    payload = v2_compiled_authority_payload()
    for invariant in payload["invariants"]:
        invariant["id"] = "INV-xxxxxxxxxxxxxxxx"
    for entry in payload["source_map"]:
        entry["invariant_id"] = "INV-xxxxxxxxxxxxxxxx"

    result = normalize_compiler_output(json.dumps(payload))

    assert re.fullmatch(r"INV-[0-9a-f]{16}", result.invariants[0].id)
    assert result.source_map[0].invariant_id == result.invariants[0].id
```

- [ ] **Step 2: Add invalid-prompt-hash regression**

Add:

```python
def test_saved_failure_invalid_prompt_hash_repairs_to_compiler_prompt_hash() -> None:
    payload = v2_compiled_authority_payload()
    payload["prompt_hash"] = "not-a-hash"

    result = normalize_compiler_output(json.dumps(payload))

    assert re.fullmatch(r"[0-9a-f]{64}", result.prompt_hash)
    assert result.prompt_hash == compute_prompt_hash(SPEC_AUTHORITY_COMPILER_INSTRUCTIONS)
```

- [ ] **Step 3: Add misplaced-source-item regression**

Add:

```python
def test_saved_failure_param_level_source_item_repairs_to_top_level() -> None:
    payload = v2_compiled_authority_payload()
    invariant = payload["invariants"][0]
    parameters = invariant["parameters"]
    parameters["source_item_id"] = invariant.pop("source_item_id")
    parameters["source_level"] = invariant.pop("source_level")

    result = normalize_compiler_output(json.dumps(payload))

    assert result.invariants[0].source_item_id == "REQ.intake-form"
    assert not hasattr(result.invariants[0].parameters, "source_item_id")
```

- [ ] **Step 4: Verify regressions pass**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py::test_saved_failure_placeholder_invariant_ids_repair_to_v2 tests/test_spec_authority_compiler_normalizer.py::test_saved_failure_invalid_prompt_hash_repairs_to_compiler_prompt_hash tests/test_spec_authority_compiler_normalizer.py::test_saved_failure_param_level_source_item_repairs_to_top_level -q
```

Expected: pass.

- [ ] **Step 5: Commit Task 11**

Run:

```bash
git add tests/test_spec_authority_compiler_normalizer.py
git commit -m "test: cover saved authority compiler failure shapes"
```

## Task 12: Full Verification And Branch Finish

**Files:**
- All files changed by Tasks 1-11.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_error_codes.py \
  tests/test_spec_authority.py \
  tests/test_spec_schema_modules.py \
  tests/test_spec_authority_compiler_normalizer.py \
  tests/test_spec_authority_compile_tool.py \
  tests/test_specs_compiler_service.py \
  tests/test_agent_workbench_authority_review.py \
  tests/test_agent_workbench_authority_projection.py \
  tests/test_agent_workbench_authority_decision_cli.py \
  tests/test_agent_workbench_authority_regenerate.py \
  tests/test_agent_workbench_application.py \
  tests/test_api_dashboard.py \
  tests/test_orchestrator_context_service.py \
  tests/test_setup_service.py \
  tests/test_phase_workflow_state.py \
  tests/test_as_built_assessment.py \
  tests/test_evidence_collect.py \
  -q
```

Expected: pass.

- [ ] **Step 2: Run static checks on changed Python files**

Build the changed Python file list and run Ruff only on Python:

```bash
changed_py="$(git diff --name-only HEAD~11..HEAD -- '*.py' '*.pyi')"
if [ -n "$changed_py" ]; then
  uv run --frozen ruff check $changed_py
fi
```

Expected: pass.

- [ ] **Step 3: Run formatting whitespace check**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 4: Inspect registry/manual references**

Run:

```bash
rg -n "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED|authority regenerate|agileforge.compiled_authority.v2|SPEC_AUTHORITY_COMPILER_VERSION" \
  services cli orchestrator_agent utils docs tests | sed -n '1,240p'
```

Expected: references exist in schema, compiler version, command registry, CLI, docs, loader tests, regenerate tests, and unsupported-artifact read-path tests.

- [ ] **Step 5: Stop before mutating runtime smoke**

Do not run real `agileforge project create` in this branch finish task. If the user wants to retry the ASA project, do it only after merge using installed `agileforge`, with JSON captured to temp files and summarized through `agileforge-cli-safety`.

- [ ] **Step 6: Request code review**

Use `superpowers:requesting-code-review`. Review scope:

- v2 schema contract
- raw version sniffing before strict validation
- no legacy stored-artifact migration
- regenerate command idempotency and dry-run
- unsupported-artifact behavior in later phase readers
- as-built/evidence source metadata migration

- [ ] **Step 7: Finish branch**

Use `superpowers:finishing-a-development-branch`. Present merge/keep/discard choices after tests and review pass.

## Self-Review Checklist

- Every design requirement maps to a task above.
- The plan chooses the three open implementation choices:
  - API/dashboard use structured `409 Conflict` unsupported-artifact response.
  - `authority regenerate --dry-run` ships in pass one.
  - stale accepted authority uses projection-level mismatch only.
- Stored artifacts are raw-sniffed before strict Pydantic validation.
- No stored v1 artifact migration or fallback is planned.
- Fresh-output repair is bounded to pre-validation compiler output.
- `source_map` remains evidence and uses real source text.
- `source_item_id` does not affect deterministic invariant IDs.
- Unsupported artifacts in Vision, Backlog, Roadmap, Story, Sprint, As-Built, and evidence collection point to `agileforge authority regenerate`.
- No task runs a real mutating `project create`.
