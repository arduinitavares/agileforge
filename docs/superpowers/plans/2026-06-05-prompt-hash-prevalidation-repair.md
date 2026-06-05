# Prompt Hash Prevalidation Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair invalid compiler-emitted `prompt_hash` before strict authority schema validation.

**Architecture:** Add one pre-validation normalizer helper beside the existing invalid invariant ID repair. The helper rewrites missing or malformed `prompt_hash` values on success-shaped payloads to the canonical hash computed from `SPEC_AUTHORITY_COMPILER_INSTRUCTIONS`, including envelope `result` payloads, then leaves final strict validation unchanged.

**Tech Stack:** Python 3.13, Pydantic, pytest, existing AgileForge authority normalizer.

---

## File Structure

- Modify `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`
  - Add `_PROMPT_HASH_RE`.
  - Add `_repair_invalid_prompt_hash_for_validation(payload)`.
  - Call it before `SpecAuthorityCompilerOutput.model_validate(...)`.

- Modify `tests/test_spec_authority_compiler_normalizer.py`
  - Add regression tests for invalid direct and envelope `prompt_hash` values.

---

### Task 1: Add Prompt Hash Regression Tests

**Files:**
- Modify: `tests/test_spec_authority_compiler_normalizer.py`

- [ ] **Step 1: Add direct-payload failing test**

Add near `test_normalizer_rewrites_bad_ids_and_prompt_hash`:

```python
def test_normalizer_repairs_invalid_prompt_hash_before_validation() -> None:
    """Invalid prompt_hash should be repaired before strict schema validation."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.instructions_source import (  # noqa: E501, PLC0415
        SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
    )
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw: dict[str, Any] = {
        "scope_themes": ["payload validation"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "user_id"},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [],
        "compiler_version": "1.0.0",
        "prompt_hash": "not-a-valid-hash",
    }

    normalized = normalize_compiler_output(json.dumps(raw))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.prompt_hash == compute_prompt_hash(
        SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
    )
```

- [ ] **Step 2: Add envelope failing test**

Add:

```python
def test_normalizer_repairs_invalid_envelope_prompt_hash_before_validation() -> None:
    """Envelope result prompt_hash should be repaired before validation."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.instructions_source import (  # noqa: E501, PLC0415
        SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
    )
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    result_payload: dict[str, Any] = {
        "scope_themes": ["payload validation"],
        "domain": None,
        "invariants": [
            {
                "id": "INV-0000000000000000",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "user_id"},
            }
        ],
        "eligible_feature_rules": [],
        "gaps": [],
        "assumptions": [],
        "source_map": [],
        "compiler_version": "1.0.0",
        "prompt_hash": "",
    }

    normalized = normalize_compiler_output(json.dumps({"result": result_payload}))

    assert isinstance(normalized.root, SpecAuthorityCompilationSuccess)
    assert normalized.root.prompt_hash == compute_prompt_hash(
        SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
    )
```

- [ ] **Step 3: Verify RED**

Run:

```bash
uv run --frozen pytest \
  tests/test_spec_authority_compiler_normalizer.py::test_normalizer_repairs_invalid_prompt_hash_before_validation \
  tests/test_spec_authority_compiler_normalizer.py::test_normalizer_repairs_invalid_envelope_prompt_hash_before_validation \
  -q
```

Expected: fail with `JSON_VALIDATION_FAILED` or assertion failure caused by strict `prompt_hash` validation.

---

### Task 2: Implement Pre-Validation Repair

**Files:**
- Modify: `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`

- [ ] **Step 1: Add regex and helper**

Add near existing regex constants:

```python
_PROMPT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
```

Add after `_drop_deprecated_compact_ir_for_success_payload`:

```python
def _repair_invalid_prompt_hash_for_validation(payload: object) -> None:
    """Repair invalid prompt_hash before strict success schema validation."""
    if not isinstance(payload, dict):
        return

    payload_dict = cast("dict[str, Any]", payload)
    result = payload_dict.get("result")
    if isinstance(result, dict):
        _repair_invalid_prompt_hash_for_validation(result)

    if "error" in payload_dict:
        return
    if not _SUCCESS_REQUIRED_KEYS_EXCEPT_SOURCE_MAP.issubset(payload_dict):
        return

    prompt_hash = payload_dict.get("prompt_hash")
    if isinstance(prompt_hash, str) and _PROMPT_HASH_RE.fullmatch(prompt_hash):
        return

    payload_dict["prompt_hash"] = compute_prompt_hash(
        SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
    )
```

- [ ] **Step 2: Call helper before validation**

In `normalize_compiler_output`, after `_default_missing_source_map_for_success_payload(payload)` and before `_repair_invalid_invariant_ids_for_validation(payload)`, add:

```python
    _repair_invalid_prompt_hash_for_validation(payload)
```

- [ ] **Step 3: Verify GREEN**

Run:

```bash
uv run --frozen pytest \
  tests/test_spec_authority_compiler_normalizer.py::test_normalizer_repairs_invalid_prompt_hash_before_validation \
  tests/test_spec_authority_compiler_normalizer.py::test_normalizer_repairs_invalid_envelope_prompt_hash_before_validation \
  -q
```

Expected: pass.

- [ ] **Step 4: Verify normalizer suite**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py tests/test_spec_authority_compiler_normalizer.py
git commit -m "fix: repair prompt hash before authority validation"
```

---

### Task 3: Failure Artifact Smoke And Quality Checks

**Files:**
- No source edits unless smoke exposes a defect.

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
        "spec_authority-20260605T182850987545Z-79c7a3f1e324.json"
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

Expected: no `JSON_VALIDATION_FAILED` for `prompt_hash`. Success is preferred; a later semantic validation blocker is acceptable.

- [ ] **Step 2: Run adjacent suite and lint**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py tests/test_spec_authority_compiler_agent.py -q
uv run --frozen ruff check orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py tests/test_spec_authority_compiler_normalizer.py
git diff --check
```

Expected: all pass.

---

## Self-Review

- Scope covers only prompt hash pre-validation repair.
- Final strict schema remains active.
- No CLI, DB, ASA spec, or workflow mutation.
- Plan contains concrete tests and commands.
