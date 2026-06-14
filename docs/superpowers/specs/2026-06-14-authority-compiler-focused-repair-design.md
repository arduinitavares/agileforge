# Authority Compiler Focused Repair Design

**Date:** 2026-06-14
**Status:** In review
**Spec mode:** proposed_change
**GitHub issue:** #130, "Make authority compiler robust to invalid source maps and repeated placeholder IDs"
**Scope:** Authority compile/regenerate CLI/API contracts, compiler model override, focused structured-item repair retry, source-metadata diagnostics, and mutation idempotency

## Revision History

- 2026-06-14: Drafted after auditing #130 and confirming that current source-map
  strictness must remain fail-closed while unresolved compile recovery needs a
  model override and focused item repair path.
- 2026-06-14: Amended after pre-implementation review to pin the regenerate
  ledger hash boundary, restrict v1 focused repair to repairable behavioral
  source-evidence failures, define compiler-model plumbing, and require
  per-invocation schema-disable evaluation.

## Summary

AgileForge should keep rejecting compiled authority when an invariant claims
source evidence that the structured spec cannot support. It must not silently
repair invented or mismatched evidence.

The missing recovery path is narrower:

- let operators choose a compiler model per authority compile/regenerate request;
- when structured authority compilation fails for a known source item, retry only
  that source item with rich validator feedback;
- validate the focused retry with the same strict source-map rules;
- merge the repaired source item's authority only if validation passes;
- return precise failure diagnostics when repair cannot produce trustworthy
  evidence.

This design reuses the existing `spec_authority_compiler_agent` as a focused
repair mode. It does not introduce a new agent.

## Current Behavior

The authority compiler already has several project-agnostic protections:

- schema retry is bounded to JSON/schema failures;
- placeholder and deterministic ID normalization exists for many compiler
  drift cases;
- source-map validation remains strict and rejects unsupported or invented
  evidence;
- quality-gate logic preserves source-map evidence when merging duplicate
  authority items.

The remaining #130 pain point is operational:

- `SOURCE_METADATA_MISMATCH` failures can force manual model configuration via
  `MODEL_CONFIG_PATH`;
- CLI/API commands do not expose a per-run compiler model override;
- retry behavior does not use the validator's exact source-metadata failure as
  rich feedback for a focused source-item repair;
- final failure messages can be too opaque for agents to identify which source
  item, invariant, and evidence excerpt need attention.

## Goals

- Expose a guarded `--compiler-model` option on authority compilation commands.
- Include compiler model selection in mutation request hashes so idempotency
  replay cannot hide different model choices.
- Support focused repair retries for structured spec items when source metadata
  validation identifies a repairable source item.
- Use rich retry feedback containing the failing invariant id, source item id,
  source level, invalid source-map evidence, validator reason, and allowed repair
  behavior.
- Validate repaired output with the same strict normalizer/source-map checks as
  normal compilation.
- Merge only the repaired source item's authority output into the broader
  compiled artifact.
- Preserve fail-closed behavior for invented evidence, ambiguous source support,
  unresolved source items, and non-structured specs.
- Improve user/agent diagnostics when focused repair fails.

## Non-Goals

- Do not weaken `SOURCE_METADATA_MISMATCH` globally.
- Do not accept source evidence that does not appear in the structured source
  item text.
- Do not add a second authority compiler agent.
- Do not make a broad whole-spec retry the default recovery for source metadata
  failures.
- Do not change authority review/acceptance semantics.
- Do not auto-accept authority after repair.
- Do not implement brownfield scope creation, scope-extension rituals, backlog,
  roadmap, story, or sprint behavior.
- Do not change model defaults in `config/models.yaml` as part of this design.

## Public CLI Contract

### Authority Compile

`agileforge authority compile` keeps its existing guards and adds an optional
compiler model:

```bash
agileforge authority compile \
  --project-id <project_id> \
  --spec-version-id <spec_version_id> \
  --expected-spec-hash <sha256> \
  --expected-state SETUP_REQUIRED \
  --expected-setup-status authority_compile_required \
  --compiler-model openrouter/openai/gpt-5.2 \
  --idempotency-key <key>
```

`--compiler-model` is optional. When omitted, AgileForge uses the configured
`spec_authority_compiler` model from `MODEL_CONFIG_PATH` or the default model
configuration.

### Authority Regenerate

`agileforge authority regenerate` also accepts the optional compiler model:

```bash
agileforge authority regenerate \
  --project-id <project_id> \
  --spec-version-id <spec_version_id> \
  --compiler-model openrouter/openai/gpt-5.2 \
  --idempotency-key <key>
```

The model override is a compile-time option, not a persisted project setting.
Each future regeneration must supply the override again if the operator wants to
use a non-default model.

## API Contract

Dashboard/API authority compile requests add an optional `compiler_model` field.
Request models continue to reject unknown fields.

```json
{
  "spec_version_id": 9,
  "expected_spec_hash": "abc123",
  "expected_state": "SETUP_REQUIRED",
  "expected_setup_status": "authority_compile_required",
  "idempotency_key": "compile-20260614-001",
  "compiler_model": "openrouter/openai/gpt-5.2"
}
```

The API does not need a public regenerate endpoint for this design unless one
already exists. If such an endpoint exists, it must mirror the CLI contract.

## Idempotency And Audit Contract

Compiler model selection affects the mutation result and must be part of every
normalized request hash for authority compile/regenerate.

Required behavior:

- same idempotency key + same compiler model + same guards replays the original
  response;
- same idempotency key + different compiler model returns the existing
  idempotency conflict behavior;
- dry-run responses include the selected compiler model;
- mutation ledger responses and failure artifacts include the selected compiler
  model in bounded metadata;
- default model selection is represented as `compiler_model=null` or
  `compiler_model_source="default_config"` consistently in response metadata.

`AuthorityCompileRequest` already owns a `normalized_request_hash()` method, so
the implementation should add `compiler_model` there.

`AuthorityRegenerateRequest` currently does not own a request-hash method; the
regenerate runner builds the ledger hash inline before calling
`MutationLedgerRepository.create_or_load(...)`. The implementation must either
add a request-hash helper and use it, or update the existing inline hash. It
must not add a dead method that the runner never calls.

## Compiler Model Plumbing

The compiler model override must travel through the existing compile call chain
without relying on `MODEL_CONFIG_PATH`.

Required authority compile path:

```text
CLI/API request
-> AgentWorkbenchApplication.authority_compile(...)
-> AuthorityCompileRequest.compiler_model
-> ProjectSetupMutationRunner._run_authority_compile(...)
-> ProjectSetupMutationRunner._ensure_pending_authority(...)
-> engine_bound_compiler(...)
-> compile_spec_authority_for_version_with_engine(..., compiler_model=...)
-> compiler_service._invoke_compiler_for_version(...)
-> compiler_service._run_compiler_attempt(...)
-> compiler_service._compile_spec_authority_output(...)
-> compiler_service._invoke_and_normalize_spec_authority(...)
-> compiler_service._invoke_spec_authority_compiler(...)
-> compiler_service._default_invoke_spec_authority_compiler(...)
```

Required authority regenerate path:

```text
CLI request
-> AgentWorkbenchApplication.authority_regenerate(...)
-> AuthorityRegenerateRequest.compiler_model
-> AuthorityRegenerateRunner._compile_authority(...)
-> compile_spec_authority_for_version_with_engine(..., compiler_model=...)
-> same compiler_service invocation chain
```

The injection seam is the compiler service's default invocation path:
`_default_invoke_spec_authority_compiler(...)` and
`_invoke_spec_authority_compiler_async(...)`. The effective compiler invocation
seam must still preserve existing tests that monkeypatch
`_invoke_spec_authority_compiler`.

## Compiler Agent Construction

The existing compiler agent remains the only authority compiler agent.

When no override is provided, AgileForge continues to use the existing
`root_agent` from `orchestrator_agent.agent_tools.spec_authority_compiler_agent`.

When `compiler_model` is provided, the compiler service builds an equivalent
agent instance for that invocation:

- same name, description, instruction, input schema, output schema,
  `output_key`, and transfer restrictions;
- same OpenRouter API key and extra body;
- model id from the request instead of `get_model_id("spec_authority_compiler")`.

The override-agent constructor must evaluate
`is_spec_compiler_schema_disabled()` at construction time. It must not copy the
module-level singleton's already-captured `output_schema`, because tests and
runtime flags can change schema-disable behavior before the override invocation.

This keeps the repair mode a prompt/input specialization of the existing agent
instead of a separate agent class.

## Focused Repair Trigger

Focused repair is available only when all of these are true:

- the input spec is structured `agileforge.spec.v1`;
- normalization/validation returns a `SpecAuthorityCompilationFailure`;
- the failure reason is `SOURCE_METADATA_MISMATCH`;
- failure details identify one or more source item ids;
- every repair-targeted failure is a repairable behavioral source-evidence
  failure, meaning the validator found that a behavioral invariant's
  `source_item_id` lacks supporting real `source_map` evidence;
- every targeted source item id exists in the current structured spec artifact.

If the failing source item cannot be identified, AgileForge must not invent a
target. It returns the original failure with improved diagnostics.

Focused repair must not run for these v1 sub-causes:

- legacy/core `FORBIDDEN_CAPABILITY` over-promotion from a non-hard source item;
- invariant evidence sourced only from `EXAMPLE` items;
- unknown source item ids;
- missing source item ids where no unambiguous target item can be determined;
- source-level mismatch that cannot be tied to one known target item.

The implementation should add structured metadata error sub-codes before
focused repair logic depends on these cases. The minimum required sub-code for
v1 focused repair is:

```text
BEHAVIORAL_SOURCE_EVIDENCE_UNSUPPORTED
```

This sub-code corresponds to the existing error family:

```text
<invariant_id> source_item_id <source_item_id> lacks supporting real source_map evidence.
```

Other source metadata failures may remain fail-closed diagnostics until a later
design expands repair safely.

If multiple source items fail, AgileForge may retry each item independently up
to the configured bounded retry count. A failed repair for any required item
keeps the full authority compile failed.

## Focused Repair Input

Focused repair invokes the existing compiler contract with a reduced
`spec_source` containing only the targeted structured source item and any
minimal surrounding structured metadata needed for schema-valid input.

The retry `domain_hint` must include concrete validator feedback:

```text
Your previous authority output failed source metadata validation.

Repair target:
- source_item_id: INTERFACE.job-create-api
- source_level: MUST
- failing invariant_id: INV-f3e41ab3a853a790
- failure reason: source_item_id lacks supporting real source_map evidence
- invalid source excerpt: <bounded excerpt when available>

Retry only this source item.
Use only source_map excerpts that appear verbatim in the source item text.
Do not invent source references or source levels.
If the source item cannot support an invariant, omit that invariant or return a
blocking gap.
Return only valid compiled authority JSON.
```

The exact text can vary, but the fields above are required whenever the
validator exposes them.

In focused repair feedback, `source_level` means the expected level from the
structured source item. If the failing invariant emitted a different level, the
feedback must include it separately as `observed_source_level`.

## Merge Contract

Focused repair output is accepted only after it passes the same normalizer,
postcondition, and source-map checks as normal compiler output.

When a focused repair succeeds:

- remove authority invariants, assumptions, and source-map entries derived from
  the targeted source item in the original normalized output;
- insert the repaired source item's validated invariants, assumptions, and
  source-map entries;
- re-run postcondition and authority-quality checks on the merged artifact;
- persist only the final merged artifact.

When a focused repair fails:

- preserve the original failed compile response as the primary failure;
- append focused repair diagnostics showing attempted item ids, retry reasons,
  retry raw-output previews, and validation failures;
- do not persist a partial compiled authority.

For v1, "derived from the targeted source item" means
`invariant.source_item_id == <target_source_item_id>`. If a future source-map
classifier supports non-behavioral/source-map-only invariants, it must be added
under a separate design or amendment.

## Failure Diagnostics

`SOURCE_METADATA_MISMATCH` and focused-repair failures must expose enough
information for agents to act without reading raw artifacts manually.

Required response fields or nested details:

- `error_code` or `error`;
- `reason`;
- `failure_stage`;
- `spec_version_id`;
- `content_ref`;
- `invalid_invariant_id`, when known;
- `source_item_id`, when known;
- `source_level`, when known;
- `source_excerpt`, bounded and only when safe to expose;
- `repair_attempted`;
- `repair_item_ids`;
- `repair_result`;
- `source_metadata_subcode`, when known;
- `suggested_commands`, including the authority command with
  `--compiler-model` when a model override is a reasonable next action.

Failure artifacts may contain richer diagnostics, but CLI/API JSON must include
a bounded actionable summary.

## Existing Behavior To Preserve

Current strict behavior is part of the product contract:

- invented excerpts remain rejected;
- excerpts with real prefixes plus invented middle text remain rejected;
- existing bad source-map entries are not blindly backfilled;
- non-normative source items cannot support hard authority unless an existing
  narrow filter explicitly allows removal of unsafe hard invariants;
- schema retry remains bounded and does not become an infinite repair loop.

## Acceptance Criteria

- `agileforge authority compile --help` documents `--compiler-model`.
- `agileforge authority regenerate --help` documents `--compiler-model`.
- API authority compile accepts `compiler_model` and still rejects unknown
  fields.
- `AuthorityCompileRequest.normalized_request_hash()` changes when
  `compiler_model` changes.
- the inline authority-regenerate ledger hash or extracted helper changes when
  `compiler_model` changes.
- Compiler service invokes the default configured model when no override is
  supplied.
- Compiler service invokes the requested model when an override is supplied.
- An override compiler agent re-evaluates
  `is_spec_compiler_schema_disabled()` at construction time.
- A structured `SOURCE_METADATA_MISMATCH` with a
  `BEHAVIORAL_SOURCE_EVIDENCE_UNSUPPORTED` sub-code and identifiable source item
  triggers a focused retry for that item.
- A structured `SOURCE_METADATA_MISMATCH` caused by legacy modality promotion or
  example-only evidence does not trigger focused repair.
- Focused retry receives rich validator feedback in `domain_hint`.
- Successful focused retry replaces only the targeted source item's authority
  output and persists a fully validated merged artifact.
- Failed focused retry does not persist partial authority and returns bounded
  diagnostics.
- Existing tests that prove fail-closed behavior for invented/bad source
  evidence still pass.

## Testing Requirements

Add focused tests at the service and command boundaries:

- CLI parser routes `--compiler-model` for `authority compile`.
- CLI parser routes `--compiler-model` for `authority regenerate`.
- API compile request accepts `compiler_model`.
- API compile request rejects misspelled or unknown model fields.
- Request hashes include compiler model selection.
- Compiler service builds/uses the override model for a single invocation.
- Override-agent construction observes the current
  `is_spec_compiler_schema_disabled()` value.
- Default compilation remains unchanged when no override is provided.
- Focused repair is attempted for a structured behavioral source-evidence
  failure with a known source item.
- Focused repair is not attempted for legacy modality promotion or example-only
  evidence failures.
- Focused repair is not attempted for non-structured specs or unknown source
  item ids.
- Focused repair domain hint includes the validator's failing source item,
  invariant id, expected source level, observed source level when mismatched,
  reason, and bounded excerpt when available.
- Successful focused repair merges only the targeted source item.
- Failed focused repair surfaces diagnostics and persists no authority row.
- Existing source-map strictness tests remain green.

## Risks

- Focused repair can become too complex if failure details do not reliably carry
  source item ids. The implementation must keep the fallback simple: no known
  source item means no focused repair.
- Creating per-invocation agent instances can bypass assumptions in tests that
  monkeypatch the module-level `root_agent`. Tests should assert behavior
  through the compiler invocation seam rather than relying on module globals.
- Model override strings are operator-controlled input. The implementation must
  pass them only to the LLM model constructor and must not treat them as file
  paths, shell commands, or config keys.

## Open Questions

- Should `workflow next` generated `authority compile` commands mention
  `--compiler-model` only in remediation text, or include it as an optional
  placeholder? Recommendation: keep generated runnable commands minimal and put
  override guidance in blocked/failure remediation.
- Should focused repair retry exactly once, matching the existing schema retry
  count, or have its own count? Recommendation: one focused repair attempt per
  failing source item in v1.

## Spec Self-Review

- Placeholder scan: no placeholder tokens remain.
- Internal consistency: the design keeps strict validation while adding only
  explicit model override and focused repair recovery.
- Scope check: this is one implementation plan touching authority
  compile/regenerate boundaries and compiler service behavior only.
- Ambiguity check: focused repair trigger, merge behavior, and non-goals are
  explicit; open questions do not block v1 implementation.
