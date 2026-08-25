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
