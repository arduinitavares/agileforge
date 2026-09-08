# Issue #260: Retry one completed Sprint

Status: clarified behavior agreed in conversation; written design awaiting review.

Issue: https://github.com/arduinitavares/agileforge/issues/260

Baseline: `da3dbf630e43a16561b335087888c0e688754925`.

## User need

A Sprint was implemented on the wrong branch. The operator wants to execute
that same Sprint again from scratch, using its already approved work.

AgileForge is a workflow guardrail. The person or executing model chooses and
prepares the correct branch or worktree. AgileForge provides **Retry Sprint**:
it makes that Sprint's approved work executable again and requires fresh
completion evidence. It does not manage Git or clean product files.

There is no target-branch field, branch-selection policy, worktree manager, Git
revert, automatic repository rebinding, or special branch migration in this
feature. Existing repository checks still apply where they already apply. A
retry must not introduce a requirement to execute on the original attempt's
branch. The new attempt follows the normal execution and evidence workflow.

## Visible behavior

1. The operator selects a specific completed Sprint and requests a retry preview.
2. The preview names the Sprint, its approved Stories and Tasks, the work that
   will become pending, and any reason retry is unavailable.
3. The operator confirms **Retry Sprint**, with a rationale. The API/CLI also
   requires actor, the preview's expected-state fingerprint, and an idempotency
   key. The dashboard carries technical bindings internally.
4. The Sprint gets a fresh execution attempt in the planned state. Normal
   explicit start remains required. Every scoped Task and Story needs fresh
   execution evidence and closure for this attempt.
5. The normal Task completion, Story closure, Sprint review, Sprint closure,
   and post-Sprint triage sequence runs again.

The dashboard shows the current retry and keeps prior completion history
accessible. A concise attempt label is enough; users do not configure an
attempt system. Earlier Sprints and all unrelated work keep their identities,
statuses, scope, and evidence.

## Sequential scope

The command targets the exact requested Sprint; it never substitutes another.
Only the latest eligible completed Sprint can be retried in issue #260. If a
later live Sprint or planning artifact exists, explain which item blocks retry.
Handling or removing later work is a separate operation. This feature does not
delete or cascade-reset it, and does not assume a supported removal command
exists where none is advertised.

Eligibility must be proven from accepted-plan lineage, Sprint start/closure,
current triage, generation attempts, and relevant transition state. The largest
Sprint ID or completion timestamp alone is insufficient. Reject missing,
ambiguous, or conflicting lineage. Require the selected Sprint's latest attempt
to be completed with resolved triage, and reject competing active delivery.

Block later live plan drafts, accepted plans, generation attempts, or dependent
artifacts that would be invalidated, including in-flight transitions. Retain
existing failure and recovery guards; a failed provider attempt is not assumed
resolved solely because it stopped running. Revalidate the exact approved
content and dependencies. Changed or superseded requirements require their
normal correction workflow, not a retry of a different scope.

A completed retry may itself be retried while the same Sprint remains eligible.
The new attempt references the immediate prior completed attempt. There can be
only one current planned/active attempt for a project. Older Sprints remain
ineligible even if their numeric IDs or timestamps are misleading.

## Preserve requirements and previous evidence

Use the original accepted plan, Story identities and acceptance criteria, Task
identities and checklists, selected scope, and dependency contract. Do not
generate a plan or Story artifact, supersede shared Stories, or weaken the
existing plan-replacement guard.

Keep the original Sprint, accepted artifacts, Task evidence, Story closures,
Sprint start/review/closure, triage, audit events, and transition receipts
unchanged. Previous completion is history, not proof of work for the retry.

Within the retried scope, dependency completion comes from the current attempt.
Completed work outside that scope continues to use its existing valid evidence.
This prevents an old Done status from unblocking a Task that must be redone.

## Internal design

Introduce a narrow execution-scope boundary: an original Sprint execution or
one retry of that Sprint. The scope owns effective execution state and the
evidence used by the graph. It does not own repository operations or approved
requirements.

Add retry-specific persistence rather than rewriting original execution rows:

- A retry attempt identifies project, source Sprint, predecessor attempt (or
  original execution), ordinal, approved-contract fingerprint, creating actor,
  rationale, and creation receipt. Its lifecycle begins planned.
- Per-attempt Story and Task progress references existing requirement identities.
  Initial progress is To Do. Original shared Story/Task rows are not reset.
- Immutable attempt-bound start, Task evidence, Story closure, Sprint review,
  Sprint closure, and triage records capture the same semantic facts required
  for original execution. Uniqueness and foreign keys bind every fact to its
  attempt and exact scoped subject.

Original execution remains readable without a historical backfill. Add the new
tables through the existing schema setup and verification mechanisms; preserve
the definitions and bytes of existing evidence records. Use separate typed
retry records and shared validation/fingerprint functions, not an unvalidated
generic JSON event bag or a second independent workflow implementation.

The execution-scope resolver is used by graph routing, command validation,
execution services, dependency checks, and read projections. Historical
integrity checks retain original facts; do not fake a historical completed
Sprint as active by overwriting its fact in a snapshot. New planning remains
blocked while the current retry is planned or active, despite the original
Sprint's completed status.

Preserve existing command bindings for original execution. Retry actions carry
the attempt identity in their positioned request and instance binding. Include
the execution scope in completion/review fingerprints. Replaying an original
receipt can return its original result but cannot mutate, satisfy, or appear as
completion of the current attempt. An action captured for attempt 2 cannot
complete work in attempt 3.

## Preview, apply, and concurrency

The preview is read-only. It reports the exact target and approved work,
preserved history, proposed state, existing repository provenance, and named
blockers. Repository information is descriptive evidence from the existing
binding; it is not a request to choose a branch or a new Git guardrail.

Its fingerprint binds the target attempt, approved scope, relevant provenance,
and eligibility facts. Apply recomputes eligibility after acquiring the existing
workflow transaction lock. It validates confirmation, actor, rationale,
expected-state fingerprint, and idempotency before creating retry state.

Use the established workflow-domain transaction and receipt boundary. Creating
the attempt, its initial progress, and its successful receipt is atomic. A
failure rolls back these writes together. Database constraints and transaction
serialization prevent competing requests from creating parallel attempts.
Same-key/same-input retries return the same result; changed input under the same
key conflicts. Different-key requests from a stale preview also conflict.

Opening the preview, reloading the dashboard, or completing the original Sprint
never retries it automatically. Retry is an optional owner action in the graph;
normal post-Sprint routing must not repeatedly recommend restarting completed
work. Once confirmed, routing selects the pending retry's valid start path.

## Components

Keep implementation focused on these existing boundaries:

- `models/`: additive retry state and evidence models, schema verification.
- `repositories/workflow.py` and `workflow/facts.py`: durable retry facts
  and their association with the original Sprint.
- `services/`: one execution-scope resolver and retry preview/apply service;
  reuse existing execution validation through that boundary.
- `workflow/definitions/`, `workflow/requests/`, `workflow/handlers/`, and
  `workflow/domain.py`: eligibility, positioned actions, transactional apply,
  and consistent execution/planning routing.
- `services/application.py`, `cli/main.py`, `cli/workflow_commands.py`, and
  `api.py`: the same preview and confirmed retry contract for each transport.
- `frontend/project.js` and its existing confirmation surface: Retry Sprint,
  blocked-state explanation, fresh current progress, and previous history.

Do not change provider selection, repository management, external product
repositories, or the platform-support strategy. Detailed file decomposition and test-first tasks
belong in the implementation plan after written-design review.

## Required verification

Use disposable synthetic repositories, profiles, and databases only. Never
copy an operator's business or trace database into a test fixture.

- Two sequential completed/triaged Sprints share an accepted multi-Story
  artifact. Retry only the second, then complete its new attempt through normal
  graph, start, completion, human-review, closure, and triage actions.
- Snapshot all original evidence and unrelated records before retry; compare
  them afterward. Prove original fingerprints and history remain valid after
  process restart and after completing the retry.
- Verify exact approved content and dependency reuse, fresh scoped To Do
  progress, and rejection of old completion/review bindings and receipts as
  evidence for the new attempt.
- Refuse older targets, misleading IDs/timestamps, later pending/accepted
  plans, later active/completed Sprints, unresolved generation/triage,
  in-flight work, and ambiguous or conflicting lineage.
- Exercise stale preview/provenance, changed requirements, same-key replay,
  changed same-key input, concurrent different-key requests, and failure after
  partial writes. Assert atomic rollback and no collateral mutation.
- Complete and retry the same eligible Sprint again; demonstrate that attempt 2
  evidence cannot complete attempt 3 and that next-Sprint planning becomes
  available only at the appropriate terminal checkpoint.
- Verify CLI/API parity, dashboard confirmation with no premature POST, named
  blockers, separate current/history display, and reload persistence.
- Prove retry invokes no provider, Git mutation, file cleanup, repository rebind,
  or external product-state operation. Preparing another branch is an executor
  action; retry adds no branch-specific acceptance requirement.
- Run focused Python, frontend, and browser regressions, then the canonical
  repository quality gate and distribution/schema verification on the final
  implementation. Independently review actual diffs and raw results.

## Working-copy safety and delivery

Implementation belongs on `alex/issue-260-retry-sprint` in the isolated
`.worktrees/issue-260` checkout. The original `master` checkout and its runtime
data remain untouched. The new worktree owns an empty disposable development
profile; tests create their own synthetic state.

Codex owns design, planning, acceptance, and final review. Implementation follows
the requested Superpowers test-first and review workflow, with any delegation
explicitly configured under the current user policy. No operational Sprint is
retried as part of developing this feature. Finish with verified commits and
present the merge, pull request, keep, and discard options for the user's
selection. Remove this task's temporary worktree when it is no longer in use,
preserving its committed deliverables unless the user explicitly chooses discard.

## Design-stage verification

The isolated checkout's `agileforge-dev info --profile issue-260-design --json`
validated its own development profile and business schema at the baseline.
No provider credential was present.

Baseline command:

```text
uv run --locked --exact --python 3.13.15 pytest tests/workflow/test_execution_graph.py tests/workflow/test_execution_transitions.py -q
```

Result: **69 passed, 5 warnings in 139.22 seconds**. Warnings concerned a blocked
socket attempt and deprecated ADK configuration. These are existing baseline
tests, not evidence that Sprint retry is implemented. No product source changed
during this design stage. The original `master` checkout remained clean at the
baseline commit.
