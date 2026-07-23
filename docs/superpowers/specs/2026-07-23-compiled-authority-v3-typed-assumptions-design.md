# Compiled Authority V3 Typed Assumptions Design

**Date:** 2026-07-23
**Status:** In review
**Spec mode:** proposed_change
**GitHub issue:** #195, "Ground structured status and count claims in compiler assumptions"
**Scope:** Compiled-authority schema, compiler output contract, assumption
normalization, stored-artifact loading, authority review, quality processing,
curation, and regeneration

## Summary

Compiled authority v2 stores assumptions as `list[str]`. Authority review can
ground only one exact sentence:

```text
Only <normative_item_id> was|is|were|are accepted
```

Equivalent false claims pass when the compiler changes the wording. Regex
expansion cannot make arbitrary English deterministic.

Compiled authority v3 will replace string assumptions with a strict
discriminated union:

- ordinary free-text assumptions;
- item-status claims;
- accepted-normative-item count claims; and
- accepted-normative-item set claims.

The host will validate every structured claim against the canonical structured
spec before persistence and again during authority review. A singleton accepted
set represents exclusivity. Claim-like prose is not allowed in a free-text
entry.

This is a deliberate breaking storage-contract change. V2 artifacts will not be
adapted or migrated in place. Existing unsupported-artifact handling will direct
operators to `agileforge authority regenerate`, which produces a new v3
authority candidate and returns the project to pending authority review.

## Context

`SpecAuthorityCompilationSuccess` currently uses a strict Pydantic schema with
`extra="forbid"`, but `assumptions` is unstructured:

```python
assumptions: list[str]
```

`_compiled_assumption_findings()` searches each string with one regular
expression. With two accepted normative items, current `master` blocks:

```text
Only REQ.alpha was accepted.
```

It does not block:

```text
REQ.alpha is the sole accepted item.
One item was accepted: REQ.alpha.
CONSTRAINT.beta is draft.
No item except REQ.alpha was accepted.
```

These are not four independent bugs. They are evidence that structured facts
are being transported through an unstructured interface.

The repository already has the correct breaking-change boundary:

- the stored artifact declares a schema version;
- readers reject unsupported schema versions before strict parsing;
- unsupported artifacts block later phases;
- remediation names `agileforge authority regenerate`; and
- regeneration creates a new pending authority instead of silently accepting it.

V3 uses that boundary instead of adding a v2 compatibility layer.

## Goals

- Make structured assumption claims explicit and statically typed.
- Make the correct representation easier than claim-like free text.
- Ground claim values and provenance against the canonical structured spec.
- Fail closed before persistence when a claim is false, ambiguous, malformed,
  or cannot be grounded.
- Recheck stored claims during authority review.
- Preserve ordinary non-claim assumptions through an explicit free-text
  variant.
- Keep schemas strict with `extra="forbid"`.
- Use one active compiled-authority schema and one compiler contract.
- Remove the legacy exclusivity regex and its string-parsing helpers.

## Non-Goals

- No general natural-language fact checking.
- No silent conversion of v2 strings into v3 free-text entries.
- No in-place mutation of stored v2 artifacts.
- No dual v2/v3 reader or compatibility union.
- No automatic acceptance after regeneration.
- No unrelated cleanup of compact-IR fields or other compiled-authority
  concepts.
- No Vision, Backlog, Roadmap, Story, or Sprint behavior change beyond the
  existing unsupported-authority gate.

## Decision

### Version Boundary

The stored schema and compiler instruction contract both advance:

```text
schema_version = agileforge.compiled_authority.v3
compiler_version = 3.0.0
```

`COMPILED_AUTHORITY_SCHEMA_VERSION` will name v3. The stored loader will support
v3 only. A v2 artifact will return the existing
`COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED` result before Pydantic validation.

The failure envelope schema version also advances to v3 so success and failure
objects describe one compiler contract.

### Assumption Union

`SpecAuthorityCompilationSuccess.assumptions` becomes:

```python
AuthorityAssumption = Annotated[
    (
        FreeTextAssumption
        | ItemStatusAssumptionClaim
        | AcceptedNormativeCountAssumptionClaim
        | AcceptedNormativeSetAssumptionClaim
    ),
    Field(discriminator="kind"),
]

assumptions: list[AuthorityAssumption]
```

Every variant uses `ConfigDict(extra="forbid")`. Its `kind` field is a required
`Literal` matching exactly one of `free_text`, `item_status`,
`accepted_normative_count`, or `accepted_normative_set`. Union trial order must
not affect parsing or validation errors.

### Free-Text Assumption

```json
{
  "kind": "free_text",
  "text": "Audit evidence is retained with each authority decision."
}
```

`text` must be non-empty after trimming. It must not contain the bounded
structured-claim cues defined below.

### Structured Claim Provenance

Every structured claim carries:

```json
{
  "source": "structured_spec",
  "artifact_id": "SPEC.authority-review",
  "source_item_ids": ["REQ.alpha"]
}
```

`artifact_id` must equal the canonical `TechnicalSpecArtifact.artifact_id`.
`source_item_ids` must be unique. Duplicate IDs are invalid and are never
silently removed. After uniqueness validation succeeds, the normalizer stores
the list in lexical order.

Provenance emitted by the compiler is untrusted input. The normalizer validates
it against the canonical spec. It does not invent, widen, or silently repair
semantic evidence.

### Item-Status Claim

```json
{
  "kind": "item_status",
  "item_id": "CONSTRAINT.beta",
  "status": "draft",
  "provenance": {
    "source": "structured_spec",
    "artifact_id": "SPEC.authority-review",
    "source_item_ids": ["CONSTRAINT.beta"]
  }
}
```

Rules:

- `item_id` supports any canonical structured spec item.
- `status` uses `AgileForgeSpecStatus`.
- The item must exist.
- The claimed status must equal the item's actual status.
- `provenance.source_item_ids` must equal `[item_id]`.

### Accepted-Normative-Count Claim

```json
{
  "kind": "accepted_normative_count",
  "count": 2,
  "provenance": {
    "source": "structured_spec",
    "artifact_id": "SPEC.authority-review",
    "source_item_ids": ["CONSTRAINT.beta", "REQ.alpha"]
  }
}
```

Rules:

- The normative scope is fixed to `REQ`, `QUALITY`, `CONSTRAINT`,
  `INTERFACE`, and `DATA`.
- `count` must be non-negative.
- The host computes the complete accepted normative item set.
- `count` must equal the size of that set.
- `provenance.source_item_ids` must equal that complete set.

The fixed kind encodes the scope. No optional `scope` field is allowed.

### Accepted-Normative-Set Claim

```json
{
  "kind": "accepted_normative_set",
  "item_ids": ["REQ.alpha"],
  "provenance": {
    "source": "structured_spec",
    "artifact_id": "SPEC.authority-review",
    "source_item_ids": ["REQ.alpha"]
  }
}
```

Rules:

- `item_ids` must be unique normative item IDs in lexical order.
- The list is allowed to be empty.
- `item_ids` must exactly equal the complete accepted normative item set.
- `provenance.source_item_ids` must equal `item_ids`.

A singleton set is the typed representation of "only this item was accepted."
The contract does not need separate `only`, `sole`, or `except` variants.

## Free-Text Claim Boundary

Typed claims do not close the bug if the compiler can place the same statement
inside `{"kind": "free_text"}`. V3 therefore reserves a finite lexical boundary.

A free-text assumption is claim-like and invalid when it contains either:

- a canonical structured item ID and any `AgileForgeSpecStatus` value; or
- the word `accepted` and either `item` or `items`.

Matching is case-insensitive after Unicode NFKC normalization. The accepted
typed JSON contract remains ASCII.

This boundary intentionally favors false positives over ungrounded authority.
The executable predicate is:

```text
invalid_free_text =
    (contains_structured_item_id AND contains_status_value)
    OR (contains_word("accepted") AND contains_word("item" OR "items"))
```

Word checks use Unicode-aware word boundaries over the NFKC-normalized,
case-folded text. Token order, distance, and punctuation do not change the
result.

An ordinary assumption may contain a structured item ID without a status word,
or a status word without a structured item ID. It may not contain both
categories anywhere in the same entry.

Examples:

```text
valid:   REQ.alpha depends on an external identity provider.
valid:   Draft audit evidence is stored with each decision.
invalid: REQ.alpha is discussed below; draft assumptions remain open.
invalid: One item was accepted: REQ.alpha.
```

The four examples from #195 and the old #177 sentence are invalid free-text
assumptions in v3.

This predicate is the complete deterministic free-text detection contract. It
does not claim to recognize every semantic paraphrase. For example,
`REQ.alpha alone is approved` falls outside the documented cue set unless a
future schema version adds those exact tokens. Arbitrary English equivalence
remains a non-goal; reviewer-visible free text remains subject to human review.

## Validation and Data Flow

### Compiler Output Boundary

The compiler instructions and output schema require v3 objects. The model may
emit free-text assumptions or typed claims. String entries are invalid.

The normalizer performs these steps:

1. Decode the raw JSON object.
2. Parse it through the strict discriminated v3 output schema.
3. Map the free-text validator's stable
   `assumption_claim_requires_typed_form` Pydantic error type to the dedicated
   compiler failure before generic validation-error conversion.
4. Reject duplicate IDs, unknown fields, unknown kinds, and empty free text.
5. Sort `item_ids` and `source_item_ids` only after uniqueness validation.
6. If structured claims exist, parse the supplied source as
   `TechnicalSpecArtifact`. Free-text-only output does not require a structured
   source.
7. Ground every structured claim before any no-invariants early return.
8. Apply normalizer filtering and deduplication.
9. Add host-generated assumptions only as `FreeTextAssumption` objects.
10. Revalidate `success.model_dump(mode="json")` through
    `SpecAuthorityCompilationSuccess` immediately before every success return.

The normalizer sorts unique IDs. It must not change a claim kind, item ID,
status, count, set membership, artifact ID, or provenance membership to make a
false claim pass.

The current host-generated meta-policy, duplicate-invariant, and non-normative
source assumption constants become typed `FreeTextAssumption` values. Direct
string append operations are removed.

### Grounding Result

Grounding is a reusable typed service, not review-specific regex logic. Callers
must parse and validate the source as `TechnicalSpecArtifact` before invoking
it. The service accepts:

- one v3 assumption entry; and
- one canonical `TechnicalSpecArtifact`.

It returns either:

- a grounded canonical assumption; or
- a `GroundingFailure` containing claim kind, claimed value, actual value,
  artifact ID, and relevant item IDs.

The normalizer and authority review use the same grounding service.

### Focused Compilation and Merge

Aggregate count and set claims describe the complete structured spec. Focused,
repair, extension-only, and accepted-base inputs do not all describe the same
complete source.

Rules:

- merge inputs carry an explicit `CompilationScope` instead of an
  undifferentiated `list[SpecAuthorityCompilationSuccess]`;
- `full_spec`, `focused_item`, `repair_item`, `accepted_base`, and
  `extension_only` are distinct scopes;
- focused, repair, and extension-only outputs that contain aggregate claims fail
  with `ASSUMPTION_CLAIM_SCOPE_INVALID`;
- free-text and item-status entries are merged;
- aggregate claims from an accepted base are invalidated when merging an amended
  scope-extension spec and recorded in the authority quality report;
- the host does not synthesize replacement claims; and
- every retained structured claim is grounded again against the final full spec.

Accepted-base invalidation is an explicit source-boundary transformation, not a
silent drop. The quality record identifies the removed assumption index, kind,
and reason `aggregate_claim_invalidated_by_scope_extension`.

These rules apply to `_merge_compilation_successes()` and every caller,
including focused compilation, focused repair, and scope-extension
base-plus-extension merging. They prevent a valid local count from becoming a
false count in the merged authority.

### Stored Artifact Loading

Stored artifacts remain untrusted input:

1. Decode JSON.
2. Require an object.
3. Read `schema_version`.
4. Return unsupported unless it is v3.
5. Strictly validate the v3 union.
6. Never run fresh-output repairs from the stored loader.

### Authority Review

Authority review renders all assumption variants. It re-runs grounding for
structured claims against the reviewed canonical spec.

A mismatch produces a non-overrideable blocking finding:

```text
COMPILER_ASSUMPTION_CLAIM_MISMATCH
```

Finding details include:

- assumption index;
- claim kind;
- claimed value;
- actual value;
- artifact ID;
- claimed source item IDs; and
- actual source item IDs.

If the structured source cannot be loaded, a structured claim blocks with:

```text
COMPILER_ASSUMPTION_CLAIM_SOURCE_UNAVAILABLE
```

Finding identity hashes the claim kind, claimed value, source item IDs, and
finding code. It does not depend only on display text.

Claim findings use a typed claim-specific finding builder whose serialized
output includes `details`. This does not require changing the shared
`AuthorityReviewFinding` dataclass used by compact-IR coverage findings.

The old `ONLY_ACCEPTED_ASSUMPTION_RE`, `_assumption_text()`, and legacy
exclusivity finding construction are removed.

## Failure Semantics

Before persistence:

| Condition | Result | Schema retry |
| --- | --- | --- |
| Invalid JSON or invalid v3 object shape | `JSON_VALIDATION_FAILED` | existing bounded retry |
| Claim-like content in `free_text` | `ASSUMPTION_CLAIM_REQUIRES_TYPED_FORM` | one bounded contract retry |
| Structured source unavailable | `ASSUMPTION_CLAIM_SOURCE_UNAVAILABLE` | no |
| Item missing or provenance mismatch | `ASSUMPTION_CLAIM_SOURCE_MISMATCH` | no |
| False status, count, or set | `ASSUMPTION_CLAIM_MISMATCH` | no |
| Aggregate claim emitted from partial scope | `ASSUMPTION_CLAIM_SCOPE_INVALID` | no |

Semantic claim failures are not repaired by changing the claim to match the
source. The compiler must return a truthful artifact or fail.

`ASSUMPTION_CLAIM_REQUIRES_TYPED_FORM` is added to the bounded schema/contract
retry set. The retry feedback names the offending assumption index and requires
one typed variant. All other semantic claim failures remain non-retryable.

## Quality, Rendering, and Curation

### Quality Processing

- Free-text assumptions deduplicate by normalized `text`.
- Structured claims deduplicate through one shared
  `canonical_assumption_key()` function.
- Different claim kinds or values never merge.
- Noisy/Jaccard grouping operates only on `FreeTextAssumption.text`.
  Structured claims are excluded from token-based similarity grouping.
- `canonical_assumption_key()` serializes every variant as sorted,
  separator-stable canonical JSON. Quality deduplication, compilation merge,
  compact-IR target hashing, review finding identity, rendering lookup, and
  curation targeting all use that function. No consumer defines a second
  assumption identity rule.
- Quality report identifiers remain positional for v3. Stable assumption IDs
  are outside this issue.

Positional IDs are assigned after final deduplication. Regeneration changes the
authority fingerprint, so v2 curation handles or quality references cannot be
replayed against v3.

### Rendering

Review JSON preserves typed objects. Human-readable output renders:

- free text as text;
- item status as `<item_id> status is <status>`;
- count as `<count> accepted normative items`; and
- set as `accepted normative items: <item_ids>`.

Rendered prose is presentation only. It is never reparsed for validation.

### Curation

- Free-text entries remain editable through their `text` field.
- Structured claim values and provenance are read-only in curation.
- Correcting a structured claim requires recompilation or regeneration.
- Curation diff and target lookup must understand the v3 object variants.
- An attempt to edit a structured claim fails before persistence with
  `AUTHORITY_CURATION_TARGET_READ_ONLY`.

Manual editing must not create authority that bypasses compiler grounding.

## Breaking Change and Regeneration

No v2 compatibility code will be added.

When a reader encounters v2:

- it returns `COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED`;
- later phases remain blocked;
- remediation names `agileforge authority regenerate`;
- regeneration writes a new v3 authority row;
- the new row is pending review; and
- existing acceptance does not transfer automatically.

V2 rows remain historical database records. They are not rewritten or deleted.
No SQL migration is required because each compiled artifact is stored as JSON
in a new authority row.

This requires an append-only persistence correction:

- every `force_recompile=True` operation inserts a new
  `CompiledSpecAuthority` row, including when the previous candidate has no
  terminal decision;
- no successful compile path updates an existing authority row in place;
- a row referenced by a terminal `SpecAuthorityAcceptance` is immutable;
- regeneration publishes the authority ID returned by compilation;
- the current update-then-clone regeneration sequence is removed; and
- mutation-ledger replay returns the already-created authority instead of
  inserting another row.

This follows the existing compiled-authority migration policy: regenerate
authoritative derived data from the accepted spec instead of guessing how an
old free-text claim should be typed.

## Rollout and Rollback

The v3 schema, compiler instructions, normalizer, stored readers, renderers, and
tests ship in one change. There is no interval where new v3 artifacts are
written by code whose readers still require v2.

Rollout does not mutate existing rows. Projects with v2 authority become
explicitly unsupported until an operator regenerates and reviews v3 authority.

Before switching the global schema constant, release verification inventories
the latest selected authority for every project and records which active
projects depend on v2. After deployment, each active project is regenerated and
reviewed explicitly. There is no automatic batch mutation or acceptance.

A raw source rollback to the old v2 implementation is unsafe because its
regeneration path updates authority rows in place. The v3 rollout is
forward-fix by default. If a v2 runtime must be restored, use a rollback build
that retains the append-only persistence correction, regenerate a new v2
candidate, and review it. No rollback process rewrites or deletes v2 or v3
history.

## Compatibility With Earlier Trust-Boundary Policy

The May 2026 structured-authority design said compiler assumptions should not
block as host-inferred semantic judgments. V3 narrows that rule:

- free-text assumptions remain reviewer-visible and non-blocking on semantic
  quality alone;
- typed claims are explicit assertions about canonical structured source data;
  and
- a typed claim that contradicts its source is a structural grounding failure,
  not host-inferred coverage judgment.

No host code decides whether an assumption is useful or persuasive. It checks
only the declared typed value against canonical source fields.

## Test Strategy

### Schema Contract

- v3 accepts each union variant.
- string assumptions fail.
- union discrimination uses `kind`, independent of union declaration order.
- unknown kinds and extra fields fail.
- invalid item IDs, statuses, counts, duplicates, and empty text fail.
- v2 succeeds only as an unsupported-version fixture, never through v3 parsing.
- every normalizer success return performs final full-model revalidation.
- all three host-generated assumption paths append typed free-text objects.

### Free-Text Boundary

- all four #195 examples fail as free text;
- the #177 exact sentence fails as free text;
- an item ID without a status/count assertion remains ordinary text;
- a status word without an item ID remains ordinary text; and
- an item ID and status word anywhere in the same entry fail, regardless of
  order, distance, or punctuation;
- case and Unicode presentation changes do not bypass the cue boundary.
- claim-like free text returns
  `ASSUMPTION_CLAIM_REQUIRES_TYPED_FORM` and retries exactly once.

### Grounding

For item status, accepted count, and accepted set:

- true claims normalize successfully;
- false claims return the exact semantic failure;
- missing items fail;
- wrong artifact IDs fail;
- incomplete or invented provenance fails; and
- unavailable structured source fails closed.

### Merge and Quality

- focused outputs cannot retain aggregate claims;
- repair and extension-only outputs reject aggregate claims explicitly;
- scope extension invalidates accepted-base aggregate claims with a quality
  record;
- merged item-status claims are re-grounded against the full spec;
- free-text duplicates merge;
- identical structured claims merge; and
- different claim values do not merge.
- noisy grouping examines only free-text entries.
- compact-IR target hashing accepts every typed variant deterministically.

### Stored Readers and Review

- stored v3 round-trips through the loader;
- stored v2 returns unsupported with regeneration remediation;
- regeneration inserts a new authority row and leaves accepted v2 content
  unchanged;
- mutation-ledger replay returns the same regenerated authority row;
- true claims remain `accept_ready` without unrelated blockers;
- false or tampered claims block and are non-overrideable;
- rendered JSON remains typed;
- rendered text is readable; and
- curation rejects structured claim edits with
  `AUTHORITY_CURATION_TARGET_READ_ONLY`.

### Regression Gates

Run the focused suites for:

- compiler agent and normalizer;
- compiler service and stored loader;
- authority quality;
- authority review and decision;
- authority curation and diff;
- regeneration and unsupported-schema routing; and
- compact-IR validation paths that enumerate assumptions.

Then run `uv run --frozen pyrepo-check --all`.

## Implementation Boundaries

Expected implementation areas:

- `utils/spec_schemas.py`
- `orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions.txt`
- `orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions_source.py`
- `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`
- `services/specs/compiler_service.py`
- `services/specs/authority_quality.py`
- `services/agent_workbench/authority_review.py`
- `services/agent_workbench/authority_regenerate.py`
- authority curation and curation-diff services
- affected tests and operator-facing schema-version assertions

Implementation must not add:

- a v2-to-v3 conversion function;
- a union accepting both `str` and typed entries;
- a fallback regex for legacy assumption sentences; or
- a stored-loader repair path.

## Acceptance Criteria

- Compiled authority uses schema v3 and compiler version 3.0.0.
- Every assumption is a strict typed object.
- True item-status, accepted-count, and accepted-set claims ground
  deterministically.
- False structured claims cannot be persisted.
- Tampered stored claims block authority review.
- Free text matching the finite reserved predicate fails explicitly.
- Ordinary free text remains supported.
- V2 artifacts fail closed with regeneration guidance.
- Accepted authority rows are immutable; regeneration is append-only.
- Regeneration produces a pending v3 authority without auto-acceptance.
- Partial-scope aggregate claims cannot leak into merged full authority.
- Host-generated assumptions and compact-IR hashes use typed canonical objects.
- The legacy exclusivity regex and compatibility helpers are deleted.
- No general NLP claim parser is introduced.
