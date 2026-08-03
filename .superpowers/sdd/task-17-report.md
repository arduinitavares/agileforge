# Task 17 Report: Remove Legacy Workflow Runtime

## Verdict

PASS. The approved workflow-graph hard break is complete. `WorkflowDomain` is the sole routing authority, the canonical aggregate is `Project`, and fresh SQLModel metadata has no legacy aggregate, session, FSM, or migration interpretation.

## TDD Evidence

### RED

The absence and fresh-schema tests were written before production edits.

```text
uv run --frozen pytest tests/workflow/test_legacy_runtime_absent.py tests/workflow/test_fresh_project_schema.py -q
```

Initial result: **15 failed, 6 warnings in 1.95s**. Failures demonstrated the still-present legacy runtime modules, symbols, tables, fields, migration entrypoint, and aggregate identity.

### GREEN

The same command on the final tree:

```text
15 passed, 5 warnings in 1.49s
```

Retained project-deletion behavior:

```text
uv run --frozen pytest tests/unit/test_delete_project.py tests/test_project_repository_deletion.py -q
22 passed, 5 warnings in 2.20s
```

Workflow contract suite:

```text
uv run --frozen pytest tests/workflow -q
489 passed, 4 warnings in 62.13s
```

## Rename Inventory

- Renamed the canonical SQLModel aggregate and all live references from `Product` to `Project`.
- Renamed table and foreign-key identity from `products` / `product_id` to `projects` / `project_id`.
- Renamed `ProductRepository`, `ProductTeam`, and `ProductPersona` to their `Project` forms.
- Replaced `repositories/product.py` with `repositories/project.py` and renamed its repository deletion test.
- Retained "product vision" only as a lowercase artifact/domain phrase, never as aggregate identity.

## Deletion Inventory

- Deleted the complete orchestrator package and FSM tree, root orchestrator tools, model/config/package role, and current documentation references.
- Deleted `services/workflow.py`, `repositories/session.py`, the old agent-workbench application/session reader, orchestrator context/query services, phase workflow state, setup/session reconstruction, and migration support.
- Deleted obsolete runtime/service layers for backlog, sprint, phase, setup, diagnostics, and command/session reconstruction.
- Deleted old migrations, migration scripts, obsolete benchmarks/debug artifacts, stale current architecture documents, Task 16 legacy snapshots, and tests owned only by removed behavior.
- Deleted `GreenfieldDiscoveryContext`, `context_key`, session-only API/model fields, and `WorkflowEvent.session_id`.
- Relocated the still-live spec-validator leaf to `adapters/adk`; moved durable authority append and planning selection behavior into workflow handlers.

The final staged diff covers 354 files with 3,534 insertions and 188,202 deletions. Git recognizes the spec-validator prompt and project-repository test as file moves; the canonical repository is recorded as a hard delete/add because its implementation was substantially reduced.

## Fresh Schema

- `SQLModel.metadata.create_all()` creates and validates `projects`.
- Metadata contains no `products` or `sessions` table.
- Current schema contains no deleted session, setup-context, or FSM state columns.
- Migration interpretation and compatibility fallback paths are absent.

## Strict Scans

Routing/session/FSM absence scan, excluding only historical plans/specs and task artifacts:

```bash
rg -n 'orchestrator_agent|FSMController|STATE_REGISTRY|fsm_state|AGILEFORGE_SESSION_DB_URL|GreenfieldDiscoveryContext|context_key' \
  --glob '!docs/superpowers/specs/**' \
  --glob '!docs/superpowers/plans/**' \
  --glob '!artifacts/**' \
  --glob '!.superpowers/**' .
```

Result: **zero matches** (`rg` exit 1).

Canonical aggregate scan:

```bash
rg -n '\bProduct\b|\bproduct_id\b|products\.product_id|repositories\.product' \
  models repositories workflow adapters services cli api.py routers tools utils tests scripts
```

Result: **zero matches** (`rg` exit 1).

`.superpowers/**` is excluded from the first command because the approved task brief and this evidence report necessarily name the forbidden historical symbols. Historical files under `docs/superpowers/specs/**`, `docs/superpowers/plans/**`, and `artifacts/**` remain evidence, not live behavior.

## Full Gates

```text
node --test tests/*.mjs
9 passed, 0 failed

git diff --check
clean

uv run --frozen pyrepo-check --all
Ruff: pass
annotation checks: pass
ty: pass
Bandit: 0 issues across 129,781 lines of code
pytest: 1,912 passed, 2 skipped, 2 deselected, 17 warnings in 112.21s
```

The warnings are existing ADK/Starlette deprecations, experimental resumability notices, and the test socket guard. No Task 17 failure or project-deletion relationship warning remains.

## Self-Review

- No production module imports a deleted runtime.
- No migration, alias, import fallback, compatibility shim, or dual schema remains.
- Task 16 read/action/replay contracts remain covered by the workflow and adapter suites.
- Durable behavior from deleted tests was retained only where the workflow or project repository still owns it.
- No new `Any`, type suppression, lint ignore, or dependency was introduced.
- Verification used in-memory or temporary test databases only; no external repository or persistent database was mutated.

## Concerns

No blocking concerns. The deliberately broad deletion removes the old application/API/session surfaces exactly as approved; consumers must use the workflow-domain and current adapter contracts.
