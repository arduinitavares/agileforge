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

An accepted Product Goal enables `specification source register`, not direct
authoring. An external agent performs optional Discovery through
`grill-with-docs`, updates `CONTEXT.md` when terminology is resolved, creates
ADRs only when warranted, and runs `to-spec` to produce a human-readable source
Specification. `grill-with-docs` is provenance attestation; AgileForge does not
claim to prove the agent's internal reasoning.

Register the exact source and applicable ADR paths using the command returned by
`workflow next`:

```sh
./agileforge-dev cli --profile local -- specification source register \
  --project-id 41 \
  --source-path specs/product-specification.md \
  --preparation-capability grill-with-docs \
  --adr-path docs/adr/0004-register-to-spec-source-before-structuring.md \
  --idempotency-key specification-source-41-1 \
  --actor operator
```

AgileForge atomically captures exact bytes, the accepted Vision fingerprint,
active Product Goal fingerprint, repository binding and revision, an explicit
`CONTEXT.md` present/absent state, and applicable ADR fingerprints. Once the
source is registered, execute the advertised `specification structure` command:

```sh
./agileforge-dev cli --profile local -- specification structure \
  --project-id 41 \
  --idempotency-key specification-structure-41-1 \
  --actor operator
```

The internal Specification Structuring Agent receives the captured Vision,
Goal, exact source, present Context, ADRs, repository evidence, pinned amendment
base, and prior human feedback when applicable. It returns semantic content
through the closed `agileforge.spec.v2` contract. AgileForge owns identities,
canonical ordering, hashes, lineage, timestamps, persistence, and rendering.

`specification review` resolves the graph-selected immutable candidate. The
human supplies only the decision and rationale plus normal transport metadata.
Acceptance does not rewrite payload or envelope bytes. Authority compilation
then consumes only the accepted typed clauses, never source Markdown,
`CONTEXT.md`, ADR or repository prose, or rendered Markdown. A
separate `authority review` and `authority decide` gate remains required before
Backlog work.

There is no Discovery artifact, lifecycle node, API route, CLI command, or UI
state. `CONTEXT.md` is optional because domain modeling creates it lazily.
Source, Context, ADR, Vision, Goal, repository, or binding drift invalidates the
attempt; read `workflow next` again and register a new immutable source.

## Command Catalog

The current fixed request kinds map to these prefixes:

| Area | Command prefixes |
| --- | --- |
| Vision | `vision bootstrap`, `vision respond`, `vision status`, `vision review`, `vision revision` |
| Product Goal | `goal respond`, `goal review`, `goal complete`, `goal abandon` |
| Specification | `specification source register`, `specification structure`, `specification status`, `specification review` |
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
specification.structure
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
