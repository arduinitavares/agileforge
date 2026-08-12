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

Use a new idempotency key for each distinct request. Reuse a key only to retry
the exact same request after an uncertain transport result.

## Specification And Authority Boundary

An accepted Product Goal enables host-owned `specification author`. AgileForge
captures the accepted Vision and Product Goal, source manifest, model and prompt
configuration, workflow attempt, and any accepted amendment base before it
invokes the configured `to-spec` producer. The producer returns a typed
`agileforge.spec.v2` payload. AgileForge validates and persists that payload
with an immutable envelope and renders the complete Markdown review view.

The operator does not supply raw JSON, a file path, Markdown, candidate IDs,
hashes, fingerprints, or lineage fields. Execute the exact `specification
author` template returned by `workflow next`.

`specification review` resolves the graph-selected immutable candidate. The
human supplies only the decision and rationale plus normal transport metadata.
Acceptance does not rewrite payload or envelope bytes. Authority compilation
then consumes the accepted typed payload, never the rendered Markdown. A
separate `authority review` and `authority decide` gate remains required before
Backlog work.

Discovery work such as `grill-with-docs`, research, interviews, repository
evidence, ADRs, or prototypes can contribute source provenance. There is no
Discovery artifact, lifecycle node, API route, CLI command, or UI state.

To use post-Goal source work, write the result to one approved repository file:
`README.md`, `CONTEXT.md`, `pyproject.toml`, `specs/spec.json`, `specs/spec.md`,
`docs/spec/spec.json`, or `docs/spec/spec.md`. Then run `repository refresh`
before `specification author`. Authoring captures a bounded snapshot and
rechecks it immediately before the provider call. A changed binding or file
makes the attempt stale; the provider never receives old source bytes. Review
source warnings under the manifest entry that produced them.

## Command Catalog

The current fixed request kinds map to these prefixes:

| Area | Command prefixes |
| --- | --- |
| Vision | `vision bootstrap`, `vision respond`, `vision status`, `vision review`, `vision revision` |
| Product Goal | `goal respond`, `goal review`, `goal complete`, `goal abandon` |
| Specification | `specification author`, `specification status`, `specification review` |
| Authority | `authority compile`, `authority feedback`, `authority decide`, `authority repair` |
| Backlog | `backlog generate`, `backlog decide` |
| Roadmap | `roadmap generate`, `roadmap decide` |
| Story | `story generate`, `story decide`, `story dependencies apply`, `story readiness repair`, `story close` |
| Sprint | `sprint generate`, `sprint decide`, `sprint start`, `sprint task complete`, `sprint review`, `sprint close`, `sprint triage` |

Registration does not imply availability. `workflow next` determines what can
run for the current facts.

## Read Surfaces

Reads never advance the workflow:

```sh
./agileforge-dev cli --profile local -- project list
./agileforge-dev cli --profile local -- project show --project-id 41
./agileforge-dev cli --profile local -- specification status --project-id 41
./agileforge-dev cli --profile local -- authority status --project-id 41
./agileforge-dev cli --profile local -- authority invariants --project-id 41
./agileforge-dev cli --profile local -- authority review --project-id 41
./agileforge-dev cli --profile local -- workflow position --project-id 41
./agileforge-dev cli --profile local -- workflow next --project-id 41
```

A read result is evidence, not permission to mutate.

## Agentic Nodes

The production recipe catalog contains:

```text
authority.compile
authority.repair
specification.author
vision.bootstrap
vision.interview
goal.interview
backlog.generate
planning.roadmap.generate
planning.story.generate
planning.sprint.plan
```

Each recipe receives host-normalized input, invokes one configured leaf, validates
structured output, and returns a positioned transition request. Recipes do not
read persistence or choose graph routes.

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
