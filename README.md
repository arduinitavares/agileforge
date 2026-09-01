# AgileForge

[![Python 3.13.15](https://img.shields.io/badge/python-3.13.15-blue.svg)](https://www.python.org/downloads/)
[![Google ADK](https://img.shields.io/badge/Google-ADK-orange.svg)](https://github.com/google/adk-python)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

AgileForge is a developer tool for agent-assisted product planning and
execution governed by one human-accepted Specification.

## Lifecycle

One durable Project owns one ordered lifecycle:

```text
Vision -> Product Goal -> Source Registration -> Specification Structuring
       -> Specification Review -> Backlog -> Roadmap
       -> Stories -> Sprint -> Execution -> Triage
```

After a human accepts a Product Goal, an external agent may run
`grill-with-docs`, update the lazily created `CONTEXT.md`, record applicable
ADRs, and run `to-spec`. It registers that exact human-readable source with
AgileForge. AgileForge captures its bytes, the present-or-absent Context state,
ADRs, accepted Vision and Goal fingerprints, and repository revision before an
internal Specification Structuring Agent creates canonical
`agileforge.spec.v2`. A human reviews that exact candidate. The accepted payload
and immutable lineage then bind every downstream planning artifact directly.

Discovery is optional work such as interviews, `grill-with-docs`, research,
repository evidence, ADRs, and prototypes. `grill-with-docs` is preparation
attestation, not proof of an external agent's internal reasoning. Discovery may
contribute to a Specification, but AgileForge does not persist an artifact or
expose a Discovery workflow gate, API, CLI command, or dashboard card. Markdown
and registered source files are evidence for structuring, not downstream
delivery contracts. Delivery consumes only the accepted typed
`agileforge.spec.v2` payload.

`WorkflowDomain.position(project_id)` derives available, waiting, blocked,
invalid, or terminal nodes from durable Project facts. Commands submit typed
requests through `WorkflowDomain.transition(request)`. ADK recipes execute
eligible model work; they do not own routing state.

The current model-backed nodes cover Vision and Product Goal interviews,
Specification structuring, Backlog, Roadmap, Story, and Sprint generation.
Human Specification review remains an explicit workflow transition.

## Architecture

- **Derived workflow graph**: one immutable fact snapshot drives routing and
  transition guards.
- **Accepted Specification**: exact reviewed v2 bytes constrain downstream
  planning and execution.
- **Canonical Specification**: a typed v2 payload contains semantics while an
  immutable envelope binds direct Vision and Product Goal lineage, the exact
  registered `to-spec` source, source preparation attestation, structurer and
  attempt identity, amendment base and diff, and exact
  payload, review-view, and candidate fingerprints.
- **Durable Project facts**: process restarts and execution-trace resets do not
  alter workflow position.
- **Schema validation**: Pydantic and SQLModel define transport and persistence
  boundaries.
- **Repository binding**: a Project may record an operator-selected repository
  path and deterministic repository observations.

## Quick Start

Prerequisites: Python 3.13.15, [uv](https://docs.astral.sh/uv/), and an OpenRouter
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

Execute the command template returned by `workflow next`. After the external
agent produces its source, the graph advertises `specification source register`;
after immutable capture, it advertises `specification structure`. AgileForge
derives and validates internal guards from the current durable position.
Operators provide only task-specific semantic fields and transport metadata
such as idempotency key and actor. Source registration takes
repository-relative source and applicable ADR paths. Humans never enter raw
workflow JSON, candidate IDs, fingerprints, or lineage identifiers for
structuring or review. Use a new idempotency key for each distinct request.

The Specification v2 cutover is a hard break. Initialize a fresh profile and
business database when moving from a checkout that used the former Discovery or
Specification schema; AgileForge does not migrate or read those records.

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
for operator acceptance evidence. The active synthetic String Calculator
dogfooding campaign follows
[docs/testing/string-calculator-dogfooding-plan.md](docs/testing/string-calculator-dogfooding-plan.md).
