# ASA Milestone 1 AgileForge Feedback

This log records factual AgileForge product feedback observed while completing
ASA Milestone 1 through the CLI/backend rituals.

Scope: feedback only. Do not treat this file as an AgileForge product-fix task
inside the ASA execution goal.

## 2026-06-11 triage and product-fix status

Use ASA as a regression fixture only. The product fixes below were implemented
as generic AgileForge workflow/bridge behavior, not ASA-specific special cases.

### Fixed

#### Project create / authority compile split

- Source: issue #128 and ASA setup/scope-extension feedback.
- Decision: accepted and implemented as a hard split.
- Fix status: fixed in `dev/project-create-authority-compile-split`.
- What changed:
  - `agileforge project create` now persists project/spec metadata and returns
    `authority_compile_required`.
  - Authority compilation is now the explicit guarded mutation
    `agileforge authority compile`.
  - `workflow next` routes setup projects through compile-required, compiling,
    compile-failed, and pending-review states.
  - Dashboard/API creation no longer hides a long-running authority compile
    operation.
- Why fixed: project creation should be observable and fast. Compiler failure
  recovery belongs to the compiler command, not project metadata creation.
  Future scope-extension and brownfield setup workflows need the same
  spec-registration-to-compile boundary.
- Expected behavior after fix: project creation completes without compiling
  authority, returns a guarded compile action, and the dashboard/CLI/API route
  the user through compile before authority review.
- Out of scope:
  - #129 brownfield setup.
  - #130 authority compiler source-map/model repair.
  - Product Goal / scope-extension workflow.

#### Authority compiler source-map repair

- Source: issue #130 and ASA authority compile feedback.
- Decision: accepted and implemented for focused source-map repair.
- Fix status: implemented in `dev/authority-compiler-focused-repair`.
- What changed:
  - `agileforge authority compile` and `agileforge authority regenerate` accept
    a per-run `--compiler-model` override.
  - Source-metadata failures now expose structured subcodes and bounded
    diagnostics for the failing source item, invariant, source level, repair
    attempt, repair item ids, repair result, and retry commands.
  - The compiler retries only structured behavioral source-evidence failures
    where every source metadata issue is marked repairable.
- Remains fail-closed:
  - Invented or unsupported authority evidence is not accepted.
  - Legacy modality promotion and example-only evidence are not auto-repaired.
  - Mixed repairable and non-repairable source metadata failures still block
    authority compilation.

#### Post-sprint story reconciliation can strand a saveable draft

- Original feedback item: `Post-sprint story reconciliation leaves saveable
  draft in unsaveable FSM state`.
- Product issue considered: real bridge executability bug. `story history`
  could report `save.available=true` and `expected_state=STORY_REVIEW`, while
  the persisted FSM stayed in `STORY_INTERVIEW`.
- Fix status: fixed in `d7ad6bf fix(workflow): recover post-sprint story
  reconciliation`.
- Why fixed: this violated the workflow contract that commands advertised as
  runnable by `workflow next` / history must be executable from the same
  workflow snapshot.
- Expected behavior after fix: stale `STORY_INTERVIEW` with a saveable draft
  routes to guarded `story save`, or already-covered Story state routes to the
  appropriate completion/recovery command.

#### Story draft saveability ignored dependency persistence blockers

- Original feedback item: `Story draft marked saveable despite unresolved
  dependency candidates`.
- Product issue considered: real gate-contract bug. Story generation/quality
  allowed `save.available=true`, while `story save` later rejected explicit
  unresolved dependency candidates.
- Fix status: fixed in `987ea0e fix(story): align dependency saveability
  gates`.
- Why fixed: saveability must mean "passes deterministic quality checks and
  known persistence preconditions." Persistence remains the final authority, but
  generation/read projections should not promise a save that dependency
  resolution can already predict will fail.
- Expected behavior after fix: explicit unresolved, ambiguous, self-edge, or
  resolution-failed dependency candidates become blocking quality findings;
  `save.available=false`; FSM remains `STORY_INTERVIEW`. Inferred unresolved
  dependency candidates remain warnings and do not block saveability.

#### Workflow next advertised sprint generation with zero candidates

- Original feedback item: `Workflow next routes to sprint generation when no
  sprint candidates remain`.
- Product issue considered: real routing bug. `workflow next` could advertise
  `sprint generate` after post-sprint `impact=none` even when `sprint
  candidates` had `count=0`.
- Fix status: partially fixed earlier in `d85e32b fix(workflow): route
  post-sprint no-candidate story continuation`, then tightened in `987ea0e
  fix(story): align dependency saveability gates`.
- Why fixed: sprint generation is not runnable when no refined candidates
  exist. `workflow next` must either route to known uncovered Story work or
  return a blocked sprint-generation action with a concrete reason.
- Expected behavior after fix:
  - If candidate count is zero and uncovered requirements exist, route to
    targeted `story generate`.
  - If candidate count is zero but requirements are already marked covered,
    keep `story pending` and `sprint candidates` visible, and list `sprint
    generate` only as blocked with `NO_REFINED_SPRINT_CANDIDATES`.

#### Dashboard allowed consumed Story requirements to be selected again

- Source: follow-up UI observation during ASA workflow, not one of the original
  2026-06-10 CLI field-note sections.
- Product issue considered: real UI affordance bug. Requirements already
  consumed by the active Story completion scope could still expose selection
  controls for planning another sprint.
- Fix status: fixed in `204bb75 fix(ui): block consumed story sprint
  selection`.
- Why fixed: UI actions must match backend scoping rules. Already consumed
  requirements should remain visible as historical/saved work, but should not
  present sprint-selection controls.
- Expected behavior after fix: consumed requirements are not selectable for the
  next sprint selection, and the selection button is hidden/disabled when no
  eligible requirements remain.

#### Sprint history hid completed execution records

- Original feedback item: `Active sprint state hides sprint generation attempt
  history`, plus follow-up velocity inspection where `agileforge sprint history
  --project-id 3` returned `count=0` despite durable completed Sprint rows.
- Product issue considered: real read-projection bug. The command exposed only
  transient Sprint planner attempts, so completed Sprint execution evidence
  required direct DB inspection.
- Fix status: fixed in `dev/sprint-history-execution-projection`.
- Why fixed: agents need completed Sprint ids, story/task counts, story points,
  timestamps, and elapsed time from the normal CLI/API path to compute velocity
  and summarize past execution without bypassing AgileForge.
- Expected behavior after fix: `sprint history` preserves `items`/`count` as
  planner-attempt history and adds `attempt_items`, `attempt_count`,
  `execution_items`, and `execution_count`. Each execution row includes Sprint
  status, story/task completion counts, story points, timestamps, elapsed
  seconds, and history fidelity.

#### Sprint task update command omits required close-evidence arguments

- Original feedback item: `Sprint task update command omits required
  close-evidence arguments`.
- Product issue considered: real command-contract bug. `workflow next` and
  `sprint task next/show` could advertise `sprint task update` commands for
  `Done` transitions without the close evidence that mutation validation
  requires.
- Fix status: fixed in `dev/task-close-evidence-contract` for issue #137.
- Why fixed: advertised runnable commands must include the evidence fields
  required to complete the same mutation from the same workflow snapshot.
- Expected behavior after fix: Done update command projections include
  `--outcome-summary`, `--validation-summary`, `--checklist-result fully_met`,
  and conditional `--artifact-ref` placeholders; mutation validation returns
  structured `TASK_CLOSE_EVIDENCE_REQUIRED` details when required close evidence
  is missing.

#### Dashboard can preserve a Story panel after Sprint execution becomes active

- Original feedback item: `Sprint-active dashboard still shows unrelated Story
  draft context`, tracked as issue #138.
- Product issue considered: real UI routing bug. The saved Sprint workspace
  already knew how to render active Sprint execution, but a state refresh with
  `preserveView=true` could accept backend `SPRINT_VIEW` while keeping
  `viewPhaseId='story'`. That made the sidebar show Sprint execution while the
  main panel stayed on Story planning artifacts.
- Fix status: fixed in `dev/dashboard-active-sprint-default` for issue #138.
- Why fixed: automatic state refresh should not strand operators on non-Sprint
  planning panels once the backend has entered active Sprint execution. Active
  Sprint states need to resolve the Sprint landing workspace by default.
- Expected behavior after fix: when refreshed state is `SPRINT_VIEW`,
  `SPRINT_LIST`, `SPRINT_UPDATE_STORY`, or `SPRINT_MODIFY`, a preserved
  non-Sprint panel is overridden and the dashboard lands on the current Sprint
  workspace. Manual navigation to Stories remains possible afterward.

#### Project scope extension after exhausted execution scope

- Source: ASA project reached `SPRINT_COMPLETE` with no refined Sprint
  candidates after its original roadmap scope was effectively exhausted.
- Product issue considered: real framework gap. Mature projects can need new
  product scope after all accepted backlog/roadmap/story work is complete, and
  the correct path should not be project recreation, manual spec bypass, or
  forced backlog refinement.
- Fix status: accepted/fixed by `dev/project-scope-extension`.
- Why fixed: continuing a mature AgileForge project should preserve Sprint,
  Story, roadmap, and velocity history while routing new scope through the same
  evidence chain as initial scope: spec, authority, backlog, roadmap, stories,
  and sprints.
- Expected behavior after fix: when execution scope is exhausted,
  `workflow next` can expose a project-agnostic scope-extension ritual. The
  amended scope must pass additive spec validation, authority
  compile/review/accept, delta backlog generation, appended roadmap phase, and
  normal Story/Sprint rituals before producing new executable work.
- Not included in v1: removal/deprecation of old accepted scope and smart
  roadmap reordering. The v1 contract is add-only and appends new roadmap
  phases conservatively.

### Considered but not fixed yet

#### Sprint generation validation failure details

- Original feedback item: `Sprint generation validation failure was recoverable
  but hard to diagnose`.
- Current status: accepted as useful product feedback, not fixed in the
  2026-06-11 bridge-contract work.
- Why not fixed yet: this is an error-reporting and validation-detail surfacing
  improvement, separate from command executability. It needs a small design pass
  for the CLI/API error envelope so validation findings are actionable without
  bloating normal responses.
- Suggested next action: add a validation-specific `issues` or
  `validation_details` projection for Sprint generation failures, with tests
  proving the CLI exposes offending story/task fields.

#### Active sprint state hides sprint generation attempt history

- Original feedback item: `Active sprint state hides sprint generation attempt
  history`.
- Current status: partially addressed. Completed execution records are now
  exposed by `sprint history`; raw planner-attempt provenance remains an open
  product decision.
- Why not fully fixed yet: planner attempts are still treated as working-set
  state and may be reset after a Sprint is saved/completed. That may be
  intentional, but if users need exact model-attempt provenance after Sprint
  start/close, that requires a durable audit/provenance design rather than a
  read-projection fix.
- Suggested next action: decide whether raw Sprint planner attempts should be
  durable audit records or only draft working state. If durable, add a separate
  provenance projection rather than overloading execution history.

#### Task update response can look like the task stayed open

- Original feedback item: `Task update response can look like the task stayed
  open`.
- Current status: accepted as product feedback, not fixed yet.
- Why not fixed yet: it is a response-shape ambiguity, not a workflow blocker.
  The mutation did succeed; the confusing part is that the envelope emphasizes
  the next recommended task without clearly separating it from the updated task.
- Suggested next action: change the response payload to clearly separate
  `updated_task` from `next_recommended_task`, and add CLI/API tests for both
  fields.

#### Sprint count/history summaries are inconsistent

- Original feedback item: `Sprint count/history summaries are inconsistent
  across commands`.
- Current status: accepted as product feedback, not fixed yet.
- Why not fixed yet: this overlaps with the active-sprint history/provenance
  question and needs terminology cleanup. `story_count`, parent workstream
  counts, and runnable leaf task counts should not share ambiguous labels.
- Suggested next action: standardize response field names around
  `stories_count`, `workstream_count`, and `tasks_count`, then update CLI/API
  tests and dashboard labels.

#### Guard values are correct but nested deeply

- Original feedback item: `Guard values are correct but nested deeply for
  repeated task/story updates`.
- Current status: accepted as lower-priority ergonomics, not fixed yet.
- Why not fixed yet: guard correctness is already intact. The remaining issue
  is operator convenience for bulk CLI usage.
- Suggested next action: consider a compact/porcelain mode for guard-bearing
  read commands after higher-priority bridge executability and response-shape
  issues are resolved.

### Maintenance notes for future agents

- Do not reopen fixed items only because ASA previously hit them. Reproduce on
  current `master` first.
- Treat every future entry as either:
  - `Fixed`, with commit id and expected behavior;
  - `Accepted / not fixed`, with reason and suggested next action;
  - `Rejected / not a product bug`, with evidence; or
  - `Needs reproduction`, with the exact missing command/output.
- Keep ASA-specific project IDs, sprint IDs, and requirement names as evidence,
  not as implementation assumptions.

## 2026-06-10

### Sprint generation validation failure was recoverable but hard to diagnose

- Project: ASA `project_id=3`
- Context: post-Sprint-14 continuation from `post_sprint_story_continuation_available`
- Command: `agileforge sprint generate --project-id 3`
- Observed result: first broad sprint generation returned `ok=false`, error code
  `MUTATION_FAILED`, message `Sprint output validation failed: poor task decomposition quality`.
- Follow-up inspection: `agileforge sprint history --project-id 3` showed
  `sprint-attempt-1`, `failure_stage=output_validation`,
  `failure_summary=Sprint output validation failed: poor task decomposition quality`.
- Additional clue in the recorded input context: previous system feedback said
  `capacity analysis does not match locked Sprint selection`.
- Recovery used: generated a narrower sprint with explicit selected story IDs
  and capacity guidance. That produced a valid `SPRINT_DRAFT`.
- Product feedback: the failure was recoverable, but the CLI summary did not
  surface enough actionable validation detail by itself. Users need a clear
  way to inspect the exact validation findings and offending fields without
  reading raw history payloads.

### Active sprint state hides sprint generation attempt history

- Project: ASA `project_id=3`
- Context: after saving and starting Sprint 15.
- Command: `agileforge sprint history --project-id 3`
- Observed result: `ok=true`, `history_count=0`.
- Expected user need: while a sprint is active, it is still useful to inspect
  the generation attempt that produced that active sprint, especially after a
  prior failed generation attempt.
- Product feedback: consider exposing current active sprint generation history,
  or clarifying in CLI help/output that `sprint history` only reports draft
  generation attempts in specific workflow states.

### Guard values are correct but nested deeply for repeated task/story updates

- Project: ASA `project_id=3`
- Context: closing Sprint 14 tasks and stories.
- Commands:
  - `agileforge sprint task show --project-id 3 --task-id <task_id>`
  - `agileforge sprint story readiness --project-id 3 --story-id <story_id>`
- Observed result: required guard values are present and accurate, but task
  update guards live under `data.task_ticket.guards`, and story close guards
  live under `data.guards`.
- Product feedback: the guard model works well, but bulk CLI execution would be
  easier if `task show`, `story readiness`, or companion commands exposed a
  compact machine-readable summary mode with `id`, `status`, `fingerprint`, and
  next update command only.

### Workflow next routes to sprint generation when no sprint candidates remain

- Project: ASA `project_id=3`
- Context: after Sprint 15 was closed and post-sprint triage was recorded as
  `impact=none`.
- Command: `agileforge workflow next --project-id 3`
- Observed result: `status=post_sprint_story_continuation_available` with valid
  commands:
  - `agileforge story pending --project-id 3`
  - `agileforge sprint candidates --project-id 3`
  - `agileforge sprint generate --project-id 3`
- Command: `agileforge story pending --project-id 3`
- Observed result: Milestone 1 still has pending non-refined requirements,
  including `pyrepo-check Quality Gate Integration`, `Raw Data Ingestion
  Pipeline`, and `Canonical Process Event Record Definition and Validation`.
- Command: `agileforge sprint candidates --project-id 3`
- Observed result: `count=0`, message `Found 0 sprint candidates for
  selected-story scope. Excluded: 11 non-refined requirements.`
- Product feedback: this is a workflow-routing gap. When there are no sprint
  candidates and pending non-refined requirements remain, `workflow next` should
  route to Story generation/refinement or explicit reconciliation, not only to
  `sprint generate`.

### Story draft marked saveable despite unresolved dependency candidates

- Project: ASA `project_id=3`
- Context: generated stories for `pyrepo-check Quality Gate Integration`.
- Command: `agileforge story generate --project-id 3 --parent-requirement "pyrepo-check Quality Gate Integration"`
- Observed result: generated `attempt-1`, `is_complete=true`,
  `is_reusable=true`, `quality.coverage_status=complete`, and
  `save.available=true`.
- Follow-up command: `agileforge story save ... --attempt-id attempt-1 ...`
- Observed result: `ok=false`, error code `INVALID_COMMAND`, message
  `Dependency candidate did not resolve to an active story.`
- Cause in generated artifact: `dependency_candidates` referenced external or
  unresolved labels such as `Python Project Scaffold and uv Management
  Setup#...` and unqualified current-draft story names.
- Recovery used: refined the same requirement with explicit feedback to omit
  unresolved dependency candidates. `attempt-2` saved successfully.
- Product feedback: the Story quality/saveability gate should catch unresolved
  dependency candidates before reporting `save.available=true`, or the save
  error should be surfaced as part of the draft quality findings.

### Task update response can look like the task stayed open

- Project: ASA `project_id=3`
- Context: updating Sprint 16 task tickets `208-220` to `Done`.
- Command: `agileforge sprint task update ... --status Done ...`
- Observed result: each update returned `ok=true`, but the summarized response
  exposed a `status=To Do` and the fingerprint for the next task ticket.
- Follow-up verification: `agileforge sprint tasks --project-id 3` showed all
  13 tasks were actually `Done`.
- Product feedback: the task update response should clearly identify the
  updated task status and separately label any next-task ticket. Otherwise a
  CLI user can reasonably think the mutation was accepted but did not change
  task state.

### Sprint count/history summaries are inconsistent across commands

- Project: ASA `project_id=3`
- Context: Sprint 16 pyrepo-check planning and execution.
- Command: `agileforge sprint start --project-id 3`
- Observed result: response reported `story_count=4` and `task_count=3`.
- Command: `agileforge sprint tasks --project-id 3`
- Observed result: listed 13 actionable task rows for the same sprint.
- Command: `agileforge sprint history --project-id 3` after generating
  `sprint-attempt-1`.
- Observed result: history summary showed `attempt_count=0` even though the
  active draft/save path had just used `sprint-attempt-1`.
- Product feedback: clarify whether `task_count` means story/workstream groups
  or actionable tasks, and expose the active sprint generation attempt in
  history or a dedicated active-sprint planning summary.

### Post-sprint story reconciliation leaves saveable draft in unsaveable FSM state

- Project: ASA `project_id=3`
- Context: after closing Sprint 17 and recording post-sprint triage with
  `impact=story` for `Canonical Process Event Record Definition and
  Validation`.
- Command: `agileforge workflow next --project-id 3`
- Observed result: `status=post_sprint_story_impact_needs_reconciliation`,
  routing to:
  - `agileforge story pending --project-id 3`
  - `agileforge story generate --project-id 3 --parent-requirement "Canonical
    Process Event Record Definition and Validation"`
- Command: `agileforge story generate --project-id 3 --parent-requirement
  "Canonical Process Event Record Definition and Validation" --input "..."`
- Observed result: `ok=true`, `attempt_id=attempt-2`,
  `artifact_fingerprint=sha256:b8ef5a1dab8c392613a62dd1d0df2edc24472081c8557c30f793171d56c2c622`,
  `is_reusable=true`, `quality.coverage_status=complete`, and zero blocking
  findings.
- Command: `agileforge story history --project-id 3 --parent-requirement
  "Canonical Process Event Record Definition and Validation"`
- Observed result: `save.available=true`, `save.expected_state=STORY_REVIEW`,
  current draft complete, and zero quality blockers.
- Command: `agileforge status --project-id 3`
- Observed result: workflow FSM remained `STORY_INTERVIEW`.
- Follow-up command: `agileforge story save ... --expected-state STORY_REVIEW`
- Observed result: `ok=false`, error code `INVALID_COMMAND`, message
  `story save requires FSM state STORY_REVIEW`.
- Follow-up command: `agileforge story complete ... --expected-state
  STORY_PERSISTENCE`
- Observed result: `ok=false`, error code `INVALID_COMMAND`, message
  `Story phase cannot complete unless current state is STORY_PERSISTENCE.`
- Product feedback: post-sprint story-impact reconciliation can produce or
  reuse a complete saveable Story draft without transitioning the session FSM
  to the state required by the save command. The workflow becomes stuck between
  `STORY_INTERVIEW` and `STORY_REVIEW`; `workflow next` routes to generation,
  but generation does not expose a runnable save/complete path.

### Sprint metrics can recommend capacity below the next dependency-closed cohort

- Project: ASA `project_id=3`
- Context: after Sprint 39 was closed and post-sprint triage was recorded as
  `impact=none`.
- Command: `agileforge workflow next --project-id 3`
- Observed result: `status=post_sprint_story_continuation_available`, routing
  to sprint continuation commands including `agileforge sprint generate`.
- Command: `agileforge sprint metrics --project-id 3`
- Observed result: `recommended_next_sprint_points=3`.
- Command: `agileforge sprint candidates --project-id 3`
- Observed result: the highest-priority eligible candidate was Story `182`,
  `Build pipeline runner that executes all stages in order`, with `5` story
  points. Other lower-priority candidates with `3` points were also listed.
- Command:
  `agileforge sprint generate --project-id 3 --max-story-points 3 --input "..."`
- Actual result: `ok=false`, error code `MUTATION_FAILED`, message
  `The highest-priority dependency-closed story cohort exceeds the explicit Sprint capacity. Increase --max-story-points or split the story.`
- Expected behavior: the planning surfaces should reconcile this before the
  user runs a doomed generate command. Either `sprint metrics` should recommend
  the minimum viable capacity for the next dependency-closed cohort, or
  `workflow next` / `sprint candidates` should explicitly say that the next
  cohort exceeds recommended capacity and suggest the exact valid choices:
  increase capacity to `5`, split Story `182`, or choose a lower-priority story
  with an explicit override.
- Why it matters: a CLI user following the recommended capacity and valid
  `workflow next` route hits a mutation failure even though there are eligible
  candidates. This makes the AgileForge ritual feel internally inconsistent.
- Severity: blocker for strict capacity-following; non-blocking only if the
  user intentionally overrides capacity to `5`.

### Dashboard does not expose CLI story IDs or current Sprint draft mapping

- Project: ASA `project_id=3`
- Context: after all roadmap requirements were saved and Sprint planning
  continued into Milestone 3 / Reproducible Pipeline Orchestration.
- Observed UI: the dashboard shows the three roadmap milestones and saved
  requirements. It shows story cards inside the selected requirement, but it
  does not expose the internal AgileForge story IDs used by the CLI, such as
  Story `180`, `181`, or `182`.
- Observed CLI: `agileforge sprint history --project-id 3` showed the latest
  Sprint draft attempt `sprint-attempt-2`, artifact fingerprint
  `sha256:44a75ca35257dd194fdd52ecbc22a15edf944bb5d6d1beb8450c317ecc340ba6`,
  selecting Story `182`, `Build pipeline runner that executes all stages in
  order`, with five planned tasks.
- Expected behavior: the UI should make the CLI-to-dashboard mapping explicit.
  At minimum, each story card should show its internal `story_id`, and the
  Sprint panel should show the active Sprint draft attempt, selected story IDs,
  points, and task titles before the user saves the Sprint.
- Actual behavior: a user following CLI execution sees references to Story
  `180`, `181`, `182`, and Sprint draft attempts, but the dashboard only shows
  milestone and requirement labels. This makes it look like the CLI is working
  on hidden or unrelated work.
- Why it matters: this is a traceability and confidence issue during real
  AgileForge ritual execution. The backend state may be correct, but the UI
  makes it hard to audit what the CLI is doing against the visible roadmap.
- Severity: non-blocking UX/traceability issue.

### Dashboard post-sprint no-candidate state needs clearer routing

- Project: ASA `project_id=3`
- Context: after Sprint `45` closed and all thirteen roadmap requirements had
  saved Story coverage.
- Initial observed UI: the main Story view remained visible with all
  requirements saved while the user was validating Sprint state.
- Correction after refreshed screenshot evidence: the sidebar displayed
  `Sprint COMPLETED`, not `Sprint ACTIVE`. The earlier "active" wording should
  not be treated as a proven active-badge bug.
- Command: `agileforge workflow next --project-id 3`
- Actual result: `status=post_sprint_sprint_candidates_unavailable`; valid
  commands were only `agileforge story pending --project-id 3` and
  `agileforge sprint candidates --project-id 3`; `agileforge sprint generate`
  was blocked with `NO_REFINED_SPRINT_CANDIDATES`.
- Command: `agileforge sprint task next --project-id 3`
- Actual result: `ok=false`, error code `INVALID_COMMAND`, message
  `No active or planned Sprint found.`
- Command: `agileforge sprint candidates --project-id 3`
- Actual result: `count=0`, message
  `Found 0 refined sprint candidate(s) in backlog (excluded non-refined=0, superseded=9, open_sprint=0).`
- Expected behavior: when the backend has no active/planned Sprint and no
  refined candidates, the dashboard should show the same no-candidate /
  post-sprint state and explain the next valid inspection or recovery action.
- Why it matters: the UI can still leave users looking at Story planning
  artifacts when the CLI truth is a blocked post-sprint candidate state. This
  makes live CLI validation look inconsistent even when the backend is behaving
  correctly.
- Severity: non-blocking UX/state-projection issue unless it causes users to
  plan or execute against stale Sprint assumptions.
