# Story Draft Quality Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic Story draft quality contract so incomplete or low-quality bounded story attempts cannot become saveable drafts.

**Architecture:** Extend the Story writer output schema with quality metadata, then evaluate that metadata in `services/story_runtime.py` before marking an attempt reusable. Keep save/review gating in `services/phases/story_service.py`, and surface the resulting quality summary through existing Story generate/retry responses.

**Tech Stack:** Python, Pydantic, pytest, existing AgileForge Story runtime and phase services.

---

### Task 1: Extend Story Writer Schema

**Files:**
- Modify: `/Users/aaat/projects/agileforge/orchestrator_agent/agent_tools/user_story_writer_tool/schemes.py`
- Modify: `/Users/aaat/projects/agileforge/orchestrator_agent/agent_tools/user_story_writer_tool/instructions.txt`
- Test: `/Users/aaat/projects/agileforge/tests/test_user_story_writer_schemas.py`

- [ ] Add `StoryQualityFinding` with fields `code`, `severity`, `message`, `affected_story_indexes`, and `affected_story_titles`.
- [ ] Add `research_caveats: list[str]` to `UserStoryItem`.
- [ ] Add `quality_schema_version`, `coverage_status`, `remaining_scope`, and `quality_findings` to `UserStoryWriterOutput`.
- [ ] Update tests proving research caveats do not force Low INVEST score.
- [ ] Update prompt instructions so `decomposition_warning` means decomposition failure and `research_caveats` means advisory uncertainty.

### Task 2: Add Runtime Quality Evaluation

**Files:**
- Modify: `/Users/aaat/projects/agileforge/services/story_runtime.py`
- Test: `/Users/aaat/projects/agileforge/tests/test_story_runtime.py`

- [ ] Add deterministic helpers to compute `story_count`, `invest_score_counts`, and blocking findings.
- [ ] Parse explicit requested story count from the request payload text.
- [ ] Mark a complete all-Low draft as `quality_gate_failed`, `is_reusable=false`, and `draft_kind="quality_blocked_draft"`.
- [ ] Mark a complete capped draft as non-reusable when the request asked for more than the per-attempt cap.
- [ ] Preserve schema and clarification failures as existing nonreusable schema failures.

### Task 3: Gate Save/Review Eligibility

**Files:**
- Modify: `/Users/aaat/projects/agileforge/services/phases/story_service.py`
- Test: `/Users/aaat/projects/agileforge/tests/test_story_phase_service.py`

- [ ] Add `story_quality_summary()` and `story_quality_saveable()` helpers.
- [ ] Make `story_save_payload()` require quality-saveable artifacts.
- [ ] Make `story_interview_summary()` return top-level `attempt_id`, `artifact_fingerprint`, `story_count`, `invest_score_counts`, `is_reusable`, and `quality`.
- [ ] Ensure quality-blocked drafts remain in `STORY_INTERVIEW` and do not expose save guards.

### Task 4: CLI/API Compatibility Check

**Files:**
- Test: `/Users/aaat/projects/agileforge/tests/test_agent_workbench_story_phase.py`
- Test: `/Users/aaat/projects/agileforge/tests/test_story_phase_service.py`

- [ ] Verify existing nested `data.current_draft` and `data.save` fields remain.
- [ ] Verify generate/retry response summaries contain the new top-level guard and quality fields inside `data`.
- [ ] Verify save still requires `attempt_id`, `expected_artifact_fingerprint`, `expected_state`, and `idempotency_key`.

### Task 5: Validation

**Commands:**

- [ ] Run schema tests:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest tests/test_user_story_writer_schemas.py -q
```

- [ ] Run runtime and phase tests:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest tests/test_story_runtime.py tests/test_story_phase_service.py -q
```

- [ ] Run changed-file lint:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen ruff check orchestrator_agent/agent_tools/user_story_writer_tool/schemes.py services/story_runtime.py services/phases/story_service.py tests/test_user_story_writer_schemas.py tests/test_story_runtime.py tests/test_story_phase_service.py
```

- [ ] Run whitespace check:

```bash
git diff --check
```
