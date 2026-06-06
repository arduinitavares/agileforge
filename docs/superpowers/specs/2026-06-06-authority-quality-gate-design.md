# Authority Quality Gate Design

Date: 2026-06-06
Status: Approved for implementation planning
Scope: Project-agnostic compiled-authority quality gate for every
`agileforge.spec.v1` project

## Problem

Compiled authority can be structurally valid and source-grounded while still
being painful to review. The current pipeline catches schema, provenance, and
source-map failures, but it does not make the authority artifact easier for a
human to review when the compiler emits duplicate, near-duplicate, or over-split
items.

ASA exposed one instance of the current user-facing problem: authority reached
pending review with 84 invariants and 22 assumptions. There were no exact
duplicate invariant texts in that review packet, but several source items
produced dense clusters such as `REQ.project-scaffold` with 10 invariants and
multiple other source items with 5 each. That is likely reviewable only with
grouping and explicit quality metadata.

ASA must not shape the product rules. It is only a regression fixture used to
prove the project-agnostic gate handles a real noisy authority artifact.

## Goals

- Run a quality gate after authority compilation/normalization and before
  human authority review.
- Detect exact duplicates, near-duplicates, over-split invariants, and noisy
  compiler assumptions for any `agileforge.spec.v1` project.
- Auto-merge only semantically equivalent authority items.
- Preserve source evidence, source levels, invariant type, deterministic IDs,
  and review traceability.
- Group related-but-distinct items for human review instead of silently merging.
- Keep project create and authority regenerate stopping at pending authority
  review.
- Make review packets and worksheets cleaner by exposing duplicate groups and
  merge decisions.

## Non-Goals

- No ASA-specific rules, thresholds, item IDs, source terms, fixture names, or
  domain-specific assumptions.
- No LLM call inside the quality gate.
- No automatic deletion of related-but-distinct invariants.
- No automatic acceptance or rejection of authority.
- No Vision, Backlog, Roadmap, Story, or Sprint progression.
- No replacement for source-map validation or semantic provenance checks.

## Placement

The gate belongs in `services/specs/compiler_service.py`, immediately after
`SpecAuthorityCompilationSuccess` is returned by compiler normalization and
immediately before `_persist_compiled_authority()` serializes and writes the
authority row.

This placement covers both:

- `agileforge project create`
- `agileforge authority regenerate`

The normalizer remains responsible for schema repair, deterministic IDs,
source-map grounding, and semantic source checks. The quality gate is a separate
post-normalization quality pass.

## Artifact Contract

Add an optional `authority_quality` object to the persisted v2 compiled authority
artifact. Existing v2 artifacts without this field remain valid. New artifacts
include it.

```json
{
  "schema_version": "agileforge.compiled_authority.v2",
  "invariants": [],
  "assumptions": [],
  "source_map": [],
  "authority_quality": {
    "schema_version": "agileforge.authority_quality.v1",
    "summary": {
      "original_invariant_count": 0,
      "final_invariant_count": 0,
      "merged_invariant_count": 0,
      "merged_assumption_count": 0,
      "review_group_count": 0,
      "near_duplicate_group_count": 0,
      "over_split_group_count": 0,
      "noisy_assumption_group_count": 0
    },
    "merged_items": [],
    "review_groups": []
  }
}
```

`authority_quality` is review metadata. It does not replace `invariants`,
`assumptions`, or `source_map`.

## Merge Rules

### Invariants

Auto-merge invariants only when all identity dimensions match:

- invariant type
- canonical JSON parameters
- top-level `source_item_id`
- top-level `source_level`

This is exact semantic equivalence. It is safe because deterministic invariant
IDs already derive from invariant type and parameters; the quality gate adds the
source identity checks that prevent merging the same rule shape from different
source authority.

When merging:

- keep the first canonical invariant in stable artifact order
- preserve the kept invariant ID
- remove duplicate invariant rows
- remap duplicate `source_map` entries to the kept invariant ID
- preserve every source-map excerpt/location unless it is an exact duplicate
- record the merge in `authority_quality.merged_items`

Do not merge if any of these differ:

- source item
- source level
- invariant type
- parameter payload

Those cases become review groups at most.

### Assumptions

Merge only exact normalized duplicate assumptions. Normalization is
case-folding, whitespace compaction, and punctuation-insensitive comparison.

Near-duplicate or noisy assumptions are grouped for review, not deleted.

## Review Group Rules

Review groups are non-blocking warnings by default. They exist to guide the
human reviewer, not to override the review decision.

Group types:

- `near_duplicate_invariants`: high text/parameter overlap but not exact merge
  candidates.
- `over_split_invariants`: many invariants from the same source item and same
  broad subject/type cluster.
- `related_source_variants`: same rule shape or subject across different source
  items or source levels.
- `noisy_assumptions`: repeated boilerplate, overly broad assumptions, or
  assumptions with high overlap.

Related-but-distinct items must never be silently merged. Each group records why
the gate did not merge it.

## Similarity Heuristics

The initial implementation should be deterministic and bounded:

- normalize text by case-folding, tokenizing alphanumerics, and dropping a small
  built-in stopword set
- compare invariants using rendered invariant text, type, source item, source
  level, and canonical parameters
- compute token Jaccard similarity for near-duplicate candidates
- only compare items inside small candidate buckets:
  - same invariant type
  - same source item
  - same subject-like parameter if available
  - same source level when present
- cap review groups and group size in the report to avoid huge packets

No heuristic similarity result is allowed to merge authority. Heuristics only
create review groups.

## Source Evidence Preservation

Source evidence remains owned by `source_map`.

For merged invariants:

- every duplicate item's `source_map` entries are carried forward under the kept
  invariant ID
- entries are deduplicated only by exact `(invariant_id, excerpt, location)`
  after remapping
- merge records include removed IDs and source evidence counts

For review groups:

- member IDs remain unchanged
- source refs and excerpts remain attached to their original invariants
- group metadata points to member IDs only

`source_item_id` alone is not evidence. It only helps decide whether merge is
safe and whether review grouping is relevant.

## Review Packet And Worksheet

`services/agent_workbench/authority_review.py` should surface
`authority_quality` in the rendered review packet.

Review summary should include compact counts:

- merged invariant count
- merged assumption count
- near-duplicate group count
- over-split group count
- noisy assumption group count

The dashboard and any worksheet generator can use the same packet data. They
should not recompute duplicate logic.

Acceptance remains human-gated:

- exact source/schema failures can still block acceptance through existing
  review findings
- quality groups are warnings by default
- accept remains possible when only quality warnings exist
- reject/refine remains available with concrete group IDs and member IDs

## ASA Regression Fixture

ASA may be used to learn and verify behavior, but not to define special rules,
thresholds, grouping keys, or source-item exceptions. Any rule derived from ASA
must also be defensible for a generic `agileforge.spec.v1` artifact.

Expected ASA checks after implementation:

- create/regenerate reaches pending authority review
- no authority accept/reject is run
- no Vision/Backlog/Roadmap/Story/Sprint command is run
- `authority_quality.summary` exists
- over-split groups include dense source-item clusters by generic criteria; in
  the current ASA artifact this may include `REQ.project-scaffold`, but that ID
  must never appear in gate code
- exact duplicate counts are reported even when zero
- worksheet/review packet is cleaner because group metadata identifies review
  focus areas

## Tests

Focused unit tests should cover:

1. Exact duplicate invariants with matching type, parameters, source item, and
   source level are merged.
2. Merged invariant source-map entries are remapped and preserved.
3. Same parameters from different source items are not merged and are grouped.
4. Same parameters with different source levels are not merged and are grouped.
5. Near-duplicate invariants are grouped, not merged.
6. Over-split invariants from the same source item are grouped.
7. Exact duplicate assumptions are merged.
8. Near-duplicate/noisy assumptions are grouped, not deleted.
9. Existing v2 artifacts without `authority_quality` still load.
10. Review packet exposes `authority_quality` summary and groups.

Integration tests should cover:

- project create persists an authority artifact with `authority_quality`
- authority regenerate persists an authority artifact with `authority_quality`
- pending authority review still reports `pending_acceptance`
- ASA fixture/caller regression shows quality groups without ASA-specific code
- negative source search confirms the quality-gate implementation does not
  contain ASA project names, ASA item IDs, or ASA domain terms

## Open Implementation Decisions

These are implementation-plan decisions, not design blockers:

- exact token similarity threshold for near-duplicate grouping
- maximum number of review groups and members shown in packets
- whether dashboard renders groups in a new tab or in the existing overview tab
- whether a standalone worksheet export command is needed in the first pass

## Risks

- Too-aggressive heuristics could make warnings noisy. Mitigation: candidate
  bucketing, group caps, and non-blocking status.
- Auto-merge could erase traceability if source-map remapping is wrong.
  Mitigation: merge only exact semantic duplicates and test source-map
  preservation directly.
- Adding `authority_quality` to persisted artifacts touches strict schema
  loading. Mitigation: make it optional in v2 and keep a nested
  `authority_quality` schema version.

## Acceptance Criteria

- Quality gate runs for all `agileforge.spec.v1` authority compilation paths.
- Only semantically equivalent invariants are auto-merged.
- Related-but-distinct items are grouped for review and not merged.
- Source evidence, source levels, invariant type, and traceability are preserved.
- Review packet/worksheet exposes merge decisions and duplicate groups.
- Project create and authority regenerate still stop at pending authority review.
- ASA is covered as a regression fixture with no ASA-specific logic.
- Source search proves the implementation does not contain ASA-specific names,
  item IDs, thresholds, or domain terms.
