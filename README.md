# AgileForge

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Google ADK](https://img.shields.io/badge/Google-ADK-orange.svg)](https://github.com/google/adk-python)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

AgileForge is a developer tool for agent-assisted product planning and
execution governed by reviewed specification authority.

## Lifecycle

One durable Project owns one ordered lifecycle:

```text
Vision -> Product Goal -> Discovery -> Specification -> Authority
       -> Backlog -> Roadmap -> Stories -> Sprint -> Execution -> Triage
```

`WorkflowDomain.position(project_id)` derives available, waiting, blocked,
invalid, or terminal nodes from durable Project facts. Commands submit typed
requests through `WorkflowDomain.transition(request)`. ADK recipes execute
eligible model work; they do not own routing state.

The current model-backed nodes cover Vision and Product Goal interviews,
authority compilation and repair, Backlog, Roadmap, Story, and Sprint
generation. Human review decisions remain explicit workflow transitions.

## Architecture

- **Derived workflow graph**: one immutable fact snapshot drives routing and
  transition guards.
- **Specification authority**: reviewed compiler output constrains downstream
  planning and execution.
- **Durable Project facts**: process restarts and execution-trace resets do not
  alter workflow position.
- **Schema validation**: Pydantic and SQLModel define transport and persistence
  boundaries.
- **Repository binding**: a Project may record an operator-selected repository
  path and deterministic repository observations.

## Quick Start

Prerequisites: Python 3.12+, [uv](https://docs.astral.sh/uv/), and an OpenRouter
key only when running model-backed nodes.

```sh
git clone https://github.com/arduinitavares/agileforge.git
cd agileforge
uv sync --frozen
./agileforge-dev init --profile local --json
./agileforge-dev info --profile local --json
```

Create one Project:

```sh
./agileforge-dev cli --profile local -- project create \
  --name "Example Project" \
  --description "Validated delivery workflow" \
  --repository-path "/absolute/path/to/repository" \
  --idempotency-key project-create-1 \
  --actor operator
```

Read graph authority before every workflow mutation:

```sh
./agileforge-dev cli --profile local -- workflow position --project-id 1
./agileforge-dev cli --profile local -- workflow next --project-id 1
```

Stable release: `agileforge workflow next --project-id 1`

Current checkout: `./agileforge-dev cli --profile local -- workflow next --project-id 1`

Current checkout UI: `./agileforge-dev ui --profile local --port auto`

Provenance: `./agileforge-dev info --profile local --json`

Execute the command template returned by `workflow next`. AgileForge derives and
validates internal guards from the current durable position. Operators provide
only task-specific semantic fields and transport metadata such as idempotency key
and actor. Use a new idempotency key for each distinct request.

Start the checkout-local dashboard:

```sh
./agileforge-dev ui --profile local --port auto
```

Provider-backed launcher children ignore the checkout `.env`. Pass an
operator-owned regular secrets file explicitly:

```sh
export AGILEFORGE_SECRETS_FILE="$HOME/.config/agileforge/provider.env"
./agileforge-dev info --profile local --secrets-file "$AGILEFORGE_SECRETS_FILE" --json
./agileforge-dev cli --profile local --secrets-file "$AGILEFORGE_SECRETS_FILE" -- workflow next --project-id 1
```

Credential values are not included in launcher output. The checkout launcher
owns its profile database, trace database, model configuration, and child
runtime environment. The redacted preflight reports `configured_models`,
`provider_credentials`, and `child_runtime_environment`; it never reports
credential values.

## Development

Use only the current checkout's `./agileforge-dev` in branches and linked
worktrees. Record `info --json` before mutations.

```sh
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen ty check
uv run --frozen ruff format --check .
```

See [CONTEXT.md](CONTEXT.md) for domain language,
[docs/agent-cli-manual.md](docs/agent-cli-manual.md) for the command contract,
and
[docs/testing/workflow-graph-acceptance-checklist.md](docs/testing/workflow-graph-acceptance-checklist.md)
for operator acceptance evidence.
