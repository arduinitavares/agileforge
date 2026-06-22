# AgileForge Platform Feedback Triage

Date: 2026-06-22

AgileForge HEAD: `45f8d69`

ASA repo: `/Users/aaat/projects/asa-deep-process-control-experiments`

ASA HEAD: `aafafd8`

Project: `3`

Purpose: issue-ready platform triage before opening GitHub issues. ASA is only
the reproduction fixture. These findings must be fixed generically for all
AgileForge projects, not as ASA-specific behavior.

## Ranking

### P0 - Story phase reuses stale Story working state after scope extension

Source files:

- `docs/feedback/platform-story-save-state-mismatch.md`
- `docs/feedback/platform-story-generate-existing-attempt-needs-input.md`

Recommended GitHub issue title:

`Story phase reuses stale draft attempts after scope extension and blocks save/generate routing`

Type: bug / workflow / agent ergonomics

Status: blocks the ASA dogfood run, but issue scope is platform-wide.

#### Live Evidence

`agileforge workflow next --project-id 3` currently returns:

- `ok=true`
- `status=next_phase_available`
- commands:
  - `agileforge story pending --project-id 3`
  - `agileforge story generate --project-id 3 --parent-requirement <parent_requirement>`

`agileforge status --project-id 3` currently shows:

- `fsm_state=STORY_INTERVIEW`
- `setup_status=passed`
- `Technology and Model Research Spike` Story runtime exists.
- runtime attempt count: `10`
- current draft projection:
  - `latest_reusable_attempt_id=attempt-10`
  - `artifact_fingerprint=sha256:5ca69869d52df6b201277432f34c7a7a91e9cc302bb5384e89330154f5609e0a`
  - `kind=complete_draft`
  - `is_complete=true`
  - `updated_at=2026-06-09T10:45:25.803888Z`

`agileforge story history --project-id 3 --parent-requirement "Technology and Model Research Spike"` currently shows:

- top-level `attempt_id=attempt-10`
- `artifact_fingerprint=sha256:5ca69869d52df6b201277432f34c7a7a91e9cc302bb5384e89330154f5609e0a`
- `story_count=8`
- `is_reusable=true`
- `quality.coverage_status=complete`
- `quality.saveable=true`
- `quality.blocking_findings=[]`
- `quality.remaining_scope=[]`
- `save.available=true`
- `save.expected_state=STORY_REVIEW`
- `current_draft.is_complete=true`

But persisted FSM remains `STORY_INTERVIEW`, so `story save` rejects the exact
guard returned by history/save metadata:

- error code: `INVALID_COMMAND`
- message: `story save requires FSM state STORY_REVIEW`

`agileforge story pending --project-id 3` currently shows the current scope as
extension scope:

- `accepted_spec_version_id=4`
- `extension_scope=true`
- first current group: `milestone_3`
- `Technology and Model Research Spike` status: `Attempted`
- `attempt_count=10`

Attempt `attempt-10` itself is old:

- `created_at=2026-06-09T10:45:25.803888Z`
- input context says `Milestone 1: Foundation & Data Pipeline`
- input context has no `accepted_spec_version_id`, `latest_spec_version_id`,
  `spec_version_id`, or `scope_extension_context`

Current pending roadmap scope says the same requirement is now in:

- `milestone_3`
- `accepted_spec_version_id=4`
- `extension_scope=true`

#### Code Evidence

Relevant code:

- `services/phases/story_service.py`
- `services/agent_workbench/story_phase.py`

Observed behavior from code:

- `generate_story_draft()` sets `state["fsm_state"] = STORY_REVIEW` when
  `story_save_payload(runtime)` exists.
- `save_story_draft()` rejects unless current persisted state is
  `STORY_REVIEW`.
- `story_interview_summary()` exposes `save.available=true` and
  `save.expected_state=STORY_REVIEW` whenever `story_save_payload(runtime)`
  exists.
- `_story_pending_items()` applies scope-extension metadata checks for saved
  stories and merge resolutions, but then uses `story_has_working_state(runtime)`
  without any scope/provenance check.
- `story_has_working_state(runtime)` treats any `draft_projection`,
  `request_projection`, current resolution, or unabsorbed feedback as current
  working state.

#### Working Hypothesis

This is not primarily a Story save guard bug.

Root cause appears to be stale Story runtime reuse across scope extension/current
roadmap scope. Old Story attempts from June 9 remain attached to the same
requirement name. After authority 8 and the new roadmap, the current requirement
is in accepted spec version 4 extension scope, but the Story runtime still has an
old saveable draft from the previous roadmap/spec context.

That stale draft causes three symptoms:

1. `story pending` marks the current extension-scope requirement as `Attempted`.
2. `story generate` without input fails with `User input is required to refine an existing story.`
3. `story history` advertises a saveable draft, but the persisted FSM remains
   `STORY_INTERVIEW`, so `story save` fails.

#### Expected Behavior

For scope-extension/current-roadmap Story work, AgileForge should either:

1. Treat old same-name Story attempts as stale/non-current unless their
   provenance matches current requirement scope, accepted spec version, and
   source item ids; or
2. Surface an explicit recovery/reset command for stale Story working state; or
3. Allow save only when the draft provenance matches the current roadmap scope
   and persisted FSM can reach `STORY_REVIEW`.

`workflow next`, `story pending`, `story history`, and `story save` must agree on
whether the draft is current and saveable.

#### Suggested Fix Direction

Add provenance-aware Story working-state checks:

- Extend Story attempt/draft projection metadata with accepted spec version and
  extension/source item ids when generated under scope extension.
- In `_story_pending_items()`, replace scope-blind `story_has_working_state()`
  with a helper that checks whether the working state matches the current
  `extension_metadata`.
- In `story_interview_summary()` / `story_save_payload()`, do not advertise
  `save.available=true` for stale drafts whose provenance does not match current
  requirement scope.
- Add a regression test with:
  - legacy same-name Story draft in runtime,
  - current scope-extension roadmap item with same requirement name,
  - expected pending status `Pending`, not `Attempted`,
  - no save command advertised for stale draft.

### P1 - Roadmap generation marks incomplete coverage as complete

Source file:

- `docs/feedback/platform-roadmap-generation-save-coverage-mismatch.md`

Recommended GitHub issue title:

`Roadmap generation can return complete draft that roadmap save rejects for coverage mismatch`

Type: bug / workflow / agent ergonomics

Status: ASA worked around this by refining/saving a corrected roadmap, so it is
not the current blocker. Still valid platform bug based on recorded evidence.

#### Recorded Evidence

`agileforge roadmap generate --project-id 3` returned:

- `ok=true`
- `attempt_id=roadmap-attempt-3`
- `is_complete=true`
- `fsm_state=ROADMAP_REVIEW`
- 3 releases
- 0 clarifying questions

Then guarded save failed:

- error code: `INVALID_COMMAND`
- message: `Roadmap coverage mismatch`
- missing foundational backlog items including:
  - `Technology and Model Research Spike`
  - `Python Project Scaffold and uv Management Setup`
  - `Raw Data Ingestion Pipeline`
  - `Canonical Process Event Record Definition and Validation`
  - `State Window Feature Generation`
  - `Delayed Temperature Reward Scoring`
  - `Offline Recommender System`
  - `pyrepo-check Quality Gate Integration`

#### Working Hypothesis

Roadmap generation and roadmap save enforce different coverage expectations.
Generation can mark a roadmap draft complete, while save applies a stricter full
coverage gate.

The remaining design question:

- Should scope-extension roadmaps cover all current backlog items, or only the
  additive extension scope?

Either answer can be valid, but generation and save must use the same answer and
surface the same expected coverage set before save.

#### Suggested Fix Direction

- If save requires all current backlog items, make roadmap generation validate
  that exact coverage before returning `is_complete=true`.
- If scope-extension roadmaps may cover only additive items, make roadmap save
  scope-aware and expose the expected coverage set in generation/history/workflow
  next.
- Add a regression test where generation returns `is_complete=false` or quality
  findings when the save coverage set would reject the draft.

## Duplicate Check

Open GitHub issues checked on 2026-06-22:

- `#129 Add brownfield setup mode with product-spec curation before authority compilation`
- `#140 authority curate can publish candidate authority but leave mutation ledger unreconciled`
- `#141 authority feedback rejects review-visible assumption targets`
- `#142 authority curate can fail on legacy curation table missing mutation_event_id`

No duplicate found for the P0 Story stale-runtime issue or the P1 Roadmap
coverage mismatch issue.

## Recommended Next Action

1. Open P0 issue first.
2. Fix P0 before resuming ASA Story generation.
3. Open P1 as a separate issue after P0 is filed.
4. Do not open `platform-story-generate-existing-attempt-needs-input` as a separate issue
   unless P0 fix shows a distinct remaining command-contract bug.
