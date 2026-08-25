# Task 5 report: Story readiness and Sprint-selection UI

## Status

Complete on `dev/issue-223-story-readiness-sprint-candidacy`, based on clean
Task 4 handoff `a816813`.

## Delivered behavior

- Replaced the approval-like normal `Validate Story` / `Validated` UI with
  separate provider-free structural eligibility and human Sprint-selection
  states. The UI renders exact `unselected`, `selected`, and `deferred` intent
  with native `Select for Sprint`, `Remove from Sprint selection`, and `Defer`
  buttons bound to the exact Story ID and selection-state fingerprint.
- Structural proof language is explicit: passing provider-free checks proves
  structural eligibility only; it does not select for Sprint, confirm
  dependencies, validate semantic quality, or generate a Sprint.
- Current deterministic failures render their stored precise diagnostics with
  no repeat-button suggestion. Only missing or stale operational evidence
  offers `Re-run structural checks`, which posts one exact Story ID to
  `POST /api/projects/{project_id}/story/structural-eligibility/reconcile`.
- Selection posts to `POST /api/projects/{project_id}/story/sprint-selection`
  with the exact `story_id`, intent, observed state fingerprint, rationale,
  actor, and idempotency key. A transient transport retry reuses that exact
  payload/key. One active Story mutation disables all duplicate Story controls.
- The reloaded server projection remains authoritative. A mutation that
  succeeds but cannot reload keeps old Story controls locked and shows an
  error; no selection state is guessed locally. Rejected stale/conflicting
  mutations remain error-visible and unlock normally for a refresh/retry.
- Strict readiness parsing fails closed for malformed eligibility, selection,
  selected-scope, dependency-safe, or candidacy fields. Dependent selection
  controls are hidden/locked, and malformed selected scope disables dependency
  confirmation.
- Dependency review now uses the exact selected, structurally eligible scope,
  rather than already-candidate Stories. Thus one selected eligible Story can
  receive dependency confirmation while accepted siblings stay unselected;
  only then does the server-projected candidate pool and Sprint form advance.

## Accessibility decisions

- State is exposed as a semantic `ul` of text-labelled states, not color alone.
- All actions are native buttons with descriptive `aria-label`s, visible
  existing focus-token styling, disabled/`aria-disabled` fail-closed controls,
  and status/error roles already used by the dashboard.
- The readiness surface is a labelled `section`; diagnostics use an accessible
  status list and projection failures use `role="alert"`.

## TDD and verification

### Node RED

```text
node --test tests/test_workflow_position_display.mjs
5 failed, 28 passed
```

The failures covered three-state markup and proof/non-proof language, stale
selected intent, deterministic diagnostics without revalidation, malformed
projection lockout, and exact idempotent selection payloads. Recorded in
`b336451` before production JS.

```text
node --test tests/test_workflow_position_display.mjs
1 failed, 33 passed
```

The final RED asserted the successful-mutation/reload-failure lockout. Recorded
in `7c524c5` before its small production helper.

### Node GREEN and static checks

```text
node --check frontend/project.js
node --test tests/*.mjs
58 passed, 0 failed
```

### Browser RED and GREEN

```text
uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py -k progressive
1 failed, 1 passed, 16 deselected
```

The RED proved the old dependency review incorrectly required a pre-existing
candidate pool after the exact selected Story had reloaded. It was committed in
`f326d6f` before the selected-scope integration.

```text
uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py -k progressive
2 passed, 16 deselected, 4 deprecation warnings
```

The browser fake asserts acceptance-derived structural state, select/reload,
scope-only dependency confirmation, candidate/Sprint-form gating, remove,
defer, reselect, accepted-sibling exclusion, exact mutation payloads, and that
no `/sprint/generate` request occurs. It uses only the test-owned ephemeral
server, profile, browser, and route fake.

## Changed files and commits

- `frontend/project.js`
- `tests/test_workflow_position_display.mjs`
- `tests/e2e/test_single_project_lifecycle_ui.py`

- `b336451` test: define Story readiness selection UI (#223)
- `9b9ec7c` feat: separate structural eligibility from Sprint selection UI (#223)
- `f326d6f` test: cover scoped Sprint selection in browser (#223)
- `16a3062` feat: review dependencies from selected Story scope (#223)
- `7c524c5` test: lock Story controls after unresolved reload (#223)
- `22b1b5e` fix: retain Story lock until projection reloads (#223)

## Concerns and protected boundaries

- Full `uv run --frozen pyrepo-check --all` remains Task 6's integration gate.
- Browser verification uses a test fake, not a real provider or manual profile;
  it intentionally stops before provider-backed Sprint generation or
  persistence.
- No provider call, real Sprint generation/persistence, profile outside the
  ephemeral E2E fixture, push, merge, issue mutation, or live manual
  acceptance occurred.
- #224 team-name/default behavior was not changed.

## Fix round 1/5: scope completeness and fail-closed Sprint controls

### Delivered corrections

- Dependency review now retains every proposed/current edge owned by the exact
  selected-and-eligible dependent scope, including prerequisites outside that
  scope. Such prerequisites render as `External/excluded prerequisite` and are
  submitted unchanged; edges owned by unselected dependents remain excluded.
- The dependency parser treats missing/non-array `stories` or `edges`, duplicate
  Story IDs, duplicate dependency endpoints, bad IDs/reasons, mismatched Story
  projections, and missing/conflicting selected-scope fingerprints as malformed.
  Only an explicit empty `edges: []` is a valid no-edge review.
- Sprint generation no longer trusts an advertised delivery action. Every
  candidate must be a complete, selected, current-eligible, dependency-safe
  candidate with positive unique ID, selection-state fingerprint, and one
  common selected-scope fingerprint. Any absent, malformed, or contradictory
  candidate projection renders an alert and no active Sprint form.
- Readiness parsing now rejects incoherent combinations. Current eligibility
  requires validated evidence without structural failures; a current ineligible
  state requires `failed` evidence and at least one nonblank diagnostic.
  `validation_status=unvalidated` renders exact `Structural evidence missing`;
  stale prior validated evidence renders `Structural evidence stale`.
- Rejected Story mutations restore each control's exact original disabled and
  ARIA state. A successful mutation with failed reload remains locked. After a
  successful reload, focus moves to a current native control in the exact Story
  row (or the row itself if no enabled successor exists).

### RED evidence

```text
node --test tests/test_workflow_position_display.mjs
4 failed, 34 passed
```

The committed RED (`44b09c2`) covered external prerequisite retention,
missing-versus-stale evidence copy and projection contradictions, malformed
candidate Sprint-form lockout/no transport, and rejected-control restoration.

```text
uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py -k progressive
1 failed, 1 passed, 16 deselected
```

The committed browser RED also proved that successful Story selection did not
restore focus to its rerendered Story row.

### GREEN verification

```text
node --check frontend/project.js
node --test tests/*.mjs
62 passed, 0 failed

uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py -k 'progressive or sprint_generation_requires_team'
3 passed, 15 deselected, 4 deprecation warnings

uv run --frozen pyrepo-check annotations ty ruff tests/e2e/test_single_project_lifecycle_ui.py
ruff, annotations, and ty passed
```

The progressive browser fake proves an external selected-scope edge is visible
and posted exactly, the unselected sibling's dependent edge stays absent, an
advertised Sprint action has no form while candidates are absent, and no
`/sprint/generate` request occurs before the candidate gate passes.

### Fix-round commits

- `44b09c2` test: expose Story scope and Sprint gate regressions (#223)
- `fab0475` fix: fail closed on Story scope and Sprint candidates (#223)
- `6db56bd` test: keep Sprint gate browser fixture checked (#223)

### Protected-boundary confirmation

No provider call, real Sprint generation/persistence, real/manual profile,
push, merge, issue mutation, or live manual acceptance occurred. Browser work
used only the existing test-owned ephemeral server, profile, context, and API
fake. The full `pyrepo-check --all` gate remains Task 6's responsibility.

### API transport alignment

Review of the browser correction found one API-only contradiction: the
application/domain request correctly permits an external prerequisite when the
dependent is selected, but `StoryDependenciesApplyApiRequest` still rejected
both endpoints unless selected. A narrow provider-free RED was committed before
the adapter change:

```text
uv run --frozen pytest -q tests/test_story_dependencies.py -k dependency_api_accepts_external_prerequisite_for_selected_dependent
1 failed, 19 deselected
```

`api.py` now requires only the reviewed edge dependent to be selected; project
membership and prerequisite validity remain application/domain responsibilities.
CLI inspection found no equivalent reviewed-edge prevalidation, so no CLI
change was needed.

```text
uv run --frozen pytest -q tests/test_story_dependencies.py -k 'dependency_api_accepts_external_prerequisite_for_selected_dependent or selected_scope_review_preserves_unrelated_edges_and_external_visibility or external_prerequisite_blocks_until_complete_without_joining_scope'
3 passed, 17 deselected, 4 deprecation warnings

uv run --frozen pyrepo-check annotations ty ruff api.py tests/test_story_dependencies.py
ruff, annotations, and ty passed
```

- `3d5d392` test: allow external dependency prerequisite at API boundary (#223)
- `60b4e03` fix: preserve external dependency prerequisites (#223)

## Fix round 2/5: torn scope and committed dependency reload lock

### Delivered corrections

- `record_sprint_plan` now requires both a strict canonical candidate projection
  and the current strict `storyDependencies` projection. Their common
  `selected_scope_fingerprint` must match exactly. A valid-but-stale candidate
  scope now renders the existing alert and no Sprint-generation form.
- Dependency scope validation now requires one valid identical selected-scope
  fingerprint across every projected Story, including unselected siblings and
  the parallel dependency Story projection. Duplicate dependency Story rows
  fail closed too.
- Every dependency edge must use `proposed`, `active`, or `rejected` status and
  cannot be self-referential. Only `proposed`/`active` edges owned by selected
  dependents are reviewable; rejected edges stay excluded and cannot be
  silently reactivated. Explicit `edges: []` and external prerequisites remain
  valid.
- A dependency POST now has one semantic payload/idempotency key across its
  single transport retry. If the POST succeeds but authority reload returns
  false or throws, the original Confirm button remains native-disabled and
  `aria-busy=true`, with the reload error visible; no second action can create a
  new payload. Only a pre-commit failure restores its captured control state.

### RED evidence

```text
node --test tests/test_workflow_position_display.mjs
4 failed, 38 passed
```

Committed in `b9c8362` before production changes. The VM tests covered
candidate/dependency scope mismatch, a conflicting unselected sibling
fingerprint, rejected-edge exclusion, self-edge rejection, absent Sprint form,
no generation transport, and post-success dependency lock semantics.

```text
uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py -k 'torn_candidate_dependency_scope or dependency_confirmation_stays_locked'
2 failed, 18 deselected, 4 deprecation warnings
```

Committed in `b744257` before production changes. The isolated browser fake
proved the old form was visible for a torn but otherwise valid candidate scope
and the old dependency control was re-enabled after successful POST/reload
failure.

### GREEN verification

```text
node --check frontend/project.js
node --test tests/*.mjs
66 passed, 0 failed

uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py -k 'progressive or sprint_generation_requires_team or torn_candidate_dependency_scope or dependency_confirmation_stays_locked'
5 passed, 15 deselected, 4 deprecation warnings

uv run --frozen pyrepo-check annotations ty ruff tests/e2e/test_single_project_lifecycle_ui.py
ruff, annotations, and ty passed
```

The browser suite uses only a test-owned ephemeral server/profile/context and
route fake. It does not submit a Sprint form: each relevant scenario asserts
that `/sprint/generate` was never reached. The existing progressive scenario
also confirms the selected dependent to external prerequisite edge is posted
and the unselected dependent edge is excluded.

### Fix-round commits

- `b9c8362` test: cover strict Sprint scope and dependency locks
- `b744257` test: cover torn Sprint scope and dependency reload lock
- `073e178` fix: bind Sprint form to current dependency scope (#223)

### Protected-boundary confirmation

No provider call, real Sprint generation/persistence, real/manual profile,
push, merge, issue mutation, or live manual acceptance occurred. #224
team-name/default behavior remains untouched. The full `pyrepo-check --all`
gate remains Task 6's responsibility.

### Recovery follow-up

The locked post-success view is intentionally non-repeatable, but a later
successful manual authority refresh must restore a newly rendered Confirm
action. The focused E2E RED below proved the stale lock otherwise survived the
fresh projection and suppressed the new action:

```text
uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py -k dependency_confirmation_stays_locked
1 failed, 19 deselected, 4 deprecation warnings
```

- `1ea1fc7` test: restore dependency action after authority refresh (#223)
- `21d1590` fix: release dependency lock after fresh authority reload (#223)
- `687c05b` test: name dependency retry expectation (#223)

The full round-two GREEN commands now pass: `node --check frontend/project.js`,
`node --test tests/*.mjs` (66 passed), the five-scenario focused browser command
above (5 passed, 15 deselected, 4 deprecation warnings), and
`uv run --frozen pyrepo-check annotations ty ruff tests/e2e/test_single_project_lifecycle_ui.py`
(ruff, annotations, and ty passed).

## Fix round 3/5: lock-aware 409 dependency rerender

### Delivered correction

`storyDependencyReviewMarkup` now reads the unresolved dependency-mutation
sentinel. If a successful dependency POST is followed by a `loadDashboard()`
HTTP 409 that rerenders stale lifecycle state, the replacement Confirm control
is native-disabled with `aria-disabled="true"` and `aria-busy="true"`, shows
the in-progress status, and remains handler-locked. The global error retains
the exact reload-conflict message. A subsequent genuinely successful project
reload clears the sentinel before rendering a new enabled current action.
Unrelated controls are unchanged.

### RED/GREEN evidence

```text
uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py -k dependency_confirmation_replacement_stays_locked
1 failed, 20 deselected, 4 deprecation warnings
```

The committed RED (`b98945e`) exercised the actual `/story/dependencies` 409
response during the post-success dashboard reload. It proved the replacement
button was formerly enabled.

```text
node --check frontend/project.js
node --test tests/*.mjs
66 passed, 0 failed

uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py -k 'dependency_confirmation_stays_locked or dependency_confirmation_replacement_stays_locked'
2 passed, 19 deselected, 4 deprecation warnings

uv run --frozen pyrepo-check annotations ty ruff tests/e2e/test_single_project_lifecycle_ui.py
ruff, annotations, and ty passed
```

The focused browser tests assert the 409 replacement cannot post a second
payload/key and that the existing later-successful-refresh recovery produces a
new enabled valid Confirm action.

### Fix-round commits and boundaries

- `b98945e` test: lock dependency replacement on reload conflict (#223)
- `2d3d70d` fix: keep dependency replacement locked on conflict (#223)
- `f2f6947` fix: preserve conflict lock through dashboard rerender (#223)

No provider call, real Sprint generation/persistence, manual profile, push,
merge, issue mutation, or live manual acceptance occurred. #224 behavior is
unchanged; full `pyrepo-check --all` remains Task 6's gate.

## Fix round 4/5: token-bound dependency mutation phases

### Delivered correction

Dependency confirmation now has an explicit token-bound `submitting` and
`awaiting_authority` contract. One mutation owns one canonical payload and
idempotency key across its transport retry. A dashboard load captures the
exact mutation token and phase when it starts; it may clear that token only
when it began in `awaiting_authority`, returned a successful current
projection, and the same token and phase remain active. A successful manual
refresh begun during the unresolved POST therefore cannot release the lock,
even if it rerenders the dashboard before the POST commits.

Every dependency Confirm replacement rendered in either phase is a native
disabled button with `aria-disabled="true"` and `aria-busy="true"`.
`submitting` copy says the dependency review is being submitted and does not
claim acceptance. After POST success, `awaiting_authority` copy may name the
accepted review and projection reload. False, thrown, and 409 associated
reloads retain the exact lock and visible error; a later successful load begun
in `awaiting_authority` resolves it and renders a new enabled current action.

The existing source-safety test now excludes only the exact internal
`'awaiting_authority'` phase token from its removed-Authority-stage text scan;
all other source and HTML occurrences remain rejected.

### RED evidence

```text
node --test --test-name-pattern='dashboard load started during dependency submission' tests/test_workflow_position_display.mjs
1 failed, 0 passed
```

The VM assertion failed because the successful manual dashboard load had
cleared the active dependency token to `null`.

```text
uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py -k dependency_submission_survives_manual_refresh_race
1 failed, 21 deselected, 4 warnings
```

The browser fake held the dependency POST pending, completed a manual refresh,
and observed an enabled replacement with no disabled, ARIA-busy, or submitting
status. Both RED tests were committed before production JavaScript.

### GREEN verification

```text
node --check frontend/project.js
node --test tests/*.mjs
67 passed, 0 failed

uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py -k 'dependency_confirmation_stays_locked or dependency_confirmation_replacement_stays_locked or dependency_submission_survives_manual_refresh_race'
3 passed, 19 deselected, 4 warnings

uv run --frozen pyrepo-check annotations ty ruff tests/e2e/test_single_project_lifecycle_ui.py
ruff, annotations, and ty passed
```

The race scenario also attempts to click the replacement while the original
POST is unresolved, then resolves the POST. It proves the POST count remains
one, the observed idempotency key set remains one, submitting copy is coherent,
and the post-success authority reload renders a new enabled Confirm action.
The existing 409 lock and later-successful-refresh recovery scenarios remain
GREEN.

### Fix-round commits and boundaries

- `b07e31a` test: expose dependency refresh submission race (#223)
- `2892657` fix: bind dependency lock to projection phase (#223)

No provider call, real Sprint generation/persistence, manual profile, push,
merge, issue mutation, or live manual acceptance occurred. Browser work used
only the existing test-owned ephemeral server/profile/context and API fake.
#224 behavior is unchanged; full `pyrepo-check --all` remains Task 6's gate.
