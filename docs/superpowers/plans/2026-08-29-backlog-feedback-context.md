# Backlog Feedback Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve exact Backlog Feedback context and present the next generation as a correction, with durable, accessible behavior across failure, refresh, duplicate activation, another tab, and focus transitions.

**Architecture:** Keep workflow, persistence, provider input, lineage, and review authority unchanged. Extend the existing application-level `backlog_review()` read composition to expose the already durable terminal Feedback projection when the graph offers the exact `BACKLOG_REVISION_REQUIRED` recovery decision, then make `frontend/project.js` fail closed unless that continuation, current position decision, reviewed artifact, and advertised action all agree.

**Tech Stack:** Python 3.13, FastAPI application services, SQLModel durable read projections, browser JavaScript, Node test runner, pytest, Playwright.

**Spec:** [GitHub issue #213](https://github.com/arduinitavares/agileforge/issues/213); exact-baseline audit at `.superpowers/sdd/2026-08-29-backlog-feedback-context/audit.md`.

## Global Constraints

- Work only in `/Users/aaat/projects/agileforge/.worktrees/issue-213-backlog-feedback-context` on `dev/issue-213-backlog-feedback-context` from `8a5ead19d150556bf1f404f6219176870affe502`.
- Planning was performed against the exact cumulative #211 baseline; retain its module-level Specification mutation lock and source-state behavior.
- Keep backend workflow semantics, provider behavior, human review authority, accepted-Backlog selection, and artifact lineage unchanged.
- Make no provider calls, profile/database mutations, GitHub mutations, pushes, merges, or manual browser actions.
- Do not add a route, request field, expected-decision header, database column, migration, dependency, or compatibility branch.
- Do not expose fingerprints, graph reason codes, internal bindings, reviewer identity, or machine lineage in visible browser copy.
- Keep #217 Story controls and #218 progressive Story readiness outside this change.
- Use `uv` for Python commands and this checkout's `./agileforge-dev` for the clean committed acceptance gate.

---

## Audited Contract

The exact base already provides:

- append-only Backlog Feedback persistence with exact artifact fingerprint and rationale;
- `BACKLOG_REVISION_REQUIRED` plus recovery recommendation and exact prior Backlog fact reference;
- correction input containing accepted Specification, prior canonical Backlog, Feedback rationale, and superseded artifact ID;
- terminal durable review projection containing candidate ID, fingerprint, version, parent, full content, and rationale;
- review replay/stale guards and generation replay/current-position recapture.

The defect is the read/UI join. `AgileForgeApplication.backlog_review()` selects only the pending `backlog.review` decision. After Feedback, the endpoint reports absence even though `backlog.generate` references the exact reviewed artifact. The browser then renders the advertised action with initial-generation copy.

Keep the pending response unchanged. Add one terminal mode:

```json
{
  "continuation": {
    "binding": {
      "node_id": "backlog.generate",
      "instance_key": null,
      "decision_fingerprint": "<current recovery decision>"
    },
    "review": {
      "phase": "backlog",
      "candidate": {
        "backlog_artifact_id": 123,
        "artifact_fingerprint": "sha256:<reviewed artifact>",
        "version_number": 1,
        "supersedes_backlog_artifact_id": null,
        "backlog_items": []
      },
      "review": {
        "state": "feedback",
        "rationale": "Add explicit correction context."
      }
    }
  }
}
```

The browser may render and enable correction only when all of these agree:

1. exactly one position decision has node `backlog.generate`, request `record_backlog_draft`, reason `BACKLOG_REVISION_REQUIRED`, recommendation `recovery`, category `available`, and the continuation decision fingerprint/instance;
2. that decision has exactly one `backlog` fact reference;
3. the projected candidate ID/fingerprint equal that fact reference;
4. the projected review state is `feedback` with a non-empty rationale;
5. exactly one advertised action matches node, instance, request kind, endpoint `backlog/generate`, and semantic transport.

If any check fails, render a `role="alert"` projection error and no enabled Backlog correction action.

Visible copy is exact for test stability:

- state heading: `Backlog Feedback recorded`;
- candidate identity: `Backlog candidate v{version} (#{artifact_id})`;
- explanation: `The next generation uses the accepted Specification, this reviewed candidate, and the Feedback below.`;
- action: `Regenerate Backlog from feedback`;
- busy: `Regenerating Backlog from feedback...`;
- failed generation suffix: `No corrected candidate was produced. The prior Backlog Feedback remains current.`;
- corrected pending identity: `Corrected Backlog candidate v{version} (#{artifact_id}), replacing #{supersedes_id}`.

## File Map

| File | Responsibility |
| --- | --- |
| `services/application.py` | Select one pending Backlog review or one exact terminal Feedback continuation from current graph decisions. |
| `frontend/project.js` | Validate the cross-read binding; render identity/content/rationale; label correction; preserve lock, status, reconciliation, and focus. |
| `tests/adapters/test_api_workflow_domain.py` | Lock application read modes, exact continuation binding, and fail-closed ambiguity. |
| `tests/test_dashboard_review_safety.mjs` | Lock safe visible continuation content and machine-binding concealment. |
| `tests/test_workflow_position_display.mjs` | Lock initial/revision/corrected copy, exact binding, busy/failure state, and rerender duplicate lock. |
| `tests/e2e/test_single_project_lifecycle_ui.py` | Exercise Feedback through correction with refresh, another tab, stale/failure/success, duplicate prevention, and focus. |

No production change is expected in `services/read_projections.py`, `api.py`, models, workflow definitions/handlers, provider adapters, or CLI code.

### Task 1: Exact Feedback Continuation Read

**Files:**
- Modify: `tests/adapters/test_api_workflow_domain.py`
- Modify: `services/application.py:1857-1865`
- Modify: `services/application.py:1934-1980`

**Interfaces:**
- Consumes: current `WorkflowPosition`, `_available_decisions()`, `_DeliveryReviewSelectionPort.review_identity()`, and `self.reads.backlog_review(project_id=..., backlog_artifact_id=...)`.
- Produces: pending `{binding, review}` unchanged; Feedback `{continuation: {binding, review}}`; existing `PLANNING_REVIEW_NOT_AVAILABLE` for true absence; existing projection conflict envelope for ambiguity or mismatch.

- [ ] **Step 1: Write the failing application contract tests**

Add focused tests beside `test_application_review_read_returns_exact_selected_content_and_binding()`:

```python
class _Reads:
    def __init__(self, *, backlog_payload: JsonObject | None = None) -> None:
        self.backlog_payload = backlog_payload

    def backlog_review(
        self, *, project_id: int, backlog_artifact_id: int
    ) -> JsonObject:
        data = self.backlog_payload or {
            "phase": "backlog",
            "project": project_id,
            "artifact": backlog_artifact_id,
        }
        return {"ok": True, "data": data, "warnings": [], "errors": []}

def test_application_backlog_read_returns_exact_feedback_continuation() -> None:
    revision = _position("backlog.generate", "record_backlog_draft", None)
    revision_decision = revision.decisions[0].model_copy(
        update={
            "category": NodeCategory.AVAILABLE,
            "recommendation_kind": RecommendationKind.RECOVERY,
            "reason_code": "BACKLOG_REVISION_REQUIRED",
            "decision_fingerprint": "decision-feedback-correction",
        }
    )
    position = revision.model_copy(update={"decisions": (revision_decision,)})
    reads = _Reads(
        backlog_payload={
            "phase": "backlog",
            "candidate": {
                "backlog_artifact_id": 7,
                "artifact_fingerprint": "sha256:" + "a" * 64,
                "version_number": 1,
            },
            "review": {"state": "feedback", "rationale": "Clarify retries."},
        }
    )
    result = AgileForgeApplication(
        workflow_domain=_Domain(position),
        read_projection=cast("Any", reads),
        delivery_review_selection=_Selection(),
    ).backlog_review(41)

    data = _object(result["data"])
    continuation = _object(data["continuation"])
    assert continuation["binding"] == {
        "node_id": "backlog.generate",
        "instance_key": None,
        "decision_fingerprint": "decision-feedback-correction",
    }
    assert _object(continuation["review"])["review"] == {
        "state": "feedback",
        "rationale": "Clarify retries.",
    }
```

Add parameterized fail-closed cases for:

- pending review plus Feedback recovery both present;
- two Feedback recovery decisions;
- recovery decision with wrong reason or recommendation;
- selection identity absent;
- projected candidate ID/fingerprint different from the selected identity;
- projected terminal state other than `feedback`;
- blank rationale.

Retain the existing pending response assertion unchanged and retain true-absence `PLANNING_REVIEW_NOT_AVAILABLE`.

- [ ] **Step 2: Run the RED tests**

Run:

```bash
uv run --frozen pytest -q tests/adapters/test_api_workflow_domain.py -k 'application_backlog_read or application_review_read'
```

Expected: the new Feedback continuation tests fail because `backlog_review()` only checks `backlog.review` and returns absence.

- [ ] **Step 3: Implement the minimal application read composition**

Refactor only the Backlog read path. Do not generalize Roadmap/Story/Sprint reads.

```python
def backlog_review(self, project_id: int) -> JsonObject:
    position = self.position(project_id=project_id)
    pending = _available_decisions(position, "backlog.review")
    feedback = tuple(
        decision
        for decision in _available_decisions(position, "backlog.generate")
        if decision.reason_code == "BACKLOG_REVISION_REQUIRED"
        and decision.recommendation_kind is RecommendationKind.RECOVERY
    )
    # Exactly one mode is allowed. Project pending as today; project Feedback
    # through the same durable Backlog review reader and exact selected identity.
```

Implement this directly in `backlog_review()` and leave `_unique_planning_review()` unchanged for Roadmap and Sprint-plan reads. Resolve one selected identity through `selection.review_identity()`, call `self.reads.backlog_review()` once, and preserve any durable projection error envelope. For continuation mode, validate the selected `(artifact_id, fingerprint)` against `data.candidate.backlog_artifact_id`, `data.candidate.artifact_fingerprint`, `data.review.state == "feedback"`, and a non-empty rationale before returning `_planning_review_read_success({"continuation": ...})`. Pending mode must return the existing `_planning_review_read_success({"binding": ..., "review": ...})` shape exactly.

Do not modify `DurableReadProjectionService`, the GET route, or mutation contracts.

- [ ] **Step 4: Run Task 1 GREEN checks**

```bash
uv run --frozen pytest -q tests/adapters/test_api_workflow_domain.py -k 'application_backlog_read or application_review_read or delivery_review_selection'
```

Expected: all selected tests pass; pending review shape remains unchanged.

- [ ] **Step 5: Commit Task 1**

```bash
git add services/application.py tests/adapters/test_api_workflow_domain.py
git commit -m "fix(backlog): project exact Feedback continuation (#213)"
```

### Task 2: Fail-Closed Backlog Feedback Presentation

**Files:**
- Modify: `tests/test_dashboard_review_safety.mjs`
- Modify: `tests/test_workflow_position_display.mjs`
- Modify: `frontend/project.js:73-79`
- Modify: `frontend/project.js:1627-1633`
- Modify: `frontend/project.js:1686-1855`
- Modify: `frontend/project.js:2286-2339`

**Interfaces:**
- Consumes: Task 1 `planningReviews.backlog.continuation`, `lifecycleState.position.decisions`, and `lifecycleState.actions`.
- Produces: `backlogFeedbackContinuationBinding(state)`, visible post-Feedback markup, correction-specific action details, and corrected pending candidate identity markup. Task 3 relies on the returned exact action/binding and stable data attributes.

- [ ] **Step 1: Write safe-rendering RED tests**

In `tests/test_dashboard_review_safety.mjs`, add a terminal Feedback fixture with hostile rationale/content and assert:

```javascript
const markup = context.deliveryPanelMarkup(position, reviews, actions, state);
assert.ok(markup.includes('Backlog Feedback recorded'));
assert.ok(markup.includes('Backlog candidate v1 (#7)'));
assert.ok(markup.includes('&lt;img src=x onerror=alert(1)&gt;'));
assert.ok(markup.includes('Regenerate Backlog from feedback'));
assert.ok(!markup.includes('data-planning-review="backlog"'));
assert.ok(!markup.includes('decision-feedback-correction'));
assert.ok(!markup.includes('sha256:'));
```

Add one table-driven test that changes each exact join input in turn: decision fingerprint, node, instance, reason, recommendation, backlog fact ID/fingerprint, candidate ID/fingerprint, review state/rationale, advertised endpoint/transport. Every case must contain `data-backlog-feedback-projection-error="true"` and no enabled correction button.

- [ ] **Step 2: Write intent and identity RED tests**

In `tests/test_workflow_position_display.mjs`, add exact assertions for:

```javascript
assert.equal(initial.label, 'Generate Backlog');
assert.equal(revision.label, 'Regenerate Backlog from feedback');
assert.equal(revision.busyLabel, 'Regenerating Backlog from feedback...');
assert.ok(correctedPendingMarkup.includes(
    'Corrected Backlog candidate v2 (#8), replacing #7',
));
```

Also assert initial Backlog copy is unchanged and a pending version-1 candidate is `Backlog candidate v1 (#7)`.

- [ ] **Step 3: Run Task 2 RED tests**

```bash
node --test tests/test_dashboard_review_safety.mjs tests/test_workflow_position_display.mjs
```

Expected: new tests fail because Backlog actions use static initial copy and no continuation renderer exists.

- [ ] **Step 4: Implement the exact browser join and presentation**

Add focused helpers in `frontend/project.js`:

```javascript
function backlogFeedbackContinuationBinding(state) {
    // Return { action, decision, candidate, review } only after all five
    // Audited Contract joins pass. Return null for any missing/duplicate/torn value.
}

function backlogCandidateIdentityMarkup(candidate) {
    // Require positive integer ID/version. Render parent only when it is a
    // positive integer; never render fingerprints.
}

function backlogFeedbackContinuationMarkup(state) {
    // Render status, identity, rationale, reviewed Backlog content, explanation,
    // action, and live status. Do not render review decision controls.
}
```

Use `escapeWorkflowText()` for every server string. Keep machine values only in memory. Give the continuation `data-backlog-feedback-continuation="true"`, `tabindex="-1"`, and a stable correction-action data attribute for Task 3.

Make `deliveryGenerationActionDetails()` branch on the exact Backlog position reason. Initial generation retains existing copy. Feedback revision consumes only `backlogFeedbackContinuationBinding()` and returns the exact correction label/busy/description. A revision action without valid continuation returns a projection-error result; `deliveryGenerationActionMarkup()` renders an alert and no control.

Prepend `backlogCandidateIdentityMarkup()` to pending Backlog review content so a successful correction is visibly versioned and names its parent.

- [ ] **Step 5: Run Task 2 GREEN checks**

```bash
node --test tests/test_dashboard_review_safety.mjs tests/test_workflow_position_display.mjs
```

Expected: all tests pass, including the original 60 baseline tests.

- [ ] **Step 6: Commit Task 2**

```bash
git add frontend/project.js tests/test_dashboard_review_safety.mjs tests/test_workflow_position_display.mjs
git commit -m "fix(ui): preserve Backlog Feedback context (#213)"
```

### Task 3: Correction Lifecycle Lock, Status, And Focus

**Files:**
- Modify: `tests/e2e/test_single_project_lifecycle_ui.py:157-347`
- Modify: `tests/e2e/test_single_project_lifecycle_ui.py:901-924`
- Modify: `tests/e2e/test_single_project_lifecycle_ui.py:1155-1310`
- Modify: `tests/e2e/test_single_project_lifecycle_ui.py:2280-2314`
- Modify: `frontend/project.js:123-147`
- Modify: `frontend/project.js:2377-2431`
- Modify: `frontend/project.js:2860-3137`
- Modify: `frontend/project.js:3235-3500`

**Interfaces:**
- Consumes: Task 2 `backlogFeedbackContinuationBinding()`, continuation/action data attributes, and candidate identity markup.
- Produces: one Backlog-correction-only module mutation token, rerender lock reapplication, durable failure/status copy, and post-review/post-generation focus restoration. No other delivery or Specification mutation state changes.

- [ ] **Step 1: Make the provider-free fake model production-shaped**

Replace the Boolean-only Backlog fake state with exact artifact/review state:

```python
from threading import Event

backlog_candidate: JsonObject | None = None
backlog_review_decision: Literal["accepted", "feedback"] | None = None
backlog_feedback_rationale: str | None = None
backlog_artifact_id: int = 0
backlog_version_number: int = 0
backlog_supersedes_artifact_id: int | None = None
backlog_decision_requests: list[JsonObject] = field(default_factory=list)
backlog_generation_gate: Event | None = None
backlog_generation_stale_once: bool = False
```

Make `/backlog/review` return the Task 1 pending or continuation shape. Make `/position` expose exact Backlog reason/recommendation/fact references. Make Feedback preserve candidate 7/v1 and make correction generation produce candidate 8/v2 superseding 7, then reset review state to pending. Existing acceptance flow must continue to set `accepted` and proceed to Roadmap.

- [ ] **Step 2: Write the durable Feedback/refresh/new-tab RED**

Add `test_issue_213_preserves_backlog_feedback_context_across_refresh_and_new_tab`:

```python
page.locator('[data-planning-review="backlog"][data-review-decision="feedback"]').click()
page.locator("#human-action-rationale").fill("Show the retry boundary.")
page.locator("#human-action-submit").dblclick()

assert len(fake.backlog_decision_requests) == 1
expect(page.locator('[data-backlog-feedback-continuation="true"]')).to_contain_text(
    "Backlog Feedback recorded"
)
expect(page.locator('[data-backlog-feedback-continuation="true"]')).to_contain_text(
    "Show the retry boundary."
)
expect(page.locator('[data-direct-action="record_backlog_draft"]')).to_have_text(
    re.compile("Regenerate Backlog from feedback")
)
assert page.evaluate("document.activeElement?.matches('[data-backlog-feedback-continuation=true], [data-backlog-correction-action=true]')")
```

Click `#refresh-project` and repeat the state assertions. Open a second Playwright page in the same routed context and prove the same candidate identity, rationale, and correction action load from fake durable state without sharing page memory.

- [ ] **Step 3: Write busy/duplicate/failure/stale/success RED tests**

Add `test_issue_213_correction_generation_is_locked_and_reconciled` with controlled fake responses:

1. Hold the correction POST after it starts.
2. Assert `aria-busy="true"`, `Regenerating Backlog from feedback...`, and the Feedback state remain visible.
3. Trigger a dashboard refresh while held, activate the replacement control, release the gate, and assert exactly one correction request.
4. Return a durable generation failure and assert the action is restored, focused, and the live status ends with `No corrected candidate was produced. The prior Backlog Feedback remains current.`
5. Return one stale 409 after an external fake state advance; assert the old correction action disappears and current review state is loaded.
6. Retry from a fresh Feedback state and succeed; assert the pending card is focused and reads `Corrected Backlog candidate v2 (#8), replacing #7`.

- [ ] **Step 4: Run Task 3 RED tests**

```bash
uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py -k issue_213
```

Expected: tests fail on missing continuation, generic busy/failure copy, duplicate rerender activation, and absent focus restoration.

- [ ] **Step 5: Add the scoped mutation lock and focus state**

Add only:

```javascript
let activeBacklogCorrectionMutation = null;
let pendingBacklogFocus = null;
```

In `runDirectAction()`:

- classify Backlog correction through `backlogFeedbackContinuationBinding(lifecycleState)` before setting busy;
- reject entry when `activeBacklogCorrectionMutation` exists;
- store `{token, decisionFingerprint, artifactId, phase}` before POST;
- retain the same token through dashboard reload;
- on rejected generation, restore only when reconciliation proves the same continuation is current;
- on completed generation plus reload failure, keep the old action disabled as existing delivery behavior requires.

Call `reapplyActiveBacklogCorrectionMutation()` from `renderDashboard()` beside, not inside, `reapplyActiveSpecificationMutation()`. It must lock only a newly rendered exact matching correction action and must never unlock or alter Specification, Story, Roadmap, or Sprint controls.

Use `pendingBacklogFocus` to restore focus after:

- Feedback success: correction action, falling back to continuation region;
- correction failure: current exact correction action/status;
- stale reconciliation: current Backlog review or continuation region;
- correction success: new pending Backlog review card.

Clear the focus request only after a target was found and focused. Do not move focus on ordinary page refresh/new-tab load.

- [ ] **Step 6: Run Task 3 GREEN and #211/#212 regression checks**

```bash
uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py -k 'issue_211 or issue_212 or issue_213'
node --test tests/test_dashboard_review_safety.mjs tests/test_workflow_position_display.mjs
```

Expected: issue #213 tests pass; the current #211 source-state/mutation-lock tests and #212 delivery lifecycle remain green.

- [ ] **Step 7: Commit Task 3**

```bash
git add frontend/project.js tests/e2e/test_single_project_lifecycle_ui.py
git commit -m "test(e2e): cover Backlog Feedback correction flow (#213)"
```

### Task 4: Final Verification And Scope Gate

**Files:**
- Verify only; no planned source changes.

**Interfaces:**
- Consumes: Tasks 1-3 complete branch.
- Produces: clean focused/full evidence and a clean committed checkout for `./agileforge-dev check --json`.

- [ ] **Step 1: Run focused Python and browser contracts**

```bash
uv run --frozen pytest -q tests/adapters/test_api_workflow_domain.py -k 'application_backlog_read or application_review_read or delivery_review_selection'
uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py -k 'issue_211 or issue_212 or issue_213'
node --test tests/test_dashboard_review_safety.mjs tests/test_workflow_position_display.mjs
```

- [ ] **Step 2: Run affected packaging and full Node checks**

```bash
uv run --frozen pytest -q tests/test_frontend_package_resources.py
node --test tests/*.mjs
git diff --check
```

- [ ] **Step 3: Audit scope and forbidden changes**

```bash
git diff --name-only 8a5ead19d150556bf1f404f6219176870affe502...HEAD
git status --short
```

Expected tracked implementation scope is exactly the six files in the File Map. Confirm no `api.py`, `services/read_projections.py`, workflow, model, migration, provider, CLI, profile, or database file changed.

- [ ] **Step 4: Run the clean committed acceptance gate**

Commit any final test-only correction first, confirm `git status --short` is empty, then run:

```bash
./agileforge-dev check --json
```

This gate is provider-free. Do not perform manual acceptance, provider action, merge, push, or GitHub mutation.

## Expected Final Scope

Production:

- `services/application.py`
- `frontend/project.js`

Tests:

- `tests/adapters/test_api_workflow_domain.py`
- `tests/test_dashboard_review_safety.mjs`
- `tests/test_workflow_position_display.mjs`
- `tests/e2e/test_single_project_lifecycle_ui.py`

No unresolved implementation decision blocks execution. The exact action wording is fixed above for deterministic tests; changing that product copy later does not require a contract redesign.
