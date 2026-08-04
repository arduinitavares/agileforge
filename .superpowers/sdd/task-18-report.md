# Task 18 Report: Prepare Operator-Led Acceptance Checklist

## Verdict

PASS for the regenerated implementation package and self-review. Fresh
independent review is still required. The Operator-run package is prepared, but
acceptance execution has not started: caRtola, ASA, and MyFinance remain
`not_run`. This report makes no external-repository pass claim.

## Scope

Original Task 18 base:
`cb3e32c4144866e81bf367f073984905abce77e9`

Final implementation HEAD:
`34d1c08f2dfa7ba077dff2ece91598068a5b3aba`

Complete regenerated implementation range:
`cb3e32c4144866e81bf367f073984905abce77e9..34d1c08f2dfa7ba077dff2ece91598068a5b3aba`

Immutable implementation package:
`.superpowers/sdd/review-cb3e32c..34d1c08.diff`

Package SHA-256:
`1f4ae1678bbc0a7d4a08668f1556c00f6a984e78054f9d98b20780326894a960`

The range includes the original Task 18 checklist/read work, the independently
approved Task 18 fixes at `9b16965`, the uv-owned profile/launcher/UI/check/CI/
distribution implementation through `3c4b62c`, and the operating-document
regeneration at `34d1c08`.

The earlier Task 18 review-fix starting HEAD was
`6031483e4aa3c419bf213b57804d869d0b6511f4`.

All four Important findings and the Minor finding in
`.superpowers/sdd/task-18-review.md` were treated as valid. The fix loop changes:

- `services/read_projections.py`
- `services/application.py`
- `cli/main.py`
- `docs/agent-cli-manual.md`
- `docs/testing/workflow-graph-acceptance-checklist.md`
- `tests/adapters/test_initial_spec_read.py`
- `tests/adapters/test_production_read_surfaces.py`
- `tests/test_workflow_acceptance_document.py`
- `.superpowers/sdd/task-18-report.md`

The existing `README.md` checklist link remains present and contract-tested.

No command accessed, inspected deeply, edited, branched, created a worktree in,
or otherwise mutated caRtola, ASA, MyFinance, or another external repository.
No provider, network workflow, persistent database, acceptance command, or Task
19 work ran. Tests used only temporary or in-memory databases.

## Task 7 Package Regeneration

Current operating guidance now distinguishes the installed stable release from
development branches and linked worktrees. Repository setup is uv-only.
Checkout-local examples use that checkout's `./agileforge-dev`; `AGENTS.md`
requires fresh sessions to record `info --json` before mutations and preserve
per-worktree profile, business database, trace database, and UI-port ownership.

The Operator checklist now initializes one exact-SHA acceptance profile for
each target. It records `info --json` before each product CLI step and takes
database/model provenance, exact forwarded argv, exit status, and the production
JSON result from the launcher's JSON envelope. It contains no manual business or
trace database exports and performs no manual schema bootstrap.

The stable release remains separate. This package does not claim that replacing
an installed stable shim is supported; that decision remains after Operator
acceptance.

## Review Fixes

### Initial-Spec Read

`agileforge project initial-spec --project-id <id>` is now a supported
facts-only read across the durable projection, production application port, and
CLI parser/handler. It returns the exact active initial-draft ID, canonical
content, content fingerprint, discovery provenance, and immutable
created/updated timestamp. It authors no routing decision or command.

The projection uses the graph's complete-chain selection rules and verifies the
persisted content hash. Typed failures cover missing Project, missing active
draft, ambiguous draft chain, malformed content, and hash mismatch. Tests prove
the returned ID/hash/content are the values referenced by the available human
decision and accepted by its guarded request payload.

### Pinned Operator Procedure

Every profile, read, and mutation instruction now starts from the literal
reviewed worktree and uses that checkout's `./agileforge-dev`. The checklist
requires a recorded SHA equality check before every CLI invocation and restart
boundary, then `info --json` before each product CLI step. One exact-SHA
acceptance profile per target owns the current schema, business DB, separate ADK
trace DB, model config, and checkout provenance without manual DB exports.

Restart means two independent one-shot CLI processes with identical pins and
recorded timestamps. The trace reset can delete only a separately configured
disposable trace file inside the acceptance temp root after path inequality and
inactive-process checks. The durable DB remains untouched and no session command
is invented.

### Correlated Evidence And Command Safety

The Task 18 top-level YAML keys remain exact. `steps` is authoritative, with one
complete record for repository, phase/status/timestamps, graph command metadata,
substitutions, profile-info provenance, launcher argv, exact forwarded argv,
production JSON result, before/after positions and guards, authority/model
identity, verification, artifacts, and structured failure. Statuses are
`not_run`, `passed`, `failed`, and `blocked`; the prepared overall status remains
`not_run`.

The Operator records the returned template, substitutes only declared values,
and records the executed argv. A new idempotency key is required for each
distinct request. The stale probe reruns the successful original template with
only a new key while preserving old guards and requires rejection with no second
mutation.

### Structural Documentation Contract

The validator requires exact section headings/order and section-scoped
preflight, caRtola, ASA, MyFinance, stale-probe, restart, trace-reset, evidence,
and stop-boundary contracts. It parses YAML and validates the complete nested
step schema and status enum. The adversarial keyword-only document is rejected.
All literal launcher examples parse through the developer parser, and every
forwarded product argv parses through the live product parser.

## TDD Evidence

### RED

The initial-spec tests were written before the production read surface:

```text
uv run --frozen pytest tests/adapters/test_initial_spec_read.py -q
5 failed, 4 warnings in 1.19s
```

All failures were caused by the missing projection/parser contract.

The strengthened checklist validator was run against the reviewed document
before its rewrite:

```text
uv run --frozen pytest tests/test_workflow_acceptance_document.py -q
2 failed, 2 passed, 5 warnings in 0.85s
```

The real checklist failed exact structure and pinned-command requirements; the
adversarial document was already rejected.

A final structural assertion for explicit repository-level result handling was
also added before its prose:

```text
uv run --frozen pytest tests/test_workflow_acceptance_document.py -q
1 failed, 3 passed, 5 warnings in 0.92s
```

It passed 4/4 after defining that only complete returned evidence can establish
`passed`, a concrete required-step failure establishes `failed`, and incomplete
evidence remains `not_run`.

The CLI-manual contract was then added before its documentation update:

```text
uv run --frozen pytest \
  tests/adapters/test_initial_spec_read.py::test_agent_cli_manual_names_initial_spec_read -q
1 failed, 4 warnings in 0.77s
```

### GREEN

Read, production transport, command renderer, CLI, durable replay, and complete
checklist/adversarial contracts:

```text
uv run --frozen pytest \
  tests/adapters/test_initial_spec_read.py \
  tests/adapters/test_production_read_surfaces.py \
  tests/adapters/test_command_renderer.py \
  tests/adapters/test_cli_workflow_domain.py \
  tests/adapters/test_adk_workflow_runner.py \
  tests/workflow/test_transition_idempotency.py \
  tests/test_workflow_acceptance_document.py -q
65 passed, 16 warnings in 6.07s
```

The exact required document command passes independently:

```text
uv run --frozen pytest tests/test_workflow_acceptance_document.py -q
4 passed, 5 warnings
```

Live parser help succeeds:

```text
uv run --frozen agileforge project initial-spec --help
usage: agileforge project initial-spec [-h] --project-id PROJECT_ID
```

Node tests were not run because no frontend or Node source/test changed.

## Hard-Break Evidence

Targeted Task 17 routing/review-residue and forbidden legacy-command scans
returned zero matches. The Task 16 command-renderer, CLI graph, ADK replay, and
transition-idempotency tests are included in the 65-test focused gate and the
full suite.

Static checks before the full gate:

```text
uv run --frozen ruff check <touched-python-files>
All checks passed!

uv run --frozen ty check
All checks passed!
```

## Full Verification

```text
./agileforge-dev check
Ruff: pass
annotation checks: pass
ty: pass
Bandit: 0 issues across 129,008 lines of code
pytest: 1,947 passed, 2 skipped, 2 deselected, 17 warnings
Node: 9 passed, 0 failed
wheel: verified
sdist: verified
```

Focused current-document contract:

```text
uv run --locked pytest tests/test_uv_only_docs.py \
  tests/test_workflow_acceptance_document.py -q
8 passed, 5 warnings
```

Exact current-guidance scan:

```text
rg -n "AGILEFORGE_SESSION_DB_URL|/Users/aaat/.local/bin/agileforge" \
  README.md .env.example AGENTS.md docs/agent-cli-manual.md \
  docs/testing/workflow-graph-acceptance-checklist.md cli tests
no matches
```

```text
git diff --check
clean
```

Warnings are existing Pydantic/ADK, Starlette/httpx, resumability, and guarded
network-test warnings. They caused no failures.

## Stop Boundary And Concerns

Checklist preparation is not acceptance execution. The package still requires
fresh independent review, then Operator execution and returned evidence. Task
19 must not start before that evidence or one concrete acceptance failure is
returned.

No implementation blocker remains. External acceptance status is deliberately
`not_run` for all three repositories.
