# Authority Quality Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a project-agnostic post-compile authority quality gate that merges only exact semantic duplicates, reports near-duplicate/over-split/noisy groups for human review, and keeps create/regenerate stopped at pending authority review.

**Architecture:** Add strict `authority_quality` schema models to the compiled-authority v2 artifact, revise invariant IDs to include source provenance, add a focused `services/specs/authority_quality.py` gate, and run it from compiler persistence paths before review packets are built. Review projection and dashboard read the persisted report; they do not recompute quality.

**Tech Stack:** Python 3.13, Pydantic v2, SQLModel-backed compiler persistence, existing AgileForge CLI/authority review JSON, existing vanilla JS dashboard.

---

## File Structure

- Modify `utils/spec_schemas.py`
  - Add `AuthorityQualitySummary`, `AuthorityQualityMergedItem`,
    `AuthorityQualityReviewGroup`, and `AuthorityQualityReport`.
  - Add optional `authority_quality` to `SpecAuthorityCompilationSuccess`.
- Modify `utils/schemes.py`
  - Re-export the new schema models.
- Modify `orchestrator_agent/agent_tools/spec_authority_compiler_agent/compiler_contract.py`
  - Extend `compute_invariant_id_from_payload()` with optional
    `source_item_id` and `source_level` keyword-only inputs.
- Modify `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`
  - Make duplicate pre-cleanup provenance-aware.
  - Recompute invariant IDs with provenance.
- Create `services/specs/authority_quality.py`
  - Own deterministic quality gate, merge logic, grouping heuristics, and report
    construction.
- Modify `services/specs/compiler_service.py`
  - Apply the gate before every compiled authority persist path.
- Modify `services/agent_workbench/authority_review.py`
  - Include `authority_quality` in review artifact payload and review summary.
- Modify `frontend/project.html`
  - Add a compact quality section to the authority overview.
- Modify `frontend/project.js`
  - Render quality summary, merge decisions, and review groups from review packet.
- Add `tests/test_authority_quality_gate.py`
  - Focused unit tests for merge/group behavior.
- Modify `tests/test_spec_schema_modules.py`
  - Schema/export tests for new models and optional old-artifact compatibility.
- Modify `tests/test_spec_authority_compiler_normalizer.py`
  - Provenance-aware invariant ID regression tests.
- Modify `tests/test_specs_compiler_service.py`
  - Persistence tests proving create/regenerate compilation stores quality report.
- Modify `tests/test_agent_workbench_authority_review.py`
  - Review packet tests proving quality report is exposed.
- Modify `tests/test_authority_review_console.mjs`
  - Dashboard rendering test for quality section.

## Implementation Decisions

- Near-duplicate invariant threshold: token Jaccard `>= 0.72`.
- Noisy/near-duplicate assumption threshold: token Jaccard `>= 0.82`.
- Over-split source-item threshold: `>= 5` invariants from one `source_item_id`.
- Over-split subject/type threshold: `>= 3` invariants in the same
  `(source_item_id, invariant type, subject-like parameter)` bucket.
- Max review groups persisted in full: `40`.
- Max group members persisted in full: `12`.
- Quality groups are non-blocking warnings.
- No ASA-specific terms, IDs, thresholds, project names, or domain words in gate
  code.

### Task 1: Schema Models For Authority Quality

**Files:**
- Modify: `utils/spec_schemas.py`
- Modify: `utils/schemes.py`
- Test: `tests/test_spec_schema_modules.py`

- [ ] **Step 1: Write failing schema tests**

Append this test to `tests/test_spec_schema_modules.py`:

```python
def test_authority_quality_report_schema_is_optional_and_strict() -> None:
    """Compiled authority v2 supports optional quality report metadata."""
    from pydantic import ValidationError  # noqa: PLC0415
    from utils.spec_schemas import (  # noqa: PLC0415
        AuthorityQualityMergedItem,
        AuthorityQualityReport,
        AuthorityQualityReviewGroup,
        AuthorityQualitySummary,
        SpecAuthorityCompilationSuccess,
    )

    success = SpecAuthorityCompilationSuccess(
        scope_themes=["Payments"],
        domain=None,
        invariants=[],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version="2.0.0",
        prompt_hash="a" * 64,
    )
    assert success.authority_quality is None

    report = AuthorityQualityReport(
        summary=AuthorityQualitySummary(
            original_invariant_count=2,
            final_invariant_count=1,
            merged_invariant_count=1,
            merged_assumption_count=0,
            review_group_count=1,
            near_duplicate_group_count=0,
            over_split_group_count=1,
            noisy_assumption_group_count=0,
        ),
        merged_items=[
            AuthorityQualityMergedItem(
                merge_id="AQ-MERGE-001",
                item_kind="invariant",
                kept_id="INV-1111111111111111",
                removed_ids=["INV-2222222222222222"],
                reason="exact_semantic_duplicate",
                source_evidence_count=2,
            )
        ],
        review_groups=[
            AuthorityQualityReviewGroup(
                group_id="AQ-GROUP-001",
                group_type="over_split_invariants",
                severity="warning",
                member_ids=["INV-1111111111111111"],
                reason="same source item produced many invariants",
                merge_allowed=False,
            )
        ],
    )
    success.authority_quality = report

    dumped = success.model_dump(mode="json")
    assert dumped["authority_quality"]["schema_version"] == (
        "agileforge.authority_quality.v1"
    )
    assert dumped["authority_quality"]["summary"]["merged_invariant_count"] == 1

    with pytest.raises(ValidationError):
        AuthorityQualityReviewGroup(
            group_id="AQ-GROUP-002",
            group_type="unsupported",
            severity="warning",
            member_ids=[],
            reason="bad type",
            merge_allowed=False,
        )


def test_compat_schemes_reexports_authority_quality_models() -> None:
    """Compatibility schema module re-exports authority quality models."""
    from utils import schemes, spec_schemas  # noqa: PLC0415

    assert schemes.AuthorityQualityReport is spec_schemas.AuthorityQualityReport
    assert schemes.AuthorityQualitySummary is spec_schemas.AuthorityQualitySummary
    assert (
        schemes.AuthorityQualityReviewGroup
        is spec_schemas.AuthorityQualityReviewGroup
    )
    assert schemes.AuthorityQualityMergedItem is spec_schemas.AuthorityQualityMergedItem
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run --frozen pytest tests/test_spec_schema_modules.py::test_authority_quality_report_schema_is_optional_and_strict tests/test_spec_schema_modules.py::test_compat_schemes_reexports_authority_quality_models -q
```

Expected: fail because the new schema models do not exist.

- [ ] **Step 3: Add schema models**

In `utils/spec_schemas.py`, insert these models immediately before
`class SpecAuthorityCompilationSuccess`:

```python
AuthorityQualityGroupType = Literal[
    "near_duplicate_invariants",
    "over_split_invariants",
    "related_source_variants",
    "noisy_assumptions",
]
AuthorityQualitySeverity = Literal["info", "warning"]
AuthorityQualityItemKind = Literal["invariant", "assumption"]


class AuthorityQualitySummary(BaseModel):
    """Compact counts produced by the authority quality gate."""

    model_config = ConfigDict(extra="forbid")

    original_invariant_count: Annotated[int, Field(ge=0)]
    final_invariant_count: Annotated[int, Field(ge=0)]
    merged_invariant_count: Annotated[int, Field(ge=0)]
    merged_assumption_count: Annotated[int, Field(ge=0)]
    review_group_count: Annotated[int, Field(ge=0)]
    near_duplicate_group_count: Annotated[int, Field(ge=0)]
    over_split_group_count: Annotated[int, Field(ge=0)]
    noisy_assumption_group_count: Annotated[int, Field(ge=0)]


class AuthorityQualityMergedItem(BaseModel):
    """One auto-merge decision made by the quality gate."""

    model_config = ConfigDict(extra="forbid")

    merge_id: Annotated[str, Field(min_length=1)]
    item_kind: AuthorityQualityItemKind
    kept_id: Annotated[str, Field(min_length=1)]
    removed_ids: Annotated[list[str], Field(min_length=1)]
    reason: Annotated[str, Field(min_length=1)]
    source_evidence_count: Annotated[int, Field(ge=0)] = 0


class AuthorityQualityReviewGroup(BaseModel):
    """Related authority items that need human review."""

    model_config = ConfigDict(extra="forbid")

    group_id: Annotated[str, Field(min_length=1)]
    group_type: AuthorityQualityGroupType
    severity: AuthorityQualitySeverity = "warning"
    member_ids: Annotated[list[str], Field(min_length=1)]
    reason: Annotated[str, Field(min_length=1)]
    merge_allowed: bool = False
    truncated: bool = False


class AuthorityQualityReport(BaseModel):
    """Persisted authority quality gate report."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agileforge.authority_quality.v1"] = (
        "agileforge.authority_quality.v1"
    )
    summary: AuthorityQualitySummary
    merged_items: list[AuthorityQualityMergedItem] = Field(default_factory=list)
    review_groups: list[AuthorityQualityReviewGroup] = Field(default_factory=list)
```

Then add this field to `SpecAuthorityCompilationSuccess` after `source_map`:

```python
    authority_quality: AuthorityQualityReport | None = Field(
        default=None,
        description="Optional host-derived quality report for review.",
    )
```

- [ ] **Step 4: Re-export schema models**

In `utils/schemes.py`, add these imports from `.spec_schemas`:

```python
    AuthorityQualityMergedItem,
    AuthorityQualityReport,
    AuthorityQualityReviewGroup,
    AuthorityQualitySummary,
```

Add the same names to `__all__`.

- [ ] **Step 5: Verify schema tests pass**

Run:

```bash
uv run --frozen pytest tests/test_spec_schema_modules.py::test_authority_quality_report_schema_is_optional_and_strict tests/test_spec_schema_modules.py::test_compat_schemes_reexports_authority_quality_models -q
```

Expected: pass.

- [ ] **Step 6: Commit schema task**

```bash
git add utils/spec_schemas.py utils/schemes.py tests/test_spec_schema_modules.py
git commit -m "feat: add authority quality report schema"
```

### Task 2: Provenance-Aware Invariant IDs

**Files:**
- Modify: `orchestrator_agent/agent_tools/spec_authority_compiler_agent/compiler_contract.py`
- Modify: `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`
- Test: `tests/test_spec_authority_compiler_normalizer.py`

- [ ] **Step 1: Write failing provenance ID tests**

Append these tests to `tests/test_spec_authority_compiler_normalizer.py`:

```python
def test_normalizer_invariant_ids_include_source_provenance() -> None:
    """Same rule shape from different source items keeps distinct IDs."""
    payload = _base_success_payload()
    payload["invariants"] = [
        {
            "id": "INV-0000000000000000",
            "type": "REQUIRED_FIELD",
            "source_item_id": "REQ.alpha",
            "source_level": "MUST",
            "parameters": {"field_name": "email"},
        },
        {
            "id": "INV-0000000000000001",
            "type": "REQUIRED_FIELD",
            "source_item_id": "REQ.beta",
            "source_level": "MUST",
            "parameters": {"field_name": "email"},
        },
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-0000000000000000",
            "excerpt": "Alpha requires email.",
            "location": "REQ.alpha.statement",
        },
        {
            "invariant_id": "INV-0000000000000001",
            "excerpt": "Beta requires email.",
            "location": "REQ.beta.statement",
        },
    ]

    normalized = normalize_compiler_output(json.dumps(payload))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    ids = [invariant.id for invariant in normalized.root.invariants]
    assert len(ids) == 2
    assert len(set(ids)) == 2
    assert {
        entry.invariant_id for entry in normalized.root.source_map
    } == set(ids)


def test_normalizer_merges_only_same_provenance_exact_duplicates() -> None:
    """Normalizer pre-cleanup does not collapse source-distinct same-shaped rules."""
    payload = _base_success_payload()
    payload["invariants"] = [
        {
            "id": "INV-0000000000000000",
            "type": "REQUIRED_FIELD",
            "source_item_id": "REQ.alpha",
            "source_level": "MUST",
            "parameters": {"field_name": "email"},
        },
        {
            "id": "INV-0000000000000001",
            "type": "REQUIRED_FIELD",
            "source_item_id": "REQ.alpha",
            "source_level": "MUST",
            "parameters": {"field_name": "email"},
        },
        {
            "id": "INV-0000000000000002",
            "type": "REQUIRED_FIELD",
            "source_item_id": "REQ.beta",
            "source_level": "MUST",
            "parameters": {"field_name": "email"},
        },
    ]
    payload["source_map"] = []

    normalized = normalize_compiler_output(json.dumps(payload))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert len(normalized.root.invariants) == 2
    assert {
        invariant.source_item_id for invariant in normalized.root.invariants
    } == {"REQ.alpha", "REQ.beta"}


def test_normalizer_exact_duplicate_cleanup_preserves_source_map_entries() -> None:
    """Exact duplicate cleanup keeps every supporting source-map entry."""
    payload = _base_success_payload()
    payload["invariants"] = [
        {
            "id": "INV-0000000000000000",
            "type": "REQUIRED_FIELD",
            "source_item_id": "REQ.alpha",
            "source_level": "MUST",
            "parameters": {"field_name": "email"},
        },
        {
            "id": "INV-0000000000000001",
            "type": "REQUIRED_FIELD",
            "source_item_id": "REQ.alpha",
            "source_level": "MUST",
            "parameters": {"field_name": "email"},
        },
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-0000000000000000",
            "excerpt": "Alpha requires email.",
            "location": "REQ.alpha.statement",
        },
        {
            "invariant_id": "INV-0000000000000001",
            "excerpt": "Email is required for alpha.",
            "location": "REQ.alpha.acceptance[0]",
        },
    ]

    normalized = normalize_compiler_output(json.dumps(payload))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert len(normalized.root.invariants) == 1
    kept_id = normalized.root.invariants[0].id
    assert [entry.invariant_id for entry in normalized.root.source_map] == [
        kept_id,
        kept_id,
    ]
```

If `_base_success_payload` is not available in the target file, create a small
local helper near existing payload helpers:

```python
def _base_success_payload() -> dict[str, object]:
    return {
        "schema_version": "agileforge.compiled_authority.v2",
        "scope_themes": ["Payments"],
        "domain": None,
        "invariants": [],
        "eligible_feature_rules": [],
        "rejected_features": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [],
        "compiler_version": "2.0.0",
        "prompt_hash": "a" * 64,
    }
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py::test_normalizer_invariant_ids_include_source_provenance tests/test_spec_authority_compiler_normalizer.py::test_normalizer_merges_only_same_provenance_exact_duplicates tests/test_spec_authority_compiler_normalizer.py::test_normalizer_exact_duplicate_cleanup_preserves_source_map_entries -q
```

Expected: fail because current IDs ignore source provenance.

- [ ] **Step 3: Extend invariant ID helper**

In `compiler_contract.py`, replace `compute_invariant_id_from_payload()` with:

```python
def compute_invariant_id_from_payload(
    invariant_type: InvariantType,
    parameters: InvariantParameters | None = None,
    *,
    source_item_id: str | None = None,
    source_level: object | None = None,
) -> str:
    """Compute deterministic invariant ID from semantics and provenance."""
    parameter_seed = _canonical_parameter_seed(parameters)
    level_value = getattr(source_level, "value", source_level)
    provenance_seed = "|".join(
        (
            str(source_item_id or ""),
            str(level_value or ""),
        )
    )
    seed = f"{invariant_type.value}|{parameter_seed}|{provenance_seed}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"INV-{digest[:16]}"
```

Callers that omit provenance remain deterministic but new compiled authority
paths will pass provenance.

- [ ] **Step 4: Make normalizer duplicate cleanup provenance-aware**

In `normalizer.py`, replace `_invariant_semantic_key()` with:

```python
def _invariant_semantic_key(inv: Invariant) -> tuple[str, str, str, str]:
    """Return stable semantic identity for exact duplicate invariant removal."""
    level_value = getattr(inv.source_level, "value", inv.source_level)
    return (
        inv.type.value,
        json.dumps(inv.parameters.model_dump(mode="json"), sort_keys=True),
        inv.source_item_id or "",
        str(level_value or ""),
    )
```

Update the ID rewrite loop in `normalize_compiler_output()`:

```python
    for inv in success.invariants:
        inv.id = compute_invariant_id_from_payload(
            inv.type,
            inv.parameters,
            source_item_id=inv.source_item_id,
            source_level=inv.source_level,
        )
```

Also update `_deduplicate_semantic_invariants()` so it remaps source-map entries
for removed exact duplicates before replacing the invariant list:

```python
def _deduplicate_semantic_invariants(success: SpecAuthorityCompilationSuccess) -> int:
    """Remove exact duplicate invariant objects before deterministic ID assignment."""
    seen: dict[tuple[str, str, str, str], Invariant] = {}
    kept: list[Invariant] = []
    removed_to_kept: dict[str, str] = {}
    removed = 0
    for inv in success.invariants:
        key = _invariant_semantic_key(inv)
        existing = seen.get(key)
        if existing is not None:
            removed_to_kept[inv.id] = existing.id
            removed += 1
            continue
        seen[key] = inv
        kept.append(inv)
    if not removed:
        return 0
    success.invariants = kept
    for entry in success.source_map:
        entry.invariant_id = removed_to_kept.get(
            entry.invariant_id,
            entry.invariant_id,
        )
    if _DUPLICATE_INVARIANT_ASSUMPTION not in success.assumptions:
        success.assumptions.append(_DUPLICATE_INVARIANT_ASSUMPTION)
    logger.info("Removed %s duplicate semantic invariant(s)", removed)
    return removed
```

- [ ] **Step 5: Update existing exact ID assertions**

Search:

```bash
rg -n "compute_invariant_id_from_payload\\(" tests/test_spec_authority_compiler_normalizer.py tests/test_spec_authority_compile_tool.py
```

Where a test expects normalized IDs for invariants with provenance, update the
expected call to pass `source_item_id=invariant.source_item_id` and
`source_level=invariant.source_level`.

Use this pattern:

```python
expected_id = compute_invariant_id_from_payload(
    invariant.type,
    invariant.parameters,
    source_item_id=invariant.source_item_id,
    source_level=invariant.source_level,
)
```

- [ ] **Step 6: Verify normalizer focused tests pass**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py::test_normalizer_invariant_ids_include_source_provenance tests/test_spec_authority_compiler_normalizer.py::test_normalizer_merges_only_same_provenance_exact_duplicates tests/test_spec_authority_compiler_normalizer.py::test_normalizer_exact_duplicate_cleanup_preserves_source_map_entries -q
```

Expected: pass.

- [ ] **Step 7: Run broader ID-sensitive tests**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py tests/test_spec_authority_compile_tool.py -q
```

Expected: pass.

- [ ] **Step 8: Commit provenance ID task**

```bash
git add orchestrator_agent/agent_tools/spec_authority_compiler_agent/compiler_contract.py orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py tests/test_spec_authority_compiler_normalizer.py tests/test_spec_authority_compile_tool.py
git commit -m "fix: include provenance in authority invariant ids"
```

### Task 3: Authority Quality Gate Unit

**Files:**
- Create: `services/specs/authority_quality.py`
- Test: `tests/test_authority_quality_gate.py`

- [ ] **Step 1: Write failing quality gate tests**

Create `tests/test_authority_quality_gate.py`:

```python
"""Tests for project-agnostic compiled authority quality gate."""

from __future__ import annotations

from services.specs.authority_quality import apply_authority_quality_gate
from utils.spec_schemas import (
    DataContractParams,
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
    StateTransitionParams,
)


def _success(
    *,
    invariants: list[Invariant],
    assumptions: list[str] | None = None,
    source_map: list[SourceMapEntry] | None = None,
) -> SpecAuthorityCompilationSuccess:
    return SpecAuthorityCompilationSuccess(
        scope_themes=["Project"],
        domain=None,
        invariants=invariants,
        eligible_feature_rules=[],
        rejected_features=[],
        gaps=[],
        assumptions=assumptions or [],
        source_map=source_map or [],
        compiler_version="2.0.0",
        prompt_hash="a" * 64,
    )


def _required(
    item_id: str,
    *,
    source_item_id: str = "REQ.alpha",
    source_level: str = "MUST",
    field_name: str = "email",
) -> Invariant:
    return Invariant(
        id=item_id,
        type=InvariantType.REQUIRED_FIELD,
        source_item_id=source_item_id,
        source_level=source_level,
        parameters=RequiredFieldParams(field_name=field_name),
    )


def test_quality_gate_merges_exact_duplicate_invariants_and_preserves_sources() -> None:
    """Exact same invariant semantics and provenance merge safely."""
    first = _required("INV-1111111111111111")
    duplicate = _required("INV-2222222222222222")
    success = _success(
        invariants=[first, duplicate],
        source_map=[
            SourceMapEntry(
                invariant_id=first.id,
                excerpt="Alpha requires email.",
                location="REQ.alpha.statement",
            ),
            SourceMapEntry(
                invariant_id=duplicate.id,
                excerpt="Email is required.",
                location="REQ.alpha.acceptance[0]",
            ),
        ],
    )

    gated = apply_authority_quality_gate(success)

    assert [invariant.id for invariant in gated.invariants] == [first.id]
    assert [entry.invariant_id for entry in gated.source_map] == [first.id, first.id]
    assert gated.authority_quality is not None
    assert gated.authority_quality.summary.merged_invariant_count == 1
    assert gated.authority_quality.merged_items[0].removed_ids == [duplicate.id]
    assert gated.authority_quality.merged_items[0].source_evidence_count == 2


def test_quality_gate_groups_same_shape_different_source_without_merging() -> None:
    """Same-shaped rules from different source items remain reviewable."""
    alpha = _required("INV-1111111111111111", source_item_id="REQ.alpha")
    beta = _required("INV-2222222222222222", source_item_id="REQ.beta")
    gated = apply_authority_quality_gate(_success(invariants=[alpha, beta]))

    assert [invariant.id for invariant in gated.invariants] == [alpha.id, beta.id]
    assert gated.authority_quality is not None
    groups = gated.authority_quality.review_groups
    assert any(group.group_type == "related_source_variants" for group in groups)


def test_quality_gate_groups_near_duplicate_invariants_without_merging() -> None:
    """High-overlap invariant text becomes a review group, not a merge."""
    first = Invariant(
        id="INV-1111111111111111",
        type=InvariantType.DATA_CONTRACT,
        source_item_id="REQ.alpha",
        source_level="MUST",
        parameters=DataContractParams(
            subject="profile",
            fields=["email", "name"],
            rule="profile record stores email and display name",
        ),
    )
    second = Invariant(
        id="INV-2222222222222222",
        type=InvariantType.DATA_CONTRACT,
        source_item_id="REQ.alpha",
        source_level="MUST",
        parameters=DataContractParams(
            subject="profile",
            fields=["email", "display_name"],
            rule="profile record persists email and display name",
        ),
    )

    gated = apply_authority_quality_gate(_success(invariants=[first, second]))

    assert len(gated.invariants) == 2
    assert gated.authority_quality is not None
    assert any(
        group.group_type == "near_duplicate_invariants"
        for group in gated.authority_quality.review_groups
    )


def test_quality_gate_groups_over_split_source_item() -> None:
    """Many invariants from one source item produce an over-split group."""
    invariants = [
        Invariant(
            id=f"INV-{index:016x}",
            type=InvariantType.STATE_TRANSITION,
            source_item_id="REQ.alpha",
            source_level="MUST",
            parameters=StateTransitionParams(
                state=f"step_{index}",
                trigger="input accepted",
                outcome=f"records step {index}",
            ),
        )
        for index in range(1, 6)
    ]

    gated = apply_authority_quality_gate(_success(invariants=invariants))

    assert len(gated.invariants) == 5
    assert gated.authority_quality is not None
    assert any(
        group.group_type == "over_split_invariants"
        for group in gated.authority_quality.review_groups
    )


def test_quality_gate_merges_exact_duplicate_assumptions_and_groups_noisy() -> None:
    """Assumption cleanup merges exact duplicates and groups high-overlap noise."""
    gated = apply_authority_quality_gate(
        _success(
            invariants=[],
            assumptions=[
                "Python runtime should be confirmed before implementation.",
                "python runtime should be confirmed before implementation",
                "Python runtime should be confirmed before implementation step.",
            ],
        )
    )

    assert gated.assumptions == [
        "Python runtime should be confirmed before implementation.",
        "Python runtime should be confirmed before implementation step.",
    ]
    assert gated.authority_quality is not None
    assert gated.authority_quality.summary.merged_assumption_count == 1
    assert any(
        group.group_type == "noisy_assumptions"
        for group in gated.authority_quality.review_groups
    )
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run --frozen pytest tests/test_authority_quality_gate.py -q
```

Expected: fail because `services.specs.authority_quality` does not exist.

- [ ] **Step 3: Implement quality gate module**

Create `services/specs/authority_quality.py`:

```python
"""Project-agnostic quality gate for compiled authority artifacts."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable

from utils.spec_schemas import (
    AuthorityQualityMergedItem,
    AuthorityQualityReport,
    AuthorityQualityReviewGroup,
    AuthorityQualitySummary,
    Invariant,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
)

NEAR_DUPLICATE_INVARIANT_THRESHOLD: float = 0.72
NOISY_ASSUMPTION_THRESHOLD: float = 0.82
OVER_SPLIT_SOURCE_ITEM_THRESHOLD: int = 5
OVER_SPLIT_SUBJECT_THRESHOLD: int = 3
MAX_REVIEW_GROUPS: int = 40
MAX_GROUP_MEMBERS: int = 12

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "of",
        "or",
        "the",
        "to",
        "with",
    }
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def apply_authority_quality_gate(
    success: SpecAuthorityCompilationSuccess,
) -> SpecAuthorityCompilationSuccess:
    """Return a copy with exact duplicates merged and quality report attached."""
    gated = success.model_copy(deep=True)
    original_invariant_count = len(gated.invariants)
    original_assumption_count = len(gated.assumptions)

    merged_items: list[AuthorityQualityMergedItem] = []
    review_groups: list[AuthorityQualityReviewGroup] = []

    _merge_exact_invariants(gated, merged_items)
    _merge_exact_assumptions(gated, merged_items)
    review_groups.extend(_related_source_variant_groups(gated.invariants))
    review_groups.extend(_near_duplicate_invariant_groups(gated.invariants))
    review_groups.extend(_over_split_groups(gated.invariants))
    review_groups.extend(_noisy_assumption_groups(gated.assumptions))
    review_groups = _dedupe_and_cap_groups(review_groups)

    summary = AuthorityQualitySummary(
        original_invariant_count=original_invariant_count,
        final_invariant_count=len(gated.invariants),
        merged_invariant_count=original_invariant_count - len(gated.invariants),
        merged_assumption_count=original_assumption_count - len(gated.assumptions),
        review_group_count=len(review_groups),
        near_duplicate_group_count=sum(
            1
            for group in review_groups
            if group.group_type == "near_duplicate_invariants"
        ),
        over_split_group_count=sum(
            1
            for group in review_groups
            if group.group_type == "over_split_invariants"
        ),
        noisy_assumption_group_count=sum(
            1
            for group in review_groups
            if group.group_type == "noisy_assumptions"
        ),
    )
    gated.authority_quality = AuthorityQualityReport(
        summary=summary,
        merged_items=merged_items,
        review_groups=review_groups,
    )
    return gated


def _invariant_exact_key(invariant: Invariant) -> tuple[str, str, str, str]:
    level_value = getattr(invariant.source_level, "value", invariant.source_level)
    return (
        invariant.type.value,
        json.dumps(
            invariant.parameters.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ),
        invariant.source_item_id or "",
        str(level_value or ""),
    )


def _merge_exact_invariants(
    success: SpecAuthorityCompilationSuccess,
    merged_items: list[AuthorityQualityMergedItem],
) -> None:
    seen: dict[tuple[str, str, str, str], Invariant] = {}
    removed_to_kept: dict[str, str] = {}
    kept: list[Invariant] = []
    removed_by_kept: dict[str, list[str]] = defaultdict(list)
    for invariant in success.invariants:
        key = _invariant_exact_key(invariant)
        existing = seen.get(key)
        if existing is None:
            seen[key] = invariant
            kept.append(invariant)
            continue
        removed_to_kept[invariant.id] = existing.id
        removed_by_kept[existing.id].append(invariant.id)
    if not removed_to_kept:
        return
    success.invariants = kept
    success.source_map = _remap_source_map(success.source_map, removed_to_kept)
    source_counts = _source_count_by_invariant(success.source_map)
    for index, (kept_id, removed_ids) in enumerate(removed_by_kept.items(), start=1):
        merged_items.append(
            AuthorityQualityMergedItem(
                merge_id=f"AQ-MERGE-{index:03d}",
                item_kind="invariant",
                kept_id=kept_id,
                removed_ids=removed_ids,
                reason="exact_semantic_duplicate",
                source_evidence_count=source_counts.get(kept_id, 0),
            )
        )


def _merge_exact_assumptions(
    success: SpecAuthorityCompilationSuccess,
    merged_items: list[AuthorityQualityMergedItem],
) -> None:
    seen: dict[str, int] = {}
    kept: list[str] = []
    removed_indexes_by_kept: dict[int, list[str]] = defaultdict(list)
    for index, assumption in enumerate(success.assumptions, start=1):
        key = _normalize_phrase(assumption)
        kept_index = seen.get(key)
        if kept_index is None:
            seen[key] = len(kept) + 1
            kept.append(assumption)
            continue
        removed_indexes_by_kept[kept_index].append(f"ASM-{index}")
    if not removed_indexes_by_kept:
        return
    success.assumptions = kept
    base = len(merged_items)
    for offset, (kept_index, removed_ids) in enumerate(
        removed_indexes_by_kept.items(),
        start=1,
    ):
        merged_items.append(
            AuthorityQualityMergedItem(
                merge_id=f"AQ-MERGE-{base + offset:03d}",
                item_kind="assumption",
                kept_id=f"ASM-{kept_index}",
                removed_ids=removed_ids,
                reason="exact_assumption_duplicate",
                source_evidence_count=0,
            )
        )


def _remap_source_map(
    entries: Iterable[SourceMapEntry],
    removed_to_kept: dict[str, str],
) -> list[SourceMapEntry]:
    seen: set[tuple[str, str, str | None]] = set()
    remapped: list[SourceMapEntry] = []
    for entry in entries:
        invariant_id = removed_to_kept.get(entry.invariant_id, entry.invariant_id)
        key = (invariant_id, entry.excerpt, entry.location)
        if key in seen:
            continue
        seen.add(key)
        remapped.append(
            SourceMapEntry(
                invariant_id=invariant_id,
                excerpt=entry.excerpt,
                location=entry.location,
            )
        )
    return remapped


def _source_count_by_invariant(entries: Iterable[SourceMapEntry]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for entry in entries:
        counts[entry.invariant_id] += 1
    return dict(counts)


def _related_source_variant_groups(
    invariants: list[Invariant],
) -> list[AuthorityQualityReviewGroup]:
    buckets: dict[tuple[str, str], list[Invariant]] = defaultdict(list)
    for invariant in invariants:
        params = json.dumps(
            invariant.parameters.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        buckets[(invariant.type.value, params)].append(invariant)
    groups: list[AuthorityQualityReviewGroup] = []
    for members in buckets.values():
        provenance_keys = {
            (
                member.source_item_id or "",
                str(getattr(member.source_level, "value", member.source_level) or ""),
            )
            for member in members
        }
        if len(members) > 1 and len(provenance_keys) > 1:
            groups.append(
                _group(
                    "related_source_variants",
                    members,
                    "same invariant shape appears under different source provenance",
                )
            )
    return groups


def _near_duplicate_invariant_groups(
    invariants: list[Invariant],
) -> list[AuthorityQualityReviewGroup]:
    buckets: dict[tuple[str, str, str, str], list[Invariant]] = defaultdict(list)
    for invariant in invariants:
        buckets[_near_duplicate_bucket(invariant)].append(invariant)
    groups: list[AuthorityQualityReviewGroup] = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        member_texts = {member.id: _invariant_text(member) for member in members}
        near_members: set[str] = set()
        for left_index, left in enumerate(members):
            for right in members[left_index + 1 :]:
                if (
                    _jaccard(member_texts[left.id], member_texts[right.id])
                    >= NEAR_DUPLICATE_INVARIANT_THRESHOLD
                ):
                    near_members.update({left.id, right.id})
        selected = [member for member in members if member.id in near_members]
        if len(selected) > 1:
            groups.append(
                _group(
                    "near_duplicate_invariants",
                    selected,
                    "high token overlap in same source/type bucket",
                )
            )
    return groups


def _near_duplicate_bucket(invariant: Invariant) -> tuple[str, str, str, str]:
    level_value = getattr(invariant.source_level, "value", invariant.source_level)
    return (
        invariant.type.value,
        invariant.source_item_id or "",
        str(level_value or ""),
        _subject_like_parameter(invariant),
    )


def _over_split_groups(invariants: list[Invariant]) -> list[AuthorityQualityReviewGroup]:
    groups: list[AuthorityQualityReviewGroup] = []
    by_source: dict[str, list[Invariant]] = defaultdict(list)
    by_subject: dict[tuple[str, str, str], list[Invariant]] = defaultdict(list)
    for invariant in invariants:
        if not invariant.source_item_id:
            continue
        by_source[invariant.source_item_id].append(invariant)
        by_subject[
            (
                invariant.source_item_id,
                invariant.type.value,
                _subject_like_parameter(invariant),
            )
        ].append(invariant)
    for members in by_source.values():
        if len(members) >= OVER_SPLIT_SOURCE_ITEM_THRESHOLD:
            groups.append(
                _group(
                    "over_split_invariants",
                    members,
                    "one source item produced many invariants",
                )
            )
    for members in by_subject.values():
        if len(members) >= OVER_SPLIT_SUBJECT_THRESHOLD:
            groups.append(
                _group(
                    "over_split_invariants",
                    members,
                    "same source/type/subject cluster produced many invariants",
                )
            )
    return groups


def _noisy_assumption_groups(assumptions: list[str]) -> list[AuthorityQualityReviewGroup]:
    members = [f"ASM-{index}" for index in range(1, len(assumptions) + 1)]
    selected: set[str] = set()
    for left_index, left in enumerate(assumptions):
        for right_index, right in enumerate(
            assumptions[left_index + 1 :],
            start=left_index + 2,
        ):
            if _jaccard(left, right) >= NOISY_ASSUMPTION_THRESHOLD:
                selected.update({f"ASM-{left_index + 1}", f"ASM-{right_index}"})
    if len(selected) < 2:
        return []
    ordered = [member for member in members if member in selected]
    return [
        AuthorityQualityReviewGroup(
            group_id="AQ-GROUP-001",
            group_type="noisy_assumptions",
            severity="warning",
            member_ids=ordered[:MAX_GROUP_MEMBERS],
            reason="compiler assumptions have high token overlap",
            merge_allowed=False,
            truncated=len(ordered) > MAX_GROUP_MEMBERS,
        )
    ]


def _group(
    group_type: str,
    members: list[Invariant],
    reason: str,
) -> AuthorityQualityReviewGroup:
    return AuthorityQualityReviewGroup(
        group_id="AQ-GROUP-000",
        group_type=group_type,
        severity="warning",
        member_ids=[member.id for member in members[:MAX_GROUP_MEMBERS]],
        reason=reason,
        merge_allowed=False,
        truncated=len(members) > MAX_GROUP_MEMBERS,
    )


def _dedupe_and_cap_groups(
    groups: list[AuthorityQualityReviewGroup],
) -> list[AuthorityQualityReviewGroup]:
    seen: set[tuple[str, tuple[str, ...]]] = set()
    deduped: list[AuthorityQualityReviewGroup] = []
    for group in groups:
        key = (group.group_type, tuple(group.member_ids))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(group)
        if len(deduped) >= MAX_REVIEW_GROUPS:
            break
    return [
        group.model_copy(update={"group_id": f"AQ-GROUP-{index:03d}"})
        for index, group in enumerate(deduped, start=1)
    ]


def _subject_like_parameter(invariant: Invariant) -> str:
    dumped = invariant.parameters.model_dump(mode="json")
    for key in ("subject", "field_name", "state", "route", "target", "capability"):
        value = dumped.get(key)
        if isinstance(value, str) and value:
            return _normalize_phrase(value)
    return ""


def _invariant_text(invariant: Invariant) -> str:
    dumped = invariant.parameters.model_dump(mode="json")
    parts = [invariant.type.value]
    for key in sorted(dumped):
        value = dumped[key]
        if isinstance(value, list):
            parts.append(" ".join(str(item) for item in value))
        else:
            parts.append(str(value))
    return " ".join(parts)


def _normalize_phrase(text: str) -> str:
    return " ".join(_tokens(text))


def _tokens(text: str) -> list[str]:
    return [
        token
        for token in _TOKEN_RE.findall(text.casefold())
        if token and token not in _STOPWORDS
    ]


def _jaccard(left: str, right: str) -> float:
    left_tokens = set(_tokens(left))
    right_tokens = set(_tokens(right))
    if not left_tokens and not right_tokens:
        return 1.0
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
```

- [ ] **Step 4: Run quality gate tests**

Run:

```bash
uv run --frozen pytest tests/test_authority_quality_gate.py -q
```

Expected: pass.

- [ ] **Step 5: Commit quality gate unit**

```bash
git add services/specs/authority_quality.py tests/test_authority_quality_gate.py
git commit -m "feat: add authority quality gate"
```

### Task 4: Compiler Persistence Hook

**Files:**
- Modify: `services/specs/compiler_service.py`
- Test: `tests/test_specs_compiler_service.py`

- [ ] **Step 1: Write failing compiler persistence test**

Append this test to `tests/test_specs_compiler_service.py` near other
`compile_spec_authority_for_version` persistence tests:

```python
def test_compile_spec_authority_for_version_persists_quality_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Compilation applies authority quality gate before persistence."""
    from services.specs import compiler_service  # noqa: PLC0415

    engine = create_engine(
        f"sqlite:///{tmp_path / 'business.sqlite3'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    ensure_schema_current(engine)
    with Session(engine) as session:
        product = Product(name="Quality Gate Project")
        session.add(product)
        session.commit()
        session.refresh(product)
        spec = SpecRegistry(
            product_id=require_id(product.product_id, "product_id"),
            spec_hash="sha256:" + "1" * 64,
            content='{"format":"agileforge.spec.v1","items":[]}',
            content_ref="specs/spec.json",
            status="approved",
            approved_at=datetime.now(UTC),
            approved_by="test",
        )
        session.add(spec)
        session.commit()
        session.refresh(spec)
        spec_version_id = require_id(spec.spec_version_id, "spec_version_id")

    def fake_extract(**_: object) -> SpecAuthorityCompilationSuccess:
        return SpecAuthorityCompilationSuccess(
            scope_themes=["Quality"],
            domain=None,
            invariants=[
                Invariant(
                    id="INV-1111111111111111",
                    type=InvariantType.REQUIRED_FIELD,
                    source_item_id="REQ.alpha",
                    source_level="MUST",
                    parameters=RequiredFieldParams(field_name="email"),
                ),
                Invariant(
                    id="INV-2222222222222222",
                    type=InvariantType.REQUIRED_FIELD,
                    source_item_id="REQ.alpha",
                    source_level="MUST",
                    parameters=RequiredFieldParams(field_name="email"),
                ),
            ],
            eligible_feature_rules=[],
            rejected_features=[],
            gaps=[],
            assumptions=[],
            source_map=[],
            compiler_version="2.0.0",
            prompt_hash="a" * 64,
        )

    monkeypatch.setattr(compiler_service, "_extract_spec_authority_llm", fake_extract)

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        spec_version_id=spec_version_id,
        force_recompile=False,
        engine=engine,
    )

    assert result["success"] is True
    with Session(engine) as session:
        authority = session.exec(
            select(CompiledSpecAuthority).where(
                CompiledSpecAuthority.spec_version_id == spec_version_id
            )
        ).one()
        assert authority.compiled_artifact_json is not None
        artifact = json.loads(authority.compiled_artifact_json)
    assert artifact["authority_quality"]["summary"]["merged_invariant_count"] == 1
    assert len(artifact["invariants"]) == 1
```

If `require_id` is not imported in this file, add:

```python
from tests.typing_helpers import require_id
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py::test_compile_spec_authority_for_version_persists_quality_report -q
```

Expected: fail because `authority_quality` is not persisted.

- [ ] **Step 3: Apply gate in compiler service**

In `services/specs/compiler_service.py`, add import:

```python
from services.specs.authority_quality import apply_authority_quality_gate
```

In `compile_spec_authority()`, immediately after extractor success and before
`prompt_hash = ...`, add:

```python
        success_artifact = apply_authority_quality_gate(success_artifact)
```

In `_persist_compiled_authority()`, as the first statement after the docstring,
add:

```python
    success = apply_authority_quality_gate(success)
```

- [ ] **Step 4: Verify compiler persistence test passes**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py::test_compile_spec_authority_for_version_persists_quality_report -q
```

Expected: pass.

- [ ] **Step 5: Run nearby compiler tests**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py::test_compile_spec_authority_for_version_persists_authority tests/test_specs_compiler_service.py::test_compile_spec_authority_for_version_returns_cached_authority tests/test_specs_compiler_service.py::test_compile_spec_authority_for_version_persists_quality_report -q
```

Expected: pass.

- [ ] **Step 6: Commit compiler hook**

```bash
git add services/specs/compiler_service.py tests/test_specs_compiler_service.py
git commit -m "feat: persist authority quality report"
```

### Task 5: Review Packet Projection

**Files:**
- Modify: `services/agent_workbench/authority_review.py`
- Test: `tests/test_agent_workbench_authority_review.py`

- [ ] **Step 1: Write failing review projection test**

Append this test to `tests/test_agent_workbench_authority_review.py`:

```python
def test_authority_review_packet_exposes_authority_quality(
    session: Session,
    tmp_path: Path,
) -> None:
    """Review packet includes persisted authority quality report."""
    project_id, _spec_version_id, authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=_base_spec(),
        )
    )
    authority = session.get(CompiledSpecAuthority, authority_id)
    assert authority is not None
    artifact = json.loads(authority.compiled_artifact_json or "{}")
    artifact["authority_quality"] = {
        "schema_version": "agileforge.authority_quality.v1",
        "summary": {
            "original_invariant_count": 2,
            "final_invariant_count": 1,
            "merged_invariant_count": 1,
            "merged_assumption_count": 0,
            "review_group_count": 1,
            "near_duplicate_group_count": 0,
            "over_split_group_count": 1,
            "noisy_assumption_group_count": 0,
        },
        "merged_items": [
            {
                "merge_id": "AQ-MERGE-001",
                "item_kind": "invariant",
                "kept_id": "INV-0123456789abcdef",
                "removed_ids": ["INV-2222222222222222"],
                "reason": "exact_semantic_duplicate",
                "source_evidence_count": 2,
            }
        ],
        "review_groups": [
            {
                "group_id": "AQ-GROUP-001",
                "group_type": "over_split_invariants",
                "severity": "warning",
                "member_ids": ["INV-0123456789abcdef"],
                "reason": "one source item produced many invariants",
                "merge_allowed": False,
                "truncated": False,
            }
        ],
    }
    authority.compiled_artifact_json = json.dumps(artifact)
    session.add(authority)
    session.commit()

    service = AuthorityReviewService(engine=_engine(session))
    result = service.review(project_id=project_id)

    assert result["ok"] is True
    pending = result["data"]["pending_authority"]
    quality = pending["artifact"]["authority_quality"]
    assert quality["summary"]["merged_invariant_count"] == 1
    assert quality["review_groups"][0]["group_type"] == "over_split_invariants"
    assert pending["review_summary"]["quality_review_group_count"] == 1
    assert pending["review_summary"]["quality_merged_invariant_count"] == 1
```

Use the same `session` fixture and `_engine(session)` helper already used by
nearby tests in this file.

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_review.py::test_authority_review_packet_exposes_authority_quality -q
```

Expected: fail because artifact payload drops `authority_quality`.

- [ ] **Step 3: Add quality payload to authority review**

In `_authority_artifact_payload()`, after building `source_map_by_id`, add:

```python
    authority_quality = (
        artifact.authority_quality.model_dump(mode="json")
        if artifact.authority_quality is not None
        else None
    )
```

In the returned artifact dict, add:

```python
            "authority_quality": authority_quality,
```

In `_fallback_authority_artifact()`, add:

```python
        "authority_quality": None,
```

In `_review_summary()`, add:

```python
    quality = artifact.get("authority_quality")
    quality_summary = (
        quality.get("summary")
        if isinstance(quality, Mapping) and isinstance(quality.get("summary"), Mapping)
        else {}
    )
```

Then add these fields to the returned summary:

```python
        "quality_merged_invariant_count": int(
            quality_summary.get("merged_invariant_count") or 0
        ),
        "quality_merged_assumption_count": int(
            quality_summary.get("merged_assumption_count") or 0
        ),
        "quality_review_group_count": int(
            quality_summary.get("review_group_count") or 0
        ),
        "quality_near_duplicate_group_count": int(
            quality_summary.get("near_duplicate_group_count") or 0
        ),
        "quality_over_split_group_count": int(
            quality_summary.get("over_split_group_count") or 0
        ),
        "quality_noisy_assumption_group_count": int(
            quality_summary.get("noisy_assumption_group_count") or 0
        ),
```

- [ ] **Step 4: Verify review projection test passes**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_review.py::test_authority_review_packet_exposes_authority_quality -q
```

Expected: pass.

- [ ] **Step 5: Commit review packet task**

```bash
git add services/agent_workbench/authority_review.py tests/test_agent_workbench_authority_review.py
git commit -m "feat: expose authority quality in review packet"
```

### Task 6: Dashboard Quality Section

**Files:**
- Modify: `frontend/project.html`
- Modify: `frontend/project.js`
- Test: `tests/test_authority_review_console.mjs`

- [ ] **Step 1: Write failing console test**

In `tests/test_authority_review_console.mjs`, add quality data to an existing
authority review fixture and assert rendered text. Use this assertion pattern:

```javascript
assert.match(html, /Authority Quality/i);
assert.match(html, /1 merged invariant/i);
assert.match(html, /over_split_invariants/i);
```

If the file uses DOM assertions instead of HTML string assertions, assert:

```javascript
assert.equal(
  document.querySelector('#authority-quality-summary')?.textContent.includes('1 merged invariant'),
  true,
);
assert.equal(
  document.querySelector('#authority-quality-groups-list')?.textContent.includes('over_split_invariants'),
  true,
);
```

- [ ] **Step 2: Run console test to verify failure**

Run:

```bash
node tests/test_authority_review_console.mjs
```

Expected: fail because no quality section exists.

- [ ] **Step 3: Add quality containers to HTML**

In `frontend/project.html`, in the authority overview tab after the metric band
or before review findings, add:

```html
<section class="space-y-2 lg:col-span-2">
    <h5 class="text-xs uppercase tracking-wider font-bold text-cyan-600 dark:text-cyan-400">Authority Quality</h5>
    <div id="authority-quality-summary" class="text-xs font-semibold text-slate-600 dark:text-slate-300"></div>
    <div id="authority-quality-groups-list" class="grid grid-cols-1 gap-2 lg:grid-cols-2"></div>
</section>
```

- [ ] **Step 4: Render quality summary and groups**

In `frontend/project.js`, add helper:

```javascript
function renderAuthorityQuality(artifact) {
    const summary = document.getElementById('authority-quality-summary');
    const groupsList = document.getElementById('authority-quality-groups-list');
    if (!summary || !groupsList) return;

    const quality = artifact?.authority_quality;
    const qualitySummary = quality?.summary || {};
    const mergedInvariants = Number(qualitySummary.merged_invariant_count || 0);
    const mergedAssumptions = Number(qualitySummary.merged_assumption_count || 0);
    const groupCount = Number(qualitySummary.review_group_count || 0);
    const invariantLabel = mergedInvariants === 1 ? 'invariant' : 'invariants';
    const assumptionLabel = mergedAssumptions === 1 ? 'assumption' : 'assumptions';
    const groupLabel = groupCount === 1 ? 'group' : 'groups';
    summary.textContent = `${mergedInvariants} merged ${invariantLabel}, ${mergedAssumptions} merged ${assumptionLabel}, ${groupCount} review ${groupLabel}`;

    const groups = safeArray(quality?.review_groups);
    if (!groups.length) {
        groupsList.replaceChildren(createEmptyState('No authority quality groups.'));
        return;
    }

    groupsList.replaceChildren(...groups.map((group) => {
        const card = document.createElement('div');
        card.className = 'rounded-lg border border-cyan-200 bg-cyan-50/70 p-3 text-xs text-cyan-950 dark:border-cyan-900 dark:bg-cyan-950/30 dark:text-cyan-100';
        const title = document.createElement('div');
        title.className = 'font-bold';
        title.textContent = `${group.group_id || 'AQ-GROUP'} ${group.group_type || 'quality_group'}`;
        const reason = document.createElement('div');
        reason.className = 'mt-1 text-cyan-800 dark:text-cyan-200';
        reason.textContent = group.reason || '';
        const members = document.createElement('div');
        members.className = 'mt-2 font-mono text-[11px] text-cyan-700 dark:text-cyan-300';
        members.textContent = safeArray(group.member_ids).join(', ');
        card.replaceChildren(title, reason, members);
        return card;
    }));
}
```

In `renderAuthorityOverview(review)`, after `const artifact = pending.artifact || {};`,
call:

```javascript
    renderAuthorityQuality(artifact);
```

- [ ] **Step 5: Verify console test passes**

Run:

```bash
node tests/test_authority_review_console.mjs
```

Expected: pass.

- [ ] **Step 6: Commit dashboard task**

```bash
git add frontend/project.html frontend/project.js tests/test_authority_review_console.mjs
git commit -m "feat: show authority quality groups"
```

### Task 7: Project-Agnostic Guardrail And ASA Regression

**Files:**
- Test: `tests/test_authority_quality_gate.py`
- Runtime verification only: `/Users/aaat/projects/asa-deep-process-control-experiments`

- [ ] **Step 1: Add source-search guard test**

Append to `tests/test_authority_quality_gate.py`:

```python
def test_authority_quality_gate_has_no_project_specific_terms() -> None:
    """Gate implementation must stay project-agnostic."""
    from pathlib import Path

    implementation = Path("services/specs/authority_quality.py").read_text()
    forbidden_terms = [
        "ASA",
        "Deep Process",
        "REQ.project-scaffold",
        "DDPG",
        "pyrometer",
        "TemperatureTargets",
        "stainless",
        "annealing",
        "pickling",
    ]
    offenders = [term for term in forbidden_terms if term in implementation]
    assert offenders == []
```

- [ ] **Step 2: Run project-agnostic guard test**

Run:

```bash
uv run --frozen pytest tests/test_authority_quality_gate.py::test_authority_quality_gate_has_no_project_specific_terms -q
```

Expected: pass.

- [ ] **Step 3: Run focused Python suite**

Run:

```bash
uv run --frozen pytest tests/test_authority_quality_gate.py tests/test_spec_schema_modules.py tests/test_spec_authority_compiler_normalizer.py tests/test_specs_compiler_service.py tests/test_agent_workbench_authority_review.py -q
```

Expected: pass.

- [ ] **Step 4: Run Ruff on changed Python files**

Run:

```bash
uv run --frozen ruff check utils/spec_schemas.py utils/schemes.py orchestrator_agent/agent_tools/spec_authority_compiler_agent/compiler_contract.py orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py services/specs/authority_quality.py services/specs/compiler_service.py services/agent_workbench/authority_review.py tests/test_authority_quality_gate.py tests/test_spec_schema_modules.py tests/test_spec_authority_compiler_normalizer.py tests/test_specs_compiler_service.py tests/test_agent_workbench_authority_review.py
```

Expected: pass.

- [ ] **Step 5: Run ASA runtime regression with normal DB only after tests pass**

From `/Users/aaat/projects/asa-deep-process-control-experiments`, use the
working-tree runner:

```bash
AF=(uv run --project /Users/aaat/projects/agileforge --frozen python -m cli.main)
tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/asa-authority-quality.XXXXXX")"
"${AF[@]}" authority regenerate \
  --project-id 3 \
  --spec-version-id 3 \
  --idempotency-key "regen-asa-authority-quality-$(date -u +%Y%m%dT%H%M%SZ)" \
  >"$tmp_dir/regenerate.json" 2>"$tmp_dir/regenerate.err"
```

If project `3` or spec version `3` has changed, stop and refresh with bounded
`status`/`authority status` summaries before choosing IDs. Do not guess.

- [ ] **Step 6: Summarize ASA regeneration safely**

Run from `/Users/aaat/projects/agileforge`:

```bash
uv run --frozen python - "$tmp_dir/regenerate.json" "$tmp_dir/regenerate.err" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
stderr = Path(sys.argv[2]).read_text(errors="replace")
print("ok", payload.get("ok"))
data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
print("project_id", data.get("project_id"))
print("authority_id", data.get("authority_id"))
print("setup_status", data.get("setup_status"))
errors = payload.get("errors") or []
if errors:
    first = errors[0]
    print("first_error_code", first.get("code"))
    print("first_error_message", first.get("message"))
print("stderr_bytes", len(stderr.encode()))
PY
```

Expected: `ok True`. If it fails, stop and collect only first error and failure
artifact path.

- [ ] **Step 7: Confirm pending review and quality report**

Run from `/Users/aaat/projects/asa-deep-process-control-experiments`:

```bash
"${AF[@]}" authority review --project-id 3 >"$tmp_dir/review.json" 2>"$tmp_dir/review.err"
```

Summarize:

```bash
uv run --project /Users/aaat/projects/agileforge --frozen python - "$tmp_dir/review.json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
data = payload.get("data", {})
pending = data.get("pending_authority", {}) if isinstance(data, dict) else {}
artifact = pending.get("artifact", {}) if isinstance(pending, dict) else {}
quality = artifact.get("authority_quality") if isinstance(artifact, dict) else None
summary = quality.get("summary") if isinstance(quality, dict) else {}
groups = quality.get("review_groups") if isinstance(quality, dict) else []
print("ok", payload.get("ok"))
print("authority_status", (pending.get("review_summary") or {}).get("acceptance_status"))
print("invariants_count", len(artifact.get("invariants") or []))
print("quality_present", isinstance(quality, dict))
print("merged_invariant_count", summary.get("merged_invariant_count"))
print("review_group_count", summary.get("review_group_count"))
print("group_types", sorted({group.get("group_type") for group in groups if isinstance(group, dict)}))
PY
```

Expected:

- `ok True`
- authority still pending review / accept-ready unless existing structural
  blockers are present
- `quality_present True`
- `review_group_count` is present
- `group_types` includes generic group types only

Do not run `authority accept`, `authority reject`, Vision, Backlog, Roadmap,
Story, or Sprint.

- [ ] **Step 8: Commit guardrail/regression task**

```bash
git add tests/test_authority_quality_gate.py
git commit -m "test: guard authority quality project agnostic behavior"
```

### Task 8: Final Verification

**Files:**
- Verify all changed files.

- [ ] **Step 1: Inspect changed files**

Run:

```bash
git status --short --branch
git diff --stat origin/master..HEAD
```

Expected: only files from this plan are changed/committed.

- [ ] **Step 2: Run final focused checks**

Run:

```bash
uv run --frozen ruff check utils/spec_schemas.py utils/schemes.py orchestrator_agent/agent_tools/spec_authority_compiler_agent/compiler_contract.py orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py services/specs/authority_quality.py services/specs/compiler_service.py services/agent_workbench/authority_review.py tests/test_authority_quality_gate.py tests/test_spec_schema_modules.py tests/test_spec_authority_compiler_normalizer.py tests/test_specs_compiler_service.py tests/test_agent_workbench_authority_review.py
uv run --frozen pytest tests/test_authority_quality_gate.py tests/test_spec_schema_modules.py tests/test_spec_authority_compiler_normalizer.py tests/test_specs_compiler_service.py tests/test_agent_workbench_authority_review.py -q
node tests/test_authority_review_console.mjs
git diff --check origin/master..HEAD
```

Expected: all pass.

- [ ] **Step 3: Summarize completion**

Report:

- commits created
- tests run
- ASA regression result
- explicit confirmation that no accept/reject or post-setup workflow phase ran
- whether `master` is ahead of `origin/master`
