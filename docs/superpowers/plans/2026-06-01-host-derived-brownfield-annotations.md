# Host-Derived Brownfield Annotations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace preview-time brownfield equality validation with host-derived As-Built annotations, structured non-blocking preview warnings, and a tiny save-time annotated-artifact gate.

**Architecture:** Backlog Primer proposes backlog candidates and may emit only `authority_ref` plus optional `capability_hint`. `services/backlog_runtime.py` derives `as_built_annotation` from the authoritative As-Built cache after model output validation. Preview/generate return success with `brownfield_warnings[]`; save validates the fingerprinted annotated artifact without re-deriving annotations.

**Tech Stack:** Python 3.13, Pydantic v2, existing ADK runner wrapper, pytest, AgileForge workflow-state Backlog phase services.

---

## File Map

- Create `utils/brownfield_annotations.py`
  - Shared Pydantic schemas/enums for annotations and warnings.
- Modify `orchestrator_agent/agent_tools/backlog_primer/schemes.py`
  - Remove model-owned `capability_name`, `as_built_status`, and `recommended_backlog_treatment` from the output contract.
  - Add `capability_hint`.
  - Allow host-owned `as_built_annotation`.
- Modify `orchestrator_agent/agent_tools/backlog_primer/instructions.txt`
  - Tell Backlog Primer not to emit host-owned As-Built metadata.
  - Keep `authority_ref` and optional `capability_hint`.
- Modify `orchestrator_agent/agent_tools/roadmap_builder/schemes.py`
  - Preserve Roadmap compatibility with annotated backlog items because
    `services/roadmap_runtime.py` passes `state["backlog_items"]` into
    `RoadmapBuilderInput` and the nested schema uses `extra="forbid"`.
- Modify `services/backlog_runtime.py`
  - Replace preview equality validation with `derive_brownfield_annotations`.
  - Remove brownfield retry from the preview/generate path.
  - Attach `as_built_annotation` and `brownfield_warnings` before computing
    output artifact.
- Modify `services/phases/backlog_service.py`
  - Strengthen the existing artifact fingerprint guard by recomputing the normal
    backlog artifact fingerprint from the persisted annotated artifact at save.
  - Add save gate that blocks only the closed warning list.
- Modify `services/phases/workflow_state.py`
  - Deep-copy recorded phase artifacts and mirrored workflow-state fields so
    reviewed attempts do not share mutable identity with convenience state.
- Modify `tests/test_agent_workbench_backlog_phase.py`
  - Rewrite preview fail-closed tests into warning tests.
- Modify `tests/test_backlog_phase_service.py`
  - Add save-gate, fingerprint, and greenfield save round-trip tests.
- Modify `tests/test_phase_workflow_state.py`
  - Add a cross-phase mutation-isolation test for shared `record_phase_attempt`.
- Modify `tests/test_backlog_primer_prompt_contract.py`
  - Remove prompt assertions requiring model-authored `capability_name`,
    `as_built_status`, and `recommended_backlog_treatment`.
  - Add assertions for host-owned annotation boundary.
- Modify `tests/test_roadmap_builder_schemas.py`
  - Prove Roadmap Builder validates annotated backlog items end to end.

## Task 1: Add Brownfield Annotation Schemas

**Files:**
- Create: `utils/brownfield_annotations.py`
- Test: `tests/test_agent_workbench_backlog_phase.py`

- [ ] **Step 1: Write failing schema/import test**

Add this test near the brownfield tests in
`tests/test_agent_workbench_backlog_phase.py`:

```python
def test_brownfield_annotation_schema_represents_dual_provenance() -> None:
    """Annotation schema must preserve host and model values side by side."""
    from utils.brownfield_annotations import (
        BrownfieldAnnotation,
        BrownfieldDisagreement,
        BrownfieldModelAssertion,
        BrownfieldSelectedCapability,
    )

    annotation = BrownfieldAnnotation.model_validate(
        {
            "schema_version": "agileforge.brownfield_annotation.v1",
            "source": "host_derived",
            "match_tier": "exact",
            "match_basis": ["authority_ref"],
            "conflict": False,
            "selected": {
                "authority_ref": "QUALITY.security-secrets",
                "capability_title": "Security Secrets",
                "invariant_refs": ["INV-506454637a21ed73"],
                "as_built_status": "not_observed",
                "recommended_backlog_treatment": "create_discovery_item",
                "confidence": "medium",
            },
            "candidates": [],
            "model_assertion": {
                "source": "model_asserted",
                "authority_ref": "QUALITY.security-secrets",
                "capability_hint": "secrets protection",
                "as_built_status": "observed",
                "recommended_backlog_treatment": "skip_new_implementation",
            },
            "disagreements": [
                {
                    "field": "as_built_status",
                    "model_value": "observed",
                    "host_value": "not_observed",
                    "code": "status_disagreement",
                }
            ],
            "warning_codes": ["status_disagreement"],
        }
    )

    assert isinstance(annotation.selected, BrownfieldSelectedCapability)
    assert isinstance(annotation.model_assertion, BrownfieldModelAssertion)
    assert isinstance(annotation.disagreements[0], BrownfieldDisagreement)
    assert annotation.warning_codes == ["status_disagreement"]
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_backlog_phase.py::test_brownfield_annotation_schema_represents_dual_provenance -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'utils.brownfield_annotations'`.

- [ ] **Step 3: Create annotation schema module**

Create `utils/brownfield_annotations.py`:

```python
"""Host-derived brownfield annotation schemas."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestrator_agent.agent_tools.as_built_assessor.schemes import (
    AssessmentConfidence,
    AssessmentStatus,
    BacklogTreatment,
)

BrownfieldMatchTier = Literal["exact", "fuzzy", "none"]
BrownfieldAnnotationSource = Literal["host_derived", "model_asserted"]
BrownfieldWarningCode = Literal[
    "metadata_filled_by_host",
    "possible_mapping",
    "looks_mapped_but_unmatched",
    "conflicting_invariants",
    "status_disagreement",
    "treatment_disagreement",
    "capability_disagreement",
    "asserted_authority_ref_unmatched",
]
BrownfieldDisagreementCode = Literal[
    "status_disagreement",
    "treatment_disagreement",
    "capability_disagreement",
]
BrownfieldWarningSeverity = Literal["info", "review", "block_on_save"]


class BrownfieldSelectedCapability(BaseModel):
    """A host-selected As-Built capability contract."""

    model_config = ConfigDict(extra="forbid")

    authority_ref: Annotated[str, Field(min_length=1)]
    capability_title: Annotated[str, Field(min_length=1)]
    invariant_refs: list[str] = Field(default_factory=list)
    as_built_status: AssessmentStatus
    recommended_backlog_treatment: BacklogTreatment
    confidence: AssessmentConfidence


class BrownfieldModelAssertion(BaseModel):
    """Raw model-provided brownfield hints preserved for provenance."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["model_asserted"] = "model_asserted"
    authority_ref: str | None = None
    capability_hint: str | None = None
    as_built_status: AssessmentStatus | None = None
    recommended_backlog_treatment: BacklogTreatment | None = None


class BrownfieldDisagreement(BaseModel):
    """A structured disagreement between model assertion and host annotation."""

    model_config = ConfigDict(extra="forbid")

    field: Annotated[str, Field(min_length=1)]
    model_value: str | None
    host_value: str | None
    code: BrownfieldDisagreementCode


class BrownfieldAnnotation(BaseModel):
    """Host-derived annotation attached to one backlog item."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agileforge.brownfield_annotation.v1"]
    source: Literal["host_derived"] = "host_derived"
    match_tier: BrownfieldMatchTier
    match_basis: list[str] = Field(default_factory=list)
    conflict: bool = False
    selected: BrownfieldSelectedCapability | None = None
    candidates: list[BrownfieldSelectedCapability] = Field(default_factory=list)
    model_assertion: BrownfieldModelAssertion = Field(
        default_factory=BrownfieldModelAssertion
    )
    disagreements: list[BrownfieldDisagreement] = Field(default_factory=list)
    warning_codes: list[BrownfieldWarningCode] = Field(default_factory=list)


class BrownfieldWarning(BaseModel):
    """Structured warning emitted during host annotation."""

    model_config = ConfigDict(extra="forbid")

    code: BrownfieldWarningCode
    item_index: Annotated[int, Field(ge=0)] | None = None
    severity: BrownfieldWarningSeverity = "review"
    match_tier: BrownfieldMatchTier
    authority_ref: str | None = None
    invariant_refs: list[str] = Field(default_factory=list)
    message: Annotated[str, Field(min_length=1)]
    details: dict[str, object] = Field(default_factory=dict)
```

`item_index` is zero-based and refers to `backlog_items[]`.

- [ ] **Step 4: Run schema test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_backlog_phase.py::test_brownfield_annotation_schema_represents_dual_provenance -q
```

Expected: PASS.

## Task 2: Change Backlog/Roadmap Item Schema Boundary

**Files:**
- Modify: `orchestrator_agent/agent_tools/backlog_primer/schemes.py`
- Modify: `orchestrator_agent/agent_tools/roadmap_builder/schemes.py`
- Modify: `tests/test_backlog_primer_agent.py`
- Modify: `tests/test_roadmap_builder_schemas.py`

- [ ] **Step 1: Write failing Backlog schema test**

Add/replace a schema test so the model-owned fields are gone and
host-owned annotation is accepted:

```python
def test_output_schema_accepts_host_annotation_and_capability_hint() -> None:
    payload = {
        "backlog_items": [
            {
                "priority": 1,
                "requirement": "Validate Captain-Aware Optimizer Contract",
                "authority_ref": "REQ.captain-aware-optimization",
                "capability_hint": "captain optimizer",
                "as_built_annotation": {
                    "schema_version": "agileforge.brownfield_annotation.v1",
                    "source": "host_derived",
                    "match_tier": "exact",
                    "match_basis": ["authority_ref"],
                    "conflict": False,
                    "selected": None,
                    "candidates": [],
                    "model_assertion": {
                        "source": "model_asserted",
                        "authority_ref": "REQ.captain-aware-optimization",
                        "capability_hint": "captain optimizer",
                        "as_built_status": None,
                        "recommended_backlog_treatment": None,
                    },
                    "disagreements": [],
                    "warning_codes": [],
                },
                "value_driver": "Strategic",
                "justification": "Validate current behavior.",
                "estimated_effort": "M",
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }

    parsed = OutputSchema.model_validate(payload)

    item = parsed.backlog_items[0]
    assert item.capability_hint == "captain optimizer"
    assert item.as_built_annotation is not None
```

- [ ] **Step 2: Write failing legacy-field rejection test**

```python
def test_output_schema_rejects_model_owned_brownfield_fields() -> None:
    payload = {
        "backlog_items": [
            {
                "priority": 1,
                "requirement": "Verify Live Squad",
                "capability_name": "Live Squad Recommendation",
                "as_built_status": "observed",
                "recommended_backlog_treatment": "skip_new_implementation",
                "value_driver": "Strategic",
                "justification": "Old contract should be removed.",
                "estimated_effort": "S",
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }

    with pytest.raises(ValidationError):
        OutputSchema.model_validate(payload)
```

- [ ] **Step 3: Update Backlog Primer `BacklogItem`**

In `orchestrator_agent/agent_tools/backlog_primer/schemes.py`:

- remove `capability_name`;
- remove `as_built_status`;
- remove `recommended_backlog_treatment`;
- keep `authority_ref`;
- add `capability_hint`;
- add `as_built_annotation`.

Use:

```python
from utils.brownfield_annotations import BrownfieldAnnotation
```

and fields:

```python
    capability_hint: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional model-authored brownfield hint. This is advisory only; "
                "the host derives authoritative annotation."
            ),
        ),
    ]
    as_built_annotation: Annotated[
        BrownfieldAnnotation | None,
        Field(
            default=None,
            description="Host-derived brownfield annotation. Models should omit it.",
        ),
    ]
```

- [ ] **Step 4: Preserve Roadmap Builder compatibility**

`services/roadmap_runtime.py` passes `state["backlog_items"]` directly into
`RoadmapBuilderInput`. Update
`orchestrator_agent/agent_tools/roadmap_builder/schemes.py` so annotated backlog
items do not fail `extra="forbid"`.

Use the same `BrownfieldAnnotation` import as Backlog Primer. Do not re-create
annotation schemas inside the Roadmap Builder module.

- [ ] **Step 5: Write Roadmap input round-trip test**

In `tests/test_roadmap_builder_schemas.py`, add a test that validates a full
`RoadmapBuilderInput` with an annotated backlog item:

```python
def test_roadmap_input_accepts_annotated_backlog_item() -> None:
    payload = {
        "backlog_items": [
            {
                "priority": 1,
                "requirement": "Validate Captain-Aware Optimizer Contract",
                "authority_ref": "REQ.captain-aware-optimization",
                "capability_hint": "captain optimizer",
                "as_built_annotation": {
                    "schema_version": "agileforge.brownfield_annotation.v1",
                    "source": "host_derived",
                    "match_tier": "exact",
                    "match_basis": ["authority_ref"],
                    "conflict": False,
                    "selected": None,
                    "candidates": [],
                    "model_assertion": {"source": "model_asserted"},
                    "disagreements": [],
                    "warning_codes": [],
                },
                "value_driver": "Strategic",
                "justification": "Validate current behavior.",
                "estimated_effort": "M",
            }
        ],
        "product_vision": "Vision",
        "technical_spec": "Spec",
        "compiled_authority": "{}",
        "time_increment": "Milestone-based",
        "prior_roadmap_state": "NO_PRIOR_ROADMAP",
        "user_input": "",
    }

    parsed = RoadmapBuilderInput.model_validate(payload)

    assert parsed.backlog_items[0].as_built_annotation is not None
```

- [ ] **Step 6: Run schema tests**

Run:

```bash
uv run --frozen pytest tests/test_backlog_primer_agent.py tests/test_roadmap_builder_schemas.py -q
```

Expected: PASS after updating tests for the new schema.

## Task 3: Host Annotation Derivation

**Files:**
- Modify: `services/backlog_runtime.py`
- Modify: `tests/test_agent_workbench_backlog_phase.py`

- [ ] **Step 1: Write exact authority match test**

Add:

```python
def test_backlog_runtime_fills_annotation_from_exact_authority_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Verify Live Squad Recommendation",
            "authority_ref": "REQ.live-squad-recommendation",
            "value_driver": "Strategic",
            "justification": "Validate existing behavior.",
            "estimated_effort": "S",
        },
    )

    assert result["success"] is True
    item = result["output_artifact"]["backlog_items"][0]
    annotation = item["as_built_annotation"]
    assert annotation["match_tier"] == "exact"
    assert annotation["selected"]["authority_ref"] == "REQ.live-squad-recommendation"
    assert annotation["selected"]["as_built_status"] == "observed"
    assert "metadata_filled_by_host" in annotation["warning_codes"]
```

- [ ] **Step 2: Write fuzzy match test**

```python
def test_backlog_runtime_warns_on_fuzzy_mapping_without_authoritative_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Live Squad Recommendation Evidence",
            "value_driver": "Strategic",
            "justification": "Looks related but has no exact authority ref.",
            "estimated_effort": "S",
        },
    )

    assert result["success"] is True
    item = result["output_artifact"]["backlog_items"][0]
    annotation = item["as_built_annotation"]
    assert annotation["match_tier"] == "fuzzy"
    assert "selected" in annotation
    assert annotation["selected"] is None
    assert "possible_mapping" in annotation["warning_codes"]
```

- [ ] **Step 3: Write host status/treatment capture test**

Create a fixture where As-Built marks a capability as `not_observed` with
`create_discovery_item`. The model supplies only `authority_ref` and
`capability_hint`; the host annotation must carry the As-Built status and
treatment.

```python
def test_annotation_preserves_status_disagreement_as_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment = _as_built_assessment_payload()
    assessment["capability_assessments"] = [
        {
            "authority_ref": "QUALITY.security-secrets",
            "invariant_refs": ["INV-506454637a21ed73"],
            "capability_title": "Security Secrets",
            "status": "not_observed",
            "confidence": "medium",
            "evidence": [],
            "limitations": ["No direct proof."],
            "recommended_backlog_treatment": "create_discovery_item",
            "reasoning": "Indirect hygiene only.",
        }
    ]

    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Verify Secrets Protection",
            "authority_ref": "QUALITY.security-secrets",
            "capability_hint": "secrets protection",
            "value_driver": "Strategic",
            "justification": "Review secret handling evidence.",
            "estimated_effort": "S",
        },
        assessment=assessment,
    )

    assert result["success"] is True
    item = result["output_artifact"]["backlog_items"][0]
    assert item["as_built_annotation"]["selected"]["as_built_status"] == (
        "not_observed"
    )
    assert item["as_built_annotation"]["selected"][
        "recommended_backlog_treatment"
    ] == "create_discovery_item"
```

- [ ] **Step 4: Write exact-match capability disagreement test**

Add a test for a valid but wrong `authority_ref`:

```python
def test_annotation_warns_when_exact_ref_conflicts_with_item_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment = _as_built_assessment_payload()
    assessment["capability_assessments"] = [
        {
            "authority_ref": "REQ.real-submit-disabled",
            "invariant_refs": ["INV-real-submit-disabled"],
            "capability_title": "Real Submit Disabled",
            "status": "observed",
            "confidence": "medium",
            "evidence": [],
            "limitations": [],
            "recommended_backlog_treatment": "skip_new_implementation",
            "reasoning": "Real-submit safety is already represented.",
        }
    ]

    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Verify Secrets Protection",
            "authority_ref": "REQ.real-submit-disabled",
            "capability_hint": "security secrets",
            "value_driver": "Strategic",
            "justification": "The ref is valid but points at a different capability.",
            "estimated_effort": "S",
        },
        assessment=assessment,
    )

    assert result["success"] is True
    annotation = result["output_artifact"]["backlog_items"][0][
        "as_built_annotation"
    ]
    assert annotation["match_tier"] == "exact"
    assert annotation["selected"]["authority_ref"] == "REQ.real-submit-disabled"
    assert "capability_disagreement" in annotation["warning_codes"]
```

- [ ] **Step 5: Implement `derive_brownfield_annotations`**

In `services/backlog_runtime.py`, replace preview equality validation with a
host annotation pass. Keep existing `_build_capability_index`,
`_mapped_capability`, and `_possible_unmapped_capability_matches` as internal
helpers where useful.

Implementation boundaries:

- exact `authority_ref` / `invariant_ref` match fills `selected`;
- exact match also compares item `requirement` and `capability_hint` against the
  selected capability title. Use `_normalize_brownfield_text` plus the existing
  brownfield token rules. Emit `capability_disagreement` when the selected title
  has tokens and neither `requirement` nor `capability_hint` shares any token
  with it;
- exact match with `conflict: true` and `selected: None` skips
  `capability_disagreement` because there is no single selected title;
- fuzzy match fills `candidates` only;
- no match fills no candidates;
- warnings are attached to both item annotation and top-level
  `brownfield_warnings[]`;
- `as_built_assessment == "NO_AS_BUILT_ASSESSMENT"` attaches no annotations,
  emits `brownfield_warnings: []`, and preserves greenfield behavior;
- no `ValueError` from brownfield annotation is returned to preview/generate
  unless the As-Built cache itself is malformed;
- malformed means the cached As-Built JSON is present but fails
  `AsBuiltAssessment.model_validate_json`.

- [ ] **Step 6: Attach annotation before artifact fingerprint**

In `run_backlog_agent_from_state`, after `OutputSchema.model_validate(parsed)`:

1. derive annotations;
2. produce `output_artifact = output_model.model_dump(exclude_none=True)`;
3. attach annotations with `annotation.model_dump(exclude_none=False)` so
   `selected: None` and model-assertion nulls remain present;
4. attach `brownfield_warnings`;
5. return success.

The fingerprint must be computed over the same present-with-null annotation
shape returned to agents.

Do not run `_with_brownfield_retry_feedback` for brownfield warnings.

- [ ] **Step 7: Run focused tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_backlog_phase.py -q -k 'annotation or fuzzy or disagreement'
```

Expected: PASS.

## Task 4: Non-Blocking Preview and Retry Removal

**Files:**
- Modify: `services/backlog_runtime.py`
- Modify: `services/phases/backlog_service.py`
- Modify: `tests/test_agent_workbench_backlog_phase.py`

- [ ] **Step 1: Rewrite preview failure test**

Replace `test_backlog_preview_surfaces_brownfield_contract_failure` with a test
where the mocked runtime returns `success: true`, `output_artifact` containing
`brownfield_warnings`, and `persisted: false`.

Expected assertions:

```python
assert result["ok"] is True
assert result["data"]["persisted"] is False
assert result["data"]["backlog_run_success"] is True
assert result["data"]["output_artifact"]["brownfield_warnings"]
```

- [ ] **Step 2: Rewrite retry metadata tests**

Change brownfield retry tests so preview invokes the agent once:

```python
assert len(calls) == 1
assert "BROWNFIELD CONTRACT RETRY" not in result.get("input_context", {}).get(
    "user_input",
    "",
)
```

Keep non-brownfield retry behavior only if there are existing tests for provider
or JSON/schema failures. Do not remove retry for invalid JSON or Pydantic output
schema failures unless a separate design approves that.

- [ ] **Step 3: Remove brownfield retry path**

In `run_backlog_agent_from_state`, remove the special retry branch for
`failure_stage == "brownfield_contract_validation"`. Brownfield annotation no
longer returns that failure stage during preview/generate.

- [ ] **Step 4: Update runtime diagnostics**

Remove `brownfield_retry_*` diagnostics from `BACKLOG_RUNTIME_DIAGNOSTIC_KEYS`
or stop emitting them for preview/generate. If keeping historical keys for
compatibility, tests must assert they are absent/null in the new success path.

- [ ] **Step 5: Run focused preview tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_backlog_phase.py -q -k 'preview or retry'
```

Expected: PASS.

## Task 5: Save-Time Annotated Artifact Gate

**Files:**
- Modify: `services/phases/backlog_service.py`
- Modify: `services/phases/workflow_state.py`
- Modify: `tests/test_backlog_phase_service.py`

- [ ] **Step 1: Write phase-attempt deep-copy test**

Add a test proving the recorded assessment and mirrored `backlog_items` do not
share mutable list identity:

```python
def test_record_backlog_attempt_deep_copies_mirrored_items() -> None:
    artifact = {
        "backlog_items": [
            {
                "priority": 1,
                "requirement": "Verify Live Squad",
                "value_driver": "Strategic",
                "justification": "Validate existing behavior.",
                "estimated_effort": "S",
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }
    state: dict[str, object] = {}

    record_backlog_attempt(
        state,
        trigger="auto_transition",
        input_context={},
        output_artifact=artifact,
        is_complete=True,
        created_at="2026-06-01T00:00:00Z",
    )

    state["backlog_items"][0]["requirement"] = "Changed During PO Review"

    assert state["product_backlog_assessment"]["backlog_items"][0][
        "requirement"
    ] == "Verify Live Squad"
```

- [ ] **Step 2: Write shared workflow-state deep-copy test**

Add a test in `tests/test_phase_workflow_state.py` proving the shared helper is
copy-on-record for any phase:

```python
def test_record_phase_attempt_deep_copies_output_and_mirror() -> None:
    state: dict[str, object] = {}
    output_artifact = {"items": [{"name": "Original"}]}

    workflow_state.record_phase_attempt(
        state,
        attempts_key="generic_attempts",
        last_input_context_key="generic_last_input",
        assessment_key="generic_assessment",
        trigger="test",
        input_context={},
        output_artifact=output_artifact,
        is_complete=True,
        created_at="2026-06-01T00:00:00Z",
        mirrored_output_field="items",
        mirrored_state_key="generic_items",
        mirrored_output_types=(list,),
    )

    output_artifact["items"][0]["name"] = "Mutated Source"
    state["generic_items"][0]["name"] = "Mutated Mirror"

    assert state["generic_assessment"]["items"][0]["name"] == "Original"
    assert state["generic_attempts"][0]["output_artifact"]["items"][0][
        "name"
    ] == "Original"
```

- [ ] **Step 3: Deep-copy recorded artifacts and mirrored fields**

In `services/phases/workflow_state.py`, import `copy` and deep-copy phase
artifacts before storing them:

```python
import copy
```

Then update `record_phase_attempt`:

```python
    normalized_output_artifact = copy.deepcopy(output_artifact)
```

and mirror values with a deep copy:

```python
        if mirrored_output_types is None:
            if mirrored_value is not None:
                state[mirrored_state_key] = copy.deepcopy(mirrored_value)
        elif isinstance(mirrored_value, mirrored_output_types):
            state[mirrored_state_key] = copy.deepcopy(mirrored_value)
```

This is intentionally repo-wide. `record_phase_attempt` is shared by backlog,
roadmap, story, and sprint phase services; every phase should get immutable
attempt snapshots rather than shared mutable references.

- [ ] **Step 4: Write persisted artifact tamper save test**

Add a test that uses the persisted shape created by `record_backlog_attempt`,
then mutates the persisted annotation before save validation.

Expected failure:

```python
def test_backlog_save_guard_recomputes_persisted_artifact_fingerprint() -> None:
    artifact = {
        "backlog_items": [
            {
                "priority": 1,
                "requirement": "Verify Live Squad",
                "authority_ref": "REQ.live-squad-recommendation",
                "capability_hint": "live squad",
                "value_driver": "Strategic",
                "justification": "Validate existing behavior.",
                "estimated_effort": "S",
                "as_built_annotation": {
                    "schema_version": "agileforge.brownfield_annotation.v1",
                    "source": "host_derived",
                    "match_tier": "exact",
                    "match_basis": ["authority_ref"],
                    "conflict": False,
                    "selected": None,
                    "candidates": [],
                    "model_assertion": {"source": "model_asserted"},
                    "disagreements": [],
                    "warning_codes": [],
                },
            }
        ],
        "brownfield_warnings": [],
        "is_complete": True,
        "clarifying_questions": [],
    }
    state: dict[str, object] = {}
    fingerprint = _backlog_artifact_fingerprint(artifact)
    attempt_count = record_backlog_attempt(
        state,
        trigger="auto_transition",
        input_context={},
        output_artifact=dict(artifact),
        is_complete=True,
        created_at="2026-06-01T00:00:00Z",
    )
    attempt_id = f"backlog-attempt-{attempt_count}"
    _attach_attempt_guards(
        state,
        attempt_id=attempt_id,
        artifact_fingerprint=fingerprint,
    )

    state["product_backlog_assessment"]["backlog_items"][0][
        "as_built_annotation"
    ]["match_tier"] = "none"

    with pytest.raises(BacklogPhaseError, match="artifact fingerprint"):
        _assert_save_guards(
            state=state,
            assessment=state["product_backlog_assessment"],
            attempt_id=attempt_id,
            expected_artifact_fingerprint=fingerprint,
        )
```

- [ ] **Step 5: Write asserted unmatched authority ref save test**

Prepare an assessment with:

```json
"brownfield_warnings": [
  {
    "code": "asserted_authority_ref_unmatched",
    "item_index": 0,
    "severity": "block_on_save",
    "match_tier": "none",
    "authority_ref": "REQ.imaginary",
    "invariant_refs": [],
    "message": "Model asserted authority_ref with no As-Built match.",
    "details": {}
  }
]
```

Expected failure:

```python
with pytest.raises(BacklogPhaseError, match="asserted authority_ref"):
    _assert_brownfield_save_gate(
        {
            "backlog_items": [],
            "brownfield_warnings": [
                {
                    "code": "asserted_authority_ref_unmatched",
                    "item_index": 0,
                    "severity": "block_on_save",
                    "match_tier": "none",
                    "authority_ref": "REQ.imaginary",
                    "invariant_refs": [],
                    "message": "Model asserted authority_ref with no As-Built match.",
                    "details": {},
                }
            ],
        }
    )
```

Also add an end-to-end `save_backlog_draft` test proving the helper is wired:

```python
@pytest.mark.asyncio
async def test_save_backlog_draft_blocks_unmatched_authority_ref_warning() -> None:
    artifact = {
        "backlog_items": [{"title": "Investigate Imaginary Requirement"}],
        "brownfield_warnings": [
            {
                "code": "asserted_authority_ref_unmatched",
                "item_index": 0,
                "severity": "block_on_save",
                "match_tier": "none",
                "authority_ref": "REQ.imaginary",
                "invariant_refs": [],
                "message": "Model asserted authority_ref with no As-Built match.",
                "details": {},
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }
    fingerprint = _backlog_artifact_fingerprint(artifact)
    state = {
        "fsm_state": "BACKLOG_REVIEW",
        "product_backlog_assessment": {
            **artifact,
            "artifact_fingerprint": fingerprint,
            "attempt_id": "backlog-attempt-1",
        },
        "backlog_attempts": [
            {
                "attempt_id": "backlog-attempt-1",
                "artifact_fingerprint": fingerprint,
                "output_artifact": artifact,
            }
        ],
    }

    async def hydrate_context() -> object:
        return SimpleNamespace(state=state, session_id="7")

    with pytest.raises(BacklogPhaseError, match="asserted authority_ref"):
        await save_backlog_draft(
            project_id=7,
            project_name="Backlog Project",
            attempt_id="backlog-attempt-1",
            expected_artifact_fingerprint=fingerprint,
            expected_state="BACKLOG_REVIEW",
            idempotency_key="save-backlog-unmatched-ref",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-06-01T00:00:00Z",
            hydrate_context=hydrate_context,
            build_tool_context=lambda context: context,
            save_backlog_tool=_fake_save_backlog_tool,
        )
```

- [ ] **Step 6: Write non-blocking warning save test**

Prepare an assessment with `possible_mapping`, `conflicting_invariants`, and
`status_disagreement` warnings but no block-listed warning codes.

Expected: `_assert_brownfield_save_gate` returns without raising.

```python
_assert_brownfield_save_gate(
    {
        "backlog_items": [],
        "brownfield_warnings": [
            {
                "code": "possible_mapping",
                "item_index": 0,
                "severity": "review",
                "match_tier": "fuzzy",
                "authority_ref": None,
                "invariant_refs": [],
                "message": "Possible As-Built match requires PO review.",
                "details": {},
            },
            {
                "code": "conflicting_invariants",
                "item_index": 0,
                "severity": "review",
                "match_tier": "exact",
                "authority_ref": "REQ.default-promotion-gate",
                "invariant_refs": [],
                "message": "Multiple invariant-level contracts exist.",
                "details": {},
            },
            {
                "code": "status_disagreement",
                "item_index": 0,
                "severity": "review",
                "match_tier": "exact",
                "authority_ref": "QUALITY.security-secrets",
                "invariant_refs": [],
                "message": "Model and host status disagree.",
                "details": {},
            },
        ],
    }
)
```

- [ ] **Step 7: Write warning cap preserves block-on-save test**

Add a test for the warning cap used by `derive_brownfield_annotations`: when an
item has more than three warnings, any `block_on_save` warning remains in
`brownfield_warnings`.

```python
assert any(
    warning["code"] == "asserted_authority_ref_unmatched"
    and warning["severity"] == "block_on_save"
    for warning in capped_warnings
)
```

- [ ] **Step 8: Implement persisted artifact fingerprint recomputation**

In `services/phases/backlog_service.py`, make the existing backlog artifact
fingerprint helper ignore guard fields that are added after the original
fingerprint is computed:

```python
_BACKLOG_ARTIFACT_GUARD_FIELDS = frozenset({"attempt_id", "artifact_fingerprint"})


def _artifact_for_fingerprint(output_artifact: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(output_artifact)
    for key in _BACKLOG_ARTIFACT_GUARD_FIELDS:
        normalized.pop(key, None)
    return normalized


def _backlog_artifact_fingerprint(output_artifact: dict[str, Any]) -> str:
    return canonical_hash(
        {
            "phase": "backlog",
            "output_artifact": _artifact_for_fingerprint(output_artifact),
        }
    )
```

Then strengthen `_assert_save_guards`:

```python
    if _backlog_artifact_fingerprint(assessment) != expected_artifact_fingerprint:
        raise BacklogPhaseError(
            "Backlog save guard mismatch: persisted artifact fingerprint does not "
            "match the reviewed Backlog draft.",
        )

    selected_artifact = selected_attempt.get("output_artifact")
    if not isinstance(selected_artifact, dict) or (
        _backlog_artifact_fingerprint(selected_artifact)
        != expected_artifact_fingerprint
    ):
        raise BacklogPhaseError(
            "Backlog save guard mismatch: selected attempt artifact fingerprint "
            "does not match the reviewed Backlog draft.",
        )
```

- [ ] **Step 9: Implement warning-code save gate**

In `services/phases/backlog_service.py`, add a helper before the call to
`save_backlog_tool`:

```python
def _assert_brownfield_save_gate(assessment: dict[str, Any]) -> None:
    warnings = assessment.get("brownfield_warnings")
    if isinstance(warnings, list):
        for warning in warnings:
            if not isinstance(warning, dict):
                continue
            if warning.get("code") == "asserted_authority_ref_unmatched":
                raise BacklogPhaseError(
                    "Backlog save blocked: asserted authority_ref has no As-Built match"
                )
```

Call `_assert_brownfield_save_gate(assessment)` inside `save_backlog_draft`
immediately after `_assert_save_guards(...)` and before the `is_complete`,
clarifying-question, and item checks:

```python
    _assert_save_guards(
        state=context.state,
        assessment=assessment,
        attempt_id=attempt_id,
        expected_artifact_fingerprint=expected_artifact_fingerprint,
    )
    _assert_brownfield_save_gate(assessment)
```

Rules:

- existing `_assert_save_guards` validates `attempt_id`, expected fingerprint
  metadata, and recomputed persisted artifact content;
- block if any `brownfield_warnings[].code` equals
  `asserted_authority_ref_unmatched`;
- do not re-derive annotations from the current As-Built cache.

- [ ] **Step 10: Write greenfield save round-trip test**

Add a greenfield/no-As-Built save test with no `as_built_annotation` and no
`brownfield_warnings`. It must pass the recomputed artifact fingerprint guard and
reach `save_backlog_tool`.

```python
@pytest.mark.asyncio
async def test_save_backlog_draft_allows_greenfield_artifact_without_annotations() -> None:
    artifact = {
        "backlog_items": [{"title": "Build Initial Capability"}],
        "is_complete": True,
        "clarifying_questions": [],
    }
    fingerprint = _backlog_artifact_fingerprint(artifact)
    state = {
        "fsm_state": "BACKLOG_REVIEW",
        "product_backlog_assessment": {
            **artifact,
            "artifact_fingerprint": fingerprint,
            "attempt_id": "backlog-attempt-1",
        },
        "backlog_attempts": [
            {
                "attempt_id": "backlog-attempt-1",
                "artifact_fingerprint": fingerprint,
                "output_artifact": artifact,
            }
        ],
    }
    captured: dict[str, object] = {}

    async def hydrate_context() -> object:
        return SimpleNamespace(state=state, session_id="7")

    def fake_save_backlog_tool(
        backlog_input: SaveBacklogInput,
        tool_context: object,
    ) -> dict[str, object]:
        captured["backlog_input"] = backlog_input
        return {"success": True, "saved_count": len(backlog_input.backlog_items)}

    payload = await save_backlog_draft(
        project_id=7,
        project_name="Backlog Project",
        attempt_id="backlog-attempt-1",
        expected_artifact_fingerprint=fingerprint,
        expected_state="BACKLOG_REVIEW",
        idempotency_key="save-backlog-greenfield",
        save_state=lambda _state: None,
        now_iso=lambda: "2026-06-01T00:00:00Z",
        hydrate_context=hydrate_context,
        build_tool_context=lambda context: context,
        save_backlog_tool=fake_save_backlog_tool,
    )

    assert payload["save_result"]["success"] is True
    assert captured["backlog_input"].backlog_items == artifact["backlog_items"]
```

- [ ] **Step 11: Run save tests**

Run:

```bash
uv run --frozen pytest tests/test_backlog_phase_service.py -q -k 'save or record_backlog_attempt'
```

Expected: PASS.

## Task 6: Prompt Contract Update

**Files:**
- Modify: `orchestrator_agent/agent_tools/backlog_primer/instructions.txt`
- Modify: `tests/test_backlog_primer_prompt_contract.py`

- [ ] **Step 1: Write prompt contract tests**

Assert:

```python
assert '"authority_ref"' in text
assert '"capability_hint"' in text
assert "do not emit as_built_annotation" in text
assert "do not emit capability_name" in text
assert "do not emit as_built_status" in text
assert "do not emit recommended_backlog_treatment" in text
```

Remove old assertions that require the model to include all four brownfield
metadata fields.

- [ ] **Step 2: Update instructions**

Change the brownfield guidance:

- model may include exact `authority_ref` when it can identify one;
- model may include free-text `capability_hint`;
- model must not emit host-owned annotation or copied As-Built status/treatment;
- host derives `as_built_annotation` after generation;
- model should still scope brownfield work as verification, hardening, discovery,
  or product work based on input context.

- [ ] **Step 3: Run prompt contract tests**

Run:

```bash
uv run --frozen pytest tests/test_backlog_primer_prompt_contract.py -q
```

Expected: PASS.

## Task 7: caRtola Failure-Shape Regression

**Files:**
- Modify: `tests/test_agent_workbench_backlog_phase.py`

- [ ] **Step 1: Add latest failure-shape minimal fixture test**

Use a minimal three-item fixture that represents the three key cases from
`backlog-20260601T110636168435Z-8d49065a9a98.json`. Do not paste the full
caRtola artifact into the test.

Fixture requirements:

- `backlog_items[0]`: exact `REQ.default-promotion-gate`, no capability name,
  with at least two As-Built capability rows for that authority ref that have
  different status/treatment values. Expected annotation is conflict.
- `backlog_items[1]`: exact `REQ.real-submit-disabled`, no capability name,
  with one homogeneous As-Built capability row. Expected annotation is selected.
- `backlog_items[2]`: `QUALITY.security-secrets` with model/As-Built
  disagreement. Expected annotation preserves disagreement data.

Expected:

```python
assert result["success"] is True
assert result["output_artifact"]["brownfield_warnings"]

default_gate = result["output_artifact"]["backlog_items"][0]["as_built_annotation"]
assert default_gate["match_tier"] == "exact"
assert default_gate["conflict"] is True
assert default_gate["selected"] is None
assert "conflicting_invariants" in default_gate["warning_codes"]
assert default_gate["candidates"]

real_submit = result["output_artifact"]["backlog_items"][1]["as_built_annotation"]
assert real_submit["match_tier"] == "exact"
assert real_submit["conflict"] is False
assert real_submit["selected"]["authority_ref"] == "REQ.real-submit-disabled"
assert "metadata_filled_by_host" in real_submit["warning_codes"]

security = result["output_artifact"]["backlog_items"][2]["as_built_annotation"]
assert security["match_tier"] == "exact"
assert security["selected"]["authority_ref"] == "QUALITY.security-secrets"
assert {
    disagreement["code"] for disagreement in security["disagreements"]
} >= {"status_disagreement", "treatment_disagreement"}
```

- [ ] **Step 2: Run the regression test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_backlog_phase.py::test_backlog_runtime_warns_for_latest_cartola_failure_shape -q
```

Expected: PASS.

## Task 8: Full Verification

**Files:**
- No new files.

- [ ] **Step 1: Run focused contract suite**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_backlog_phase.py tests/test_backlog_phase_service.py tests/test_phase_workflow_state.py tests/test_backlog_primer_prompt_contract.py tests/test_roadmap_builder_schemas.py -q
```

Expected: PASS.

- [ ] **Step 2: Run full repository gate**

Run:

```bash
uv run --frozen pyrepo-check --all
```

Expected: all checks pass.

- [ ] **Step 3: Commit**

```bash
git add utils/brownfield_annotations.py \
  orchestrator_agent/agent_tools/backlog_primer/schemes.py \
  orchestrator_agent/agent_tools/backlog_primer/instructions.txt \
  orchestrator_agent/agent_tools/roadmap_builder/schemes.py \
  services/backlog_runtime.py \
  services/phases/backlog_service.py \
  services/phases/workflow_state.py \
  tests/test_agent_workbench_backlog_phase.py \
  tests/test_backlog_phase_service.py \
  tests/test_phase_workflow_state.py \
  tests/test_backlog_primer_agent.py \
  tests/test_backlog_primer_prompt_contract.py \
  tests/test_roadmap_builder_schemas.py
git commit -m "refactor: derive brownfield backlog annotations in host"
```

## Self-Review Checklist

- Preview/generate do not fail closed for brownfield warnings.
- Preview/generate do not invoke `BROWNFIELD CONTRACT RETRY`.
- Exact matches fill host annotation.
- Fuzzy matches produce warnings only.
- Conflicting invariants produce warnings only.
- Model/As-Built disagreement is represented as structured data.
- Save blocks only the closed block list.
- Save recomputes the persisted artifact fingerprint and does not re-derive
  annotations.
- Greenfield/no-As-Built save round-trip still succeeds.
- Recorded attempts and mirrored `backlog_items` do not share mutable object
  identity.
- `uv run --frozen pyrepo-check --all` passes.
