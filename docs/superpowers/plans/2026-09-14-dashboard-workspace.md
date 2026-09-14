# AgileForge lifecycle workspace implementation plan

> For agentic workers: use Superpowers subagent-driven-development with the user's explicit model policy. The user authorized this first implementation slice on 14 September 2026. Implement the single integrated task below, with tests first and independent specification/quality review before completion.

**Goal:** Replace the mixed vertical project page with the approved persistent 13-stage map and one focused inspector, including every Task in a selected Sprint.

**Architecture:** Retain `project.js` as the owner of existing authoritative reads, mutations and reconciliation locks. Add a focused classic-script workspace module for presentation state, map rendering and scoped Task reads. Preserve original/retry evidence and all supported forms without adding backend mutations, invented statuses, automatic polling or a framework.

**Stack:** Existing HTML/JavaScript, Node's built-in tests, existing FastAPI reads. Python tools use uv and the current checkout only.

**Spec:** `docs/superpowers/specs/2026-09-14-dashboard-workspace.md`, plus reference snapshots under this plan's `.superpowers/sdd` workspace. Runtime user data is not a test fixture.

## Global constraints

- Keep all 13 stages: Create Project; Vision; Product Goal; Specification; Backlog; Roadmap; Stories; Sprint plan & start; Develop & verify; Close Stories; Review & close Sprint; Learn & triage; Assess next Sprint. Stages01–06 are framing,07–13 recurring delivery.
- Separate Here, Viewing, persisted status, graph availability and observed activity. Navigation does not mutate the workflow.
- Preserve approved warm neutral canvas, white inspector, restrained teal current marker, blue Viewing, Inter,8px rounding. Body14px, supporting controls at least12px. Structural responsive layout at1024px, not tiny text.
- First slice says 'Manual refresh' and a last-confirmed timestamp. No simulated activity, SSE, polling or frontend-created In Progress.
- Reuse guarded action handlers and fingerprint/instance-key protections. Do not unlock after a failed read or add a generic advance-stage mutation.
- Preserve selected stage, Sprint, retry scope, Task, tab, filters, scroll, keyboard focus and unsent form values across reconciliation. Old responses cannot replace a newer view.
- Selected record disappearance retains its identity and shows unavailable state until user chooses; do not navigate or steal focus.
- Do not touch main checkout, user AGENTS changes, live profiles, stored secrets, billing, remote Git, or production data. No merge, push, PR, deployment or worktree removal in this task.

## Task 1: Connected lifecycle workspace and complete Task board

This is one integrated feature with one review gate. Work in four test-first increments below; each increment must be testable before continuing.

### File ownership

Create:
- `frontend/lifecycle-workspace.js`: presentation derivation, local view state, scoped read orchestration and DOM coordination.
- `frontend/lifecycle-workspace.css`: scoped workspace styles and responsive layout.
- `tests/test_lifecycle_workspace.mjs`: behavioral state, board, loading/reconciliation tests in the existing Node VM style.

Modify:
- `frontend/project.html`: approved two-pane shell, map host, one inspector, shared dialogs, load CSS and workspace script before `project.js`.
- `frontend/project.js`: thin workspace integration and stage-scoped rendering; preserve existing projection/mutation functions.
- `pyproject.toml`: include `*.css` in frontend package data.
- `tests/test_frontend_package_resources.py`, `scripts/verify_distribution.py`: include both new assets in resource, archive and installed checks.
- `cli/dev_checks.py`, `.github/workflows/ci.yml`, `tests/dev_runtime/test_dev_checks.py`, `tests/test_ci_contract.py`: register new workspace suite plus existing review-safety/cockpit-synchronization suites.
- Existing affected frontend test files only to add coverage or adapt an intentionally changed public presentation contract; do not weaken behavioral expectations.

No backend schema or service changes are needed for this slice. Do not create an npm pipeline.

### Public module contract

Expose the classic-script lexical binding `AgileForgeWorkspace`. It must load independently in Node VM, without accessing DOM at module initialization. Existing tests that load `project.js` alone must still work through an optional `typeof AgileForgeWorkspace !== 'undefined'` integration guard.

```js
// All methods are members of AgileForgeWorkspace.
// JsonObject means an ordinary JSON object from existing routes.
// StageId is an integer1..13. Unknown fields fail closed, not fabricated.
stages(); // Array<{id:number,label:string,group:'framing'|'delivery'}>
stageForDecision(decision); // number|null
currentStageIds(position); // number[], from required/recovery immediate decisions only
taskRows(status, position, actions); // Array<{task:JsonObject,availability:string,action:JsonObject|null}>
taskCounts(tasks); // {total:number,done:number,remaining:number}
createView(); // {stageId:null,sprintId:null,taskId:null,tab:'details',filter:'all',scopeKey:null}
reconcileView(view, status); // same selection values retained, plus record availability result
createController({requestJson,onChange}); // explicit dependencies; no global fetch
// controller.select({projectId,sprintId,taskId,scopeKey}) -> Promise<void>
// controller.refresh() -> Promise<void>
// controller.snapshot() -> {kind,data,error,selection,lastConfirmedAt}
// controller.dispose() cancels pending reads and invalidates response generation
```

Controller `kind` is `idle|loading|ready|stale|error|unavailable`. `data` holds confirmed selected Task detail/history only. `stale` retains prior confirmed data after refresh failure. Initial failure is `error`; domain Task-not-found is `unavailable` with selection retained. It must require positive project/Sprint/Task IDs and bind response identity/retry scope before accepting data. `onChange` schedules display only; it cannot alter formal records.

Implementation may add private functions. These public names and shapes are the neighboring task/test contract and must stay consistent. DOM functions belong in the same scoped module and receive a bridge with existing read/render/action dependencies; no direct status mutation is permitted.

### Increment A: Presentation derivation

- [ ] Add runnable Node VM tests, including these exact starting cases. Extend the fixture with complete decision/fact references for actionable Task tests, using the existing retry suite's shape.

```js
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const source = fs.readFileSync(path.resolve(import.meta.dirname,
    '../frontend/lifecycle-workspace.js'), 'utf8');
function api() {
    const context = vm.createContext({AbortController, URLSearchParams, console});
    vm.runInContext(source, context);
    return vm.runInContext('AgileForgeWorkspace', context);
}
test('all thirteen stages retain logical navigation order', () => {
    assert.deepEqual(Array.from(api().stages(), s => s.id),
        [1,2,3,4,5,6,7,8,9,10,11,12,13]);
});
test('current work excludes optional retry and retains concurrent required work', () => {
    const position = {decisions: [
        {request_kind:'retry_sprint', category:'available', recommendation_kind:'optional_reentry'},
        {request_kind:'complete_task', category:'available', recommendation_kind:'required'},
        {request_kind:'close_story', category:'available', recommendation_kind:'required'}
    ]};
    assert.deepEqual(Array.from(api().currentStageIds(position)), [9,10]);
});
test('full inventory counts completion independently of task availability', () => {
    const result = api().taskCounts([
        {task_id:14,status:'Done'},
        {task_id:15,status:'To Do'},
        {task_id:16,status:'To Do'}
    ]);
    assert.equal(result.total,3);
    assert.equal(result.done,1);
    assert.equal(result.remaining,2);
});
test('reconciliation retains the inspected task after another task completes', () => {
    const view = {...api().createView(),stageId:9,sprintId:5,taskId:14,tab:'checks',filter:'all'};
    const status = {sprint:{sprint_id:5},current_retry:null,tasks:[
        {task_id:14,status:'Done'},{task_id:15,status:'Done'},{task_id:16,status:'Done'}
    ]};
    const result = api().reconcileView(view,status);
    assert.equal(result.taskId,14);
    assert.equal(result.tab,'checks');
    assert.equal(result.stageId,9);
});
```

- [ ] Run `node --test tests/test_lifecycle_workspace.mjs` and retain the expected failing result before adding production code. A missing-file error is setup evidence; add the minimal module surface and confirm assertions fail for the missing behavior before implementing it.
- [ ] Implement the pure functions. `stageForDecision` maps exact request kinds from the spec. `currentStageIds` includes required/recovery available decisions plus waiting review kinds supported by `cli/workflow_commands.py:render_workflow_next`; exclude optional re-entry and future/complete/blocked decisions from Here. Show multiple current markers if needed. No fallback current Stories.
- [ ] Pair actionable Tasks exactly as `sprintExecutionProjection()` does: request kind, node, instance key, available decision, ready reason and exact Task fingerprint reference. Locked actions stay locked. Noncurrent dependency-satisfied To Do rows are Queued; unknown dependency state is unknown, not blocked. Done stays Done and visible.
- [ ] Run new and existing workflow-position/Sprint-retry tests. Verify unknown/malformed task records and retry scope cannot acquire an executable action.

### Increment B: Real workspace shell and scoped inspector

- [ ] Add tests proving selecting stage07 while current08 leaves Here08 and shows only stage07 content, clicking the map makes zero fetch mutations, keyboard stage order is01–13, and Back restores stage/tab. Include DOM assertions, not only string-existence assertions.
- [ ] Build the map as native buttons with `aria-current="step"` only for graph-current cards and separate Viewing text. Keep one inspector and its labelled tabs. Add Return to current work with explicit behavior for several current stages. Selecting a stage never chooses a mutation.
- [ ] Scope existing panel markup by selected stage. Stages02–04 reuse their existing panel functions; stage01 owns identity/repository. Split the `deliveryPanelMarkup()` assembly into stage-scoped pieces while preserving existing guarded functions, continuation checks and form data attributes. All existing actions stay reachable in their relevant inspector. Stage09 shows the full Task board;10 Story closure;11 review/close;12 triage;13 remaining candidates/retained Sprint and optional re-entry conditions.
- [ ] Keep project header and short Goal strip. Remove the third context pane as a visible competing inspector. Move its relevant source/binding detail into the selected subject. Preserve shared dialogs and existing event delegation. Do not retain invented source hash fallbacks such as `sha256:verified` as visible evidence.
- [ ] Preserve scroll/focus/drafts around render by stable semantic keys, with no raw innerHTML form restoration. A source/Task scope change must never silently attach an old draft to a new subject. Browser Back restores view state without re-running mutations. Auto-refresh is not part of this slice.
- [ ] Validate1024px and1440px layouts. The map stays visible and legible; detail scroll is contained. Stage buttons, tabs and row controls support keyboard focus. Motion respects reduced-motion settings.

### Increment C: Complete board, scoped reads and honest freshness

- [ ] Add asynchronous tests with deferred promises: select Task14 then15; resolve15 then14; only15 remains. Refresh the same selection, reject fetch, and retain confirmed data with stale state. Select an unavailable Task and preserve selected ID with unavailable text. Include mismatched project/Sprint/retry response identity and malformed JSON.
- [ ] Build every board row from the selected Sprint's validated `tasks` inventory or `/sprints/{id}/tasks` response, not from available actions. Use the existing `/sprints/{id}` route for historical Sprint selection and existing history for retry/original context. Do not pretend the API supports arbitrary retry-selection arguments; if that detail route addresses only effective scope, clearly present retained historical evidence and guard current detail reads.
- [ ] Selected Task uses `/sprints/{id}/tasks/{taskId}` and `/execution`, requested only for the selected subject. Details show description and metadata checklist where valid; Checks show persisted checklist/acceptance evidence, not imaginary test runs; Activity shows retained status logs and explicit unavailable external activity. Escape all untrusted data through existing text-escaping conventions. No raw HTML from evidence strings.
- [ ] `createController` owns AbortController and a monotonically increasing request generation. Accept a result only when generation and complete subject/scope match. Catch every async UI handler failure and render contextual error. Failed detail reads must not erase a valid board or global confirmed state.
- [ ] Update counts/current data after existing manual refresh and mutation confirmation. Preserve valid selection/tab/filter/scroll/focus/drafts. Display explicit 'Manual refresh' and last successful confirmation time. A refresh failure keeps confirmed data and existing mutation locks; no apparent rollback.
- [ ] Verify UI-level Task14 selection remains while Task16 changes in a fresh server snapshot, including tab, scroll and focus. Counts progress0/3→1/3→2/3→3/3 with all rows visible. AllDone must not close the Story/Sprint.

### Increment D: Packaging and integration checks

- [ ] Add both assets to resource/archive/installed verification and CSS package data. Register required Node suites in the four matching runner/CI contract files.
- [ ] Run all affected frontend suites, then Python package/check-registration tests with the repo-pinned controller. Resolve any regressions caused by this change without weakening tests.
- [ ] Produce a concise implementation report under the plan's SDD workspace with changed files, RED/GREEN receipts, commands/results, any unverified browser scenarios and remaining concerns. Do not claim deployment or backend live updates.
- [ ] Commit the verified candidate locally after targeted checks pass. Parent runs the full clean-checkout gate, independent review, browser/visual acceptance and any necessary fixes. No remote operation.

## Verification commands

Run from `C:\Users\atavares\Projects\agileforge\.worktrees\dashboard-lifecycle`:

```powershell
node --test tests/test_lifecycle_workspace.mjs tests/test_workflow_position_display.mjs tests/test_sprint_retry_dashboard.mjs tests/test_dashboard_review_safety.mjs tests/test_cockpit_action_synchronization.mjs tests/test_create_project_modal_required_fields.mjs tests/test_vision_interview_ui.mjs
pyrepo-check --python 3.13.15 pytest tests/test_frontend_package_resources.py tests/dev_runtime/test_dev_checks.py tests/test_ci_contract.py
git diff --check
```

After the local commit, parent runs `sh ./agileforge-dev check`, retaining output outside the checkout, plus isolated browser acceptance of the built application. A failing focused baseline is evidence to diagnose, not an excuse to relax the gate.

## Exit criteria

- New scripts/styles ship in the package, not just the checkout.
- All13stages, full Task inventory and selected detail render from actual API contracts.
- Existing guarded forms and retries retain their safety tests.
- Selection/freshness behavior and the two-pane layout are verified; manual refresh and unavailable external activity are labelled honestly.
- Independent reviewer returns specification compliance and code-quality verdicts; blocking defects fixed and confirmed.
- Live-update backend, activity ingestion, merge/push and operator rollout remain separate milestones.
