# Issue #259: Pending Sprint plan review beside completed Sprint

Status: implementation handoff prepared by Astra; no product code changed.

Source: https://github.com/arduinitavares/agileforge/issues/259

Reviewed baseline: `844fc0d3ba2cd2dbb87627bb4c09eb87a90d8b16`.

## Problem and decision

`deliveryPanelMarkup` suppresses the pending Sprint plan review whenever the
Sprint-status projection has `kind === 'ready'`. That is a projection-validity
classification, not a claim that no new plan needs review. A completed historical
Sprint and a distinct pending next plan are legitimate concurrent projections.

Invoke `planningReviewCardMarkup('Sprint plan review', reviews.sprintPlan,
'sprint', 0)` independently of Sprint status. Keep the helper's existing pending,
binding, content, and owner checks. Keep backend projection and decision logic.

Show a small `Plan #<artifact ID> · Pending review` label inside a valid Sprint
review card when its candidate supplies a positive integer
`sprint_plan_artifact_id`. Read the ID from the displayed review candidate, not
from the completed Sprint's accepted plan. Keep the current review heading and
completed Sprint heading. Do not display fingerprints or introduce a new API.
Absence of optional display metadata must not change existing review validity.

## Required behavior

- Completed Sprint 31 / accepted Plan 41 remains visible with its original goal.
- Distinct pending Plan 42 renders its own identity, goal, Story scope, points,
  Tasks, and Accept / Request changes / Reject controls.
- Both surfaces survive a fresh browser load and reload.
- Each decision opens the existing human-confirmation dialog. No POST occurs
  merely from loading, reloading, or opening that dialog.
- Confirmed submission uses the captured current decision fingerprint, the
  existing rationale/actor/idempotency payload, and the existing endpoint.
  Sprint reviews currently have `instance_key: null`; do not require an instance
  header for that case or invent an artifact selector in the request body.
- Successful synthetic acceptance can refresh to a distinct planned Sprint
  associated with Plan 42. It must not automatically start that Sprint.
- Start remains unavailable while the plan is pending. Preserve first-plan,
  planned, active, and completed-without-pending behavior.
- Preserve existing stale/malformed projection and conflict handling; regression
  tests must exercise it without changing its implementation.
- All tests use synthetic fixtures and disposable profiles, with no provider
  calls or reads/copies/mutations of operator business or trace databases.

## Scope

Production edits belong in `frontend/project.js`; tests belong in
`tests/test_workflow_position_display.mjs` and
`tests/e2e/test_single_project_lifecycle_ui.py`.

Do not change `record_sprint_plan` action filtering, candidate selection,
completed-Story filtering, backend projections, workflow guards, decision
transport, runtime configuration, dependency versions, or historical evidence.

Issue #232 covers completed-Story selection and correction controls. It does not
explicitly own restoring next-plan generation from the dashboard. Leave the
generation filter unchanged without claiming that separate work is already fully
tracked there.

## Corrected audit evidence

- The suppressing ternary was introduced by
  `eb7237f09f67f23722ac2442919980fd3be5781f` (`fix: preserve accepted Sprint
  continuity`), not `5cdaee33`.
- Astra reproduced the suppression and proposed removal in memory at the baseline.
  Six rendering scenarios behaved as expected. The existing Node suite passed
  108 tests against both original and in-memory modified source.
- Those checks did not modify source and did not prove browser interaction or
  server-side acceptance for the new composite scenario. Gemini must supply new
  regression evidence. Browser API fakes establish UI behavior, not durable
  backend transaction correctness.

## Roles and completion boundary

Astra owns this design and independently reviews the resulting diff and raw
verification evidence. Gemini implements and tests the bounded plan. Routine
fixture and formatting details can be resolved locally; an architectural or
scope change returns to Astra with evidence.

The user's paste of the companion implementation instruction starts execution.
The output is a reviewable local branch/commit and verification report. It is not
authorization to publish, merge, close issues, accept an operator's Sprint plan,
start a Sprint, or change a live runtime.
