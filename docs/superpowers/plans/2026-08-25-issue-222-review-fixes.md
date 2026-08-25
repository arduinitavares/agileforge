# Issue 222 Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Story sizing and ordering rationales mandatory and visible, distinguish local Story order from derived rank, and prove that precise human feedback creates an exact superseding candidate with preserved lineage.

**Architecture:** Keep planning recommendations inside the immutable canonical Story item so fingerprints bind rationales with content, and project those fields through the existing review API into CLI and browser views. Reuse the existing feedback lineage and ADK recipe path for revisions; prove the complete provider-free flow with a deterministic typed leaf rather than manually inserting the replacement artifact.

**Tech Stack:** Python 3.13, Pydantic, SQLModel, Google ADK workflows, vanilla JavaScript, Node test runner, pytest, uv, pyrepo-check.

**Spec:** `docs/feedback/2026-08-24-story-refinement-to-sprint-selection-design-handoff.md`

## Global Constraints

- No provider-backed generation, Sprint generation, manual acceptance, merge, push, or issue closure.
- Preserve exact lineage, freshness, idempotency, stale-action, and duplicate-submission guards.
- Keep issue #222 separate from #223 readiness/Sprint selection and #224 solo terminology/defaults.
- Use only the linked worktree `dev/issue-222-human-owned-story-refinement` and uv-based commands.

---

### Task 1: Bind sizing and ordering rationales into the canonical Story contract

**Files:**
- Modify: `services/contracts/story.py`
- Modify: `services/packets/canonical.py`
- Modify: `adapters/adk/prompts/story.txt`
- Modify: `adapters/adk/prompts/story_patch.txt`
- Modify: `docs/superpowers/specs/2026-08-21-accepted-specification-delivery-contract-design.md`
- Test: `tests/services/contracts/test_story.py`
- Test: `tests/adapters/test_story.py`
- Test: `tests/workflow/test_single_project_graph.py`

**Interfaces:**
- Consumes: `UserStoryAgentItem`, `CanonicalStoryItem`, and `canonicalize_story_items(...)`.
- Produces: required non-blank `effort_rationale: str` and `order_rationale: str` fields included in canonical item fingerprints and packet validation.

- [x] **Step 1: Add failing contract tests**

  Extend the valid Story fixture with literal rationales, then assert `UserStoryAgentItem.model_validate(...)` rejects each missing and whitespace-only rationale independently.

- [x] **Step 2: Verify the contract tests fail for the missing fields**

  Run: `uv run --frozen pytest -q tests/services/contracts/test_story.py -k rationale`

  Expected before implementation: validation does not require both rationale fields.

- [x] **Step 3: Add the minimal canonical fields and validation**

  Add both rationale fields to provider and canonical Story models, reuse the existing non-blank text validator, copy them during canonicalization, and include them in `_STORY_ITEM_SHAPE`.

- [x] **Step 4: Require rationale and exact feedback semantics in both Story prompts**

  State that effort/order rationales are distinct from INVEST evidence and that exact `user_input` sizing, ordering, dependency, and criteria adjustments must appear in the revised proposal.

- [x] **Step 5: Verify the focused contract and prompt tests**

  Run: `uv run --frozen pytest -q tests/services/contracts/test_story.py tests/adapters/test_story.py`

  Expected: all selected tests pass.

### Task 2: Prove the precise feedback replacement path

**Files:**
- Modify: `tests/adapters/test_adk_workflow_runner.py`
- Delete: `tests/services/test_story_feedback_refinement.py`

**Interfaces:**
- Consumes: `DeliveryActionInputService.build(...)`, `AdkWorkflowRunner.run(...)`, `DecideStory`, and the production `planning.story.generate` recipe.
- Produces: provider-free evidence that feedback text reaches `UserStoryWriterInput.user_input`, the revised provider result is persisted as version 2, and `supersedes_story_artifact_id` points to version 1.

- [x] **Step 1: Replace the manual-persistence test with a deterministic provider-free model**

  Use the production Story agent with a provider-free sequence model that records each request and returns two literal typed outputs: first `Story B, Story A` with Story A sized `M` and dependent on X, then `Story A, Story B` with Story A sized `S` and all dependencies removed.

- [x] **Step 2: Prove the production input boundary**

  Assert the production action-input builder includes the prior canonical artifact plus the exact persisted review outcome and rationale, and that the second provider request receives that same `user_input` byte-for-byte.

- [x] **Step 3: Drive the real feedback transition and successor generation**

  Run the first generation through `AdkWorkflowRunner`, submit `DecideStory(decision="feedback")` with the literal request `Change Story A effort to S, move Story A before Story B, and remove dependency X.`, rebuild the production action input, and run the successor with a new idempotency key.

- [x] **Step 4: Assert exact revised content and lineage**

  Assert persisted artifacts have versions `[1, 2]`, version 2 supersedes version 1, Story A is first with effort `S`, Story B is second, both dependency lists are empty, the feedback decision remains bound to version 1, and no operational `UserStory` exists before acceptance.

- [x] **Step 5: Remove the bypass test and verify the real runner test**

  Delete `tests/services/test_story_feedback_refinement.py` because it manually records the replacement and cannot fail when feedback generation is broken.

  Run: `uv run --frozen pytest -q tests/adapters/test_adk_workflow_runner.py -k 'precise_feedback or story_runner_valid_first'`

  Expected: both tests pass without a provider call.

### Task 3: Expose unambiguous planning evidence in CLI and browser review

**Files:**
- Modify: `cli/main.py`
- Modify: `frontend/project.js`
- Test: `tests/adapters/test_cli_workflow_domain.py`
- Test: `tests/test_workflow_position_display.mjs`
- Test: `tests/services/test_durable_product_definition_projections.py`

**Interfaces:**
- Consumes: Story review projection fields `order`, `rank`, `estimated_effort`, `story_points`, `effort_rationale`, and `order_rationale`.
- Produces: `Story order within PBI`, `Derived rank`, `Effort rationale`, and `Order rationale` labels in both human review surfaces.

- [x] **Step 1: Add rendering assertions for exact labels and rationale values**

  Assert CLI and browser markup render the local ordinal separately from the derived global rank and display both rationale strings.

- [x] **Step 2: Verify rendering assertions fail with the ambiguous labels**

  Run: `uv run --frozen pytest -q tests/adapters/test_cli_workflow_domain.py -k story_item_lines && node --test tests/test_workflow_position_display.mjs`

  Expected before implementation: assertions fail on `Backlog order` / `Rank` and missing rationales.

- [x] **Step 3: Implement the minimal label and rationale rendering changes**

  Keep values and APIs unchanged; change only presentation labels and render the two canonical rationale fields.

- [x] **Step 4: Fail closed on missing rationale evidence**

  Keep feedback and rejection available, but disable acceptance in the CLI and browser when INVEST, sizing-rationale, or ordering-rationale evidence is absent or malformed. Use one accurate diagnostic for all three evidence classes.

- [x] **Step 5: Verify projection and rendering suites**

  Run: `uv run --frozen pytest -q tests/adapters/test_cli_workflow_domain.py tests/services/test_durable_product_definition_projections.py && node --test tests/test_workflow_position_display.mjs`

  Expected: all selected tests pass.

### Task 4: Verify and checkpoint the complete review fix

**Files:**
- Review: all paths changed from `51af3fc67b13182c549deab3951e2d60fe73910a`

**Interfaces:**
- Consumes: the complete issue #222 working diff.
- Produces: one clean, reviewable commit with provider-free verification evidence.

- [x] **Step 1: Run syntax and whitespace checks**

  Run: `git diff --check`

  Expected: no output and exit 0.

- [x] **Step 2: Run all changed focused tests**

  Run the Python test files affected by the new required fields plus `node --test tests/test_workflow_position_display.mjs`.

  Expected: zero failures.

- [x] **Step 3: Run the full provider-free gate**

  Run: `uv run --frozen pyrepo-check --all`

  Expected: exit 0 with pytest, Ruff, annotations, Ty, Bandit, and launcher checks green.

- [x] **Step 4: Review the final diff and protected boundaries**

  Confirm no profile database, provider configuration, readiness/Sprint-selection behavior, solo ownership behavior, or manual-acceptance state changed.

- [x] **Step 5: Commit the verified fix**

  Run: `git add <exact changed paths> && git commit -m "fix(story): require exact planning refinement evidence (#222)"`
