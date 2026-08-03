# Workflow Graph Operator Acceptance Checklist

This is an Operator-run evidence package. It is not an execution report.

```yaml
acceptance_status: not_run
```

checklist preparation is not acceptance execution. No repository below has
passed acceptance. The status remains `not_run` until the Operator returns the
completed evidence. Do not start Task 19 from this document alone.

## Scope And Ownership

The only repositories covered are:

- caRtola: `/Users/aaat/projects/caRtola`
- ASA: `/Users/aaat/projects/asa-deep-process-control-experiments`
- MyFinance: `/Users/aaat/myfinance`

The Operator runs every command. The Operator owns all external changes. This
package must not inspect deeply, edit, branch, create a worktree in, or otherwise
mutate any of these repositories. Repository reads and any external changes made
during acceptance are Operator actions, not AgileForge implementation actions.

## Current Command Contract

`WorkflowDomain.position(project_id)` is the sole routing authority. Use Project
terminology. `workflow position` and `workflow next` are facts-only reads;
neither read advances the workflow. A Project Shell is the only pre-position
mutation. Every later mutation must be the exact task-specific command returned
by `workflow next`.

These literal examples were checked against the live parser:

```sh
agileforge project create --name <name> --origin brownfield --idempotency-key <idempotency-key> --changed-by <actor>
agileforge workflow position --project-id <id>
agileforge workflow position --project-id <id> --include-optional
agileforge workflow next --project-id <id>
agileforge project show --project-id <id>
agileforge authority status --project-id <id>
agileforge authority invariants --project-id <id>
agileforge authority review --project-id <id> --include-spec full
```

Angle-bracket values are literal Operator placeholders. The graph-authored
mutation is represented only as `<returned-command>` in this checklist. Do not
infer a mutation from a node name or from the stage descriptions below.

## Fresh Database Preflight

Complete once per acceptance run, before opening any Project Shell.

- [ ] Record the UTC start timestamp.
- [ ] Record the AgileForge commit SHA.
- [ ] Record the selected model configuration path, every configured model role
  and model ID, and any command-level `--model-id` override.
- [ ] Record each target repository commit SHA and dirty state before any
  Operator-owned work. Use `git -C <path> rev-parse HEAD` and
  `git -C <path> status --short` or equivalent read-only evidence.
- [ ] Choose new, disposable, previously nonexistent SQLite files for the
  business database and ADK execution-trace database. They must be different
  files in a test-only location.
- [ ] Export `AGILEFORGE_DB_URL=sqlite:///<path>/agileforge-acceptance-<timestamp>.sqlite3`.
- [ ] Export
  `AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL=sqlite:///<path>/agileforge-adk-trace-<timestamp>.sqlite3`.
- [ ] Point `MODEL_CONFIG_PATH` at the recorded model configuration.
- [ ] Create the current schema with
  `uv run --frozen python <agileforge-worktree>/agile_sqlmodel.py`.
- [ ] Confirm the new business database exists. Do not copy, open for writes,
  migrate, rename, or delete any prior AgileForge database. This is a fresh
  schema run with no migration; the prior database remains untouched.
- [ ] Keep each Project on this same fresh business database through its restart
  checks. Never replace the business database to simulate a restart.

After each Project Shell is created, record the `project_id`, the first
`graph_version`, and the initial `fact_fingerprint` returned by `workflow next`.

## Mutation Protocol

Use this protocol for every graph mutation in every repository section.

1. Run `agileforge workflow next --project-id <id>` immediately before the
   mutation.
2. Save the complete JSON result. For the selected command, record the exact
   `graph_version`, `fact_fingerprint`, `node_id`, `request_kind`,
   `decision_fingerprint`, and `instance_key` value, including `null`.
3. Confirm the returned command carries `--project-id`, `--graph-version`,
   `--expected-fact-fingerprint`, `--expected-decision-fingerprint`, and, when
   advertised, `--instance-key`. Preserve its payload-file form, idempotency key,
   actor, model ID, and correlation ID.
4. Prepare the request or input file from Operator-approved evidence. Do not
   change the command prefix or remove a guard.
5. Run exactly `<returned-command>` once.
6. Save stdout, stderr, exit code, and completion timestamp. Then capture
   `workflow position` and `workflow next` again and record the after-guards.

Stop the run and report the command plus evidence when any advertised command
cannot parse or execute. Also stop when a command built from the immediately
preceding position fails while its graph, fact, decision, and instance guards
are unchanged. Do not substitute another command, strip guards, retry with
altered input, or create a repair command.

An uncertain transport result may be retried once only with the identical
command and identical idempotency key. Otherwise, read `workflow next` again.

## caRtola Acceptance

Repository: `/Users/aaat/projects/caRtola`

Run the mutation protocol through this evidence sequence:

- [ ] Open exactly one brownfield Project Shell.
- [ ] Record a repository baseline bound to the Operator-recorded commit and
  dirty state.
- [ ] Record a complete Git-aware inventory.
- [ ] Run initial specification curation.
- [ ] Review the curated specification and record the human initial-spec
  decision advertised by the graph.
- [ ] Register the initial scope.
- [ ] Compile authority and record the actual model ID.
- [ ] Capture the facts-only authority status, invariants, and full review.
- [ ] Record the human authority decision advertised by the graph.
- [ ] Capture the resulting position and accepted authority identity.
- [ ] Stop the AgileForge process cleanly, then start a new process with the same
  fresh business database and configuration.
- [ ] Recapture the position and authority reads without running a mutation.

Inventory proof must include `git_available: true`, `truncated: false`, the full
inventory file count and paths, suppressed entries with reasons, selected model
file count and paths, repository snapshot/commit evidence, and the persisted
inventory fingerprint. A model-file budget may reduce `selected_for_model`; it
must not silently truncate the complete inventory. A limit error is a stopped
run, not partial acceptance.

Restart proof must show the same accepted authority ID and authority
fingerprint/hash before and after restart. Compare position projections while
excluding only the read timestamp: graph version, fact fingerprint, decision
identities, categories, instance keys, reason codes, waiting/blocked/invalid
nodes, and terminal state must reproduce.

## ASA Acceptance

Repository: `/Users/aaat/projects/asa-deep-process-control-experiments`

Run the mutation protocol through this evidence sequence:

- [ ] Open exactly one brownfield Project Shell.
- [ ] Record a repository baseline bound to the Operator-recorded commit and
  dirty state.
- [ ] Record a complete Git-aware inventory.
- [ ] Run initial specification curation.
- [ ] Review the curated specification and record the human initial-spec
  decision advertised by the graph.
- [ ] Register the initial scope.
- [ ] Compile authority and record the actual model ID.
- [ ] Capture the facts-only authority status, invariants, and full review.
- [ ] Record the human authority decision advertised by the graph.
- [ ] Capture the resulting position and accepted authority identity.
- [ ] Stop the AgileForge process cleanly, then start a new process with the same
  fresh business database and configuration.
- [ ] Recapture the position and authority reads without running a mutation.

Inventory proof must include `git_available: true`, `truncated: false`, the full
inventory file count and paths, suppressed entries with reasons, selected model
file count and paths, repository snapshot/commit evidence, and the persisted
inventory fingerprint. A model-file budget may reduce `selected_for_model`; it
must not silently truncate the complete inventory. A limit error is a stopped
run, not partial acceptance.

Restart proof must show the same accepted authority ID and authority
fingerprint/hash before and after restart. Compare position projections while
excluding only the read timestamp: graph version, fact fingerprint, decision
identities, categories, instance keys, reason codes, waiting/blocked/invalid
nodes, and terminal state must reproduce.

## MyFinance Real-Feature Acceptance

Repository: `/Users/aaat/myfinance`

"Statement Streams and Coverage" is the real feature supplied by the Operator to test AgileForge. AgileForge must guide the work through accepted authority, backlog, roadmap/story, sprint planning, task execution, review, sprint close, and post-sprint triage. Operator runs every command and owns all MyFinance changes.

Use synthetic evidence only and an isolated MyFinance test environment. The
Operator chooses and records that isolation. This checklist does not create a
MyFinance worktree or prescribe implementation steps. Do not prescribe MyFinance code changes. Operator owns all external changes.

Run the mutation protocol and retain evidence for:

- [ ] Brownfield Project Shell, complete Git-aware inventory, approved current
  product context, initial specification decision, initial scope registration,
  and accepted authority.
- [ ] Backlog draft and human decision whose wording remains consistent with the
  approved Statement Streams and Coverage context.
- [ ] Reviewable roadmap draft and decision facts.
- [ ] Reviewable story draft, dependency/readiness evidence, and human decision
  facts.
- [ ] Reviewable sprint plan, review evidence, human decision, and start facts.
- [ ] Task execution evidence using synthetic artifacts only, followed by task
  completion and story review/close facts advertised by the graph.
- [ ] Sprint review and close facts.
- [ ] Post-sprint triage facts reached through the graph without any command that
  repairs transient execution-trace state or deleted routing state.

Record the approved context verbatim, backlog language, roadmap/story/sprint/task
fact IDs and fingerprints, accepted authority ID/hash, every model ID, and the
position/fact fingerprint after each lifecycle boundary.

### Stale-Command Rejection Probe

Perform this only when one `workflow next` snapshot advertises two independent
commands. Save both exact returned commands. Execute one under the normal
mutation protocol, then read `workflow next` again and show that the Project fact
fingerprint changed. Attempt the other saved command unchanged as a negative
guard probe. Record the nonzero result and stale-guard error, prove no business
fact was added, and discard the stale command. If two independent commands are
never advertised, record the probe as `not_observable`; do not manufacture a
second mutation.

A stale rejection is expected negative evidence. By contrast, a command using
the current unchanged guards that fails to parse or execute stops acceptance.

### Process And ADK Execution-Trace Independence

The current configuration supports a separate
`AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL`. It does not expose an Operator session
deletion command. For this test-only reset:

1. Capture `workflow position`, stop the AgileForge process, and preserve the
   business database.
2. Verify `<adk-trace-db-path>` is the disposable trace file recorded in the
   preflight and is not `<business-db-path>`.
3. Preserve any needed trace evidence, then run `rm -- <adk-trace-db-path>` while
   AgileForge is stopped.
4. Start a clean AgileForge process with the same business database and the same
   configured trace path. Do not run a mutation.
5. Recapture `workflow position`. Compare the graph version, fact fingerprint,
   and decision/instance fingerprints, excluding only the read timestamp.
6. Continue only through commands newly returned by `workflow next`; do not
   recreate or repair routing state from the deleted trace.

Record the exact stopped-process boundary, trace path check, timestamps, and
before/after positions. This procedure deletes only the isolated test trace
database, never the business database or a repository file.

## Evidence Template

Create one copy per repository. Keep this base template exactly, then attach one
step record from the next block for every read, mutation, restart, and negative
probe.

```yaml
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
```

Use this copy-ready record for each step:

```yaml
step_id: ""
acceptance_status: not_run
repository_path: ""
started_at: ""
completed_at: ""
command: ""
request_or_input_file: ""
result:
  exit_code: null
  stdout: ""
  stderr: ""
guards:
  graph_version_before: ""
  fact_fingerprint_before: ""
  decision_fingerprint: ""
  instance_key: null
  graph_version_after: ""
  fact_fingerprint_after: ""
failure:
  expected: false
  code: ""
  message: ""
  mutation_applied: null
evidence_files: []
notes: ""
```

For process restart and trace-reset steps, put the read command in `command`, use
`null` for non-applicable decision/instance guards, and attach both position
payloads. For a stopped run, leave all unexecuted later steps `not_run` and add
the first concrete failure to `observed_failures`.

## Stop Boundary

The implementation worker hands this checklist to the Operator and stops.
Checklist preparation is not acceptance execution. Keep
`acceptance_status: not_run` for caRtola, ASA, and MyFinance until the Operator
returns command outputs and completed evidence. Do not claim any of the three
passed, do not execute an external acceptance step, and do not start Task 19.
