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

Use the checkout's locked Python 3.13.15 environment and the same `pyrepo-check`
controller pinned in `.github/workflows/ci.yml`. Run from that checkout's root.
`pyrepo-check` provisions and invokes the repository Python through uv; the full
gate remains `./agileforge-dev check` (`sh ./agileforge-dev check` in PowerShell).
Quality checks do not require an initialized operator profile. Before runtime
mutations, inspect the selected existing profile with
`./agileforge-dev info --profile <name> --json`.

### Everyday feedback

Select the owning tests, then expand to their callers when a contract changes:

```sh
# Pure graph decisions, fingerprints, ordering and guard properties.
pyrepo-check --python 3.13.15 pytest tests/workflow/test_graph_properties.py

# Database fixture ownership and canonical fingerprint compatibility.
pyrepo-check --python 3.13.15 pytest tests/test_db_tools.py tests/test_agent_workbench_fingerprints.py tests/workflow/test_fingerprints.py

# One persisted retry lifecycle, including reloads and stale actions.
pyrepo-check --python 3.13.15 pytest tests/workflow/test_sprint_retry_execution.py::test_multistory_retries_preserve_history_reload_and_reject_stale_actions

# File-oriented checks for the changed Python files.
pyrepo-check --python 3.13.15 ruff annotations ty bandit workflow/fingerprints.py tests/conftest.py
```

These are focused selections. The pure graph file does not exercise database
transactions, API adapters, browser behavior, packaged installation or process
ownership. A changed fixture, shared hash, persistence contract or runtime
launcher needs its regression tests and the full gate. Focused coverage reports
are guidance, not a full-suite coverage result.

### Full validation

```sh
./agileforge-dev check
```

This still runs lock validation, the canonical Python quality checks and pytest,
the registered Node suites, whitespace validation and isolated distribution
verification in the existing order. Default pytest selection still excludes
`integration` and blocks external sockets. No timeout, platform marker or
required check is relaxed by the performance changes.

The real launcher smoke checks require a clean checkout. Commit the candidate locally before this gate; uncommitted changes cause acceptance-mode initialization to fail. Run it before requesting final review or merging, after changes to shared test
fixtures or workflow authority, and when focused results leave affected callers
uncertain. Follow the separate operator acceptance checklist when real provider
or product acceptance is needed; this gate does not perform those workflows.

### Isolation and the optimization boundary

The autouse database guard is installed for every test. Its getters request the
function-scoped `engine` fixture on demand; an explicit `engine` or `session`
argument also constructs it normally. All requests in that test resolve to the
same fixture. A per-test lock also serializes concurrent first requests from
background or TestClient threads. Pure tests no longer build an unused schema.
Each requested engine
still creates a new in-memory SQLite database and the complete current schema.
No rows, engines or mutable schema snapshots are shared between tests.

The fixture's connection listener belongs to its engine. It no longer adds a
listener to the global `Engine` class for every test. Cleanup disposes the owned
in-memory pool in `finally`, including after setup failure, instead of issuing
DROP statements and disabling foreign keys. File-backed persistence, migration,
rollback and process-ownership tests keep their existing real resources.

Canonical normalization returns exact built-in JSON scalar values before
checking the more expensive datetime and container protocols. Subclasses keep
the previous dispatch path. Key-collision rejection, datetime conversion,
canonical bytes and SHA-256 inputs are unchanged. There is no cache of mutable
workflow facts or validation outcomes and no parallel pytest execution.

### Reproducing measurements

Retain the complete output in a new file for each run. Pytest reports every
setup, call and teardown duration through the repository's `--durations=0
--durations-min=0` configuration. This adds reporting without changing selection.
Sum by phase and module to see cumulative costs; the runner's separate top-ten
summary alone cannot establish where the suite spends all its time.

On Linux, for example:

```sh
/usr/bin/time -p ./agileforge-dev check > canonical-unique-run.log 2>&1
/usr/bin/time -p pyrepo-check --python 3.13.15 pytest tests/workflow/test_graph_properties.py > focused-unique-run.log 2>&1
```

Keep the exit code, commit and diff, platform and filesystem, CPU/memory,
Python/dependency/controller versions, exact command and selected node IDs.
Record whether dependencies and browser binaries are already installed. Run
one benchmark workload at a time, with fresh Python processes and fresh test
databases; compare warm dependency caches separately from environment setup.
Measure pytest time and total command wall time separately. The full gate also
performs checks and package verification outside pytest.

Timing budgets and the before/after measurements for issue #265 are recorded in
the retained validation report. They are investigation thresholds for the
measured environment, not test timeouts or a universal CI service-level promise.
The historical Windows runs of 5,547.66 and 6,106.09 pytest seconds used different
revisions and are not a controlled before/after comparison. Linux development
and CI measurements must remain separate from them. Container and platform
cutover work belongs to issue #263.

See [CONTEXT.md](CONTEXT.md) for domain language,
[docs/agent-cli-manual.md](docs/agent-cli-manual.md) for the command contract,
and
[docs/testing/workflow-graph-acceptance-checklist.md](docs/testing/workflow-graph-acceptance-checklist.md)
for operator acceptance evidence. The active synthetic String Calculator
dogfooding campaign follows
[docs/testing/string-calculator-dogfooding-plan.md](docs/testing/string-calculator-dogfooding-plan.md).
