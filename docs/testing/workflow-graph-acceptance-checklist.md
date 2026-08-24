# Workflow Graph Operator Acceptance Checklist

## Acceptance State

```yaml
acceptance_status: not_run
```

This document defines evidence to collect. It does not claim an acceptance run
has completed.

## Targets

The external repositories are:

- caRtola: `/Users/aaat/projects/caRtola`
- ASA: `/Users/aaat/projects/asa-deep-process-control-experiments`
- MyFinance: `/Users/aaat/myfinance`

The Operator owns all external repository reads and changes. AgileForge work
must not mutate a target repository unless the Operator explicitly performs and
records that action.

## Reviewed Runtime Pin

Use the reviewed checkout's launcher for every command:

```sh
export AGILEFORGE_WORKTREE="/Users/aaat/projects/agileforge/.worktrees/domain-workflow-graph-hard-break"
export AGILEFORGE_SHA="<reviewed-agileforge-sha>"
git -C "$AGILEFORGE_WORKTREE" rev-parse HEAD
```

The observed SHA must equal `AGILEFORGE_SHA`. Assign one fresh acceptance
profile per target:

- `acceptance-cartola`
- `acceptance-asa`
- `acceptance-myfinance`

The Specification v2 cutover does not migrate the former Discovery or
Specification tables. Each name above must resolve to a newly initialized
profile and empty business database tied to the reviewed SHA. Stop if a profile
contains records from a pre-v2 checkout.

Initialize and inspect the selected profile:

```sh
cd "$AGILEFORGE_WORKTREE"
./agileforge-dev init --profile "$ACCEPTANCE_PROFILE" \
  --mode acceptance --expect-sha "$AGILEFORGE_SHA" --json
./agileforge-dev info --profile "$ACCEPTANCE_PROFILE" \
  --secrets-file "$AGILEFORGE_SECRETS_FILE" --json
```

Record checkout identity, profile root, business database, ADK trace database,
model configuration, schema fingerprints, configured model roles, credential
presence, and UTC timestamp. Never record credential values.

The redacted preflight fields are `configured_models`, `provider_credentials`,
and `child_runtime_environment`. Supply provider configuration only with
`--secrets-file`; never capture credential values.

## Project Creation

Create one Project for the target repository:

```sh
./agileforge-dev cli --profile "$ACCEPTANCE_PROFILE" \
  --secrets-file "$AGILEFORGE_SECRETS_FILE" --json -- \
  project create \
  --name "$PROJECT_NAME" \
  --description "$PROJECT_DESCRIPTION" \
  --repository-path "$TARGET_REPOSITORY" \
  --idempotency-key "$PROJECT_CREATE_KEY" \
  --actor "$ACCEPTANCE_ACTOR"
```

Record the returned Project ID and repository binding.

## Command Protocol

Before each mutation, record:

```sh
./agileforge-dev cli --profile "$ACCEPTANCE_PROFILE" \
  --secrets-file "$AGILEFORGE_SECRETS_FILE" --json -- \
  workflow position --project-id "$PROJECT_ID"
./agileforge-dev cli --profile "$ACCEPTANCE_PROFILE" \
  --secrets-file "$AGILEFORGE_SECRETS_FILE" --json -- \
  workflow next --project-id "$PROJECT_ID"
```

For every advertised command:

1. Record the original command template.
2. Substitute only declared operator inputs.
3. Record the final argv without secrets.
4. Execute through the same launcher and profile.
5. Record the result and the next facts-only position.

AgileForge derives and validates internal guards from the current durable
position. Operators provide only task-specific semantic fields and transport
metadata such as idempotency key and actor. Do not enter raw JSON, file or
Markdown paths, candidate IDs, hashes, fingerprints, or lineage fields for
Specification authoring or review.

Use a distinct idempotency key for each distinct request. Stop when a fully
instantiated advertised command cannot parse or when a newly selected semantic
command is rejected as stale without another mutation changing the position.

## Lifecycle Evidence

For each target, collect evidence in this order:

1. Create the Project with the target repository path.
2. Complete and accept the Vision interview.
3. Complete and accept one Product Goal.
4. Perform any useful Discovery activities, such as `grill-with-docs`, research,
   repository inspection, ADR review, or a prototype. Record them only as source
   provenance; they are not an artifact or lifecycle gate. Materialize any
   model-facing result in an approved source file (`README.md`, `CONTEXT.md`,
   `pyproject.toml`, `specs/spec.json`, `specs/spec.md`, `docs/spec/spec.json`,
   or `docs/spec/spec.md`) and refresh the repository binding after the final
   write.
5. Execute the host-owned `specification source register` command advertised by
   `workflow next`.
6. Execute the advertised `specification structure` command and verify the
   review packet contains the complete canonical
   `agileforge.spec.v2` payload, deterministic Markdown, direct Vision and
   Product Goal lineage, source and producer evidence, attempt identity,
   amendment base and diff when applicable, and exact payload, view, and
   candidate fingerprints.
   Verify each source warning is nested under its own manifest entry.
7. Have the human accept, reject, or provide feedback on that exact
   Specification candidate.
8. Generate and accept Backlog using the exact accepted Specification.
9. Generate and accept Roadmap.
10. Generate and accept Stories.
11. Review Story dependencies and readiness.
12. Generate, review, and start a Sprint plan.
13. Complete Tasks and close Stories.
14. Review and close the Sprint.
15. Record post-Sprint triage.
16. Fulfill or abandon the Product Goal through the graph.

At each boundary record durable IDs, fingerprints, human decisions, model IDs
for model-backed nodes, and the before/after Workflow Position.

Confirm that no Discovery node, command, endpoint, or dashboard state appears.
Do not treat a Markdown or file projection as an accepted Specification.

## Target Notes

### caRtola

Use the repository as evidence for deterministic repository binding and a
provider-backed path through an accepted Specification. Record repository SHA and dirty
state before and after the run.

### ASA

Use the repository as evidence that structured technical specification content
can reach an accepted Specification and downstream planning without repository
mutation by AgileForge.

### MyFinance

Use "Statement Streams and Coverage" as the Operator-supplied feature context.
Use synthetic execution evidence in an isolated target test environment. Record
Backlog, Roadmap, Story, Sprint, Task, review, closure, and triage identities.

## Restart Proof

The production CLI is one-shot. For restart proof:

1. Record position in invocation A.
2. Let the process exit.
3. Recheck the reviewed SHA and profile `info --json`.
4. Record position in a fresh invocation B.
5. Compare fact fingerprint, graph version, decisions, instance keys, and node
   categories after excluding only read timestamps.

The business database and profile identity must remain unchanged.

## Trace Reset Proof

After a recorded position:

1. Remove only the profile-owned ADK execution-trace database using the
   launcher-supported procedure.
2. Keep the business database untouched.
3. Start a fresh CLI process.
4. Record the same Workflow Position.
5. Prove durable business facts and routing decisions did not change.

## Stale Position Proof

Choose one successful normal mutation:

1. Record the semantic command advertised by `workflow next`.
2. Execute it successfully.
3. Read `workflow position` and `workflow next` again in a fresh invocation.
4. Confirm the current response reflects the new durable position and selects
   the next semantic action.
5. Execute only the newly selected semantic command.
6. Confirm only the expected mutations were applied.

Public transports cannot inject internal guards. Low-level stale concurrency
belongs in automated domain tests.

## Distribution And Quality Evidence

Before external acceptance, record:

```sh
uv run --frozen pytest tests/test_single_lifecycle_absence.py \
  tests/test_prompt_package_resources.py \
  tests/adapters/test_agent_contract_boundaries.py \
  tests/adapters/test_production_runtime_cutover.py -q
uv run --frozen python scripts/verify_distribution.py
uv run --frozen ruff check .
uv run --frozen ty check
uv run --frozen ruff format --check .
git diff --check
```

## Stop Conditions

Stop and report without repair when:

- checkout SHA or profile provenance changes
- business and trace database ownership is ambiguous
- a required graph decision is absent
- an advertised command cannot parse
- a newly selected semantic command is rejected as stale before another
  mutation changes the position
- a provider result cannot pass the declared schema
- a pre-v2 profile or database is selected
- any transport requests raw Specification JSON, a file path, Markdown, an ID,
  a hash, a fingerprint, or lineage data from the human
- a Discovery artifact, gate, API, CLI command, or UI control appears
- downstream planning receives anything except the exact accepted typed v2 payload
- target repository state changes without Operator ownership
- credential values appear in captured evidence
