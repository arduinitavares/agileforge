# Accepted Specification Delivery Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILLS: use
> `superpowers:test-driven-development` for every behavior change,
> `superpowers:systematic-debugging` for every unexpected failure,
> `superpowers:verification-before-completion` before every review handoff or
> completion claim, and `superpowers:subagent-driven-development` for execution.

**Goal:** Implement issue #210 by removing compiled Authority from the live
delivery lifecycle and making the exact human-accepted `agileforge.spec.v2`
candidate the sole delivery contract for Backlog, Roadmap, Story, Sprint, Task,
and execution packets.

**Architecture:** Specification acceptance creates one decision-grounded,
immutable registry version. A deep loader proves its exact accepted decision,
candidate bytes, source lineage, project ownership, and hash. Every downstream
artifact carries exact parent identities and stable host-minted item references.
Drafts remain immutable review artifacts; operational Story, Sprint, and Task
rows appear only inside accepted-decision transactions. The change is a fresh-
schema hard break: no migration, Authority compatibility reader, duplicate
contract, or hidden fallback survives.

**Tech stack:** Python 3.12+, Pydantic 2, SQLModel/SQLAlchemy, SQLite, FastAPI,
vanilla JavaScript, pytest/Hypothesis, Ruff, ty, and the repository's uv-only
toolchain.

**Approved design:**
`docs/superpowers/specs/2026-08-21-accepted-specification-delivery-contract-design.md`

## Global constraints

- Work only in
  `/Users/aaat/projects/agileforge/.worktrees/context-grounded-vision-bootstrap`
  on `dev/context-grounded-vision-bootstrap`; create no branch or worktree.
- Preserve the untracked handoff
  `docs/feedback/2026-08-20-issue-210-authority-ir-validation-handoff.md`.
- Do not call a provider, start the Manual Test UI, transfer a profile, click
  Compile, push, merge, close #210, or modify `master`.
- Never mutate `.agileforge/dev/profiles/manual-string-calculator-209-916e9ff`.
  Source fixture extraction uses SQLite read-only immutable connections.
- Make one fresh-schema hard break. Add no migration, dual read/write,
  compatibility model, legacy Task-metadata fallback, or automatic provider
  loop.
- Preserve exact accepted Specification bytes and item IDs. Do not infer
  machine-enforcement categories or semantic relevance from prose.
- Human review remains mandatory for Specification, Backlog, Roadmap, Story,
  and Sprint plan. Hybrid Story validation is explicit and makes exactly one
  provider call only when a human requests it.
- Keep implementation and verification provider-free. Provider behavior is
  exercised only through fakes/fixtures.
- Use `uv run --frozen` for Python project commands. Add no dependency and no
  new broad type/lint suppression.
- Follow RED -> GREEN -> REFACTOR per seam. Unexpected failures require root-
  cause diagnosis before editing.
- This coupled cutover receives one final commit only. At execution start,
  stage the approved docs and this plan, excluding the handoff. After each task,
  review only the unstaged task delta, then stage it as the next checkpoint.
  Never present or commit a partial cutover as complete.
- The full `pyrepo-check` gate includes acceptance-launcher smoke tests that
  intentionally reject a dirty checkout. Run focused checks while dirty, obtain
  all reviews, create the single commit, then run the full gate from the clean
  committed checkout. If a post-commit correction is necessary, amend that same
  unpushed commit after RED/GREEN/review and rerun the full gate.

## Fixed implementation rulings

- Keep the existing `SpecRegistry` composite FK to its source candidate. Add the
  accepted-decision composite FK; both are required because exact bytes and the
  human decision are separate relational proofs.
- Implement deep loading in `services/specs/accepted_specification.py` with one
  typed `AcceptedSpecificationIntegrityError` and stable string codes.
- Implement exact artifact ancestry/current-leaf selection in
  `services/planning_lineage.py`; repositories project facts but do not invent a
  second lineage algorithm.
- Backlog acceptance creates no placeholder `UserStory` rows. Delete the old
  placeholder-based Backlog replacement guard; exact Specification and artifact
  currentness isolate historical work.
- Operational Story rows have no `superseded_by_story_id`. Whole-artifact
  replacement may change cardinality, so continuity is represented only by
  `StoryArtifact.supersedes_story_artifact_id`; acceptance marks prior same-chain
  rows `is_superseded=True` atomically.
- Hybrid Story validation stays an explicit service/tool action. No graph node,
  default configuration, or transition invokes it automatically.
- Sprint stream IDs are `SPS-` plus one UUID4 hex value. A collision returns
  `SPRINT_PLAN_STREAM_ID_COLLISION`; there is no hidden retry. Idempotent replay
  reuses the persisted ID.
- Do not add Task cancellation to #210. Existing completion/review/close behavior
  stays; already-cancelled Task status remains terminal.
- Declare one reviewed fresh-schema signature manifest independently of SQLModel
  metadata. Assert normalized SQLModel metadata equals it, then compare inspected
  existing tables against that same manifest. Explicitly forbid the six
  retained-column project-global uniques listed by the design.
- Map concurrent acceptance/activation `IntegrityError` failures with the
  existing rollback-and-reload pattern to stable stale/collision domain errors;
  never leave an accepted decision without its registry or operational rows.

## Task 1: Freeze issue #210 evidence and write top-level RED seams

**Files:**

- Create: `tests/fixtures/issue_210/legacy_authority/outer-envelope.json`
- Create: `tests/fixtures/issue_210/legacy_authority/compiler-input.json`
- Create: `tests/fixtures/issue_210/legacy_authority/authority-input.json`
- Create: `tests/fixtures/issue_210/legacy_authority/initial-output.json`
- Create: `tests/fixtures/issue_210/legacy_authority/repaired-output.json`
- Create: `tests/fixtures/issue_210/legacy_authority/manifest.json`
- Create: `tests/fixtures/issue_210/gold/specification-candidate.json`
- Create: `tests/fixtures/issue_210/gold/canonical-specification.json`
- Create: `tests/fixtures/issue_210/gold/manifest.json`
- Create: `tests/issue_210/test_fixture_integrity.py`
- Create: `tests/issue_210/test_direct_specification_lifecycle.py`
- Update: `tests/workflow/test_fresh_project_schema.py`

- [x] Read attempt 30 from trace session
  `sha256:aef95c5d48c71877c35d2ea950cbdac088cc4629612a2229b093a9ff73fbc0b8`
  using an immutable read-only SQLite URI. Write the five exact payloads without
  reformatting or semantic normalization.
- [x] Assert exact byte counts and SHA-256 values from the audit handoff:
  `10154/5e18990c...57eb`, `9943/111a61e6...7467`,
  `9728/34bbc829...9a19`, `8749/88f091dc...a1c6`, and
  `8721/4670cc02...7594`.
- [x] Read accepted String Calculator candidate 2 and its canonical payload from
  the protected business DB through an immutable read-only connection. Assert
  payload/spec hash
  `sha256:4f39ae394d3910bc52d73256eddc11edd66e57074025e1ec7f037e8e69a33025`
  and candidate fingerprint
  `sha256:f8714ebde7f56a1de259fa8df4283be6521881a814e036df5c61d33a1a1110ee`.
- [x] Prove the gold payload contains the complete item set, including
  `DATA.001`, and round-trips through the current v2 canonicalizer byte-for-byte.
- [x] Write one provider-free lifecycle test expressing the approved outcome:
  accepting the exact Specification enables Backlog directly; no Authority fact,
  node, input field, table, or review step is required.
- [x] Add baseline/mixed-schema RED fixtures and assertions for old Authority
  tables, retired columns, all-fresh columns with any of the six old global
  uniques, and a missing UserStory replay unique.
- [x] Run the new tests. Record RED as missing direct-Specification behavior, not
  fixture corruption. Run `git diff --check`.

## Task 2: Bind accepted registry versions to exact decisions and add the deep loader

**Files:**

- Update: `models/product_definition.py`
- Update: `models/specs.py`
- Create: `services/specs/accepted_specification.py`
- Update: `services/specs/__init__.py`
- Update: `workflow/handlers/product_discovery.py`
- Update: `workflow/facts.py`
- Update: `repositories/workflow.py`
- Create: `tests/services/specs/test_accepted_specification.py`
- Update: `tests/workflow/test_product_definition_models.py`
- Update: `tests/workflow/test_product_definition_facts.py`
- Update: `tests/workflow/test_specification_acceptance_revalidation.py`

- [x] Add RED model tests for the unique decision tuple and composite
  `SpecRegistry` decision FK while retaining the exact candidate FK.
- [x] Add RED loader tests for exact historical/current loads and all closed
  codes: missing/foreign version, non-accepted or mismatched decision, candidate
  identity mismatch, canonical bytes/hash mismatch, broken source lineage, and
  ambiguous current registry rows. The exact loader must succeed for both
  `approved` and `superseded`; the current loader selects only the sole approved
  row and returns `None` only when none exists. A separate new-planning guard
  maps a valid superseded exact load to `STALE_SPECIFICATION`.
- [x] Require `SpecRegistry.source_specification_decision_id`; remove duplicated
  `approved_at`, `approved_by`, and `approval_notes`; add the partial unique
  current-approved index.
- [x] Define frozen `AcceptedSpecification` with project ID, registry version and
  hash, accepted decision ID/metadata, source candidate identity/fingerprint,
  canonical JSON, and parsed `SpecificationPayload`.
- [x] Reuse `load_candidate_contract`, `canonical_spec_json`, and
  `canonical_spec_hash`; never reconstruct a payload from review prose.
- [x] In `execute_decide_specification`, insert/flush the terminal accepted
  decision, then branch explicitly: initial acceptance requires zero current
  registry rows; amendment acceptance requires exactly one current row matching
  its base and mutates/flushes that row to `superseded`. Only then insert/flush
  the decision-bound registry, all inside the outer transaction. Classify
  only the expected partial-unique/base race as `STALE_SPECIFICATION`, roll back
  everything, and reload current lineage. Exact replay returns the same decision
  and registry identities with no unpaired decision.
- [x] Change `SpecVersionFact` and repository loading to derive acceptance from
  the bound accepted decision, not copied approval columns.
- [x] Run focused model/loader/acceptance tests, Ruff, ty, and `git diff --check`.

## Task 3: Make the fresh schema and exact artifact identities authoritative

**Files:**

- Update: `models/workflow.py`
- Update: `models/core.py`
- Update: `models/specs.py`
- Update: `models/db.py`
- Update: `models/__init__.py`
- Update: `agile_sqlmodel.py`
- Update: `repositories/project.py`
- Delete: `models/authority_curation.py`
- Create: `services/planning_lineage.py`
- Create: `tests/services/test_planning_lineage.py`
- Update: `tests/workflow/test_fresh_project_schema.py`
- Update: `tests/workflow/test_product_definition_models.py`
- Update: `tests/test_user_story_model_import_boundary.py`

- [x] Add RED metadata tests for every exact unique, FK, partial index, check,
  required column, and retired reference in the design.
- [x] Replace Backlog Authority fields with Specification identity and lineage-
  scoped history/fingerprint uniques. Scope Roadmap history/fingerprints to its
  exact Backlog parent.
- [x] Replace Story prose/row identities with exact Backlog artifact/item,
  Roadmap artifact, and host Story-item identity; scope history to one exact
  Backlog item.
- [x] Replace Sprint-plan `sprint_id` with exact Specification identity and
  `sprint_plan_stream_id`; add `activated_sprint_id` and accepted/null check to
  decisions.
- [x] Rebuild `UserStory` with immutable Story artifact/item fingerprints,
  exact Specification version/hash, canonical
  `spec_item_ids_json`, and `acceptance_criteria_json`. Remove seed/refinement,
  prose/slot, archive/reset, mutable criteria history, and row-pointer fields.
- [x] Keep Backlog identity on the immutable `StoryArtifact`; `UserStory` loaders
  derive and verify its Backlog artifact/item through the exact Story parent.
- [x] Add a schema assertion that `UserStory` has no direct Backlog artifact/item
  identity column.
- [x] Make `Task.metadata_json` required with no default.
- [x] Delete `CompiledSpecAuthority`, `SpecAuthorityAcceptance`, all Authority
  curation tables/relationships, and Authority model registration in this same
  schema slice. Update project deletion ordering and SQLModel export boundaries.
- [x] Implement exact parent walking, transitive accepted-leaf selection, cycle,
  cross-key, branch, and ambiguity detection in `services/planning_lineage.py`.
  This module owns chain-key validation, first-version rules, immediate-prior
  supersession, next-version allocation, accepted-leaf cardinality, and Sprint
  stream/current-cycle selection; phase services and repositories call these
  operations instead of recreating partial lineage algorithms.
- [x] Declare a reviewed complete structural-signature manifest independent of
  SQLModel metadata. Assert the normalized fresh SQLModel metadata equals it.
- [x] Extend `_assert_current_business_schema` to run before `create_all`, allow
  only empty or exactly current schemas, reject retired references and forbidden
  broad uniques, and compare inspected signatures to the same manifest. Preserve
  `UNSUPPORTED_BUSINESS_SCHEMA`.
- [x] Prove exact baseline and every mixed fixture fail before any schema write;
  prove empty/fresh databases succeed and each required-signature mutation fails.
- [x] Run focused schema/model/lineage tests, Ruff, ty, and `git diff --check`.

## Task 4: Replace prose and invariant contracts with stable item references

**Files:**

- Update: `services/contracts/backlog.py`
- Update: `services/contracts/roadmap.py`
- Update: `services/contracts/story.py`
- Update: `services/contracts/sprint.py`
- Update: `services/contracts/specification_validation.py`
- Create: `services/contracts/specification_references.py`
- Create: `tests/services/contracts/test_backlog.py`
- Update: `tests/services/contracts/test_roadmap.py`
- Update: `tests/services/contracts/test_story.py`
- Update: `tests/services/contracts/test_sprint.py`
- Create: `tests/services/contracts/test_specification_references.py`

- [x] Split provider-owned ID-free Backlog output from the host canonical
  Backlog envelope. Validate unique priorities and normalized display text, sort
  canonically, then host-mint `PBI-000001...PBI-999999`.
- [x] Validate canonical sorted non-empty `spec_item_ids` against the exact
  accepted Specification. Reject unknown/duplicate/non-canonical references in
  one bounded pass.
- [x] Make Roadmap releases carry ordered `backlog_item_ids`, each parent item
  exactly once, with no copied prose as identity.
- [x] Split provider Story output from host canonical Story content; require one
  to eight items and host-mint only `US-0001...US-0008`, preserve ordered
  acceptance criteria as JSON, and compute
  per-item fingerprints over the complete immutable content.
- [x] Define the closed canonical Story item exactly as the design lists and use
  one shared host persona parser across output validation, activation,
  structural validation, and tests. Persist only the explicitly mapped
  operational subset; resolve all other reviewed planning content from the
  immutable artifact.
- [x] Require every Story evidence set to be a non-empty subset of its exact
  Backlog item's evidence set and every Task evidence set to be a non-empty
  subset of its exact Story item's evidence set. Every evidence set must contain
  at least one qualifying normative Specification item type/level.
- [x] Add root Specification identity/canonical JSON exactly once to Story and
  Sprint inputs. Story/Task children carry only stable evidence ID sets.
- [x] Replace invariant bindings with `relevant_spec_item_ids`; delete
  `validate_task_invariant_bindings` and any lexical Authority cue gate.
- [x] Add property tests for canonical permutations, bounds, duplicate/unknown
  references, Unicode acceptance criteria, and fingerprint sensitivity. Add
  explicit rejection tests for zero or nine Story items and for any empty or
  whitespace-only acceptance-criterion element; preserve all other criterion
  bytes and ordering on round-trip/replay.
- [x] Run focused contract tests, Ruff, ty, and `git diff --check`.

## Task 5: Cut workflow facts and graph directly from Specification to Backlog

**Files:**

- Update: `workflow/facts.py`
- Update: `repositories/workflow.py`
- Update: `workflow/definitions/root.py`
- Update: `workflow/definitions/product_discovery.py`
- Update: `workflow/definitions/backlog.py`
- Update: `workflow/definitions/planning.py`
- Update: `workflow/definitions/execution.py`
- Update: `workflow/requests/product_definition.py`
- Update: `workflow/requests/planning.py`
- Update: `workflow/requests/__init__.py`
- Update: `workflow/handlers/product_definition.py`
- Update: `workflow/handlers/planning.py`
- Update: `workflow/handlers/__init__.py`
- Update: `workflow/domain.py`
- Delete: `workflow/definitions/authority.py`
- Delete: `workflow/requests/authority.py`
- Delete: `workflow/handlers/authority.py`
- Delete/replace: `tests/workflow/test_authority_graph.py`
- Delete/replace: `tests/workflow/test_authority_transitions.py`
- Delete/replace: `tests/workflow/test_authority_restart.py`
- Update: `tests/workflow/test_single_project_graph.py`
- Update: `tests/workflow/test_vision_backlog_graph.py`
- Update: `tests/workflow/test_vision_backlog_transitions.py`
- Update: `tests/workflow/test_planning_graph.py`
- Update: `tests/workflow/test_planning_joins.py`
- Update: `tests/workflow/test_planning_transitions.py`
- Update: `tests/workflow/test_execution_graph.py`
- Update: `tests/workflow/test_execution_transitions.py`

- [x] Write graph RED tests: exact current accepted Specification plus Product
  Goal enables `backlog.generate`; Authority has no node, edge, fact, action, or
  reference.
- [x] Remove Authority facts/loaders and Authority fields from planning facts.
  Project immutable artifact items and exact accepted-decision Specification
  identity instead of normalized prose identities.
- [x] Route `Specification -> Backlog -> Roadmap -> Story -> Sprint -> Execution`.
  All transitions use the single lineage service for current accepted leaves.
- [x] Update request/handler unions to exact new lineage fields and stable item
  IDs. Reject wrong project, version/hash, parent artifact, item, or stale review
  before persistence.
- [x] Add accepted-A -> feedback-B -> accepted-C tests for Backlog, Roadmap,
  Story, and Sprint-plan ancestry. B cannot displace A; C becomes current only on
  acceptance.
- [x] Add amendment tests proving old drafts/reviews are stale, accepted history
  remains readable, old loose Stories cannot plan, and active Sprint lineage is
  the only execution exception.
- [x] Run focused graph/transition tests that can execute during the hard break,
  plus Ruff, ty, and `git diff --check`.

## Task 6: Build direct-Specification application/runtime inputs

**Files:**

- Update: `services/application.py`
- Update: `services/roadmap_runtime.py`
- Update: `services/story_runtime.py`
- Update: `services/sprint_selection.py`
- Update: `adapters/adk/agents/backlog.py`
- Update: `adapters/adk/agents/roadmap.py`
- Update: `adapters/adk/agents/story.py`
- Update: `adapters/adk/agents/sprint.py`
- Update: `adapters/adk/prompts/backlog.txt`
- Update: `adapters/adk/prompts/roadmap.txt`
- Update: `adapters/adk/prompts/story.txt`
- Update: `adapters/adk/prompts/story_patch.txt`
- Update: `adapters/adk/prompts/sprint.txt`
- Update: `adapters/adk/recipes.py`
- Update: `adapters/adk/model_roles.py`
- Update: `tests/test_roadmap_runtime.py`
- Update: `tests/test_story_runtime.py`
- Update: `tests/test_sprint_selection.py`
- Update: `tests/adapters/test_adk_workflow_runner.py`

- [x] Replace `_DeliveryLineage` and every delivery input builder with the deep
  accepted-Specification loader plus exact current artifact parents.
- [x] Serialize the complete canonical gold Specification exactly once at the
  invocation root; assert `DATA.001` and every other gold item survives Backlog,
  Roadmap, Story, Sprint, and semantic-review input construction unchanged.
- [x] Remove `technical_spec`, `compiled_authority`, Authority caches,
  invariant-ID fields, and partial duplicate Specification copies from runtime
  contracts and prompts.
- [x] Prompts state that the accepted Specification is source of truth, evidence
  IDs are required where schema declares them, supported behavior is not a
  generic gap, and humans judge semantic relevance.
- [x] Preserve provider-free replay/idempotency and normalize every fake provider
  result through the same production contract. No automatic validation call is
  introduced.
- [x] Run focused runtime/adapter tests, Ruff, ty, and `git diff --check`.

## Task 7: Persist immutable Backlog and Roadmap artifacts only

**Files:**

- Update: `services/agent_workbench/backlog_phase.py`
- Update: `services/agent_workbench/roadmap_phase.py`
- Update: `repositories/workflow.py`
- Update: `services/read_projections.py`
- Delete obsolete prose-link code from: `services/story_linkage.py`
- Update: `tests/workflow/test_vision_backlog_transitions.py`
- Update: `tests/workflow/test_planning_transitions.py`
- Update: `tests/services/test_durable_product_definition_projections.py`
- Delete/replace: `tests/test_story_linkage.py`

- [x] Make Backlog recording validate the exact Specification/Goal, mint the
  canonical PBI items, and persist only one immutable artifact.
- [x] Delete `persist_accepted_backlog_in_session`,
  `_story_from_validated_backlog_item`, placeholder Story creation, and
  `_blocks_backlog_replacement`.
- [x] Make Backlog feedback/rejection append only a decision; acceptance changes
  currentness only through exact artifact ancestry.
- [x] Make Roadmap recording resolve ordered PBI IDs from its exact Backlog,
  require every parent item exactly once, and persist only immutable content.
- [x] Scope version/fingerprint history and read projections to exact parent
  lineage; duplicate prose never serves as a foreign key.
- [x] Backlog and resolved Roadmap review projections show every cited
  Specification item's title, statement, level, acceptance criteria, and
  verification method from the exact accepted version.
- [x] Prove accepted A stays byte-identical/current after feedback B and accepted
  C switches only inside C's transaction for both phases.
- [x] Run focused Backlog/Roadmap transition/projection tests, Ruff, ty, and
  `git diff --check`.

## Task 8: Activate whole Story artifacts only after human acceptance

**Files:**

- Update: `services/agent_workbench/story_phase.py`
- Update: `repositories/story.py`
- Update: `services/story_dependencies.py`
- Update: `services/story_feedback_quality.py`
- Update: `workflow/planning_integrity.py`
- Update: `workflow/handlers/planning.py`
- Update: `services/application.py`
- Update: `tests/workflow/test_planning_transitions.py`
- Update: `tests/test_story_dependencies.py`
- Update: `tests/test_story_feedback_quality.py`
- Delete/replace: `tests/test_create_user_story.py`

- [x] Make Story draft recording validate exact Backlog/Roadmap/item lineage,
  mint canonical `US-*` items and fingerprints, and write only `StoryArtifact`.
- [x] Delete draft-time operational writers and prose/slot delete/patch paths.
  Feedback/rejection must produce zero `UserStory` mutations.
- [x] On exact artifact acceptance, transactionally materialize all immutable
  UserStory rows, canonical criteria/evidence IDs, and Specification pins. Replay
  creates no duplicates; any row failure rolls back the decision and all rows.
- [x] For same-chain accepted replacement, mark all prior current rows
  `is_superseded=True` and create all new rows atomically; do not assign
  one-to-one replacement pointers.
- [x] Rebuild targeted correction: resolve selected operational row to its exact
  artifact item, accept one provider replacement through a fake in tests, copy
  untouched immutable items, and record a complete reviewable replacement
  artifact. No direct row mutation or partial acceptance remains.
- [x] Story review projection resolves each Story evidence set to exact
  Specification titles, statements, levels, acceptance criteria, and
  verification methods before the human decision.
- [x] Reject dependency edges across Specification lineages and recompute
  planning integrity from immutable item IDs.
- [x] Prove accepted A -> feedback B leaves A selectable and byte-identical;
  accepting C atomically switches the same Backlog-item lineage.
- [x] Run focused Story/dependency/transition tests, Ruff, ty, and
  `git diff --check`.

## Task 9: Replace Authority-derived Story validation with bounded direct-Spec review

**Files:**

- Update: `services/specs/story_validation_service.py`
- Update: `services/contracts/specification_validation.py`
- Update: `utils/spec_schemas.py`
- Update: `tools/spec_tools.py`
- Update: `services/specs/__init__.py`
- Update or delete: `scripts/apply_story_validation.py`
- Delete: `scripts/dry_run_story_validation.py`
- Delete: `scripts/eval_spec_validation.py`
- Delete: `scripts/_debug_validation_detail.py`
- Rewrite: `tests/test_story_validation_service.py`
- Rewrite: `tests/test_story_validation_pinning.py`
- Rewrite: `tests/test_spec_validation_modes.py`
- Rewrite: `tests/test_alignment_evidence_persistence.py`
- Delete/replace: `tests/test_dry_run_story_validation.py`
- Delete/replace: `tests/test_apply_story_validation_script.py`
- Delete/replace: `tests/test_eval_spec_validation.py`

- [x] Replace modes with `structural` and explicit `hybrid`; remove default-env
  auto-hybrid behavior and provider-only mode.
- [x] Structural validation evaluates the complete finite rule set in canonical
  code order and returns all applicable bounded failures in one pass. Implement
  exactly `STORY_ACCEPTANCE_INVALID`, `STORY_ITEM_BINDING_INVALID`,
  `SPECIFICATION_BINDING_INVALID`, `SPEC_ITEM_REFERENCES_INVALID`,
  `STORY_STATEMENT_INVALID`, and `ACCEPTANCE_CRITERIA_INVALID`; all are blocking
  and v2 defines no structural warning code.
- [x] Add a provider-free rule-matrix test that triggers every code alone and in
  valid combinations, proves exact ordering and dependency applicability, and
  proves any code yields `ready_for_sprint=False`.
- [x] Add bounded `StorySpecificationReviewOutput` with at most 50 findings,
  exact allowed codes, exact item-boundary rules, duplicate-pair rejection,
  `complete=True`, and `compliant == not findings`.
- [x] Hybrid makes exactly one injected fake provider call in tests, performs no
  repair/retry, and maps malformed/truncated/out-of-bound output to
  `STORY_SPECIFICATION_REVIEW_INVALID`.
- [x] Replace `ValidationEvidence` with v2 exact Story/Backlog/Specification
  identities, canonical content fingerprint, structural results, semantic state,
  findings, and derived reference set. The input fingerprint includes the exact
  persona plus current `story_points` and `rank`; recompute it before persistence
  and planning so readiness repair stales earlier evidence.
- [x] Implement the design's explicit rule prerequisite/subcheck map and matrix
  fixtures for missing artifact, missing item, broken Specification, and broken
  Backlog parents; never fabricate dependent findings.
- [x] Remove `FORBIDDEN_CAPABILITY`, `REQUIRED_FIELD`, invariant summaries, and
  all Authority lexical compatibility logic. Also delete the cue-based
  connectivity, zero-millisecond, hard-coded scope-placeholder, and redundant
  persona-warning rules.
- [x] Keep failed evidence diagnostic but not ready. Only passing current v2
  evidence makes an accepted Story eligible for Sprint planning.
- [x] Run focused validation/pinning tests, Ruff, ty, and `git diff --check`.

## Task 10: Make Sprint plans immutable drafts and activate them atomically

**Files:**

- Update: `services/agent_workbench/sprint_phase.py`
- Update: `utils/task_metadata.py`
- Update: `services/sprint_selection.py`
- Update: `workflow/execution_integrity.py`
- Update: `workflow/planning_integrity.py`
- Update: `workflow/handlers/planning.py`
- Update: `services/application.py`
- Update: `tests/workflow/test_planning_transitions.py`
- Update: `tests/workflow/test_execution_transitions.py`
- Update: `tests/workflow/test_execution_recovery.py`
- Update: `tests/test_task_execution_service.py`
- Update: `tests/services/contracts/test_sprint.py`
- Update: `tests/test_sprint_selection.py`

- [x] Host-mint/replay the Sprint plan stream ID and hash the complete host
  envelope, including team, task-decomposition flag, exact Specification,
  candidate set, and validated provider output.
- [x] Preserve one ordered boundary: `WorkflowDomain` opens its existing SQLite
  `BEGIN IMMEDIATE` transaction, then authoritatively replays or claims the
  receipt inside it. Exact replay returns without the handler; a new claim
  continues in the same transaction through lineage stream selection, Sprint
  host-envelope construction, fingerprinting, and insertion. Add no read-only
  preflight/second transaction. Remove caller/provider-authored stream IDs and
  plan fingerprints.
- [x] Implement the stream allocation state machine in the lineage service:
  feedback and rejected successors reuse their prior stream; an accepted
  unstarted plan may only be replaced inside that stream; a new stream may be
  minted only after the prior stream's activated Sprint starts or reaches a
  terminal state, or when a new Specification version/hash becomes current.
  Prove the service cannot yield two accepted unstarted current streams.
- [x] Add the full stream matrix: first mint, idempotent replay, feedback reuse,
  rejected-plan reuse, accepted-unstarted reuse, post-start new stream, post-end
  new stream, amended-Specification new stream, forbidden parallel stream,
  concurrent distinct idempotency keys, and one-shot collision returning
  `SPRINT_PLAN_STREAM_ID_COLLISION`.
- [x] Sprint-plan review projection renders exact Task descriptions plus every
  resolved cited Specification title, statement, level, acceptance criterion,
  and verification method before acceptance.
- [x] Draft recording writes only `SprintPlanArtifact`; it must not create/update/
  delete Team, Sprint, SprintStory, or Task rows.
- [x] Feedback/rejection writes only the decision. Acceptance transactionally
  resolves/creates Team and planned Sprint, materializes membership/Tasks, and
  stores required `activated_sprint_id`.
- [x] Replacement in one stream may reuse the Sprint only while unstarted and
  PLANNED with no SprintStart, execution log, completion evidence, or terminal
  Story/Sprint fact. Otherwise fail closed without altering accepted A.
- [x] Replace Task metadata with strict `task_metadata.v2`; remove v1/default
  fallback. Verify exact plan, Story, Specification, evidence IDs, and Task
  content at StartSprint and execution boundaries.
- [x] Start resolves Sprint only through the sole current accepted plan decision,
  rejects stale Specification as `STALE_SPECIFICATION`, and rejects concurrent
  active Sprint as `ACTIVE_SPRINT_EXISTS`.
- [x] Prove active old-lineage Sprint completion/review/close and packet reads
  remain possible; a loose old Story/Task cannot use that exception.
- [x] Run focused Sprint/execution tests, Ruff, ty, and `git diff --check`.

## Task 11: Rebuild canonical packets and remove Authority operator surfaces

**Files:**

- Update: `services/packets/canonical.py`
- Update: `services/packet_renderer.py`
- Update: `services/read_projections.py`
- Update: `services/application.py`
- Update: `api.py`
- Update: `cli/main.py`
- Update: `cli/workflow_commands.py`
- Update: `frontend/project.html`
- Update: `frontend/project.js`
- Update: `tests/test_canonical_packets.py`
- Update: `tests/test_packet_renderer.py`
- Update: `tests/adapters/test_api_workflow_domain.py`
- Update: `tests/adapters/test_cli_workflow_domain.py`
- Update: `tests/adapters/test_command_renderer.py`
- Update: `tests/e2e/test_single_project_lifecycle_ui.py`

- [x] Build packets from the deep accepted-Specification loader, immutable
  artifact items, canonical acceptance criteria, and strict v2 Task metadata.
- [x] Render exact referenced Specification items and explicit `current` or
  `superseded`; never substitute the current version for pinned execution.
- [x] Delete Authority read projection methods, status/card payloads, application
  ports/methods, API request models/routes/builders, CLI protocols/parsers/
  handlers/mappings, dashboard card/actions/polling, and workflow command text.
- [x] UI lifecycle becomes Specification accepted -> Backlog available. All
  later human review controls remain and API/CLI/UI tests prove those controls
  expose the exact resolved Specification evidence defined in Tasks 7, 8, and
  10 rather than raw IDs alone.
- [x] Add transport/static tests proving no Authority endpoint, command, action,
  card, prompt field, or live projection remains and no raw ID/hash is requested
  from a human.
- [x] Run packet/API/CLI/frontend tests, JavaScript checks, Ruff, ty, and
  `git diff --check`.

## Task 12: Delete dead Authority production, tests, benchmarks, and live docs

**Files:**

- Delete: `services/authority_compilation_input.py`
- Delete: `services/authority_review_projection.py`
- Delete: `services/agent_workbench/authority_projection.py`
- Delete: `services/contracts/authority.py`
- Delete: `services/contracts/authority_input_v2.py`
- Delete: `services/contracts/specification.py`
- Delete: `services/contracts/specification_normalizer.py`
- Delete: `services/specs/authority_curation_diff.py`
- Delete: `services/specs/authority_quality.py`
- Delete: `services/specs/authority_selection.py`
- Delete: `services/specs/compiler_service.py`
- Delete: `adapters/adk/agents/authority.py`
- Delete: `adapters/adk/agents/specification.py`
- Delete Authority recipe/role/prompt registrations
- Delete: `utils/spec_authority_ir.py`
- Delete: `utils/spec_authority_assumptions.py`
- Delete: `utils/authority_curation_trace.py`
- Delete Authority scripts, `benchmarks/authority-quality/`, live generated
  Authority artifacts/SQL, and Authority-only tests
- Update: `README.md`
- Update: `CONTEXT.md`
- Update: `docs/adr/0005-use-accepted-specification-as-delivery-contract.md`
- Create: `tests/issue_210/test_authority_surface_removed.py`

- [x] Verify Task 3 already removed all Authority models/tables/relationships and
  registration; delete only the remaining non-schema production consumers here.
- [x] Remove Authority-only agents, prompts, recipes, model roles, configuration,
  scripts, benchmarks, generated artifacts, SQL, exports, and tests. Preserve
  only historical docs, the issue-210 legacy fixture, and schema/deletion tests.
- [x] Prune mixed-use `utils/spec_schemas.py` and configuration to only surviving
  direct-Specification/Story validation types.
- [x] Update README/current context to show direct Specification lifecycle and
  the explicit semantic caveat; do not rewrite historical ADRs/handoffs as if
  Authority never existed.
- [x] Static boundary test scans production modules, metadata, graph, routes,
  CLI, frontend, prompts, recipes, model roles, scripts, benchmarks, README, and
  generated artifacts for the prohibited live names in the design.
- [x] Allow Authority terms only in historical docs, the exact legacy fixture,
  old-schema sentinel/tests, and the deletion test itself.
- [x] Run the deletion/static tests, import collection, Ruff, ty, and
  `git diff --check`.

## Task 13: Cross-cut regression, independent reviews, one commit, and clean gate

**Files:**

- Update any focused test/production file only for a proven regression.
- Update this plan's checklist and the plan-specific SDD ledger.
- Preserve the untracked handoff unchanged.

- [x] Run the provider-free gold lifecycle end-to-end: accepted Specification ->
  Backlog -> Roadmap -> Story artifact -> Story acceptance/materialization ->
  structural validation -> Sprint plan -> Sprint acceptance/activation -> start.
- [x] Prove every input contains the exact gold Specification once, all stable
  item references resolve, `DATA.001` remains supported behavior, and no
  Authority artifact or generic gap appears.
- [x] Run accepted-A -> feedback-B -> accepted-C seams for every artifact phase,
  amendment/currentness/active-Sprint seams, atomic rollback/replay tests, exact
  baseline/mixed schema rejection, and attempt-30 fixture integrity.
- [x] Run focused test groups in fresh processes, Ruff, ty, static deletion
  inventory, `git diff --check`, and inspect the complete diff/stat/status.
- [x] Obtain fresh independent correctness, scope, and lean reviews. Fix every
  valid in-scope finding RED-first and obtain clean re-reviews.
- [x] Recompute protected profile file/logical hashes, SQLite integrity/FK checks,
  table/row counts, profile metadata, listener status, and String Calculator
  repository branch/commit/tree/cleanliness. Compare with the recorded baseline.
- [x] Stage everything except the preserved handoff, verify the staged diff, and
  create one scoped commit. Confirm the worktree is clean except the expected
  untracked handoff.
- [x] Run the complete gate from the clean committed checkout:

```bash
uv run --frozen pyrepo-check --all
```

- [x] If the clean gate exposes a real issue-210 regression, write a RED test,
  fix it, rerun focused reviews, amend the same unpushed commit, and rerun the
  complete gate. Do not suppress the launcher clean-check.
- [x] Record the final commit SHA and repeat protected-state comparison after all
  verification. Do not run Manual Test or any paid/provider action.

## Verification index

- Exact evidence: `tests/issue_210/test_fixture_integrity.py`.
- Accepted decision/bytes/lineage: deep-loader and Specification acceptance tests.
- Fresh hard break: model metadata and `test_fresh_project_schema.py`.
- Stable item IDs/references: contract and planning-lineage tests.
- Direct graph: single-project, Vision/Backlog, planning, execution tests.
- Immutable reviews/atomic activation: phase transition tests.
- Direct Specification inputs: runtime/adapter gold fixture tests.
- Story validation v2: structural/hybrid/pinning tests.
- Sprint/Task integrity: planning/execution/recovery tests.
- Pinned packets/currentness: canonical packet/renderer tests.
- No live Authority: static boundary plus API/CLI/UI graph tests.
- Repository quality: `uv run --frozen pyrepo-check --all` from the clean commit.

## Completion report contract

Report the reproduced audit verdict, proven root cause, chosen direct-
Specification design, rejected alternatives, changed files, RED/GREEN and full-
gate results, independent review verdicts, final commit SHA, exact protected-
state comparison, residual semantic risk, and this sole next manual step:
initialize a fresh acceptance profile pinned to the verified commit and inspect
the accepted Specification review before authorizing any paid retry.
