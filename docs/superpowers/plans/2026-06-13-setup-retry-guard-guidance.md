# Setup Retry Guard Guidance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make failed setup retry guidance copy-paste valid and make stale fingerprint errors return a corrected retry action.

**Architecture:** Reuse the existing `setup_retry_context_fingerprint()` contract. `workflow next` will derive the failed setup spec path from workflow state and render a retry command with a concrete fingerprint. Retry guard failures will reuse the existing structured `next_actions` pattern with the actual fingerprint substituted into the retry request.

**Tech Stack:** Python, pytest, SQLModel-backed mutation ledger tests, AgileForge CLI/workbench service layer.

---

### Task 1: Workflow Next Publishes Concrete Setup Retry Guards

**Files:**
- Modify: `tests/test_agent_workbench_application.py`
- Modify: `services/agent_workbench/application.py`

- [ ] **Step 1: Write the failing test**

Add a test that builds a failed setup workflow state with `setup_spec_file_path` pointing to a real structured spec file. Assert that `workflow_next()` emits a concrete `project setup retry` command containing the resolved spec path and a `sha256:` fingerprint, with no placeholder tokens.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "failed_setup"`

Expected: FAIL because the command still includes `<spec-file>` and `<expected_context_fingerprint>`.

- [ ] **Step 3: Write minimal implementation**

In `services/agent_workbench/application.py`, import `Path`, `SpecContentNormalizationError`, and `setup_retry_context_fingerprint`. Add a small helper that reads `setup_spec_file_path` from workflow state, resolves it, computes the retry fingerprint, and returns a concrete command. If the spec path is missing or unreadable, keep the command blocked with a concrete reason instead of claiming it is runnable.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "failed_setup"`

Expected: PASS.

### Task 2: Stale Fingerprint Errors Return Corrected Retry Action

**Files:**
- Modify: `tests/test_agent_workbench_project_setup.py`
- Modify: `services/agent_workbench/project_setup.py`

- [ ] **Step 1: Write the failing test**

Extend the stale context retry test to assert that `STALE_CONTEXT_FINGERPRINT` includes `data.next_actions[0].args.expected_context_fingerprint` equal to `details.actual_context_fingerprint`, preserving the other retry args.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen pytest tests/test_agent_workbench_project_setup.py -q -k "stale_state_and_context"`

Expected: FAIL because stale fingerprint errors currently return only generic remediation and no corrected next action.

- [ ] **Step 3: Write minimal implementation**

In `services/agent_workbench/project_setup.py`, add a helper that creates a corrected `ProjectSetupRetryRequest` with `expected_context_fingerprint` set to the current fingerprint, then returns `_retry_action()` in `data.next_actions`. Use it for both dry-run stale context rejection and persisted guard rejection.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --frozen pytest tests/test_agent_workbench_project_setup.py -q -k "stale_state_and_context"`

Expected: PASS.

### Task 3: Focused and Full Verification

**Files:**
- Verify only.

- [ ] **Step 1: Run focused setup/workflow tests**

Run:
`uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "failed_setup or setup_retry"`
`uv run --frozen pytest tests/test_agent_workbench_project_setup.py -q -k "setup_retry or compiler"`
`uv run --frozen pytest tests/test_agent_workbench_cli.py -q -k "setup_retry"`

- [ ] **Step 2: Run final quality gate**

Run: `pyrepo-check --all`

- [ ] **Step 3: Commit**

Commit message: `fix(setup): publish runnable retry guards`
