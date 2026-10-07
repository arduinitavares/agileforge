# Issue #244 Lifecycle Summary Consistency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this approved plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. External Opus review: APPROVE WITH CHANGES; user authorized phase 2 after the revisions recorded below. No commits.

**Goal:** Make workflow progress, the default workbench view, the Vision summary, and retained desktop navigation badges agree with authoritative projections, while preserving deliberate navigation as a separate viewing choice.

**Architecture:** Reuse `AgileForgeWorkspace.currentStageIds()` and `initialStageId()` for current work, and add one small display adapter in `frontend/project.js` for existing artifact projections. Expose the already-selected accepted Backlog identity in `story_pending`; do not infer acceptance from counts, missing decisions, or action availability. Project changes are fresh page loads; same-project refresh overwrites every target badge after success, preserves last confirmed labels after failure, and never overwrites an existing selected view.

**Tech Stack:** Existing browser JavaScript, Node's test/VM facilities, Python 3.13, pytest, stdlib `HTMLParser`, and the existing durable read service. No new dependencies.

**Spec:** [Issue #244 and its scope-extending comments](https://github.com/arduinitavares/agileforge/issues/244), plus the user's phase-1 requirements. This is a bounded existing-flow correction with an explicitly requested written plan; non-interactive brainstorming and this plan replace the skill's default separate design/approval conversation. [#267](https://github.com/arduinitavares/agileforge/issues/267) and [#237](https://github.com/arduinitavares/agileforge/issues/237) define coordination boundaries, not additional implementation scope.

## Global Constraints

- Baseline: detached HEAD `8c9e693a8820784f509c7c12f9f3942850392be0`, including #236 / PR #291. Preserve `goal.effective_questions.questions` handling at `frontend/project.js:915-917`.
- Phase 1 is complete. External Claude Opus 5.5 review verified diagnosis/design and requested the bounded revisions in this document; phase 2 is now approved.
- Phase 2 requires a fresh ultra implementer per implementation task, task review with both specification-compliance and code-quality verdicts, final independent whole-change review, TDD, and verification before completion.
- Only this issue. No workflow transitions, acceptance rules, source registration behavior, authority generation, repository refresh/replacement behavior, or archival-history UI changes.
- Provider-free verification. Use test engines or disposable temporary databases only; never real profiles/DBs under the user's home or existing `agileforge-*` Docker resources.
- Use only `uv run --frozen` for Python project commands. On this macOS host, do not invoke product runtime/launcher processes; Linux CI owns real-runtime checks. Never use a bare/user-level `agileforge`.
- Git is read-only: no commits, branches, staging, stash, pushes, PRs, or GitHub comments. Retain uncommitted files. Do not create another worktree for this task.
- Preserve hidden visibility of `#top-cockpit` and `#master-stage-nav`; do not restore the old layout or add a new desktop sidebar. Fix their retained bindings and the current visible workspace's selection behavior.
- Tests exercise data, DOM bindings, events, and parsed HTML/YAML structure. No prose/document regex assertions or brittle exact test/read counts as substitutes for behavior. The existing one-snapshot/one-dashboard-GET contracts remain meaningful checks.
- Follow repository typing and path-banner rules. Do not add `# type: ignore`; use a narrowly justified `# ty: ignore[rule]` only as a last resort.

## Review Focus

1. Accepted Backlog with no pending items and no Roadmap must still show acceptance: Task 1 producer tests and Task 2 adapter tests.
2. Required work, optional reentry, concurrent work, and locked transport actions must keep the existing workspace meaning of current progress: Task 2 decision-order/permutation and Sprint-context tests.
3. An accepted artifact and a pending successor are separate facts; candidate existence, Feedback, and absent decisions never establish acceptance: Tasks 1–3 accepted-ancestor and pending-review tests.
4. Project switch is a fresh page load with neutral static labels; same-project state refresh replaces every badge, while failed refresh retains last confirmed labels and Manual refresh required: Tasks 3–4.
5. Deliberate navigation/history survives same-project refresh without changing workflow progress; default selection uses initialStageId once. Do not add automatic focus advancement or cross-project/history ownership: Task 4. Audit existing Linux e2e assertions before editing.

## Investigation and baseline evidence

All source references below are for the baseline SHA, not promised post-change line numbers.

| Finding | Evidence |
| --- | --- |
| Category and recommendation ranks increase with priority, but the legacy resolver sorts ascending and takes element zero. | `frontend/project.js:384-419`, `:4555-4563`. The card renderer correctly chooses a decision when the comparator is **positive**, `:538-548`; do not reverse the shared comparator globally. |
| Vision summary reads an unsupported field. | `frontend/project.js:4722-4723` reads `vision.accepted`; the working panel reads `projection.current.statement`, `:865-871`. The producer emits `current`, `candidate`, and `review`, `services/read_projections.py:2477-2494`. |
| Specification review and accepted-base status are different projections. | Dashboard uses `specification_review`, `api.py:988-990`; source-only output is `source`, null `candidate/review`, and `SPECIFICATION_NOT_STRUCTURED`, `services/read_projections.py:2797-2813`. Existing `specification_status.current` can preserve an accepted base, `:2674-2731`; do not assume a null current review means accepted history was deleted. |
| Retained framing badges are static. | `frontend/project.html:176,184,192,200,208` initializes Complete/Active/Accepted. `renderMasterStageNav`, `frontend/project.js:4821-4834`, only updates Story count and a truthy Sprint status. Its Sprint branch never clears an earlier badge for an absent Sprint. |
| The current layout already fixes part of the reported screenshot. | `#top-cockpit` is hidden at `frontend/project.html:81`; legacy navigation is hidden at `:161-162`. The actual scripts load workspace before project at `:360-361`. `ensureWorkspaceView`, `frontend/project.js:4032-4038`, uses the workspace's required-stage selection. |
| Existing workspace semantics separate current work from viewing. | `frontend/lifecycle-workspace.js:39-51,94-116` excludes optional reentry and handles concurrent/current Sprint work; map rendering applies distinct current/viewing markers, `:290-319`. |
| Existing navigation is page-scoped and already retains a chosen view. | `selectedProjectId` is set once at `frontend/project.js:7784`; `frontend/app.js:30,192` performs full-page project navigation; history uses the same URL at `frontend/project.js:3936-3937`. Preserve selection/history; no cross-project ownership layer is needed. |
| Accepted-current Backlog metadata is missing from the dashboard data. | `backlogReview` is pending/continuation-oriented; accepted correction returns `PLANNING_REVIEW_NOT_AVAILABLE`, `services/application.py:2579-2605`. `story_pending` already selects the exact accepted root, `services/read_projections.py:3044-3058`, but successful output only exposes coverage items/counts, `:3059-3067,3126-3133`. Zero coverage cannot establish absence of an accepted root. |
| A current test masks the Vision mismatch. | `tests/test_cockpit_action_synchronization.mjs:213-235` supplies invented `vision.accepted`, instead of the producer's `vision.current`. Correct the fixture; do not add compatibility for that invented field. |

### Throwaway reproduction

The phase-1 script lives outside the repo at `$TMPDIR/issue244-repro.VQRA7thdum/repro.mjs` (resolved during this run to `/var/folders/fh/0bmky89j2d54xdptjs_1mrjm0000gn/T/issue244-repro.VQRA7thdum/repro.mjs`). It evaluates the actual source in Node VM contexts with synthetic projection data and a fetch function that throws. It uses accepted Vision (`current`), active Goal, registered source, no candidate/Sprint, required Specification structuring, and downstream blocked decisions. This is a renderer reproduction, not a live product/CLI session.

```text
workspace absent: phase=Sprint; Vision=Direction pending; heading=Sprint / Delivery Loop
workspace loaded: phase=Sprint; Vision=Direction pending; heading=Specification / Project Framing
deliberate Sprint view with workspace: heading=Sprint plan & start; phase still incorrectly Sprint
active Sprint -> absent Sprint: previous badge=Active; next badge=Active
```

The current visible heading is therefore already correct for the single required Specification state. The retained summary/badge defects remain, and loading/default-selection ownership still needs coverage. Do not report the old screenshot as an unchanged visible-master reproduction.

Baseline verification completed without providers or product runtime:

- The nine CI Node suites: **269 passed**, exit 0; local Node `v26.7.0` versus CI Node 24. Log: `$TMPDIR/issue244-node-baseline.oPJEAMwvOd`.
- `uv run --frozen pytest tests/workflow/test_product_discovery_graph.py tests/services/test_durable_product_definition_projections.py tests/adapters/test_api_dashboard_bundle.py -q`: **153 passed, 5 warnings**, exit 0. Log: `$TMPDIR/issue244-pytest-baseline.2W7UMydO7o`.
- These are baseline checks. New regression tests and final quality gates are phase-2 work.

The independent read-only projection inventory used one native agent, requested explicitly as `gpt-6.1-sol` / `ultra`, Standard context, with no descendants. The spawn response exposed task identity, not separate runtime-setting confirmation. No usage windows were exposed; no external provider session was started.

## Designs considered

1. **Reverse the stage sort and patch labels independently.** Smallest diff, but leaves separate current-stage semantics, ambiguous concurrent work, optional reentry, stale selection, and accepted-artifact detection unresolved. Reversing the comparator itself would also break the card renderer's positive-priority convention. Rejected as incomplete.
2. **Shared display adapter over existing current-work/artifact projections, with one additive Backlog identity field. Chosen.** Reuses workspace selection/history and the backend accepted-leaf selector. Keeps viewing separate from progress and adds no workflow authority. Trade-off: a small public read-response addition and truthful first-load/refresh presentation.
3. **New backend aggregate lifecycle stage/status endpoint or authority step.** Could centralize all labels, but duplicates existing routing/projections, expands API/domain scope, and would entangle #267's historical Specification context. Rejected; no new `current_stage` backend field or authority generation is justified here.

## Display and transport contracts

### Existing authority to consume

- Current work: `AgileForgeWorkspace.currentStageIds(state.position, state)` and `initialStageId(state.position, state)`. Use the authoritative decision recommendation/category plus existing Sprint context, never the selected view, card ordering, or highest completed artifact.
- Vision summary: `state.vision.current.statement` before any candidate statement. Accepted current direction stays identifiable while a revision is pending. A candidate is labelled Draft; explicit absence after a successful read may show `Direction pending`.
- Goal badge: conflict/active first, then the current `candidate/review`, then historical terminal `outcome.outcome`. A next Goal candidate may coexist with the prior resolved outcome; that outcome must not label the current candidate. Pure terminal display uses `outcome.outcome`, not a made-up `outcome.fulfilled` boolean.
- Specification badge: the current-source review projection's `source`, `candidate`, `review.state`, and `stale_reason`. Accepted means an exact current candidate with accepted review; source-only means Source registered / Awaiting structuring. A null source with SPECIFICATION_SOURCE_NOT_REGISTERED means **Source not current**, with stale_reason as detail, never Accepted or Source required. This must remain truthful in #267's accepted-Backlog/active-Sprint state. Conflict is Unavailable.
- Backlog badge: new `state.storyPending.accepted_backlog`, independent of item/count values. Pending/Feedback successor details may come from `planningReviews.backlog.review` or `.continuation.review`; they do not replace an accepted ancestor.
- Roadmap badge: `state.acceptedRoadmap.kind === 'ready'` plus `.data.state === 'accepted'`. Pending review/Feedback is separate; optional read failure is Unavailable, never Accepted/Absent.
- Sprint badge: validated `state.sprintStatus.kind`; ready uses `data.effective_status ?? data.sprint.status`, absent shows Not started, error shows Unavailable. Always overwrite the badge.

### Narrow missing field

Add this field to **both successful** `DurableReadProjectionService.story_pending()` response data branches:

```text
accepted_backlog: null | {
  backlog_artifact_id: positive integer,
  artifact_fingerprint: nonempty string
}
```

`null` means the existing selector found no accepted Backlog for the current accepted Specification/Goal lineage. A reference is copied from `backlog_lineage.backlog`, including zero-item coverage. A missing field, malformed reference, wrong project ID, or failed read means unknown/unavailable. Existing snapshot/lineage/content errors stay errors.

This is justified because pending reviews and an absent Roadmap do not tell the frontend whether Backlog is accepted. Do not parse graph fact references or count Stories to reconstruct acceptance. The existing API `GET /api/projects/{id}/story/pending`, dashboard `storyPending` slot, and CLI `story pending` JSON acquire the same additive data field. No new endpoint, bundle slot, request field, database schema, mutation, or extra snapshot load.

### Local display interface

Introduce near the existing cockpit/stage functions in `frontend/project.js`:

```text
projectLifecycleDisplayProjection(state, readState) -> {
  currentStageIds: integer[], primaryStageId: integer | null,
  phaseLabel: string, visionLabel: string,
  badges: { [legacyStageName]: { label: string, detail: string } }
}
readState = { kind: 'loading' | 'ready' | 'unavailable' }
```

This pure adapter has no I/O, navigation writes, or mutation authorization role. The page owns one project. No new general frontend lifecycle engine/module or project/history ownership layer is required.

For ready reads, phase labels use the workspace stage label. A null primary with multiple current stages shows `Multiple current stages`; no advertised current work shows `No current stage`. Missing workspace support shows `Unavailable`, without a Stories/Sprint guess. Preserve the existing workspace mapping/semantics rather than adding another rank-based fallback.

Before the first successful read, target labels are neutral Loading… (or Unavailable after initial failure), with no claimed current stage. After a successful render, an in-flight or failed same-project refresh retains the last confirmed labels with the existing Manual refresh required freshness note; do not replace them with Unavailable. Successful refresh recomputes every target badge. Rendering uses textContent/attributes. The hidden inspector at project.js:4900-4909 is out of scope.

The source-only fixture must produce:

| Surface | Expected |
| --- | --- |
| Current-work primary/current IDs | Specification / `[4]` |
| Phase summary | Specification |
| Vision summary | `Vision: Accepted · <current statement snippet>` |
| Default heading/kicker | Specification / Project Framing |
| Vision / Goal badges | Complete / Active |
| Specification badge/detail | Source registered / Awaiting structuring |
| Backlog / Roadmap badges | Blocked, with their projected blocker reason |
| Sprint badge | Not started |
| Deliberately selected Sprint | Sprint view remains selected; heading kicker identifies Viewing; phase/current marker stay Specification |

For accepted artifacts with pending successors, keep accepted-base evidence and expose review/revision context in badge detail/title. Do not claim acceptance from pending content. Do not rewrite the hidden radar's percentage algorithm, health tiles, action-priority selection, cycle metrics, or inspector provenance in this issue; only neutralize the target loading labels and bind phase/Vision/framing/Sprint labels.

### Default and deliberate selection

Preserve ensureWorkspaceView's existing rule: initialize a null view from initialStageId, then never overwrite an existing view. Preserve map clicks, legacy navigation, stage jumps, explicit action navigation, and browser history; viewing affects the inspected panel/title, never workflow progress. Use a Viewing kicker when the inspected stage differs from current work. Do not add workspaceProjectId, workspaceSelectionMode, history projectId ownership, automatic focus advancement, or cross-project A-to-B asynchronous scenarios.

Project switching means a fresh page load, covered by static HTML neutral-label tests. State switching means a same-project successful S1-to-S2 refresh that overwrites all badges, including active-to-absent Sprint and accepted-to-error/absent Roadmap. One existing sequence/abort late-response regression is sufficient if useful; do not change that guard's scope.

## File map

| File | Responsibility |
| --- | --- |
| `services/read_projections.py` | Add accepted Backlog identity metadata to the existing read only. |
| `frontend/project.js` | Shared display adapter, target rendering, initial readiness/last-confirmed refresh behavior, Viewing kicker. |
| `frontend/project.html` | Stable badge IDs and neutral target initial labels; preserve layout visibility. |
| `tests/services/test_durable_product_definition_projections.py` | Accepted-root producer and empty-coverage/lineage boundaries. |
| `tests/adapters/test_api_dashboard_bundle.py` | Source-only and accepted-Backlog bundle propagation on one snapshot. |
| `tests/adapters/test_production_read_surfaces.py` | API/CLI additive-field passthrough if its fake read payload needs extension. |
| `tests/dashboard_bundle_fixture.mjs` | Synthetic schema-correct source-only and accepted/absence payload factories. |
| `tests/test_project_lifecycle_summary.mjs` (new) | Visible renderDashboard acceptance, adapter/badges, deliberate navigation, loading and same-project refresh regressions; load both actual frontend scripts. |
| `tests/e2e/test_single_project_lifecycle_ui.py` | Audit workbench/map/history assertions and update fake Story-pending metadata explicitly when needed; Linux runtime verification stays in CI. |
| `tests/test_cockpit_action_synchronization.mjs` | Correct the misleading Vision fixture and preserve cockpit action safety. |
| `tests/test_lifecycle_summary_markup.py` (new) | Parse real HTML for initial target labels, badge bindings, and retained visibility. |
| `.github/workflows/ci.yml`, `tests/test_ci_contract.py` | Include the new Node suite using existing YAML/argv structural assertions. |

No production change to `frontend/lifecycle-workspace.js` is expected. Existing tests there verify the reused stage contract; change its semantics only if phase-2 evidence identifies a separate in-scope defect and the coordinator records a justified plan correction.

## Phase-2 tasks (not executed)

### Task 1: Expose the accepted Backlog identity from the existing read

**Files:** Modify `services/read_projections.py:3044-3133`, `tests/services/test_durable_product_definition_projections.py`, `tests/adapters/test_api_dashboard_bundle.py`; narrowly extend `tests/adapters/test_production_read_surfaces.py` if needed.

**Interfaces:** Consumes `current_backlog_lineage(snapshot).backlog`. Produces `story_pending().data.accepted_backlog` as defined above; existing slot and items/count contracts remain unchanged.

- [x] Write failing `test_story_pending_exposes_accepted_backlog_identity_without_items` and `test_story_pending_exposes_null_without_accepted_backlog` using `_story_pending_snapshot` (`:129`) and `_chain_backlog` fixtures. Assert exact selected ID/fingerprint or explicit null; do not derive expectation from `count`.

  With `_chain_backlog(102, "accepted", None).model_copy(update={"version_number": 1})` as a valid standalone root and empty coverage, the decisive assertions are:

  ```python
  assert data["accepted_backlog"] == {
      "backlog_artifact_id": 102,
      "artifact_fingerprint": "sha256:backlog-102",
  }
  assert data["items"] == []
  # The separate no-root test asserts data["accepted_backlog"] is None.
  ```

- [x] Extend accepted-ancestor/Feedback-successor and current-root tests (`:243,289`) to assert the accepted root survives zero pending coverage and excludes historical/unaccepted leaves. Retain corruption/conflict error assertions (`:384,439`).
- [x] Run `uv run --frozen pytest tests/services/test_durable_product_definition_projections.py -k 'story_pending' -q`; expect missing-field failures, not import/setup failures.
- [x] Add the field to the two successful return data mappings; annotate the important module/local JSON values as needed. Copy the existing selected identity; do not add a second selector/query or weaken error paths.
- [x] Add a bundle companion regression for accepted Backlog with no Roadmap and source-only null metadata. Reuse `_application` and durable seeds; assert standalone/bundle field agreement and existing snapshot isolation. Extend API/CLI read passthrough structurally if needed, using injected application objects rather than launching product processes.
- [x] If injected-application CLI tests select the home runtime fence on macOS, add a typed fixture scoped to tests/adapters/test_production_read_surfaces.py redirecting runtime_roots to tmp_path; retain actual runtime_fence checks and unchanged production behavior. Run the exact pytest commands without touching home state.
- [x] Audit the e2e fake story_pending at tests/e2e/test_single_project_lifecycle_ui.py:1003-1007: it currently lacks accepted_backlog. Add explicit metadata for its seeded lineage where applicable; never infer acceptance from its counts.
- [x] Run `uv run --frozen pytest tests/services/test_durable_product_definition_projections.py tests/adapters/test_api_dashboard_bundle.py tests/adapters/test_production_read_surfaces.py -q`; expect pass. Package the task diff, commands/results, and reference identity evidence for spec/quality review.

### Task 2: Share current-work and artifact display semantics

**Files:** Modify `frontend/project.js:4555-4563,4696-4739,5085-5229`, `tests/dashboard_bundle_fixture.mjs`, `tests/test_cockpit_action_synchronization.mjs`; create `tests/test_project_lifecycle_summary.mjs`; modify `.github/workflows/ci.yml` and `tests/test_ci_contract.py:241-276` to register it.

**Interfaces:** Consumes Task 1's field and existing workspace functions/artifact fields. Produces `projectLifecycleDisplayProjection(state, readState)` and `lifecycleDisplayRead` local readiness metadata. `renderTopCockpit` consumes its phase/Vision labels. The old resolver's sort-first behavior is removed/replaced; the shared comparator's positive-priority semantics remain unchanged.

- [x] Add `sourceOnlyLifecycleState({ projectId = 7 } = {})` and `sourceOnlyDashboardBundle({ projectId = 7 } = {})` exports to the test fixture, plus accepted-state factories. Use producer field names; no `vision.accepted`, `specification.accepted/active`, or accepted Backlog invented inside a pending-only review. Include optional source replacement and blocked downstream decisions. The Node harness evaluates both shipped scripts and stubs all network operations.
- [x] Write failing `source-only state uses Specification and accepted current Vision` and `accepted Vision remains the summary anchor during revision` Node tests. Use `Accepted direction` as the short current statement; separately assert long/markup-looking statement handling without depending on arbitrary truncation length. Decisive adapter assertions:

  ```javascript
  const display = context.projectLifecycleDisplayProjection(
      sourceOnlyLifecycleState(), { kind: 'ready' },
  );
  assert.equal(display.phaseLabel, 'Specification');
  assert.equal(display.primaryStageId, 4);
  assert.deepEqual(Array.from(display.currentStageIds), [4]);
  assert.equal(display.visionLabel, 'Vision: Accepted · Accepted direction');
  assert.equal(display.badges.Specification.label, 'Source registered');
  assert.equal(display.badges.Backlog.label, 'Blocked');
  ```

- [x] Add decision-order permutations, optional-reentry-only, multiple required stages, locked structuring action, accepted artifacts without advertised satisfied decisions, and validated active-Sprint/optional-source-reentry cases. Assert parity with the existing workspace helpers, not a newly encoded decision-rank algorithm.
- [x] Add artifact tests for accepted Backlog with empty coverage/no Roadmap; pending/Feedback successor preserving an accepted base; absent and malformed/missing acceptance metadata; Roadmap read error; Goal terminal outcomes; Specification conflict and source-only review. A null review must not assert that historical acceptance disappeared.
- [x] Add the #267-shaped fixture (null source, SPECIFICATION_SOURCE_NOT_REGISTERED, accepted Backlog, active Sprint): assert Source not current with the reason as detail, truthful Backlog/Sprint badges, and no required/Accepted Specification claim. Test Goal terminal labels using producer outcome.outcome, not fulfilled.
- [x] Make the primary acceptance test call renderDashboard with the actual workspace script and source-only fixture: stage 4 alone has aria-current="step", workbench title/kicker are Specification / Project Framing, and Return to current work is absent. Test the actual mounted DOM structure/attributes; retained hidden-binding tests are secondary.
- [x] Run `node --test tests/test_project_lifecycle_summary.mjs tests/test_cockpit_action_synchronization.mjs`; expect the target summary/adapter failures first.
- [x] Preserve legacy hidden progress-bar rules while changing phase titles: map the authoritative primaryStageId to existing Sprint/Execution/Review names through workspaceStageForLegacy, with behavioral RED/GREEN for the existing 80/95 percentages. Require a positive project ID before presenting ready Sprint status.
- [x] Implement the pure adapter and phase/Vision rendering; introduce readiness metadata initially loading and publish ready only after a successful existing dashboard load. Preserve primary-action, action-binding, mutation-reconciliation, and #236 Goal question behavior.
- [x] Correct the cockpit test's Vision fixture to `current` and mark its synthetic loaded snapshot ready. Use actual workspace source in stage tests; absent module support must produce a neutral result, not alternate progress inference.
- [x] Register the new suite in the existing CI `node --test` argv list and structural `required_suites` set. Run `node --test tests/test_project_lifecycle_summary.mjs tests/test_cockpit_action_synchronization.mjs tests/test_lifecycle_workspace.mjs tests/test_product_goal_interview_ui.mjs` and `uv run --frozen pytest tests/test_ci_contract.py -q`; expect pass. Package evidence for task review.

### Task 3: Render truthful badges and neutral initial labels

**Files:** Modify `frontend/project.html:81-111,169-208,228-234,277-281` and `frontend/project.js:4821-4834`; extend `tests/test_project_lifecycle_summary.mjs`; create `tests/test_lifecycle_summary_markup.py`.

**Interfaces:** Consumes Task 2's adapter. Produces stable `nav-vision-badge`, `nav-goal-badge`, `nav-specification-badge`, `nav-backlog-badge`, `nav-roadmap-badge` IDs and complete per-render assignments, alongside existing `nav-sprint-badge`.

- [x] Write failing `framing badges use source and accepted artifact projections` and `Sprint badge clears when a later projection is absent` Node DOM tests that call the actual renderer and inspect each framing badge's `textContent` and detail/title. Cover actual accepted artifacts and pending successors as well. Assert five named bindings individually, not an exact count of matching strings. After source-only render:

  ```javascript
  assert.equal(elements['nav-vision-badge'].textContent, 'Complete');
  assert.equal(elements['nav-goal-badge'].textContent, 'Active');
  assert.equal(elements['nav-specification-badge'].textContent, 'Source registered');
  assert.equal(elements['nav-backlog-badge'].textContent, 'Blocked');
  assert.equal(elements['nav-roadmap-badge'].textContent, 'Blocked');
  assert.equal(elements['nav-sprint-badge'].textContent, 'Not started');
  ```

- [x] Write `test_initial_lifecycle_labels_are_neutral` and `test_legacy_badge_bindings_and_visibility_are_preserved` using stdlib `HTMLParser` over the actual HTML. Inspect IDs/attributes/text structurally: target phase/Vision/framing/Sprint labels are Loading before scripts; no accepted/active initial claims; summary/navigation remain hidden; workbench initial wording is neutral.
- [x] Run `node --test tests/test_project_lifecycle_summary.mjs` and `uv run --frozen pytest tests/test_lifecycle_summary_markup.py -q`; expect stale/initial label failures.
- [x] Bind all named target badges through the shared adapter on every render, including null/error/loading branches; set text/detail deterministically and clear earlier values. Keep Story counts and existing action controls intact. Use projected blocker details rather than invented acceptance explanations.
- [x] Replace target initial Complete/Active/Accepted/Pending/Sprint/Delivery claims with neutral Loading labels, including the Goal status badge, phase detail and workbench heading. Preserve hidden containers, existing element IDs, and script ordering. Update the project script cache token so shipped HTML requests the changed bundle.
- [x] Run both focused commands again plus `node --test tests/test_cockpit_action_synchronization.mjs tests/test_workflow_position_display.mjs`; expect pass. Package the parsed-template/DOM evidence for task review.

### Task 4: Preserve deliberate navigation and last confirmed refresh labels

**Files:** Narrowly modify `frontend/project.js:4836-4897,5126-5309` where needed; extend `tests/test_project_lifecycle_summary.mjs`; audit `tests/test_workflow_position_display.mjs:4300-4324` and `tests/e2e/test_single_project_lifecycle_ui.py` workbench/map/history assertions before edits.

**Interfaces:** Consumes the shared display/readiness contract and existing workspaceView/history unchanged. Null view defaults once through initialStageId; existing views survive refresh. Failed refresh keeps last confirmed display labels/freshness.

- [x] Resolve the Task 1 reviewer's synthetic identity check during the e2e fixture audit: accepted Story-pending root ID 41/fingerprint must match the existing accepted Roadmap lineage (_fingerprint("b")); include the real producer project_id shape where consumed. Keep this fixture-only and independent of counts.
- [x] Audit existing e2e expectations for workbench-stage-*, workspace-stage-* aria-current, history, and the Select a stage case near :5394. Preserve those semantics; document any necessary fixture-only adjustment. Real e2e execution requires the pre-merge Linux CI gate.
- [x] Write first-load/initial-failure tests and same-project successful S1-to-S2 badge replacement tests, including Sprint active-to-absent and Roadmap accepted-to-error/absent. Write failed refresh after ready render: labels remain last confirmed and Manual refresh required stays visible.
- [x] Write failing `deliberate Sprint view survives same-project refresh`. Drive the existing click/selection/history path rather than only assigning selectedStageTab. After deliberate selection and a completed source-only refresh, assert:

  ```javascript
  assert.equal(state('workspaceView.stageId'), 8);
  assert.equal(elements['workbench-stage-title'].textContent, 'Sprint plan & start');
  assert.equal(elements['workbench-stage-kicker'].textContent, 'Viewing · Delivery Loop');
  assert.equal(elements['cockpit-active-stage-label'].textContent, 'Specification');
  // Inspect the mounted map's stage 4 current marker separately from stage 8 viewing.
  ```

- [x] Preserve browser Back/Forward and existing view's stage/task/tab/filter. Optionally assert that the existing sequence/abort guard ignores a late same-project response; no cross-project tests or new ownership fields.
- [x] Run `node --test tests/test_project_lifecycle_summary.mjs tests/test_workflow_position_display.mjs`; expect missing Viewing/refresh-preservation failures where behavior is changed. Existing default/view-preservation acceptance may already pass; record it as regression coverage, not fabricated RED.
- [x] Make the minimal refresh-label preservation and Viewing-kicker changes. Do not rewrite history, existing-view preservation, or default-selection timing. Phase and map progress consume authoritative projections independently of the inspected stage.
- [x] Run `node --test tests/test_project_lifecycle_summary.mjs tests/test_workflow_position_display.mjs tests/test_lifecycle_workspace.mjs tests/test_cockpit_action_synchronization.mjs tests/test_sprint_retry_dashboard.mjs` and `uv run --frozen pytest tests/e2e -q --co`; expect pass. Package same-project evidence for review.

### Final whole-diff review correction: R1–R3

Final Astra review verified two interactions missed by task tests: clearing planningReviews after 409 changes map/Return progress despite frozen phase labels, and a prior Goal outcome overrides a next Goal candidate. It also found a non-neutral Delivery Loop kicker on unselected first failure. These are issue-244 display corrections, not workflow changes.

**Additional files:** `frontend/lifecycle-workspace.js`, `tests/test_lifecycle_workspace.mjs`; narrowly revisit `frontend/project.js`, `frontend/project.html`, and `tests/test_project_lifecycle_summary.mjs`. Update the workspace script cache token if its bridge changes.

- [x] One fresh ultra implementer handles all final findings in one fix wave, failing behavioral tests first.
- [x] Retain confirmed currentStageIds/primaryStageId for map markers, Return markup/target, Viewing kicker and summary after failed refresh. Prefer an optional display-only workspace bridge using the already-derived scalar/list projection; keep existing callers' defaults and live action authority separate. Preserve 409 planning-review clearing. A successful refresh replaces progress without advancing the selected view.
- [x] Cover confirmed completed Sprint + pending next plan -> 409 -> consistent frozen progress/Return -> successful recovery, including deliberate inspection and real Return click.
- [x] Cover pending and Feedback next-Goal candidates after both fulfilled and abandoned prior outcomes; current review wins, terminal-only tests remain.
- [x] Neutral loading/unavailable kicker for null view; retain deliberate historical stage9 Viewing · Delivery Loop and existing Select a stage/default timing.
- [x] Rerun covering/full CI Node and affected HTML/package/CI Python contracts. Reuse unchanged backend/API/CLI Python evidence with hashes; rerun required static/whitespace checks after the final fix.
- [x] Exactly one scoped final re-review, with both compliance and quality verdicts, then record every residual/deferred finding disposition. No second final fix wave.

Only the internal frontend map display bridge may acquire optional scalar/list display metadata; public backend/API/CLI/schema contracts remain exactly the previously approved accepted_backlog addition.

## Phase-2 review and execution protocol

- Load the active Superpowers SDD/TDD/review/verification skills at execution time; preserve their current templates. Use the existing managed worktree. The user's selected fresh-ultra-implementer path takes precedence over a default implementation route.
- Explicitly disclose/request `gpt-6.1-sol`, reasoning `ultra`, Standard context for each fresh task implementer. No descendants; reserve slots centrally. Never silently lower effort or select another model. If descendant controls cannot be enforced, stop that delegation and report the exact gap.
- Use fresh Copilot `claude-opus-5.5`, reasoning `high`, through Orca for the chosen task reviewer seat, producing both specification-compliance and task-quality verdicts. Before any entry, load `orca-cli` plus its version-matched guide and verify effective settings, tool/disclosure restrictions, and lifecycle. Supply coordinator-owned source/diff/evidence snapshots with tool operations denied by default. The external phase-1 plan review happens after this handoff, not during this run.
- Final substantive independent review is fresh native `gpt-6-astra`, reasoning `ultra`, Standard context, honoring the user's explicit requirement that every spawned native agent use ultra. Supply requirements, full diff, tests, and deferred findings without the implementation conversation. This is one bounded read-only review with no descendants; stronger effort does not expand permissions. Only one Astra workstream, four total workstreams maximum, and no concurrent mutations to shared files.
- Use the SDD plan-specific workspace/ledger. First ledger line identifies this exact plan; record per-task baselines, reports, both verdicts, findings, rulings, and completion. Because Git mutations are prohibited, replace commit checkpoints with retained file snapshots/hashes and full diffs including new files; do not use a commit-range package that omits uncommitted work. Preserve the SDD brief/report/review-package contracts.
- Preserve the SDD fix budget: five rounds maximum per task, rounds 1–3 resume the same implementer; rounds 4–5 use fresh stronger judgment at explicitly permitted settings. Preserve Minor/deferred and parked-finding records for final review. Re-review only the findings/fix diff; do not add duplicate routine reviewer seats. A permitted-effort conflict at escalation needs explicit resolution, not substitution or bypass.
- Validate reviewer claims against source and verification evidence. Reuse successful same-code test evidence; rerun covering checks after actual fixes. User approval of this plan is required before any of these tasks start.

## Required acceptance gates

Run after phase-2 changes and covering fixes. Redirect verbose logs to uniquely named `$TMPDIR` files and report bounded summaries. These are requirements, not evidence of phase-1 completion.

```bash
uv run --frozen ty check
uv run --frozen ruff check .
uv run --frozen ruff check --select ANN .
uv run --frozen ruff format --check services/read_projections.py tests/services/test_durable_product_definition_projections.py tests/adapters/test_api_dashboard_bundle.py tests/adapters/test_production_read_surfaces.py tests/test_ci_contract.py tests/test_lifecycle_summary_markup.py
git diff --check
```

The format command lists planned changed **Python** files; remove an untouched optional test file if Task 1 did not modify it, and add any justified changed Python file. Never feed JS/HTML/Markdown/YAML into Ruff. `ty check` is repo-wide and must report **`All checks passed!`** with exit 0. A passing targeted pytest run does not replace it; PR #291 previously failed at this gate. If `pytest.fail` is necessary, use the repository convention:

```python
pytest.fail(
    "message"  # ty: ignore[invalid-argument-type]
)
```

Prefer typed assertions/helpers without suppression. A baseline failure outside this issue must be recorded and distinguished from introduced failures; do not weaken checks or silently expand scope.

Targeted Python regressions (new names from the tasks):

```bash
uv run --frozen pytest tests/services/test_durable_product_definition_projections.py -k 'story_pending' -q
uv run --frozen pytest tests/test_lifecycle_summary_markup.py tests/test_ci_contract.py -q
```

Affected Python suites, including the existing source/acceptance/transport contracts:

```bash
uv run --frozen pytest tests/services/test_durable_product_definition_projections.py tests/services/test_planning_lineage.py tests/workflow/test_product_discovery_graph.py tests/workflow/test_direct_specification_lineage.py tests/adapters/test_api_dashboard_bundle.py tests/adapters/test_production_read_surfaces.py tests/test_frontend_package_resources.py tests/test_lifecycle_summary_markup.py tests/test_ci_contract.py -q
uv run --frozen pytest tests/e2e -q --co
```

Node gate: the full existing CI suite list plus the new suite, with CI executing Node 24:

```bash
node --test tests/test_workflow_position_display.mjs tests/test_lifecycle_workspace.mjs tests/test_sprint_retry_dashboard.mjs tests/test_dashboard_review_safety.mjs tests/test_dashboard_bundle.mjs tests/test_cockpit_action_synchronization.mjs tests/test_create_project_modal_required_fields.mjs tests/test_vision_interview_ui.mjs tests/test_product_goal_interview_ui.mjs tests/test_project_lifecycle_summary.mjs
```

Successful gates mean no failed tests/checks, no dropped existing suites, and recorded skip reasons. **Required pre-merge CI gate: Linux `./agileforge-dev check`, including real e2e execution.** This macOS worktree can collect e2e tests but cannot run that product-runtime gate; do not start or alter the user's Docker resources. Native test-service calls stay under pytest's test-engine/socket guards.

## Explicit change boundary and reviewer decisions

**Will change:** UI-only current-work presentation, Vision statement source/accepted label, retained framing/Sprint badges, neutral first-load labels, last-confirmed-label preservation and Viewing kicker, plus regression coverage/CI registration. The only server contract addition is data.accepted_backlog on the existing Story-pending read across API/dashboard/CLI JSON. No CLI command, endpoint, schema, graph version, mutation, automatic focus, or project/history ownership changes.

**Will not change:** Source registration/provenance rules, transitions, acceptance/supersession rules, stored artifacts, authority generation, provider calls, Goal effective-question semantics, hidden/visible layout choice, action authorization/dispatch behavior, health/cycle/progress-percentage algorithms, or #267/#237's historical context/rebinding recovery workflows.

Reviewer/user decisions resolved before phase 2:

1. The additive accepted_backlog identity is approved; preserve conflict/error paths and cover producer/bundle/API/CLI passthrough.
2. Specification badge is current source/review; null non-current source is Source not current with stale_reason detail, never Source required. Historical context stays with #267.
3. Multiple current stages / No current stage and Viewing wording are approved. Preserve existing user view and history, with no automatic progression.
4. Visible renderDashboard acceptance is primary; hidden summary/sidebar bindings remain truthful and cheap without unhiding or touching the inspector.
5. Local Node 26 baseline is not Node 24 CI proof; the final CI Node gate remains required. No product-runtime verification was performed or is authorized on this macOS host.

## Phase-1 handoff

- [x] Read issue body/comments and repository instructions; verify exact baseline and clean tracked state.
- [x] Apply using-superpowers, systematic-debugging + bounded-codebase-investigation, non-interactive brainstorming, and writing-plans in order.
- [x] Reproduce renderer/badge defects outside the repository; distinguish current visible layout from the original report.
- [x] Trace existing authoritative projections and justify the single missing accepted-Backlog identity field.
- [x] Run existing provider-free baseline suites; write and self-review this plan.
- [x] External Opus plan review and user phase-2 approval; required scope reductions applied before implementation.
- [x] Phase-2 implementation, task reviews, final Astra review/re-review, and required local acceptance gates; Linux pre-merge CI remains required.

## Additional authorized correction: atomic confirmed-read publication

The subsequent independent Opus whole-diff review approved all seven plan points and identified one medium concurrency finding. The user explicitly authorized a further bounded TDD correction and scoped review; this authorization applies only to the confirmed-display publication race, without reopening the completed task scopes.

- [x] Add a behavioral Node regression in `tests/test_project_lifecycle_summary.mjs` using the actual template, loader, selected-Sprint inventory request, deferred request barriers, and deterministic confirmation times. Confirm S1; let successful load A publish S2 and stall in inventory loading; start load B and fail it with a non-409 response; release A and confirm its supersession result; trigger a real user render. Assert badges, Vision, phase, map current markers, Return target, and freshness all describe A. Include a pending-inventory observation/render to catch the transient mismatch. Run the regression RED before changing production code.
- [x] In `frontend/project.js`, publish the display snapshot and internal confirmation time synchronously with the successful `lifecycleState` assignment, before reconciliation callbacks or an `await` can render/yield. Render visible freshness with the labels so the DOM never advances its timestamp ahead of the displayed read. Preserve existing abort/sequence guards, action-authority clearing, inventory requests, navigation/history, and last-confirmed labels after failure.
- [x] Request fresh scoped specification and quality review through the configured Opus route, followed by independent Astra review of this correction. Preserve prior review evidence; do not repeat the unchanged whole-diff review.
- [x] Run `uv run --frozen ty check` (must say `All checks passed!`), `uv run --frozen ruff check .`, `uv run --frozen ruff check --select ANN .`, Ruff format on all changed Python files, `git diff --check`, every Node suite in the current CI command, and `uv run --frozen pytest tests/test_ci_contract.py tests/test_lifecycle_summary_markup.py -q`. Keep all changes uncommitted and report actual outputs.

This correction changes only frontend publication timing, visible freshness synchronization, and regression coverage. It adds no projection fields, API/CLI/schema changes, workflow transitions, authority, automatic navigation, provider calls, or runtime state mutations. Linux pre-merge CI remains required as above.
