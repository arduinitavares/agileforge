# Decision Hard-Ban Filter Design

## Goal

Allow AgileForge authority compilation to continue when the model promotes a non-normative structured `DECISION` item into a hard `FORBIDDEN_CAPABILITY` invariant, without weakening the existing fail-closed metadata validation for other source types.

## Problem

`agileforge project create` for the ASA project reached authority compilation but failed with `SOURCE_METADATA_MISMATCH`. The compiler emitted a `FORBIDDEN_CAPABILITY` invariant sourced from `DECISION.research-before-algorithm`, whose structured source item has no hard level. The current metadata validator correctly rejects this as over-promotion because the source is not `MUST`, `MUST_NOT`, or `NON_GOAL`.

The failure is a useful safety signal, but killing the entire project bootstrap for a non-normative decision note is too strict. The host normalizer should remove this unsafe hard invariant before final metadata validation, then preserve the validation guard for other over-promotions.

## Design

Patch `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`.

Before `_structured_authority_metadata_errors(...)` runs, add a narrow filtering pass over normalized `SpecAuthorityCompilationSuccess`:

- Consider only invariants where `invariant.type == InvariantType.FORBIDDEN_CAPABILITY`.
- Use existing `source_map` references to find structured source item IDs.
- If all known source items for that invariant are `DECISION` items with `level is None`, remove the invariant.
- Remove `source_map` entries that reference removed invariant IDs.
- Add one bounded assumption note such as `Excluded non-normative DECISION item from hard forbidden authority.`.

The filter should not affect:

- `NON_GOAL` sourced hard bans.
- `MUST` or `MUST_NOT` sourced hard bans.
- over-promoted `REQ`, `QUALITY`, `CONSTRAINT`, `INTERFACE`, `DATA`, or other non-hard items.
- invariants that have mixed source evidence where at least one known source item is not a non-normative `DECISION`.

## Testing

Add regression coverage in `tests/test_spec_authority_compiler_normalizer.py`.

Required cases:

- A `FORBIDDEN_CAPABILITY` sourced only from `DECISION.*` with no level normalizes successfully and is filtered out.
- Its `source_map` references are removed.
- A sibling valid invariant remains.
- The assumption note is added once.
- Existing over-promotion tests still return `SOURCE_METADATA_MISMATCH`.
- The saved ASA failure artifact normalizes past the specific DECISION hard-ban blocker, or the helper-level test proves the exact rule.

## Non-Goals

- Do not edit the ASA spec.
- Do not weaken `SOURCE_METADATA_MISMATCH` globally.
- Do not allow all `DECISION` items to support hard authority.
- Do not continue AgileForge workflow past pending authority review.
