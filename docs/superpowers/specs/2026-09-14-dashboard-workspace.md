# AgileForge dashboard contracts

14 September 2026. This is the source-to-screen contract for milestone C, the first connected workspace slice. It was derived from the checked repository at `C:\Users\atavares\Projects\agileforge`, `master`, `733d90f94bc4e1a580e55c360e215551ab6a946c`, without opening a live database or running the application. The dirty `AGENTS.md` is outside this work.

The contract distinguishes the first slice from later work. The first slice uses existing HTTP reads and guarded mutation routes, and says **Manual refresh required**. It does not add a database table, polling, SSE, WebSocket, an activity-ingestion endpoint, or an execution status invented by the browser.

## Authorities and vocabulary

| Screen fact | Authority | First-slice rule |
| --- | --- | --- |
| **You are here** | Current Sprint status, Task and Story completion facts, exact execution-attempt triage, and `GET /api/projects/{project_id}/position` | Confirmed Sprint work determines the delivery phase. Pending reviews and required Specification authoring may coexist with it. Other available actions have separate badges. Without a current Sprint, eligible required/recovery decisions identify current work. Several stages may be marked **Here**. |
| **Formal record/status** | Sprint, Task, completion, Story-completion, review, closure and triage fields in the selected Sprint reads | The dashboard renders the persisted value exactly. In particular, Task work and passing checks do not change `task.status`; only the completion transition does. |
| **Availability** | Exact `position.actions` entry paired to the exact `position.decisions` row by `node_id`, `request_kind`, and `instance_key` | `Ready`, `locked`, waiting, blocked and optional re-entry are availability/routing facts. They are separate from a Task's formal status. The action caption must preserve `request_kind` and the returned reason/blocker in expanded detail. |
| **Activity/evidence** | Existing retained Task execution logs, Task completion evidence, `sprint_history.execution_attempts`, plan-artifact attempts, and the dated supplied report fixture | First slice labels the source of each entry. It shows no generic live provider, test, review, Git or terminal feed because no complete source is currently exposed. Missing source is **Unavailable**, never idle/success/failure. |
| **Viewed selection** | Browser-only state: selected stage, Sprint, Task, inspector tab, board filters and scroll position | It changes the inspector only. It does not call a workflow mutation and does not overwrite the graph-derived current marker. |
| **Freshness** | Completion time of the last successful full read set in the browser | First slice shows `Last confirmed <time>; manual refresh required`. A failed refresh retains the last confirmed data and changes only freshness/error presentation. |

The position endpoint is the sole routing projection (`api.py:871`). Its payload is `{ status: "success", data: WorkflowPosition, actions: Action[] }`; ordinary durable read routes use `{ status: "success", data: ..., warnings: [] }` (`api.py:_read_payload`). A decision has graph fields including `node_id`, `instance_key`, `request_kind`, `category`, `recommendation_kind`, `reason_code`, `blockers`, and fact references. A transportable action is deliberately smaller:

```json
{
  "node_id": "execution.task.complete",
  "instance_key": "task:71",
  "request_kind": "complete_task",
  "endpoint": "sprint/task/complete",
  "transport": "semantic",
  "availability": "locked",
  "reason_code": "..."
}
```

`availability` and `reason_code` are optional additions made only for the current capability checks. Absence of `availability` is not a promise that a hand-authored request is executable; the paired action and decision are required. The frontend must not manufacture actions from an endpoint string, the lifecycle diagram, or a stale prior response.

## Persistent 13-stage map and inspector reuse

The lifecycle document is the naming authority for the 13 stages (`docs/agileforge-lifecycle.md:18-30`). `frontend/project.js` currently collapses them into `STAGES = [Vision, Product Goal, Specification, Backlog, Roadmap, Stories, Sprint, Execution, Review]`; `REQUEST_STAGE` additionally maps `close_story` to **Stories**, and `review_sprint`/`record_post_sprint_triage` to **Review**. That nine-item mapping is an existing implementation detail, not a valid 13-stage progress authority.

The first slice keeps all thirteen cards visible and uses one inspector. It may reuse the existing rendering functions and panel containers below, but must make selection identifiers 01–13 rather than overload `selectedStageTab` with the old nine labels. `project.html` currently owns `#vision-panel`, `#goal-panel`, `#specification-panel`, `#repository-panel`, and `#delivery-panel`; its `.stage-nav-btn` navigation and `updateStageView()` currently give one selected tab the only blue/current treatment. The slice must split this into an independently rendered **You are here** marker and a **Viewing stage NN** selection treatment.

| Map card | Inspector subject and reusable current frontend surface | Existing source reads/decision kinds | First-slice display contract |
| --- | --- | --- | --- |
| 01 Create Project | Project identity; reuse project header / `renderDashboard()` rather than a new workflow panel. Repository inspection remains a utility view (`#repository-panel`, `repositoryPanelMarkup()`). | `GET /api/projects/{id}` and `GET /position`; no assumed create action for an existing project. | Name, description and repository binding. It must not call the delete route or present repository attachment as progress. |
| 02 Vision | `#vision-panel`, `visionPanelMarkup()` | `/vision/status`; position actions such as `generate_vision_bootstrap`, `record_vision_interview_turn`, `decide_vision_review`, `begin_vision_revision`. | Candidate/accepted/review content plus all matching advertised actions. Locked capability is a lock reason, not a failed Vision. |
| 03 Product Goal | `#goal-panel`, `productGoalPanelMarkup()` | `/goals/status`; `record_product_goal_interview_turn`, `decide_product_goal_review`, `fulfill_product_goal`, `abandon_product_goal`. | Goal candidate/active/outcome and returned review action. Optional outcome is not a lifecycle-completion percentage. |
| 04 Specification | `#specification-panel`, `specificationPanelMarkup()` | `/specifications/review`; `register_specification_source`, `structure_specification`, `decide_specification`. | Current source, candidate, review and source/provenance lock state. Preserve the existing source-preview and expected-fingerprint protections. |
| 05 Backlog | scoped content within `#delivery-panel`: `planningReviewCardMarkup()`, `backlogReviewMarkup()`, `deliveryGenerationActionMarkup()` | `/backlog/review`; `record_backlog_draft`, `decide_backlog`. | Candidate/review/continuation only when returned. A correction remains a graph action with its actual reason; it is not an extra stage. |
| 06 Roadmap | scoped `#delivery-panel`: `roadmapReviewMarkup()`, `planningReviewCardMarkup()` | `/roadmap/review`; `record_roadmap_draft`, `decide_roadmap`. | Candidate/review and returned action, including a waiting human decision. |
| 07 Stories | scoped `#delivery-panel`: `storyReadinessMarkup()`, `storyDependencyReviewMarkup()`, `storyReviewMarkup()`, `sprintCandidatePoolMarkup()` | `/story/reviews`, `/story/pending`, `/story/dependencies`, `/sprint/candidates`; `record_story_draft`, `decide_story`, `apply_story_dependencies`, `repair_story_readiness`. | Story content, selection, structural evidence and dependency condition. Stories with separate current decisions remain separate entries; a selected Story does not move **You are here**. |
| 08 Sprint plan and start | scoped `#delivery-panel`: `sprintReviewMarkup()`, `sprintStatusMarkup()`, existing start and plan controls | `/sprint/plan/review`, `/sprint/candidates`, `/sprint/status`; `record_sprint_plan`, `decide_sprint_plan`, `start_sprint`, `start_sprint_retry`. | Proposed plan, its exact review, accepted-plan lineage and distinct start state. `activated_sprint_id` alone does not mean active; `sprint.status`/`effective_status` and `start` govern that label. |
| 09 Develop and verify | new board region inside the focused inspector; it can reuse `sprintExecutionProjection()` for exact actionable Task pairing and `sprintStatusMarkup()` for summary. | Existing status plus `GET /sprints/{sprint_id}/tasks`, Task detail/history/packet routes below; `complete_task` actions only. | Complete inventory and selected Task. Formal status comes from every Task row; action availability is overlaid for only exact advertised Task actions. No inferred `In Progress`. |
| 10 Close Stories | separate inspector subview, not the former **Stories** bucket. Reuse `sprintStatusMarkup()` task/Story facts and direct-action binding machinery. | Selected Sprint `stories`, `story_completions`; position action `close_story`. | A Story can be ready to close while another retains work. Show each Story completion as a separate durable fact; never treat 3/3 Tasks Done as Story closure. |
| 11 Review and close Sprint | separate inspector subview, not the former **Review** bucket. Reuse `sprintStatusMarkup()` and its exact direct-action binding support. | Selected Sprint `review`, `closure`; position actions `review_sprint`, then `close_sprint`. | Review and closure are distinct facts/actions. A returned `close_sprint` action can coexist with other decisions and must not make review invisible. |
| 12 Learn and triage | separate inspector subview; first slice reuses sprint history/selected Sprint data and position action binding. | Selected Sprint's `original_triage` in status; `/sprints` `execution_attempts[]` has a scope-specific `triage[]`; position action `record_post_sprint_triage`. The richer `sprint_review()` service projection has no HTTP route. | Find the execution-attempt row by the selected `sprint_id` and exact `retry_attempt_id` (null for original), then render its `triage[]` with that row's ordinal/status. Do not substitute original triage for a retry. |
| 13 Assess next Sprint | separate inspector subview, assembled from the existing planning and retained Sprint history surfaces, without a terminal-complete claim. | `/sprints`, `/sprint/candidates`, `/sprint/plan/review`, `/position`; optional `retry_sprint`, planning correction/re-entry actions only when advertised. | Previous Sprint evidence and conditions for new planning. Triage neither starts a Sprint nor applies a Backlog/Specification correction. Formal Sprint retry is explicit optional re-entry; ordinary command, test or provider retry is activity, not a retry attempt. |

The Project lifecycle says a stage can contain multiple commands and review decisions. The map separates confirmed work from action availability:

1. Associate **every** `position.data.decisions` row with a 13-stage card and retain its graph category, recommendation kind, reason/blockers and exact matching action if one exists. For execution, map `complete_task` to 09, `close_story` to 10, `review_sprint` and `close_sprint` to 11, and `record_post_sprint_triage` to 12. Planning actions belong to 08; no position action makes stage 13 current by itself. Complete/future/blocked decisions retain their own card status but are not **Here** merely because they exist.
2. Use the current Sprint's `effective_status`, falling back to its recorded status, to derive delivery work. A planned Sprint is at 08. An active Sprint with unfinished Tasks is at 09; a Story whose Tasks are Done but lacks a completion record is at 10. Both can coexist. Once all Tasks and Stories are complete, the active Sprint is at 11. After completion, a pending next-Sprint plan review is at 08. Otherwise the completed Sprint is at 12 until its exact execution attempt has triage, then at 13. Read this from the current Sprint context, independently of a historical Sprint selected for inspection. Missing, failed or mismatched Sprint reads provide no delivery marker.
3. With confirmed Sprint work, also mark supported pending reviews and available required Specification capture or authoring. Show other available actions on their cards as **Action available** or **Optional action** without changing the delivery marker. Without a current Sprint, use available decisions recommended as `required` or `recovery`, plus supported waiting reviews. Supported review kinds are `decide_backlog`, `decide_product_goal_review`, `decide_roadmap`, `decide_sprint_plan`, `decide_specification`, `decide_story`, `decide_vision_review`, and `review_sprint`. Optional re-entry does not establish current work.
4. Render **Viewing** from local selection. On first entry, open the confirmed Sprint phase, preferring unfinished Task work when Story closure is also ready. Without a current Sprint, open a stage only when the graph identifies one unambiguous stage. Otherwise show **Select a stage**. Never choose the first, lowest or highest available action. Refresh and rebinding preserve an existing stage, Sprint, Task and inspector-tab selection.

### Specification acceptance after repository rebinding

An accepted Specification remains accepted when the Project moves to another checkout, worktree, branch or repository. Acceptance remains tied to its accepted Vision and Product Goal. Rebinding does not rewrite accepted artifacts, source rows or their captured provenance.

Source eligibility is separate. A source captured under the previous binding is not eligible for new authoring under the current binding. When a current accepted Specification exists, preparing a source under the new binding is optional. Changing raw source files alone does not revoke acceptance. Explicit source capture enables a revision and retains the existing repository identity, provenance, freshness and review checks. A changed accepted Vision or Product Goal still requires a source for that new product definition.

## Existing selected-Sprint and Task payloads

All durable Task views must bind to the chosen `sprint_id`, then retain the returned `current_retry` and `effective_status` in every request and display. The source emits original and retry scopes differently; the UI must never mix them.

### Board list

`GET /api/projects/{project_id}/sprints/{sprint_id}/tasks` delegates to `DurableReadProjectionService.sprint_tasks()` and returns:

```json
{
  "status": "success",
  "data": {
    "project_id": 7,
    "sprint_id": 31,
    "items": ["Task row ..."],
    "count": 1,
    "current_retry": null,
    "effective_status": "active"
  },
  "warnings": []
}
```

Each original-scope `items[]` row begins with the exact `TaskFact` fields:

```json
{
  "task_id": 71,
  "sprint_id": 31,
  "story_id": 101,
  "description": "Preserve the original completion history.",
  "metadata_json": "...",
  "status": "To Do",
  "dependencies_satisfied": true,
  "fact_fingerprint": "sha256:...",
  "instance_key": "task:71"
}
```

The status projection verifies the same row shape and adds `fact_fingerprint` before scope binding (`services/read_projections.py:_sprint_status_from_snapshot`). Existing dashboard fixture `tests/test_sprint_retry_dashboard.mjs` proves `task_id`, `sprint_id`, `story_id`, `description`, `status`, `fact_fingerprint`, and `instance_key`; the typed `TaskFact` is the source for `metadata_json` and `dependencies_satisfied` (`workflow/facts.py:497-506`). In a current retry, `instance_key` is `retry:{retry_attempt_id}:task:{task_id}` and the effective status is retry-local. `current_retry` has exactly:

```json
{
  "retry_attempt_id": 101,
  "ordinal": 2,
  "status": "active",
  "predecessor_retry_attempt_id": null,
  "sprint_instance_key": "retry:101:sprint:31"
}
```

`count` is inventory count. Board aggregates are computed only from `items`: total is `count`, Done is rows whose formal `status === "Done"`, and remaining is `count - Done`. Any other persisted status must retain its own count/label; do not collapse it into Queued, Blocked or In progress. `dependencies_satisfied === false` may support **dependency condition not satisfied**. Absence of a `complete_task` action is insufficient evidence for a dependency block, so a row without an exact advertised action is labelled **No completion action currently advertised**, not blocked. A row with `dependencies_satisfied === true` but no action is **Queued / not current** only as a presentation availability label, never a formal status.

### Selected Task detail and history

The focused inspector uses these existing routes, requested only after a Task is selected:

| Read | Existing response `data` contract | Use |
| --- | --- | --- |
| `GET /sprints/{sprint_id}/tasks/{task_id}` | `{ project_id, task, completion, current_retry, effective_status, original_task, original_completion }` | **Details**: exact current task ticket and its immutable completion fact. `completion` is null until formal completion. |
| `GET /sprints/{sprint_id}/tasks/{task_id}/execution` | `{ project_id, task, completion, items, count, current_retry, effective_status, original_task, original_completion }` | **Activity**: retained `TaskExecutionLog` rows only. Each `items[]` record is `{ log_id, task_id, sprint_id, old_status, new_status, outcome_summary, artifact_refs_json, acceptance_result, notes, changed_by, changed_at }`, ordered `changed_at` descending. |
| `GET /sprints/{sprint_id}/tasks/{task_id}/packet[?flavor=...]` | Without `flavor`, the exact canonical `task_packet.v4`; with `flavor`, `{ packet, render }` | **Checks/evidence context**: bounded accepted-lineage packet, not live telemetry or a status feed. |

The Task-completion object is durable completion evidence, not an execution-log entry: `{ completion_id, task_id, sprint_id, outcome_summary, artifact_refs, acceptance_result, checklist_result, evidence_fingerprint }`. `acceptance_result` is only `partially_met` or `fully_met`. This supports a clear detail layout: formal Task status; requirements/dependencies; completion evidence; retained status history; then packet/check material. The UI must not claim that arbitrary packet content is a latest test result, provider run or human approval.

Selected Task actions are selected from `position.actions` only when all of these agree: `request_kind === "complete_task"`, the paired decision is `category === "available"`, reason is `NEXT_TASK_READY` or `IN_PROGRESS_TASK_REQUIRED`, decision and Task `instance_key` are identical, and decision has an exact `task` fact reference matching the Task row's `fact_fingerprint`. This is the existing `sprintExecutionProjection()` contract in `project.js`. It permits several next Task actions; it does not choose one by task number.

### Selected Sprint and retained history

`GET /api/projects/{project_id}/sprints/{sprint_id}` and `/sprint/status` return selected-Sprint data with these fields: `project_id`, `sprint`, `accepted_plan`, `current_retry`, `effective_status`, `start`, `original_start`, `tasks`, `stories`, `story_completions`, `review`, `closure`, `original_review`, `original_closure`, and `original_triage`. `sprint` has `sprint_id`, `status` (`planned`, `active`, `completed`), `completed_at`. `accepted_plan` contains exact plan IDs/fingerprints, `acceptance { rationale, reviewer, decided_at }`, `selected_stories[] { story_id, story_item_id, title, story_points, task_count }`, `total_points`, and `task_count`.

`GET /sprints` returns `{ project_id, attempts, sprints, execution_attempts }`. `execution_attempts[]` preserves original and retry scope: `sprint_id`, `retry_attempt_id`, `ordinal`, `status`, `predecessor_retry_attempt_id`, `sprint_instance_key`, `task_instance_keys`, `start`, `task_completions`, `story_completions`, `review`, `closure`, and `triage`. It is the only existing browser read suitable for a retained lifecycle summary. It is not a complete external work feed.

## Actions, guards, and stale-state handling

The first slice must reuse the existing action handler pattern rather than add a general “advance stage” button.

1. Every mutating control uses the exact advertised action object and `postAction()`/the existing specialized handler. It sends a fresh `idempotency_key` and `actor`, and, where the existing handler requires it, expected candidate/decision/instance/source headers. The API itself filters to semantically routable decisions; it can deliberately omit ambiguous selectors.
2. Controls bind to the action's `node_id`, `instance_key`, `request_kind`, endpoint and returned fingerprints. A selected old Task cannot submit a completion request after a fresh position/action pairing no longer validates. The UI must not substitute `task:{id}` for an advertised retry instance key.
3. During a mutation, retain the existing disabled/busy states. After an accepted mutation, request a fresh full dashboard state before unlocking. `isDashboardReconciled(requiredSequence)` is true only for a positive integer sequence at or below `lastSuccessfulDashboardLoadSequence`; it protects against an old or failed load unlocking controls.
4. If the mutation response succeeds but its confirmation read fails or does not contain the exact expected authoritative evidence, preserve the last confirmed view, set `activeDeliveryUnreconciled`, and keep controls locked. A failed read never reverses the mutation or converts it to a failed workflow transition.
5. A rejected/failed mutation restores the previous enabled-control state only when no write occurred. A cancelled confirmation before a write is activity with no formal phase advance; the prior confirmed candidate/review remains current.
6. Retry scope is authoritative. Current retry progress uses retry-local `task`, `completion`, `stories`, review/closure/triage data and `effective_status`; original facts stay visible only in explicit **Original attempt** history fields. Provider/command retries, test reruns and corrections never increment retry ordinal. Formal retry appears only through a current advertised `retry_sprint` route and its exact preview/binding.

The generic `runDirectAction()` handler cannot be attached blindly to a new Task board: task completion has strong selection requirements, requires the exact `instance_key`, and the current frontend does not fetch per-Task detail/history. A board implementation must either route its control through the existing execution binding/action proof or add a narrowly tested wrapper that retains every guard above. It must not post by a displayed Task ID alone.

## Browser presentation state and manual refresh

The new local presentation state is intentionally non-authoritative:

```text
view = {
  viewedStage: "09".."13" or "01".."08",
  selectedSprintId: number | null,
  selectedTaskId: number | null,
  inspectorTab: "overview" | "details" | "checks" | "activity",
  filters: { taskStatus?: string, availability?: string, storyId?: number },
  inspectorScrollTop: number,
  boardScrollTop: number,
  lastConfirmedAt: ISO-8601 | null,
  refreshState: "idle" | "refreshing" | "failed"
}
```

This state is preserved across a successful manual full refresh when its referenced stage/Sprint/Task still exists. When a selected Task disappears from the returned selected Sprint or is no longer in the returned scope, preserve `selectedTaskId` and every other valid local context field. The inspector shows **This Task is no longer available in the selected scope** until the user makes a new selection; it does not clear selection, choose a replacement, move focus, or scroll the board. When the selected Task remains, preserve its tab and scroll positions even when another Task's formal status/count changes. Never use an old Task response to overwrite a newer full-state response; request sequencing/cancellation must follow the existing `dashboardLoadSequence` and `AbortController` pattern.

Refresh behavior for the first slice:

- initial load: render map/inspector loading state; do not mark a stage current until `/position` succeeds;
- successful manual refresh: atomically replace read state, update `lastConfirmedAt`, preserve the selected subject or render its unavailable-in-scope state, then render;
- one read fails: retain the prior confirmed state, show its confirmation time and a visible failure message; no mutation controls unlock from that failure;
- no active Sprint: show **No Sprint selected/recorded** and avoid the Task endpoints;
- zero Task inventory: show `0 Tasks` from `count`, not an empty-filter message;
- filtered no-match: preserve total/count and show **No Tasks match this filter**;
- stale response/cancelled request: discard without erroring or changing the display.

## Explicitly proposed later contracts — not implemented or approved for this slice

### Durable project-update mechanism

This proposal defines the minimum behavior a later backend design must satisfy; it does not select a transport or schema.

- Each visible-project business mutation writes its durable, monotonically ordered project-change row atomically in the same transaction as the business facts and workflow receipt. The row contains project ID, opaque cursor/version, committed time, affected durable subject references and mutation/receipt identity. It covers Task completion, Story closure, Sprint review, Sprint close and triage as well as existing planning transitions. Only notification occurs after commit.
- A browser notification is only a hint `{ project_id, cursor }`. On it, the browser performs authoritative reads and commits the result only if its returned version/cursor is not older than the last confirmed state. Repeated, lost and out-of-order hints are harmless.
- Reconnect/reload supplies a cursor and returns missed committed change identities or signals that the browser must take a full authoritative snapshot. A failed fetch keeps the prior confirmed state and never replays a mutation.
- The accepted implementation plan must name the retention/replay boundary, authorization boundary, transaction/outbox relationship, exactly covered mutation paths, user-visible freshness wording, and tests for delayed older responses, duplicate notices, reconnect, a read failure after a successful mutation and a change made outside the browser.

No existing `workflow_events` observation proves this coverage. Existing records are durable facts but are not evidence of a complete change journal or browser push channel.

### External activity capture

This separate proposal is for displayable observations, not formal workflow state.

- A durable activity item needs an immutable ID, project/Sprint/Task subject when known, source kind, observed/recorded timestamps, outcome classification, safe summary, correlation/reference to the supporting artifact or receipt, and an explicit visibility/redaction policy.
- Sources must identify whether they are workflow mutation, internal attempt, validation/check, human decision, Git/delivery, or integrated external execution. An item can link to a formal completion but cannot set `Task.status`, create Story closure, review, closure, triage, or retry scope.
- Unintegrated tools report **Unavailable**, not silence. Imported historical reports retain their source/date and remain evidence, not live activity.
- The later implementation must define deduplication/idempotency, cancellation/denial/partial-output retention, retention/redaction, correlation rules and the boundary between observed activity and a claim of complete observation.

## Acceptance cases and owning tests

These are the required contract tests for the first implementation slice. They name the existing owners that prove the underlying server behavior and the new owning frontend suite that must be added and registered when implementation starts.

| Case | Expected result | Existing authority test / new owning test |
| --- | --- | --- |
| Thirteen cards are always present; 09–13 do not collapse to the old Execution/Review cards | One persistent map, explicit **You are here** and **Viewing** may differ | `tests/test_workflow_position_display.mjs`; add `tests/test_lifecycle_workspace.mjs` |
| Required/recovery current-stage set follows CLI-compatible categories | Available required/recovery decisions plus only supported waiting review kinds are **Here**; optional re-entry is separate; an empty set has no fake Stories current marker | `tests/test_workflow_position_display.mjs`; add `tests/test_lifecycle_workspace.mjs` |
| Multiple advertised decisions coexist | All matching cards show their own action/condition; no first-action/maximum-stage inference | `tests/test_workflow_position_display.mjs`; add `tests/test_lifecycle_workspace.mjs` |
| Board uses complete Task inventory | `count/items` render every Task, including Done and non-current Task; aggregate comes from statuses | `tests/services/test_sprint_status_projection.py`; add `tests/test_lifecycle_workspace.mjs` |
| Formal status differs from availability/activity | A To Do Task can show retained activity; `dependencies_satisfied` and exact action availability remain separate | `tests/services/test_sprint_status_projection.py`, `tests/test_task_execution_service.py`; add `tests/test_lifecycle_workspace.mjs` |
| Selected Task detail/history preserves retry boundary | Retry-local Task has retry instance key/current evidence and original completion remains in original fields; Stage 12 reads triage from the matching history attempt scope | `tests/services/test_sprint_status_projection.py`, `tests/test_sprint_retry_dashboard.mjs`; add `tests/test_lifecycle_workspace.mjs` |
| Completion, Story closure, review, close and triage remain separate | Each formal fact/action appears only at its own stage; no 3/3 shortcut | `tests/workflow/test_execution_transitions.py`; add `tests/test_lifecycle_workspace.mjs` |
| Exact action binding rejects stale selection | The board cannot send a Task ID-only completion or an action from an old retry/current position | `tests/test_sprint_retry_dashboard.mjs`, `tests/workflow/test_execution_recovery.py`; add `tests/test_lifecycle_workspace.mjs` |
| Accepted mutation plus failed reload remains locked | Prior confirmed view stays visible; `activeDeliveryUnreconciled` retains lock until a successful authoritative reload | `tests/test_cockpit_action_synchronization.mjs`, `tests/test_sprint_retry_dashboard.mjs`; extend/add `tests/test_lifecycle_workspace.mjs` |
| Refresh preserves valid selection and local context | Task 14 selected/tab/scroll survives Task 16 update; an unavailable selected Task preserves its selected ID/context and waits for user choice without stealing focus | add `tests/test_lifecycle_workspace.mjs` |
| First-slice freshness/error wording is honest | Manual refresh is explicit; failed refresh retains timestamped confirmed data; no activity source is invented | add `tests/test_lifecycle_workspace.mjs` |

When tests are added, register the Node suite in `cli/dev_checks.py`, `.github/workflows/ci.yml`, `tests/dev_runtime/test_dev_checks.py`, and `tests/test_ci_contract.py`, following the repository's current registration contract. Backend projection changes require corresponding service/API tests; this first slice should require none because it consumes the existing routes.

## Material gaps and recommended resolutions

1. **There is no dedicated selected-Sprint review/triage HTTP route.** This does not block Stage 12: `/sprints` already exposes retry-scoped `execution_attempts[].triage[]`. The first slice must select the exact `sprint_id`/`retry_attempt_id` row and label its scope. Recommend: use that existing projection now; consider a dedicated bounded route only if the later product needs less project-wide history or additional review fields.
2. **There is no complete external activity feed.** Task execution logs and retained Sprint history cover durable status transitions, not AGY/provider/test/review/Git activity. Recommend: label those sources unavailable in production and use dated report fixtures only for design review until the separate capture contract is implemented.
3. **The current frontend's `selectedStageTab` conflates viewing and current.** It also only knows nine labels. Recommend: introduce a small local presentation-state adapter in the first frontend slice, retaining existing mutation state/guards and leaving graph derivation unchanged.
4. **The existing `loadDashboard()` does not fetch Task list/detail/history.** Recommend: add scoped, abortable reads only after a selected Sprint/Task exists; preserve the successful full dashboard state if a scoped read fails and do not gate graph action validity on optional activity reads.
5. **Automatic updates have no demonstrated durable notification coverage.** Recommend: ship manual-refresh freshness first, then approve the separate durable update mechanism after coverage and recovery semantics are designed and tested.
