# Issue #259 Pending Sprint Plan Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. The user selected Gemini for execution and Astra for independent review. Do not spawn additional implementation agents or switch models for this handoff. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore a distinct pending next-Sprint plan review beside a completed Sprint, with exact review identity and unchanged decision safeguards.

**Architecture:** Compose Sprint status and pending plan review independently. Reuse the existing review renderer, confirmation dialog, and decision transport; add only a small candidate-ID label. Prove rendering and browser submission using synthetic fixtures.

**Tech Stack:** Existing vanilla JavaScript renderer, Node `node:test` / VM harness, Python pytest / Playwright browser harness, uv-managed checkout runtime.

**Spec:** `docs/superpowers/specs/2026-09-08-issue-259-pending-sprint-review-design.md` and https://github.com/arduinitavares/agileforge/issues/259

## Global Constraints

- Baseline reviewed by Astra: `844fc0d3ba2cd2dbb87627bb4c09eb87a90d8b16`.
- Preserve the completed Sprint summary and clearly distinguish it from the next pending plan.
- Review content and controls remain bound to the exact current candidate and decision binding.
- Preserve stale/malformed projection handling, human confirmation, idempotency, and existing workflow guards.
- No automatic acceptance, Sprint start, duplicate generation, or mutation of historical accepted evidence.
- Use disposable synthetic fixtures/profiles. Do not modify or copy the operator's existing business or trace databases for tests.
- No provider calls, live runtime restart, backend changes, dependency upgrades, or edits for #232.
- Use only uv for Python. In a branch/worktree, use that checkout's `./agileforge-dev`; never a user-level `agileforge` shim. Run its `info --json` before runtime mutations.
- Preserve unrelated edits. No reset/clean, broad formatting, assertion weakening, silent skips, or permission bypass.
- Gemini implements the approved design. Astra reviews the actual diff and evidence before any publication/integration step.

## Repository and evidence anchors

Line numbers are from the reviewed baseline; locate functions again before editing.

| File / symbol | Purpose |
| --- | --- |
| `frontend/project.js:2103`, `planningReviewCardMarkup` | Pending/binding/content checks and three decision buttons |
| `frontend/project.js:2069`, `sprintReviewMarkup` | Current Sprint goal, owner, Story points, and Task evidence |
| `frontend/project.js:3290`, `deliveryPanelMarkup` | Incorrect status-based review suppression |
| `frontend/project.js:3324` | Generation-action filter, explicitly unchanged |
| `frontend/project.js:4438`, `planningReviewBinding` | Captures decision fingerprint and nullable instance |
| `frontend/project.js:4574`, `submitHumanAction` | Confirmation submission and conflict refresh |
| `services/read_projections.py:3033` | Review envelope; candidate contains `sprint_plan_artifact_id` and `artifact_fingerprint` |
| `tests/test_workflow_position_display.mjs` | `loadFrontend`, `acceptedSprintStatus`, `validatedSprintOwner`, `storyReview` |
| `tests/e2e/test_single_project_lifecycle_ui.py` | `SprintContinuityLifecycle`, `FakeLifecycle`, `dashboard_harness`, `_open_project_page` |
| `.github/workflows/ci.yml`, `cli/dev_checks.py` | Existing canonical verification commands |

The application response has `data.binding` and `data.review`. In the renderer,
the selected object is `reviews.sprintPlan`, its candidate is
`reviews.sprintPlan.review.candidate`, and its state is
`reviews.sprintPlan.review.review.state`. Do not flatten these levels.

Plan self-check: the JavaScript regression below failed against unchanged source
(108 existing tests passed; the new example failed). With only the planned guard
removal and identity fragment applied in memory, all 109 tests passed. This
validates the example without modifying product source. The matrix and browser
tests remain implementation work; no browser verification is claimed here.

## Task 1: Establish isolated execution and add the failing rendering regression

**Files:** Modify `tests/test_workflow_position_display.mjs` only during the red step.

**Interfaces:** Consume the existing `loadFrontend`, `acceptedSprintStatus`,
`validatedSprintOwner`, and `storyReview` helpers. Exercise the real
`deliveryPanelMarkup(position, reviews, actions, context)`; do not stub the review
helper or call it instead of the delivery panel for the regression.

- [ ] Read both the design and plan. Record current branch, HEAD, and dirty status.
  If HEAD advanced, inspect the relevant diff and retain compatible changes. If
  the cause or interfaces materially changed, return evidence to Astra before
  changing the design.
- [ ] Use `superpowers:using-git-worktrees` to create or verify an isolated
  worktree at
  `C:/Users/atavares/Projects/agileforge/.worktrees/alex-issue-259-pending-sprint-review`
  on branch `alex/issue-259-pending-sprint-review`, based on current `master`.
  These were not created when Astra prepared the handoff. Create them only if
  absent; if already present, inspect their branch, HEAD, and changes before
  reuse. Run every subsequent edit and verification command in that execution
  checkout, not the main `master` checkout or any old #251/#252 worktree.
  Copy these two handoff documents into
  the worktree if they are still uncommitted in the source checkout. Do not stash
  or discard them from the source checkout.
- [ ] From the execution checkout run `sh ./agileforge-dev info --json` on Windows
  through the available Git shell, or `./agileforge-dev info --json` in a POSIX
  shell. Inspect the checkout/runtime paths. Use the existing test harness's
  disposable profile for browser work.
- [ ] Add this focused regression beside the existing Sprint tests:

```javascript
test('issue 259: completed Sprint and distinct pending plan coexist', async () => {
    const context = loadFrontend();
    const owner = await validatedSprintOwner(context);
    const status = acceptedSprintStatus();
    status.sprint.status = 'completed';
    status.sprint.completed_at = '2026-09-08T00:00:00Z';
    status.accepted_plan.status = 'completed';
    status.start = {
        start_id: 81,
        sprint_id: status.sprint.sprint_id,
        sprint_plan_artifact_id: status.accepted_plan.sprint_plan_artifact_id,
        sprint_plan_artifact_decision_id: status.accepted_plan.sprint_plan_artifact_decision_id,
        plan_fingerprint: status.accepted_plan.plan_fingerprint,
        candidate_set_fingerprint: status.accepted_plan.candidate_set_fingerprint,
        task_content_fingerprint: status.accepted_plan.task_content_fingerprint,
    };
    assert.ok(await context.validateSprintStatusProjection(status, 7));
    const selected = {
        binding: {
            decision_fingerprint: `sha256:${'f'.repeat(64)}`,
            instance_key: null,
        },
        review: {
            phase: 'sprint_plan',
            project_id: 7,
            candidate: {
                sprint_plan_artifact_id: 42,
                artifact_fingerprint: `sha256:${'9'.repeat(64)}`,
                sprint_owner: owner,
                sprint_goal: 'Deliver the next distinct scope.',
                selected_stories: [{
                    ...storyReview('backlog_item:PBI-000003').review.candidate.story_items[0],
                    story_title: 'Next distinct Story',
                    story_points: 5,
                    tasks: [{
                        description: 'Implement the next distinct Task.',
                        task_kind: 'implementation',
                        checklist_items: ['Verify the next scope.'],
                        specification_evidence: [],
                    }],
                }],
            },
            review: { state: 'pending' },
        },
    };
    const markup = context.deliveryPanelMarkup(
        { decisions: [] },
        { backlog: {}, sprintPlan: selected },
        [],
        { sprintStatus: { kind: 'ready', data: status } },
    );
    assert.ok(markup.includes('data-sprint-status="completed"'));
    assert.ok(markup.includes('Sprint #31 is complete'));
    assert.ok(markup.includes('Ship accepted scope.'));
    assert.ok(markup.includes('data-planning-review-card="sprint"'));
    assert.ok(markup.includes('data-sprint-plan-identity="true"'));
    assert.ok(markup.includes('Plan #42'));
    assert.ok(markup.includes('Pending review'));
    assert.ok(markup.includes('Deliver the next distinct scope.'));
    assert.ok(markup.includes('Next distinct Story'));
    assert.ok(markup.includes('(derived: 5 pts)'));
    assert.ok(markup.includes('Implement the next distinct Task.'));
    for (const decision of ['accepted', 'feedback', 'rejected']) {
        assert.ok(markup.includes(`data-review-decision="${decision}"`));
    }
    assert.ok(!markup.includes('data-direct-action="start_sprint"'));
});
```

- [ ] Run `node --test --test-name-pattern="issue 259" tests/test_workflow_position_display.mjs`.
  Record the failure at the missing Sprint review card. A fixture exception,
  skipped test, or empty selection is not the intended red result.
- [ ] Extend this fixture into a small local test helper if needed for the matrix
  below. Preserve the same owner object after validation: owner validation is
  object-identity based, so cloning it afterward invalidates the fixture.

| Additional case | Required assertion |
| --- | --- |
| No Sprint (`kind: 'absent'`) + valid first pending plan | Review and all controls visible |
| Planned / active / completed Sprint + absent review | No Sprint review card |
| Completed Sprint + accepted / feedback / rejected review | No Sprint review card |
| Completed Sprint + missing binding | No Sprint review card |
| Completed Sprint + missing candidate or invalid owner | No Sprint review card, preserving existing behavior |
| Invalid/missing plan display ID | No fabricated identity label; existing validity behavior unchanged |
| Sprint-status error + independently valid pending review | Status error remains; pending review is not suppressed by status; no Start |

Assert Story points through the existing rendered points text or a scoped browser
assertion, not by matching an unrelated digit elsewhere in the full panel.

## Task 2: Apply the bounded production change

**Files:** Modify `frontend/project.js` only.

**Interfaces:** No API or function-signature changes. Continue using the existing
`positiveInteger` and `escapeWorkflowText` helpers.

- [ ] Replace only the suppressing ternary in `deliveryPanelMarkup`:

```diff
-        context?.sprintStatus?.kind === 'ready'
-            ? ''
-            : planningReviewCardMarkup('Sprint plan review', reviews.sprintPlan, 'sprint', 0),
+        planningReviewCardMarkup('Sprint plan review', reviews.sprintPlan, 'sprint', 0),
```

- [ ] Inside `planningReviewCardMarkup`, after its existing `candidate` variable
  is established and after the existing validity checks, compute this optional
  display fragment:

```javascript
const sprintPlanIdentity = scope === 'sprint'
    && positiveInteger(candidate?.sprint_plan_artifact_id)
    ? `<p class="mt-1 text-xs text-slate-600" data-sprint-plan-identity="true">Plan #${escapeWorkflowText(candidate.sprint_plan_artifact_id)} · Pending review</p>`
    : '';
```

- [ ] Interpolate `${sprintPlanIdentity}` immediately after the existing card
  heading and before its content. Preserve the Backlog-specific title/focus,
  Story acceptance checks, all buttons, and the rest of the helper.
- [ ] Run `node tests/test_workflow_position_display.mjs`. Record the new total,
  which must include the added regressions; the old 108-test count alone is not
  evidence for the new assertions.
- [ ] Inspect the diff. Confirm the `record_sprint_plan` filter and all decision,
  mutation, projection-validation, and backend code are unchanged.

## Task 3: Add browser coverage for loading, confirmation, and submission

**Files:** Modify `tests/e2e/test_single_project_lifecycle_ui.py`.

**Interfaces:** Reuse `dashboard_harness` and `_open_project_page`. Use a narrowly
scoped synthetic fixture derived from `SprintContinuityLifecycle`; do not change
the existing #227 fixture's behavior. Its `_sprint_status_response()` returns
`(status_code, envelope)` with data under `envelope['data']`. Its inherited
`planning_review_overrides` supplies review response data by endpoint. Its
`_assert_fields` already checks actor, idempotency key, and forbidden body fields.

- [ ] Add a local `Issue259Lifecycle` dataclass deriving from
  `SprintContinuityLifecycle`, with independent per-instance review request
  recording. Keep state in the Python fake, never in `page.evaluate` assignments
  to production lifecycle globals.
- [ ] Seed these exact, mutually distinct authorities using existing fixture
  builders and `cast('JsonObject', ...)` at typed dictionary boundaries:

| Surface | Seed |
| --- | --- |
| Historical Sprint | Sprint 31, completed, Plan 41, original accepted goal |
| Pending review | Plan 42; new goal, a different Story title, 5 points, and distinct Task description |
| Review binding | A valid synthetic SHA-256 decision fingerprint, null instance |
| Position | Current `planning.sprint.review` / `decide_sprint_plan`, available, `SPRINT_PLAN_REVIEW_REQUIRED`, same fingerprint |
| Start | No available start action while pending |
| Post-acceptance projection | Different Sprint 32, planned, accepted Plan 42 with matching review content |

Use `super()._sprint_status_response()` to obtain a fresh valid status envelope;
modify its Sprint status/ID, accepted-plan status/ID and evidence, and Task Sprint
IDs consistently. Historical completion uses a concrete `completed_at`. Preserve
the owner projection. A completed Sprint also requires valid start evidence;
changing a planned fixture's status alone is invalid. Populate `data['start']`
with `start_id: 61` and the historical plan's exact `sprint_id`,
`sprint_plan_artifact_id`, `sprint_plan_artifact_decision_id`, `plan_fingerprint`,
`candidate_set_fingerprint`, and `task_content_fingerprint`. For the new planned
Sprint, set `start` to `None` and `completed_at` to `None`. Keep accepted-plan
Story points/Task counts consistent with its `total_points`, `task_count`, and
`data['tasks']`. Use `estimated_effort: 'S'` in the pending Story so the existing
renderer emits `(derived: 5 pts)`; include valid INVEST evidence using
`_valid_invest_assessment_payload()`. Do not claim actual backend acceptance from
this fake.

- [ ] Override `_mutate` only for `/sprint/decide` and reject unexpected
  `/sprint/start` or `/sprint/generate` calls in this scenario. Before recording a
  successful decision, check:

```python
self._assert_fields(body, {"decision", "rationale"})
assert headers.get("x-agileforge-expected-decision") == self.sprint_plan_decision_fingerprint
assert "x-agileforge-expected-instance" not in headers
assert body["decision"] in {"accepted", "feedback", "rejected"}
```

The fixture must validate the incoming fingerprint against its current authority,
not merely echo the request. Record the body and header copy. On accepted, expose
the distinct planned Sprint 32. On feedback/rejected, remove the pending review
and retain the completed historical Sprint. Do not add a real start operation.

- [ ] Add `test_issue_259_pending_next_plan_survives_reload_and_confirms_decision`,
  parameterized over `accepted`, `feedback`, and `rejected`. With a fresh fixture
  per parameter, implement these exact browser assertions:

```python
completed = page.locator('[data-sprint-status="completed"]')
review = page.locator('[data-planning-review-card="sprint"]')
expect(completed).to_contain_text("Sprint #31 is complete")
expect(review).to_contain_text("Plan #42")
expect(review).to_contain_text("Pending review")
expect(review).to_contain_text("Deliver the next distinct scope.")
expect(review).to_contain_text("Next distinct Story")
expect(review).to_contain_text("(derived: 5 pts)")
expect(review).to_contain_text("Implement the next distinct Task.")
expect(page.locator('[data-direct-action="start_sprint"]')).to_have_count(0)
assert fake.review_requests == []
page.reload(wait_until="networkidle")
expect(completed).to_be_visible()
expect(review).to_be_visible()
for choice in ("accepted", "feedback", "rejected"):
    expect(review.locator(f'[data-review-decision="{choice}"]')).to_be_enabled()
review.locator(f'[data-review-decision="{decision}"]').click()
expect(page.locator("#human-action-dialog")).to_be_visible()
assert fake.review_requests == []
page.locator("#human-action-rationale").fill("Reviewed the distinct next plan.")
page.locator("#human-action-submit").click()
expect(page.locator("#human-action-dialog")).not_to_be_visible()
expect(review).to_have_count(0)
assert len(fake.review_requests) == 1
assert fake.review_requests[0]["decision"] == decision
assert fake.review_requests[0]["rationale"] == "Reviewed the distinct next plan."
```

Here `page` and `context` come from `_open_project_page(dashboard_harness, fake)`,
`fake` is the new `Issue259Lifecycle(repositories={})`, and `decision` is the
pytest parameter. Close the context in `finally`. Also check no API errors, no
generation/start requests, Story points inside `review`, and the appropriate
planned/completed post-decision surface. In the accepted case assert Sprint 32
is planned, has the new goal, and is not active. The existing #227 test continues
to verify the separate exact-start flow.

- [ ] Add `test_issue_259_stale_confirmation_preserves_current_review`. Open the
  Plan 42 confirmation dialog; then change the fake's current decision
  fingerprint and pending candidate to distinct Plan 43 before submitting. The
  fake must return HTTP 409 for the old captured fingerprint and leave successful
  decisions empty. Assert the UI reports the stale review, refreshes to Plan 43,
  retains the completed Sprint, and never starts or generates anything. Reuse
  the repository's existing conflict-response shape.
- [ ] Run `uv run --locked pytest tests/e2e/test_single_project_lifecycle_ui.py -k test_issue_259`.
  Confirm the named tests actually ran, none were silently skipped, and fresh
  load/reload used served production code and routed synthetic APIs.

## Task 4: Verify, commit locally, and return evidence to Astra

**Files:** Review only the scoped files and the two handoff documents.

- [ ] Run the adjacent Sprint browser coverage once after the final edit:

```text
uv run --locked pytest tests/e2e/test_single_project_lifecycle_ui.py -k "test_issue_259 or test_issue_227 or test_sprint_review_browser or test_issue_212_delivery_generation_lifecycle_flow"
```

- [ ] Run the three existing frontend suites, matching CI:

```text
node --test tests/test_workflow_position_display.mjs tests/test_create_project_modal_required_fields.mjs tests/test_vision_interview_ui.mjs
```

- [ ] Run `git diff --check` and the checkout's canonical `check` command:
  `sh ./agileforge-dev check --json` on Windows with Git shell, or
  `./agileforge-dev check --json` on POSIX. Preserve raw logs and exit statuses.
  Do not edit source while the gate runs. If environment prerequisites fail,
  record the exact failing stage and keep it distinct from a code failure or a
  pass; do not bypass it or repair unrelated files.
- [ ] Inspect the final diff against the design. Verify review identity is read
  from pending candidate 42, never historical accepted Plan 41; no transport or
  backend changes; no generation filter change; no live data use.
- [ ] Commit the scoped change locally on the implementation branch with a
  conventional message such as `fix: show pending Sprint review after completion`.
  Do not push, create a PR, merge, close #259, or alter a live runtime in this
  execution handoff.
- [ ] Return the review packet below. Keep the implementation worktree available
  while Astra is reviewing it. Once review/integration is finished, remove that
  temporary worktree using the repository's normal worktree cleanup; never remove
  a worktree containing unpreserved changes.

## Required review packet

1. Exact model/effort as exposed by the current Gemini session; state unavailable
   if the UI does not expose them. Do not guess or silently switch models.
2. Execution checkout, branch, base SHA, final commit SHA, and `git status --short`.
3. `git diff --stat` and a concise explanation of each changed production area.
4. Red regression evidence, then exact commands, exit codes, and counts for the
   final Node, focused browser, adjacent browser, and canonical checks.
5. Paths to raw logs and synthetic screenshots if captured. No operator content.
6. Confirmation that both distinct surfaces survived reload; each button used
   human confirmation; captured binding and null-instance behavior were checked;
   stale confirmation was rejected; Start/generation were not triggered.
7. Any deviations, unrun checks, failures, or skipped tests. Do not call a mocked
   browser acceptance a verified backend transaction or a deployment.

Routine fixture corrections are within the plan. Return to Astra when evidence
requires a production change outside the renderer/identity scope, a changed
decision contract, or a revised design. Do not broaden the repair to get a green
test result.
