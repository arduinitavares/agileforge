# Story Sentinel Validation Implementation Plan

> **For agentic workers:** Use `superpowers:executing-plans` or
> `superpowers:subagent-driven-development` to execute this plan.

**Goal:** Prevent exact authoring-sentinel Story prose from becoming a complete,
acceptable candidate while preserving substantive prose, immutable identities,
and the current accepted Story set.

**Architecture:** Strict `canonicalize_story_items` rejects sentinel-only
provider output and every new or accepted candidate. A separate, named
review-inspection path revalidates immutable pre-rule artifacts against their
exact bytes, fingerprint, host item sequence, parent evidence, and lineage,
then returns deterministic sentinel field paths. Read surfaces project only a
sanitized unavailable candidate plus its actionable review binding. Acceptance
stays blocked; feedback and rejection remain available.

**Spec:** GitHub issue #229.

## Constraints

- Start from exact SHA `f28a76962d9cc30a58d53bf9f49045e476f01459`.
- Do not access or mutate the preserved #228 evidence profile.
- Reject only normalized whole-field matches from the explicit sentinel set.
- Do not rewrite valid bytes, fingerprints, identities, or authority guards.
- Use deterministic synthetic fixtures and no provider calls.
- Commit locally only; do not push, merge, close the issue, or run human
  acceptance.

## Task 1: Define and prove the domain rule

**Files:**

- `services/contracts/story.py`
- `adapters/adk/recipes.py`
- `tests/services/contracts/test_story.py`
- `tests/adapters/test_adk_workflow_runner.py`
- `tests/test_story_runtime.py`

- [x] Add RED cases for `placeholder`, `TBD`, `TODO`, `to be determined`,
  `N/A`, and `not applicable`, including case, whitespace, and wrapper
  normalization.
- [x] Add the exact three-Story provider fixture: third title and all twelve
  INVEST rationale/evidence fields contain `placeholder`.
- [x] Prove meaningful prose merely containing `placeholder` remains valid.
- [x] Add `StorySentinelContentError.fields` and
  `is_story_sentinel_text(value)`.
- [x] Enforce the rule once inside `canonicalize_story_items`, before host IDs
  and fingerprints are minted.
- [x] Verify bounded runtime repair fails with `output_validation`, no reusable
  output artifact, and thirteen `story_items[2]...` paths.
- [x] Strip nested Markdown-style authoring wrappers without broadening matches
  to substantive prose.
- [x] Persist only safe field paths for sentinel failures; omit raw provider
  output and its preview.
- [x] Pre-scan parsed and partial agent output so sentinel prose that fails
  earlier Pydantic validation also omits raw output, validation inputs,
  exception text, and traceback.
- [x] Scan prose-wrapped partial JSON using only the `user_stories` key so a
  truncated response missing `is_complete` cannot bypass sanitization.
- [x] Enforce the same pre-Pydantic scan in the live
  `planning.story.generate` recipe; keep raw provider mappings out of repair
  prompts, exception chains, and durable node-attempt failures.
- [x] Apply the live pre-scan to parseable embedded/truncated leaf text before
  JSON validation can echo the entire response.
- [x] Normalize every Story provider-output Pydantic failure to fixed schema
  paths and bounded error codes; never serialize provider `input`, raw output,
  or a raw exception cause into repair feedback, logs, runtime artifacts, or
  durable failures.
- [x] Make unclassified `AgentInvocationError` failures generic and terminal;
  never persist their partial provider output or exception cause.
- [x] Convert provider-owned Specification reference failures to exact
  `story_items[i].spec_item_ids` paths and bounded error types before repair,
  logging, runtime artifacts, or durable failure persistence.
- [x] Recover fixed-schema paths from real item-level Pydantic mapping/list
  inputs when no partial output is available.

## Task 2: Preserve persistence and read authorities

**Files:**

- `services/read_projections.py`
- `tests/test_create_user_story.py`
- `tests/services/test_durable_product_definition_projections.py`

- [x] Seed and accept artifact A, then prove sentinel successor B is rejected
  before insertion.
- [x] Prove A's canonical bytes, fingerprint, active operational rows, and
  non-superseded state remain unchanged.
- [x] Simulate a pre-fix persisted sentinel artifact with internally consistent
  fingerprints.
- [x] Revalidate pre-rule sentinel bytes structurally, then project an
  actionable review with `candidate_available: false`, safe `invalid_fields`,
  an empty Story item list, and no provider values.
- [x] Allow only feedback and rejection on that recovery projection; strict
  acceptance still raises before activation.
- [x] Prove read validation does not rewrite stored bytes.
- [x] Re-run existing feedback, rejection, activation-rollback, and replay
  preservation tests.

## Task 3: Keep API, CLI, and browser parity

**Files:**

- `cli/main.py`
- `frontend/project.js`
- `tests/adapters/test_api_workflow_domain.py`
- `tests/adapters/test_cli_workflow_domain.py`
- `tests/test_workflow_position_display.mjs`
- `tests/e2e/test_single_project_lifecycle_ui.py`

- [x] Preserve the structured API envelope and exact machine binding for the
  sanitized recovery review.
- [x] Make CLI acceptance require a present, explicitly complete candidate,
  zero clarifying questions, substantive required Story fields, and valid
  INVEST evidence.
- [x] Apply the same acceptance semantics in the browser.
- [x] Surface deterministic sentinel field paths without raw values.
- [x] Keep feedback and rejection enabled.
- [x] Cover exact sentinels, explicit incompleteness, substantive negatives,
  and real Playwright interaction.

## Task 4: Review, verify, and commit locally

- [x] Run focused domain, runtime, persistence, API, CLI, JavaScript, and
  Playwright suites.
- [x] Obtain an independent correctness review with GPT-5.6 Sol at `xhigh`.
- [x] Obtain an independent lean-scope review with GPT-5.6 Terra at `medium`.
- [x] Resolve the verified recovery, wrapper-normalization, raw-output,
  pre-Pydantic/agent-error, and live-recipe durable sanitization findings;
  re-run affected focused tests.
- [x] Re-review the revised diff independently for correctness and lean scope.
- [x] Inspect the exact base-to-working-tree diff and secret patterns.
- [ ] Create one local atomic commit.
- [ ] Run `./agileforge-dev check --json` from the clean committed checkout.
- [ ] Report exact worktree, branch, start/final SHA, verification, data
  boundaries, and prohibited actions not performed.
