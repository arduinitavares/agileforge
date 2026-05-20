# Structured Authority Trust Boundary Design

Date: 2026-05-19
Status: Draft for user review
Branch context: `dev/authority-coverage-matrix-phase-2e`

## Problem

AgileForge now supports `agileforge.spec.v1` structured technical specs, but the authority review path still carries legacy host-derived semantic checks. The compiler receives structured spec JSON, then host code still derives requirement candidates and authority mappings and uses those derived findings to block acceptance.

That violates the intended trust boundary:

- LLM/spec authoring produces semantic structure.
- LLM authority compiler interprets the structured spec into compiled authority.
- Host code validates structure, freshness, schema, and references.
- Human or agent reviewer accepts or rejects semantic correctness.

The host must not parse prose or typed spec items and decide whether semantic authority coverage is good enough. That is what caused the current failure: a human approved a reviewable compiled authority, but `AUTHORITY_COVERAGE_INCOMPLETE` blocked acceptance because host-derived candidate coverage was incomplete.

## Goals

- Make `agileforge.spec.v1` JSON the only accepted input for authority compilation.
- Remove legacy Markdown authority compilation behavior.
- Remove host-derived requirement candidate coverage from review acceptability.
- Preserve structural safety: schema validity, source hash freshness, review token freshness, compiled artifact validity, and valid source references.
- Make the human workflow usable without manual `/tmp`, `jq`, token-copying, or candidate override rituals.

## Non-Goals

- No automatic Markdown-to-structured-spec conversion in `project create`.
- No legacy Markdown compatibility flag.
- No host semantic extraction fallback.
- No broad deterministic “coverage completeness” gate based on generated candidates.
- No attempt to prove semantic perfection before allowing human acceptance.

## Input Contract

Authority compilation accepts only valid `agileforge.spec.v1` JSON.

Valid:

```text
specs/spec.json
schema_version = "agileforge.spec.v1"
```

Invalid:

```text
specs/app.md
plain text
arbitrary JSON
rendered Markdown review view
```

When a non-structured spec is passed to `project create`, AgileForge returns:

```text
SPEC_SOURCE_FORMAT_UNSUPPORTED
Expected agileforge.spec.v1 JSON.
Generate structured spec first, then retry with specs/spec.json.
```

The remediation should point to the structured spec generation path or a future explicit conversion command. `project create` must not silently convert Markdown.

## Review And Acceptance Trust Boundary

Host code may block authority acceptance only for structural or freshness failures:

- invalid `agileforge.spec.v1` JSON
- schema validation failure
- source content hash changed since review
- bad or missing review token when token is required
- compiled authority missing or corrupt
- compiler output invalid JSON/schema
- source references malformed or pointing to nonexistent spec item IDs
- review packet cannot be fully generated

Host code must not block acceptance for semantic judgments such as:

- uncovered requirement candidate
- weak candidate mapping
- low invariant count
- inferred compiler assumption
- sparse source map
- host-derived coverage incompleteness

The review packet is a human review artifact, not an automated semantic judge. It should show the compiled authority, source spec summary/items, compiler assumptions, gaps, source references, structural diagnostics, and accept/reject guidance.

Acceptance means a human or authorized agent reviewed the compiled authority and chose to make it canonical.

## Compiler Output Contract

The authority compiler reads structured spec items and emits compiled authority:

```text
spec.json typed items
-> LLM compiler
-> compiled authority
```

Compiled authority may include:

- `invariants`
- `eligible_feature_rules`
- `rejected_features`
- `gaps`
- `assumptions`
- `source_map`

The following must not be required for acceptance and must not appear as public blockers in the structured spec path:

- `requirement_candidates`
- `authority_mappings`
- `AUTHORITY_CANDIDATE_*`
- `AUTHORITY_COVERAGE_INCOMPLETE`

Source references are structural references, not proof of semantic perfection. A `source_ref` must point to an existing structured spec item ID such as `REQ.current-budget`. If an excerpt is present, it should be bounded and reviewable. Host code checks that referenced item IDs exist; it does not judge whether the reference is semantically ideal.

If the compiler produces no source references, the review packet may warn with `SOURCE_REFS_MISSING`, but acceptance remains allowed after human review.

If the compiler references nonexistent spec item IDs, review and accept block with `SOURCE_REF_INVALID` until the authority is regenerated or corrected.

## Human Workflow

The desired basic flow is:

```bash
agileforge project create --name "caRtola" --spec-file specs/spec.json
```

The command should return a clear next step:

```text
Project created.
Authority pending review.
Next: agileforge authority review --project-id 2 --open
```

Review should have a simple human path:

```bash
agileforge authority review --project-id 2 --open
```

The command should write stable local files, not require the user to manage `/tmp` manually:

```text
.agileforge/reviews/authority-review-project-2.md
.agileforge/reviews/authority-review-project-2.json
```

Accept should be simple:

```bash
agileforge authority accept --project-id 2
```

The CLI may auto-use the latest fresh review token when it can prove freshness. Advanced token mode remains available:

```bash
agileforge authority accept --project-id 2 --review-token <token>
```

Reject remains explicitly guarded:

```bash
agileforge authority reject \
  --project-id 2 \
  --review-token <token> \
  --idempotency-key authority-reject-2-001 \
  --reason "Compiled authority misses budget rule."
```

## Removal Scope

Remove or hard-disable these behaviors:

- legacy Markdown authority compilation
- host Markdown parser as authority acceptance gate
- requirement candidate extraction for acceptability
- candidate-to-authority coverage blocking
- candidate-specific override UX
- `AUTHORITY_COVERAGE_INCOMPLETE` blocking

Because the new product decision is a clean break, legacy compatibility should not be retained behind a hidden flag.

## Test Requirements

Tests must prove:

- `.md` spec passed to `project create` fails with `SPEC_SOURCE_FORMAT_UNSUPPORTED`.
- valid `agileforge.spec.v1` JSON creates a pending authority.
- structured review packet has no public `requirement_candidates`.
- structured review packet has no public `authority_mappings`.
- structured review packet has no `AUTHORITY_CANDIDATE_*` findings.
- structured review packet has no `AUTHORITY_COVERAGE_INCOMPLETE` blocker.
- accept succeeds despite compiler assumptions, gaps, sparse source refs, or weak semantic mappings when structural checks pass.
- accept blocks stale review/source hash.
- accept blocks invalid source reference item IDs.
- `project create` points to a simple human review command.

## Documentation Updates

Documentation must state:

- `spec.json` is the canonical source of truth.
- `spec.md` is a rendered human review view only.
- Markdown is not accepted as authority compilation input.
- Host validation is structural/freshness validation only.
- Human approval decides semantic acceptability.

## Open Questions

- Whether `agileforge spec profile convert` should be built next as an explicit LLM-assisted conversion command.
- Whether `.agileforge/reviews/` should be configurable.
- Whether `authority review --open` should open Markdown in the editor, browser, or both.
