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
