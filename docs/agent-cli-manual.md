# AgileForge Agent CLI Manual

The CLI is JSON-first. `WorkflowDomain` is the sole routing authority. Read the
current graph position and execute the exact command template it advertises.

## Runtime Selection

Stable release:

```sh
agileforge workflow next --project-id 1
```

Current checkout:

```sh
./agileforge-dev init --profile local --json
./agileforge-dev info --profile local --json
./agileforge-dev cli --profile local -- workflow next --project-id 1
```

Stable release: `agileforge workflow next --project-id 1`

Current checkout: `./agileforge-dev cli --profile local -- workflow next --project-id 1`

Current checkout UI: `./agileforge-dev ui --profile local --port auto`

Provenance: `./agileforge-dev info --profile local --json`

In a branch or linked worktree, use that checkout's `./agileforge-dev`.
`info --json` reports the checkout, profile, databases, `configured_models`,
`provider_credentials`, and `child_runtime_environment` without exposing
credential values.

The `agileforge.spec.v2` cutover is a hard break. Create a fresh profile when
the checkout changes from the former Discovery or Specification persistence
schema. The runtime does not migrate or read those records.

## Vision Bootstrap Development

Use a fresh profile and database for manual Vision bootstrap development:

```sh
./agileforge-dev init --profile vision-bootstrap-manual --json
./agileforge-dev ui --profile vision-bootstrap-manual --port auto
./agileforge-dev info --profile vision-bootstrap-manual --json
```

Inspect the reported provenance before any manual mutation. Invoke the
grounded Vision lifecycle through semantic CLI commands only:

```sh
./agileforge-dev cli --profile vision-bootstrap-manual -- vision bootstrap \
  --project-id 1 \
  --idempotency-key vision-bootstrap-1 \
  --actor operator
./agileforge-dev cli --profile vision-bootstrap-manual -- vision respond \
  --project-id 1 \
  --text "Keep the evidence-grounded direction." \
  --idempotency-key vision-respond-1 \
  --actor operator
./agileforge-dev cli --profile vision-bootstrap-manual -- vision status --project-id 1
./agileforge-dev cli --profile vision-bootstrap-manual -- vision review \
  --project-id 1 \
  --decision accepted \
  --rationale "The evidence and direction are correct." \
  --idempotency-key vision-review-1 \
  --actor operator
```

The operator owns manual acceptance and makes the acceptance decision. Automated
tests use temporary fixtures only; they never use the manual profile or a real
repository.

## Create A Project

Project creation is the only command used before graph position exists:

```sh
./agileforge-dev cli --profile local -- project create \
  --name "Example Project" \
  --description "Optional product context" \
  --repository-path "/absolute/path/to/repository" \
  --idempotency-key project-create-1 \
  --actor operator
```

`--description` and `--repository-path` are optional. Name, idempotency key,
and actor are required.

## Routing Reads

Run these reads before every workflow mutation:

```sh
./agileforge-dev cli --profile local -- workflow position --project-id 41
./agileforge-dev cli --profile local -- workflow next --project-id 41
```

`workflow position` returns the complete typed position.
`workflow next` returns required and recovery decisions with executable command
templates. A decision includes:

- node and optional instance identity
- request kind and reason code
- required operator inputs
- exact command template

Do not infer a command from the visible artifact state or from a previous
position.

## Mutation Contract

A positioned command carries semantic inputs and transport metadata:

```text
--project-id
--instance-key
--idempotency-key
--actor
--correlation-id
```

Use `--instance-key` only when the returned template includes it. Model-backed
commands take normalized input. Deterministic commands take their declared
semantic fields. AgileForge derives and validates internal guards from the
current durable position. Operators provide only task-specific semantic fields
and transport metadata such as idempotency key and actor.

Story dependency confirmation additionally carries the
`--selected-scope-fingerprint` shown by the current dependency projection. This
is the operator-observed scope being confirmed, not an internal graph guard.
AgileForge rejects it if selection or structural evidence changed before the
mutation.

Use a new idempotency key for each distinct request. Reuse a key only to retry
the exact same request after an uncertain transport result.

## Accepted Specification Boundary

An accepted Product Goal enables `specification source register`, not direct
authoring. An external agent may perform optional Discovery, update
`CONTEXT.md`, create warranted ADRs, and run `to-spec` to produce a
human-readable source. Register the exact source and applicable ADR paths using
the command returned by `workflow next`:

```sh
./agileforge-dev cli --profile local -- specification source register \
  --project-id 41 \
  --source-path specs/product-specification.md \
  --preparation-capability grill-with-docs \
  --adr-path docs/adr/0005-use-accepted-specification-as-delivery-contract.md \
  --idempotency-key specification-source-41-1 \
  --actor operator
```

After capture, execute the advertised structuring command:

```sh
./agileforge-dev cli --profile local -- specification structure \
  --project-id 41 \
  --idempotency-key specification-structure-41-1 \
  --actor operator
```

The structurer receives captured Vision, Goal, source, Context, ADR, repository,
amendment, and prior-feedback evidence. It returns the closed
`agileforge.spec.v2` payload. AgileForge owns canonical identities, ordering,
hashes, lineage, persistence, and rendering.

`specification review` resolves the graph-selected immutable candidate. Human
acceptance does not rewrite payload or envelope bytes. The exact accepted
Specification then binds Backlog, Roadmap, Story, Sprint, Task, and execution
artifacts directly. There is no intermediate compiled contract or separate
review gate.

## Command Catalog

The current fixed request kinds map to these prefixes:

| Area | Command prefixes |
| --- | --- |
| Vision | `vision bootstrap`, `vision respond`, `vision status`, `vision review`, `vision revision` |
| Product Goal | `goal status`, `goal respond`, `goal review`, `goal complete`, `goal abandon` |
| Specification | `specification source register`, `specification structure`, `specification status`, `specification review` |
| Backlog | `backlog generate`, `backlog decide` |
| Roadmap | `roadmap generate`, `roadmap decide` |
| Story | `story generate`, `story decide`, `story dependencies apply`, `story readiness repair`, `story close` |
| Sprint | `sprint generate`, `sprint decide`, `sprint retry-preview`, `sprint retry`, `sprint start`, `sprint task complete`, `sprint review`, `sprint close`, `sprint triage` |

Registration does not imply availability. `workflow next` determines what can
run for the current facts.

## Task completion checklist input

Use exactly one checklist source when recording Task completion. For ordinary
entries, repeat `--checklist-item KEY=VALUE`; only the first `=` separates the
key from its result, so `check=expected=actual` records `check` with result
`expected=actual`. Results are arbitrary nonblank strings, including `met` and
`failed`; a result word does not determine Task acceptance.

You can also use explicit 1-based indexes in the Task's stored checklist order.
For checklist `("Run focused tests", "Confirm A=B=C")`, this source records the
exact keys with results `met` and `observed=A=B=C`:

```sh
--checklist-item 1=met --checklist-item 2=observed=A=B=C
```

Indexes must be canonical ASCII positive decimals within the checklist's bounds.
Zero, signs, leading zeros, decimal points, Unicode digits, duplicate indexes,
and mixed text/index shorthand are rejected. An omitted valid index is passed
to the completion service, which rejects incomplete coverage; the CLI never
fills missing results.

Complete exact-text coverage retains literal semantics even when checklist names
look numeric. If the same complete input also has a full index reading and the
resolved dictionaries differ, it is ambiguous and rejected. For checklist
`("2", "1")`, `1=a,2=b` is ambiguous; equal results produce the same dictionary
and are accepted. Checklist `("1", "2")` has identical literal and index
readings. For `("Run tests", "1")`, `1=met,2=met` collides with the literal
item `"1"` and is rejected, while complete literal input
`"Run tests=failed", "1=met"` remains supported. Partial literal coverage never
falls back from a collision to another reading.

Use `--checklist-file PATH` for numeric collisions or when a checklist key
contains `=`. File keys always mean exact text, including numeric keys:

```json
{
  "Run --ignore=tests/e2e": "exit=0",
  "Check A=B=C": "observed=A=B=C"
}
```

```sh
./agileforge-dev cli --profile local -- sprint task complete \
  --project-id 41 \
  --instance-key task:7 \
  --outcome-summary "Implemented the requested behavior." \
  --artifact-ref services/example.py \
  --acceptance-result fully_met \
  --checklist-file checklist.json \
  --idempotency-key complete-task-41-1 \
  --actor operator
```

The file must be valid UTF-8 JSON containing a nonempty object of nonblank
string keys and values. Both keys and values are trimmed for the same semantic
normalization as `--checklist-item`. Duplicate keys, including escaped and
whitespace-normalized duplicates, are rejected. Do not mix the file option with
`--checklist-item` or repeat `--checklist-file`. Include one result for every
required Task checklist item and verify each item before submitting. Replay the
identical completion with the same idempotency key and the same checklist
payload. Exact-text, index, and file input with the same resolved dictionary
replay under the same key when all other fields, actor, and correlation ID stay
the same. Retained Task metadata allows indexed replay after completion, a
closed Sprint, or a later active Sprint. Changed results or other semantic
fields conflict; do not change, omit, or add entries on replay.

If a payload is rejected, correct it and use a fresh idempotency key because
rejected receipts are durable. In particular, an indexed attempt rejected by
an older CLI stored literal keys such as `{"1": "met"}`. Retrying through the
new index normalization with that same key conflicts instead of replaying;
use a fresh key for the corrected completion.

Checklist coverage errors list the accepted Task items with 1-based numbers in
their stored order. Malformed `--checklist-item` pairs and duplicate normalized
keys can include the same list when an injected application's scoped Task read
is available; otherwise the original argument error is preserved. Diagnostics
show at most 20 items and 160 characters per item, using `…` for truncated text
and `… and N more` for omitted items. Copy the exact required text, including
internal spacing, when correcting a payload. These diagnostics do not relax
coverage validation: argument and index errors exit 2 without recording
completion, while coverage errors exit 1 and retain the existing durable
rejection receipt. Numeric inline sources require the selected Task's scoped
metadata read; failed reads return an error without guessing checklist names.

### Post-Sprint triage payloads

Use exactly one payload source when recording triage for the advertised Sprint
instance. For `--impact none`, inline input is equivalent to a file containing
`{"summary": "No downstream change."}`:

```sh
./agileforge-dev cli --profile local -- sprint triage \
  --project-id 41 --instance-key sprint:31 --impact none \
  --summary "No downstream change." \
  --idempotency-key triage-41-1 --actor operator

./agileforge-dev cli --profile local -- sprint triage \
  --project-id 41 --instance-key sprint:31 --impact none \
  --file triage.json \
  --idempotency-key triage-41-1 --actor operator
```

The inline summary must be nonblank. Accepted text retains surrounding
whitespace, Unicode, quotes, newlines, and `=` exactly. The payload contains
only the `summary` field. `backlog` and `specification` impacts require
`--file`; file input retains its existing JSON-object validation and contents
for every impact, without an added summary requirement.

Neither source, both sources, or repeated occurrences of either option exit 2
before triage dispatch. Repeated `--file` is now intentionally rejected rather
than silently using the final path. File and inline input with the same object
replay under the same idempotency key when project, instance, impact, actor,
and correlation ID also remain identical. Changed text or metadata conflicts
under that key. Reusing the same payload with a fresh key remains a rejected
correction attempt rather than a replay.

## Read Surfaces

Reads never advance the workflow:

```sh
./agileforge-dev cli --profile local -- project list
./agileforge-dev cli --profile local -- project show --project-id 41
./agileforge-dev cli --profile local -- specification status --project-id 41
./agileforge-dev cli --profile local -- workflow position --project-id 41
./agileforge-dev cli --profile local -- workflow next --project-id 41
```

A read result is evidence, not permission to mutate.

### Product Goal interview questions

`goal status --project-id 41` returns `data.effective_questions` through CLI JSON,
the Goal status HTTP endpoint, and the dashboard Goal slot. When an accepted
Vision has a healthy Goal interview context, this object contains an ordered
`questions` list and a `source` from the closed set `{builtin_starter, generated}`.

`generated` preserves the selected durable interview chain's latest questions.
`builtin_starter` supplies these questions when that chain has no questions yet,
including a reopened Goal after feedback or rejection and the next Goal after
resolution:

1. What valuable outcome should this Project achieve next?
2. What observable result will prove success?
3. What boundary keeps this Goal focused?

The field is `null` when there is no accepted Vision, a Goal is active, a
candidate awaits review, or selected facts or candidate projection conflict.
Workflow-advertised actions still determine whether a response form is available.

Status reads do not call a provider, save questions, or fabricate transcript
turns. `latest_questions` continues to expose only the last selected persisted
turn's questions, or `[]` for a chain without turns. Starters can coexist with a
nonempty transcript only for invalid history whose latest questions are empty;
the fallback preserves the display behavior without repairing that history.

## Sprint Retry

Run Sprint retry work from the executor-prepared branch or worktree and its
profile. Retry does not choose or change a branch, manage a worktree, rebind a
repository, or require the original attempt's branch. Start with the live
position and next actions; retry is available only when `workflow next`
advertises it for the latest eligible completed Sprint. Do not substitute an
older Sprint ID or infer a retry binding from a previous attempt.

Preview the exact source Sprint before requesting a retry. The preview is a
read: it lists the retained Sprint scope and blockers, then returns the
`expected_state_fingerprint` to confirm.

```sh
./agileforge-dev cli --profile local -- sprint retry-preview \
  --project-id 41 \
  --sprint-id 12
```

If the preview has no blockers, explicitly confirm the exact observed state.
Use a rationale that records the fresh evidence requiring another attempt.

```sh
./agileforge-dev cli --profile local -- sprint retry \
  --project-id 41 \
  --sprint-id 12 \
  --confirm \
  --expected-state-fingerprint 'sha256:...' \
  --rationale "New validation evidence requires the Sprint to be repeated." \
  --actor operator \
  --idempotency-key sprint-12-retry-1
```

The request is rejected when confirmation is absent or false, the Sprint is
unknown or not the current eligible source, or its fingerprint is stale. Read a
new preview and `workflow next` after such a failure; use the advertised action
and a new idempotency key for a changed request.

Creating a retry only prepares its new attempt. Read `workflow next` again and
use the provided retry-bound start identity exactly. `--instance-key` is required
for this advertised retry start and has the form
`retry:<attempt-id>:sprint:<source-sprint-id>`. An unbound original `sprint
start` keeps its existing selectorless behavior.

```sh
./agileforge-dev cli --profile local -- sprint start \
  --project-id 41 \
  --instance-key retry:7:sprint:12 \
  --idempotency-key sprint-12-retry-1-start \
  --actor operator
```

The retry uses the original accepted requirements but requires fresh execution
evidence. Sprint status, tasks, task detail and history, review, and Sprint
history show the current retry progress and its exact bindings. Original starts,
task and Story completion evidence, review, closure, triage, and timeline facts
remain available as source-attempt history. Canonical Story and Task packets
remain the accepted requirement and source-execution snapshots; use workflow
position/next and the scoped Sprint and Task reads for current retry progress
and action identities.

## Agentic Nodes

The production recipe catalog contains:

```text
specification.structure
vision.bootstrap
vision.interview
goal.interview
backlog.generate
planning.roadmap.generate
planning.story.generate
planning.sprint.plan
```

Each recipe receives host-normalized input, invokes one configured leaf,
validates structured output, and returns a positioned transition request.
Recipes do not read persistence or choose graph routes.

## Stale Position Recovery

When a mutation reports that the workflow position changed:

1. Stop using the previously selected command.
2. Read `workflow position` and `workflow next` again.
3. Review the current reason code and available semantic action.
4. Execute only the newly selected semantic command with its task-specific
   fields and a fresh idempotency key.

Public transports cannot inject internal guards. Low-level stale concurrency
belongs in automated domain tests.

## Provider Configuration

Pass provider secrets only through an operator-owned regular file:

```sh
./agileforge-dev info --profile local \
  --secrets-file "$HOME/.config/agileforge/provider.env" --json
./agileforge-dev cli --profile local \
  --secrets-file "$HOME/.config/agileforge/provider.env" -- \
  workflow next --project-id 41
```

The launcher accepts only its allowlisted provider variables and does not print
credential values.
