# Authority Curation Patch Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make targeted authority curation model output small patch operations that AgileForge applies deterministically, while improving failure artifacts for ADK invocation failures.

**Architecture:** ADK remains responsible for critique, repair planning, and patch suggestion. AgileForge service code owns structured authority mutation: load source authority, verify every patch target is feedback-approved, apply only allowed field changes, compute lineage, validate bounded diff, and publish candidate. Failure artifacts must include enough provider/ADK diagnostics to debug no-candidate failures.

**Tech Stack:** Python, SQLModel, Pydantic v2, ADK 2.0, pytest, existing mutation ledger and authority curation trace utilities.

---

### Task 1: Patch Output Schema

**Files:**
- Modify: `orchestrator_agent/agent_tools/authority_curation/schemes.py`
- Modify: `orchestrator_agent/agent_tools/authority_curation/agent.py`
- Test: `tests/test_authority_curation_agent.py`

- [x] Add failing tests proving `AuthorityCurationRepairOutput` accepts `patches` and no longer requires a full `candidate_authority_json`.
- [x] Add strict Pydantic patch models for `assumption`, `gap`, and `invariant` targets with `replace_text` operations.
- [x] Update repair compiler instruction to return patch operations, not a full authority copy.
- [x] Run `uv run pytest tests/test_authority_curation_agent.py -q`.

### Task 2: Deterministic Patch Applier

**Files:**
- Modify: `services/agent_workbench/authority_curation.py`
- Test: `tests/test_agent_workbench_authority_curation.py`

- [x] Add failing ASA-shaped tests for five-target feedback: assumptions `ASM-11`, `ASM-26`, `ASM-39`, `ASM-42` and invariant `INV-943d18f5ecffcd3c`.
- [x] Implement host-owned patch application against source authority JSON.
- [x] Reject missing or untargeted patches with fail-closed curation response.
- [x] Preserve unrelated authority content and produce lineage for changed invariant IDs.
- [x] Run focused curation tests.

### Task 3: Better ADK Failure Artifacts

**Files:**
- Modify: `services/agent_workbench/authority_curation.py`
- Test: `tests/test_agent_workbench_authority_curation.py`

- [x] Add failing test for an ADK invocation failure with no raw output.
- [x] Persist exception message, event count, partial output length, model id, requested model id, and trace artifact id when available.
- [x] Keep secrets and raw request bodies out of failure metadata.
- [x] Run focused failure artifact tests.

### Task 4: Verification

**Files:**
- Verify all touched tests.

- [x] Run `uv run pytest tests/test_authority_curation_agent.py tests/test_agent_workbench_authority_curation.py -q`.
- [x] Run `pyrepo-check --all`.
- [x] Review diff for unrelated changes.
