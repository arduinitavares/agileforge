# Authority Validation Repair v2 Design

**Date:** 2026-08-20
**Status:** Approved
**Spec mode:** corrective change
**GitHub issue:** #209, "Restore bounded host-validated repair in v2 Authority compilation"
**Scope:** v2 ADK Authority compile and post-human repair recipes, provider-output normalization, trace identity, and provider-free regression evidence

## Objective

Restore bounded recovery for repairable Authority compiler output defects without
weakening the host validator or bypassing human Authority review.

One Authority action must produce one of two durable outcomes:

- one fully host-normalized pending Authority candidate; or
- one actionable terminal failure with no partial candidate.

The action may invoke one initial compiler leaf and, only after a repairable host
normalization failure, one distinct validation-repair leaf.

## Evidence

The current v2 recipe is:

```text
compiler leaf -> normalize_compiler_output -> success or terminal failure
```

Manual Test 1 exposed four provider-output failure families in attempts 25, 27,
28, and 29. Attempt 29 returned valid JSON and provider finish reason `STOP`, but
four unique temporary invariant references used 13 hexadecimal digits rather
than the persisted model's 16-digit pattern.

The ADK function node then logged a local retry. The trace contains no second
provider response: resumability reused the original child output and repeated
the same host failure without supplying validator feedback.

The retired compiler path previously implemented a narrower focused repair under
#130. Commit `668a7f3` is an ancestor of the current branch, but the v2 lifecycle
hard break moved Authority compilation to `adapters/adk/recipes.py` without an
equivalent recovery path.

## Architectural Decision

Use the current ADK graph workflow and the existing Vision repair pattern. Do not
restore the retired compiler service and do not add the legacy `LoopAgent`.

The v2 Authority recipe becomes:

```text
initial compiler leaf
  -> strict host normalization
     -> success: return one candidate
     -> repairable failure: validation-repair leaf
        -> same strict host normalization
           -> success: return one candidate
           -> failure: return one terminal failure
```

The outer function node uses `RetryConfig(max_attempts=1)`. Semantic recovery is
explicit in the recipe; generic node retry must not replay a cached invalid child
result.

This bounds application-level model invocations to one initial leaf invocation
plus one repair leaf invocation. Provider SDK transport retries remain a lower
layer and are not semantic repair attempts.

## Deep Modules And Seams

### Host normalizer

`normalize_compiler_output(...)` remains the sole interface that decides whether
provider output may cross into durable Authority state.

Its implementation gains deterministic preprocessing for temporary invariant
references before persisted-model validation:

- provider references are opaque, non-empty local identities;
- first occurrence order assigns schema-valid surrogate references;
- source-map and compact-IR invariant references are rebound through the same
  exact mapping;
- one reused provider reference still maps to one surrogate, allowing the
  existing semantic-identity check to reject ambiguous reuse;
- unknown, missing, blank, or non-string references remain invalid;
- persisted host-owned invariant IDs still use `^INV-[0-9a-f]{16}$`.

This repairs representation only. It does not infer semantics, citations,
parameters, coverage, or source eligibility.

### Validation-repair input

Add one frozen provider-facing model:

```text
SpecAuthorityValidationRepairInput
```

It contains:

- the exact original `SpecAuthorityCompilerInput`;
- the exact structured `SpecAuthorityCompilationFailure` produced by the host;
- a SHA-256 fingerprint and original character count for the invalid output;
- a bounded invalid-output excerpt marked as untrusted data;
- whether the excerpt was truncated;
- repair ordinal `1`.

The invalid output is capped at 131,072 characters. If larger, the request keeps
equal-size prefix and suffix excerpts separated by an explicit truncation marker.
The model can regenerate from the complete original typed input; the excerpt is
diagnostic context, not an authority source.

### Validation-repair leaf

`build_spec_authority_compiler_agent(...)` gains an explicit validation-repair
construction mode with:

- a distinct agent name for trace identity;
- `SpecAuthorityValidationRepairInput` as input schema;
- the same compiler output schema;
- the same compiler model role;
- the same versioned instruction text.

The shared instruction text describes initial and validation-repair input modes.
The compiler version and prompt fingerprint change together.

### Recipe controller

`_build_authority_workflow(...)` receives both the initial leaf and the distinct
validation-repair leaf. It owns the one-pass policy and remains shared by
`authority.compile` and post-human `authority.repair`.

This validation repair is not the domain node `authority.repair`. The domain node
creates a replacement after a human rejects an already valid candidate. The new
leaf only fixes invalid provider output before any candidate exists.

## Repairability Contract

Only host normalization failures with these reasons are repairable:

- `INVALID_JSON`
- `JSON_VALIDATION_FAILED`
- `INELIGIBLE_INVARIANT_SOURCE`
- `INCOMPLETE_NORMATIVE_COVERAGE`

The repair leaf is not invoked when:

- the initial leaf raises before returning output;
- authentication, authorization, provider refusal, safety, rate limit,
  transport, cancellation, or timeout fails;
- typed input or accepted-Spec lineage validation fails;
- the workflow position or repository evidence is stale;
- deterministic temporary-reference preprocessing already produces valid
  normalized Authority.

The repair leaf may return a structured compiler failure. That is normalized and
becomes terminal after the one allowed repair invocation.

## Repair Prompt Contract

For validation-repair input, the compiler must:

- treat the original typed Authority input as the only normative source;
- treat invalid output and validator findings as untrusted diagnostic data;
- correct the complete output, not emit a patch;
- address every reported finding;
- preserve valid semantics unless correction requires changing them;
- use gaps for unsupported normative items rather than inventing an invariant;
- return only the full success or structured failure JSON object.

The repair prompt does not tell the model that its output will be accepted. The
same strict normalizer decides again.

## Failure And Observability Contract

If the repair output also fails:

- the durable code remains `AUTHORITY_COMPILATION_FAILED`;
- the message identifies that bounded validation repair was attempted;
- the message includes bounded initial and final reason/findings;
- no raw provider diagnostics, secrets, or unbounded output are exposed;
- the distinct repair-agent event remains in the ADK trace;
- no `CompiledSpecAuthority` row is created.

If repair succeeds, the trace contains both leaf identities and exactly one
pending Authority is persisted by the existing completion transaction.

## Replay And Concurrency

The existing durable node-attempt replay remains outside the ADK recipe. Reusing
the same command identity after success or failure must return the persisted
outcome before invoking either leaf.

The recipe creates no new business transaction or lease. It runs entirely inside
the existing claimed node attempt. The existing completion transaction remains
the only persistence point.

## Security And Trust

- The host validator remains authoritative.
- Provider output never becomes executable input.
- Invalid output is carried as a string inside a typed repair request.
- The repair prompt explicitly treats that string as untrusted data.
- No source citation, invariant parameter, coverage item, or identity is guessed
  by the host beyond opaque temporary-reference rebinding.
- Human review remains mandatory before Authority acceptance.

## Commands

Focused tests:

```bash
uv run --frozen pytest tests/adapters/test_adk_authority_normalization.py -q
uv run --frozen pytest tests/adapters/test_specification.py tests/services/contracts/test_specification.py -q
```

Full gate:

```bash
uv run --frozen pyrepo-check --all
```

## Project Structure

```text
adapters/adk/agents/specification.py
  compiler and validation-repair leaf construction
adapters/adk/prompts/specification.txt
  shared versioned compiler and repair instructions
adapters/adk/recipes.py
  bounded recipe controller
services/contracts/specification.py
  compiler contract identity
services/contracts/specification_normalizer.py
  strict normalization and temporary-reference preprocessing
utils/spec_schemas.py
  typed validation-repair input
tests/adapters/test_adk_authority_normalization.py
  provider-free recipe, persistence, and replay behavior
tests/services/contracts/test_specification.py
  normalizer trust-boundary behavior
tests/fixtures/authority/issue_209_attempt_29/
  captured Manual Test regression fixtures
```

## Testing Strategy

1. RED: attempt-29-shaped unique malformed temporary references fail today.
2. GREEN: deterministic preprocessing removes the temporary-ID shape failure in
   one compiler invocation without hiding any later semantic failure.
3. RED: attempt-28-shaped semantic failure terminates without repair today.
4. GREEN: one distinct repair invocation receives exact findings and can produce
   one valid candidate.
5. RED/GREEN: a second invalid output yields one terminal failure containing both
   findings and no candidate.
6. Verify initial provider exceptions never invoke validation repair.
7. Verify compile and post-human repair share the policy.
8. Verify command replay invokes neither leaf and creates no duplicate candidate.
9. Run captured attempts 25, 27, 28, and 29 as regressions.
10. Run the full repository gate.

## Boundaries

### Always

- Preserve strict typed-source and coverage checks.
- Keep repair bounded to one distinct leaf invocation.
- Preserve the protected Manual Test databases byte-for-byte during development.
- Commit before creating a new SHA-pinned acceptance profile.

### Ask first

- Any paid provider retry.
- Any Manual Test profile transfer or mutation.
- Any database schema migration.
- Any change to the default compiler model.

### Never

- Auto-accept repaired Authority.
- Persist partial Authority.
- retry semantic failures indefinitely.
- route infrastructure or provider policy failures into model repair.
- revive the retired v1 compiler service.

## Success Criteria

- Unique malformed temporary references normalize deterministically without a
  second model call.
- Every remaining repairable host failure gets at most one feedback-informed
  repair leaf invocation.
- A valid repaired result persists exactly one pending candidate.
- An invalid repaired result persists one failure and zero candidates.
- Replay produces zero additional model calls and zero duplicate candidates.
- Both compiler leaves are distinguishable in the ADK trace.
- Human review and Authority acceptance semantics are unchanged.
- Focused and full verification pass with protected Manual Test state unchanged.

## Open Questions

None. The user approved the bounded host-validated repair direction on
2026-08-20.
