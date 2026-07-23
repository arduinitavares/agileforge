# Compiled Authority V3 Typed Assumptions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace free-form compiler claims with a strict compiled-authority v3 assumption union, ground structured claims against the canonical spec, preserve authority history during regeneration, and close issue #195 without adding legacy compatibility code.

**Architecture:** Assumption models, canonical identity, display rendering, and deterministic grounding live in one dependency-safe utility module. The compiler boundary emits and stores v3 only. The normalizer grounds fresh structured claims before persistence; authority review re-grounds stored claims before acceptance. Merge inputs carry explicit scope so partial aggregate claims cannot leak into full authority. Regeneration becomes append-only and curation may edit only `free_text.text`.

**Tech Stack:** Python 3.12, Pydantic v2, SQLModel, pytest, the existing AgileForge structured-spec profile, compiler normalizer, authority quality gate, mutation ledger, and CLI review workflow.

## Global Constraints

- Do not add a v2-to-v3 converter, `str | AuthorityAssumption`, legacy regex fallback, stored-loader repair, automatic batch regeneration, or automatic acceptance.
- Do not add a SQL data migration; v2 rows remain immutable JSON history and v3 regeneration inserts new rows.
- Keep historical v2 database rows and historical design documents unchanged. Current readers must return `COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED` for v2.
- Reject duplicate `item_ids` and `source_item_ids` before sorting. Never repair provenance membership.
- Use one `canonical_assumption_key()` for quality deduplication, merge deduplication, compact-IR assumption hashes, review finding identity, rendering lookup, and curation targeting.
- Treat the free-text cue check as the finite documented predicate, not general natural-language understanding.
- Ground structured claims from a parsed `TechnicalSpecArtifact`; do not ground from raw text or display prose.
- Every `force_recompile=True` compilation inserts a new row. No successful compilation updates an existing `CompiledSpecAuthority`.
- Once multiple rows can exist, downstream readers load an exact compiler/acceptance authority ID or the newest row by `authority_id DESC`; unordered spec-version lookup is invalid.
- Run the focused test named in each task before committing that task. Migrate that task's fixtures to v3 at the same time; Task 7 performs the residual repository-wide fixture sweep.
- Do not push, open a pull request, regenerate a real project, or close issue #195 during implementation without a separate user-approved delivery step.

## File Map

| Area | Files |
| --- | --- |
| Assumption contract | `utils/spec_authority_assumptions.py`, `utils/spec_schemas.py`, `utils/schemes.py` |
| Compact identity | `utils/spec_authority_ir.py`, `utils/spec_schemas.py` |
| Compiler contract | `orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions.txt`, `instructions_source.py`, `normalizer.py` |
| Compile, merge, load, persist | `services/specs/compiler_service.py`, `services/specs/authority_quality.py`, `services/specs/authority_selection.py` |
| Multi-row readers | `services/specs/story_validation_service.py`, `services/orchestrator_context_service.py`, `services/specs/pending_authority_service.py`, `tools/export_snapshot.py` |
| Review | `services/agent_workbench/authority_review.py` |
| Regeneration | `services/agent_workbench/authority_regenerate.py` |
| Curation | `services/agent_workbench/authority_curation.py`, `services/specs/authority_curation_diff.py`, `services/agent_workbench/error_codes.py` |
| Operator docs and fixture | `docs/agent-cli-manual.md`, `benchmarks/authority-quality/todomvc/agileforge/compiled-authority.json` |

---

### Task 1: Add the Typed Assumption Contract and Grounding Service

**Files:**

- Create: `utils/spec_authority_assumptions.py`
- Modify: `utils/schemes.py`
- Create: `tests/test_spec_authority_assumptions.py`
- Modify: `tests/test_spec_schema_modules.py`

**Interfaces:**

- Consumes: one raw assumption object and, for structured claims, one parsed `TechnicalSpecArtifact`.
- Produces: `AuthorityAssumption`, `GroundingFailure`, `canonical_assumption_key()`, `ground_assumption()`, `is_structured_assumption()`, and `render_assumption_text()`.
- Does not yet change `SpecAuthorityCompilationSuccess.assumptions`; this task is additive so its commit remains independently green.

- [ ] **Step 1: Write failing schema and free-text boundary tests.**

Add parametrized tests covering all four variants, missing or unknown kinds,
missing `provenance.source`, extra fields, strings, empty text, duplicate IDs,
sorted canonical output, discriminator behavior independent of union declaration
order, all four #195 paraphrases, the #177 sentence, Unicode NFKC/case changes,
and the two valid free-text controls.

```python
@pytest.mark.parametrize(
    "text",
    [
        "Only REQ.alpha was accepted.",
        "REQ.alpha was the only accepted item.",
        "One item was accepted: REQ.alpha.",
        "Accepted items: REQ.alpha.",
        "Only CONSTRAINT.beta was accepted.",
        "ＲＥＱ.alpha is discussed; DRAFT assumptions remain open.",
    ],
)
def test_free_text_rejects_reserved_claim_cues(text: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        FreeTextAssumption(kind="free_text", text=text)

    assert any(
        error["type"] == "assumption_claim_requires_typed_form"
        for error in exc_info.value.errors()
    )


@pytest.mark.parametrize(
    "text",
    [
        "REQ.alpha depends on an external identity provider.",
        "Draft audit evidence is stored with each decision.",
    ],
)
def test_free_text_keeps_non_claim_assumptions(text: str) -> None:
    assert FreeTextAssumption(kind="free_text", text=text).text == text
```

Run:

```bash
uv run pytest tests/test_spec_authority_assumptions.py -q
```

Expected: FAIL because the module and models do not exist.

- [ ] **Step 2: Implement strict models and the finite cue predicate.**

Define the contract in `utils/spec_authority_assumptions.py`. Use `PydanticCustomError` so the normalizer can distinguish claim-like free text from generic JSON validation later.

```python
class _StrictAssumptionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


StructuredSpecItemId = Annotated[
    str,
    Field(pattern=STRUCTURED_ITEM_ID_PATTERN),
]
NormativeSpecItemId = Annotated[
    str,
    Field(pattern=NORMATIVE_ITEM_ID_PATTERN),
]


class StructuredSpecClaimProvenance(_StrictAssumptionModel):
    source: Literal["structured_spec"]
    artifact_id: Annotated[
        str,
        Field(pattern=r"^SPEC\.[a-z0-9][a-z0-9.-]{1,96}$"),
    ]
    source_item_ids: list[StructuredSpecItemId]

    @field_validator("source_item_ids")
    @classmethod
    def validate_source_item_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("source_item_ids must be unique")
        return sorted(value)


class FreeTextAssumption(_StrictAssumptionModel):
    kind: Literal["free_text"]
    text: Annotated[str, Field(min_length=1)]

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("text must not be empty")
        if free_text_requires_typed_claim(text):
            raise PydanticCustomError(
                "assumption_claim_requires_typed_form",
                "claim-like assumption must use a typed claim variant",
            )
        return text


class ItemStatusAssumptionClaim(_StrictAssumptionModel):
    kind: Literal["item_status"]
    item_id: StructuredSpecItemId
    status: AgileForgeSpecStatus
    provenance: StructuredSpecClaimProvenance


class AcceptedNormativeCountAssumptionClaim(_StrictAssumptionModel):
    kind: Literal["accepted_normative_count"]
    count: Annotated[int, Field(strict=True, ge=0)]
    provenance: StructuredSpecClaimProvenance


class AcceptedNormativeSetAssumptionClaim(_StrictAssumptionModel):
    kind: Literal["accepted_normative_set"]
    item_ids: list[NormativeSpecItemId]
    provenance: StructuredSpecClaimProvenance

    @field_validator("item_ids")
    @classmethod
    def validate_item_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("item_ids must be unique")
        return sorted(value)


AuthorityAssumption = Annotated[
    FreeTextAssumption
    | ItemStatusAssumptionClaim
    | AcceptedNormativeCountAssumptionClaim
    | AcceptedNormativeSetAssumptionClaim,
    Field(discriminator="kind"),
]
AUTHORITY_ASSUMPTION_ADAPTER = TypeAdapter(AuthorityAssumption)
```

Implement the exact predicate after `unicodedata.normalize("NFKC", text).casefold()`: invalid when the normalized text contains both a canonical structured item ID and any `AgileForgeSpecStatus` word, or contains the word `accepted` and the word `item` or `items`. Use Unicode-aware word-boundary regexes. Do not add synonyms such as `approved`, `sole`, or `alone`.

- [ ] **Step 3: Write failing canonical identity, rendering, and grounding tests.**

Cover:

- normalized free-text identity;
- separator-stable sorted JSON identity for each structured variant;
- distinct keys for distinct kinds or values;
- readable rendering for all variants;
- true and false item-status claims;
- true and false accepted-count claims;
- true and false accepted-set claims;
- missing items, wrong artifact ID, incomplete provenance, and invented provenance.

```python
def test_grounding_rejects_false_accepted_set(
    structured_spec: TechnicalSpecArtifact,
) -> None:
    claim = AcceptedNormativeSetAssumptionClaim(
        kind="accepted_normative_set",
        item_ids=["REQ.alpha"],
        provenance=StructuredSpecClaimProvenance(
            source="structured_spec",
            artifact_id=structured_spec.artifact_id,
            source_item_ids=["REQ.alpha"],
        ),
    )

    result = ground_assumption(claim, structured_spec)

    assert isinstance(result, GroundingFailure)
    assert result.reason == "ASSUMPTION_CLAIM_MISMATCH"
    assert result.claimed_value == ["REQ.alpha"]
    assert result.actual_value == ["CONSTRAINT.beta", "REQ.alpha"]
```

Run:

```bash
uv run pytest tests/test_spec_authority_assumptions.py -q
```

Expected: FAIL because canonical identity, rendering, and grounding are absent.

- [ ] **Step 4: Implement shared identity, rendering, and grounding.**

Use a frozen dataclass for failures and one exhaustive function for grounding:

```python
GroundingFailureReason = Literal[
    "ASSUMPTION_CLAIM_SOURCE_MISMATCH",
    "ASSUMPTION_CLAIM_MISMATCH",
]


@dataclass(frozen=True)
class GroundingFailure:
    reason: GroundingFailureReason
    claim_kind: str
    claimed_value: object
    actual_value: object
    artifact_id: str
    claimed_source_item_ids: tuple[str, ...]
    actual_source_item_ids: tuple[str, ...]


def canonical_assumption_key(assumption: AuthorityAssumption) -> str:
    payload = assumption.model_dump(mode="json")
    if isinstance(assumption, FreeTextAssumption):
        payload["text"] = normalize_free_text_identity(assumption.text)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def render_assumption_text(assumption: AuthorityAssumption) -> str:
    if isinstance(assumption, FreeTextAssumption):
        return assumption.text
    if isinstance(assumption, ItemStatusAssumptionClaim):
        return f"{assumption.item_id} status is {assumption.status.value}"
    if isinstance(assumption, AcceptedNormativeCountAssumptionClaim):
        return f"{assumption.count} accepted normative items"
    return "accepted normative items: " + ", ".join(assumption.item_ids)


def ground_assumption(
    assumption: AuthorityAssumption,
    artifact: TechnicalSpecArtifact,
) -> AuthorityAssumption | GroundingFailure:
    if isinstance(assumption, FreeTextAssumption):
        return assumption

    items_by_id = {item.id: item for item in artifact.items}
    accepted_ids = sorted(
        item.id
        for item in artifact.items
        if item.type in NORMATIVE_SPEC_TYPES
        and item.status == AgileForgeSpecStatus.ACCEPTED
    )
    provenance = assumption.provenance
    claimed_sources = tuple(provenance.source_item_ids)

    if provenance.artifact_id != artifact.artifact_id:
        return GroundingFailure(
            reason="ASSUMPTION_CLAIM_SOURCE_MISMATCH",
            claim_kind=assumption.kind,
            claimed_value=assumption.model_dump(mode="json"),
            actual_value={"artifact_id": artifact.artifact_id},
            artifact_id=provenance.artifact_id,
            claimed_source_item_ids=claimed_sources,
            actual_source_item_ids=(),
        )

    if isinstance(assumption, ItemStatusAssumptionClaim):
        item = items_by_id.get(assumption.item_id)
        actual_sources = (assumption.item_id,) if item is not None else ()
        if (
            item is None
            or item.status != assumption.status
            or claimed_sources != actual_sources
        ):
            return GroundingFailure(
                reason=(
                    "ASSUMPTION_CLAIM_MISMATCH"
                    if item is not None and item.status != assumption.status
                    else "ASSUMPTION_CLAIM_SOURCE_MISMATCH"
                ),
                claim_kind=assumption.kind,
                claimed_value=assumption.status.value,
                actual_value=item.status.value if item is not None else None,
                artifact_id=artifact.artifact_id,
                claimed_source_item_ids=claimed_sources,
                actual_source_item_ids=actual_sources,
            )
        return assumption

    actual_sources = tuple(accepted_ids)
    claimed_value: object
    actual_value: object
    if isinstance(assumption, AcceptedNormativeCountAssumptionClaim):
        claimed_value = assumption.count
        actual_value = len(accepted_ids)
    else:
        claimed_value = assumption.item_ids
        actual_value = accepted_ids

    if claimed_sources != actual_sources:
        reason: GroundingFailureReason = "ASSUMPTION_CLAIM_SOURCE_MISMATCH"
    elif claimed_value != actual_value:
        reason = "ASSUMPTION_CLAIM_MISMATCH"
    else:
        return assumption
    return GroundingFailure(
        reason=reason,
        claim_kind=assumption.kind,
        claimed_value=claimed_value,
        actual_value=actual_value,
        artifact_id=artifact.artifact_id,
        claimed_source_item_ids=claimed_sources,
        actual_source_item_ids=actual_sources,
    )
```

Re-export the public assumption types through `utils/schemes.py` only if existing compatibility consumers require that boundary. New production code imports the dedicated module directly.

- [ ] **Step 5: Run the additive contract suite and commit.**

```bash
uv run pytest tests/test_spec_authority_assumptions.py tests/test_spec_schema_modules.py -q
git diff --check
git add utils/spec_authority_assumptions.py utils/schemes.py tests/test_spec_authority_assumptions.py tests/test_spec_schema_modules.py
git commit -m "feat: add typed authority assumption contract"
```

---

### Task 2: Cut the Compiler Boundary to V3

**Files:**

- Modify: `utils/spec_schemas.py`
- Modify: `utils/spec_authority_ir.py`
- Modify: `orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions.txt`
- Modify: `orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions_source.py`
- Modify: `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`
- Modify: `services/specs/compiler_service.py`
- Modify: `services/specs/authority_quality.py`
- Modify: `services/agent_workbench/authority_review.py`
- Test: `tests/test_spec_schema_modules.py`
- Test: `tests/test_spec_authority_ir.py`
- Test: `tests/test_spec_authority_compiler_agent.py`
- Test: `tests/test_spec_authority_compiler_normalizer.py`
- Test: `tests/test_specs_compiler_service.py`
- Test: `tests/test_authority_quality_gate.py`

**Interfaces:**

- Consumes: v3 compiler JSON with object assumptions and optional canonical structured source.
- Produces: a fully revalidated `SpecAuthorityCompilationSuccess` or a stable compiler failure.
- Stored loader accepts v3 only; v2 remains an unsupported-version input.

- [ ] **Step 1: Write failing v3 schema, loader, normalizer, and retry tests.**

Add tests proving:

- success and failure schema versions default to `agileforge.compiled_authority.v3`;
- compiler version is `3.0.0`;
- string assumptions and unknown `kind` values fail;
- each typed variant round-trips through stored loading;
- stored v2 returns `COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED`;
- claim-like free text returns `ASSUMPTION_CLAIM_REQUIRES_TYPED_FORM` and triggers exactly one contract retry;
- missing or invalid structured source fails without retry;
- true structured claims normalize;
- false claims do not persist;
- no-invariants output still grounds claims before returning;
- all three host-generated assumption paths append `FreeTextAssumption`;
- every success return passes final full-model revalidation.

Use complete v3 objects in current-path fixtures:

```python
"assumptions": [
    {
        "kind": "accepted_normative_set",
        "item_ids": ["CONSTRAINT.beta", "REQ.alpha"],
        "provenance": {
            "source": "structured_spec",
            "artifact_id": "SPEC.normalizer",
            "source_item_ids": ["CONSTRAINT.beta", "REQ.alpha"],
        },
    }
]
```

Keep one explicitly named historical v2 fixture only for the unsupported-loader assertion.

Run:

```bash
uv run pytest \
  tests/test_spec_schema_modules.py \
  tests/test_spec_authority_compiler_agent.py \
  tests/test_spec_authority_compiler_normalizer.py \
  tests/test_specs_compiler_service.py \
  -q
```

Expected: FAIL on v2 constants, string assumptions, missing grounding, and retry routing.

- [ ] **Step 2: Change the Pydantic compiler contract and compact assumption identity.**

In `utils/spec_schemas.py`:

```python
class SpecAuthorityCompilationSuccess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agileforge.compiled_authority.v3"] = (
        "agileforge.compiled_authority.v3"
    )
    assumptions: Annotated[
        list[AuthorityAssumption],
        Field(description="Explicit typed assumptions made during compilation."),
    ]


class SpecAuthorityCompilationFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agileforge.compiled_authority.v3"] = (
        "agileforge.compiled_authority.v3"
    )
```

Change both compact-IR assumption ID paths to hash the shared canonical key, not normalized display text:

```python
def _generated_compact_assumption_id(
    candidate_id: str,
    target_kind: AuthorityTargetKind,
    assumption: AuthorityAssumption,
) -> str:
    payload = {
        "assumption_key": canonical_assumption_key(assumption),
        "candidate_id": candidate_id,
        "target_kind": target_kind.value,
    }
    return f"ASM-{_compact_ir_canonical_hash(payload)}"
```

Apply the same payload rule in `utils/spec_authority_ir.py`. If compact-IR source input supplies only `target_text`, first construct `FreeTextAssumption(kind="free_text", text=target_text)` and then call `canonical_assumption_key()`; never hash presentation prose from a structured claim.

- [ ] **Step 3: Update the compiler instructions and version source.**

Set:

```python
SPEC_AUTHORITY_COMPILER_VERSION = "3.0.0"
```

The success schema in `instructions.txt` must show every object variant, state that strings are invalid, require structured provenance, and forbid claim-like text from the finite cue boundary. The failure object must also declare:

```json
{
  "schema_version": "agileforge.compiled_authority.v3",
  "error": "SPEC_COMPILATION_FAILED",
  "reason": "short reason",
  "blocking_gaps": []
}
```

- [ ] **Step 4: Make normalizer mutation, grounding, and failure mapping typed.**

Replace the three string constants with `FreeTextAssumption` instances and append them through one identity-aware helper:

```python
def _append_host_assumption(
    success: SpecAuthorityCompilationSuccess,
    assumption: FreeTextAssumption,
) -> None:
    existing = {
        canonical_assumption_key(item)
        for item in success.assumptions
    }
    if canonical_assumption_key(assumption) not in existing:
        success.assumptions.append(assumption)
```

Extract the dedicated Pydantic error before generic conversion:

```python
def _claim_like_assumption_failure(
    exc: ValidationError,
) -> SpecAuthorityCompilerOutput | None:
    for error in exc.errors(include_url=False):
        if error["type"] != "assumption_claim_requires_typed_form":
            continue
        location = ".".join(str(part) for part in error["loc"])
        return _failure(
            reason="ASSUMPTION_CLAIM_REQUIRES_TYPED_FORM",
            blocking_gaps=[
                f"{location}: use item_status, accepted_normative_count, "
                "or accepted_normative_set"
            ],
        )
    return None
```

After strict parsing and before the no-invariants branch:

```python
structured_claims = [
    assumption
    for assumption in success.assumptions
    if is_structured_assumption(assumption)
]
if structured_claims:
    if source_format != "agileforge.spec.v1" or not source_text:
        return _failure(
            reason="ASSUMPTION_CLAIM_SOURCE_UNAVAILABLE",
            blocking_gaps=["Structured assumption claims require canonical spec JSON."],
        )
    try:
        spec_artifact = TechnicalSpecArtifact.model_validate_json(source_text)
    except ValidationError as exc:
        return _failure(
            reason="ASSUMPTION_CLAIM_SOURCE_UNAVAILABLE",
            blocking_gaps=[_summarize_validation_error("structured source", exc)],
        )
    for index, assumption in enumerate(success.assumptions, start=1):
        grounded = ground_assumption(assumption, spec_artifact)
        if isinstance(grounded, GroundingFailure):
            return _grounding_failure_output(index=index, failure=grounded)
```

Route every success exit through:

```python
def _validated_success_output(
    success: SpecAuthorityCompilationSuccess,
) -> SpecAuthorityCompilerOutput:
    validated = SpecAuthorityCompilationSuccess.model_validate(
        success.model_dump(mode="json")
    )
    return SpecAuthorityCompilerOutput(root=validated)
```

Do not leave a direct `SpecAuthorityCompilerOutput(root=success)` success return in the normalizer.

- [ ] **Step 5: Switch the loader, retry contract, quality gate, and basic merge to typed values.**

In `services/specs/compiler_service.py`:

```python
COMPILED_AUTHORITY_SCHEMA_VERSION = "agileforge.compiled_authority.v3"
_SCHEMA_RETRY_FAILURES = frozenset(
    {
        "INVALID_JSON",
        "JSON_VALIDATION_FAILED",
        "ASSUMPTION_CLAIM_REQUIRES_TYPED_FORM",
    }
)
```

Update retry feedback to name v3 and require a typed variant for the dedicated failure. Keep semantic grounding failures outside the retry set.

Replace `_dedupe_strings()` for assumptions with:

```python
def _dedupe_assumptions(
    assumptions: Iterable[AuthorityAssumption],
) -> list[AuthorityAssumption]:
    seen: set[str] = set()
    result: list[AuthorityAssumption] = []
    for assumption in assumptions:
        key = canonical_assumption_key(assumption)
        if key in seen:
            continue
        seen.add(key)
        result.append(assumption)
    return result
```

In `authority_quality.py`, type the kept list as `list[AuthorityAssumption]`, deduplicate with `canonical_assumption_key()`, and build noisy groups from only:

```python
free_text_entries = [
    (index, assumption.text)
    for index, assumption in enumerate(assumptions, start=1)
    if isinstance(assumption, FreeTextAssumption)
]
```

Use `render_assumption_text()` wherever review currently passes a Pydantic object to a text field. Task 5 replaces the legacy semantic regex; this step only keeps the v3 object rendering path valid during the schema cut.

- [ ] **Step 6: Migrate the focused suites and run the v3 boundary tests.**

For Pydantic constructors:

```python
assumptions=[
    FreeTextAssumption(kind="free_text", text="Audit evidence is retained.")
]
```

For raw JSON:

```python
"assumptions": [
    {"kind": "free_text", "text": "Audit evidence is retained."}
]
```

Run:

```bash
uv run pytest \
  tests/test_spec_authority_assumptions.py \
  tests/test_spec_schema_modules.py \
  tests/test_spec_authority_ir.py \
  tests/test_spec_authority_compiler_agent.py \
  tests/test_spec_authority_compiler_normalizer.py \
  tests/test_specs_compiler_service.py \
  tests/test_authority_quality_gate.py \
  -q
git diff --check
git add \
  utils/spec_schemas.py \
  utils/spec_authority_ir.py \
  orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions.txt \
  orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions_source.py \
  orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py \
  services/specs/compiler_service.py \
  services/specs/authority_quality.py \
  services/agent_workbench/authority_review.py \
  tests/test_spec_schema_modules.py \
  tests/test_spec_authority_ir.py \
  tests/test_spec_authority_compiler_agent.py \
  tests/test_spec_authority_compiler_normalizer.py \
  tests/test_specs_compiler_service.py \
  tests/test_authority_quality_gate.py
git commit -m "feat: cut compiled authority contract to v3"
```

---

### Task 3: Make Compilation Merge Scope-Aware

**Files:**

- Modify: `utils/spec_schemas.py`
- Modify: `services/specs/compiler_service.py`
- Modify: `services/specs/authority_quality.py`
- Test: `tests/test_specs_compiler_service.py`
- Test: `tests/test_authority_quality_gate.py`

**Interfaces:**

- Consumes: `ScopedCompilationSuccess` values plus the final parsed full spec.
- Produces: one merged, deduplicated, re-grounded success or an exact semantic failure.
- Records accepted-base aggregate invalidation in authority quality metadata.

- [ ] **Step 1: Write failing scope and quality-record tests.**

Add cases for:

- full-spec count/set claims retained when true;
- focused, repair, and extension-only count/set claims fail with `ASSUMPTION_CLAIM_SCOPE_INVALID`;
- accepted-base aggregate claims are removed during scope extension;
- the removal records assumption index, kind, and reason `aggregate_claim_invalidated_by_scope_extension`;
- item-status claims from partial inputs remain and re-ground against the final full spec;
- different structured values do not merge;
- noisy grouping ignores every structured variant.

```python
def test_scope_extension_invalidates_base_aggregate_claim() -> None:
    merged = _merge_compilation_successes(
        [
            ScopedCompilationSuccess(
                scope=CompilationScope.ACCEPTED_BASE,
                success=_success_with_true_count_claim(count=1),
            ),
            ScopedCompilationSuccess(
                scope=CompilationScope.EXTENSION_ONLY,
                success=_success_with_item_status_claim("REQ.beta", "accepted"),
            ),
        ],
        final_spec=_spec_with_accepted_items("REQ.alpha", "REQ.beta"),
    )

    assert all(
        assumption.kind != "accepted_normative_count"
        for assumption in merged.assumptions
    )
    assert merged.authority_quality is not None
    assert merged.authority_quality.invalidated_items[0].reason == (
        "aggregate_claim_invalidated_by_scope_extension"
    )
```

Run:

```bash
uv run pytest \
  tests/test_specs_compiler_service.py \
  tests/test_authority_quality_gate.py \
  -q
```

Expected: FAIL because merge inputs have no scope and quality has no invalidation record.

- [ ] **Step 2: Add explicit scope and invalidation models.**

Use a closed enum and frozen wrapper:

```python
class CompilationScope(StrEnum):
    FULL_SPEC = "full_spec"
    FOCUSED_ITEM = "focused_item"
    REPAIR_ITEM = "repair_item"
    ACCEPTED_BASE = "accepted_base"
    EXTENSION_ONLY = "extension_only"


@dataclass(frozen=True)
class ScopedCompilationSuccess:
    scope: CompilationScope
    success: SpecAuthorityCompilationSuccess
```

Represent invalidation explicitly instead of overloading a merge record:

```python
class AuthorityQualityInvalidatedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invalidation_id: Annotated[str, Field(min_length=1)]
    item_kind: Literal["assumption"] = "assumption"
    removed_id: Annotated[str, Field(min_length=1)]
    assumption_kind: Literal[
        "accepted_normative_count",
        "accepted_normative_set",
    ]
    reason: Literal["aggregate_claim_invalidated_by_scope_extension"]


class AuthorityQualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agileforge.authority_quality.v1"] = (
        "agileforge.authority_quality.v1"
    )
    summary: AuthorityQualitySummary
    merged_items: list[AuthorityQualityMergedItem] = Field(default_factory=list)
    invalidated_items: list[AuthorityQualityInvalidatedItem] = Field(
        default_factory=list
    )
    review_groups: list[AuthorityQualityReviewGroup] = Field(default_factory=list)
```

Preserve and deterministically renumber invalidation records whenever `apply_authority_quality_gate()` rebuilds a report.

- [ ] **Step 3: Refactor every merge caller to declare scope.**

Change the merge signature:

```python
def _merge_compilation_successes(
    compilations: Sequence[ScopedCompilationSuccess],
    *,
    final_spec: TechnicalSpecArtifact | None,
) -> SpecAuthorityCompilationSuccess:
```

Before concatenation:

1. Reject aggregate claims from `FOCUSED_ITEM`, `REPAIR_ITEM`, or `EXTENSION_ONLY`.
2. Remove aggregate claims from `ACCEPTED_BASE` only when an `EXTENSION_ONLY` input is present, and add invalidation records.
3. Deduplicate with `canonical_assumption_key()`.
4. Re-ground every retained structured claim against `final_spec`.
5. Convert missing source, source mismatch, semantic mismatch, and scope mismatch into their exact compiler failure reason at the caller boundary.

Apply validation and re-grounding even when the input sequence contains one
success; remove the existing `len(successes) == 1` return as a semantic bypass.

Update all callers, including:

- iterative focused compilation;
- focused schema retry;
- focused coverage repair;
- metadata repair;
- single/full compilation;
- accepted-base plus extension-only scope-extension merge.

Never infer scope from list position.

- [ ] **Step 4: Run merge tests and commit.**

```bash
uv run pytest \
  tests/test_specs_compiler_service.py \
  tests/test_authority_quality_gate.py \
  tests/test_spec_authority_compiler_normalizer.py \
  -q
git diff --check
git add \
  utils/spec_schemas.py \
  services/specs/compiler_service.py \
  services/specs/authority_quality.py \
  tests/test_specs_compiler_service.py \
  tests/test_authority_quality_gate.py
git commit -m "feat: make authority assumption merges scope aware"
```

---

### Task 4: Make Recompilation and Regeneration Append-Only

**Files:**

- Create: `services/specs/authority_selection.py`
- Modify: `services/specs/compiler_service.py`
- Modify: `services/agent_workbench/authority_regenerate.py`
- Modify: `services/agent_workbench/authority_projection.py`
- Modify: `services/agent_workbench/read_projection.py`
- Modify: `services/agent_workbench/project_setup.py`
- Modify: `services/agent_workbench/as_built_assessment.py`
- Modify: `services/agent_workbench/evidence_collect.py`
- Modify: `services/specs/story_validation_service.py`
- Modify: `services/orchestrator_context_service.py`
- Modify: `services/specs/pending_authority_service.py`
- Modify: `tools/export_snapshot.py`
- Create: `tests/test_authority_selection.py`
- Test: `tests/test_specs_compiler_service.py`
- Test: `tests/test_agent_workbench_authority_regenerate.py`
- Test: `tests/test_agent_workbench_authority_projection.py`
- Test: `tests/test_agent_workbench_read_projection.py`
- Test: `tests/test_agent_workbench_project_setup.py`
- Test: `tests/test_as_built_assessment.py`
- Test: `tests/test_evidence_collect.py`
- Test: `tests/test_story_validation_service.py`
- Test: `tests/test_orchestrator_context_service.py`
- Test: `tests/test_pending_authority_service.py`
- Test: `tests/test_export_snapshot.py`

**Interfaces:**

- Consumes: a successful compilation and `force_recompile` intent.
- Produces: one newly inserted authority ID and a pending-review regeneration response bound to exactly that ID.
- Preserves: every existing authority row and terminal acceptance relation.
- Selection policy: exact compiler/mutation IDs first; terminal decisions load their
  exact `pending_authority_id`; execution validation uses the accepted decision;
  pending/current review lookup without an exact ID uses `authority_id DESC`.
  An unordered lookup by `spec_version_id` is forbidden.

- [ ] **Step 1: Write failing history, selection, and idempotency tests.**

Add cases proving:

- forced recompilation inserts a second row even when the first has no terminal decision;
- an accepted v2 row and its `SpecAuthorityAcceptance` remain byte-for-byte unchanged;
- regeneration returns the authority ID produced by compilation, even if a newer unrelated row exists;
- regeneration does not clone after compilation;
- replaying the same completed mutation returns the same authority ID and row count;
- product cache points at the new v3 candidate;
- story validation remains pinned to an accepted v2 row while a newer v3 row is only pending, so the unsupported block is not bypassed;
- story validation moves to v3 only after a terminal acceptance references that exact v3 row;
- orchestrator context fallback backfills the newest v3 artifact, never the older v2 row;
- pending compilation returns the compiler-provided authority ID without replacing it through a spec-version query;
- snapshot export uses the exact acceptance-bound row when an acceptance exists;
- snapshot export without an acceptance uses the newest row deterministically.
- as-built assessment and evidence collection fail closed when an accepted
  decision has no exact authority row; they never fall forward to a pending row.

```python
def test_force_recompile_inserts_without_mutating_existing_row(
    session: Session,
    existing_authority: CompiledSpecAuthority,
) -> None:
    before_json = existing_authority.compiled_artifact_json

    result = compile_spec_authority_for_version_with_engine(
        engine=session.get_bind(),
        spec_version_id=existing_authority.spec_version_id,
        force_recompile=True,
    )

    rows = session.exec(
        select(CompiledSpecAuthority)
        .where(
            CompiledSpecAuthority.spec_version_id
            == existing_authority.spec_version_id
        )
        .order_by(cast("Any", CompiledSpecAuthority.authority_id).asc())
    ).all()
    assert len(rows) == 2
    assert rows[0].compiled_artifact_json == before_json
    assert result["authority_id"] == rows[1].authority_id
```

Run:

```bash
uv run pytest \
  tests/test_authority_selection.py \
  tests/test_specs_compiler_service.py \
  tests/test_agent_workbench_authority_regenerate.py \
  tests/test_story_validation_service.py \
  tests/test_orchestrator_context_service.py \
  tests/test_pending_authority_service.py \
  tests/test_export_snapshot.py \
  -q
```

Expected: FAIL because `_persist_compiled_authority()` updates, regeneration
clones/searches latest, and several readers use unordered spec-version lookup.

- [ ] **Step 2: Centralize authority-row selection before enabling multiple rows.**

Create `services/specs/authority_selection.py`:

```python
def compiled_authority_by_id(
    session: Session,
    *,
    authority_id: int,
    expected_spec_version_id: int | None = None,
) -> CompiledSpecAuthority | None:
    authority = session.get(CompiledSpecAuthority, authority_id)
    if (
        authority is not None
        and expected_spec_version_id is not None
        and authority.spec_version_id != expected_spec_version_id
    ):
        return None
    return authority


def latest_compiled_authority(
    session: Session,
    *,
    spec_version_id: int,
) -> CompiledSpecAuthority | None:
    return session.exec(
        select(CompiledSpecAuthority)
        .where(CompiledSpecAuthority.spec_version_id == spec_version_id)
        .order_by(cast("Any", CompiledSpecAuthority.authority_id).desc())
    ).first()


def compiled_authority_for_acceptance(
    session: Session,
    *,
    acceptance: SpecAuthorityAcceptance,
) -> CompiledSpecAuthority | None:
    authority_id = acceptance.pending_authority_id
    if authority_id is None:
        return None
    return compiled_authority_by_id(
        session,
        authority_id=authority_id,
        expected_spec_version_id=acceptance.spec_version_id,
    )


def latest_accepted_authority_decision(
    session: Session,
    *,
    product_id: int,
    spec_version_id: int,
) -> SpecAuthorityAcceptance | None:
    return session.exec(
        select(SpecAuthorityAcceptance)
        .where(
            SpecAuthorityAcceptance.product_id == product_id,
            SpecAuthorityAcceptance.spec_version_id == spec_version_id,
            SpecAuthorityAcceptance.status == "accepted",
        )
        .order_by(
            cast("Any", SpecAuthorityAcceptance.decided_at).desc(),
            cast("Any", SpecAuthorityAcceptance.id).desc(),
        )
    ).first()
```

Use these helpers from compiler, projection, regeneration, and the downstream
readers named in this task. Do not retain private copies with different
fallback rules.

- [ ] **Step 3: Make persistence insert-only.**

Delete the update branch from `_persist_compiled_authority()`:

```python
authority = CompiledSpecAuthority(
    spec_version_id=spec_version_id,
    compiler_version=compiler_version,
    prompt_hash=prompt_hash,
    compiled_at=datetime.now(UTC),
    compiled_artifact_json=compiled_artifact_json,
    scope_themes=json.dumps(scope_themes),
    invariants=json.dumps(invariants),
    eligible_feature_ids=json.dumps([]),
    rejected_features=json.dumps([]),
    spec_gaps=json.dumps(spec_gaps),
)
session.add(authority)
session.commit()
session.refresh(authority)
recompiled = force_recompile
```

Existing cache-hit logic remains responsible for avoiding this function during an ordinary non-forced reuse. Keep the lease check immediately before insertion and keep progress recording after the committed row exists.

- [ ] **Step 4: Bind mutations and readers to deterministic rows.**

Replace `_publish_pending_authority_candidate(request)` with an ID-based loader:

```python
def _load_compiled_candidate(
    self,
    *,
    request: AuthorityRegenerateRequest,
    compile_result: Mapping[str, object],
) -> CompiledSpecAuthority | None:
    authority_id = compile_result.get("authority_id")
    if not isinstance(authority_id, int):
        return None
    with Session(self.engine) as session:
        return compiled_authority_by_id(
            session,
            authority_id=authority_id,
            expected_spec_version_id=request.spec_version_id,
        )
```

Delete the update-then-clone path and any helper used only to find or clone the latest authority. Persist the returned ID in the mutation-ledger completion response so replay does not call compilation again.

Migrate the other multi-row consumers in the same commit:

- `pending_authority_service.py` trusts and verifies `compile_result["authority_id"]`;
- `story_validation_service.py` uses `latest_accepted_authority_decision()` plus `compiled_authority_for_acceptance()` and never promotes a newer pending candidate into execution validation;
- `orchestrator_context_service.py` uses `latest_compiled_authority()` before backfilling product cache;
- `export_snapshot.py` uses `latest_accepted_authority_decision()` plus `compiled_authority_for_acceptance()` when an accepted decision exists, fails closed if that exact accepted row is unavailable, otherwise uses `latest_compiled_authority()`, and parses through `load_compiled_artifact()` instead of directly accepting an arbitrary row;
- `as_built_assessment.py` and `evidence_collect.py` use `compiled_authority_for_acceptance()` and delete their latest-row fallbacks;
- `authority_projection.py`, `read_projection.py`, and `project_setup.py` use the shared exact/latest helpers instead of retaining local ordered-query copies.

- [ ] **Step 5: Run persistence, selection, and downstream-reader tests and commit.**

```bash
uv run pytest \
  tests/test_authority_selection.py \
  tests/test_specs_compiler_service.py \
  tests/test_agent_workbench_authority_regenerate.py \
  tests/test_agent_workbench_authority_projection.py \
  tests/test_agent_workbench_read_projection.py \
  tests/test_agent_workbench_project_setup.py \
  tests/test_as_built_assessment.py \
  tests/test_evidence_collect.py \
  tests/test_story_validation_service.py \
  tests/test_orchestrator_context_service.py \
  tests/test_pending_authority_service.py \
  tests/test_export_snapshot.py \
  -q
git diff --check
git add \
  services/specs/authority_selection.py \
  services/specs/compiler_service.py \
  services/agent_workbench/authority_regenerate.py \
  services/agent_workbench/authority_projection.py \
  services/agent_workbench/read_projection.py \
  services/agent_workbench/project_setup.py \
  services/agent_workbench/as_built_assessment.py \
  services/agent_workbench/evidence_collect.py \
  services/specs/story_validation_service.py \
  services/orchestrator_context_service.py \
  services/specs/pending_authority_service.py \
  tools/export_snapshot.py \
  tests/test_authority_selection.py \
  tests/test_specs_compiler_service.py \
  tests/test_agent_workbench_authority_regenerate.py \
  tests/test_agent_workbench_authority_projection.py \
  tests/test_agent_workbench_read_projection.py \
  tests/test_agent_workbench_project_setup.py \
  tests/test_as_built_assessment.py \
  tests/test_evidence_collect.py \
  tests/test_story_validation_service.py \
  tests/test_orchestrator_context_service.py \
  tests/test_pending_authority_service.py \
  tests/test_export_snapshot.py
git commit -m "fix: preserve authority history during regeneration"
```

---

### Task 5: Replace Review Regexes With Typed Re-Grounding

**Files:**

- Modify: `services/agent_workbench/authority_review.py`
- Test: `tests/test_agent_workbench_authority_review.py`
- Test: `tests/test_agent_workbench_authority_decision.py`

**Interfaces:**

- Consumes: a strictly loaded v3 artifact and the reviewed canonical structured spec.
- Produces: typed JSON assumptions, readable text, and non-overrideable claim findings.
- Deletes: `ONLY_ACCEPTED_ASSUMPTION_RE`, `_assumption_text()`, and legacy exclusivity finding construction.

- [ ] **Step 1: Write failing typed review tests.**

Add cases for:

- truthful item status, accepted count, and accepted set remain `accept_ready`;
- tampered status, count, set, artifact ID, or provenance produces `COMPILER_ASSUMPTION_CLAIM_MISMATCH`;
- unavailable structured source produces `COMPILER_ASSUMPTION_CLAIM_SOURCE_UNAVAILABLE`;
- claim findings are blocking and `override_allowed` is false;
- finding details contain the index, kind, claimed/actual values, artifact ID, and claimed/actual source IDs;
- finding identity changes when claim kind, value, provenance, or finding code changes;
- JSON review output preserves typed objects;
- text review output renders all four variants readably;
- no review path reparses rendered prose.

Replace the old exact-sentence tests with a true singleton-set claim and a false singleton-set claim. The free-text paraphrases belong in normalizer boundary tests.

Run:

```bash
uv run pytest \
  tests/test_agent_workbench_authority_review.py \
  tests/test_agent_workbench_authority_decision.py \
  -q
```

Expected: FAIL because review still relies on one regex.

- [ ] **Step 2: Implement typed stored-claim findings.**

Iterate over `SpecAuthorityCompilationSuccess.assumptions`, not an untyped mapping:

```python
def _compiled_assumption_findings(
    *,
    artifact: SpecAuthorityCompilationSuccess,
    spec_artifact: TechnicalSpecArtifact | None,
) -> list[JsonDict]:
    findings: list[JsonDict] = []
    for index, assumption in enumerate(artifact.assumptions, start=1):
        if not is_structured_assumption(assumption):
            continue
        if spec_artifact is None:
            findings.append(
                _compiled_claim_source_unavailable_finding(
                    assumption_index=index,
                    assumption=assumption,
                )
            )
            continue
        grounded = ground_assumption(assumption, spec_artifact)
        if isinstance(grounded, GroundingFailure):
            findings.append(
                _compiled_claim_mismatch_finding(
                    assumption_index=index,
                    assumption=assumption,
                    failure=grounded,
                )
            )
    return findings
```

Build finding identity from:

```python
payload = {
    "assumption_key": canonical_assumption_key(assumption),
    "code": code,
    "claimed_source_item_ids": list(failure.claimed_source_item_ids),
}
```

Serialize the required `details` fields and set `override_allowed=False`.

- [ ] **Step 3: Use shared rendering and delete the legacy path.**

Render review-visible assumptions with:

```python
assumptions = [
    _plain_item(
        item_id=f"ASM-{index}",
        text=render_assumption_text(assumption),
    )
    for index, assumption in enumerate(artifact.assumptions, start=1)
]
```

The JSON packet keeps `artifact.assumptions` as typed objects. For text output,
convert only the value passed to `_append_text_item_lines()`:

```python
def _rendered_assumption_items(value: object) -> list[JsonDict]:
    rendered: list[JsonDict] = []
    for index, raw_assumption in enumerate(_as_list(value), start=1):
        assumption = AUTHORITY_ASSUMPTION_ADAPTER.validate_python(raw_assumption)
        rendered.append(
            {
                "id": f"ASM-{index}",
                "text": render_assumption_text(assumption),
                "assumption_key": canonical_assumption_key(assumption),
            }
        )
    return rendered
```

Use that helper in `_render_review_text()`, fallback classification evidence,
and any review summary path that currently calls `str(dict)` for an assumption.
Keep malformed v3 artifacts blocked by the existing shape finding; rendering
must not make them valid. Replace hard-coded required-version display values
with `COMPILED_AUTHORITY_SCHEMA_VERSION`.

Delete `ONLY_ACCEPTED_ASSUMPTION_RE`, `_assumption_text()`, `_normalize_structured_item_id()` if no other caller remains, and the `COMPILER_ASSUMPTION_UNSUPPORTED` builder. Do not retain aliases for the old finding code.

- [ ] **Step 4: Run review tests and commit.**

```bash
uv run pytest \
  tests/test_agent_workbench_authority_review.py \
  tests/test_agent_workbench_authority_decision.py \
  tests/test_agent_workbench_authority_projection.py \
  -q
git diff --check
git add \
  services/agent_workbench/authority_review.py \
  tests/test_agent_workbench_authority_review.py \
  tests/test_agent_workbench_authority_decision.py
git commit -m "feat: ground typed assumptions during authority review"
```

---

### Task 6: Make Structured Claims Read-Only in Curation

**Files:**

- Modify: `services/agent_workbench/error_codes.py`
- Modify: `services/agent_workbench/authority_curation.py`
- Modify: `services/specs/authority_curation_diff.py`
- Test: `tests/test_agent_workbench_error_codes.py`
- Test: `tests/test_agent_workbench_authority_curation.py`

**Interfaces:**

- Consumes: feedback targets, a v3 source artifact, and a proposed curation patch/candidate.
- Produces: a free-text-only assumption edit or `AUTHORITY_CURATION_TARGET_READ_ONLY`.
- Prevents: semantic or provenance edits to structured claims before candidate persistence.

- [ ] **Step 1: Write failing curation and diff tests.**

Add cases proving:

- `replace_text` updates only `FreeTextAssumption.text`;
- targeting an item-status, count, or set claim returns `AUTHORITY_CURATION_TARGET_READ_ONLY`;
- attempting to change structured value, `artifact_id`, or `source_item_ids` through a full candidate also returns the read-only error;
- the error occurs before a candidate row or curation success event is persisted;
- curation target content hashes use `canonical_assumption_key()`;
- positional review IDs remain `ASM-1`, `ASM-2`, after final deduplication;
- ordinary gap and invariant curation is unchanged.

```python
def test_authority_curate_rejects_structured_assumption_patch(
    authority_runner: AuthorityCurationRunner,
) -> None:
    result = authority_runner.curate(
        _request_targeting_assumption("ASM-1"),
        repair_output={
            "patches": [
                {
                    "target_kind": "assumption",
                    "target_id": "ASM-1",
                    "operation": "replace_text",
                    "value": "Only REQ.alpha was accepted.",
                }
            ]
        },
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == (
        "AUTHORITY_CURATION_TARGET_READ_ONLY"
    )
```

Run:

```bash
uv run pytest \
  tests/test_agent_workbench_error_codes.py \
  tests/test_agent_workbench_authority_curation.py \
  -q
```

Expected: FAIL because all assumption targets are currently patchable.

- [ ] **Step 2: Register the exact error code.**

Add to `ErrorCode` and `_ERROR_REGISTRY`:

```python
AUTHORITY_CURATION_TARGET_READ_ONLY = "AUTHORITY_CURATION_TARGET_READ_ONLY"

ErrorCode.AUTHORITY_CURATION_TARGET_READ_ONLY: ErrorMetadata(
    code=ErrorCode.AUTHORITY_CURATION_TARGET_READ_ONLY.value,
    default_exit_code=4,
    retryable=False,
    description="The authority curation target is read-only.",
),
```

- [ ] **Step 3: Split assumption lookup from legacy text-or-dict lookup.**

Gaps may keep their current text/dict helper. Assumptions must parse as v3 objects:

```python
def _find_assumption_target(
    value: object,
    *,
    target_id: str,
) -> tuple[list[Any], int, AuthorityAssumption] | None:
    if not isinstance(value, list) or not target_id.startswith("ASM-"):
        return None
    raw_index = target_id.removeprefix("ASM-")
    if not raw_index.isdigit():
        return None
    index = int(raw_index) - 1
    if index < 0 or index >= len(value):
        return None
    assumption = AUTHORITY_ASSUMPTION_ADAPTER.validate_python(value[index])
    return value, index, assumption
```

Before applying an assumption patch:

```python
if not isinstance(assumption, FreeTextAssumption):
    raise _AuthorityCurationTargetReadOnlyError(target_id)
```

For an allowed edit, replace the list item with:

```python
FreeTextAssumption(kind="free_text", text=replacement).model_dump(mode="json")
```

Bind repair-menu content hashes and assumption diff equality to `canonical_assumption_key()`. Add a pre-persistence comparison that rejects any structured claim whose canonical key differs between source and candidate, even if a full-candidate workflow bypassed the patch applier.

- [ ] **Step 4: Run curation tests and commit.**

```bash
uv run pytest \
  tests/test_agent_workbench_error_codes.py \
  tests/test_agent_workbench_authority_curation.py \
  tests/test_agent_workbench_authority_review.py \
  -q
git diff --check
git add \
  services/agent_workbench/error_codes.py \
  services/agent_workbench/authority_curation.py \
  services/specs/authority_curation_diff.py \
  tests/test_agent_workbench_error_codes.py \
  tests/test_agent_workbench_authority_curation.py
git commit -m "feat: protect structured assumptions from curation"
```

---

### Task 7: Complete the V3 Fixture, Benchmark, and Operator-Documentation Migration

**Files:**

- Create: `tests/authority_assumption_fixtures.py`
- Modify: all current-path compiled-authority fixtures found by the inventory commands below
- Modify: `benchmarks/authority-quality/todomvc/agileforge/compiled-authority.json`
- Modify: `docs/agent-cli-manual.md`
- Preserve: historical design documents under `docs/superpowers/specs/`

**Interfaces:**

- Consumes: current-path test/benchmark authority payloads.
- Produces: v3 typed fixtures, plus explicitly named historical-v2 fixtures for unsupported/regeneration tests.
- Makes the remaining repository test graph agree with the breaking schema boundary.

- [ ] **Step 1: Add explicit fixture constructors.**

Use a small test-only helper so current and historical contracts cannot be confused:

```python
def free_text_assumption(text: str) -> dict[str, str]:
    return {"kind": "free_text", "text": text}


def historical_v2_compiled_authority(
    *,
    prompt_hash: str,
    assumptions: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "agileforge.compiled_authority.v2",
        "scope_themes": [],
        "domain": None,
        "invariants": [],
        "eligible_feature_rules": [],
        "rejected_features": [],
        "gaps": [],
        "assumptions": assumptions or [],
        "source_map": [],
        "compiler_version": "2.0.0",
        "prompt_hash": prompt_hash,
        "ir_schema_version": None,
        "ir_provenance": None,
    }
```

Current fixtures must use v3 and `3.0.0`; only tests whose name asserts unsupported history or regeneration from history may call `historical_v2_compiled_authority()`.

- [ ] **Step 2: Run the residual fixture inventory and migrate every current path.**

Inventory:

```bash
rg -l \
  'agileforge\.compiled_authority\.v2|compiler_version.?[:=].?"2\.0\.0"' \
  tests benchmarks docs/agent-cli-manual.md

rg -l \
  'assumptions\s*=\s*\[|"assumptions"\s*:\s*\[' \
  tests benchmarks \
  --glob '*.py' \
  --glob '*.json'
```

The migration must cover current-path fixtures in these areas:

- agent workbench review, decision, projection, regeneration, curation, CLI, execution guard, phase-one integration, and scope discovery;
- API dashboard and sprint flow;
- as-built assessment and evidence collection;
- authority gate and quality benchmark;
- export/import and snapshot;
- spec authority, compiler tool, compiler agent, normalizer, schemas, validation modes, and compiler service;
- alignment evidence, story validation/pinning, and update-spec compile flows.

Preserve v2 only where the test explicitly proves unsupported loading, status routing, historical-row immutability, or regeneration from an old accepted row.

- [ ] **Step 3: Update the benchmark fixture and operator manual.**

Convert the TodoMVC compiled-authority benchmark to v3 typed assumptions without changing its semantic content. Update `docs/agent-cli-manual.md` so regeneration says “fresh v3 authority,” explains that v2 is unsupported history, states that regeneration inserts a pending row without transferring acceptance, and records the forward-fix rollback rule: a restored v2 runtime must retain append-only persistence and may not rewrite historical rows.

Do not edit the June v2 design documents; they are historical records.

- [ ] **Step 4: Run the migrated consumer suites and commit.**

```bash
uv run pytest \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_execution_guard.py \
  tests/test_agent_workbench_phase1_integration.py \
  tests/test_agent_workbench_scope_discovery.py \
  tests/test_agent_workbench_vision_phase.py \
  tests/test_alignment_evidence_persistence.py \
  tests/test_api_backlog_flow.py \
  tests/test_api_dashboard.py \
  tests/test_api_roadmap_flow.py \
  tests/test_api_sprint_flow.py \
  tests/test_api_story_interview_flow.py \
  tests/test_api_vision_flow.py \
  tests/test_as_built_assessment.py \
  tests/test_authority_gate.py \
  tests/test_authority_quality_benchmark.py \
  tests/test_evidence_collect.py \
  tests/test_export_import_labels.py \
  tests/test_export_snapshot.py \
  tests/test_orchestrator_context_service.py \
  tests/test_phase_workflow_state.py \
  tests/test_select_project_hydration.py \
  tests/test_setup_service.py \
  tests/test_spec_authority.py \
  tests/test_spec_authority_compile_tool.py \
  tests/test_spec_validation_modes.py \
  tests/test_story_validation_pinning.py \
  tests/test_story_validation_service.py \
  tests/test_update_spec_and_compile_authority.py \
  -q
git diff --check
git add \
  tests/authority_assumption_fixtures.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_execution_guard.py \
  tests/test_agent_workbench_phase1_integration.py \
  tests/test_agent_workbench_scope_discovery.py \
  tests/test_agent_workbench_vision_phase.py \
  tests/test_alignment_evidence_persistence.py \
  tests/test_api_backlog_flow.py \
  tests/test_api_dashboard.py \
  tests/test_api_roadmap_flow.py \
  tests/test_api_sprint_flow.py \
  tests/test_api_story_interview_flow.py \
  tests/test_api_vision_flow.py \
  tests/test_as_built_assessment.py \
  tests/test_authority_gate.py \
  tests/test_authority_quality_benchmark.py \
  tests/test_evidence_collect.py \
  tests/test_export_import_labels.py \
  tests/test_export_snapshot.py \
  tests/test_orchestrator_context_service.py \
  tests/test_phase_workflow_state.py \
  tests/test_select_project_hydration.py \
  tests/test_setup_service.py \
  tests/test_spec_authority.py \
  tests/test_spec_authority_compile_tool.py \
  tests/test_spec_validation_modes.py \
  tests/test_story_validation_pinning.py \
  tests/test_story_validation_service.py \
  tests/test_update_spec_and_compile_authority.py \
  benchmarks/authority-quality/todomvc/agileforge/compiled-authority.json \
  docs/agent-cli-manual.md
git commit -m "test: migrate compiled authority fixtures to v3"
```

---

### Task 8: Verify the Complete Trust Boundary and Prepare Delivery

**Files:**

- Verify: all files changed by Tasks 1-7
- Compare against: `docs/superpowers/specs/2026-07-23-compiled-authority-v3-typed-assumptions-design.md`

**Interfaces:**

- Consumes: the complete branch.
- Produces: test evidence, a clean diff, and a delivery-ready branch.
- Does not mutate real project authority rows.

- [ ] **Step 1: Run the focused trust-boundary suite.**

```bash
uv run pytest \
  tests/test_spec_authority_assumptions.py \
  tests/test_authority_selection.py \
  tests/test_spec_schema_modules.py \
  tests/test_spec_authority_ir.py \
  tests/test_spec_authority_compiler_agent.py \
  tests/test_spec_authority_compiler_normalizer.py \
  tests/test_specs_compiler_service.py \
  tests/test_authority_quality_gate.py \
  tests/test_agent_workbench_authority_projection.py \
  tests/test_agent_workbench_read_projection.py \
  tests/test_agent_workbench_project_setup.py \
  tests/test_agent_workbench_authority_review.py \
  tests/test_agent_workbench_authority_decision.py \
  tests/test_agent_workbench_authority_regenerate.py \
  tests/test_agent_workbench_authority_curation.py \
  tests/test_agent_workbench_error_codes.py \
  tests/test_as_built_assessment.py \
  tests/test_evidence_collect.py \
  tests/test_story_validation_service.py \
  tests/test_orchestrator_context_service.py \
  tests/test_pending_authority_service.py \
  tests/test_export_snapshot.py \
  -q
```

- [ ] **Step 2: Prove the legacy implementation is gone.**

```bash
rg -n \
  'ONLY_ACCEPTED_ASSUMPTION_RE|_assumption_text|COMPILER_ASSUMPTION_UNSUPPORTED' \
  services utils orchestrator_agent

rg -n \
  'list\[str\].*assumption|assumptions:.*list\[str\]|str \| AuthorityAssumption|AuthorityAssumption \| str' \
  services utils orchestrator_agent
```

Expected: no production matches.

Review every remaining v2 match:

```bash
rg -n 'agileforge\.compiled_authority\.v2|compiler_version.?[:=].?"2\.0\.0"' \
  services utils orchestrator_agent tests benchmarks docs/agent-cli-manual.md
```

Expected: only deliberately named historical/unsupported fixtures. Production compiler and current benchmark/manual paths contain no v2 contract.

- [ ] **Step 3: Run the full repository gate.**

```bash
git diff --check
uv run --frozen pyrepo-check --all
git status --short
```

Expected:

- `git diff --check` exits 0;
- `pyrepo-check --all` exits 0;
- `git status --short` shows only the intended implementation-plan checkbox update if execution recorded progress after the final implementation commit.

- [ ] **Step 4: Self-review every acceptance criterion.**

Confirm from code and tests:

- v3/3.0.0 only for current compiled authority;
- strict discriminated assumptions;
- exact finite free-text cue failure and one retry;
- deterministic grounding before persistence and during review;
- partial-scope aggregate rejection/invalidation;
- canonical identity used in all six named consumers;
- append-only forced compilation and ledger replay;
- exact/latest authority-row selection remains deterministic with retained v2 history;
- typed JSON and readable text rendering;
- structured curation read-only;
- v2 unsupported with explicit regeneration guidance;
- no automatic conversion, regeneration, or acceptance;
- all issue #195 paraphrases are regression tests.

- [ ] **Step 5: Prepare, but do not perform, the operator rollout.**

For each active project ID identified by the operator, collect a read-only status snapshot:

```bash
uv run agileforge authority status --project-id "$PROJECT_ID"
```

After the code is reviewed and deployed, each unsupported active project is regenerated and reviewed explicitly:

```bash
uv run agileforge authority regenerate \
  --project-id "$PROJECT_ID" \
  --spec-version-id "$SPEC_VERSION_ID" \
  --idempotency-key "$IDEMPOTENCY_KEY"

uv run agileforge authority review --project-id "$PROJECT_ID"
```

Do not run these mutation commands during implementation verification. Do not transfer the old acceptance. Keep issue #195 open until the reviewed change is delivered; close it only with the final test/PR evidence.
