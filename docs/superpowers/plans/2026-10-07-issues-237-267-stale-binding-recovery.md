# Issues #237 and #267: Stale Repository Binding Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task after Phase 2 approval. Use test-driven-development, requesting-code-review, and verification-before-completion. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Explain stale repository observations, preserve Specification registration selections through explicit recovery, and show accepted Specification context separately from current-binding source eligibility.

**Architecture:** Keep all provenance checks and workflow decisions intact. Attach one typed, sanitized recovery projection to existing stale failures and locked API actions; expose accepted context and source-binding eligibility through additive read fields. Reuse the dashboard's lifecycle helpers and workspace render memory, retaining form fields while stale registration is locked.

**Tech Stack:** Existing Python 3.13, SQLModel/Pydantic contracts, FastAPI transports, vanilla JavaScript dashboard, pytest, Node's test runner, and Linux-only Playwright acceptance tests. No new dependency or provider integration.

**Spec:** The Design section in this file and the 2026-09-18 triage comments on [#237](https://github.com/arduinitavares/agileforge/issues/237) and [#267](https://github.com/arduinitavares/agileforge/issues/267). This combined artifact is explicitly requested for Phase 1; no separate spec file or implementation is authorized now.

## Phase and provenance

- Phase 2 approved with changes after external Claude Opus 5.5 verdict **APPROVE WITH CHANGES**. The changes below supersede the Phase 1 stopping rule. Implement without commits; provider-free constrains product behavior/tests, not development review agents. Use fresh native Codex ultra task implementers/reviewers as explicitly selected by the user.
- Phase 1 output: this uncommitted plan only. Implementation, new tests, external model sessions, commits, branches, stash, GitHub writes, and product runtime launches are outside this phase.
- Actual clean detached checkout: `b3a4fb415de75e92fdfa5709986a6ba690eb6b51`. Its parent is `07654b8`, the #244 merge. `git diff --name-only 07654b8 HEAD` lists only `AGENTS.md` and three `docs/agents/` files; the investigated product code is identical to the requested baseline.
- Read both issue threads and triage comments before tracing code. Applied using-superpowers, systematic-debugging, bounded-codebase-investigation, safe-shell-commands, using-codex-subagents, non-interactive brainstorming, and writing-plans.
- One fresh native investigation worker was requested with `model=gpt-6.1-sol`, `reasoning_effort=ultra`, Standard context, and no descendants. The spawn receipt exposes task identity, not separate effective-model telemetry. No Codex usage windows are exposed; remaining allowance is unknown.
- Do not treat the older issue body's `SPECIFICATION_SOURCE_REQUIRED` observation after acceptance as the present workflow contract. This baseline already offers optional source replacement after acceptance.

## Global constraints

- This is one coordinated workstream for #237 and #267; no unrelated cleanup.
- No silent repository refresh, provenance bypass, repository replace/reset, changed source identity, accepted decision redo, or workflow transition changes.
- A Repository Binding observation is recorded evidence, not a continuously live Clean/Dirty assertion. A failed check describes what that check observed, not perpetual current state.
- Preserve #244 lifecycle labels and shared derivation helpers, including the Specification badge `Source not current`.
- Provider-free product behavior and verification: temporary/in-memory databases and temporary fixture repositories only. Never use home profiles/databases, real `agileforge-*` containers/volumes, or an actual model call.
- Use only `uv run --frozen ...` for Python tools. Never use the bare/user-level `agileforge` shim. Native `./agileforge-dev` rejection on macOS (exit 2) is expected, not a workaround target.
- Native pytest is supported through `tests/conftest.py`; real launcher/API process and Playwright acceptance runs require Linux. Do not run `tests/e2e` on this macOS host.
- Git remains read-only in this sandbox. Leave changes uncommitted. Fixture Git operations must be confined to disposable test directories, as existing tests already do.
- Keep relevant annotations explicit, and keep any existing repository-relative path banner first. Do not add retired words `authority` or `invariant` to `api.py`, `cli/main.py`, `cli/workflow_commands.py`, `services/application.py`, or `services/read_projections.py`, including comments/docstrings.
- Tests assert typed structures, actual DOM state, requests, persisted identity, and behavior. Do not test documentation prose/regex or incidental element/test counts.

## Review focus

1. Dirty-to-dirty status changes must be stale even when both dirty booleans match; classify from the status fingerprint too (Task 1).
2. Inspection failure must not invent live status or repeat sensitive probe diagnostics; preserve a locked action with bounded guidance (Tasks 1 and 3).
3. A refreshed binding invalidates an old package preview; keep inputs but require a new package check and current request guards (Task 4).
4. Repeated locked renders and collapsed revision controls must preserve fields and expose recovery without requiring a hidden-panel search (Tasks 4 and 5).
5. A refresh failure, a dashboard reconciliation failure after a successful mutation, and an unrelated/network registration error must not be presented as the same definite no-registration outcome (Tasks 4 and 5).

## Current behavior and root cause

### Registration and stale observation

- `services/specification_source_registration.py:298-313`: `_verify_provenance` raises `REPOSITORY_PROVENANCE_STALE` either when inspection fails or when the observation differs; the messages omit a sanitized cause/recovery object.
- `services/specification_source_registration.py:359-371`: `prepare` verifies provenance before `_capture_selected_documents`. The issue's guard is functioning correctly; source bytes are not captured on this failure.
- `services/specification_source_registration.py:558-587`: identity includes worktree path, common Git directory, HEAD, branch/detached state, dirty state, status fingerprint, and remotes. Preserve the exact comparison, including dirty-to-dirty drift.
- `services/application.py:5094-5104`: the typed service failure is reduced to workflow code/message. `api.py:664-695` similarly reduces a failed capability check to a locked action and reason code. `api.py:779-795` has a separate preview error envelope.
- `frontend/project.js:5181-5191`: `checkedResponsePayload` retains message/status/code on an Error but drops other structured recovery data. `frontend/project.js:7359-7364` sends the raw error to both the global error and the existing inline source status.

### Accepted history versus source eligibility

- `workflow/definitions/product_discovery.py:49-105`: current source selection requires the exact active binding ID and accepted Vision/Goal lineage; a source registered against an earlier binding stays immutable but is excluded.
- `workflow/definitions/product_discovery.py:480-521`: accepted Specification selection is independent of the current source/binding.
- `workflow/definitions/product_discovery.py:638-661,693-694`: when an accepted Specification exists, missing current source produces `SPECIFICATION_SOURCE_REPLACEMENT_AVAILABLE` / `optional_reentry`. Do not change this rule or unrelated delivery availability.
- `services/read_projections.py:2678-2737`: `specification_status` already validates and exposes `current` accepted registry/candidate content when the current candidate is absent. Reuse this path rather than finding a historical candidate by recency.
- `services/read_projections.py:2790-2819`: `specification_review` returns `source=null`, `candidate=null`, `review=null`, and `stale_reason=SPECIFICATION_SOURCE_NOT_REGISTERED`, without accepted context or an explicit binding distinction.
- `frontend/project.js:994-1038`: current source metadata and accepted Specification rendering already have separate helpers. Preserve their distinction. #244's `Source not current` badge remains the summary for this accepted/rebound state.

### Recovery and field retention

- `api.py:1350-1360`, `services/application.py:2874-2906`, `services/project_lifecycle.py:180-216`: explicit refresh re-inspects the active path and appends a new immutable binding observation through existing guards. It does not edit the target worktree.
- `frontend/project.js:1318-1342`: the repository card pairs bare `Clean`/`Dirty` with a separate inspection timestamp, allowing recorded state to be mistaken for a live check.
- `frontend/project.js:955-959`: locked registration removes the entire form. `frontend/project.js:3930-3999,5052-5053,5128-5129`: existing workspace memory preserves fields on ordinary renders, but capture replaces the remembered fields wholesale. A second render with no source form erases the remembered selection.
- `frontend/project.js:7571-7573`: the existing explicit Refresh path calls `runDirectAction('refresh_repository_binding', ..., 'repository/refresh')`; reuse it with targeted recovery feedback.

### Reproduction and baseline evidence

Provider-free throwaway root script: `$TMPDIR/agileforge237267-repro.aMlntz4oI5`; successful log: `$TMPDIR/agileforge237267-repro-output.I7hZwYwsCH`. It used an in-memory SQLite database, existing lifecycle fixture helpers, disposable Git fixtures, real capture/probe/refresh services, and a capture spy. No product process was started.

```text
rebind: review source=null, candidate=null, stale_reason=SPECIFICATION_SOURCE_NOT_REGISTERED;
        accepted_spec_id=1, accepted_spec_unchanged=true
clean_to_dirty: saved_dirty=false, observed_dirty=true, head_unchanged=true;
                ok=false, code=REPOSITORY_PROVENANCE_STALE, capture_called=false
locked_action: availability=locked, reason=REPOSITORY_PROVENANCE_STALE;
               recommendation=optional_reentry, decision=SPECIFICATION_SOURCE_REPLACEMENT_AVAILABLE
explicit_refresh_and_retry: refresh_ok=true, retry_ok=true;
                            accepted_spec_id=1, source_ids=[1,2], binding_ids=[1,3]
```

An initial retry probe failed because a historical fixed workflow clock predated the real probe's inspection timestamp. Correcting only the disposable fixture's probe timestamp made the actual refresh/retry succeed; this is not evidence for changing production timestamp validation.

The frontend worker's throwaway DOM probe, `$TMPDIR/issue237267-draft.I03eAzswvx`, confirmed ordinary render retention, form disappearance on lock, and field loss after a second locked render followed by unlock. Its DOM double needed realistic `id`/`name` getters to exercise existing memory; distinguish that fixture correction from product behavior.

Executed baseline commands:

```sh
uv run --frozen pytest tests/workflow/test_specification_rebinding.py tests/services/test_specification_source_application.py tests/services/test_specification_source_registration.py -q
node --test tests/test_project_lifecycle_summary.mjs tests/test_lifecycle_workspace.mjs tests/test_cockpit_action_synchronization.mjs
```

Results: Python **35 passed**, Node **86 passed**, both exit 0. These are existing-suite baselines and temporary reproductions, not proof of the planned recovery UI. Full gates and Linux e2e were not run in Phase 1.

## Design

### Alternatives

| Design | Benefits | Trade-offs / disposition |
| --- | --- | --- |
| Frontend copy keyed only by `REPOSITORY_PROVENANCE_STALE`, plus a link to existing Refresh | Smallest UI edit | Cannot distinguish changed worktree, changed repository identity, and failed inspection; API/CLI still lack the shared sanitized cause. Does not solve review projection ambiguity. Rejected as incomplete. |
| Shared typed recovery, additive accepted/source-binding read fields, explicit inline refresh, retained locked form | Covers both issues without changing decision rules; sanitization is owned where the probe result exists; reuses existing lifecycle/memory helpers | Adds small public JSON fields and requires behavioral UI tests and error propagation. **Chosen.** |
| New repository recovery endpoint or general live-status subsystem | Could centralize arbitrary future repository diagnostics | Extra probes, consistency/state ownership, and broader contracts without a current need. Deferred. Automatic refresh or relaxing source/binding identity is excluded by the non-goals. |

### Shared recovery contract

Create `RepositoryBindingRecovery` in `services/contracts/repository_recovery.py`, a frozen typed JSON contract:

```text
reason_code: Literal["REPOSITORY_PROVENANCE_STALE"]
cause: Literal["WORKTREE_CHANGED", "REPOSITORY_IDENTITY_CHANGED", "INSPECTION_UNAVAILABLE"]
recorded_binding_id: int
recorded_dirty: bool
observed_dirty: bool | None
changed_fields: tuple[Literal["worktree", "git_directory", "head", "branch", "detached_head", "working_tree_status", "remotes"], ...]
action: Literal["refresh_repository_binding"]
```

`changed_fields` contains categories only, in the fixed order above, never raw values. `working_tree_status` is present when dirty or status fingerprint differs. Identity differences take precedence over status-only drift. Inspection failure has `observed_dirty=null` and empty changed fields. No source bytes, filenames/status entries, absolute paths, Git directories, branch names, SHAs, remotes, exception strings, actor data, or fingerprints appear in this recovery object.

Attach optional `recovery: RepositoryBindingRecovery | None` to `SpecificationSourceRegistrationError`. Build it from the already performed provenance check, without another probe, write, or weakening `_probe_matches_context`. Retain existing codes and existing fallback diagnostics for unrelated errors; consumers do not infer recovery by matching message text.

Expose the identical serialized object at:

- `position.actions[*].repository_recovery` for stale locked registration actions.
- Preview failure HTTP `detail.repository_recovery`.
- Failed registration `TransitionResult.output.repository_recovery`, therefore HTTP `detail.output.repository_recovery` and existing CLI result JSON.

Use the existing failure output container instead of adding service types to `workflow/contracts.py`. Only stale failures with a service recovery object carry this field; no changes to successful mutation output. API status codes remain 409 for failed registration and 422 for stale preview. Locked actions remain locked. The recovery action is an allowlisted token, not an arbitrary URL or executable command. A failed submission consumes only `detail.output.repository_recovery`; projected action recovery is used only for a currently locked action, never as evidence about a failed submission. The final `verify_prepared` failure path can return the same stale code without recovery output; it must receive generic treatment and must not claim nothing was registered merely from its code.

Current-code qualification: `workflow/domain.py:1184-1220` converts a failed final verification to `STALE_SPECIFICATION_INPUT` without recovery output, even when the service failure is `REPOSITORY_PROVENANCE_STALE`. Preserve this mapping. Cover the actual final-write path and frontend stale-code-without-metadata variants; neither licenses metadata-backed no-registration copy.

### Accepted context and source-binding eligibility

Add `current` to `specification_review` by sharing the existing validated accepted-registry projection used by `specification_status`. It follows `specification_status.current` semantics; do not broaden pending-amendment behavior in this workstream. Top-level `source`, `candidate`, and `review` continue to describe the eligible source/selected candidate, never substituted historical rows.

Add `source_binding` to both Specification status and review projections:

```text
state: "current" | "not_registered" | "different_binding" | "not_ready" | "conflict"
active_repository_binding_id: int | None
accepted_source: { specification_source_id: int, source_fingerprint: str, repository_binding_id: int } | None
```

Select accepted source by the exact validated accepted candidate's source ID/fingerprint. `different_binding` applies only when a verified accepted source exists, no current eligible source exists, and its binding differs from the active binding. `current` requires the existing exact current-source selector. Missing accepted lineage/binding yields `not_ready`; absent evidence yields `not_registered`; conflicts yield `conflict` or the existing typed read failure, never a guessed source. Missing/inconsistent accepted-source identity is a typed failure.

Preserve `schema_version=agileforge.specification_review.v2`, existing `stale_reason` values, existing source/candidate/review meanings, workflow codes, and business schema. New fields are additive metadata, not new lifecycle state. Read projections remain durable reads; live stale-observation recovery comes from the existing capability check attached to API actions, not a second live probe inside read projection code.

### UI behavior and terminology

- Use `Recorded working tree` / `Clean` or `Dirty` with the saved inspection timestamp in the repository card. Recovery can say `At the failed check: Dirty`; it must not relabel recorded Clean as current Clean.
- Preserve `Clean`/`Dirty` as standalone text nodes in the card. The inline button uses `data-repository-recovery-action="refresh"`, exclusively; it does not use the repository card's `data-repository-action="refresh"` attribute.
- For a definite non-2xx stale registration rejection: **“Specification source registration failed. No source was registered by this attempt.”** Then the bounded cause: **“The working tree changed since the saved repository inspection.”** Include recorded/check statuses when supplied.
- Locked action before submission: **“Specification source registration is locked because the repository observation is stale.”** Do not claim a submission failed when none occurred.
- Failed preview: **“The source package could not be checked because the repository observation is stale. Nothing was registered.”** Preview never registers anything.
- Identity change and inspection failure get their own bounded cause, without claiming file edits when the probe could not establish them. For `REPOSITORY_IDENTITY_CHANGED` with only HEAD/branch/detached-state changes, say **“The repository revision changed since the saved inspection.”** Reserve repository-location/details copy for worktree/Git-directory/remote differences; do not describe an ordinary new commit as an identity change.
- Recovery guidance: **“Refresh the repository binding, check the selected package again, then retry registration. Your entered fields are retained.”** An explicit **“Refresh repository binding”** button uses the existing refresh endpoint. Refresh does not automatically check or register the package.
- When `source_binding.state === "current"`, explicitly warn **“Refreshing records a new repository observation. The current registered source will stop being current until a source is registered for the new binding.”** Refresh always appends a binding ID, even for the same path; exact source-binding eligibility therefore changes. Add real service and Node cases for current source plus stale-locked replacement action, while accepted rows remain unchanged.
- Rebinding context: **“The accepted Specification remains available. Its source was registered under an earlier repository binding and is not the current source for this binding.”** Render this copy and `data-accepted-specification-context` source/binding metadata **only** when `source_binding.state === "different_binding"`; never infer it from acceptance plus no source. Test accepted `not_registered`, `not_ready`, and `conflict` projections have no such copy/marker. Preserve optional revision registration and the #244 `Source not current` badge derivation unchanged.
- Render accepted content once using `acceptedSpecificationMarkup`; the rebinding marker is conditional as above. Do not generate review/acceptance controls for historical context.
- Keep the source fields mounted when this stale action is locked. Disable preview/register and gate their handlers against current action availability. Reuse `captureWorkspaceRenderState` / `restoreWorkspaceRenderState`; retain raw user field values, not preview keys, source fingerprints, decision fingerprints, or idempotency keys. Extend existing disclosure memory only for the revision-registration details element.
- Show the stale recovery notice outside a collapsed optional revision disclosure so it is visible. Preserve whether the operator opened that disclosure across refresh. Keep draft retention project/view scoped; do not copy a draft to another Project.
- Clear or explicitly flag retained source draft values when the active binding's worktree path changes within a Project. Binding-ID-only refresh at the same path preserves values; a different worktree must not inherit an unqualified selection or preview.
- `checkedResponsePayload` retains a validated/allowlisted recovery object on Error, using the two error-envelope locations above. Unstructured/network/unrelated errors retain their existing treatment; do not assert definite no-write outcome for them. A successful registration followed by dashboard read failure continues to use existing unreconciled-state handling.
- Inline refresh and the repository-card Refresh share execution and busy handling. For 409 conflict, 5xx/unreadable response, and network rejection, keep the draft/recovery visible and use fixed frontend copy **“Repository binding refresh failed. Your entered fields are retained. Try refreshing again.”** Never echo server text in the inline refresh-failure message. On successful refresh, reload/reconcile, retain fields, clear stale preview, and instruct the operator to check again. A second stale failure after a successful refresh shows the appropriate stale recovery again without claiming refresh failed. If the returned binding does not match the recovery, use current action/projection data rather than reusing old recovery permission.
- Known endpoint limitation: `ProjectLifecycleService.refresh_repository` does not catch `RepositoryProbeError`; it may produce an unstructured 500. Do not change this endpoint in this workstream; cover the failure variants in the frontend.

### Contract change inventory

**Will change:** service error metadata; failed registration output/CLI JSON with `repository_recovery`; API locked-action and preview error metadata; additive review `current`; additive status/review `source_binding`; repository observation labels; stale-form rendering, recovery button/status, disclosure retention, and preview invalidation guidance.

**Will not change:** database/business schema or migration support; `WorkflowError`/graph contracts; API routes, request bodies, existing status codes or source-selection guards; successful result shape; source hashes/bytes/IDs or binding association; accepted registry/candidate/decision rows; workflow recommendation/transition rules; Task/Sprint execution; #244 summary semantics; repository contents; provider invocation behavior.

## Implementation tasks — Phase 2 only

### Task 1: Classify stale checks into a sanitized recovery contract

**Files:** Create `services/contracts/repository_recovery.py`; modify `services/specification_source_registration.py`; test `tests/services/test_specification_source_registration.py`.

**Interfaces:** Produce `RepositoryBindingRecovery` above, `SpecificationSourceRegistrationError(..., *, recovery: RepositoryBindingRecovery | None = None)`, and private `_repository_binding_recovery(context: _DurableSourceContext, observed: RepositoryProbeResult | None) -> RepositoryBindingRecovery`. The existing service methods/signatures remain unchanged.

- [ ] Write failing tests `test_stale_worktree_recovery_prevents_capture`, `test_dirty_to_dirty_recovery_reports_status_drift`, `test_identity_drift_recovery_remains_locked`, and `test_uninspectable_repository_recovery_is_sanitized` using real temporary clean/dirty fixtures where supported and a controlled raising probe for inspection failure.
- [ ] Assert `error.code` stays stale; recovery equals the declared typed categories/booleans; capture spy is never called; saved source/binding rows equal pre-attempt snapshots. Use sentinel path/remote/filename data and assert no recovery field can carry it. Cover branch/HEAD/path/remotes drift independently without weakening the comparator.
- [ ] Run red: `uv run --frozen pytest tests/services/test_specification_source_registration.py -q`. Expected failures are missing recovery metadata, not a fixture/import/platform failure.
- [ ] Implement the contract and metadata construction in `_verify_provenance`; do not change `_probe_identity`, the comparison, or before/middle/after/final-write checks.
- [ ] Run green with the same command plus `uv run --frozen pytest tests/services/test_specification_source_application.py -q`.
- [ ] Perform the required task review with specification-compliance and code-quality verdicts; record findings, rulings, and actual checks in the Phase 2 ledger. No commit in this sandbox.

### Task 2: Expose accepted history and exact source eligibility separately

**Files:** Modify `services/read_projections.py`; test `tests/services/test_durable_product_definition_projections.py` and `tests/workflow/test_specification_rebinding.py`.

**Interfaces:** Produce shared private method `_accepted_specification_current(self, *, snapshot: WorkflowFactSnapshot, spec: SpecVersionFact | None) -> JsonObject | _SpecificationReadFailure | None` for the existing no-candidate accepted path, and `_specification_source_binding_data(snapshot: WorkflowFactSnapshot, selection: ProductDefinitionSelection) -> JsonObject` for the declared binding metadata. Any identity mismatch must use the existing typed read failure pattern rather than returning a plausible historical source.

- [ ] Write failing tests `test_rebound_review_preserves_exact_accepted_context` and `test_source_binding_projection_distinguishes_history_from_current_source`. Seed accepted Specification/source, append a different binding, and compare exact pre/post IDs, fingerprints, canonical source bytes, accepted decision, and registry content.
- [ ] Add cases with no acceptance, same-binding current source, missing prerequisites, conflict/missing accepted-source evidence, and a registered replacement source. Assert historical source never becomes top-level current `source`, never enables structuring, and acceptance/optional replacement decisions stay unchanged.
- [ ] Run red: `uv run --frozen pytest tests/services/test_durable_product_definition_projections.py tests/workflow/test_specification_rebinding.py -q`.
- [ ] Extract only the existing validated accepted projection for reuse; add the fields on all successful projection branches and update structural dictionary expectations. Preserve existing `stale_reason` and pending candidate semantics.
- [ ] Run green with the same command and `uv run --frozen pytest tests/workflow/test_product_discovery_graph.py tests/workflow/test_product_discovery_transitions.py tests/workflow/test_specification_source_transitions.py -q`.
- [ ] Task review: two verdicts and exact lineage evidence in the ledger. No workflow-definition/handler or business-schema edits are expected; any apparent need is a scope/design finding.

### Task 3: Carry recovery through API actions, failures, and existing CLI JSON

**Files:** Modify `services/application.py` and `api.py`; test `tests/services/test_specification_source_application.py`, `tests/adapters/test_api_workflow_domain.py`, `tests/adapters/test_api_dashboard_bundle.py`, and `tests/adapters/test_cli_workflow_domain.py`. No CLI production edit is expected: `cli/main.py:2530-2532` already serializes the result.

**Interfaces:** Consume Task 1 `error.recovery`; attach only `recovery.model_dump(mode="json")` at the three declared locations. Existing error/status/code fields remain. Dashboard reads carry Task 2 projection fields without additional repository probes.

- [ ] Write failing tests `test_stale_registration_result_contains_recovery_without_a_write`, `test_locked_source_action_contains_same_sanitized_recovery`, `test_stale_source_preview_contains_recovery_without_registration`, and `test_cli_stale_registration_serializes_recovery`.
- [ ] Assert failed registration is HTTP 409, stale preview is 422, capability action is still locked, and all three recovery values match the same fixture observation. Assert no source capture/transition/provider call on failure, and unsupported capture capability/unrelated errors have their existing behavior.
- [ ] Assert status/review/dashboard projections expose exact accepted/source-binding identities after rebinding. CLI exits 1 and prints the new failure output without adding a command, refresh, or source write. Use injected test application and suite platform bypass; never launch the real CLI on macOS.
- [ ] Add service coverage for a current registered source with a stale-locked replacement action: explicit refresh appends a new binding ID and makes that source non-current while accepted rows/bytes remain unchanged. After explicit recheck/retry, assert the resulting next route/action enables `structure_specification`, not merely `ok=true`. Cover final `verify_prepared` stale failure without recovery output as a generic failure contract.
- [ ] Run red: `uv run --frozen pytest tests/services/test_specification_source_application.py tests/adapters/test_api_workflow_domain.py tests/adapters/test_api_dashboard_bundle.py tests/adapters/test_cli_workflow_domain.py -q`.
- [ ] Add the narrow propagation at application registration catch, action capability catch, and `_source_preview_error`. Keep successful mutation/result semantics and routes unchanged.
- [ ] Run green with the same command and `uv run --frozen pytest tests/adapters/test_production_read_surfaces.py -q`.
- [ ] Task review: two verdicts, sanitization/HTTP/CLI evidence, and no extra probe/write in the ledger.

### Task 4: Render explicit recovery and preserve entered fields while locked

**Files:** Modify `frontend/project.js`; test `tests/test_project_lifecycle_summary.mjs`, `tests/test_cockpit_action_synchronization.mjs`, and fixture `tests/dashboard_bundle_fixture.mjs`. `frontend/project.html` and `frontend/lifecycle-workspace.js` should need no production change; their existing structure/helpers are constraints, verified by existing tests.

**Interfaces:** Add `repositoryBindingRecoveryDisplay(value) -> validated display | null`, `repositoryBindingRecoveryMarkup(recovery, context) -> HTML`, and `sourceRegistrationRecoveryFromError(error) -> recovery | null`. Pass context `locked`, `preview`, or `registration` explicitly. Extend `checkedResponsePayload` to preserve only validated recovery. Reuse existing source markup, accepted markup, lifecycle summary derivation, workspace memory, `runDirectAction`, and busy/reconciliation helpers. A small explicit source-recovery refresh wrapper may provide inline completion/error hooks to the existing direct-action path; it must not introduce a second mutation implementation.

- [ ] Add realistic accepted-rebound and stale-source-registration fixture builders using actual node ID `specification.source.register`, a repository projection, current decision fingerprint, and Task 3 envelopes. Do not reuse the existing source-only fixture as proof of registration behavior: it lacks a realistic registration action and uses a different node ID.
- [ ] Write failing structural DOM tests `stale registration shows inline recovery and no-write outcome`, `locked registration retains draft across repeated renders`, `refresh retains selection and invalidates preview`, `refresh failure retains actionable inline recovery`, `accepted rebound context is visible without opening revision controls`, and `unrelated or post-success read failure does not claim nothing registered`.
- [ ] Extend the existing summary DOM harness only as needed for actual field `id`/`name`/`value`/`disabled`/disclosure `open` state and delegated events. Assert mounted nodes, roles, disabled controls, field values, disclosure state, and intercepted requests; do not assert frontend source strings or regex against rendered prose.
- [ ] Exercise source path, multiline ADR input, capability input, repeated lock/unlock renders, navigating away/back, and another Project. Assert selection is retained only in its own Project. Capture request sequence: failed check or stale registration -> one user-triggered refresh -> new explicit preview -> manual register with fresh headers. No automatic retry/check/refresh.
- [ ] Cover current-source refresh consequences, accepted `not_registered`/`not_ready`/`conflict` without rebinding copy, worktree-path-change draft clearing/flagging, a second stale error after successful refresh, HEAD/branch revision copy, and stale final-verification error without recovery metadata. Exercise refresh 409, unreadable/5xx, and network rejection: fixed inline frontend failure text, retained draft and recovery, no server-text echo.
- [ ] Run red: `node --test tests/test_project_lifecycle_summary.mjs tests/test_cockpit_action_synchronization.mjs`.
- [ ] Implement the declared UI copy/metadata, retain stale locked form fields, gate both preview and submit handlers, preserve revision disclosure state in existing memory, and invalidate preview authorization after binding change. Permit refresh while registration is locked without unlocking registration itself.
- [ ] Keep #244's `Source not current` and all accepted/pending/current-source summary cases. Accepted history must render once and have no semantic decision controls. Put recovery outside collapsed details. Repository card continues showing the recorded status until explicit refresh succeeds.
- [ ] Run green: `node --test tests/test_project_lifecycle_summary.mjs tests/test_cockpit_action_synchronization.mjs tests/test_lifecycle_workspace.mjs tests/test_dashboard_review_safety.mjs tests/test_dashboard_bundle.mjs`; also `uv run --frozen pytest tests/test_lifecycle_summary_markup.py -q`.
- [ ] Task review: two verdicts, event/request evidence and retention assertions in the ledger. No new Node suite or dependency is needed, so no CI runner list change is expected.

### Task 5: Add Linux browser acceptance and audit affected locators

**Files:** Modify `tests/e2e/test_single_project_lifecycle_ui.py`. Add only narrowly scoped fixture behavior needed to expose stale preview/registration, refresh failure/success, a new binding observation, and immutable accepted source lineage.

**Interfaces:** A dedicated `_FakeLifecycle` subclass/stateful test fixture must produce the Task 2/3 structures and guard outcomes. The current fake's unconditional preview/register success and refresh counter are insufficient acceptance evidence.

- [ ] Audit every locator/expectation affected by recorded-state copy, mounted disabled fields, accepted history markers, and recovery outside revision details. Specifically inspect existing source registration and revision tests around lines 3250-3307 and 3448-3610. Preserve their collapsed-panel expectations unless a documented recovery-state difference requires a scoped change.
- [ ] Write failing browser tests `test_stale_source_registration_recovers_with_retained_fields`, `test_source_binding_refresh_failure_keeps_registration_selection`, and `test_rebound_accepted_specification_exposes_locked_source_recovery`.
- [ ] In the first flow, allow initial package check, then change the fixture observation before submission so registration fails before capture; assert local no-write message and recovery button; explicitly refresh; assert all inputs remain, old preview is unusable, new check is required, and manual retry succeeds. In the second, fail refresh and assert inputs and recovery remain. In the third, show accepted content/source identity plus `Source not current` and visible stale guidance with disabled capture controls.
- [ ] Scope buttons and copy by unique containers: source form, recovery marker, accepted-context marker, repository card, or summary. The existing global `[data-repository-action="refresh"]` helper at line 3370 must be scoped to `#repository-panel`; the inline button exclusively uses `[data-repository-recovery-action="refresh"]`. In `test_mobile_dirty_repository_wraps_without_overflow`, scope line 3806 `get_by_text("Dirty", exact=True)` and line 3816 `[data-repository-action="refresh"]` to `#repository-panel`; keep the card's Dirty/Clean text nodes standalone. Split `_submit_specification_source` (lines 3244-3258) for the stale-between-preview-and-submit test, and use `_select_workspace_stage(page, 4)` before Specification assertions. Avoid global `get_by_text("Accepted Specification")`, global Refresh-name matching, strict-mode duplicate text, and visibility assertions on fields inside a closed details element. Do not use `.first()` to conceal duplicates.
- [ ] On Linux CI/authorized Linux runner only, run red then green: `uv run --frozen pytest tests/e2e/test_single_project_lifecycle_ui.py -q`. Record the actual Linux result. Do not create/use Docker resources or execute this command on the current macOS host.
- [ ] Run `uv run --frozen pytest tests/adapters/test_api_dashboard_bundle.py tests/services/test_durable_product_definition_projections.py tests/workflow/test_specification_rebinding.py -q` to keep fake browser structures grounded in host contracts.
- [ ] Task review: two verdicts and browser verification limits in the ledger. Mac-only implementation cannot claim browser acceptance before Linux evidence exists.

## Phase 2 execution and review contract

- Stop after Phase 1. The user supplies external Claude Opus 5.5 plan feedback and approval before execution. Do not launch that external review, providers, or an implementation worker now.
- Preserve the user-selected subagent-driven-development path. Each meaningful task has a fresh scoped implementer, red/green evidence, and task review with separate **specification-compliance** and **code-quality** verdicts, then fresh whole-diff independent review.
- User explicitly requires every Codex child in this workstream to use `reasoning_effort="ultra"`; explicitly select a permitted model and fresh context at every spawn. Native implementation uses `gpt-6.1-sol`; final independent review uses fresh `gpt-6-astra` under that explicit effort instruction. No descendants/automatic fan-out; reserve slots centrally and preserve the four-workstream ceiling and one-active-Astra limit. If controls cannot be maintained, report the precise limitation rather than silently selecting another effort.
- Review routing is resolved by the Phase 2 instruction: use fresh native Codex ultra task reviewers, each returning specification-compliance and code-quality verdicts. The external Opus plan review is already supplied. Product-provider calls remain forbidden; development-time review agents are permitted. This explicit user choice supersedes the default Opus task-review route.
- At Phase 2 setup, use the installed SDD `scripts/sdd-workspace` for this plan, keep its `progress.md` identity and task/interface conflict scan, and record task completions, submitted packets, two verdicts, findings/rulings, actual checks, and fix rounds. Read the current implementation/task-review/re-review/final-review templates; do not invent another workflow or shared ledger.
- Preserve SDD's maximum five task fix rounds (rounds 1-3 original implementer, rounds 4-5 fresh more capable implementer under allowed settings) and final review's one fix dispatch plus one scoped re-review. Adjudicate remaining findings according to the rubric; do not hide a material acceptance gap behind a green verdict. Review continuations must use a verified allowed route or fresh scoped context.
- No git commit steps are executable in this sandbox. Retain uncommitted patches/check evidence in the ledger; do not stage, stash, branch, push, open a PR, or delete this requested plan. The plan remains the reviewable artifact. Do not apply generic SDD cleanup/finishing steps that would discard it.

## Required acceptance gates

All gates below are mandatory for Phase 2. They are not satisfied by the narrow Phase 1 baselines. Redirect verbose output to task-owned temporary logs, preserve exit status, and show a bounded summary.

```sh
uv run --frozen ty check
uv run --frozen ruff check .
uv run --frozen ruff check --select ANN .
uv run --frozen ruff format --check services/contracts/repository_recovery.py services/specification_source_registration.py services/application.py services/read_projections.py api.py tests/services/test_specification_source_registration.py tests/services/test_specification_source_application.py tests/services/test_durable_product_definition_projections.py tests/workflow/test_specification_rebinding.py tests/adapters/test_api_workflow_domain.py tests/adapters/test_api_dashboard_bundle.py tests/adapters/test_cli_workflow_domain.py tests/e2e/test_single_project_lifecycle_ui.py
git diff --check

uv run --frozen pytest tests/services/test_specification_source_registration.py tests/services/test_specification_source_application.py tests/services/test_durable_product_definition_projections.py tests/workflow/test_specification_rebinding.py tests/workflow/test_specification_source_transitions.py tests/workflow/test_specification_acceptance_revalidation.py tests/workflow/test_repository_attachment.py tests/adapters/test_api_workflow_domain.py tests/adapters/test_api_dashboard_bundle.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_production_read_surfaces.py tests/test_lifecycle_summary_markup.py -q

uv run --frozen pytest tests/services tests/workflow tests/adapters tests/test_ci_contract.py tests/test_lifecycle_summary_markup.py -q

node --test tests/test_workflow_position_display.mjs tests/test_lifecycle_workspace.mjs tests/test_sprint_retry_dashboard.mjs tests/test_dashboard_review_safety.mjs tests/test_dashboard_bundle.mjs tests/test_cockpit_action_synchronization.mjs tests/test_create_project_modal_required_fields.mjs tests/test_vision_interview_ui.mjs tests/test_product_goal_interview_ui.mjs tests/test_project_lifecycle_summary.mjs

node --test $(sed -n '91,100p' .github/workflows/ci.yml | grep -oE 'tests/[^ ]+\.mjs')
uv run --frozen pytest tests/e2e --collect-only -q
```

- `ty check` is **repo-wide** and must exit 0 with **“All checks passed!”**. PR #291's failure is a reason to require this exact gate. Fix typing normally; `# type: ignore` does not work here. Only as a justified last resort use `# ty: ignore[rule]`.
- Follow the existing `pytest.fail` message-line convention when needed:

```python
pytest.fail(
    "unexpected provider call"  # ty: ignore[invalid-argument-type]
)
```

- The Ruff format command covers all planned changed Python files. Adjust it to the actual changed Python file list if review narrows/extends within scope; do not pass JavaScript/Markdown files to Ruff.
- Explicitly retain `tests/adapters/test_production_read_surfaces.py` in the gate: its retired-word ban includes docstrings/comments in the five listed production surfaces.
- Native test execution must isolate runtime fences from home. The changed CLI workflow suite uses a scoped temporary-root fixture. A throwaway pytest plugin outside the repository redirects only the observed native runtime modules to disposable test directories: `test_cli_sprint_retry.py`, `test_cli_sprint_triage.py`, `test_cli_task_completion.py`, `test_vision_bootstrap_cli.py`, `test_story_sprint_selection.py`, `test_story_validation_application.py`, `test_api_sprint_retry.py`, `test_command_renderer.py`, and `test_vision_bootstrap_api.py`. The initial affected-suite run exposed nine additional failures in the last five modules, all from the home lock path. Real fence behavior remains active; do not change production fencing or the home environment. Record the plugin, environment, and gate output with the verification evidence.
- The Node command is the current `.github/workflows/ci.yml:89-100` frontend suite set. It includes #244's summary suite even though the checkout-local `cli/dev_checks.py` frontend subset is smaller; do not assume the smaller runner covers the change.
- On Linux CI only: `uv run --frozen pytest tests/e2e/test_single_project_lifecycle_ui.py -q`, followed by the existing full canonical CI gate. No current-host Docker/container execution is authorized by this plan-only phase.
- Completion requires review of exact saved/observed statuses, no capture/no new source on stale rejection, explicit refresh failure/success, retained inputs, fresh preview plus successful retry, immutable accepted/source identity, and unchanged unrelated execution availability. Final independent reviewer receives requirements, complete diff, verification evidence, and Linux limitations in fresh context, not the implementation conversation.

## Open questions and risks for plan review

1. **Public additive shape:** Confirm `repository_recovery` in failed `output`, action metadata, and preview detail; and `current`/`source_binding` on the existing v2 read contract. This plan deliberately preserves schema labels and `stale_reason`, but strict consumers/tests must be audited for additional keys. No business-schema change is proposed.
2. **Accepted-context boundary:** This plan mirrors existing status semantics in review `current`, including existing pending-amendment behavior. Broadening accepted-history presentation beyond the rebinding case is a separate scope choice; do not make that change implicitly.
3. **Review transport resolved:** Provider-free constrains product behavior/tests only. The user explicitly selected Codex ultra task reviews; native Astra supplies fresh final independence. No review-routing approval blocker remains.
4. **Freshness/races:** Recovery reflects a specific failed check. Rebinding or another refresh between that check and a click must never make metadata grant capture permission; use current API action/decision guards and require a new preview. Technical changes to the refresh endpoint's concurrency contract are outside scope.
5. **Linux acceptance:** Browser and full runtime evidence cannot be obtained natively here. Collect all e2e tests locally as required; report Linux execution as unverified until CI supplies the result. Do not confuse collection with browser execution. The mandatory current-host gate set can be completed without starting Linux product processes.

## Phase 1 self-review

- Both triage scopes map to Tasks 1-5; shared terminology/recovery is defined once.
- All provenance/source identity checks, accepted decisions, and workflow transitions are preserved.
- Retention uses the existing memory path and is based on a reproduced failure, not an assumed need for another draft store.
- Test tasks name exact files, failing behaviors, red/green commands, and structural/persistence assertions; the Linux locator audit is included in the same change.
- Full ty/Ruff/ANN/format/whitespace, production read-surface, targeted/affected pytest, CI Node, and Linux browser gates are explicit.
- Phase 2 routes/ledger/review budgets are preserved; implementation has not started.
