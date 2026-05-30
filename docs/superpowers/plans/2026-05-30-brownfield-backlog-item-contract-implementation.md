# Brownfield Backlog Item Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce brownfield-aware Backlog Primer output so As-Built-observed capabilities become verification, hardening, discovery, or conflict Product Backlog Items instead of greenfield-looking rebuild items.

**Architecture:** Add optional brownfield metadata fields to the Backlog Primer and Roadmap Builder schemas, then add host-side validation in `services/backlog_runtime.py` after `OutputSchema.model_validate(...)` and before `model_dump(...)`. The validator must compare output items against the authoritative `input_context["as_built_assessment"]`, not only against model-provided metadata.

**Tech Stack:** Python 3.13, Pydantic v2, existing ADK runner wrapper, pytest, AgileForge workflow-state Backlog runtime, existing failure artifact writer.

---

## File Map

- Modify `orchestrator_agent/agent_tools/backlog_primer/schemes.py`
  - Add optional metadata fields to `BacklogItem`.
  - Update `requirement` field description to mean work item title.
- Modify `orchestrator_agent/agent_tools/backlog_primer/instructions.txt`
  - Update JSON output contract.
  - Tell Backlog Primer to emit brownfield metadata when `as_built_assessment` maps to a capability.
  - Tell Backlog Primer that `requirement` is the action-oriented work item title and `capability_name` is the assessed product capability.
- Modify `orchestrator_agent/agent_tools/roadmap_builder/schemes.py`
  - Add the same optional metadata fields to nested `BacklogItem` to avoid `extra="forbid"` breakage.
- Modify `services/backlog_runtime.py`
  - Add `BrownfieldContractValidationError`.
  - Add `_validate_brownfield_contract(output_model, input_context)`.
  - Parse As-Built input with `AsBuiltAssessment.model_validate_json(...)`.
  - Match backlog items to As-Built capabilities using item-side `authority_ref`, `capability_name`, and `requirement`.
  - Enforce metadata equality and title prefix allowlists.
  - Return failure artifact with `failure_stage="brownfield_contract_validation"`.
- Modify `tests/test_backlog_primer_agent.py`
  - Add schema tests for enriched Backlog Primer items.
- Modify `tests/test_agent_workbench_backlog_phase.py`
  - Add runtime validation tests for valid and invalid brownfield outputs.
- Modify or create `tests/test_backlog_primer_prompt_contract.py`
  - Add prompt contract checks for the new output fields and title semantics.
- Modify or create `tests/test_roadmap_builder_schemas.py`
  - Add schema test proving Roadmap Builder accepts enriched backlog items.

---

### Task 1: Backlog Primer Schema Metadata

**Files:**
- Modify: `tests/test_backlog_primer_agent.py`
- Modify: `orchestrator_agent/agent_tools/backlog_primer/schemes.py`

- [ ] **Step 1: Write failing schema test for enriched backlog item**

Add this test inside `TestBacklogPrimerSchemas` in `tests/test_backlog_primer_agent.py`:

```python
    def test_output_schema_accepts_brownfield_metadata(self) -> None:
        """Backlog items may carry optional As-Built trace metadata."""
        payload: dict[str, Any] = {
            "backlog_items": [
                {
                    "priority": 1,
                    "requirement": "Validate Captain-Aware Optimizer Contract",
                    "capability_name": "Captain-Aware Squad Optimizer",
                    "authority_ref": "REQ.captain-aware-optimization",
                    "as_built_status": "observed_with_missing_evidence",
                    "recommended_backlog_treatment": "create_verification_item",
                    "value_driver": "Strategic",
                    "justification": "As-Built evidence indicates the optimizer exists.",
                    "estimated_effort": "M",
                    "technical_note": "Validate existing captain multiplier behavior.",
                }
            ],
            "is_complete": False,
            "clarifying_questions": [],
        }

        parsed = OutputSchema.model_validate_json(json.dumps(payload))

        item = parsed.backlog_items[0]
        assert item.requirement == "Validate Captain-Aware Optimizer Contract"
        assert item.capability_name == "Captain-Aware Squad Optimizer"
        assert item.authority_ref == "REQ.captain-aware-optimization"
        assert item.as_built_status == "observed_with_missing_evidence"
        assert item.recommended_backlog_treatment == "create_verification_item"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run --frozen pytest tests/test_backlog_primer_agent.py::TestBacklogPrimerSchemas::test_output_schema_accepts_brownfield_metadata -q
```

Expected: FAIL with Pydantic `extra_forbidden` for the new metadata fields.

- [ ] **Step 3: Add optional metadata fields to Backlog Primer schema**

In `orchestrator_agent/agent_tools/backlog_primer/schemes.py`, update imports and `BacklogItem`:

```python
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestrator_agent.agent_tools.as_built_assessor.schemes import (
    AssessmentStatus,
    BacklogTreatment,
)
```

Change the `requirement` description:

```python
    requirement: Annotated[
        str,
        Field(
            min_length=3,
            description=(
                "Action-oriented Product Backlog Item title describing remaining "
                "work. In brownfield contexts this is not the capability name."
            ),
        ),
    ]
```

Add these fields immediately after `requirement`:

```python
    capability_name: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional brownfield trace: product capability being assessed."
            ),
        ),
    ]
    authority_ref: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional brownfield trace: As-Built authority_ref/spec reference."
            ),
        ),
    ]
    as_built_status: Annotated[
        AssessmentStatus | None,
        Field(
            default=None,
            description="Optional brownfield trace: As-Built capability status.",
        ),
    ]
    recommended_backlog_treatment: Annotated[
        BacklogTreatment | None,
        Field(
            default=None,
            description=(
                "Optional brownfield trace: recommended backlog treatment from "
                "As-Built."
            ),
        ),
    ]
```

- [ ] **Step 4: Run schema tests**

Run:

```bash
uv run --frozen pytest tests/test_backlog_primer_agent.py::TestBacklogPrimerSchemas -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add tests/test_backlog_primer_agent.py orchestrator_agent/agent_tools/backlog_primer/schemes.py
git commit -m "feat: add brownfield metadata to backlog schema"
```

---

### Task 2: Roadmap Builder Schema Compatibility

**Files:**
- Create or Modify: `tests/test_roadmap_builder_schemas.py`
- Modify: `orchestrator_agent/agent_tools/roadmap_builder/schemes.py`

- [ ] **Step 1: Write failing Roadmap schema test**

If `tests/test_roadmap_builder_schemas.py` does not exist, create it with:

```python
"""Schema tests for Roadmap Builder agent contracts."""

from __future__ import annotations

from orchestrator_agent.agent_tools.roadmap_builder.schemes import RoadmapBuilderInput


def test_roadmap_input_accepts_enriched_backlog_items() -> None:
    """Roadmap Builder must accept Backlog Primer brownfield metadata."""
    parsed = RoadmapBuilderInput.model_validate(
        {
            "backlog_items": [
                {
                    "priority": 1,
                    "requirement": "Validate Captain-Aware Optimizer Contract",
                    "capability_name": "Captain-Aware Squad Optimizer",
                    "authority_ref": "REQ.captain-aware-optimization",
                    "as_built_status": "observed_with_missing_evidence",
                    "recommended_backlog_treatment": "create_verification_item",
                    "value_driver": "Strategic",
                    "justification": "As-Built evidence indicates existing behavior.",
                    "estimated_effort": "M",
                    "technical_note": "Validate existing captain multiplier behavior.",
                }
            ],
            "product_vision": "For operators who need safe live recommendations.",
            "technical_spec": "Spec content",
            "compiled_authority": '{"invariants":[]}',
        }
    )

    item = parsed.backlog_items[0]
    assert item.capability_name == "Captain-Aware Squad Optimizer"
    assert item.authority_ref == "REQ.captain-aware-optimization"
    assert item.as_built_status == "observed_with_missing_evidence"
    assert item.recommended_backlog_treatment == "create_verification_item"
```

If the file already exists, add only the test function and imports it needs.

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run --frozen pytest tests/test_roadmap_builder_schemas.py::test_roadmap_input_accepts_enriched_backlog_items -q
```

Expected: FAIL with Pydantic `extra_forbidden`.

- [ ] **Step 3: Add optional metadata fields to Roadmap Builder nested BacklogItem**

In `orchestrator_agent/agent_tools/roadmap_builder/schemes.py`, add imports:

```python
from orchestrator_agent.agent_tools.as_built_assessor.schemes import (
    AssessmentStatus,
    BacklogTreatment,
)
```

Change the `requirement` description:

```python
    requirement: Annotated[
        str,
        Field(
            min_length=3,
            description="Action-oriented backlog work item title.",
        ),
    ]
```

Add these fields immediately after `requirement`:

```python
    capability_name: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional brownfield trace: product capability.",
        ),
    ]
    authority_ref: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional brownfield trace: As-Built authority_ref.",
        ),
    ]
    as_built_status: Annotated[
        AssessmentStatus | None,
        Field(
            default=None,
            description="Optional brownfield trace: As-Built status.",
        ),
    ]
    recommended_backlog_treatment: Annotated[
        BacklogTreatment | None,
        Field(
            default=None,
            description="Optional brownfield trace: As-Built treatment.",
        ),
    ]
```

- [ ] **Step 4: Run Roadmap schema test**

Run:

```bash
uv run --frozen pytest tests/test_roadmap_builder_schemas.py::test_roadmap_input_accepts_enriched_backlog_items -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add tests/test_roadmap_builder_schemas.py orchestrator_agent/agent_tools/roadmap_builder/schemes.py
git commit -m "feat: allow roadmap input brownfield metadata"
```

---

### Task 3: Backlog Primer Prompt Contract

**Files:**
- Create or Modify: `tests/test_backlog_primer_prompt_contract.py`
- Modify: `orchestrator_agent/agent_tools/backlog_primer/instructions.txt`

- [ ] **Step 1: Write failing prompt contract tests**

Create `tests/test_backlog_primer_prompt_contract.py`:

```python
"""Prompt contract checks for the Backlog Primer agent."""

from pathlib import Path


INSTRUCTIONS_PATH = Path(
    "orchestrator_agent/agent_tools/backlog_primer/instructions.txt"
)


def _instructions() -> str:
    return INSTRUCTIONS_PATH.read_text(encoding="utf-8")


def test_backlog_prompt_defines_brownfield_output_fields() -> None:
    """Prompt must name every brownfield metadata output field."""
    text = _instructions()

    assert '"capability_name"' in text
    assert '"authority_ref"' in text
    assert '"as_built_status"' in text
    assert '"recommended_backlog_treatment"' in text


def test_backlog_prompt_separates_work_item_title_from_capability() -> None:
    """Prompt must distinguish requirement title from capability identity."""
    text = _instructions()

    assert "requirement is the action-oriented work item title" in text
    assert "capability_name is the product capability" in text


def test_backlog_prompt_requires_as_built_metadata_when_capability_maps() -> None:
    """Prompt must tell model to emit metadata for mapped As-Built items."""
    text = _instructions()

    assert "When a backlog item maps to an As-Built capability" in text
    assert "must include capability_name, authority_ref, as_built_status, and recommended_backlog_treatment" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --frozen pytest tests/test_backlog_primer_prompt_contract.py -q
```

Expected: FAIL because the prompt does not yet contain the new contract text.

- [ ] **Step 3: Update Backlog Primer instructions**

In `orchestrator_agent/agent_tools/backlog_primer/instructions.txt`, update the output example to:

```text
{
  "backlog_items": [
    {
      "priority": 1,
      "requirement": "Action-oriented work item title",
      "capability_name": "Optional: product capability being assessed",
      "authority_ref": "Optional: As-Built authority_ref/spec reference",
      "as_built_status": "Optional: observed | observed_with_missing_evidence | contradicted | not_observed | unclear",
      "recommended_backlog_treatment": "Optional: skip_new_implementation | create_verification_item | create_hardening_item | create_authority_conflict_item | create_discovery_item | create_product_item | po_review_required",
      "value_driver": "Revenue" | "Customer Satisfaction" | "Strategic",
      "justification": "Why this priority? Link to Vision and As-Built status when present.",
      "estimated_effort": "S" | "M" | "L" | "XL",
      "technical_note": "Optional: sizing context, scope caveats, As-Built evidence limitation, or effort rationale"
    }
  ],
  "is_complete": boolean,
  "clarifying_questions": ["Question 1", "Question 2"]
}
```

Add this paragraph under `AS-BUILT ASSESSMENT`:

```text
* `requirement` is the action-oriented work item title. It names the remaining backlog work, not merely the capability.
* `capability_name` is the product capability from As-Built `capability_title`.
* When a backlog item maps to an As-Built capability, it must include capability_name, authority_ref, as_built_status, and recommended_backlog_treatment.
* If `status=observed`, title the item with Verify, Document, Monitor, or Preserve. Do not title it as Build, Implement, Create, or the plain capability name.
* If `status=observed_with_missing_evidence`, title the item with Verify, Validate, Harden, Formalize, or Add Evidence For.
* If `status=contradicted`, title the item with Resolve, Align, or Correct.
* If `status=unclear`, title the item with Discover, Investigate, or Clarify.
* If `status=not_observed`, Build, Add, Implement, or Create may be appropriate when accepted authority requires the capability.
```

- [ ] **Step 4: Run prompt contract tests**

Run:

```bash
uv run --frozen pytest tests/test_backlog_primer_prompt_contract.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add tests/test_backlog_primer_prompt_contract.py orchestrator_agent/agent_tools/backlog_primer/instructions.txt
git commit -m "docs: specify brownfield backlog prompt contract"
```

---

### Task 4: Brownfield Runtime Validation Tests

**Files:**
- Modify: `tests/test_agent_workbench_backlog_phase.py`

- [ ] **Step 1: Add helper for Backlog agent output JSON**

Add this helper after `_as_built_state(...)` in `tests/test_agent_workbench_backlog_phase.py`:

```python
def _backlog_output_json(
    item: dict[str, Any],
    *,
    is_complete: bool = True,
    clarifying_questions: list[str] | None = None,
) -> str:
    """Return Backlog Primer output JSON with one item."""
    return json.dumps(
        {
            "backlog_items": [item],
            "is_complete": is_complete,
            "clarifying_questions": clarifying_questions or [],
        }
    )
```

- [ ] **Step 2: Add valid brownfield runtime test**

Add this test near existing `build_backlog_input_context` tests:

```python
def test_backlog_runtime_accepts_valid_brownfield_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime accepts item metadata that matches authoritative As-Built input."""
    from services.backlog_runtime import run_backlog_agent_from_state

    async def fake_invoke(payload: Any) -> str:
        del payload
        return _backlog_output_json(
            {
                "priority": 1,
                "requirement": "Verify Live Squad Recommendation",
                "capability_name": "Live squad recommendation",
                "authority_ref": "REQ.live-squad-recommendation",
                "as_built_status": "observed",
                "recommended_backlog_treatment": "skip_new_implementation",
                "value_driver": "Strategic",
                "justification": "Capability is observed; preserve evidence.",
                "estimated_effort": "S",
                "technical_note": "Verify existing evidence remains canonical.",
            }
        )

    monkeypatch.setattr("services.backlog_runtime._invoke_backlog_agent", fake_invoke)
    state = {
        "product_vision_assessment": {
            "product_vision_statement": "A clear saved vision.",
            "is_complete": True,
        },
        "pending_spec_content": "SPEC CONTENT",
        "compiled_authority_cached": "AUTHORITY JSON",
        **_as_built_state(_as_built_assessment_payload()),
    }

    async def call_runtime() -> dict[str, Any]:
        return await run_backlog_agent_from_state(
            state,
            project_id=2,
            user_input=None,
        )

    result = anyio.run(call_runtime)

    assert result["success"] is True
    assert result["failure_stage"] is None
    item = result["output_artifact"]["backlog_items"][0]
    assert item["capability_name"] == "Live squad recommendation"
```

Also add `import anyio` near the top of the file if it is not already imported.

- [ ] **Step 3: Add missing metadata regression test**

Add:

```python
def test_backlog_runtime_rejects_capability_title_without_brownfield_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain capability title must not bypass brownfield validation."""
    from services.backlog_runtime import run_backlog_agent_from_state

    async def fake_invoke(payload: Any) -> str:
        del payload
        return _backlog_output_json(
            {
                "priority": 1,
                "requirement": "Live squad recommendation",
                "value_driver": "Strategic",
                "justification": "This repeats the observed capability title.",
                "estimated_effort": "M",
                "technical_note": "Should be rejected before preview/save.",
            }
        )

    monkeypatch.setattr("services.backlog_runtime._invoke_backlog_agent", fake_invoke)
    state = {
        "product_vision_assessment": {
            "product_vision_statement": "A clear saved vision.",
            "is_complete": True,
        },
        "pending_spec_content": "SPEC CONTENT",
        "compiled_authority_cached": "AUTHORITY JSON",
        **_as_built_state(_as_built_assessment_payload()),
    }

    async def call_runtime() -> dict[str, Any]:
        return await run_backlog_agent_from_state(
            state,
            project_id=2,
            user_input=None,
        )

    result = anyio.run(call_runtime)

    assert result["success"] is False
    assert result["failure_stage"] == "brownfield_contract_validation"
    assert "missing capability_name" in str(result["failure_summary"])
```

- [ ] **Step 4: Add mismatched status/treatment test**

Add:

```python
def test_backlog_runtime_rejects_mismatched_brownfield_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model-provided status and treatment must match authoritative As-Built."""
    from services.backlog_runtime import run_backlog_agent_from_state

    async def fake_invoke(payload: Any) -> str:
        del payload
        return _backlog_output_json(
            {
                "priority": 1,
                "requirement": "Build Live Squad Recommendation",
                "capability_name": "Live squad recommendation",
                "authority_ref": "REQ.live-squad-recommendation",
                "as_built_status": "not_observed",
                "recommended_backlog_treatment": "create_product_item",
                "value_driver": "Strategic",
                "justification": "Incorrectly treats observed behavior as missing.",
                "estimated_effort": "L",
                "technical_note": "Should fail due to metadata mismatch.",
            }
        )

    monkeypatch.setattr("services.backlog_runtime._invoke_backlog_agent", fake_invoke)
    state = {
        "product_vision_assessment": {
            "product_vision_statement": "A clear saved vision.",
            "is_complete": True,
        },
        "pending_spec_content": "SPEC CONTENT",
        "compiled_authority_cached": "AUTHORITY JSON",
        **_as_built_state(_as_built_assessment_payload()),
    }

    async def call_runtime() -> dict[str, Any]:
        return await run_backlog_agent_from_state(
            state,
            project_id=2,
            user_input=None,
        )

    result = anyio.run(call_runtime)

    assert result["success"] is False
    assert result["failure_stage"] == "brownfield_contract_validation"
    summary = str(result["failure_summary"])
    assert "as_built_status" in summary
    assert "recommended_backlog_treatment" in summary
```

- [ ] **Step 5: Add noun-only or greenfield-looking title test**

Add:

```python
def test_backlog_runtime_rejects_observed_item_with_greenfield_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observed capabilities need preserve/verify/document titles, not build titles."""
    from services.backlog_runtime import run_backlog_agent_from_state

    async def fake_invoke(payload: Any) -> str:
        del payload
        return _backlog_output_json(
            {
                "priority": 1,
                "requirement": "Build Live Squad Recommendation",
                "capability_name": "Live squad recommendation",
                "authority_ref": "REQ.live-squad-recommendation",
                "as_built_status": "observed",
                "recommended_backlog_treatment": "skip_new_implementation",
                "value_driver": "Strategic",
                "justification": "Title conflicts with observed status.",
                "estimated_effort": "M",
                "technical_note": "Should fail due to title allowlist.",
            }
        )

    monkeypatch.setattr("services.backlog_runtime._invoke_backlog_agent", fake_invoke)
    state = {
        "product_vision_assessment": {
            "product_vision_statement": "A clear saved vision.",
            "is_complete": True,
        },
        "pending_spec_content": "SPEC CONTENT",
        "compiled_authority_cached": "AUTHORITY JSON",
        **_as_built_state(_as_built_assessment_payload()),
    }

    async def call_runtime() -> dict[str, Any]:
        return await run_backlog_agent_from_state(
            state,
            project_id=2,
            user_input=None,
        )

    result = anyio.run(call_runtime)

    assert result["success"] is False
    assert result["failure_stage"] == "brownfield_contract_validation"
    assert "title prefix" in str(result["failure_summary"])
```

- [ ] **Step 6: Run tests to verify they fail**

Run:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_backlog_phase.py::test_backlog_runtime_accepts_valid_brownfield_item \
  tests/test_agent_workbench_backlog_phase.py::test_backlog_runtime_rejects_capability_title_without_brownfield_metadata \
  tests/test_agent_workbench_backlog_phase.py::test_backlog_runtime_rejects_mismatched_brownfield_metadata \
  tests/test_agent_workbench_backlog_phase.py::test_backlog_runtime_rejects_observed_item_with_greenfield_title \
  -q
```

Expected:
- valid test may fail because schema fields are not implemented if Task 1 was skipped;
- invalid tests fail because no brownfield validator exists yet.

- [ ] **Step 7: Commit failing tests only if project convention allows red commits**

Do not commit red tests if the team expects every commit green. If red commits are not allowed, leave this task uncommitted and continue directly to Task 5.

---

### Task 5: Brownfield Runtime Validator Implementation

**Files:**
- Modify: `services/backlog_runtime.py`

- [ ] **Step 1: Add imports and exception class**

In `services/backlog_runtime.py`, update imports:

```python
import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
```

Add imports near the existing Backlog Primer schema imports:

```python
from orchestrator_agent.agent_tools.as_built_assessor.schemes import (
    AsBuiltAssessment,
    CapabilityAssessment,
)
from orchestrator_agent.agent_tools.backlog_primer.schemes import (
    BacklogItem,
    InputSchema,
    OutputSchema,
)
```

Add after `_FailureDetails`:

```python
@dataclass(frozen=True)
class _AsBuiltCapabilityMatch:
    """Normalized As-Built capability data used by brownfield validation."""

    authority_ref: str
    capability_title: str
    status: str
    recommended_backlog_treatment: str


class BrownfieldContractValidationError(ValueError):
    """Raised when Backlog output violates authoritative As-Built context."""
```

- [ ] **Step 2: Add normalization and lookup helpers**

Add below `_normalize_validation_errors(...)`:

```python
def _normalize_contract_key(value: object) -> str:
    """Normalize contract keys for comparison without rewriting output."""
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _as_built_text(input_context: BacklogInputContext) -> str:
    value = input_context.get("as_built_assessment")
    return value if isinstance(value, str) else ""


def _load_as_built_assessment(
    input_context: BacklogInputContext,
) -> AsBuiltAssessment | None:
    text = _as_built_text(input_context).strip()
    if not text or text == "NO_AS_BUILT_ASSESSMENT":
        return None
    try:
        return AsBuiltAssessment.model_validate_json(text)
    except ValidationError as exc:
        raise BrownfieldContractValidationError(
            f"Invalid as_built_assessment input: {exc}"
        ) from exc


def _capability_matches(
    assessment: AsBuiltAssessment,
) -> dict[str, list[_AsBuiltCapabilityMatch]]:
    lookup: dict[str, list[_AsBuiltCapabilityMatch]] = {}
    for capability in assessment.capability_assessments:
        match = _AsBuiltCapabilityMatch(
            authority_ref=capability.authority_ref,
            capability_title=capability.capability_title,
            status=capability.status,
            recommended_backlog_treatment=capability.recommended_backlog_treatment,
        )
        for raw_key in (capability.authority_ref, capability.capability_title):
            normalized = _normalize_contract_key(raw_key)
            if not normalized:
                continue
            lookup.setdefault(normalized, []).append(match)
    return lookup
```

- [ ] **Step 3: Add match resolution and title helpers**

Add below `_capability_matches(...)`:

```python
def _unique_matches(
    matches: Iterable[_AsBuiltCapabilityMatch],
) -> list[_AsBuiltCapabilityMatch]:
    """Deduplicate equivalent matches while preserving order."""
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[_AsBuiltCapabilityMatch] = []
    for match in matches:
        key = (
            match.authority_ref,
            match.capability_title,
            match.status,
            match.recommended_backlog_treatment,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(match)
    return unique


def _item_match_candidates(item: BacklogItem) -> list[tuple[str, str]]:
    """Return item-side fields that can map a backlog item to As-Built."""
    candidates: list[tuple[str, str]] = [("requirement", item.requirement)]
    if item.authority_ref:
        candidates.append(("authority_ref", item.authority_ref))
    if item.capability_name:
        candidates.append(("capability_name", item.capability_name))
    return candidates


def _match_item_to_capability(
    item: BacklogItem,
    lookup: dict[str, list[_AsBuiltCapabilityMatch]],
) -> _AsBuiltCapabilityMatch | None:
    """Return the As-Built capability matched by item fields."""
    matches: list[_AsBuiltCapabilityMatch] = []
    for _field_name, raw_value in _item_match_candidates(item):
        normalized = _normalize_contract_key(raw_value)
        if normalized in lookup:
            matches.extend(lookup[normalized])

    unique = _unique_matches(matches)
    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]

    if item.authority_ref and item.capability_name:
        normalized_ref = _normalize_contract_key(item.authority_ref)
        normalized_name = _normalize_contract_key(item.capability_name)
        narrowed = [
            match
            for match in unique
            if _normalize_contract_key(match.authority_ref) == normalized_ref
            and _normalize_contract_key(match.capability_title) == normalized_name
        ]
        narrowed_unique = _unique_matches(narrowed)
        if len(narrowed_unique) == 1:
            return narrowed_unique[0]

    same_contract = {
        (match.status, match.recommended_backlog_treatment) for match in unique
    }
    if len(same_contract) == 1:
        return unique[0]

    refs = ", ".join(
        f"{match.authority_ref} / {match.capability_title}" for match in unique
    )
    raise BrownfieldContractValidationError(
        f"Backlog item {item.requirement!r} maps ambiguously to As-Built "
        f"capabilities: {refs}"
    )


_TITLE_PREFIXES: dict[str, tuple[str, ...]] = {
    "observed": ("verify", "document", "monitor", "preserve"),
    "observed_with_missing_evidence": (
        "verify",
        "validate",
        "harden",
        "formalize",
        "add evidence for",
    ),
    "contradicted": ("resolve", "align", "correct"),
    "unclear": ("discover", "investigate", "clarify"),
    "not_observed": ("build", "add", "implement", "create"),
}


def _title_has_allowed_prefix(title: str, status: str) -> bool:
    normalized = _normalize_contract_key(title)
    prefixes = _TITLE_PREFIXES.get(status)
    if not prefixes:
        return False
    return any(
        normalized == prefix or normalized.startswith(f"{prefix} ")
        for prefix in prefixes
    )
```

- [ ] **Step 4: Add the validator**

Add below `_title_has_allowed_prefix(...)`:

```python
def _validate_brownfield_contract(
    *,
    output_model: OutputSchema,
    input_context: BacklogInputContext,
) -> None:
    """Validate Backlog output against authoritative As-Built input."""
    assessment = _load_as_built_assessment(input_context)
    if assessment is None:
        return
    lookup = _capability_matches(assessment)
    if not lookup:
        return

    errors: list[str] = []
    for index, item in enumerate(output_model.backlog_items, start=1):
        match = _match_item_to_capability(item, lookup)
        if match is None:
            continue

        prefix = f"backlog_items[{index}] {item.requirement!r}"
        if item.capability_name is None:
            errors.append(f"{prefix}: missing capability_name")
        elif (
            _normalize_contract_key(item.capability_name)
            != _normalize_contract_key(match.capability_title)
        ):
            errors.append(
                f"{prefix}: capability_name {item.capability_name!r} does not "
                f"match As-Built capability_title {match.capability_title!r}"
            )

        if item.authority_ref is None:
            errors.append(f"{prefix}: missing authority_ref")
        elif (
            _normalize_contract_key(item.authority_ref)
            != _normalize_contract_key(match.authority_ref)
        ):
            errors.append(
                f"{prefix}: authority_ref {item.authority_ref!r} does not match "
                f"As-Built authority_ref {match.authority_ref!r}"
            )

        if item.as_built_status is None:
            errors.append(f"{prefix}: missing as_built_status")
        elif item.as_built_status != match.status:
            errors.append(
                f"{prefix}: as_built_status {item.as_built_status!r} does not "
                f"match As-Built status {match.status!r}"
            )

        if item.recommended_backlog_treatment is None:
            errors.append(f"{prefix}: missing recommended_backlog_treatment")
        elif item.recommended_backlog_treatment != match.recommended_backlog_treatment:
            errors.append(
                f"{prefix}: recommended_backlog_treatment "
                f"{item.recommended_backlog_treatment!r} does not match As-Built "
                f"treatment {match.recommended_backlog_treatment!r}"
            )

        if _normalize_contract_key(item.requirement) == _normalize_contract_key(
            item.capability_name
        ):
            errors.append(
                f"{prefix}: requirement must be a work item title, not the "
                "capability_name"
            )

        if not _title_has_allowed_prefix(item.requirement, match.status):
            allowed = ", ".join(_TITLE_PREFIXES.get(match.status, ()))
            errors.append(
                f"{prefix}: title prefix is not allowed for As-Built status "
                f"{match.status!r}; expected one of: {allowed}"
            )

    if errors:
        raise BrownfieldContractValidationError("; ".join(errors))
```

- [ ] **Step 5: Integrate validator into runtime**

In `run_backlog_agent_from_state(...)`, immediately after successful `OutputSchema.model_validate(parsed)` and before `output_model.model_dump(...)`, add:

```python
    try:
        _validate_brownfield_contract(
            output_model=output_model,
            input_context=input_context,
        )
    except BrownfieldContractValidationError as exc:
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="brownfield_contract_validation",
            details=_FailureDetails(
                message=f"Backlog brownfield contract validation failed: {exc}",
                raw_text=raw_text,
                exception=exc,
            ),
        )
```

- [ ] **Step 6: Run focused runtime tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_backlog_phase.py::test_backlog_runtime_accepts_valid_brownfield_item \
  tests/test_agent_workbench_backlog_phase.py::test_backlog_runtime_rejects_capability_title_without_brownfield_metadata \
  tests/test_agent_workbench_backlog_phase.py::test_backlog_runtime_rejects_mismatched_brownfield_metadata \
  tests/test_agent_workbench_backlog_phase.py::test_backlog_runtime_rejects_observed_item_with_greenfield_title \
  -q
```

Expected: PASS.

- [ ] **Step 7: Commit Tasks 4 and 5**

```bash
git add tests/test_agent_workbench_backlog_phase.py services/backlog_runtime.py
git commit -m "feat: validate brownfield backlog output"
```

---

### Task 6: Regression Coverage For Failure Envelope And Existing Paths

**Files:**
- Modify: `tests/test_agent_workbench_backlog_phase.py`

- [ ] **Step 1: Add failure envelope assertion test**

Add:

```python
def test_backlog_preview_surfaces_brownfield_contract_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workbench preview envelope should expose brownfield validation failures."""
    async def fake_run_backlog_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        del state, project_id, user_input
        return {
            "success": False,
            "error": "Backlog brownfield contract validation failed",
            "failure_stage": "brownfield_contract_validation",
            "failure_summary": "Backlog brownfield contract validation failed",
            "failure_artifact_id": "backlog-failure-brownfield-1",
            "has_full_artifact": True,
            "input_context": {
                "product_vision_statement": "A clear saved vision.",
                "technical_spec": "SPEC CONTENT",
                "compiled_authority": "AUTHORITY JSON",
                "prior_backlog_state": "NO_HISTORY",
                "as_built_assessment": "{}",
                "implementation_evidence": "NO_EVIDENCE",
                "user_input": "",
            },
            "output_artifact": {
                "is_complete": False,
                "error": "BACKLOG_GENERATION_FAILED",
                "failure_summary": "Backlog brownfield contract validation failed",
            },
            "is_complete": False,
        }

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        state = tool_context.state
        state["pending_spec_content"] = "SPEC CONTENT"
        state["compiled_authority_cached"] = "AUTHORITY JSON"
        state["product_vision_assessment"] = {
            "product_vision_statement": "A clear saved vision.",
            "is_complete": True,
        }
        return {"success": True, "project_id": product_id}

    monkeypatch.setattr(
        "services.agent_workbench.backlog_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.backlog_phase.run_backlog_agent_from_state",
        fake_run_backlog_agent_from_state,
    )
    runner = BacklogPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.preview(project_id=2)

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    assert (
        result["errors"][0]["details"]["failure_stage"]
        == "brownfield_contract_validation"
    )
```

- [ ] **Step 2: Run failure envelope test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_backlog_phase.py::test_backlog_preview_surfaces_brownfield_contract_failure -q
```

Expected: PASS. If it fails, inspect `_backlog_runtime_error(...)`; do not change the CLI error code unless existing runtime failures use a different code.

- [ ] **Step 3: Run Backlog phase regression file**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_backlog_phase.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit Task 6**

```bash
git add tests/test_agent_workbench_backlog_phase.py
git commit -m "test: cover brownfield backlog failure envelope"
```

---

### Task 7: Focused Verification

**Files:**
- No code changes unless verification exposes a defect.

- [ ] **Step 1: Run all focused tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_backlog_primer_agent.py \
  tests/test_backlog_primer_prompt_contract.py \
  tests/test_roadmap_builder_schemas.py \
  tests/test_agent_workbench_backlog_phase.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run focused lint**

Run:

```bash
uv run --frozen ruff check \
  orchestrator_agent/agent_tools/backlog_primer/schemes.py \
  orchestrator_agent/agent_tools/backlog_primer/instructions.txt \
  orchestrator_agent/agent_tools/roadmap_builder/schemes.py \
  services/backlog_runtime.py \
  tests/test_backlog_primer_agent.py \
  tests/test_backlog_primer_prompt_contract.py \
  tests/test_roadmap_builder_schemas.py \
  tests/test_agent_workbench_backlog_phase.py
```

Expected: PASS. If Ruff refuses to parse `instructions.txt`, rerun without that file and report the skip.

- [ ] **Step 3: Run broader quality gate if available**

Run:

```bash
pyrepo-check --all
```

Expected: PASS. If `pyrepo-check` is not installed, report that explicitly and run:

```bash
uv run --frozen pytest -q
uv run --frozen ruff check .
```

- [ ] **Step 4: Verify working tree**

Run:

```bash
git status --short
```

Expected: no uncommitted files.

---

## Self-Review Notes

- Spec coverage:
  - Optional Backlog Primer metadata fields: Task 1.
  - Roadmap `extra="forbid"` compatibility: Task 2.
  - Prompt semantics for `requirement` vs `capability_name`: Task 3.
  - Input-aware brownfield validation against authoritative As-Built context: Tasks 4 and 5.
  - Omitted metadata with `requirement == capability_title`: Task 4.
  - Failure stage `brownfield_contract_validation`: Tasks 5 and 6.
  - No DB migration and no save gate: no task added.
- Deliberate deferrals:
  - Persisting metadata into story/task schemas.
  - Save-time fresh As-Built gate.
  - Multi-capability backlog item model.
  - Replacing the fixed 10+ item completeness rule.
