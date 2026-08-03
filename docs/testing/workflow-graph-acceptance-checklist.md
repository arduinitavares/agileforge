# Workflow Graph Operator Acceptance Checklist

## Acceptance State

No repository acceptance has run. Checklist preparation and review do not change
this state.

```yaml
acceptance_status: not_run
```

The state remains `not_run` until the Operator returns completed evidence. This
document contains no caRtola, ASA, or MyFinance acceptance result.

## Scope And Ownership

The only external repositories covered are:

- caRtola: `/Users/aaat/projects/caRtola`
- ASA: `/Users/aaat/projects/asa-deep-process-control-experiments`
- MyFinance: `/Users/aaat/myfinance`

The Operator runs every command and owns all external changes. The AgileForge
implementation worker must not inspect deeply, edit, branch, create a worktree
in, or otherwise mutate these repositories. The Operator records repository
reads and any Operator-owned external changes as evidence.

## Reviewed Runtime Pin

Use one reviewed checkout for schema creation and every CLI invocation:

```sh
export AGILEFORGE_WORKTREE="/Users/aaat/projects/agileforge/.worktrees/domain-workflow-graph-hard-break"
export AGILEFORGE_SHA="<reviewed-agileforge-sha>"
```

The Operator must, before every CLI invocation and before each restart boundary,
run and record:

```sh
git -C "$AGILEFORGE_WORKTREE" rev-parse HEAD
```

The result must equal `AGILEFORGE_SHA`. Stop on any mismatch. Do not use a
globally installed or PATH-selected `agileforge`. Start each command from the
reviewed checkout:

```sh
cd "$AGILEFORGE_WORKTREE"
uv run --frozen agileforge project list
```

All later `uv run --frozen agileforge` examples assume the current directory is
still `AGILEFORGE_WORKTREE` and that the SHA equality check just passed. Record
the `cd`, SHA check, exact argv, exit result, and timestamp with each step.

## Fresh Database Preflight

Perform a separate preflight for caRtola, ASA, and MyFinance. Allocate a new
disposable database per repository acceptance run. Never reuse one run's
database or temp root for another run.

1. Create and record a new test-only `ACCEPTANCE_TEMP_ROOT` that did not exist for
   an earlier run.
2. Pin all non-secret acceptance environment values for the entire run:

```sh
export ACCEPTANCE_TEMP_ROOT="<new-absolute-test-temp-root>"
export BUSINESS_DB_PATH="$ACCEPTANCE_TEMP_ROOT/agileforge-business.sqlite3"
export TRACE_DB_PATH="$ACCEPTANCE_TEMP_ROOT/agileforge-adk-trace.sqlite3"
export AGILEFORGE_DB_URL="sqlite:///${BUSINESS_DB_PATH}"
export AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL="sqlite:///${TRACE_DB_PATH}"
export MODEL_CONFIG_PATH="$AGILEFORGE_WORKTREE/config/models.yaml"
export ACCEPTANCE_ACTOR="<operator-identity>"
```

3. Record the values, the model-config file hash, every configured model role and
   model ID, the reviewed AgileForge SHA, and the UTC start timestamp. Record
   credential presence without writing credential values into evidence.
4. Prove both files are new and the business and trace paths differ:

```sh
test ! -e "$BUSINESS_DB_PATH"
test ! -e "$TRACE_DB_PATH"
test "$BUSINESS_DB_PATH" != "$TRACE_DB_PATH"
```

5. Confirm both paths resolve inside `ACCEPTANCE_TEMP_ROOT`. Stop if either path
   escapes it.
6. From the SHA-verified checkout, create only the current schema:

```sh
cd "$AGILEFORGE_WORKTREE"
uv run --frozen python agile_sqlmodel.py
```

7. Confirm the fresh business database now exists. There is no migration. The
   prior durable database remains untouched: do not copy, migrate, rename,
   replace, delete, or point any acceptance environment variable at it.
8. Record the target repository SHA and dirty state with Operator-run read-only
   Git commands before Operator-owned work. Never let AgileForge inventory alter
   that state.

Keep `AGILEFORGE_WORKTREE`, `AGILEFORGE_SHA`, `AGILEFORGE_DB_URL`,
`AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL`, `MODEL_CONFIG_PATH`, and
`ACCEPTANCE_ACTOR` unchanged for the whole repository run.

## Command And Placeholder Protocol

`WorkflowDomain.position(project_id)` is the sole routing authority. Project
Shell creation is the only pre-position mutation. Open it through the pinned
checkout:

```sh
uv run --frozen agileforge project create --name "$PROJECT_NAME" --origin brownfield --idempotency-key "$PROJECT_OPEN_KEY" --changed-by "$ACCEPTANCE_ACTOR"
```

Record the returned `PROJECT_ID`. Before every later mutation, run:

```sh
uv run --frozen agileforge workflow next --project-id "$PROJECT_ID"
```

Save the graph-returned command template unchanged. It begins with `agileforge`
and may declare only these substitution slots: `<input-file>` or
`<request-file>`, `<idempotency-key>`, and `<actor>`. The Operator must:

1. Record the original command template.
2. Replace only declared placeholders with shell-quoted concrete values.
3. Record a placeholder-to-value map without secrets.
4. Parse and record the final executed argv, preserving project, graph, fact,
   decision, and `instance_key` guards exactly.
5. Run that argv from `AGILEFORGE_WORKTREE` through `uv run --frozen`, so argv
   element zero remains the checkout's `agileforge` entry point.

Never call an uninstantiated angle-bracket template as an executable command.
Never infer a mutation from `node_id`, a phase name, or visible artifacts.

Use a new idempotency key for each distinct request. Reuse the same key only for
one retry of the exact same request after an uncertain transport result; the
template, substitutions, payload, guards, and actor must also remain identical.

Record before and after facts-only reads for every mutation:

```sh
uv run --frozen agileforge workflow position --project-id "$PROJECT_ID"
uv run --frozen agileforge workflow next --project-id "$PROJECT_ID"
```

The supported initial-specification review is also a facts-only read:

```sh
uv run --frozen agileforge project initial-spec --project-id "$PROJECT_ID"
```

It returns the exact active draft ID, canonical content, content fingerprint,
provenance path, and immutable created/updated timestamp. Bind the subsequent
graph-authored human decision to that same ID and fingerprint. Stop on typed
`PROJECT_NOT_FOUND`, `INITIAL_SPEC_DRAFT_NOT_FOUND`,
`INITIAL_SPEC_DRAFT_AMBIGUOUS`, or `INITIAL_SPEC_DRAFT_INVALID` output.

Other supported facts-only reads used by this checklist are:

```sh
uv run --frozen agileforge project show --project-id "$PROJECT_ID"
uv run --frozen agileforge authority status --project-id "$PROJECT_ID"
uv run --frozen agileforge authority invariants --project-id "$PROJECT_ID"
uv run --frozen agileforge authority review --project-id "$PROJECT_ID" --include-spec full
```

Stop when an advertised, fully instantiated command cannot parse or execute, or
when a command using the immediately preceding unchanged guards fails. Do not
remove guards, substitute a nearby command, alter input to force progress, or
author a repair command.

## caRtola Acceptance

Repository: `/Users/aaat/projects/caRtola`

Execute and record this ordered lifecycle:

1. Open one brownfield Project Shell.
2. Record the exact repository baseline from Operator-supplied SHA/dirty facts.
3. Record a complete Git-aware inventory.
4. Run initial specification curation through the graph-returned template.
5. Inspect it with
   `uv run --frozen agileforge project initial-spec --project-id "$PROJECT_ID"`.
6. Submit the graph-authored human initial-spec decision bound to the reviewed
   draft ID and hash.
7. Complete initial scope registration.
8. Run authority compile and record the actual model ID.
9. Capture the facts-only authority review, status, and invariants.
10. Submit the graph-authored human authority decision.
11. Take a position capture and record the accepted authority ID and hash.
12. Perform the pinned CLI process restart procedure below.
13. Take a position recapture and repeat the authority reads without mutation.

Inventory proof must contain `git_available: true`, `truncated: false`, the
complete inventory file count and paths, suppressed entries and reasons, model
selection count and paths, total bytes, repository snapshot evidence, and the
persisted inventory fingerprint. A bounded model selection is not permission to
truncate the complete inventory. A limit error stops the run.

Restart proof must show the accepted authority ID and hash are unchanged, the
same fact fingerprint is reproduced, and graph version, decisions,
`instance_key` values, waiting/blocked/invalid nodes, and terminal state match
after excluding only read timestamps.

## ASA Acceptance

Repository: `/Users/aaat/projects/asa-deep-process-control-experiments`

Execute and record this ordered lifecycle:

1. Open one brownfield Project Shell.
2. Record the exact repository baseline from Operator-supplied SHA/dirty facts.
3. Record a complete Git-aware inventory.
4. Run initial specification curation through the graph-returned template.
5. Inspect it with
   `uv run --frozen agileforge project initial-spec --project-id "$PROJECT_ID"`.
6. Submit the graph-authored human initial-spec decision bound to the reviewed
   draft ID and hash.
7. Complete initial scope registration.
8. Run authority compile and record the actual model ID.
9. Capture the facts-only authority review, status, and invariants.
10. Submit the graph-authored human authority decision.
11. Take a position capture and record the accepted authority ID and hash.
12. Perform the pinned CLI process restart procedure below.
13. Take a position recapture and repeat the authority reads without mutation.

Inventory proof must contain `git_available: true`, `truncated: false`, the
complete inventory file count and paths, suppressed entries and reasons, model
selection count and paths, total bytes, repository snapshot evidence, and the
persisted inventory fingerprint. A bounded model selection is not permission to
truncate the complete inventory. A limit error stops the run.

Restart proof must show the accepted authority ID and hash are unchanged, the
same fact fingerprint is reproduced, and graph version, decisions,
`instance_key` values, waiting/blocked/invalid nodes, and terminal state match
after excluding only read timestamps.

## MyFinance Real-Feature Acceptance

Repository: `/Users/aaat/myfinance`

"Statement Streams and Coverage" is the real feature supplied by the Operator to test AgileForge. AgileForge must guide the work through accepted authority, backlog, roadmap/story, sprint planning, task execution, review, sprint close, and post-sprint triage. Operator runs every command and owns all MyFinance changes.

Use synthetic evidence only and an isolated MyFinance test environment selected
and recorded by the Operator. Do not prescribe MyFinance code changes. Operator
owns all external changes. This checklist does not create a MyFinance worktree or
select implementation steps.

Execute and record this ordered lifecycle:

1. Open one brownfield Project Shell and record the approved context.
2. Record the repository baseline and complete Git-aware inventory.
3. Run curation, then inspect the draft with
   `uv run --frozen agileforge project initial-spec --project-id "$PROJECT_ID"`.
4. Submit the graph-authored human initial-spec decision.
5. Complete initial scope registration and reach accepted authority.
6. Generate and decide a backlog consistent with the approved Statement Streams
   and Coverage language.
7. Generate and decide a reviewable roadmap.
8. Generate, inspect, and decide reviewable story facts and dependencies.
9. Complete sprint planning, review, decision, and start facts.
10. Record task execution using synthetic artifacts only.
11. Complete review and closure facts for tasks and stories.
12. Record sprint close facts after the graph-authored review boundary.
13. Reach post-sprint triage through graph facts without transient-state repair.

Record context, backlog language, roadmap/story/sprint/task IDs and hashes,
model IDs, authority identity, and every position fingerprint. This run must also
perform the stale-guard rejection, pinned CLI process restart, and ADK
execution-trace reset procedures below without recreating routing state.

## Stale-Guard Rejection Probe

Choose one ordinary mutation whose original guarded command template can first
succeed. Record its before-position and substitutions.

1. Execute the first successful mutation with its distinct idempotency key.
2. Capture the after-position and prove the Project fact fingerprint changed.
3. Re-instantiate the original template with a new idempotency key only.
4. Preserve the old graph, fact, decision, and instance guards and the original
   payload/actor values.
5. Execute that pinned argv as a negative probe and require stale rejection.
6. Read facts again and prove no second mutation was applied.

The new key prevents receipt replay from masking guard evaluation. Record the
typed error, exit result, exact old guards, new key, and unchanged durable facts.
If the first mutation does not change facts, select another normal mutation; do
not fabricate a fact or strip a guard.

## Pinned CLI Process Restart

The production command is a one-shot CLI: every invocation creates a process,
composes the application, performs one command, and exits. There is no daemon to
stop or resume.

For restart proof, record invocation A, wait for it to exit, repeat the SHA
check, then perform a separate fresh process/shell invocation B. Both must use
the same recorded worktree SHA, same AGILEFORGE_DB_URL, same MODEL_CONFIG_PATH,
same ACCEPTANCE_ACTOR, and same AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL. Also keep
the Project ID and all other pinned environment values unchanged.

Use position reads on both sides:

```sh
uv run --frozen agileforge workflow position --project-id "$PROJECT_ID"
uv run --frozen agileforge workflow position --project-id "$PROJECT_ID"
```

Record both argv vectors, process exit results, and timestamps. Compare the full
position snapshots while excluding only `evaluated_at`. For caRtola and ASA,
repeat authority reads and prove accepted authority ID/hash stability. Do not run
a mutation between A and B.

## ADK Execution-Trace Reset

Run this only for MyFinance after a position capture. The target must be the
separately configured disposable trace file from preflight, inside
ACCEPTANCE_TEMP_ROOT, and it must differ from the durable business database.

1. Record that the preceding one-shot command exited and no active acceptance
   CLI process remains. Do not delete while any test command is active.
2. Resolve and record `TRACE_DB_PATH`, `BUSINESS_DB_PATH`, and
   `ACCEPTANCE_TEMP_ROOT`; stop unless trace is inside the root and unequal to
   business.
3. Preserve required trace evidence, then delete only that trace file:

```sh
rm -- "$TRACE_DB_PATH"
```

4. Repeat the reviewed-SHA check and start a new pinned CLI process with every
   preflight value unchanged.
5. Capture position before any mutation and compare graph version, fact
   fingerprint, decision fingerprint, and `instance_key` values with the
   pre-reset snapshot.

Never delete the durable DB or another file. Never run or invent a session
deletion command. Continue only through a newly returned graph command after the
new pinned CLI process proves position independence.

## Evidence Template

Create one YAML document per repository run. `steps` is the authoritative source
for command, guard, authority, model, verification, and failure correlation. The
top-level arrays are summaries derived from completed steps; they must never
override or repair a step record.

Step status values are `not_run`, `passed`, `failed`, and `blocked`:

- `not_run`: the Operator has not executed the step.
- `passed`: the recorded result met the step contract.
- `failed`: the command or verification ran and failed.
- `blocked`: an earlier failure or safety boundary prevented execution.

The prepared template keeps overall `acceptance_status` as `not_run`. Only the
Operator's returned evidence can establish an overall result. A failed step's
structured record must stand alone as sufficient Task 19 input without
correlating parallel arrays.

Overall repository status uses `not_run`, `passed`, or `failed`. After Operator
evidence returns, mark it `passed` only when every required step is passed and
all required verification succeeds. Mark it `failed` when returned evidence
contains a concrete required-step failure. A blocked step prevents a pass, and
incomplete evidence remains not_run rather than being inferred as success.

```yaml
acceptance_status: not_run
repository_name: ""
repository_path: ""
repository_commit: ""
repository_dirty: false
agileforge_commit: ""
project_id: 0
graph_versions: []
commands: []
fact_fingerprints: []
decision_fingerprints: []
authority_ids: []
authority_hashes: []
model_ids: []
verification_commands: []
verification_results: []
final_position: {}
observed_failures: []
runtime:
  agileforge_worktree: ""
  acceptance_temp_root: ""
  business_db_url: ""
  trace_db_url: ""
  model_config_path: ""
  actor: ""
  started_at: ""
steps:
  - step_id: ""
    repository:
      name: ""
      path: ""
      commit: ""
      dirty: false
    phase: ""
    status: not_run
    timestamps:
      started_at: null
      completed_at: null
    node_id: null
    request_kind: null
    recommendation_kind: null
    command_template: null
    placeholder_substitutions: {}
    executed:
      argv: []
      exit_code: null
      result: {}
    positions:
      before:
        captured_at: null
        argv: []
        exit_code: null
        result: {}
        graph_version: null
        fact_fingerprint: null
        decision_fingerprint: null
        instance_key: null
      after:
        captured_at: null
        argv: []
        exit_code: null
        result: {}
        graph_version: null
        fact_fingerprint: null
        decision_fingerprint: null
        instance_key: null
    guards:
      graph_version: null
      fact_fingerprint: null
      decision_fingerprint: null
      instance_key: null
    authority:
      authority_id: null
      authority_hash: null
    model_id: null
    verification:
      command: null
      result: {}
    attached_artifacts: []
    failure:
      kind: null
      code: null
      message: null
      details: {}
      expected: false
      mutation_applied: null
```

Copy the step object for every preflight read, Project read, graph read,
mutation, negative probe, restart read, trace reset, and verification. Record
exact executed argv, not a reconstructed shell string. Preserve raw JSON results
as attached artifacts when they are too large for the inline result.

## Stop Boundary

Checklist preparation is not acceptance execution. Keep
`acceptance_status: not_run` for caRtola, ASA, and MyFinance until the Operator
returns complete evidence or one concrete failure. Do not execute an external
acceptance step from this implementation task. Do not start Task 19. After an
independent review accepts this package, hand it to the Operator and stop.
