# AgileForge Agent CLI Manual

This manual is the operational reference for agents using the `agileforge`
command-line interface. It is written for agent skill authors and automation
authors who need deterministic command contracts, recovery rules, and safe
workflow sequencing.

The CLI is JSON-first. Agents should parse JSON envelopes, inspect error codes,
and follow explicit remediation and `next_actions` fields. Do not scrape human
help text when a JSON command or schema is available.

## Current Scope

The installed CLI supports:

- Project inspection.
- Project creation with a guarded mutation ledger.
- Project setup retry for interrupted creation/setup recovery.
- Workflow and status inspection.
- Spec Authority status, review, accept, reject, and invariant inspection.
- Vision generate, history, and save.
- Backlog generate, history, and save.
- Roadmap generate, history, and save.
- Story pending, generate, retry, history, save, complete, repair, and read projections.
- Sprint candidates, generate, history, save, start, status, tasks, and task
  tickets.
- Bounded context packs for agents.
- CLI diagnostics, schema readiness, command discovery, and command schemas.
- Mutation ledger inspection and recovery lease acquisition.

The installed CLI does not yet support:

- Story close, Sprint close, deleting, or resetting workflow artifacts from the
  CLI.
- Task claim/lease, per-checklist-item tracking, or automatic validation run
  capture.

Agents must not invent unavailable commands. Always confirm command availability
with:

```sh
agileforge capabilities
agileforge command schema "agileforge project create"
```

## Mental Model

AgileForge stores canonical agile workflow state in the central AgileForge
repository. The CLI is the agent-facing transport over that state. Agents call
the CLI from any project directory, but AgileForge internals still resolve from
the central repository.

The CLI has two broad classes of commands:

- Read-only projections: inspect state and return guard tokens.
- Mutations: create or repair canonical state through idempotent commands.

Mutations are guarded by a mutation ledger. The ledger records command identity,
idempotency key, request hash, project id where known, progress steps, status,
stored response, and recovery metadata. This exists so agents can safely retry
after timeouts, crashes, interrupted setup, and stale reads.

Manual checkpoints are a core policy. Generated or compiled artifacts do not
automatically become accepted canonical authority unless an explicit installed
command says so. `project create` compiles a pending authority artifact, and
`authority accept` is the explicit command that makes that reviewed artifact
canonical.

## Central Shim Usage

The intended machine-level executable is:

```sh
~/.local/bin/agileforge
```

The shim should run the central AgileForge repository through `uv`:

```sh
#!/bin/sh
exec uv run --project /Users/aaat/projects/agileforge python -m cli.main "$@"
```

This design means:

- Agents can call `agileforge` from any caller repository.
- Relative user inputs such as `--spec-file specs/spec.json` resolve relative to
  the caller's current working directory.
- AgileForge code, dependencies, `.env`, and storage settings resolve through
  the central repository.
- Agents do not need to install AgileForge into every project.

Confirm the shim:

```sh
command -v agileforge
agileforge --help
```

On the standard local development machine, `command -v agileforge` should return
`/Users/aaat/.local/bin/agileforge`, and that shim should call the central repo.
Agents should still invoke `agileforge ...` directly. Do not introduce shell
aliases such as `$AF` into task instructions unless the user explicitly asks for
one.

Confirm caller-relative file behavior from another repository:

```sh
cd /path/to/caller-project
agileforge project create --dry-run \
  --dry-run-id preview-project-001 \
  --name "Preview Project" \
  --spec-file specs/spec.json
```

## Environment Expectations

The CLI reads AgileForge configuration from the central repo runtime. The usual
environment variables are:

- `OPEN_ROUTER_API_KEY`
- `AGILEFORGE_DB_URL`
- `AGILEFORGE_SESSION_DB_URL`

Run diagnostics before mutation-heavy work:

```sh
agileforge doctor
agileforge schema check
```

`doctor` checks runtime readiness. `schema check` verifies storage readiness for
the CLI contract. If either command returns `ok: false`, agents should stop and
surface the structured error.

## Project Setup Through Story

This is the current canonical CLI flow for starting a project from an
`agileforge.spec.v1` JSON spec, reviewing and accepting compiled authority, and
running Vision, Backlog, Roadmap, and Story. Run these commands from the caller
repository, not from the AgileForge repo, so relative paths such as
`specs/spec.json` resolve correctly.

Validate the structured spec and rendered Markdown pair when both are present:

```sh
cd /path/to/caller-project

agileforge spec profile validate \
  --spec-file specs/spec.json \
  --render-md specs/spec.md
```

Create the AgileForge project:

```sh
agileforge project create \
  --name "Project Name" \
  --spec-file specs/spec.json \
  --idempotency-key "create-project-$(date +%Y%m%d%H%M%S)" \
  --changed-by codex > project-create.json
```

Read the project id from the JSON envelope:

```sh
PROJECT_ID="$(
  python - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("project-create.json").read_text())
if not payload.get("ok"):
    raise SystemExit(json.dumps(payload.get("errors", payload), indent=2))
print(payload["data"]["project_id"])
PY
)"
```

Ask AgileForge for the next installed command:

```sh
agileforge workflow next --project-id "$PROJECT_ID" | python -m json.tool
```

For a newly created project, the next step should be authority review. Produce a
full review packet:

```sh
agileforge authority review \
  --project-id "$PROJECT_ID" \
  --include-spec full > authority-review.json
```

Agents must inspect `authority-review.json` before acceptance. The review is not
a shell command. Do not type review prose into the terminal. The agent should
read the JSON packet and verify accepted spec items, invariants, source-map
evidence, gaps, assumptions, and rejected features. If the compiled authority
misses, distorts, or weakens accepted normative requirements, reject it.

Accept only after review passes:

```sh
agileforge authority accept \
  --project-id "$PROJECT_ID" \
  --changed-by codex > authority-accept.json
```

Confirm authority is current and ask for the next step:

```sh
agileforge authority status --project-id "$PROJECT_ID" | python -m json.tool
agileforge workflow next --project-id "$PROJECT_ID" | python -m json.tool
```

After authority acceptance, the next installed command should be Vision
generation:

```sh
agileforge vision generate --project-id "$PROJECT_ID" > vision-generate.json
```

If the Vision command returns `ok: false`, stop and report the first error code,
message, and details. Provider/runtime failures are hard CLI failures and should
not be hidden.

If the Vision command returns `ok: true` with `data.is_complete: false`, inspect
`data.output_artifact.clarifying_questions`. Answer the actual questions through
`--input`:

```sh
agileforge vision generate \
  --project-id "$PROJECT_ID" \
  --input "Answer the questions here." > vision-refine.json
```

Use `--input` only for real clarification answers or explicit user feedback. Do
not add generic prompting text to make the command run.

If the Vision command returns `ok: true` with `data.is_complete: true`, save it:

```sh
agileforge vision save --project-id "$PROJECT_ID" > vision-save.json
```

The save response includes `data.saved_vision` and `data.vision_fingerprint` so
agents can confirm exactly what became canonical.

Then ask for the next installed command again:

```sh
agileforge workflow next --project-id "$PROJECT_ID" | python -m json.tool
```

Use history only for inspection/debugging:

```sh
agileforge vision history --project-id "$PROJECT_ID" | python -m json.tool
```

After Vision save, the next installed command should be Backlog generation:

```sh
agileforge backlog generate --project-id "$PROJECT_ID" > backlog-generate.json
```

If the Backlog command returns `ok: false`, stop and report the first error code,
message, and details. Backlog runtime and persistence failures are hard CLI
failures and should not be hidden.

If the Backlog command returns `ok: true` with `data.is_complete: false`, inspect
`data.output_artifact.clarifying_questions`. Answer the actual questions through
`--input`:

```sh
agileforge backlog generate \
  --project-id "$PROJECT_ID" \
  --input "Answer the questions here." > backlog-refine.json
```

Use `--input` only for real clarification answers or explicit user feedback. Do
not add generic prompting text to make the command run.

If the Backlog command returns `ok: true` with `data.is_complete: true`, save
that exact reviewed draft. Use the `attempt_id` and `artifact_fingerprint` from
the successful generate/refine response:

```sh
ATTEMPT_ID="$(
  python - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("backlog-generate.json").read_text())
print(payload["data"]["attempt_id"])
PY
)"

ARTIFACT_FINGERPRINT="$(
  python - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("backlog-generate.json").read_text())
print(payload["data"]["artifact_fingerprint"])
PY
)"

agileforge backlog save \
  --project-id "$PROJECT_ID" \
  --attempt-id "$ATTEMPT_ID" \
  --expected-artifact-fingerprint "$ARTIFACT_FINGERPRINT" \
  --expected-state BACKLOG_REVIEW \
  --idempotency-key "save-backlog-$PROJECT_ID-$(date +%Y%m%d%H%M%S)" \
  > backlog-save.json
```

If the saved draft came from a refinement file such as `backlog-refine.json`,
read `attempt_id` and `artifact_fingerprint` from that latest file instead.
Never save an older attempt after a newer refinement.

Backlog save enforces one canonical active backlog. A new reviewed save
supersedes replaceable active `backlog_seed` rows before inserting the new
draft. If any active backlog row has already progressed downstream, save fails
closed with `MUTATION_FAILED`.

For projects affected by older CLI versions that appended multiple active
Backlog seed sets, use the supported reconciliation command. It keeps the latest
saved active seed cohort and supersedes older replaceable seed rows:

```sh
agileforge backlog reconcile \
  --project-id "$PROJECT_ID" \
  --idempotency-key "reconcile-backlog-$PROJECT_ID-$(date +%Y%m%d%H%M%S)" \
  > backlog-reconcile.json
```

If reconciliation returns `ok: false`, stop. It means replacement is unsafe,
usually because an active backlog row was refined, linked to sprint planning, or
moved out of `To Do`.

Then ask for the next installed command again:

```sh
agileforge workflow next --project-id "$PROJECT_ID" | python -m json.tool
```

After Backlog save, the next installed command should be Roadmap generation:

```sh
agileforge roadmap generate --project-id "$PROJECT_ID" > roadmap-generate.json
```

If the Roadmap command returns `ok: false`, stop and report the first error
code, message, and details. Roadmap runtime and persistence failures are hard
CLI failures and should not be hidden.

If the Roadmap command returns `ok: true` with `data.is_complete: false`,
inspect `data.output_artifact.clarifying_questions`. Answer the actual
questions through `--input`:

```sh
agileforge roadmap generate \
  --project-id "$PROJECT_ID" \
  --input "Answer the questions here." > roadmap-refine.json
```

If the Roadmap command returns `ok: true` with `data.is_complete: true`, save
that exact reviewed draft. Use the `attempt_id` and `artifact_fingerprint` from
the latest successful generate/refine response:

```sh
ATTEMPT_ID="$(
  python - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("roadmap-generate.json").read_text())
print(payload["data"]["attempt_id"])
PY
)"

ARTIFACT_FINGERPRINT="$(
  python - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("roadmap-generate.json").read_text())
print(payload["data"]["artifact_fingerprint"])
PY
)"

agileforge roadmap save \
  --project-id "$PROJECT_ID" \
  --attempt-id "$ATTEMPT_ID" \
  --expected-artifact-fingerprint "$ARTIFACT_FINGERPRINT" \
  --expected-state ROADMAP_REVIEW \
  --idempotency-key "save-roadmap-$PROJECT_ID-$(date +%Y%m%d%H%M%S)" \
  > roadmap-save.json
```

Roadmap save enforces exact coverage of the canonical active Backlog. Every
active backlog item must appear exactly once in the saved roadmap releases. If
an item is missing, unknown, or duplicated, save fails closed.

Then ask for the next installed command again:

```sh
agileforge workflow next --project-id "$PROJECT_ID" | python -m json.tool
```

Use history only for inspection/debugging:

```sh
agileforge backlog history --project-id "$PROJECT_ID" | python -m json.tool
agileforge roadmap history --project-id "$PROJECT_ID" | python -m json.tool
```

After Roadmap save, the next installed command should be Story pending:

```sh
agileforge story pending --project-id "$PROJECT_ID" | python -m json.tool
```

Story generation is per Roadmap requirement. Pick an exact pending requirement
from `story pending`, then generate:

```sh
agileforge story generate \
  --project-id "$PROJECT_ID" \
  --parent-requirement "Requirement title from story pending" \
  > story-generate.json
```

Story generate, history, and save payloads are flattened at the CLI envelope
`data` level. Do not read a second nested `data` object under that envelope.

Generate:

- attempt id: `data.current_draft.attempt_id`
- fingerprint: `data.current_draft.artifact_fingerprint`
- draft artifact: `data.output_artifact`
- save guard: `data.save`

Save:

- save result: `data.save_result`
- saved attempt: `data.attempt_id`
- saved fingerprint: `data.artifact_fingerprint`

If the Story command returns `ok: false`, stop and report the first error code,
message, and details. Story runtime and persistence failures are hard CLI
failures and should not be hidden.

If the Story command returns `ok: true` with a retryable runtime failure in
history, retry the same frozen request without inventing feedback:

```sh
agileforge story retry \
  --project-id "$PROJECT_ID" \
  --parent-requirement "Requirement title from story pending" \
  > story-retry.json
```

If the Story command returns `ok: true` with `data.output_artifact.is_complete:
false`, inspect `data.output_artifact.clarifying_questions`. Answer the actual
questions through `--input`:

```sh
agileforge story generate \
  --project-id "$PROJECT_ID" \
  --parent-requirement "Requirement title from story pending" \
  --input "Answer the questions here." \
  > story-refine.json
```

If the Story command returns a complete reviewed draft, save that exact attempt:

```sh
ATTEMPT_ID="$(
  python - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("story-generate.json").read_text())
print(payload["data"]["save"]["attempt_id"])
PY
)"

ARTIFACT_FINGERPRINT="$(
  python - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("story-generate.json").read_text())
print(payload["data"]["save"]["artifact_fingerprint"])
PY
)"

agileforge story save \
  --project-id "$PROJECT_ID" \
  --parent-requirement "Requirement title from story pending" \
  --attempt-id "$ATTEMPT_ID" \
  --expected-artifact-fingerprint "$ARTIFACT_FINGERPRINT" \
  --expected-state STORY_REVIEW \
  --idempotency-key "save-story-$PROJECT_ID-$(date +%Y%m%d%H%M%S)" \
  > story-save.json
```

If the saved draft came from `story-refine.json` or `story-retry.json`, read the
attempt id and artifact fingerprint from that latest reviewed file instead.
Never save an older attempt after a newer refinement.

Repeat `story pending`, `story generate`, review, and guarded `story save` until
every Roadmap requirement is saved or explicitly merged by Story resolution
state. Then complete the Story phase:

```sh
agileforge story complete \
  --project-id "$PROJECT_ID" \
  --expected-state STORY_PERSISTENCE \
  --idempotency-key "complete-story-$PROJECT_ID-$(date +%Y%m%d%H%M%S)" \
  > story-complete.json
```

Story complete fails closed unless all Roadmap requirements are covered. On
success, it moves the workflow to Sprint setup:

```sh
agileforge workflow next --project-id "$PROJECT_ID" | python -m json.tool
agileforge sprint candidates --project-id "$PROJECT_ID" | python -m json.tool
```

Story save persists Sprint planning metadata:

| `estimated_effort` | `story_points` |
| --- | ---: |
| `XS` | 1 |
| `S` | 2 |
| `M` | 3 |
| `L` | 5 |
| `XL` | 8 |

Refined child story rank is derived from Roadmap parent order plus child slot.
For example, the first child of the second Roadmap item receives rank `201`.

### Reviewing Story Dependencies

Story generation can propose prerequisite edges, but they are not active truth
until reviewed and applied. Inspect first:

```sh
agileforge story dependencies inspect \
  --project-id "$PROJECT_ID" \
  > story-dependencies-inspect.json
```

Create a guarded dependency review artifact from proposed edges plus
deterministic same-requirement slot hints:

```sh
agileforge story dependencies propose \
  --project-id "$PROJECT_ID" \
  --expected-state SPRINT_SETUP \
  --idempotency-key "dep-propose-$PROJECT_ID-$(date +%Y%m%d%H%M%S)" \
  > story-dependencies-propose.json
```

Review `data.edges`, `data.cycle_count`, and `data.artifact_fingerprint`. Apply
only the exact reviewed attempt:

```sh
ATTEMPT_ID="$(python - <<'PY'
import json
print(json.load(open("story-dependencies-propose.json"))["data"]["attempt_id"])
PY
)"
ARTIFACT_FINGERPRINT="$(python - <<'PY'
import json
print(json.load(open("story-dependencies-propose.json"))["data"]["artifact_fingerprint"])
PY
)"

agileforge story dependencies apply \
  --project-id "$PROJECT_ID" \
  --attempt-id "$ATTEMPT_ID" \
  --expected-artifact-fingerprint "$ARTIFACT_FINGERPRINT" \
  --expected-state SPRINT_SETUP \
  --idempotency-key "dep-apply-$PROJECT_ID-$(date +%Y%m%d%H%M%S)" \
  > story-dependencies-apply.json
```

`inspect`, `propose`, and `apply` are allowed in `STORY_PERSISTENCE`,
`SPRINT_SETUP`, and `SPRINT_DRAFT` so a bad graph cannot deadlock Sprint
planning. Active cycles still block Sprint candidate readiness until resolved.

### Repairing Story Readiness Before Sprint

Use this when refined stories were saved before AgileForge persisted
`story_points` and rank.

```sh
agileforge story repair-readiness \
  --project-id "$PROJECT_ID" \
  --expected-state SPRINT_SETUP \
  --idempotency-key "repair-story-readiness-$PROJECT_ID-$(date +%Y%m%d%H%M%S)" \
  > story-repair-readiness.json
```

This command only backfills `story_points` and `rank`. It does not rewrite story
title, description, acceptance criteria, status, validation evidence, or
workflow phase. It fails closed if Sprint work has already started for any
active refined story.

### Correcting a Saved Story Before Sprint

Use this only when Story is complete but Sprint work has not started.

```sh
agileforge story reopen \
  --project-id 2 \
  --parent-requirement "Live Pre-Lock Recommendation Workflow with Risk-Audited Artifact" \
  --expected-state SPRINT_SETUP \
  --idempotency-key reopen-story-2-live-budget-001
```

Then regenerate with explicit feedback:

```sh
agileforge story generate \
  --project-id 2 \
  --parent-requirement "Live Pre-Lock Recommendation Workflow with Risk-Audited Artifact" \
  --input "Correct the budget contract: accepted spec REQ.budget-bound says missing available budget must require an explicit operator-provided budget. Do not preserve a default budget fallback for live recommendation." \
  > story-generate-corrected-live-budget.json
```

Review `data.output_artifact`, then save with the returned
`data.save.attempt_id` and `data.save.artifact_fingerprint`.

## Sprint Phase

After Story completion, inspect candidate readiness before invoking the model:

```sh
agileforge sprint candidates --project-id "$PROJECT_ID" | python -m json.tool
```

Do not generate a Sprint while `data.readiness.status` is `blocked`. Resolve the
reported blockers first; Sprint generation fails closed when candidates are
unsized, still carry default priority, or contain active dependency graph
errors such as `STORY_DEPENDENCY_CYCLE`.

Generate a Sprint draft from the reviewed candidate set:

```sh
agileforge sprint generate \
  --project-id "$PROJECT_ID" \
  --selected-story-ids 66,85 \
  --team-velocity-assumption Medium \
  --sprint-duration-days 14 \
  > sprint-generate.json
```

Use `--selected-story-ids` only when intentionally constraining scope; otherwise
omit it and let AgileForge lock a deterministic cohort from all ready
candidates. Use `--input` for real refinement feedback only.

#### Sprint Selection Contract

`agileforge sprint generate` does not ask the model to choose arbitrary Sprint
scope. AgileForge first locks a deterministic story cohort from planning-ready
candidates:

- Default mode selects a dependency-closed cohort, bounded by
  `--max-story-points` when provided and the velocity story-count band. If a
  selected candidate has an active prerequisite that is also a candidate,
  AgileForge includes that prerequisite first.
- Manual mode requires a dependency-closed `--selected-story-ids` list. If a
  selected story requires another active candidate story and that prerequisite
  is omitted, Sprint generation fails with
  `SPRINT_SELECTION_DEPENDENCY_MISSING`. If all required stories are present
  but out of order, AgileForge reorders them into dependency-safe order and
  reports `SPRINT_SELECTION_MANUAL_REORDERED`.

### Dependency-Aware Sprint Selection

Default generation:

```sh
agileforge sprint generate --project-id "$PROJECT_ID"
```

Manual override:

```sh
agileforge sprint generate \
  --project-id "$PROJECT_ID" \
  --selected-story-ids <prerequisite_id>,<dependent_id>
```

The Sprint Planner receives only the locked cohort. Its job is to write the
Sprint Goal, explain cohesion, and decompose the selected stories into tasks. If
the model adds, drops, or changes selected story IDs, AgileForge fails the run
with `MUTATION_FAILED`.

If generation returns `ok: false`, stop and report the first error code, message,
and details. Sprint runtime failures are hard CLI failures and should not be
hidden.

### Sprint Draft Freshness

Sprint drafts are saveable only when they are the latest complete Sprint attempt
and were generated from the current Story/dependency candidate source. If a
Story is saved/reopened or dependency edges are applied, AgileForge clears the
unsaved Sprint draft and returns to Sprint setup. If Sprint regeneration fails,
older complete attempts remain visible in history for audit but cannot be saved.

Use `agileforge workflow next --project-id "$PROJECT_ID"` before saving. If it
does not advertise `agileforge sprint save`, regenerate Sprint first:

```sh
agileforge sprint generate \
  --project-id "$PROJECT_ID" \
  --input "Regenerate after current Story/dependency changes. Use locked deterministic cohort."
```

When generation returns a complete reviewed draft, save that exact attempt:

```sh
ATTEMPT_ID="$(
  python - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("sprint-generate.json").read_text())
print(payload["data"]["attempt_id"])
PY
)"

ARTIFACT_FINGERPRINT="$(
  python - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("sprint-generate.json").read_text())
print(payload["data"]["artifact_fingerprint"])
PY
)"

agileforge sprint save \
  --project-id "$PROJECT_ID" \
  --team-name "Delivery" \
  --sprint-start-date "2026-05-25" \
  --attempt-id "$ATTEMPT_ID" \
  --expected-artifact-fingerprint "$ARTIFACT_FINGERPRINT" \
  --expected-state SPRINT_DRAFT \
  --idempotency-key "save-sprint-$PROJECT_ID-$(date +%Y%m%d%H%M%S)" \
  > sprint-save.json
```

If the saved draft came from a refinement file, read `attempt_id` and
`artifact_fingerprint` from that latest reviewed file instead. Never save an
older attempt after a newer refinement.

### Sprint Execution Start

After save, start the reviewed Sprint through a guarded mutation:

```sh
agileforge sprint start \
  --project-id "$PROJECT_ID" \
  --expected-state SPRINT_PERSISTENCE \
  --idempotency-key "start-sprint-$PROJECT_ID-$(date +%Y%m%d%H%M%S)" \
  > sprint-start.json
```

`--sprint-id` is optional. If omitted, AgileForge resolves the current planned
Sprint for the project. On success the persisted Sprint becomes `Active`, the
workbench FSM moves to `SPRINT_VIEW`, and repeated calls with the same
idempotency key replay the same payload.

Inspect execution status and task rows:

```sh
agileforge sprint status --project-id "$PROJECT_ID" | python -m json.tool
agileforge sprint tasks --project-id "$PROJECT_ID" | python -m json.tool
```

`sprint tasks` is dependency-aware. The task rows preserve the existing fields
and add read-only execution metadata:

- `task_execution_order`
- `story_execution_order`
- `direct_blocked_by_story_ids`
- `blocked_by_story_ids`
- `unblocks_story_ids`
- `is_blocked`
- `dependency_order_source`

Use the row order and `story_execution_order` as the implementation order.
Do not start a task whose `is_blocked` is `true`. `blocked_by_story_ids` is
transitive and is cleared only when every prerequisite story is `Done`,
including prerequisites completed in earlier sprints.

These fields reflect the active `story dependencies` graph. They do not infer
hidden prerequisites from prose. If the task order contradicts the reviewed
Sprint intent, inspect and repair story dependencies before directing an
implementation agent.

The response also includes `dependency_summary` with `active_edge_count`,
`cycle_count`, `blocked_story_count`, and `ordering`. If active dependencies
contain a cycle after the sprint has started, `sprint tasks` still returns
`ok: true`, emits `SPRINT_TASK_DEPENDENCY_CYCLE_FALLBACK` in `warnings`, and
uses rank fallback order so execution views remain recoverable.

`workflow next` in `SPRINT_VIEW` should advertise `sprint task next`,
`sprint status`, `sprint tasks`, `sprint task show`, `sprint task update`, and
`sprint history`.

### Sprint Task Tickets

For agent execution, use the task-ticket commands instead of copying UI prompts.
The ticket is the work contract for one task: task metadata, parent story,
checklist, artifact targets, dependency blockers, history summary, and guard
values.

Get the next recommended task:

```sh
agileforge sprint task next --project-id "$PROJECT_ID" > task-next.json
```

If a task is already `In Progress`, `task next` returns that task before
offering new `To Do` work. Otherwise it returns the first unblocked `To Do` task
by dependency-aware execution order. If no work is available, `data.task_ticket`
is `null` and `data.reason` explains why.

Show a specific ticket or history:

```sh
agileforge sprint task show \
  --project-id "$PROJECT_ID" \
  --task-id "$TASK_ID" > task-show.json

agileforge sprint task history \
  --project-id "$PROJECT_ID" \
  --task-id "$TASK_ID" | python -m json.tool
```

Start work by moving the task to `In Progress` with the guard values from the
latest ticket:

```sh
EXPECTED_STATUS="$(
  python - <<'PY'
import json
from pathlib import Path
payload = json.loads(Path("task-next.json").read_text())
print(payload["data"]["task_ticket"]["guards"]["expected_status"])
PY
)"

TASK_FINGERPRINT="$(
  python - <<'PY'
import json
from pathlib import Path
payload = json.loads(Path("task-next.json").read_text())
print(payload["data"]["task_ticket"]["guards"]["expected_task_fingerprint"])
PY
)"

TASK_ID="$(
  python - <<'PY'
import json
from pathlib import Path
payload = json.loads(Path("task-next.json").read_text())
print(payload["data"]["task_ticket"]["task"]["task_id"])
PY
)"

agileforge sprint task update \
  --project-id "$PROJECT_ID" \
  --task-id "$TASK_ID" \
  --status "In Progress" \
  --expected-status "$EXPECTED_STATUS" \
  --expected-task-fingerprint "$TASK_FINGERPRINT" \
  --idempotency-key "task-$TASK_ID-start-$(date +%Y%m%d%H%M%S)" \
  --notes "Starting work." > task-start.json
```

Mark a task `Done` only after the work is actually implemented and verified.
Completion requires evidence:

```sh
agileforge sprint task update \
  --project-id "$PROJECT_ID" \
  --task-id "$TASK_ID" \
  --status Done \
  --expected-status "In Progress" \
  --expected-task-fingerprint "$TASK_FINGERPRINT" \
  --idempotency-key "task-$TASK_ID-done-$(date +%Y%m%d%H%M%S)" \
  --outcome-summary "Implemented the required behavior." \
  --checklist-result fully_met \
  --artifact-ref path/to/changed_file.py \
  --validation-summary "uv run --frozen pytest path/to/test.py -q"
```

If the task has artifact targets, `Done` requires at least one `--artifact-ref`.
`Done` also requires `--validation-summary`, `--outcome-summary`, and
`--checklist-result fully_met` or `partially_met`.

Task update safety rules:

- `--idempotency-key`, `--expected-status`, and
  `--expected-task-fingerprint` are required.
- Repeating the same command with the same idempotency key replays the stored
  response. Reusing the key with a different command body fails.
- Updating a blocked task to `In Progress` or `Done` fails closed.
- Stale status or stale fingerprint fails closed. Refresh the ticket and retry.
- `Done` and `Cancelled` are terminal for this phase.

## JSON Envelope Contract

Every command returns one JSON envelope on stdout.

Success shape:

```json
{
  "ok": true,
  "data": {},
  "warnings": [],
  "errors": [],
  "meta": {
    "schema_version": "agileforge.cli.v1",
    "command": "agileforge project list",
    "command_version": "1",
    "agileforge_version": "0.1.0",
    "storage_schema_version": "2",
    "generated_at": "2026-05-16T17:20:12Z",
    "correlation_id": "69767371-fd30-4bf3-861e-a83e9127d5e7"
  }
}
```

Failure shape:

```json
{
  "ok": false,
  "data": null,
  "warnings": [],
  "errors": [
    {
      "code": "SPEC_FILE_NOT_FOUND",
      "message": "The requested spec file was not found.",
      "details": {
        "spec_file": "specs/spec.json"
      },
      "remediation": [
        "Create the spec file or pass the correct caller-relative path."
      ],
      "exit_code": 2,
      "retryable": false
    }
  ],
  "meta": {
    "schema_version": "agileforge.cli.v1",
    "command": "agileforge project create",
    "command_version": "1",
    "agileforge_version": "0.1.0",
    "storage_schema_version": "2",
    "generated_at": "2026-05-16T17:20:12Z",
    "correlation_id": "69767371-fd30-4bf3-861e-a83e9127d5e7"
  }
}
```

Agent rules:

- Always parse stdout as JSON.
- Treat `ok` as the primary success indicator.
- On `ok: false`, read the first error code and remediation.
- Do not assume `data` is present when `ok` is false. Some recovery errors
  include useful `data`, but not all errors do.
- Preserve `meta.correlation_id` in logs.
- Treat `meta.command_version` and `meta.schema_version` as compatibility
  inputs for agent skills.
- Do not parse stderr for command results. Logging should not be part of the
  data contract.

Python parser pattern:

```sh
payload="$(agileforge status --project-id 1)"
PAYLOAD="$payload" python - <<'PY'
import json
import os
import sys

envelope = json.loads(os.environ["PAYLOAD"])
if not envelope["ok"]:
    error = envelope["errors"][0]
    print(error["code"], file=sys.stderr)
    print(error.get("remediation", []), file=sys.stderr)
    raise SystemExit(error.get("exit_code", 1))

print(envelope["data"])
PY
```

## Command Discovery

Start with capabilities:

```sh
agileforge capabilities
```

This returns installed command metadata:

- `name`
- `phase`
- `mutates`
- `stable`
- `destructive`
- accepted guard fields
- idempotency policy
- required and optional inputs
- possible error codes

Inspect one command contract:

```sh
agileforge command schema "agileforge project create"
```

Use `command schema` before writing an agent skill workflow. It gives the
machine-readable command contract, including:

- required input fields
- optional input fields
- whether the command mutates state
- whether idempotency is required
- guard policy
- documented error codes
- exit codes
- envelope schema

List available command names:

```sh
agileforge capabilities | python -c 'import json,sys; p=json.load(sys.stdin); print("\n".join(c["name"] for c in p["data"]["commands"]))'
```

## Installed Command Reference

### Operational Commands

```sh
agileforge doctor
agileforge schema check
agileforge capabilities
agileforge command schema "agileforge project create"
```

Use these before and during agent workflows.

### Project Commands

```sh
agileforge project list
agileforge project show --project-id 1
agileforge project create --name "Project" --spec-file specs/spec.json --idempotency-key create-project-001
agileforge project create --dry-run --dry-run-id preview-project-001 --name "Project" --spec-file specs/spec.json
agileforge project setup retry --project-id 1 --spec-file specs/spec.json --expected-state SETUP_REQUIRED --expected-context-fingerprint sha256:... --idempotency-key setup-retry-001
agileforge project setup retry --project-id 1 --spec-file specs/spec.json --expected-state SETUP_REQUIRED --expected-context-fingerprint sha256:... --recovery-mutation-event-id 10 --idempotency-key recovery-retry-001
```

`project create` and `project setup retry` mutate state.

### Workflow Commands

```sh
agileforge workflow state --project-id 1
agileforge workflow next --project-id 1
```

Use `workflow state` for current FSM/session state and `workflow next` for
installed next commands.

### Authority Commands

```sh
agileforge authority status --project-id 1
agileforge authority review --project-id 1
agileforge authority review --project-id 1 --include-spec full
agileforge authority accept --project-id 1
agileforge authority reject --project-id 1 --review-token <review_token> --reason "..." --idempotency-key reject-001
agileforge authority invariants --project-id 1
agileforge authority invariants --project-id 1 --spec-version-id 3
```

Use `authority review` before any decision. The normal accept path uses the
reviewed pending authority for the project and does not require agents to pass
review tokens or idempotency keys in the command text.

### Vision Commands

```sh
agileforge vision generate --project-id 1
agileforge vision generate --project-id 1 --input "answers or review feedback"
agileforge vision history --project-id 1
agileforge vision save --project-id 1
```

Use `vision generate` without `--input` for the initial Vision run. Use
`--input` only after a prior draft asks clarifying questions or the user gives
explicit refinement feedback. `vision save` is valid only after the current
Vision draft is complete.

### Backlog Commands

```sh
agileforge backlog generate --project-id 1
agileforge backlog generate --project-id 1 --input "answers or review feedback"
agileforge backlog history --project-id 1
agileforge backlog save --project-id 1 --attempt-id <attempt_id> --expected-artifact-fingerprint <fingerprint> --expected-state BACKLOG_REVIEW --idempotency-key save-backlog-001
agileforge backlog reconcile --project-id 1 --idempotency-key reconcile-backlog-001
```

Use `backlog generate` without `--input` for the initial Backlog run. Save only
the reviewed current draft, using its returned attempt id and artifact
fingerprint.

### Roadmap Commands

```sh
agileforge roadmap generate --project-id 1
agileforge roadmap generate --project-id 1 --input "answers or review feedback"
agileforge roadmap history --project-id 1
agileforge roadmap save --project-id 1 --attempt-id <attempt_id> --expected-artifact-fingerprint <fingerprint> --expected-state ROADMAP_REVIEW --idempotency-key save-roadmap-001
```

Use `roadmap generate` after Backlog persistence. Save only the reviewed current
draft. Roadmap save is guarded and must exactly cover the active backlog.

### Story Commands

```sh
agileforge story show --story-id 42
agileforge story pending --project-id 1
agileforge story generate --project-id 1 --parent-requirement "Roadmap requirement"
agileforge story generate --project-id 1 --parent-requirement "Roadmap requirement" --input "answers or review feedback"
agileforge story retry --project-id 1 --parent-requirement "Roadmap requirement"
agileforge story history --project-id 1 --parent-requirement "Roadmap requirement"
agileforge story save --project-id 1 --parent-requirement "Roadmap requirement" --attempt-id <attempt_id> --expected-artifact-fingerprint <fingerprint> --expected-state STORY_REVIEW --idempotency-key save-story-001
agileforge story complete --project-id 1 --expected-state STORY_PERSISTENCE --idempotency-key complete-story-001
agileforge story reopen --project-id 1 --parent-requirement "Roadmap requirement" --expected-state SPRINT_SETUP --idempotency-key reopen-story-001
agileforge story repair-readiness --project-id 1 --expected-state SPRINT_SETUP --idempotency-key repair-story-readiness-001
agileforge story dependencies inspect --project-id 1
agileforge story dependencies propose --project-id 1 --expected-state SPRINT_SETUP --idempotency-key dep-propose-001
agileforge story dependencies apply --project-id 1 --attempt-id <attempt_id> --expected-artifact-fingerprint <fingerprint> --expected-state SPRINT_SETUP --idempotency-key dep-apply-001
```

`story show`, `story pending`, `story history`, and `story dependencies inspect`
are read-only. `story generate`, `story retry`, `story save`, `story complete`,
`story reopen`, `story repair-readiness`, `story dependencies propose`, and
`story dependencies apply` mutate workflow state. Save, complete, reopen,
repair-readiness, dependency propose, and dependency apply are guarded and
require explicit idempotency keys.

### Sprint Commands

```sh
agileforge sprint candidates --project-id 1
agileforge sprint generate --project-id 1
agileforge sprint history --project-id 1
agileforge sprint save --project-id 1 --team-name Delivery --sprint-start-date 2026-05-25 --attempt-id <attempt_id> --expected-artifact-fingerprint <fingerprint> --expected-state SPRINT_DRAFT --idempotency-key save-sprint-001
agileforge sprint start --project-id 1 --expected-state SPRINT_PERSISTENCE --idempotency-key start-sprint-001
agileforge sprint status --project-id 1
agileforge sprint tasks --project-id 1
agileforge sprint task next --project-id 1
agileforge sprint task show --project-id 1 --task-id 42
agileforge sprint task history --project-id 1 --task-id 42
agileforge sprint task update --project-id 1 --task-id 42 --status "In Progress" --expected-status "To Do" --expected-task-fingerprint <fingerprint> --idempotency-key task-42-start-001 --notes "Starting work."
```

`candidates`, `history`, `status`, `tasks`, `task next`, `task show`, and
`task history` are read-only. `generate`, `save`, `start`, and `task update`
mutate state. `save`, `start`, and `task update` require idempotency keys.

### Context Commands

```sh
agileforge context pack --project-id 1 --phase overview
agileforge context pack --project-id 1 --phase sprint-planning
```

Use context packs to get bounded agent context and guard tokens.

### Status Command

```sh
agileforge status --project-id 1
```

Use this for quick project orientation.

### Mutation Ledger Commands

```sh
agileforge mutation list
agileforge mutation list --project-id 1
agileforge mutation list --project-id 1 --status recovery_required
agileforge mutation show --mutation-event-id 10
agileforge mutation resume --mutation-event-id 10
```

`mutation show` and `mutation list` are read-only. `mutation resume` is a
mutating operational command that acquires a recovery lease on a
recovery-required mutation. At the current phase, domain-specific project setup
repair should normally use `project setup retry`; use `mutation resume` only
when the returned remediation tells you to inspect or acquire recovery.

## Idempotency Keys

Domain mutations require `--idempotency-key` for non-dry-run execution.

Installed domain mutations:

- `agileforge project create`
- `agileforge project setup retry`
- `agileforge backlog save`
- `agileforge backlog reconcile`
- `agileforge roadmap save`
- `agileforge story save`
- `agileforge story complete`

For `project create` and `project setup retry`, the parser enforces:

- non-dry-run mutations require `--idempotency-key`
- dry-runs forbid `--idempotency-key`
- dry-runs require `--dry-run-id`

Token rules for `idempotency_key` and `dry_run_id`:

- ASCII only
- 8 to 128 characters
- allowed characters: `A-Z`, `a-z`, `0-9`, `.`, `_`, `:`, `-`

Good keys:

```text
create-cartola-20260516-001
setup-retry-project-12-001
agent:project-create:cartola:001
```

Bad keys:

```text
short
contains spaces
contains/slashes
contains-non-ascii
```

Idempotency behavior:

- Same command, same key, same canonical request: replay the stored response.
- Same command, same key, different canonical request: return
  `IDEMPOTENCY_KEY_REUSED`.
- Different command can use its own key namespace, but agent skills should still
  make keys globally descriptive.
- If the agent times out after submitting a mutation, retry the exact same
  command with the same idempotency key before creating a new attempt.

The canonical request hash includes normalized inputs such as resolved spec path,
spec hash, stale guards, recovery link, and `changed_by` where relevant. It does
not include `correlation_id`.

`agileforge mutation resume` is also mutating, but it is an operational recovery
command over an existing ledger row. It does not accept an idempotency key
because the mutation event id is the identity of the recovery target.

## Correlation IDs and Changed By

Mutating commands accept:

```sh
--correlation-id CORRELATION_ID
--changed-by CHANGED_BY
```

Use `--correlation-id` to connect logs across a larger agent run. If omitted,
the CLI generates one.

Use `--changed-by` to identify the actor in the mutation ledger. If omitted,
the default is:

```text
cli-agent
```

Recommended agent values:

```text
codex
claude
cursor-agent
ci-agent
```

Keep `changed_by` stable for a run. Changing `changed_by` on an idempotent retry
can change the canonical request hash for some commands and trigger
`IDEMPOTENCY_KEY_REUSED`.

## Dry-Run Semantics

Dry-runs validate inputs and preview deterministic command behavior without
creating mutation ledger rows and without writing domain state.

For `project create`:

```sh
agileforge project create \
  --dry-run \
  --dry-run-id preview-cartola-001 \
  --name "caRtola" \
  --spec-file specs/spec.json
```

Expected success data includes:

- `preview_available: true`
- `name`
- `resolved_spec_path`

For `project setup retry`:

```sh
agileforge project setup retry \
  --dry-run \
  --dry-run-id preview-setup-retry-001 \
  --project-id 1 \
  --spec-file specs/spec.json \
  --expected-state SETUP_REQUIRED \
  --expected-context-fingerprint sha256:... \
  --recovery-mutation-event-id 10
```

Dry-run rules:

- Do not pass `--idempotency-key` with `--dry-run`.
- Always pass `--dry-run-id`.
- Do not treat dry-run success as proof the real command will succeed later.
  State may change between preview and execution.
- A dry-run does not consume an idempotency key.
- A dry-run does not acquire recovery leases.
- A dry-run does not update existing recovery ledger rows.

## Creating a Project

Project creation is the first installed canonical mutation flow.

It does all of the following:

1. Resolves the spec file relative to the caller current working directory.
2. Validates the spec file exists and is readable.
3. Creates a `Product`.
4. Persists a `SpecRegistry` version.
5. Compiles a pending `CompiledSpecAuthority`.
6. Initializes or reconciles workflow session setup state.
7. Finalizes the mutation ledger response.

It does not create a `SpecAuthorityAcceptance` row.

### Recommended Agent Flow

From the caller project:

```sh
cd /path/to/caller-project
test -f specs/spec.json
```

Preview:

```sh
agileforge project create \
  --dry-run \
  --dry-run-id preview-cartola-001 \
  --name "caRtola" \
  --spec-file specs/spec.json
```

Execute:

```sh
agileforge project create \
  --name "caRtola" \
  --spec-file specs/spec.json \
  --idempotency-key create-cartola-20260516-001 \
  --changed-by codex
```

Parse project id:

```sh
payload="$(agileforge project create \
  --name "caRtola" \
  --spec-file specs/spec.json \
  --idempotency-key create-cartola-20260516-001 \
  --changed-by codex)"

PROJECT_ID="$(
  PAYLOAD="$payload" python -c 'import json,os; p=json.loads(os.environ["PAYLOAD"]); print(p["data"]["project_id"] if p["ok"] else "")'
)"
```

Inspect status:

```sh
agileforge status --project-id "$PROJECT_ID"
agileforge workflow state --project-id "$PROJECT_ID"
agileforge authority status --project-id "$PROJECT_ID"
```

Project-create success data uses the same authority naming policy as
`authority status`:

- `authority_id` is the accepted authority id and remains `null` after project
  creation.
- `pending_authority_id` is the compiled authority awaiting review.
- `compiled_authority_id` is an alias for the compiled pending authority created
  by setup.
- `pending_compiled_spec_version_id` is the spec version used to compile the
  pending authority.

Expected authority state immediately after successful project creation:

```json
{
  "status": "pending_acceptance",
  "authority_id": null,
  "pending_authority_id": 3,
  "pending_compiled_spec_version_id": 3,
  "pending_authority_fingerprint": "sha256:..."
}
```

Agent stop rule:

- If authority is pending, stop and report the project id and pending authority
  details before moving to Vision or backlog work.
- Do not treat pending authority as accepted. Retrieve the review packet, ask
  for review, and record an explicit accept or reject decision.
- Do not use direct SQLite edits or HTTP calls to accept authority.

## Authority Review And Decision

Pending authority is a manual checkpoint. A project created from the CLI should
initially show `status: pending_acceptance`, `authority_id: null`, and a
populated `pending_authority_id`. It is usable for review, but it is not
canonical until accepted.

### Structured Spec Authority Flow

Authority compilation accepts only `agileforge.spec.v1` JSON.

Use:

```bash
agileforge project create \
  --name "Project Name" \
  --spec-file specs/spec.json \
  --idempotency-key create-project-001
agileforge authority review --project-id <project_id> --open
agileforge authority accept --project-id <project_id>
```

Markdown specs are render views or source material for a separate
structured-spec generation step. They are not accepted by `project create`.

Host validation checks schema, freshness, compiled artifact shape, and
source-reference item IDs. It does not block acceptance based on generated
requirement candidates or inferred semantic coverage.

Detect pending review with all three projections:

```sh
agileforge status --project-id "$PROJECT_ID"
agileforge authority status --project-id "$PROJECT_ID"
agileforge workflow next --project-id "$PROJECT_ID"
```

`workflow next` should advertise:

```text
agileforge authority review --project-id <id> --open
```

Important `authority status` fields:

- `status`: `pending_acceptance`, `current`, `stale`, or another high-level
  authority state.
- `authority_id`: accepted/current authority id only; it is `null` while review
  is pending.
- `pending_authority_id`: compiled authority awaiting review.
- `accepted_decision_id`: accepted decision row if authority is current.
- `pending_compiled_spec_version_id`: spec version used for pending authority.
- `pending_authority_fingerprint`: fingerprint for the pending compiled
  authority.
- `authority_fingerprint`: projection fingerprint for the reported status.
- `disk_spec`: resolved disk spec path and hash information.

Retrieve the review packet:

```sh
agileforge authority review --project-id "$PROJECT_ID" --open > review.json
python -m json.tool review.json >/dev/null
```

Important review packet fields:

- `pending_authority.ir_provenance`: `not_applicable` for structured
  AgileForge specs. Candidate IR is not part of the acceptance gate.
- `review_summary`: compact decision status for agents and UI.
- `pending_authority.ir_packet_limits`: render limits and `truncated` status.

Agent review rule:

- Ask an AI reviewer this exact question, using the review packet as evidence:

```text
Does this compiled interpretation correctly represent the spec?
```

- The reviewer should compare source requirements, `pending_authority.artifact`,
  `spec.source_outline`, source-map evidence, gaps, assumptions, and rejected
  features.
- Stop on uncertainty. Do not accept just because the packet exists.
- If host structural validation returns `AUTHORITY_REVIEW_INCOMPLETE`, fix the
  source or compiler output and rerun review before deciding.

Accept after a positive review:

```sh
agileforge authority accept \
  --project-id "$PROJECT_ID" > accept.json

python -m json.tool accept.json >/dev/null
```

Reject when the compiled authority is wrong or unreviewable:

```sh
review_token="$(
  python -c 'import json; print(json.load(open("review.json"))["data"]["guard_tokens"]["review_token"])'
)"

agileforge authority reject \
  --project-id "$PROJECT_ID" \
  --review-token "$review_token" \
  --idempotency-key "authority-reject-$PROJECT_ID-001" \
  --reason "Compiled authority omits the dashboard requirement." > reject.json

python -m json.tool reject.json >/dev/null
```

If the source spec is missing or unreadable at decision time, accept/reject
fails closed before writing a decision row. `AUTHORITY_SOURCE_UNAVAILABLE`
means the reviewed source cannot be reloaded; restore the spec file or rerun
review from a readable source before deciding.

Expected outcomes:

- After accept, `authority status` returns `ok: true`, `status: current`,
  non-null `authority_id`, and `pending_authority_id: null`. `workflow next`
  should no longer advertise `agileforge authority review --project-id` for the
  same project.
- After reject, authority remains non-canonical. Vision remains locked and the
  next action is to update or recompile the spec in a later workflow slice.

Dashboard behavior mirrors the CLI service. Pending projects render as
`Pending Authority Review`, fetch the same review packet, and submit decisions
through the same guarded service path. The dashboard must not accept
fingerprint-only mutations; stale pages should refresh after source or workflow
guards change.

Read invariants only after acceptance:

```sh
agileforge authority invariants --project-id "$PROJECT_ID"
```

If no authority is accepted, invariant output may return an authority-related
error. Agents should surface that status rather than forcing progress.

## Guard Tokens

Guard tokens prevent agents from mutating stale state.

Installed guard-bearing mutation commands include:

```sh
agileforge project setup retry
agileforge authority accept
agileforge authority reject
```

`project setup retry` required guards:

- `--expected-state`
- `--expected-context-fingerprint`

Authority decision commands use the reviewed pending authority for the project.
Explicit guard mode remains available for non-interactive integrations that
store the full `guard_tokens` tuple, but it is not the recommended CLI path.

Get current state:

```sh
agileforge workflow state --project-id "$PROJECT_ID"
```

Get context fingerprint:

```sh
agileforge context pack --project-id "$PROJECT_ID" --phase overview
```

Extract `expected_context_fingerprint`:

```sh
CTX="$(
  agileforge context pack --project-id "$PROJECT_ID" --phase overview |
  python -c 'import json,sys; print(json.load(sys.stdin)["data"]["guard_tokens"]["expected_context_fingerprint"])'
)"
```

If a guarded command returns `STALE_STATE` or `STALE_CONTEXT_FINGERPRINT`, do not
retry blindly. Refresh `workflow state` and `context pack`, review the new
state, and then issue a new command with a new idempotency key.

## Mutation Ledger

The mutation ledger is the source of truth for mutation attempts and recovery.

List all mutation events:

```sh
agileforge mutation list
```

List project-specific events:

```sh
agileforge mutation list --project-id "$PROJECT_ID"
```

List recovery-required events:

```sh
agileforge mutation list --project-id "$PROJECT_ID" --status recovery_required
```

Show one event:

```sh
agileforge mutation show --mutation-event-id "$MUTATION_EVENT_ID"
```

Common statuses:

- `pending`: a command owns an active lease or may be stale-pending.
- `succeeded`: mutation finalized successfully.
- `guard_rejected`: stale guard or similar precondition blocked mutation.
- `domain_failed_no_side_effects`: request failed before domain writes started.
- `recovery_required`: some durable side effect may need reconciliation.
- `superseded`: an original recovery row was superseded by a linked retry row.

Agent rules:

- If a command returns `MUTATION_IN_PROGRESS`, wait briefly and inspect the
  mutation event before retrying.
- If a command returns `MUTATION_RECOVERY_REQUIRED`, inspect the mutation event
  and follow `data.next_actions` or `errors[0].remediation`.
- If a command returns `IDEMPOTENCY_KEY_REUSED`, do not keep retrying. Generate
  a new idempotency key only after reviewing why the request differs.
- If a command returns `MUTATION_RESUME_CONFLICT`, another worker may have won a
  recovery lease. Re-read the mutation event.

## Project Setup Retry

Use setup retry when `project create` created the project but setup did not reach
pending authority review.

For ordinary compiler/setup failures such as `SPEC_COMPILE_FAILED`, retry does
not need a recovery event id:

```sh
agileforge project setup retry \
  --project-id "$PROJECT_ID" \
  --spec-file specs/spec.json \
  --expected-state SETUP_REQUIRED \
  --expected-context-fingerprint "$CTX" \
  --idempotency-key "setup-retry-$PROJECT_ID-001" \
  --changed-by codex
```

When `project create` returns `MUTATION_RECOVERY_REQUIRED`, the retry command
must link to the original recovery event:

```sh
agileforge project setup retry \
  --project-id "$PROJECT_ID" \
  --spec-file specs/spec.json \
  --expected-state SETUP_REQUIRED \
  --expected-context-fingerprint "$CTX" \
  --recovery-mutation-event-id "$RECOVERY_EVENT_ID" \
  --idempotency-key "setup-retry-$PROJECT_ID-001" \
  --changed-by codex
```

Recommended recovery sequence for `MUTATION_RECOVERY_REQUIRED`:

1. Read the original mutation:

   ```sh
   agileforge mutation show --mutation-event-id "$RECOVERY_EVENT_ID"
   ```

2. Confirm project state:

   ```sh
   agileforge workflow state --project-id "$PROJECT_ID"
   agileforge authority status --project-id "$PROJECT_ID"
   ```

3. Get a fresh context fingerprint:

   ```sh
   CTX="$(
     agileforge context pack --project-id "$PROJECT_ID" --phase overview |
     python -c 'import json,sys; print(json.load(sys.stdin)["data"]["guard_tokens"]["expected_context_fingerprint"])'
   )"
   ```

4. Run retry with a new idempotency key:

   ```sh
   agileforge project setup retry \
     --project-id "$PROJECT_ID" \
     --spec-file specs/spec.json \
     --expected-state SETUP_REQUIRED \
     --expected-context-fingerprint "$CTX" \
     --recovery-mutation-event-id "$RECOVERY_EVENT_ID" \
     --idempotency-key "setup-retry-$PROJECT_ID-$(date +%Y%m%d%H%M%S)" \
     --changed-by codex
   ```

5. Re-read mutation list:

   ```sh
   agileforge mutation list --project-id "$PROJECT_ID"
   ```

A successful linked retry supersedes the original recovery row. Replaying the
original create idempotency key should return the stored recovery/success
response rather than creating duplicate setup artifacts.

## Mutation Resume

Use:

```sh
agileforge mutation resume --mutation-event-id "$MUTATION_EVENT_ID"
```

This command is an operational recovery command. It mutates only ledger recovery
ownership. It does not accept altered domain arguments from the original command.

Use it when:

- a remediation explicitly tells you to inspect or acquire recovery;
- you need to determine whether recovery is still owned by another worker;
- a stale recovery row needs a lease transition before domain-specific repair.

Do not use it as a replacement for `project setup retry` when the remediation
requires stale guards and a spec file.

## Error Codes

Registered CLI error codes include:

| Code | Meaning | Agent response |
| --- | --- | --- |
| `INVALID_COMMAND` | Parser or flag contract failed. | Fix command syntax. |
| `COMMAND_EXCEPTION` | Unexpected exception. | Surface logs and command envelope. |
| `COMMAND_NOT_IMPLEMENTED` | Command route is not implemented. | Stop using that command. |
| `SCHEMA_NOT_READY` | Storage schema is missing or incompatible. | Run diagnostics, migrate/init storage outside the agent workflow. |
| `PROJECT_NOT_FOUND` | Project id does not exist. | Refresh project list. |
| `PROJECT_ALREADY_EXISTS` | Project name is already used. | Pick a different name or inspect existing project. |
| `STORY_NOT_FOUND` | Story id does not exist. | Refresh story/project state. |
| `SPEC_VERSION_NOT_FOUND` | Spec version does not exist. | Refresh authority status. |
| `SPEC_FILE_NOT_FOUND` | Spec path cannot be found. | Fix caller-relative path. |
| `SPEC_FILE_INVALID` | Spec path exists but is invalid. | Fix spec file content/path. |
| `SPEC_SOURCE_FORMAT_UNSUPPORTED` | Spec source is not `agileforge.spec.v1` JSON. | Generate `specs/spec.json` and retry with that file. |
| `SPEC_COMPILE_FAILED` | Authority compilation failed. | Inspect error details, retry only when cause is fixed. |
| `AUTHORITY_REVIEW_REQUIRED` | An authority decision was attempted before review evidence was available. | Run `authority review`, then retry the decision from current project state. |
| `AUTHORITY_NOT_ACCEPTED` | No accepted authority exists. | Stop or request manual authority review. |
| `AUTHORITY_NOT_COMPILED` | Selected spec has no compiled authority. | Re-read authority status. |
| `AUTHORITY_NOT_PENDING` | There is no pending authority decision for this project. | Re-read `authority status` and `workflow next`. |
| `AUTHORITY_ALREADY_DECIDED` | The pending authority already has a terminal decision. | Replay the same idempotency key or refresh status. |
| `AUTHORITY_SOURCE_CHANGED` | The source spec or authority snapshot changed after review. | Rerun `authority review` and decide from the new packet. |
| `AUTHORITY_SOURCE_UNAVAILABLE` | The source spec cannot be read at decision time. | Restore the readable source file, then rerun `authority review`. |
| `AUTHORITY_REVIEW_INCOMPLETE` | Host structural validation found stale, malformed, or incomplete authority data. | Fix the structured spec or compiler output, then rerun `authority review`. |
| `AUTHORITY_GUARD_INCOMPLETE` | Explicit authority decision mode omitted required guards. | Pass every field from `guard_tokens` and an idempotency key. |
| `AUTHORITY_ACCEPTANCE_MISMATCH` | Accepted authority provenance drifted. | Stop and surface. |
| `AUTHORITY_INVARIANTS_INVALID` | Stored invariant JSON is invalid. | Stop and surface storage issue. |
| `STALE_STATE` | Expected workflow state mismatched. | Refresh state, review, use new key. |
| `STALE_ARTIFACT_FINGERPRINT` | Reviewed artifact changed. | Re-read artifact before mutating. |
| `STALE_CONTEXT_FINGERPRINT` | Reviewed context changed. | Rebuild context pack before retry. |
| `STALE_AUTHORITY_VERSION` | Accepted authority version changed. | Re-read authority before retry. |
| `CONFIRMATION_REQUIRED` | Destructive confirmation missing. | Add required confirmation flags only after review. |
| `ACTIVE_STATE_BLOCKS_DELETE` | Active workflow blocks destructive op. | Stop or complete/reset workflow first. |
| `SCHEMA_VERSION_MISMATCH` | Storage schema version incompatible. | Run schema migration/check outside workflow. |
| `MUTATION_FAILED` | Mutation failed without a more specific code. | Inspect details and mutation ledger. |
| `MUTATION_ROLLBACK` | Mutation rolled back or needs recovery. | Inspect mutation ledger. |
| `MUTATION_IN_PROGRESS` | Active lease exists. | Wait or inspect event. |
| `MUTATION_RECOVERY_REQUIRED` | Durable recovery is required. | Follow `next_actions` and remediation. |
| `MUTATION_RESUME_CONFLICT` | Another worker acquired recovery. | Re-read mutation event. |
| `MUTATION_RECOVERY_INVALID` | Recovery link is invalid. | Refresh mutation list and use correct event id. |
| `IDEMPOTENCY_KEY_REUSED` | Same key used with different request. | Stop and generate a new reviewed attempt. |
| `MUTATION_NOT_FOUND` | Mutation event id does not exist. | Refresh mutation list. |
| `WORKFLOW_SESSION_FAILED` | Workflow session setup failed. | Inspect recovery state and retry setup if directed. |

Agents should use the command-specific schema for exact possible errors:

```sh
agileforge command schema "agileforge project setup retry"
```

## Copy-Paste Recipes

### Health Check

```sh
agileforge doctor
agileforge schema check
agileforge capabilities
```

### Create New Project From Caller Repo

```sh
cd /path/to/caller-project

agileforge project create \
  --dry-run \
  --dry-run-id preview-my-project-001 \
  --name "My Project" \
  --spec-file specs/spec.json

payload="$(
  agileforge project create \
    --name "My Project" \
    --spec-file specs/spec.json \
    --idempotency-key create-my-project-20260516-001 \
    --changed-by codex
)"

PROJECT_ID="$(
  PAYLOAD="$payload" python -c 'import json,os,sys; p=json.loads(os.environ["PAYLOAD"]); print(p["data"]["project_id"] if p["ok"] else ""); sys.exit(0 if p["ok"] else p["errors"][0].get("exit_code",1))'
)"

agileforge authority status --project-id "$PROJECT_ID"
```

### Inspect Pending Authority

```sh
agileforge authority status --project-id "$PROJECT_ID" |
python -c 'import json,sys; p=json.load(sys.stdin); d=p["data"]; print({"status": d["status"], "authority_id": d["authority_id"], "pending_authority_id": d["pending_authority_id"], "pending_spec": d["pending_compiled_spec_version_id"]})'

agileforge workflow next --project-id "$PROJECT_ID" |
python -c 'import json,sys; p=json.load(sys.stdin); print(p["data"].get("next_valid_commands") or p["data"].get("next_actions"))'
```

### Review And Accept Authority

```sh
agileforge authority review --project-id "$PROJECT_ID" --open > review.json
python -m json.tool review.json >/dev/null

# Ask: Does this compiled interpretation correctly represent the spec?

agileforge authority accept \
  --project-id "$PROJECT_ID" > accept.json
python -m json.tool accept.json >/dev/null

agileforge authority status --project-id "$PROJECT_ID" |
python -c 'import json,sys; d=json.load(sys.stdin)["data"]; assert d["status"] == "current"; assert d["authority_id"] is not None; assert d["pending_authority_id"] is None; print("authority current")'
```

### Recover Project Setup

```sh
agileforge mutation list --project-id "$PROJECT_ID" --status recovery_required

RECOVERY_EVENT_ID=10

CTX="$(
  agileforge context pack --project-id "$PROJECT_ID" --phase overview |
  python -c 'import json,sys; print(json.load(sys.stdin)["data"]["guard_tokens"]["expected_context_fingerprint"])'
)"

agileforge project setup retry \
  --project-id "$PROJECT_ID" \
  --spec-file specs/spec.json \
  --expected-state SETUP_REQUIRED \
  --expected-context-fingerprint "$CTX" \
  --recovery-mutation-event-id "$RECOVERY_EVENT_ID" \
  --idempotency-key "setup-retry-$PROJECT_ID-$(date +%Y%m%d%H%M%S)" \
  --changed-by codex
```

### Replay After Timeout

If a mutation command timed out and the agent does not know whether it succeeded,
run the exact same command again with the exact same idempotency key:

```sh
agileforge project create \
  --name "My Project" \
  --spec-file specs/spec.json \
  --idempotency-key create-my-project-20260516-001 \
  --changed-by codex
```

Do not change `--name`, `--spec-file`, `--changed-by`, or the idempotency key
during replay. A different request with the same key returns
`IDEMPOTENCY_KEY_REUSED`.

## Agent Skill Guidance

An AgileForge CLI skill should implement these steps.

### Startup

1. Run `command -v agileforge`.
2. Run `agileforge doctor`.
3. Run `agileforge schema check`.
4. Run `agileforge capabilities`.
5. Cache installed command names and command schemas for the current session.

### Before Any Mutation

1. Confirm command appears in `capabilities`.
2. Read `command schema`.
3. Build request from schema-required fields.
4. Generate a valid idempotency key for non-dry-run mutations.
5. Use `--dry-run` first when the command supports it and deterministic preview
   is useful.
6. For guarded commands, fetch fresh guard tokens immediately before execution.

### After Any Mutation

1. Parse the JSON envelope.
2. If `ok: true`, store `data.mutation_event_id` when present.
3. Follow `data.next_actions` when present.
4. If `ok: false`, branch on `errors[0].code`.
5. For recovery errors, inspect `mutation show` and use exact remediation.
6. Never create a second mutation attempt until idempotency replay and recovery
   state are understood.

### Stop Conditions

The skill must stop and surface state when:

- command is not installed;
- schema is not ready;
- authority is pending and the agent has not reviewed `authority review`;
- an AI or human reviewer cannot confirm the compiled interpretation represents
  the spec;
- stale guard is returned;
- idempotency key was reused with a different request;
- `AUTHORITY_REVIEW_INCOMPLETE` is returned and no human has reviewed omitted
  source out of band;
- mutation recovery requires manual inspection;
- direct DB/API/browser use would be needed to continue.

### Do Not

- Do not install AgileForge into each caller repo.
- Do not call FastAPI routes or require a web server for the CLI workflow.
- Do not use browser automation as the agent interface.
- Do not edit SQLite directly.
- Do not treat generated or compiled authority as accepted.
- Do not mutate after a stale guard without refreshing state.
- Do not retry with a changed request and the same idempotency key.

## Future Commands Not Yet Installed

The broader CLI roadmap includes commands such as:

- `agileforge sprint generate`
- `agileforge sprint start`
- `agileforge task log`
- `agileforge workflow reset`
- `agileforge project delete`

These are not part of the current installed command set unless they appear in
`agileforge capabilities`. Agent skills must check capabilities at runtime and
must not assume future commands exist.
