# To-Spec Single Specification Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:test-driven-development` for every behavior change and `superpowers:verification-before-completion` before every completion claim or commit.

**Goal:** Implement issue #199 by making `to-spec` the single typed Specification-authoring boundary, removing persisted Discovery as a lifecycle gate, and preserving exact human Specification and Authority review.

**Architecture:** Accepted Product Goal facts route directly to an immutable `agileforge.spec.v2` candidate. Semantic bytes contain only the canonical specification; lifecycle, source, producer, renderer, attempt, base, and review metadata live in an immutable host-owned envelope. Acceptance projects those exact bytes into `SpecRegistry`. Authority receives a deterministic typed view of the accepted payload, never Markdown, plain text, or provenance prose.

**Tech Stack:** Python 3.12+, Pydantic 2, SQLModel/SQLite, FastAPI, vanilla JavaScript, pytest, Hypothesis, Ruff, ty, and the repository's uv-only toolchain.

## Global Constraints

- Work only in `/Users/aaat/projects/agileforge/.worktrees/context-grounded-vision-bootstrap` on `dev/context-grounded-vision-bootstrap`; create no branch or worktree.
- Treat `/tmp/agileforge-issue-199-handoff-20260811.md` and GitHub issue #199 as the approved design and acceptance contract.
- Make a hard break. Add no migration, v1 compatibility, dual-read, or dual-write path.
- Keep `agileforge.spec.v1` closed as historical documentation/code only; current runtime accepts only `agileforge.spec.v2`.
- Discovery may appear only as bounded source provenance. It is not a model, foreign key, graph node, request, API/CLI command, dashboard card, or acceptance gate.
- Humans review one complete deterministic projection. They never enter raw JSON, IDs, hashes, fingerprints, or hidden lineage values.
- Specification acceptance and rejection target one immutable candidate and never rewrite its semantic payload or envelope.
- Authority compiles only an accepted typed v2 payload; Markdown/plain text and provenance prose are never compiler inputs.
- Preserve the separate human Authority review gate and every downstream lifecycle stage.
- Use `uv run --frozen` for project commands. Add no dependency or typing suppression.
- Do not access, reuse, migrate, or mutate `manual-string-calculator-7` or any external acceptance project.
- Every implementation task follows RED -> GREEN -> REFACTOR and ends with focused verification before a narrow commit.

## Canonical Contracts

### Semantic payload

Create `utils/agileforge_spec_profile_v2.py` with frozen, `extra="forbid"` models and `SCHEMA_VERSION = "agileforge.spec.v2"`.

- `SpecificationPayload`: `schema_version`, stable `specification_id`, `title`, `summary`, `problem_statement`, typed `items`, typed `relations`, `controlled_terms`, and `external_references`.
- `SpecificationItem`: stable `item_id`, closed `item_type`, `title`, `statement`, optional rationale/level, verification and acceptance criteria, tags, and typed `source_notes`. There is no review-status or redundant `normative` flag.
- `SpecificationRelation`: typed relation kind plus valid source/target item IDs.
- No status, decision, version number, timestamp, renderer, candidate ID, host path, or producer metadata belongs in payload bytes.
- Canonicalize every declared unordered collection before JSON serialization: items by stable ID; relations by kind/source/target; controlled terms by `(term, scope)`; external references by stable ID; item tags lexically. Reject duplicates before sorting. Preserve authored order for acceptance criteria and source notes; reordering either intentionally changes the hash.
- Validate unique stable IDs, closed values, relation endpoints, and normalized-set duplicates. Compute SHA-256 only over canonical UTF-8 JSON.
- Stable IDs persist across ordinary title/statement/criteria edits. Type changes under an existing ID are invalid. A one-to-one identity replacement needs an explicit old/new mapping; splits and merges are represented as justified removals plus additions, not implicit replacement.

### Candidate envelope

Persist an immutable host-owned envelope alongside canonical bytes:

- `candidate_kind`: `initial` or `amendment`.
- Direct accepted Vision and Product Goal IDs plus content fingerprints.
- `base_specification_id` and `base_payload_fingerprint` for amendments; both absent for initial candidates.
- Canonical source manifest, accepted-fact fingerprint, producer input fingerprint, producer capability/version, model/config, prompt fingerprint, and attempt/correlation identifiers. Host database IDs and record timestamps are excluded from candidate identity.
- Payload fingerprint, profile version, renderer version, complete review-view fingerprint, deterministic amendment diff, and final candidate fingerprint.
- The candidate fingerprint commits to all Authority-affecting payload bytes plus the complete immutable envelope. A decision targets this fingerprint.

For amendments, store a full resulting payload. Compute added, changed, and removed stable IDs against the pinned accepted base. The producer supplies `removed_items[{old_id, justification}]`, `replacements[{old_id, new_id, justification}]`, and optional rationales for same-ID modifications. Computed removed IDs must exactly equal declared removals. Each replacement old ID exists in the base and is absent from the result; each new ID is new and present. Reject stale bases, type changes under one ID, and unexplained removals before persistence and again before acceptance. Initial candidates require no base and an empty amendment manifest.

### Authority input

Create a deterministic `agileforge.authority-input.v2` host projection containing:

- accepted specification ID/version/payload fingerprint and typed lineage IDs;
- normative items and relations between normative items;
- explicitly separated non-normative context;
- typed source-reference IDs only, never source-note or external-reference prose as invariants.

The Authority-eligible allowlist is exhaustive: `REQ`, `QUALITY`, `CONSTRAINT`, `INTERFACE`, and `DATA` items whose level is not `INFORMATIVE`. `GOAL`, `NON_GOAL`, `DECISION`, `ASSUMPTION`, `RISK`, `EXAMPLE`, `OPEN_QUESTION`, and informative items are review context only. The compiler may use that context for explanation but can create invariants only from eligible item statements/criteria, and every invariant citation must target an eligible typed specification item ID.

### Authoring attempt protocol

`specification.author` is one registered agentic workflow node built on existing `WorkflowNodeAttempt`, `WorkflowNodeAttemptOutcome`, transition receipts, and ADK recipe infrastructure. Do not create a second attempt ledger.

1. Start atomically captures the graph decision, accepted Vision/Goal references, exact accepted base (when present), canonical source manifest, business-fact/input fingerprints, producer/prompt/model/config versions, correlation ID, and idempotency claim before a provider call.
2. Same idempotency key plus same input replays without another provider call. Reuse with different input conflicts. Unknown/expired attempts require existing explicit recovery semantics; they are never silently called again.
3. Recheck all captured facts before provider invocation. Drift closes the attempt as obsolete with `STALE_SPECIFICATION_INPUT` and makes zero provider calls.
4. Validate and canonicalize provider output. Invalid output closes the attempt as failure with a durable bounded diagnostic and writes no candidate.
5. Complete inside one transaction: recheck Vision, Goal, base, source-manifest/input fingerprint, and current graph decision; persist at most one candidate; close the exact attempt successfully. Drift closes it as obsolete and writes no candidate.
6. Acceptance transactionally rechecks candidate fingerprint, current lineage/source/input fingerprints, and amendment base before writing one terminal decision and accepted version.

Stable public failure codes are `UNSUPPORTED_SPECIFICATION_SCHEMA`, `INVALID_SPECIFICATION_PAYLOAD`, `STALE_SPECIFICATION_INPUT`, `STALE_SPECIFICATION_BASE`, `SPECIFICATION_CANDIDATE_CONFLICT`, `SPECIFICATION_AMENDMENT_MISMATCH`, and `SPECIFICATION_PRODUCER_FAILED`.

---

### Task 1: Lock The V2 Payload, Canonicalizer, Renderer, And Envelope

**Files:**
- Create: `utils/agileforge_spec_profile_v2.py`
- Create: `services/specs/candidate_contract.py`
- Create: `tests/test_agileforge_spec_profile_v2.py`
- Create: `tests/services/specs/test_candidate_contract.py`
- Update: `utils/__init__.py` only if an existing export convention requires it

- [ ] Write payload parsing tests for closed fields/enums, unique IDs, relation endpoints, normative flags, and rejection of lifecycle/host metadata.
- [ ] Write property tests proving identical canonical bytes/hash for permutations of every declared unordered collection and proving ordered criteria remain order-sensitive.
- [ ] Write renderer tests proving all Authority-affecting fields, including source notes and external references, appear in a deterministic Markdown projection.
- [ ] Write envelope tests proving payload and envelope immutability, candidate-fingerprint sensitivity, complete review-view fingerprinting, and source/attempt metadata validation.
- [ ] Write amendment tests for deterministic added/changed/removed output, pinned base hash, stale-base rejection, justified removals, and explicit stable-ID replacements.
- [ ] Run the new tests and verify RED because v2 modules do not exist.
- [ ] Implement the smallest closed v2 models, canonicalizer, complete renderer, envelope builder, and amendment differ to turn the tests GREEN.
- [ ] Run focused Ruff, ty, pytest, and `git diff --check`; commit as `feat: define immutable specification v2 contract`.

### Task 2: Replace Discovery Persistence With Direct Candidate Lineage

**Files:**
- Update: `models/product_definition.py`
- Update: `models/specs.py`
- Update: `models/db.py`
- Update: `workflow/facts.py`
- Update: `repositories/workflow.py`
- Update: `repositories/project.py`
- Update: `tests/workflow/test_product_definition_models.py`
- Update: `tests/workflow/test_product_definition_facts.py`
- Update: `tests/workflow/test_product_goal_domain_reload.py`
- Update: `tests/test_project_repository_deletion.py`
- Update downstream model fixtures that construct candidates/spec versions

- [ ] Rewrite tests first to require no `DiscoveryArtifact` table/fact/FK and direct accepted Vision/Product Goal lineage on `SpecificationCandidate` and `SpecRegistry`.
- [ ] Add persistence tests for every immutable envelope field, canonical bytes/hash validation, direct lineage reload, and rejection of stale/missing accepted source facts.
- [ ] Add initial/amendment persistence tests: initial forbids a base; amendment requires the current accepted base pair and deterministic diff; stale base fails without writing a row.
- [ ] Add retry/concurrency tests proving repeated identical candidate recording yields one exact candidate and conflicting output cannot reuse the idempotency key.
- [ ] Add a startup test proving a pre-#199 database/profile fails before workflow reads or writes with a stable unsupported-schema diagnostic; do not migrate or partially reuse it.
- [ ] Run focused tests and verify RED against the Discovery-backed models.
- [ ] Delete `DiscoveryArtifact`, `DiscoveryArtifactFact`, Discovery repository loading, delete ordering, and every Discovery FK. Replace candidate/spec facts and loaders with direct lineage plus v2 envelope validation.
- [ ] Ensure Specification acceptance copies exact canonical bytes/hash into `SpecRegistry`, never mutates candidate bytes, and only supersedes the prior accepted registry row after an exact decision.
- [ ] Run focused Ruff, ty, pytest, and `git diff --check`; commit as `refactor: persist specifications without discovery gate`.

### Task 3: Route Product Goal Directly To One Durable To-Spec Attempt

**Files:**
- Replace: `workflow/requests/product_discovery.py` with a Specification-focused request module or rename it consistently
- Update: `workflow/handlers/product_discovery.py`
- Update: `workflow/definitions/product_discovery.py`
- Update: `workflow/requests/__init__.py`
- Update: `workflow/handlers/__init__.py`
- Update: `workflow/domain.py`
- Update: `services/application.py`
- Update: `models/workflow.py` only if an existing attempt contract needs a typed outcome/error extension
- Update: `adapters/adk/recipes.py`
- Add/update: the registered `to-spec` ADK prompt and typed adapter response contract
- Replace/remove: `services/product_discovery_selection.py`
- Update: `tests/workflow/test_product_discovery_graph.py`
- Update: `tests/workflow/test_product_discovery_transitions.py`
- Update: `tests/workflow/test_graph_properties.py`
- Update: `tests/workflow/test_single_project_graph.py`
- Update: `tests/services/test_product_goal_application.py`

- [ ] Write graph tests proving accepted Product Goal enables `specification.author` directly and `discovery.record` is absent from all node/edge/next-action output. Preserve the existing later-Sprint re-entry clock for an amendment.
- [ ] Add authoring-input and agentic lifecycle tests for the exact start/complete/fail/recover protocol above: stale-before-call makes zero provider calls; drift-during-call writes no candidate; malformed output is durable failure; same key/input calls once and replays; same key/different input conflicts; successful completion cannot create a second candidate.
- [ ] Define a typed host-built authoring input and typed v2 provider result. The host resolves project, source facts, canonical source manifest, base, graph/input fingerprints, and candidate identity before the provider call; no human or external caller uploads arbitrary JSON/Markdown.
- [ ] Add transition tests for exact terminal feedback/rejection, revision supersession, competing attempts, and stale candidate decisions.
- [ ] Add decision tests proving accept/reject/feedback target the same candidate fingerprint transactionally and cannot be replayed onto a revision.
- [ ] Run the focused tests and verify RED.
- [ ] Delete `RecordDiscoveryArtifact`, raw `RecordSpecificationCandidate`, their handler/dispatch/rule/node, and the discovery selection service. Register `specification.author` directly after accepted Product Goal using the existing node-attempt/receipt/ADK machinery.
- [ ] Implement atomic attempt completion, typed candidate persistence, and exact decision handling without a raw JSON/Markdown registration bypass.
- [ ] Run focused Ruff, ty, pytest, and `git diff --check`; commit as `feat: route product goals directly to to-spec`.

### Task 4: Expose One Complete Review Packet Through API, CLI, And Dashboard

**Files:**
- Update: `services/read_projections.py`
- Update: `api.py`
- Update: `cli/main.py`
- Update: `cli/workflow_commands.py`
- Update: `frontend/project.html`
- Update: `frontend/project.js`
- Update: `tests/services/test_durable_product_definition_projections.py`
- Update: `tests/adapters/test_api_workflow_domain.py`
- Update: `tests/adapters/test_command_renderer.py`
- Update: `tests/e2e/test_single_project_lifecycle_ui.py`

- [ ] Write projection tests proving one packet includes canonical payload, complete rendered review, lineage/source/producer/attempt metadata, base/diff, payload/view/candidate fingerprints, and decision state.
- [ ] Version the packet as `agileforge.specification_review.v2`; add schema-driven coverage that fails when a new reviewable payload/envelope field has no renderer mapping. Include source warnings and amendment justifications.
- [ ] Prove projection/view fingerprint changes when any Authority-affecting field changes and stays identical across API and CLI retrieval.
- [ ] Write transport tests proving Discovery endpoints/commands are absent; humans do not submit IDs/hashes/fingerprints; stale decisions return a typed conflict; unsafe raw content is rejected.
- [ ] Write UI tests proving there is no Discovery card/form/fetch, no raw JSON editor, and Specification review renders every payload section plus amendment diff before the exact decision controls.
- [ ] Run focused tests and verify RED.
- [ ] Implement a single read projection used by API, CLI, and dashboard. Delete Discovery routes, maps, commands, renderer, state, card, and JavaScript fetches.
- [ ] Render exact safe `specification.author` command templates from `workflow next`; never ask a human to author internal fields. API and CLI review both call one application operation; transports capture the packet candidate fingerprint, while the human supplies only decision/rationale.
- [ ] Run focused Ruff, ty, pytest, Node/UI checks, and `git diff --check`; commit as `feat: unify specification review surfaces`.

### Task 5: Compile Authority Only From Accepted Typed V2 Input

**Files:**
- Create/update: `services/contracts/authority_input.py`
- Update: `services/authority_compilation_input.py`
- Update: `services/contracts/specification_normalizer.py`
- Update: `services/specs/profile_content.py`
- Update: `services/specs/compiler_service.py`
- Update: `utils/spec_schemas.py`
- Update: `adapters/adk/prompts/specification.txt`
- Update: `tools/spec_tools.py`
- Update: `tests/services/test_authority_compilation_input.py`
- Update: `tests/services/contracts/test_specification.py`
- Update: `tests/test_specs_compiler_service.py`
- Update: `tests/adapters/test_specification.py`
- Update: `tests/workflow/test_authority_transitions.py`

- [ ] Write tests proving only an accepted exact v2 registry payload can build Authority input; v1, Markdown, plain text, unaccepted candidates, and mismatched hashes fail closed.
- [ ] Prove all compiler entrypoints share one strict persisted-v2 loader; mutable `content_ref`, raw preview/update, and direct tool calls cannot bypass it.
- [ ] Write deterministic projection tests separating normative items from non-normative context and excluding provenance prose from invariant source material.
- [ ] Write compiler tests proving invariants cite valid typed item IDs, cannot originate from non-normative/source prose, and preserve deterministic output under unordered source permutations.
- [ ] Keep/extend workflow tests proving compiled Authority still needs independent human acceptance before Backlog.
- [ ] Run focused tests and verify RED.
- [ ] Implement `agileforge.authority-input.v2`; remove `plain_text` detection/fallback and runtime v1 acceptance paths; update prompt/tool contracts to accept only the host-built typed projection.
- [ ] Delete public/raw compiler or registration paths that bypass accepted `SpecRegistry`; keep only internal helpers required by the workflow.
- [ ] Run focused Ruff, ty, pytest, and `git diff --check`; commit as `refactor: compile authority from accepted typed specs`.

### Task 6: Remove Residual Discovery And Supersede Active Documentation

**Files:**
- Delete: `docs/examples/scope-discovery/challenge-artifact.example.json`
- Supersede/update: `docs/adr/0002-store-discovery-artifacts-in-agileforge-state.md`
- Update: `README.md`
- Update: `docs/agent-cli-manual.md`
- Update: `docs/testing/workflow-graph-acceptance-checklist.md`
- Update: `docs/superpowers/specs/2026-08-05-single-project-lifecycle-hard-break-design.md`
- Add: `docs/superpowers/specs/2026-08-11-agileforge-spec-profile-v2.md`
- Mark superseded: `docs/superpowers/plans/2026-08-05-single-project-lifecycle-hard-break.md`
- Remove/update all tests and fixtures that still encode persisted Discovery or runtime v1/plain-text behavior

- [ ] Add/adjust structural tests that scan active runtime, docs, commands, route maps, and dashboard for obsolete Discovery gates and raw compiler entry points while allowing explicit historical/superseded references.
- [ ] Run those tests and verify RED with the residual references.
- [ ] Delete obsolete code/examples/tests and update active lifecycle docs, ADR status, exact CLI examples, v2 profile contract, and operator acceptance checklist.
- [ ] Prove `agileforge.spec.v1` is described as closed/historical and explicitly rejected by current runtime.
- [ ] Run targeted docs/contract tests, Ruff, ty, and `git diff --check`; commit as `docs: retire persisted discovery lifecycle`.

### Task 7: Independent Review, Full Verification, And Handoff

- [ ] Inspect `git status`, `git diff --stat`, and the complete diff for scope; prove no external project/profile/database was touched.
- [ ] Run a scoped code review against the issue/handoff, emphasizing security, stale/retry behavior, exact fingerprints, amendment removal policy, and raw compiler bypasses.
- [ ] Run a fresh-context adversarial review of the implemented contract. Fix every valid in-scope finding with RED-first regression tests and narrow commits.
- [ ] Search active code/docs for `DiscoveryArtifact`, `discovery.record`, `/discovery`, `plain_text`, raw Specification registration, runtime `agileforge.spec.v1`, hidden human-entered IDs/fingerprints, and incomplete rendering.
- [ ] Run focused issue #199 tests again from a clean process.
- [ ] Run the complete fresh gate:

```bash
uv run --frozen pyrepo-check --all
```

- [ ] Re-run `git status --short --branch`, capture the verified commit SHA, and confirm the development worktree is clean.
- [ ] Do not run Manual Test 1. Hand the user exact `./agileforge-dev` commands to initialize a fresh acceptance profile pinned to the verified commit; the user owns all external acceptance decisions.

## Verification Index

- Direct Product Goal -> Specification: workflow graph and application tests.
- Stale source/candidate/base: repository, transition, and API conflict tests.
- Retry/concurrency/exact decision: transition and model uniqueness tests.
- Canonical ordering/stable IDs: v2 property tests.
- Complete review and immutable acceptance: renderer/projection/persistence tests.
- Initial/amendment/diff/removal rules: candidate contract and transition tests.
- Typed-only Authority and provenance exclusion: Authority input/compiler tests.
- Separate Authority gate: Authority/Backlog workflow tests.
- No Discovery/raw UI: API/CLI/E2E/structural tests.
- No v1/plain-text runtime: profile normalizer/compiler tests.
- Full repository quality: `uv run --frozen pyrepo-check --all`.
