# Backlog Feedback Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the exact reviewed Backlog and human Feedback visible while its correction is ready, active, failed, or recoverable, without weakening durable attempt serialization, stale/replay guards, lineage, provider behavior, or human review authority.

**Architecture:** Keep workflow evaluation and durable read projections unchanged. Compose the current graph position with the existing durable Backlog review in `AgileForgeApplication.backlog_review()`. In the browser, validate durable Feedback display independently from correction-action executability. Add one Backlog-correction module token for same-tab stale-render protection; rely on the existing durable node-attempt lease for cross-tab duplicate protection.

**Tech Stack:** Python 3.13, FastAPI application services, SQLModel, browser JavaScript, Node test runner, pytest, Playwright.

**Spec:** [GitHub issue #213](https://github.com/arduinitavares/agileforge/issues/213); corrected exact-baseline audit at `.superpowers/sdd/2026-08-29-backlog-feedback-context/audit.md`; governing review at `.superpowers/sdd/2026-08-29-backlog-feedback-context/preimplementation-review.md`.

## Global Constraints

- Work only in `/Users/aaat/projects/agileforge/.worktrees/issue-213-backlog-feedback-context` on `dev/issue-213-backlog-feedback-context` from cumulative base `8a5ead19d150556bf1f404f6219176870affe502`.
- Preserve #211's module-level Specification mutation lock, source-state validation, and browser-side deferred-fetch test pattern. Backlog correction state is separate.
- Keep workflow rules, durable attempts, provider inputs/calls, review writes, accepted-Backlog selection, artifact lineage, replay order, stale guards, and human authority unchanged.
- Make no provider calls, profile/database mutation outside tests, GitHub mutation, push, merge, or manual browser action.
- Add no route, request/header field, model, migration, dependency, compatibility branch, or generalized delivery lock.
- Never render machine fingerprints, graph reason codes, internal bindings, reviewer identity, or attempt identity as visible copy.
- Keep #217 Story controls and #218 progressive Story readiness outside this change.
- Use `uv` for Python commands. Use this checkout's `./agileforge-dev` only for the final clean committed acceptance gate.

## Root Cause

Feedback persistence and correction input are already complete. A terminal `BacklogArtifactDecision` retains the reviewed artifact fingerprint, `feedback`, rationale, reviewer, idempotency key, and time. The graph then keeps the reviewed Backlog as the current leaf and changes `backlog.generate` through revision-ready, active, failed-retry, and expired-recovery states. The existing durable Backlog projection still returns the exact candidate, Specification and Product Goal lineage, content, and rationale.

The gap is application/browser composition:

1. `AgileForgeApplication.backlog_review()` only selects `backlog.review`, so every post-Feedback `backlog.generate` state becomes `PLANNING_REVIEW_NOT_AVAILABLE`.
2. `frontend/project.js` therefore cannot show the durable Feedback context and labels every Backlog generation as initial generation.
3. The generic delivery busy state is tied to a DOM control. A rerender can replace that control while its POST is unresolved, allowing a same-tab duplicate. Cross-tab serialization already belongs to the server's durable attempt lease and must not be reimplemented in JavaScript.

No evidence requires `services/read_projections.py` or `api.py` production changes. The former already returns every durable field needed for the join. The latter already serializes either successful application read shape without transformation and maps projection conflicts to HTTP 409.

## Production State Matrix

The application recognizes Feedback continuation only after joining a unique reviewed Backlog projection to one current `backlog.generate` decision. `instance_key` is `null` in every row. `request_kind` is `record_backlog_draft` in every continuation row.

| State | Exact decision tuple: category / recommendation / reason | Exact fact references | `GET /backlog/review` | Visible state | Executable action | Focus when initiated in this tab |
| --- | --- | --- | --- | --- | --- | --- |
| revision-ready | `available` / `recovery` / `BACKLOG_REVISION_REQUIRED` | one `backlog`, one `specification`, one `product_goal`; no `node_attempt`; no other fact type | `{continuation: {binding, review}}` | Heading `Backlog Feedback recorded`; reviewed candidate identity/content and rationale | Exactly one matching advertised semantic `backlog/generate` action; label `Regenerate Backlog from feedback` | Fresh correction button; fallback to continuation container |
| active | `waiting` / `required` / `BACKLOG_GENERATION_ACTIVE` | one `backlog`, one `specification`, one `product_goal`; no `node_attempt`; no other fact type | Same continuation shape, bound to the active decision | `Backlog correction is in progress. The recorded Feedback remains current.` | None. Missing action is expected, not a projection error | Continuation container |
| failed-retry | `available` / `recovery` / `BACKLOG_GENERATION_FAILED` | one `backlog`, one `specification`, one `product_goal`, exactly one `node_attempt`; no other fact type | Same continuation shape, bound to the failed decision | `Backlog correction failed. No corrected candidate was produced; the recorded Feedback remains current.` | Exactly one matching advertised correction action | Fresh correction button; fallback to continuation container |
| expired-recovery | `available` / `recovery` / `BACKLOG_GENERATION_RECOVERY_REQUIRED` | one `backlog`, one `specification`, one `product_goal`, exactly one `node_attempt`; no other fact type | Same continuation shape, bound to the recovery decision | `The previous Backlog correction attempt expired. The recorded Feedback remains current and can be retried.` | Exactly one matching advertised correction action | Fresh correction button; fallback to continuation container |
| corrected-pending | `waiting` / `required` / `BACKLOG_REVIEW_REQUIRED` on `backlog.review` and `decide_backlog` | one corrected `backlog`, one `specification`, one `product_goal`; no other fact type | Existing pending `{binding, review}` shape unchanged | `Corrected Backlog candidate v{version} (#{id}), replacing #{parent_id}` plus current review controls | No correction action | `[data-planning-review-card="backlog"]`, made programmatically focusable with `tabindex="-1"` |
| valid absence | No current Feedback continuation. Required examples: initial `BACKLOG_GENERATION_REQUIRED` with only Specification/Goal references; accepted `BACKLOG_CORRECTION_AVAILABLE` optional re-entry; terminal non-Feedback review | Whatever the unchanged workflow state requires; never reinterpret it as Feedback | Existing `PLANNING_REVIEW_NOT_AVAILABLE` | Existing initial/accepted/non-Feedback UI; no Feedback projection alert | Existing advertised behavior unchanged | No #213 focus movement |

For all four continuation states, additionally require:

- exactly one decision for the selected mode;
- `node_id == "backlog.generate"`;
- `request_kind == "record_backlog_draft"`;
- `instance_key is None`;
- exact category/recommendation/reason from the matrix;
- selected durable `(backlog_artifact_id, artifact_fingerprint)` equals the `backlog` fact reference;
- projected `candidate.backlog_artifact_id` and `candidate.artifact_fingerprint` equal that same selected identity;
- projected `lineage.specification.spec_version_id/spec_hash` equal the `specification` reference;
- projected `lineage.product_goal.product_goal_artifact_id/product_goal_fingerprint` equal the `product_goal` reference;
- projected `review.state == "feedback"` and trimmed `review.rationale` is non-empty.

The `node_attempt` reference proves only the state-specific graph binding for failed or expired recovery. It is never displayed and does not change the reviewed candidate identity.

## Response And Browser Contracts

Pending response stays byte-shape compatible:

```json
{
  "binding": {
    "decision_fingerprint": "<pending backlog.review decision>",
    "instance_key": null
  },
  "review": { "phase": "backlog", "candidate": {}, "review": { "state": "pending" } }
}
```

Feedback continuation is additive:

```json
{
  "continuation": {
    "binding": {
      "node_id": "backlog.generate",
      "instance_key": null,
      "decision_fingerprint": "<current continuation decision>"
    },
    "review": {
      "phase": "backlog",
      "lineage": {
        "specification": { "spec_version_id": 31, "spec_hash": "<hidden>" },
        "product_goal": {
          "product_goal_artifact_id": 21,
          "product_goal_fingerprint": "<hidden>"
        }
      },
      "candidate": {
        "backlog_artifact_id": 7,
        "artifact_fingerprint": "<hidden>",
        "version_number": 1,
        "supersedes_backlog_artifact_id": null,
        "backlog_items": []
      },
      "review": { "state": "feedback", "rationale": "Show retry context." }
    }
  }
}
```

The browser exposes two pure contracts:

```javascript
backlogFeedbackContinuationProjection(state)
// { kind: 'display', mode, decision, candidate, review }
// { kind: 'absent' }
// { kind: 'error', code: 'BACKLOG_FEEDBACK_PROJECTION_INVALID' }

backlogCorrectionActionBinding(state, continuation)
// { kind: 'ready', action }
// { kind: 'unavailable', reason: 'absent' | 'active' }
// { kind: 'error', code: 'BACKLOG_CORRECTION_ACTION_INVALID' }
```

`backlogFeedbackContinuationProjection()` validates durable display context without consulting advertised actions. `backlogCorrectionActionBinding()` consumes a valid display projection and validates an executable action only for revision-ready, failed-retry, or expired-recovery. Active Feedback remains visible when the server correctly advertises no action. An invalid action renders the valid Feedback plus an alert and no enabled correction control.

## Mutation Token Contract

`activeBacklogCorrectionMutation` is module-local and applies only to Feedback correction. It never claims cross-tab authority.

```javascript
{
  token,
  phase: 'submitting' | 'awaiting_authority' | 'recovering_failure',
  action,
  backlogArtifactId,
  decisionFingerprint,
  focusIntent
}
```

- Create it synchronously before the correction POST. `submitting` blocks every same-tab exact or stale replacement correction control, including controls introduced by rerender.
- After a 2xx POST, change to `awaiting_authority` before calling `loadDashboard()`. A failed, aborted, or superseded reload preserves the token and keeps old/replacement correction controls disabled because the outcome is uncertain.
- After a rejected or network-failed POST, change to `recovering_failure` before authoritative reload. Do not clear in `finally`.
- `loadDashboard()` snapshots token and phase at start, as #211 does. A load started during `submitting` cannot clear the token. An aborted, superseded, failed, or malformed load cannot clear it.
- A successful load started in `awaiting_authority` clears only after it proves either: (a) corrected-pending candidate whose `supersedes_backlog_artifact_id` equals the token's reviewed artifact; or (b) the exact prior continuation is no longer current and the server returns a valid non-Feedback absence/current state. If the same correction decision remains, preserve the token.
- A successful load started in `recovering_failure` clears only after it renders an authoritative current state. For revision-ready, failed-retry, or expired-recovery, re-enable only the freshly rendered exact action. For active, keep no action. For corrected-pending or valid absence, never revive the detached old action.
- A page reload/new tab starts with no module token. Its controls are governed entirely by the durable server state: active has no action; failed/expired exposes retry; corrected-pending exposes review.
- Server-owned duplicate protection remains the durable `WorkflowNodeAttempt` lease. A forced POST from another tab during active state must return HTTP 409 with `TRANSITION_NOT_AVAILABLE` before a second provider entry. JavaScript does not synchronize tabs.

Focus is armed only by the initiating tab and consumed once after authoritative rendering:

- revision/failed/expired: `[data-backlog-correction-action="true"]:not([disabled])`, fallback `[data-backlog-feedback-continuation="true"]`;
- active: `[data-backlog-feedback-continuation="true"]`;
- malformed projection/action: `[data-backlog-feedback-projection-error="true"]`;
- corrected pending: `[data-planning-review-card="backlog"]`;
- ordinary refresh or a new tab: no automatic focus movement.

## Six-File Scope

| File | Responsibility |
| --- | --- |
| `services/application.py` | Join current pending review or exact Feedback continuation to the existing durable Backlog projection. |
| `frontend/project.js` | Validate display/action separately; render context and state; own correction token reconciliation and focus. |
| `tests/adapters/test_api_workflow_domain.py` | Real-domain lifecycle, malformed-position application tests, response modes, and route serialization. |
| `tests/test_dashboard_review_safety.mjs` | Escaping and exact hidden-canary coverage for Feedback rendering. |
| `tests/test_workflow_position_display.mjs` | State matrix, typed pure contracts, exact copy, deterministic unresolved-fetch module lock, and token cleanup. |
| `tests/e2e/test_single_project_lifecycle_ui.py` | Visible Feedback/correction lifecycle, refresh/new tab, server duplicate rejection, durable failure/recovery, success/reload failure, and focus. |

No production edit is expected in `services/read_projections.py`, `api.py`, workflow definitions/handlers/domain, models/migrations, providers, CLI code, or HTML.

### Task 1: Exact Application Projection Across Attempt States

**Files:**
- Modify: `tests/adapters/test_api_workflow_domain.py`
- Modify: `services/application.py`

**Interfaces:**
- Consume one captured `WorkflowPosition`, `DeliveryReviewSelectionService.review_identity()`, and `DurableReadProjectionService.backlog_review()`.
- Produce pending `{binding, review}` unchanged, Feedback `{continuation: {binding, review}}`, existing `PLANNING_REVIEW_NOT_AVAILABLE` for valid absence, or `WORKFLOW_FACT_CONFLICT` for a candidate Feedback join that is ambiguous, malformed, or torn.

- [ ] **Step 1: Add a real-domain lifecycle RED test**

Add `test_application_backlog_feedback_continuation_tracks_real_attempt_lifecycle`. Use the test SQLite `engine`, real `WorkflowDomain` with a mutable fixed clock, real `DeliveryReviewSelectionService`, and real `DurableReadProjectionService`. Reuse existing Specification/Goal and Backlog persistence helpers; do not call a provider.

The test must create and assert this exact sequence:

1. Persist Backlog candidate 7 with real Specification and Product Goal parents. Before review, `backlog_review()` returns the unchanged pending shape.
2. Apply real `DecideBacklog(decision="feedback", rationale="Show the retry boundary.")`. The graph becomes revision-ready and `backlog_review()` must return continuation for candidate 7 and the exact rationale.
3. Apply real `StartNodeAttempt` against the captured `backlog.generate` decision. The graph becomes active. The continuation remains successful and bound to the new active decision; its decision has no `node_attempt` reference.
4. Apply real `FailNodeAttempt(failure_code="PROVIDER_UNAVAILABLE")`. The graph becomes failed-retry. The same continuation remains successful and the decision has exactly one matching `node_attempt` reference.
5. Start a second real attempt from failed-retry, advance the mutable clock to its lease expiry, and read again. The graph becomes expired-recovery. The same continuation remains successful and references exactly the second expired attempt.
6. At every continuation state, assert exact request/category/recommendation/reason/instance, exact Backlog/Specification/Goal reference equality, exact candidate/lineage equality, unchanged content/rationale, and one durable projection call per application read.
7. While the first attempt is active, submit a second start from the newly captured waiting position and assert `TRANSITION_NOT_AVAILABLE` plus one durable live attempt. This is the backend proof for cross-tab serialization.

**RED expectation:** Pending assertions pass. Revision-ready, active, failed-retry, and expired-recovery assertions each fail with `PLANNING_REVIEW_NOT_AVAILABLE`, because current `backlog_review()` ignores `backlog.generate`.

- [ ] **Step 2: Add exact malformed-position RED tests**

Build production-shaped `NodeDecision` fixtures with exact references and a selection fake that returns nullable identity and counts calls. Parameterize one mutation at a time:

- wrong `request_kind`;
- non-null `instance_key`;
- wrong category for each reason;
- wrong recommendation for each reason;
- wrong or unknown reason while a Backlog reference identifies a candidate continuation;
- zero, duplicate, or wrong `backlog` reference;
- zero, duplicate, or wrong `specification` reference;
- zero, duplicate, or wrong `product_goal` reference;
- unexpected fact type;
- `node_attempt` present in revision-ready or active;
- absent/duplicate `node_attempt` in failed-retry or expired-recovery;
- selected durable Backlog identity mismatch;
- projected candidate ID/fingerprint mismatch;
- projected Specification ID/hash mismatch;
- projected Product Goal ID/fingerprint mismatch;
- pending/accepted terminal state under a continuation tuple;
- blank Feedback rationale;
- pending and continuation modes both present;
- two candidate continuation decisions.

Assert `WORKFLOW_FACT_CONFLICT` exactly. Cases rejected before durable selection assert zero projection calls. Selection-identity failures assert zero projection calls. Candidate/lineage/review failures assert exactly one projection call. Keep explicit GREEN regression cases for initial generation, accepted optional correction, and a terminal rejected Backlog: each returns `PLANNING_REVIEW_NOT_AVAILABLE`, not a conflict.

**RED expectation:** Each new malformed test fails because the current method returns `PLANNING_REVIEW_NOT_AVAILABLE`; call-count assertions prevent an unrelated error path from satisfying the test.

- [ ] **Step 3: Add route-mode characterization coverage without editing `api.py`**

Extend the existing `_FakeApplication`/`TestClient` route test for `GET /api/projects/41/backlog/review`. Parameterize the fake application to return:

- existing pending `{binding, review}`;
- additive `{continuation: {binding, review}}`.

Assert HTTP 200, `status == "success"`, and exact unmodified `data` for each mode.

Run the new route test by itself after writing it:

```bash
uv run --frozen pytest -q tests/adapters/test_api_workflow_domain.py \
  -k 'backlog_review_route_modes'
```

**Characterization expectation:** Both pending and continuation cases pass immediately because `_read_payload()` already transports successful application data unchanged. A failure here disproves the six-file design and must be investigated before editing `api.py`.

- [ ] **Step 4: Run Task 1 RED**

```bash
uv run --frozen pytest -q tests/adapters/test_api_workflow_domain.py \
  -k 'backlog_feedback_continuation or backlog_continuation_position'
```

Expected: the named continuation and malformed-position tests fail for the exact reasons above; existing pending and valid-absence controls pass. The separately run route characterization remains green.

- [ ] **Step 5: Implement the minimal Backlog-only application composition**

Refactor only `AgileForgeApplication.backlog_review()` and add private Backlog-specific validators near the existing planning-review helpers.

1. Capture one position.
2. Preserve the existing unique pending `backlog.review` behavior.
3. Inspect only `backlog.generate` decisions that identify a reviewed Backlog. Treat the exact accepted optional tuple and non-Feedback terminal review as valid absence.
4. Validate the matrix tuple and exact fact cardinality before durable selection.
5. Resolve Backlog identity with `DeliveryReviewSelectionService.review_identity()`.
6. Call `self.reads.backlog_review()` once.
7. Validate candidate, Specification, Product Goal, terminal Feedback state, and rationale against the decision references.
8. Return the continuation binding using the same captured decision fingerprint and instance.
9. Preserve durable projection error envelopes. Return `WORKFLOW_FACT_CONFLICT` for ambiguity/torn candidate data and `PLANNING_REVIEW_NOT_AVAILABLE` only for valid absence.

Do not modify `_unique_planning_review()` for Roadmap, Story, or Sprint plan. Do not change `DurableReadProjectionService`, API routes, workflow rules, or mutation contracts.

- [ ] **Step 6: Run Task 1 GREEN**

```bash
uv run --frozen pytest -q tests/adapters/test_api_workflow_domain.py \
  -k 'backlog_feedback_continuation or backlog_continuation_position or backlog_review_route_modes or application_review_read or delivery_review_selection'
```

Expected: every selected named test passes. In particular, all four continuation states return `ok=True`, malformed joins return exactly `WORKFLOW_FACT_CONFLICT`, pending shape is unchanged, valid absence returns exactly `PLANNING_REVIEW_NOT_AVAILABLE`, and both route response modes serialize unchanged.

- [ ] **Step 7: Commit Task 1 implementation**

```bash
git add services/application.py tests/adapters/test_api_workflow_domain.py
git commit -m "fix(backlog): project durable Feedback lifecycle (#213)"
```

### Task 2: Independent Display And Action Contracts

**Files:**
- Modify: `tests/test_dashboard_review_safety.mjs`
- Modify: `tests/test_workflow_position_display.mjs`
- Modify: `frontend/project.js`

**Interfaces:**
- Consume Task 1 continuation, current `position.decisions`, and advertised `actions`.
- Produce the two typed pure contracts, safe Feedback markup for all four modes, exact correction action details for three executable modes, and corrected-pending identity.

- [ ] **Step 1: Add state-matrix and pure-contract RED tests**

In `tests/test_workflow_position_display.mjs`, create one production-shaped fixture per matrix row. Assert:

- `backlogFeedbackContinuationProjection()` returns `kind: 'display'` and exact mode for revision-ready, active, failed-retry, and expired-recovery;
- active returns display even with no advertised action;
- `backlogCorrectionActionBinding()` returns `ready` only for revision-ready, failed-retry, and expired-recovery with exactly one matching action;
- active returns `{kind: 'unavailable', reason: 'active'}`;
- true absence returns `{kind: 'absent'}` and action `{kind: 'unavailable', reason: 'absent'}`;
- malformed continuation returns display `error` independently of action;
- a valid display with missing, duplicate, wrong endpoint, wrong transport, wrong node/request/instance action returns action `error` while preserving the Feedback markup;
- initial generation remains `Generate Backlog` / `Generating Backlog...`;
- correction remains `Regenerate Backlog from feedback` / `Regenerating Backlog from feedback...`;
- corrected pending renders `Corrected Backlog candidate v2 (#8), replacing #7`.

**RED expectation:** Calls to both new helpers fail because they do not exist; Feedback mode markup/copy assertions fail because the browser only renders pending review and generic initial generation.

- [ ] **Step 2: Add safe-rendering RED tests with exact hidden canaries**

In `tests/test_dashboard_review_safety.mjs`, render hostile Backlog content and rationale in each continuation mode. Include a legitimate visible string such as `sha256:customer-token` to prove lexical prefixes are allowed. Use exact hidden values:

- decision: `sha256:hidden-backlog-decision-canary`;
- candidate: `sha256:hidden-backlog-artifact-canary`;
- Specification: `sha256:hidden-specification-canary`;
- Product Goal: `sha256:hidden-product-goal-canary`;
- attempt: `sha256:hidden-node-attempt-canary`;
- reviewer: `reviewer-hidden@example.com`.

Assert hostile content is escaped; the visible `sha256:customer-token` survives; every exact hidden canary is absent; graph reason codes are absent; no terminal review buttons render; and the durable candidate content/rationale remain visible even when action validation fails.

**RED expectation:** Feedback heading/content/canary assertions fail because no continuation renderer exists. The test must not use a broad `!markup.includes('sha256:')` ban.

- [ ] **Step 3: Run Task 2 RED**

```bash
node --test tests/test_dashboard_review_safety.mjs tests/test_workflow_position_display.mjs
```

Expected: the named new state-matrix, pure-contract, identity, and canary tests fail; existing baseline tests pass.

- [ ] **Step 4: Implement display projection and action binding separately**

Add `backlogFeedbackContinuationProjection(state)` with the exact matrix and join rules. It must not inspect `state.actions`. Add `backlogCorrectionActionBinding(state, continuation)` using existing action matching conventions and exact semantic endpoint `backlog/generate`.

Add focused markup helpers that:

- render one `[data-backlog-feedback-continuation="true"]` container with `tabindex="-1"`;
- render the candidate version/ID, full escaped current Backlog content, Feedback rationale, and the state-specific human status;
- never render Feedback as a pending review card;
- render `[data-backlog-feedback-projection-error="true"]` with `role="alert"` and `tabindex="-1"` for invalid display or action data;
- retain valid Feedback content when only action data is invalid;
- render `[data-backlog-correction-action="true"]` only for a `ready` action;
- prepend candidate identity to pending Backlog review and include the parent identity for corrected pending;
- give the pending Backlog card `tabindex="-1"` for programmatic post-success focus.

Use `escapeWorkflowText()` for every server string. Keep all bindings and fingerprints in memory only. Reuse existing delivery action binding attributes instead of inventing a second matcher.

- [ ] **Step 5: Run Task 2 GREEN**

```bash
node --test tests/test_dashboard_review_safety.mjs tests/test_workflow_position_display.mjs
```

Expected: both files exit 0. Every matrix row, typed result, exact label/status, corrected identity, hostile-string escape, visible lexical `sha256:` example, and exact hidden-canary assertion passes; all pre-existing Node tests remain green.

- [ ] **Step 6: Commit Task 2 implementation**

```bash
git add frontend/project.js tests/test_dashboard_review_safety.mjs tests/test_workflow_position_display.mjs
git commit -m "fix(ui): render durable Backlog Feedback states (#213)"
```

### Task 3: Deterministic Same-Tab Lock And Reconciliation

**Files:**
- Modify: `tests/test_workflow_position_display.mjs`
- Modify: `frontend/project.js`

**Interfaces:**
- Consume Task 2's exact continuation/action contracts and existing `captureDeliveryActionBinding()`, `deliveryActionElementMatches()`, `currentDeliveryActionContainers()`, and `loadDashboard()` sequence/abort protection.
- Produce the Backlog-only token phases, rerender lock reapplication, authoritative cleanup, and one-shot focus intent.

- [ ] **Step 1: Add unresolved-fetch module-lock RED coverage**

Add `test('Backlog correction module lock blocks a stale rerender until authority reconciles', ...)` in `tests/test_workflow_position_display.mjs`.

1. Install a fetch promise whose correction POST remains unresolved.
2. Start one exact Feedback correction and assert one POST, token phase `submitting`, disabled control, `aria-busy="true"`, and busy text.
3. Replace the DOM representation with a newly rendered stale revision-ready correction control while the promise is unresolved.
4. Reapply active mutation state and activate the replacement twice. Assert it remains disabled and POST count stays one.
5. Resolve the POST successfully but make the next dashboard reload fail. Assert phase `awaiting_authority`, token retained, and old/replacement action disabled.
6. Run a successful authoritative corrected-pending reload. Assert token cleared only then and focus intent targets the corrected pending card.

**RED expectation:** The replacement control is enabled and can issue a second POST because current busy state is DOM-local; no Backlog token/phases exist; completed-but-reload-failed cleanup unlocks too early.

- [ ] **Step 2: Add failure and load-sequencing RED coverage**

Add focused tests for:

- rejected POST changes `submitting -> recovering_failure` before reload;
- successful authoritative failed-retry reload clears the token and enables only the freshly rendered matching action;
- authoritative active reload clears local failure recovery but renders no action;
- stale authoritative revision with a different decision fingerprint clears recovery and enables only the fresh action;
- a load started during `submitting` cannot clear;
- aborted, superseded, failed, or malformed loads preserve all phases;
- a load started in `awaiting_authority` that still returns the same continuation cannot clear;
- a valid non-Feedback absence may clear `awaiting_authority` without reviving detached controls;
- focus chooses exact retry, active continuation, projection error, or corrected card selectors and is consumed once.

**RED expectation:** Every token lifecycle assertion fails because current delivery generation has no Backlog module token and clears/unlocks from generic `finally` behavior.

- [ ] **Step 3: Run Task 3 RED**

```bash
node --test tests/test_workflow_position_display.mjs \
  --test-name-pattern='Backlog correction|Backlog Feedback focus'
```

Expected: all new lock, phase, cleanup, and focus tests fail for the exact missing-state behaviors above.

- [ ] **Step 4: Implement the Backlog-only token**

Add `activeBacklogCorrectionMutation` beside, but independent of, `activeSpecificationMutation`, `activeStoryMutation`, and `activeDependencyMutation`.

- Detect correction only from a Task 2 `ready` binding, not from button text.
- Create the token before `postAction()` and transition phases exactly as specified in Mutation Token Contract.
- Add a focused `reapplyActiveBacklogCorrectionMutation()` after delivery rendering. Reuse `deliveryActionElementMatches()` and `currentDeliveryActionContainers()`.
- Snapshot token/phase at `loadDashboard()` start. Reconcile only after sequence/abort checks and after the complete authoritative state has been assigned.
- Branch generic `runDirectAction()` only for Backlog Feedback correction. Preserve all other delivery action behavior.
- Never clear this token from generic `finally`. Never re-enable the detached initiating button after rerender.
- Keep #211 Specification mutation code unchanged.

- [ ] **Step 5: Run Task 3 GREEN**

```bash
node --test tests/test_workflow_position_display.mjs \
  --test-name-pattern='Backlog correction|Backlog Feedback focus'
node --test tests/test_dashboard_review_safety.mjs tests/test_workflow_position_display.mjs
```

Expected: every named lock/phase/focus test passes, then both complete Node files exit 0 with all old and new tests green. The unresolved POST records exactly one fetch; reload failure retains the token; only an authoritative qualifying load clears it.

- [ ] **Step 6: Commit Task 3 implementation**

```bash
git add frontend/project.js tests/test_workflow_position_display.mjs
git commit -m "fix(ui): lock Backlog correction until authority reloads (#213)"
```

### Task 4: Browser Lifecycle, Durable Recovery, And Focus

**Files:**
- Modify: `tests/e2e/test_single_project_lifecycle_ui.py`
- Modify: `frontend/project.js` only if E2E exposes a contract mismatch already specified above

**Interfaces:**
- Consume the exact response modes, matrix fixtures, token behavior, and focus selectors.
- Prove visible end-to-end behavior. Do not use E2E as the sole same-tab lock or durable-attempt proof.

- [ ] **Step 1: Add a production-shaped issue-213 fixture without `threading.Event`**

Layer a small Backlog correction lifecycle helper over `FakeLifecycle`; do not broadly rewrite unrelated fake state. It must retain candidate 7/v1, exact Specification/Goal lineage, Feedback rationale, decision fingerprint per state, state-specific references, action availability, candidate 8/v2 parent 7 after success, request counts, and simulated provider-entry count.

Use the existing #211 browser-side deferred-fetch pattern from `test_issue_204_structuring_reports_local_state_and_reloads_successor`: replace `window.fetch` for the exact correction POST with an unresolved Promise and expose a resolver that returns an exact synthetic `Response`. Do not add `threading.Event`, sleep-based route blocking, or a blocking Playwright route callback.

The fixture must return exact HTTP contracts:

- active forced duplicate POST: HTTP 409, `detail.error.code == "TRANSITION_NOT_AVAILABLE"`, active position, no correction action, provider-entry count unchanged;
- durable provider failure: HTTP 409, `detail.error.code == "EXTERNAL_EXECUTION_FAILED"`; following GETs expose failed-retry decision, one `node_attempt` reference, continuation, and retry action;
- stale rejection: HTTP 409, `detail.error.code == "STALE_POSITION"`; following GETs expose the replacement current decision/action;
- success: HTTP 200 success; following GETs expose corrected-pending candidate 8/v2 superseding 7;
- injected reload failure: exact HTTP 409 read error for one load, followed by normal authoritative responses.

- [ ] **Step 2: Add Feedback, refresh, and new-tab RED**

Add `test_issue_213_feedback_context_survives_refresh_and_new_tab`:

1. Request changes with rationale `Show the retry boundary.` and double-activate submit; assert one review POST.
2. Assert revision-ready heading, candidate `v1 (#7)`, full reviewed content, exact rationale, correction label, and focus on the correction action or continuation fallback.
3. Click the existing dashboard refresh and repeat visible assertions.
4. Open another page in the routed browser context. Assert the same context is reconstructed from durable fake state and the new page does not steal focus through a #213 focus intent.

**RED expectation:** The endpoint is treated as absent after Feedback; heading/rationale/identity/correction-label/focus assertions fail. Existing double-submit protection should remain one POST.

- [ ] **Step 3: Add active, cross-tab duplicate, failure, and expired RED**

Add `test_issue_213_active_failure_and_expiry_are_durable` using the deferred browser fetch:

1. Start correction; wait for the unresolved Promise; assert initiating control busy text and one provider entry.
2. Put the fake's durable state in active, refresh the initiating page, and open/refresh a second page. Both must show the active status and Feedback context with no correction action.
3. Force the exact correction POST from the second page. Assert HTTP 409 `TRANSITION_NOT_AVAILABLE`, no second provider entry, and no enabled action.
4. Put durable state in failed-retry and resolve the initiating POST with HTTP 409 `EXTERNAL_EXECUTION_FAILED`. Assert the initiating page reloads authoritative state, shows durable failure text, focuses the fresh retry action, and never re-enables the detached old button.
5. Reload and open a new tab after failure. Both show the same rationale/candidate/failure and an enabled exact retry.
6. Start a retry, put durable state in expired-recovery, then reload and open a new tab. Both show exact expired recovery text and action; no local live-region state is required to recover.

**RED expectation:** Active loses Feedback because display depends on an advertised action; the fake lacks server-owned active rejection; failure/expiry do not survive reload/new tab; focus and exact HTTP assertions fail.

- [ ] **Step 4: Add stale, success, and reload-failure RED**

Add `test_issue_213_correction_reconciles_stale_and_successful_outcomes`:

1. Return `STALE_POSITION` and a replacement revision-ready decision. Assert the old token/action is discarded only after reload, the fresh action is enabled, and focus lands on it.
2. Retry and return HTTP 200 while forcing the first post-success dashboard reload to fail. Assert visible uncertain-outcome error, token retained, and every stale/replacement correction action disabled.
3. Trigger the next successful dashboard refresh. Assert candidate `Corrected Backlog candidate v2 (#8), replacing #7`, normal pending review controls, token cleared, and focus on `[data-planning-review-card="backlog"]`.
4. Reload and open a new tab after success. Both show corrected pending review and never show the old Feedback correction action.

**RED expectation:** Generic delivery cleanup unlocks after reload failure; corrected identity/parent/focus do not exist; old Feedback context is not reconciled by exact lineage.

- [ ] **Step 5: Run Task 4 RED**

```bash
uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py \
  -k 'issue_213'
```

Expected: all three new issue-213 E2E tests fail at the exact missing continuation, durable-state, HTTP, lock, identity, or focus assertions described above.

- [ ] **Step 6: Complete only specified browser behavior exposed by E2E**

Adjust `frontend/project.js` only where the E2E test exposes a mismatch with Tasks 2-3. Do not move server state into JavaScript, add tab synchronization, change API contracts, or alter #211 controls.

- [ ] **Step 7: Run Task 4 GREEN**

```bash
uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py \
  -k 'issue_204_structuring or issue_211 or issue_212 or issue_213'
```

Expected: all selected #204/#211/#212/#213 lifecycle tests pass. The issue-213 tests prove visible state and focus across refresh/new tab; the Node test remains the deterministic same-tab lock proof; the real-domain application test remains the durable lease proof.

- [ ] **Step 8: Commit Task 4 implementation**

```bash
git add frontend/project.js tests/e2e/test_single_project_lifecycle_ui.py
git commit -m "test(e2e): cover Backlog Feedback recovery (#213)"
```

### Task 5: Scope And Verification

**Expected modified files:**

```text
services/application.py
frontend/project.js
tests/adapters/test_api_workflow_domain.py
tests/test_dashboard_review_safety.mjs
tests/test_workflow_position_display.mjs
tests/e2e/test_single_project_lifecycle_ui.py
```

- [ ] **Step 1: Run focused contracts**

```bash
uv run --frozen pytest -q tests/adapters/test_api_workflow_domain.py \
  -k 'backlog_feedback_continuation or backlog_continuation_position or backlog_review_route_modes or application_review_read or delivery_review_selection'
node --test tests/test_dashboard_review_safety.mjs tests/test_workflow_position_display.mjs
uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py \
  -k 'issue_204_structuring or issue_211 or issue_212 or issue_213'
```

Expected: all selected Python tests pass, both complete Node files pass, and all selected lifecycle E2E tests pass with zero failures.

- [ ] **Step 2: Run adjacent workflow and projection regressions**

```bash
uv run --frozen pytest -q \
  tests/workflow/test_vision_backlog_graph.py \
  tests/workflow/test_vision_backlog_transitions.py \
  tests/adapters/test_api_workflow_domain.py
```

Expected: all tests pass without changes to workflow or durable projection semantics.

- [ ] **Step 3: Audit scope and hidden data**

```bash
PLAN_BASE=$(git log -1 --format=%H -- docs/superpowers/plans/2026-08-29-backlog-feedback-context.md)
git diff --name-only "$PLAN_BASE"...HEAD
git diff --check "$PLAN_BASE"...HEAD
rg -n 'hidden-backlog-decision-canary|hidden-backlog-artifact-canary|hidden-specification-canary|hidden-product-goal-canary|hidden-node-attempt-canary|reviewer-hidden@example.com' \
  tests/test_dashboard_review_safety.mjs
```

Expected: the implementation diff after the committed plan contains exactly the six files above; `git diff --check` is silent; exact canaries exist only in tests and their assertions prove they are absent from rendered markup.

- [ ] **Step 4: Commit any verification-only test correction**

Only if a test correction was required, stage only the six scoped files and use a narrow conventional commit. Do not add audit/ledger files, generated output, profiles, databases, or caches.

- [ ] **Step 5: Run clean committed acceptance**

```bash
./agileforge-dev info --json
./agileforge-dev check --json
```

Expected: `info` reports this exact worktree/branch/committed SHA and isolated profile paths; the provider-free check succeeds from a clean committed checkout. Do not run a provider action or manual browser acceptance.

## Explicit Non-Goals

- No backend workflow or provider redesign.
- No new review-history endpoint or read-projection query.
- No browser expected-decision header for generation.
- No cross-tab JavaScript lock.
- No changed human review authority or automatic acceptance.
- No generalized delivery-generation concurrency refactor.
- No #217/#218 work.

## Remaining Decisions

No blocking technical decision remains. Product wording can change later, but this plan fixes exact copy so implementation and tests are deterministic. A generalized compare-and-swap contract for every delivery-generation route is separate work; #213 composes the existing durable attempt authority rather than expanding that API.
