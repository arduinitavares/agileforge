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

## Command Catalog

The current fixed request kinds map to these prefixes:

| Area | Command prefixes |
| --- | --- |
| Vision | `vision respond`, `vision review`, `vision revision` |
| Product Goal | `goal respond`, `goal review`, `goal complete`, `goal abandon` |
| Discovery | `discovery record` |
| Specification | `specification record`, `specification review` |
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
