# Authority Review Human Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `agileforge authority review --format text` produce a concise human acceptance summary for issue #133 while preserving JSON output for automation.

**Architecture:** Keep the existing `AuthorityReviewService.review(..., output_format="text")` and CLI `--format text` plumbing. Enrich only the text renderer in `services/agent_workbench/authority_review.py`, deriving sections from the existing review packet fields.

**Tech Stack:** Python, pytest, AgileForge CLI, existing authority review service.

---

### Task 1: Add Service Text Summary Regression

**Files:**
- Modify: `tests/test_agent_workbench_authority_review.py`

- [ ] **Step 1: Write the failing test**

Add a test near `test_review_text_format_returns_ok_packet_with_human_text` that builds a pending authority with invariants, gaps, assumptions, rejected features, and authority quality groups, then calls:

```python
result = runner.review(project_id=project_id, output_format="text")
```

Assert the returned `data["text"]` contains:

```python
"Recommendation: accept"
"Preserved requirements:"
"two user-visible consent decisions"
"Gaps:"
"No blocking gaps found."
"Assumptions:"
"operator confirms export boundary"
"Excluded/non-current scope:"
"NON_GOAL"
"future training automation"
"Warnings:"
"duplicate/over-split"
"ACCEPT:"
"REJECT:"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_review.py -q -k "review_text"
```

Expected: the new test fails because the current text renderer does not include the human decision sections.

### Task 2: Add CLI Plain-Text Regression

**Files:**
- Modify: `tests/test_agent_workbench_cli.py`

- [ ] **Step 1: Write the failing test**

Add a CLI test that invokes the authority review command with `--format text` against an existing pending authority fixture and asserts stdout contains:

```python
"Authority review for project"
"Recommendation:"
"Preserved requirements:"
```

and does not start with a JSON object.

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_cli.py -q -k "authority_review"
```

Expected: the new assertion fails until the renderer emits the richer text summary.

### Task 3: Enrich Text Renderer

**Files:**
- Modify: `services/agent_workbench/authority_review.py`

- [ ] **Step 1: Implement minimal renderer helpers**

Add local helpers near `_render_review_text` to:

- read list fields from the review packet safely;
- extract short item text from dictionaries via `text`, `description`, `summary`, or `id`;
- cap list output to a small number of bullets;
- derive `Recommendation: accept` from `review_summary.acceptance_status == "accept_ready"` and `Recommendation: reject or resolve blocking findings` otherwise;
- summarize rejected features as excluded/non-current scope, preserving visible `NON_GOAL` and future-scope text when present;
- summarize duplicate/over-split warnings from `authority_quality.review_groups` and review summary quality counts.

- [ ] **Step 2: Preserve commands and guard context**

Keep the existing `ACCEPT:` and `REJECT:` command lines at the bottom so operators can immediately act on the review.

- [ ] **Step 3: Run focused tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_review.py -q -k "review_text"
uv run --frozen pytest tests/test_agent_workbench_cli.py -q -k "authority_review"
```

Expected: both focused test selections pass.

### Task 4: Verify and Commit

**Files:**
- Modified files from Tasks 1-3.

- [ ] **Step 1: Run focused authority suites**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_review.py -q -k "review"
uv run --frozen pytest tests/test_agent_workbench_cli.py -q -k "authority"
```

Expected: both pass.

- [ ] **Step 2: Run final gate**

Run:

```bash
pyrepo-check --all
```

Expected: all checks pass.

- [ ] **Step 3: Commit**

Run:

```bash
git add services/agent_workbench/authority_review.py tests/test_agent_workbench_authority_review.py tests/test_agent_workbench_cli.py docs/superpowers/plans/2026-06-13-authority-review-human-summary.md
git commit -m "fix(authority): summarize review decisions in text output"
```

Expected: one commit on `dev/authority-review-human-summary`.

---

## Self-Review

- Spec coverage: issue #133 requirements are covered through the service text renderer and CLI text output path.
- Placeholder scan: no `TBD`, `TODO`, or unresolved steps remain.
- Scope check: JSON output remains unchanged; this plan does not alter authority compilation, acceptance guards, or default CLI format.
