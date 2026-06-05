# Decision Hard-Ban Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Filter non-normative `DECISION`-sourced hard bans during authority normalization so project create can reach compiled authority review without weakening metadata validation.

**Architecture:** Add one host-side normalizer pass before structured metadata validation. The pass removes only `FORBIDDEN_CAPABILITY` invariants whose known source evidence is exclusively `DECISION` items with no level, then prunes matching `source_map` entries and records a bounded assumption.

**Tech Stack:** Python 3.13, Pydantic models in `utils/spec_schemas.py`, pytest, existing AgileForge normalizer helpers.

---

## File Structure

- Modify `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`
  - Add a constant assumption message.
  - Add helper functions to detect and filter non-normative `DECISION` hard bans.
  - Call the helper before `_structured_authority_metadata_errors(...)`.

- Modify `tests/test_spec_authority_compiler_normalizer.py`
  - Add RED regression for `FORBIDDEN_CAPABILITY` sourced from `DECISION.*` with `level=None`.
  - Keep existing over-promotion failure tests unchanged.

---

### Task 1: Add Regression Test

**Files:**
- Modify: `tests/test_spec_authority_compiler_normalizer.py`

- [ ] **Step 1: Add failing test**

Append this test near the existing `SOURCE_METADATA_MISMATCH` tests:

```python
def test_normalizer_filters_non_normative_decision_hard_ban() -> None:
    """DECISION rationale must not become a hard forbidden authority invariant."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )
    from utils.agileforge_spec_profile import (  # noqa: PLC0415
        TechnicalSpecArtifact,
        canonical_spec_json,
    )

    source_text = canonical_spec_json(
        TechnicalSpecArtifact.model_validate(
            {
                "schema_version": "agileforge.spec.v1",
                "artifact_id": "SPEC.decision-filter",
                "title": "Decision Filter Spec",
                "status": "draft",
                "version": "0.1",
                "created_at": "2026-06-05",
                "updated_at": "2026-06-05",
                "summary": "Exercise decision filtering.",
                "problem_statement": "Research decisions should not become hard bans.",
                "items": [
                    {
                        "id": "DECISION.research-before-algorithm",
                        "type": "DECISION",
                        "status": "accepted",
                        "title": "Research before algorithm",
                        "statement": (
                            "Research the best model and stack before deciding "
                            "the final algorithm."
                        ),
                    },
                    {
                        "id": "REQ.include-review-token",
                        "type": "REQ",
                        "status": "accepted",
                        "title": "Review token",
                        "statement": "The system MUST include review token evidence.",
                        "level": "MUST",
                        "verification": "inspection",
                        "acceptance": [
                            "Review packets include review token evidence."
                        ],
                    },
                ],
            }
        )
    )
    raw: dict[str, Any] = {
        "scope_themes": ["research safety"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-1111111111111111",
                "type": "FORBIDDEN_CAPABILITY",
                "parameters": {
                    "capability": "final algorithm selection before research"
                },
            },
            {
                "id": "INV-2222222222222222",
                "type": "REQUIRED_FIELD",
                "parameters": {
                    "source_item_id": "REQ.include-review-token",
                    "source_level": "MUST",
                    "field_name": "review token evidence",
                },
            },
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [
            {
                "invariant_id": "INV-1111111111111111",
                "excerpt": (
                    "Research the best model and stack before deciding "
                    "the final algorithm."
                ),
                "location": "DECISION.research-before-algorithm.statement",
            },
            {
                "invariant_id": "INV-2222222222222222",
                "excerpt": "The system MUST include review token evidence.",
                "location": "REQ.include-review-token.statement",
            },
        ],
        "compiler_version": "1.0.0",
        "prompt_hash": "0" * 64,
    }

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=source_text,
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert [invariant.type for invariant in normalized.root.invariants] == [
        InvariantType.REQUIRED_FIELD
    ]
    assert all(
        "DECISION.research-before-algorithm" not in (entry.location or "")
        for entry in normalized.root.source_map
    )
    assert normalized.root.assumptions.count(
        "Excluded non-normative DECISION item from hard forbidden authority."
    ) == 1
```

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py::test_normalizer_filters_non_normative_decision_hard_ban -q
```

Expected: fail with `SOURCE_METADATA_MISMATCH`.

- [ ] **Step 3: Commit test only is not needed**

Do not commit at RED. Continue to Task 2.

---

### Task 2: Implement Narrow Filter

**Files:**
- Modify: `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`

- [ ] **Step 1: Add helper code**

Add this constant near the existing assumption constants:

```python
_NON_NORMATIVE_DECISION_ASSUMPTION = (
    "Excluded non-normative DECISION item from hard forbidden authority."
)
```

Add this helper before `_structured_authority_metadata_errors`:

```python
def _filter_non_normative_decision_hard_bans(
    success: SpecAuthorityCompilationSuccess,
    *,
    source_text: str,
) -> int:
    """Remove hard bans sourced only from non-normative DECISION items."""
    source_items = _structured_profile_items_by_id(source_text)
    if not source_items or not success.invariants:
        return 0

    source_map_ids = _source_map_item_ids_by_invariant(success)
    removed_ids: set[str] = set()
    kept_invariants: list[Invariant] = []

    for invariant in success.invariants:
        if invariant.type != InvariantType.FORBIDDEN_CAPABILITY:
            kept_invariants.append(invariant)
            continue

        source_item_ids = source_map_ids.get(invariant.id, set())
        known_items = [
            source_items[source_item_id]
            for source_item_id in source_item_ids
            if source_item_id in source_items
        ]
        if known_items and all(
            item.get("type") == "DECISION" and item.get("level") is None
            for item in known_items
        ):
            removed_ids.add(invariant.id)
            continue

        kept_invariants.append(invariant)

    if not removed_ids:
        return 0

    success.invariants = kept_invariants
    success.source_map = [
        entry for entry in success.source_map if entry.invariant_id not in removed_ids
    ]
    if _NON_NORMATIVE_DECISION_ASSUMPTION not in success.assumptions:
        success.assumptions.append(_NON_NORMATIVE_DECISION_ASSUMPTION)
    return len(removed_ids)
```

- [ ] **Step 2: Call helper before metadata validation**

In `normalize_compiler_output`, before `_structured_authority_metadata_errors(...)`, add:

```python
        _filter_non_normative_decision_hard_bans(
            success,
            source_text=source_text,
        )
```

- [ ] **Step 3: Verify GREEN**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py::test_normalizer_filters_non_normative_decision_hard_ban -q
```

Expected: pass.

- [ ] **Step 4: Verify adjacent behavior**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py tests/test_spec_authority_compiler_normalizer.py
git commit -m "fix: filter non-normative decision hard bans"
```

---

### Task 3: Failure Artifact Smoke

**Files:**
- No source edits unless smoke exposes a bug.

- [ ] **Step 1: Replay saved artifact**

Run:

```bash
uv run --frozen python - <<'PY'
import json
from pathlib import Path

from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (
    normalize_compiler_output,
)
from utils.spec_schemas import SpecAuthorityCompilationSuccess

artifact = json.loads(
    Path(
        "/Users/aaat/projects/agileforge/logs/failures/spec_authority/"
        "spec_authority-20260605T175631516140Z-9fbbf99c95ac.json"
    ).read_text()
)
raw = artifact.get("raw_output") or artifact.get("raw_output_preview") or ""
source = Path(
    "/Users/aaat/projects/asa-deep-process-control-experiments/specs/spec.json"
).read_text()
normalized = normalize_compiler_output(
    raw,
    source_text=source,
    source_format="agileforge.spec.v1",
)
print("root_type", type(normalized.root).__name__)
if isinstance(normalized.root, SpecAuthorityCompilationSuccess):
    inv_ids = [invariant.id for invariant in normalized.root.invariants]
    source_ids = [entry.invariant_id for entry in normalized.root.source_map]
    print("invariant_count", len(inv_ids))
    print("unique_invariant_ids", len(set(inv_ids)))
    print("source_map_count", len(source_ids))
    print("source_map_unknown_refs", len(set(source_ids) - set(inv_ids)))
else:
    print("failure_reason", normalized.root.reason)
    for gap in normalized.root.blocking_gaps[:3]:
        print("gap", gap[:300])
PY
```

Expected: `root_type SpecAuthorityCompilationSuccess`, no unknown refs.

- [ ] **Step 2: Run lint and diff check**

Run:

```bash
uv run --frozen ruff check orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py tests/test_spec_authority_compiler_normalizer.py
git diff --check
```

Expected: both pass.

---

## Self-Review

- Spec coverage: plan implements only non-normative `DECISION` hard-ban filtering.
- Guard preservation: existing metadata validation remains in place.
- No placeholders: every code block is concrete.
- Scope: no ASA spec edits, no CLI mutation, no workflow continuation.
