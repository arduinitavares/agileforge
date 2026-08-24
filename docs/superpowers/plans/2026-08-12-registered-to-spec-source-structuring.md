# Registered To-Spec Source And Structuring Implementation Plan

> **Execution:** Use RED -> GREEN -> REFACTOR for every behavior change. Run
> `uv run --frozen pyrepo-check --all` before the one requested follow-up
> commit. Do not run Manual Test 1 or external acceptance.

**Goal:** Replace direct Specification authoring with immutable external
`to-spec` source registration followed by a closed AgileForge-owned structuring
agent, while retaining exact candidate review and typed-only Authority input.

**Architecture:** Add one immutable `SpecificationSource` aggregate and fact.
Gate `specification.structure` on the current source. Build the provider input
only from the registered bundle and durable lineage. Bind every candidate to
that exact source. Keep Discovery deleted.

## Task 1: Lock the source bundle and persistence

**Files:**

- Create `services/contracts/specification_source.py`
- Update `models/product_definition.py`, `agile_sqlmodel.py`, `models/db.py`
- Update `workflow/facts.py`, `repositories/workflow.py`,
  `repositories/project.py`
- Add/update product-definition model, fact, reload, deletion, and schema tests

1. Write RED tests for a closed canonical bundle, exact bytes, present/absent
   Context, ADR permutation stability, invalid paths/UTF-8, lineage constraints,
   source fact reload, deletion order, and old-schema rejection.
2. Implement `SpecificationSource`, canonical bundle validation/fingerprinting,
   source facts/loaders, and exact candidate source foreign keys.
3. Verify focused pytest, Ruff, Ty, and `git diff --check`.

## Task 2: Register sources and gate the graph

**Files:**

- Update `workflow/requests/product_discovery.py`, request exports,
  `workflow/handlers/product_discovery.py`, handler exports, `workflow/domain.py`
- Update `workflow/definitions/product_discovery.py`
- Add `services/specification_source_registration.py`
- Update `services/application.py`
- Add/update graph, transition, application, drift, replay, and concurrency tests

1. Write RED tests proving no source means structuring unavailable, a valid
   source enables it, identical registration replays/no-ops, replacement is
   immutable and nonbranching, and old-source candidates cannot be accepted.
2. Implement safe exact-byte capture, semantic request preparation, transactional
   registration, source selection, graph nodes, and source-aware business-fact
   fingerprints.
3. Revalidate source and lineage before/after provider execution, completion,
   and acceptance. Preserve rejection/feedback under drift.

## Task 3: Convert authoring to structuring and close ADK output

**Files:**

- Update `services/contracts/specification_authoring.py` and
  `services/specification_authoring_input.py`
- Replace ADK agent/prompt provider symbols with structuring terminology
- Update `adapters/adk/recipes.py`, `adapters/adk/runner.py`, model roles,
  application wiring, candidate contracts/handlers, and focused tests

1. Write RED tests that ADK requires top-level `payload`, exposes the nested
   closed v2 schema, and rejects bare/v1/extra-field results.
2. Rename the provider contract to `SpecificationStructuringInput/Output` and
   make the leaf advertise the real closed output schema.
3. Supply exact source, optional Context, ADRs, repository revision/evidence,
   base, and prior feedback. Remove opportunistic unregistered source loading.
4. Store prompt version/hash and structurer provenance in the host envelope;
   keep every lifecycle field out of provider output.

## Task 4: Cut over transports, review projection, and docs

**Files:**

- Update `api.py`, `cli/main.py`, `cli/workflow_commands.py`
- Update `services/read_projections.py`, `frontend/project.html`,
  `frontend/project.js`
- Update `CONTEXT.md`, ADR 0003, add ADR 0004, and update active profile/design
- Add/update API, CLI, dashboard, read-projection, E2E-collection, Authority
  boundary, and hard-break tests

1. Add source registration/status transport surfaces whose human inputs are
   repository-relative paths, preparation attestation, and applicable ADR paths.
2. Replace “author” actions with “structure”; render source provenance and the
   same exact candidate review packet across API/CLI/dashboard.
3. Prove Authority provider bytes exclude source Markdown, Context, ADR, and
   repository prose. Prove no Discovery surface returns.
4. Correct `CONTEXT.md`: Discovery is optional `grill-with-docs` activity, not a
   persisted artifact.

## Task 5: Review, full verification, and commit

1. Run focused canonicalization, graph, drift, transport, UI, and Authority
   suites.
2. Run fresh scoped correctness and adversarial reviews; fix only evidenced
   Critical/Important defects with RED tests.
3. Confirm Manual Test 1 and the main worktree are untouched.
4. Run `uv run --frozen pyrepo-check --all` from the development worktree.
5. Inspect the final diff, stage only the follow-up, and create one new commit
   without squashing or pushing.
