# Task 17 Report: Remove Legacy Workflow Runtime

## Verdict

PASS. The Task 17 hard break and independent-review fix loop are complete.
`WorkflowDomain` remains the sole routing authority, `Project` is the canonical
aggregate, and fresh production metadata contains no legacy aggregate, session,
FSM, or migration interpretation.

## Fix-Loop TDD Evidence

### RED

Tests were added or tightened before each production correction.

1. Fresh production bootstrap:
   `tests/test_fresh_process_bootstrap.py` initially failed in a spawned process
   with `NoReferencedTableError` because production bootstrap registered workflow
   foreign keys without registering the `projects` table.
2. Canonical packets:
   `tests/test_canonical_packets.py` initially reported 7 failures. The retained
   reader returned the old minimal v1 projection and lacked task_packet.v2 and
   story_packet.v1 metadata, fingerprints, snapshots, ownership, authority,
   validation, constraints, and freshness behavior.
3. Legacy authorities and schema interpretation:
   the first review-absence run found 59 violations, including the importable
   command-authoring authority review service, schema-readiness interpretation,
   `next_actions`, and obsolete service modules.
4. Exact model roles:
   the role-equality regression failed because production and test configs
   contained seven roles outside the live eight-role ADK registry.
5. Project rename breadth:
   the initial whole-repository absence test failed on stale public packet keys,
   tool/script/test names, error codes, and aggregate identifiers. The later
   Python-token test exposed 457 lowercase aggregate-identity occurrences while
   allowing product-vision, product-backlog, and product-definition artifacts.
6. Installed prompt resources:
   the wheel regression initially failed because prompt text was not package data
   and leaf loaders assumed source-tree `Path` locations.

### GREEN

Final focused review set:

```text
uv run --frozen pytest -q \
  tests/test_fresh_process_bootstrap.py \
  tests/test_canonical_packets.py \
  tests/test_packet_renderer.py \
  tests/test_prompt_package_resources.py \
  tests/test_task17_review_absence.py \
  tests/test_model_config_env.py \
  tests/test_model_package_boundary.py \
  tests/adapters/test_agent_contract_boundaries.py \
  tests/adapters/test_production_read_surfaces.py
86 passed, 5 warnings in 8.23s
```

Fresh bootstrap plus packet/API/renderer behavior:

```text
uv run --frozen pytest -q tests/test_fresh_process_bootstrap.py \
  tests/test_canonical_packets.py tests/test_packet_renderer.py
12 passed, 5 warnings in 1.82s
```

Retained business behavior moved to current owners:

```text
uv run --frozen pytest -q \
  tests/test_agent_workbench_authority_projection.py \
  tests/test_story_validation_pinning.py \
  tests/test_story_validation_service.py \
  tests/test_project_repository_deletion.py \
  tests/unit/test_delete_project.py \
  tests/workflow/test_authority_transitions.py
113 passed, 4 warnings in 6.24s
```

Installed wheel resources:

```text
uv run --frozen pytest -q tests/test_prompt_package_resources.py
2 passed, 5 warnings in 2.24s
```

## Production Changes

- `models.db` explicitly imports and retains module references for `core`,
  `specs`, `events`, `workflow`, `agent_workbench`, `authority_curation`, and
  `brownfield` before `SQLModel.metadata.create_all()`.
- The fresh-process test uses multiprocessing `spawn`, a temporary SQLite file,
  production API lifespan, complete foreign-key resolution, actual table
  inspection, representative Project foreign keys, and negative checks for
  `products` and `sessions`.
- `services/packets/canonical.py` now builds task_packet.v2 and story_packet.v1
  from current Project-owned durable records. It includes deterministic metadata
  and source fingerprints, source snapshots, Project/Sprint/Story context,
  pinned accepted authority, validation/findings, execution constraints,
  ownership enforcement, and freshness failures.
- `DurableReadProjectionService` owns the non-routing packet read boundary and
  the renderer consumes `context.project`. No deleted session/FSM routing was
  restored.
- `DurableAuthorityReviewProjection` owns the retained facts-only normalization.
  Command recommendations and second-authority behavior were deleted.
- Production and test model configs equal the live eight-role registry, including
  `spec_validator`. The orchestrator identity and obsolete ADK leaf were removed.
- All retained prompt text and model config YAML are package data. Prompt and
  default-config loading use `importlib.resources`; the wheel test imports every
  retained leaf outside the source tree.
- Newly exposed DB-tool results use explicit `TypedDict` contracts and structured
  mapping validation. The Task 17 delta adds no `Any` or suppression.

## Deletion And Rename Inventory

- Deleted `services/agent_workbench/authority_review.py`,
  `authority_regenerate.py`, `fake_mutation.py`, and `schema_readiness.py` plus
  their obsolete tests.
- Deleted the obsolete pending-authority service and packet-service wrapper.
  Durable assertions were retained in workflow, projection, packet, validation,
  and repository tests rather than by restoring old routing architecture.
- Removed storage-schema interpretation, readiness/version mismatch errors,
  `next_actions`, command-authoring review recommendations, and dead callers.
- Completed public Project naming in packet docs, JSON keys, errors, tools,
  scripts, fixtures, comments, local identifiers, and test/file names. Domain
  terms such as product vision remain only as artifact language.
- Renamed the benchmark and link-spec test to Project forms and removed stale
  product-identity codes, including `SPEC_PRODUCT_MATCH`.
- Relocated the TCC proposal, chapters, diagrams, extraction utility, and v1
  packet material without rewriting their historical claims under
  `artifacts/historical-schema/`. Historical Python sources use `.py.txt` so
  they cannot be interpreted as live executable modules.
- Did not restore migrations, compatibility aliases, legacy imports, session/FSM
  routing, or dual schemas.

## Strict Scans

The scan input is every current tracked plus untracked file:

```bash
git ls-files -z --cached --others --exclude-standard
```

The shell loop excluded only:

```text
docs/superpowers/plans/**
docs/superpowers/specs/**
artifacts/**
.superpowers/**
```

The three final patterns and results were:

```text
orchestrator_agent|FSMController|STATE_REGISTRY|fsm_state|AGILEFORGE_SESSION_DB_URL|GreenfieldDiscoveryContext|context_key
routing: 0 matches across 439 files

\bProduct\b|\bproduct_id\b|products\.product_id|repositories\.product|ProductRepository|ProductTeam|ProductPersona
aggregate: 0 matches across 439 files

WORKFLOW_RUNNER_IDENTITY|agile_orchestrator|storage_schema_version|next_actions|AuthorityReviewService|PRODUCT_NOT_FOUND|SPEC_PRODUCT_MATCH|product_spec_linked|product_name|query_product_structure|benchmark_product_structure|link_spec_to_product|product_context|product_authority_cache_persisted|product_not_found|product_description|sample_product|--product-id|_load_product|_build_product|_update_product|analyze_product_personas
review_residue: 0 matches across 439 files
```

Current documentation/config/package scan:

```bash
rg -n -i '\borchestrator\b|orchestrator_' README.md docs config \
  pyproject.toml .env.example \
  -g '!docs/superpowers/plans/**' -g '!docs/superpowers/specs/**'
```

Result: zero matches (`rg` exit 1).

Reviewer added-line scan from the Task 17 starting commit:

```bash
git diff --unified=0 c0a607abdd800abba07a6ce0bb539c8f06caefba \
  -- '*.py' | awk '
    /^diff --git / { f=$4; sub("b/", "", f) }
    /^\+[^+]/ {
      if ($0 ~ /(^|[^[:alnum:]_])Any([^[:alnum:]_]|$)|type:[[:space:]]*ignore|#[[:space:]]*noqa|pylint:[[:space:]]*(disable|enable)|pyright:|mypy:/)
        print f ":" $0
    }'
```

Result: zero added `Any`, type suppressions, lint ignores, or tool-specific
suppressions.

## Full Gates

```text
node --test tests/test_create_project_modal_required_fields.mjs \
  tests/test_workflow_position_display.mjs
9 passed, 0 failed

uv run --frozen pytest -q
1836 passed, 2 skipped, 2 deselected, 17 warnings in 112.40s

uv run --frozen pyrepo-check --all
Ruff: pass
annotation checks: pass
ty: pass
Bandit: 0 issues across 188,015 lines of code
pytest: 1836 passed, 2 skipped, 2 deselected, 17 warnings in 110.65s

git diff --check
clean
```

The warnings are existing ADK/Starlette deprecations, experimental resumability
notices, and deliberate socket-guard warnings. No Task 17 failure remains.

## Self-Review

- No production module imports a deleted runtime or schema interpreter.
- WorkflowDomain remains the only routing/mutation authority and its fixed 43
  mutation kinds, read/action guards, replay, and provider-at-most-once coverage
  remain in the 1,836-test gate.
- Packet construction is a pure read projection and does not introduce routing.
- Fresh bootstrap imports production code in a spawned process, so conftest model
  imports cannot mask missing metadata registration.
- Historical TCC evidence is preserved verbatim under an explicitly historical
  artifact path; SHA-256 comparisons matched every source Git blob to its
  relocated file.
- Verification used only in-memory or temporary test databases. No provider,
  network, persistent database, external repository, caRtola, ASA, or MyFinance
  mutation occurred.

## Concerns

No blocking concerns. Existing third-party deprecation and experimental-feature
warnings remain unchanged and are recorded above.

## Second Review Fix Loop (2026-08-03)

Baseline: `ee4e4c49e35494b8148260f809938be3c82fecd0`.

### RED Evidence

The second fix loop added failing tests before production changes:

```text
uv run --frozen pytest -q \
  tests/test_canonical_packets.py::test_packet_fingerprints_cover_complete_canonical_validation_evidence
1 failed: source_snapshot had no validation_evidence_hash

uv run --frozen pytest -q \
  tests/test_compiler_remediation_commands.py \
  tests/test_specs_compiler_service.py::test_compiled_authority_schema_unsupported_helpers_use_graph_recovery \
  tests/test_specs_compiler_service.py::test_source_metadata_failure_details_include_repair_guidance
3 failed: compiler output still authored deleted authority commands and stale guards

uv run --frozen pytest -q \
  tests/test_task17_review_absence.py tests/test_fresh_process_bootstrap.py
3 failed, 1 passed: both legacy services were importable, the ledger table was
present, and current docs/tests/production contained deleted routing commands

uv run --frozen pytest -q \
  tests/test_prompt_package_resources.py::test_built_wheel_contains_and_loads_retained_prompt_resources
1 failed: both deleted service modules were present in the built wheel
```

The initial whole-current-Python scan at the second-loop baseline also recorded
`762` `Any` matches across `76` files and `653` typing/lint/checker suppression
matches across `94` files. This established the inherited repository baseline
instead of limiting the audit to added lines.

### Implementation And Deletion Inventory

- Added `source_snapshot.validation_evidence_hash` to both packet flavors. It is
  SHA-256 over the complete parsed `ValidationEvidence.model_dump(mode="json")`
  using canonical JSON key ordering. It therefore covers validity,
  spec/input/validator binding, checked rules and invariants, boundary IDs,
  failures, warnings, alignment findings, and their timestamps.
- Added Task and Story regressions for warning, failure, rule, and compliance
  boundary mutations while `validated_at` and `input_hash` remain fixed. The
  same canonical evidence with reordered JSON object keys remains stable.
- Replaced unsupported-schema and source-metadata compiler recovery output with
  only `agileforge workflow next --project-id <id>`. A parser-backed regression
  validates every compiler remediation output.
- Replaced the obsolete CLI manual with the current WorkflowDomain contract:
  `workflow position`, `workflow next`, the fixed graph command catalog, and
  graph/fact/decision/repeated-instance guards.
- Deleted `services/agent_workbench/mutation_ledger.py`,
  `services/agent_workbench/backlog_refinement_events.py`, and
  `tests/test_agent_workbench_mutation_ledger.py`.
- Removed `CliMutationLedger` and `cli_mutation_ledger` from fresh metadata.
  The remaining agent-workbench models now use explicit `ClassVar[str]` table
  names, removing their local type suppressions.
- Removed only the ledger-coupled approval tests/helpers from
  `tests/test_backlog_refinement_service.py`; all 52 pure operation/contract
  tests remain.
- Moved two byte-identical historical feedback records to
  `artifacts/historical-feedback/` (`R100`) rather than rewriting historical
  observations as current behavior.
- The wheel regression now builds from a clean temporary source snapshot and
  proves the deleted modules, model, and table are absent while retained prompt
  resources and leaf imports still work. It no longer creates `build/` or
  `*.egg-info` in the worktree.

### GREEN Evidence

```text
Focused packet/remediation/absence/schema/wheel/read suites:
160 passed, 5 warnings

Pure backlog-refinement operations:
52 passed, 5 warnings

Compiler, Story validation, and authority projection:
173 passed, 5 warnings

Task 16 replay/provider-at-most-once set:
5 passed, 7 warnings

API/CLI catalog, agent boundaries, and model roles:
56 passed, 6 warnings

Node frontend guards:
9 passed, 0 failed

Installed wheel resources and absence:
2 passed, 5 warnings
```

The live parser probe parsed all `43` `COMMAND_PREFIXES` mutation forms plus
`workflow next` and `workflow position` using graph/fact/decision guards.

Final full gate:

```text
uv run --frozen pyrepo-check --all
Ruff: pass
annotation checks: pass
ty: pass
Bandit: 0 issues across 121,238 lines of code
pytest: 1,797 passed, 2 skipped, 2 deselected, 17 warnings in 112.08s

git diff --check
clean
```

### Strict Scan Scope And Results

The scan input was every existing current tracked plus untracked file from:

```bash
git ls-files -z --cached --others --exclude-standard
```

Only these historical roots were excluded:

```text
docs/superpowers/plans/**
docs/superpowers/specs/**
artifacts/**
.superpowers/**
```

Final scope: `435` tracked-live files, including root files, `.env.example`,
frontend, scripts, tools, current docs, package metadata, fixtures, and tests;
`338` were Python files.

```text
orchestrator_agent|FSMController|STATE_REGISTRY|fsm_state|AGILEFORGE_SESSION_DB_URL|GreenfieldDiscoveryContext|context_key
0 matches

\bProduct\b|\bproduct_id\b|products\.product_id|repositories\.product|ProductRepository|ProductTeam|ProductPersona
0 matches

WORKFLOW_RUNNER_IDENTITY|agile_orchestrator|storage_schema_version|next_actions|AuthorityReviewService|PRODUCT_NOT_FOUND|SPEC_PRODUCT_MATCH|product_spec_linked|product_name|query_product_structure|benchmark_product_structure|link_spec_to_product|product_context|product_authority_cache_persisted|product_not_found|product_description|sample_product|--product-id|_load_product|_build_product|_update_product|analyze_product_personas
0 matches

CliMutationLedger|MutationLedgerRepository|cli_mutation_ledger|services\.agent_workbench\.(mutation_ledger|backlog_refinement_events)|agileforge (workflow state|project setup|authority (accept|reject|curate|regenerate)|backlog reset-active|sprint save|story save)|--expected-state|--expected-context-fingerprint|\bFSM\b|mutation ledger
0 matches
```

The required whole-current-Python audit ended at `713` inherited `Any` matches
across `72` files and `651` inherited suppression matches across `94` files.
Deleting the obsolete runtime removed `49` `Any` matches and two suppressions.
The second-loop added-line scan from `ee4e4c4` returned zero `Any`, type ignores,
lint ignores, or checker suppressions. Ruff annotations and `ty` both pass.

### Second-Loop Self-Review

- The validation hash is inside the source snapshot, so every covered evidence
  change necessarily changes `source_fingerprint` for both packet flavors.
- Packet construction remains a pure read projection. No routing or provider
  behavior was added.
- `WorkflowTransitionReceipt`/`WorkflowDomain` remains the only live command
  idempotency and replay mechanism; no production import reaches the deleted
  services.
- Fresh-process bootstrap and the isolated wheel both prove the removed table,
  model, and service modules are absent.
- The 43-kind catalog, exact frontend guards, facts-only authority reads, model
  role equality, pinned authority/ownership/freshness behavior, and provider
  at-most-once tests remain green.
- Verification used only in-memory/temporary databases and offline wheel builds.
  No provider, network, persistent database, external repository, caRtola, ASA,
  or MyFinance mutation occurred.

### Second-Loop Concerns

No Task 17 blocker remains. The whole-live scan intentionally reports inherited
repository-wide `Any` and checker-suppression debt rather than hiding it behind
an added-lines-only scan; eliminating that baseline would be a separate broad
typing refactor. Existing third-party warnings remain unchanged.
