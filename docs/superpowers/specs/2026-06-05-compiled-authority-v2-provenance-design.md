# Compiled Authority V2 Provenance Design

## Status

Draft.

## Spec Mode

`proposed_change`

## Purpose

Fix the authority compiler contract that has repeatedly blocked
`agileforge project create` after spec validation and dry-run success. The root
problem is not the caller spec. The problem is that compiled authority currently
mixes provenance with type-specific invariant semantics:

- Some behavioral invariant parameter schemas carry `source_item_id` and
  `source_level`.
- Other parameter schemas forbid the same fields.
- The compiler therefore emits provenance inside `parameters` inconsistently,
  which strict validation rejects before authority can be saved.

The new contract is:

```text
Invariant = authority semantics + provenance
parameters = type-specific semantic payload only
```

## Current Behavior

`agileforge project create` for the ASA project validates
`specs/spec.json`, succeeds in dry-run, then reaches these durable setup steps:

- `product_created`
- `product_spec_linked`
- `spec_registry_written`
- `spec_marked_approved`

Authority compilation then fails before compiled authority is saved. The latest
failure is:

```text
SPEC_COMPILATION_FAILED: JSON_VALIDATION_FAILED
output: function-after[validate_compact_ir_references(), SpecAuthorityCompilationSuccess].invariants.1.parameters.ForbiddenCapabilityParams.source_item_id: Extra inputs are not permitted
```

This shows the compiler put `source_item_id` inside
`FORBIDDEN_CAPABILITY.parameters`, where the strict schema forbids it.

The current schema in `utils/spec_schemas.py` has:

- `BehavioralAuthorityParams` carrying `source_item_id` and `source_level`.
- Behavioral parameter models inheriting from `BehavioralAuthorityParams`.
- Legacy/core parameter models such as `ForbiddenCapabilityParams`,
  `RequiredFieldParams`, `MaxValueParams`, and `RelationConstraintParams`
  keeping `extra="forbid"` and rejecting provenance fields.

The compiler instructions also currently tell the model to put
`source_item_id` and `source_level` in behavioral `parameters`, reinforcing the
mixed convention.

## Goals

- Make compiled authority provenance placement uniform and obvious.
- Keep `parameters` semantic-only for every invariant type.
- Keep strict schemas with `extra="forbid"`.
- Fail old stored artifacts explicitly rather than silently migrating them.
- Preserve project-create bootstrap gate: successful create stops at pending
  authority review and does not accept authority or advance later phases.
- Keep deterministic normalizer repairs for harmless model drift.
- Add one bounded schema-feedback retry as a safety net, not as the main fix.

## Non-Goals

- No silent migration of stored v1 compiled authority artifacts.
- No weakening of typed parameter schemas.
- No fake source evidence.
- No retry for semantic failures such as source mismatch, over-promotion,
  missing evidence, or unsafe hard constraints.
- No Vision, Backlog, Roadmap, Story, or Sprint behavior change.

## V2 Schema Shape

Add an explicit compiled authority artifact schema version:

```python
SpecAuthorityCompilationSuccess.schema_version = (
    "agileforge.compiled_authority.v2"
)
```

The field should be required on saved compiled authority artifacts. New compiler
output must normalize into this shape before persistence.

The compiler version must also bump:

```python
SPEC_AUTHORITY_COMPILER_VERSION = "2.0.0"
```

The artifact schema version is the storage contract. The compiler version is the
compiler/instruction contract. Both must be visible in saved authority metadata.

Move provenance to top-level invariant fields:

```json
{
  "id": "INV-0123456789abcdef",
  "type": "DATA_CONTRACT",
  "source_item_id": "REQ.persistence",
  "source_level": "MUST",
  "parameters": {
    "subject": "todo records",
    "fields": ["id", "title", "completed"],
    "rule": "records persist across reload"
  }
}
```

`source_item_id` and `source_level` are optional schema fields because some
legacy/core invariants may be source-map-only during normalization. Semantic
validators decide when they are mandatory.

Provenance must not affect deterministic invariant IDs. IDs stay based on
semantic authority only:

```text
hash input = invariant.type + canonical semantic parameters
```

Do not include `source_item_id`, `source_level`, `source_map`, excerpts, or
locations in invariant ID hashing.

All invariant parameter schemas must be semantic-only:

- `ForbiddenCapabilityParams`: `capability`
- `RequiredFieldParams`: `field_name`
- `MaxValueParams`: `field_name`, `max_value`
- `RelationConstraintParams`: `expression`
- `UserInteractionParams`: `trigger`, `target`, `expected_response`
- `StateTransitionParams`: `state`, `trigger`, `outcome`
- `DataContractParams`: `subject`, `fields`, `rule`
- `RouteContractParams`: `route`, `route_name`, `behavior`
- `VisibilityRuleParams`: `target`, `condition`, `visibility`

Remove `source_item_id` and `source_level` from all behavioral parameter
schemas. After strict validation, param-level provenance is invalid.

### Source Map Shape

`source_map` remains the evidence layer. It must map invariant IDs to real source
text from the current `agileforge.spec.v1` source.

`source_item_id` and `source_level` are provenance hints. They are not evidence
by themselves and must not replace `source_map`.

Keep item identity in `SourceMapEntry.location` for now and exact source text in
`SourceMapEntry.excerpt`. Do not add `source_item_id` to `SourceMapEntry` in
this change.

## Old Artifact Failure Behavior

Stored v1 or unversioned compiled authority artifacts must fail closed with a
structured error:

```text
COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED
```

Required remediation:

```text
Regenerate compiled authority for the approved spec version.
```

Loading old projects must not crash. Every reader must surface the structured
error or a domain-specific envelope containing that code and remediation.

No stored artifact migration is allowed. The normalizer may repair only fresh
compiler output before saving v2.

`COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED` must be registered in
`services/agent_workbench/error_codes.py` so CLI/API envelopes can report it
consistently.

### Raw Version Sniff

Stored compiled authority must be version-sniffed before strict Pydantic
validation.

Algorithm:

1. If `compiled_artifact_json` is missing or empty, return `missing`.
2. Parse raw JSON with `json.loads`.
3. If parsing fails, return `invalid_json`.
4. If parsed value is not an object, return `schema_invalid`.
5. Read `schema_version = payload.get("schema_version")`.
6. If `schema_version != "agileforge.compiled_authority.v2"`, return
   `schema_unsupported` with `COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED`.
7. Only after the schema version matches v2, run strict Pydantic validation.

Do not attempt to parse old v1 invariants through the v2 strict schema before
returning `COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED`. This is not a compatibility
migration and not a legacy fallback.

### Central Loader Contract

Introduce or harden a central compiled authority loader that distinguishes:

- missing artifact
- invalid JSON
- unsupported schema version
- v2 schema-invalid artifact
- valid v2 success artifact

The existing `load_compiled_artifact(authority)` returns `None` for multiple
failure modes. V2 implementation should add a typed result or companion helper
so callers can return `COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED` instead of
silently treating old artifacts as missing or invalid.

Exact result shape:

```python
CompiledAuthorityLoadStatus = Literal[
    "ok",
    "missing",
    "invalid_json",
    "schema_invalid",
    "schema_unsupported",
]

class CompiledAuthorityLoadResult(BaseModel):
    ok: bool
    status: CompiledAuthorityLoadStatus
    artifact: SpecAuthorityCompilationSuccess | None = None
    error_code: str | None = None
    message: str | None = None
    remediation: list[str] = []
    observed_schema_version: str | None = None
    spec_version_id: int | None = None
    authority_id: int | None = None
```

Required unsupported result:

```json
{
  "ok": false,
  "status": "schema_unsupported",
  "error_code": "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED",
  "message": "Compiled authority artifact schema is unsupported.",
  "remediation": [
    "Run agileforge authority regenerate --project-id <project_id> --spec-version-id <spec_version_id> --idempotency-key <key>."
  ],
  "observed_schema_version": null
}
```

The legacy `load_compiled_artifact(authority)` may remain as a convenience shim
only if it delegates to the new loader and never hides
`schema_unsupported` in code paths that need operator feedback.

## Backward-Read Inventory

Implementation must audit and update these compiled-authority readers before
the schema is changed.

### Compiler Service

Files:

- `services/specs/compiler_service.py`
- `services/setup_service.py`

Reader surfaces:

- `load_compiled_artifact`
- `_load_acceptance_context`
- `_lookup_reusable_accepted_authority`
- `_compiled_authority_metrics`
- `check_spec_authority_status`
- `get_compiled_authority_by_version`
- product cache updates to `Product.compiled_authority_json`
- tool-context cache writes to `compiled_authority_cached`
- setup rehydration of active project and compiled authority cache

Expected v1 behavior:

- Acceptance must fail with `COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED`, not
  `invalid_artifact` without detail.
- Status/read commands must surface regenerate remediation.
- Caches must not be refreshed from unsupported artifacts.

### Pending Authority And Project Create

Files:

- `services/specs/pending_authority_service.py`
- `services/agent_workbench/project_setup.py`

Reader/writer surfaces:

- pending authority compile result handling
- `project create` authority compile persistence
- `project setup retry` replay of failed setup

Expected v1 behavior:

- Existing unsupported compiled authority cannot satisfy pending authority.
- Regeneration through project setup retry must stop at pending authority review.
- No auto-acceptance may be introduced.

### Authority Review And Decision

Files:

- `services/agent_workbench/authority_review.py`
- `services/agent_workbench/authority_decision.py`
- `services/agent_workbench/authority_projection.py`
- `api.py`

Reader surfaces:

- local `_load_compiled_artifact`
- `_compiled_artifact_shape_findings`
- review packet construction
- authority status projection
- authority fingerprint payloads
- accept/reject guard checks
- dashboard authority review construction
- dashboard story/task authority context helpers

Expected v1 behavior:

- Review must emit a blocking, non-overridable finding with
  `COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED`.
- Accept must not allow unsupported artifacts.
- Status must preserve pending/current distinctions but include unsupported
  artifact remediation when an authority row exists but cannot be used.

### Workflow Projection And Phase Gates

Files:

- `services/agent_workbench/vision_phase.py`
- `services/agent_workbench/backlog_phase.py`
- `services/agent_workbench/roadmap_phase.py`
- `services/agent_workbench/story_phase.py`
- `services/agent_workbench/sprint_phase.py`
- `services/orchestrator_context_service.py`
- `orchestrator_agent/fsm/deterministic_tool_adapters.py`
- `services/phases/backlog_service.py`

Reader surfaces:

- `compiled_authority_cached`
- `compiled_authority_json`
- authority refs extracted from cached JSON
- phase readiness checks
- on-demand authority fallback and active-project cache hydration

Expected v1 behavior:

- Later phases must not proceed from unsupported cached authority.
- Readiness output should report authority regeneration, not generic missing
  state, when unsupported artifact JSON is present.
- Cached strings should be parsed through the central loader or a shared parser,
  not ad hoc `json.loads`.

### Runtime Prompt Inputs

Files:

- `services/vision_runtime.py`
- `services/backlog_runtime.py`
- `services/roadmap_runtime.py`
- `services/story_runtime.py`
- `services/sprint_runtime.py`
- `services/specs/story_validation_service.py`
- `orchestrator_agent/agent_tools/product_vision_tool/tools.py`

Reader surfaces:

- LLM payload fields named `compiled_authority`
- `compiled_authority_json`

Expected v1 behavior:

- Unsupported artifacts must not be passed to downstream agents as if they were
  valid authority.
- Payload builders should return structured precondition errors before invoking
  downstream agents.

### As-Built And Evidence Collection

Files:

- `services/agent_workbench/as_built_assessment.py`
- `services/agent_workbench/evidence_collect.py`

Reader surfaces:

- accepted authority lookup
- raw `json.loads(authority.compiled_artifact_json)`
- `targets_from_compiled_authority`
- source-map-derived authority targets
- raw reads of `invariant.parameters.source_item_id`
- raw reads of `source_map[].source_item_id`

Expected v1 behavior:

- Unsupported artifacts must block assessment/evidence collection with
  regeneration remediation.
- No assessment cache should be written from unsupported authority.
- Target extraction must move from raw `parameters.source_item_id` reads to
  top-level invariant provenance and/or `SourceMapEntry.location`.

### CLI And Dashboard/API Projections

Files:

- `cli/main.py`
- `api.py`
- `services/agent_workbench/application.py`
- dashboard/API modules that expose authority status, review packets, workflow
  status, or active project state

Expected v1 behavior:

- CLI envelopes should include error code, message, remediation, and retryable
  classification.
- JSON output must stay bounded and structured.
- UI/API readers must not crash on unsupported artifacts.

## Regenerate Command Path

Current obvious CLI authority commands are:

- `agileforge authority status`
- `agileforge authority review`
- `agileforge authority accept`
- `agileforge authority reject`
- `agileforge authority invariants`

No standalone CLI command is currently evident for regenerating compiled
authority for an approved spec without moving into acceptance or later phases.

The implementation must add this explicit command/API:

```bash
agileforge authority regenerate \
  --project-id <project_id> \
  --spec-version-id <approved_spec_version_id> \
  --idempotency-key <key>
```

Required behavior:

- Requires an approved `SpecRegistry` row.
- Uses the existing authority compiler and persistence path.
- Forces recompile of the selected spec version.
- Replaces the existing pending compiled authority for that spec version.
- Clears or invalidates stale product/state caches.
- Does not create or accept `SpecAuthorityAcceptance`.
- Does not advance Vision, Backlog, Roadmap, Story, or Sprint.
- Stops with pending authority review and points to `agileforge authority review`.
- Uses mutation-ledger and idempotency semantics because it mutates persistent
  project state.

Request metadata:

- `project_id`: required.
- `spec_version_id`: required and must belong to the project.
- `idempotency_key`: required.
- `changed_by`: optional actor string; default can be CLI/system actor.
- `dry_run`: optional preview. Dry-run must not invoke the compiler or mutate
  authority rows; it only validates guards and reports what would regenerate.

Success data:

- `project_id`
- `spec_version_id`
- `authority_id`
- `pending_authority_id`
- `status: "authority_pending_review"`
- `compiled_authority_schema_version: "agileforge.compiled_authority.v2"`
- `compiler_version: "2.0.0"`
- `authority_fingerprint`
- `next_actions`: `agileforge authority review --project-id <project_id>`

Error set:

- `PROJECT_NOT_FOUND`
- `SPEC_VERSION_NOT_FOUND`
- `SPEC_VERSION_PROJECT_MISMATCH`
- `SPEC_VERSION_NOT_APPROVED`
- `MUTATION_IN_PROGRESS`
- `IDEMPOTENCY_CONFLICT`
- `SPEC_COMPILE_FAILED`
- `COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED` when regeneration is recommended by a
  reader, not when fresh regeneration itself succeeds.

### Accepted Authority Re-Review Rule

Regeneration never deletes historical `SpecAuthorityAcceptance` rows and never
auto-accepts a new authority artifact.

After regeneration:

- Any existing accepted decision whose stored fingerprint/compiler/prompt hash
  no longer matches the regenerated authority is historical only.
- Authority status must no longer report that decision as current.
- Workflow must return to pending authority review.
- Vision/Backlog/Roadmap/Story/Sprint gates must remain locked until the new
  pending authority is reviewed and accepted.
- Review/accept guards must refer to the regenerated pending authority ID and
  fingerprint.

The CLI manual and every unsupported-artifact remediation must name
`agileforge authority regenerate`.

## Normalizer Repair Boundaries

Fresh compiler output may receive pre-validation repair before strict v2
validation.

Allowed deterministic repairs:

- Replace invalid, missing, or non-string `prompt_hash` with host-computed
  `compute_prompt_hash(SPEC_AUTHORITY_COMPILER_INSTRUCTIONS)`.
- Replace placeholder or invalid invariant IDs with deterministic host IDs.
- Default missing `source_map` to `[]` only for otherwise success-shaped output.
- Move misplaced `parameters.source_item_id` and
  `parameters.source_level` to top-level invariant fields, then delete them from
  `parameters`.

Disallowed repairs:

- Do not repair stored compiled authority artifacts.
- Do not run fresh-output repair from stored-artifact loaders.
- Do not invent `source_item_id` or `source_level`.
- Do not invent `source_map` excerpts.
- Do not promote `source_item_id` into evidence.
- Do not change invariant meaning to satisfy schema.
- Do not downgrade semantic/source failures to warnings.
- Do not keep param-level provenance fallback accessors.
- Do not keep `source_item_id` or `source_level` temporarily in behavioral
  parameter schemas.

Ordering:

1. Parse raw JSON or return `INVALID_JSON`.
2. Apply fresh-output pre-validation repairs.
3. Strictly validate v2 schema.
4. Rewrite deterministic IDs and source-map invariant IDs.
5. Repair source-map excerpts only from real source text.
6. Run semantic/source validators.
7. Save only valid v2 success artifacts.

After step 3, no param-level provenance is legal.

## Semantic Source Rules

For structured `agileforge.spec.v1` input:

- `source_item_id` must resolve to an existing spec item ID when present.
- `source_level` must match that item when present.
- Invariants that rely on structured source-level proof must include valid
  top-level `source_item_id` and `source_level`.
- `source_map` must contain real source text from the spec item or acceptance
  text that supports the invariant.
- `source_map.location` should cite the typed item ID when possible.
- When structured source proof is required, at least one of
  `Invariant.source_item_id` or `SourceMapEntry.location` must resolve to the
  real spec item, and the source-map excerpt must resolve to real text from
  that item.
- If `source_map` cannot be resolved to real source text, fail closed.
- `FORBIDDEN_CAPABILITY` remains hard authority only for explicit
  `MUST_NOT`, `NON_GOAL`, or equivalent hard exclusions. It must not be inferred
  from `OPEN_QUESTION`, soft `DECISION`, or unsupported research notes.
- Semantic/source failures return normal compiler validation failure, not retry.

## Compiler Instruction Update

Update `orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions.txt`
to state:

- Every invariant may include top-level `source_item_id` and `source_level`.
- No invariant `parameters` object may include provenance fields.
- Parameter examples must show semantic fields only.
- `source_map` is required evidence and must quote real spec text.
- `source_item_id` is not evidence and does not replace `source_map`.
- If the compiler cannot produce real source evidence, return a failure object
  or an item-ID gap as appropriate.

## Retry Trigger Matrix

Add one bounded schema-feedback retry as a safety net.

| Failure | Retry? | Reason |
| --- | --- | --- |
| `INVALID_JSON` | yes, max 1 | Model returned unparsable JSON. |
| `JSON_VALIDATION_FAILED` | yes, max 1 | Model returned schema-shaped JSON with structural drift. |
| Unsupported stored artifact | no | Stored artifact must be regenerated, not repaired. |
| `SOURCE_METADATA_MISMATCH` | no | Semantic/source failure. |
| over-promotion to hard invariant | no | Semantic failure. |
| missing real source-map evidence | no | Source evidence failure. |
| invocation timeout | no schema retry | Retry belongs to setup recovery policy. |
| provider exception | no schema retry | Retry belongs to provider/resilience policy. |

Retry feedback must include:

- exact first validation error
- statement that full corrected JSON must be returned
- statement that semantics must not be changed to make validation pass
- attempt index and max attempts

Every attempt must be logged in failure artifacts:

- raw output
- normalized failure reason
- first validation error
- feedback text or feedback summary
- whether retry was attempted
- final outcome

## Testing Requirements

### Schema Tests

- v2 success requires `schema_version`.
- v2 invariant supports top-level `source_item_id` and `source_level`.
- all parameter schemas reject `source_item_id` and `source_level`.
- legacy behavioral params containing provenance fail after strict validation.
- `extra="forbid"` remains active.
- compiler version reports `2.0.0`.
- deterministic invariant ID hash ignores provenance fields.

### Stored Artifact Loader Tests

- missing `schema_version` returns `COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED`
  before Pydantic v2 validation.
- wrong `schema_version` returns `COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED`.
- old v1 behavioral params are not parsed through v2 strict schema before
  returning unsupported.
- invalid JSON returns `invalid_json`, not unsupported.
- valid v2 artifact returns `ok` with parsed artifact.
- unsupported response includes remediation naming
  `agileforge authority regenerate`.

### Normalizer Tests

- misplaced `parameters.source_item_id/source_level` move to top-level before
  strict validation.
- both moved fields are deleted from `parameters`.
- fresh-output repair produces v2 artifact.
- stored artifact loader does not apply fresh-output repair.
- prompt-hash and placeholder-ID repairs still pass.
- source-map excerpts are repaired only from real source text.
- no fake source-map excerpts are created.
- no param-level provenance fallback accessor remains after validation.

### Semantic Source Tests

- top-level `source_item_id` resolves to an `agileforge.spec.v1` item.
- `source_level` mismatch fails with `SOURCE_METADATA_MISMATCH`.
- `source_item_id` without supporting source-map evidence fails closed.
- valid source item plus real source-map excerpt passes.
- `source_map.location` resolves to a real spec item when top-level
  `source_item_id` is absent but structured proof is required.
- unresolved `source_map.location` fails closed when structured proof is
  required.
- `FORBIDDEN_CAPABILITY` from `OPEN_QUESTION`, soft `DECISION`, or non-hard
  source fails.
- `FORBIDDEN_CAPABILITY` from hard exclusion passes only with real evidence.

### Unsupported Artifact Reader Tests

Every inventory area above needs at least one v1/unsupported fixture test.

Required assertions:

- no crash
- structured `COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED`
- regeneration remediation present
- downstream phase/agent invocation does not proceed
- no cache refresh from unsupported artifact
- later phases fail closed and point to `agileforge authority regenerate` when
  only v1 authority exists.
- `api.py`, `services/orchestrator_context_service.py`,
  `services/setup_service.py`, as-built/evidence collection, and raw
  provenance-reader paths are covered explicitly.

### Regenerate Command Tests

- approved spec version can regenerate compiled authority.
- draft/unapproved spec version is rejected.
- regenerate saves v2 compiled authority.
- regenerate stops at pending authority review.
- regenerate does not create accepted authority.
- regenerate does not advance Vision, Backlog, Roadmap, Story, or Sprint.
- idempotency replay is stable.
- mutation-ledger records start, progress, success, validation failure, and
  idempotent replay.
- idempotency conflict with different payload is rejected.
- existing accepted authority becomes non-current after regenerated authority
  fingerprint changes.
- workflow points to authority review after regeneration.

### Retry Tests

- `INVALID_JSON` gets one feedback retry.
- `JSON_VALIDATION_FAILED` gets one feedback retry.
- successful retry persists final v2 artifact and logs both attempts.
- second schema failure returns `SPEC_COMPILE_FAILED` with artifact details.
- semantic/source failures do not retry.

### Project-Create Regression

Use saved failure artifacts before live mutation:

- artifact with param-level `source_item_id` normalizes to v2 or reaches the
  next semantic failure.
- artifact with invalid prompt hash still normalizes past prompt hash.
- artifact with placeholder invariant IDs still normalizes past ID validation.

Only after unit and integration tests pass should a guarded real ASA
`project create` be retried.

## Acceptance Criteria

- Compiled authority v2 schema is explicit and strict.
- Stored old artifacts fail closed with `COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED`.
- Stored artifacts are raw-version-sniffed before strict Pydantic validation.
- `COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED` is registered in
  `services/agent_workbench/error_codes.py`.
- Compiler version is `2.0.0`.
- Every compiled-authority reader in the inventory has defined unsupported
  behavior.
- `agileforge authority regenerate` exists, uses mutation-ledger/idempotency,
  and does not advance beyond pending authority review.
- Existing accepted authority becomes non-current after regeneration until the
  regenerated pending authority is reviewed and accepted.
- Compiler instructions match v2 shape.
- Normalizer repairs only fresh output and only deterministic/mechanical drift.
- Semantic validators read top-level provenance and verify real source evidence.
- Deterministic invariant IDs ignore provenance.
- Schema-only retry is bounded to one retry and never runs for semantic/source
  failures.

## Risks

- Reader inventory may miss a cached-string path that bypasses the central
  loader.
- Breaking old artifacts can block active local projects until regenerate is
  available.
- Normalizer repair can become too permissive if it starts inventing evidence.
- Retry feedback can mask prompt/schema contract drift if logs are weak.
- Existing tests may assume behavioral parameters contain provenance; those
  tests must be deliberately rewritten, not mass-updated blindly.

## Open Questions

- Exact dashboard/API response shape for unsupported artifacts.
- Whether `authority regenerate --dry-run` should be implemented in the first
  pass or deferred until after real regenerate idempotency is stable.
- Whether old accepted authority rows need a new explicit `stale` status, or
  whether projection-level fingerprint mismatch is sufficient.
