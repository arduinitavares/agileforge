# Phase 5: Iterative Authority Compiler

## Goal

Make structured `agileforge.spec.v1` authority compilation resilient to single-shot
LLM omission by compiling accepted high-priority spec items through a focused item
pass and merging the normalized authority output.

## Constraints

- Do not use deterministic prose extraction as the semantic judge.
- Use structured spec item IDs and metadata as the deterministic coverage scaffold.
- Preserve the existing compiler seam and test monkeypatch behavior.
- Keep persisted artifacts compatible with the current authority schema.
- Keep benchmark-specific TodoMVC guardrails out of production compiler logic.

## Plan

1. Add failing compiler-service tests that prove single-shot compilation misses
   accepted `MUST`/`MUST_NOT` item coverage while an iterative path invokes the
   compiler once per accepted high-priority item and merges all outputs.
2. Add structured-spec helpers to identify accepted `MUST`/`MUST_NOT` items and
   produce item-focused `agileforge.spec.v1` payloads without changing item text.
3. Add a merge helper for normalized success artifacts that deduplicates
   invariants, source maps, gaps, assumptions, scope themes, and feature rules.
4. Wire preview and persisted compile paths to use the iterative structured-spec
   compiler path while preserving failure envelopes and legacy plain-text behavior.
5. Add a generic source-item coverage gate so accepted high-priority items must
   appear as authority, source-map evidence, or explicit gaps.
6. Run focused tests first, then `pyrepo-check`.

## Definition Of Done

- Compiler-service tests prove per-item invocation and merged coverage.
- Missing focused item coverage returns a structured failure envelope.
- Existing preview and persisted compile behavior remains compatible.
- `pyrepo-check` passes without new suppressions.
