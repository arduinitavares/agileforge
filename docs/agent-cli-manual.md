# AgileForge Agent CLI Manual

This is the current operational contract for the `agileforge` command-line
interface. The CLI is JSON-first. Parse its JSON output and use the exact
commands advertised by the workflow graph.

## Runtime Selection

Stable release: `agileforge workflow next --project-id 1`

Current checkout: `./agileforge-dev cli --profile local -- workflow next --project-id 1`

Current checkout UI: `./agileforge-dev ui --profile local --port auto`

Provenance: `./agileforge-dev info --profile local --json`

Use only uv for repository setup. Run `uv sync --frozen`, then initialize one
isolated development profile with
`./agileforge-dev init --profile local --json`. In a development branch or
linked worktree, run that checkout's launcher and record `info --json` before
mutations. The installed stable release remains a separate runtime.

One info command provides the complete redacted preflight:

```sh
./agileforge-dev info --profile local --secrets-file /path/to/provider.env --json
```

Omit `--secrets-file` when no provider source is needed. The JSON includes typed
model roles and IDs in `configured_models`, presence booleans in
`provider_credentials`, and the exact derived non-secret values in
`child_runtime_environment`. It never emits credential values. Acceptance
profiles additionally refuse tracked changes or nonignored untracked source at
initialization and every use.

## Routing Authority

`WorkflowDomain` is the sole routing authority. It derives a Project's current
position from durable facts and returns the decisions available at that
position. A caller does not select a phase or reconstruct routing state.

Use these reads before every mutation:

```sh
./agileforge-dev cli --profile local -- workflow position --project-id 41
./agileforge-dev cli --profile local -- workflow next --project-id 41
```

`workflow position` returns the complete typed position. Add
`--include-optional` when optional re-entry decisions are needed.

`workflow next` returns only required and recovery decisions. Each command in
its `commands` array is authored by the graph and contains:

- `node_id` and optional `instance_key`
- `request_kind`
- `reason_code`
- `decision_fingerprint`
- an executable command template

The response also carries `graph_version` and `fact_fingerprint`. Treat all
three fingerprints as one read snapshot.

## Project Shell

Open a new Project Shell before it has a graph position:

```sh
./agileforge-dev cli --profile local -- project create \
  --name "Example Project" \
  --origin greenfield \
  --idempotency-key project-41-open \
  --changed-by operator
```

Allowed origins are `greenfield` and `brownfield`. After creation, use
`workflow next` to obtain the first graph-owned action. `project abandon` is a
positioned graph mutation and must be executed only when advertised.

## Graph Command Contract

Every positioned mutation uses the graph guards returned by the same position
read:

```text
--project-id
--graph-version
--expected-fact-fingerprint
--expected-decision-fingerprint
--instance-key                  # required only when advertised
--idempotency-key
--changed-by
--correlation-id                # optional
```

Deterministic mutations take `--request-file`. Agentic mutations take
`--input-file` and optionally `--model-id`. Do not convert one payload form into
the other.

Example of the command shape returned for an agentic authority compilation:

```sh
./agileforge-dev cli --profile local -- authority compile \
  --project-id 41 \
  --graph-version agileforge.workflow.v1 \
  --expected-fact-fingerprint <fact-fingerprint> \
  --expected-decision-fingerprint <decision-fingerprint> \
  --input-file authority-input.json \
  --idempotency-key authority-compile-41 \
  --changed-by operator
```

Always execute the returned template. Do not infer a command from `node_id`,
the previous command, or a Project's visible artifacts.

## Fixed Mutation Catalog

The live graph maps its fixed request kinds to these command prefixes:

| Area | Graph-authored command prefixes |
| --- | --- |
| Project | `project abandon` |
| Initial scope | `scope register` |
| Greenfield discovery | `discovery challenge record`, `discovery prd record`, `discovery prd decide`, `discovery spec record`, `discovery spec decide` |
| Brownfield discovery | `brownfield baseline record`, `brownfield inventory record`, `brownfield curate`, `brownfield spec decide` |
| Authority | `authority compile`, `authority feedback`, `authority decide`, `authority repair` |
| Vision | `vision generate`, `vision decide` |
| Backlog | `backlog generate`, `backlog decide`, `backlog reconcile` |
| Roadmap | `roadmap generate`, `roadmap decide` |
| Story | `story generate`, `story decide`, `story dependencies apply`, `story readiness repair`, `story close` |
| Sprint | `sprint generate`, `sprint review`, `sprint decide`, `sprint start`, `sprint task complete`, `sprint triage`, `sprint close` |
| Scope extension | `scope extension start`, `scope extension challenge record`, `scope extension prd record`, `scope extension prd decide`, `scope extension spec record`, `scope extension spec decide`, `scope extension register`, `scope extension reconcile`, `scope extension abandon` |

The catalog is closed. Availability is still position-specific: a registered
prefix is not permission to run it. `workflow next` is the authority for what
can run now.

The agentic request kinds are authority compilation/repair, brownfield spec
curation, and Vision, Backlog, Roadmap, Story, and Sprint generation. All other
positioned mutations use deterministic request JSON.

## Authority Reads And Decisions

The retained authority reads are facts-only:

```sh
./agileforge-dev cli --profile local -- authority status --project-id 41
./agileforge-dev cli --profile local -- authority invariants --project-id 41
./agileforge-dev cli --profile local -- authority review --project-id 41
```

`authority review` may use `--include-spec auto|full|summary`. Read projections
do not author mutation recommendations. The graph advertises `authority decide`
when an exact pending authority is reviewable. Execute the returned command with
its pending-authority, authority-fingerprint, review-fingerprint, decision, and
rationale fields intact.

## Reads

Read commands do not advance the workflow. Useful current projections include:

```sh
./agileforge-dev cli --profile local -- project list
./agileforge-dev cli --profile local -- project show --project-id 41
./agileforge-dev cli --profile local -- project initial-spec --project-id 41
./agileforge-dev cli --profile local -- authority status --project-id 41
./agileforge-dev cli --profile local -- authority invariants --project-id 41
./agileforge-dev cli --profile local -- authority review --project-id 41
./agileforge-dev cli --profile local -- workflow position --project-id 41
./agileforge-dev cli --profile local -- workflow next --project-id 41
```

`project initial-spec` returns the exact active curated initial-spec draft ID,
its canonical content and `content_fingerprint`, discovery provenance, and
created/updated timestamps. It fails with a typed validation error when the
Project is absent, no active draft exists, or the active draft is ambiguous.
This is a facts-only, non-routing read: it returns no commands,
recommendations, decision authorship, or process/session state.

Artifact history and Project-specific reads remain available through
checkout-local launcher help and nested production `--help` output. A read
result is evidence, not a
routing instruction, unless it came from `workflow position` or `workflow next`.

## Guard Failures

When a graph, fact, decision, or repeated-instance guard is stale:

1. Stop using the old command template.
2. Read `workflow position` or `workflow next` again.
3. Rebuild the request/input file against the new decision if needed.
4. Execute the newly advertised command with a new idempotency key.

Do not substitute a nearby command or remove guards to force execution.

## Idempotency And Replay

`WorkflowTransitionReceipt` is the durable command replay contract. Repeating
the exact request with the same idempotency key returns its stored result.
Reusing a key for different request content fails closed.

For an uncertain transport result, retry the exact command once. Then read
`workflow next` again. Provider-backed actions are protected by the same
receipt/attempt contract; callers must not issue a second, altered request to
work around an in-progress result.

## Compiler Recovery

Compiler failures can include this orientation command:

```sh
./agileforge-dev cli --profile local -- workflow next --project-id 41
```

That command is intentionally a fresh graph read. It may advertise repair,
compilation, abandonment, or another recovery decision based on current durable
facts. Compiler code does not author a replacement mutation command.

## Command Discovery

Use parser help to confirm installed syntax:

```sh
./agileforge-dev --help
./agileforge-dev cli --profile local -- --help
./agileforge-dev cli --profile local -- workflow --help
./agileforge-dev cli --profile local -- workflow next --help
./agileforge-dev cli --profile local -- workflow position --help
./agileforge-dev cli --profile local -- authority --help
```

For machine execution, prefer the command string returned by `workflow next`
over prose examples in this manual.

## Operational Safety

- Point test runs at an in-memory or temporary database.
- Keep provider calls disabled in contract tests.
- Use a unique idempotency key for each distinct request.
- Preserve `instance_key` for repeated graph nodes.
- Never mutate a caller repository as a side effect of inspecting workflow
  position.
- Stop on an unrecognized command or guard field and inspect live parser help.
