# Accepted Specification Delivery Contract Design

Date: 2026-08-21
Status: Approved for implementation on 2026-08-21
Issue: #210
Baseline: `916e9ff55bdcd39b2a28c197c13c91b35d141b15`

## Objective

Remove the redundant Authority compilation and review phase. The exact
human-accepted `agileforge.spec.v2` Specification becomes the sole canonical
product-definition contract for Backlog, Roadmap, Story, Sprint, and execution.

This document resolves the remaining implementation choices. The human approved
this corrected design on 2026-08-21.

## Proven problem

AgileForge already stores a complete, typed, fingerprint-bound Specification
after human review. It then requires a provider to reinterpret that same
meaning into a compiled Authority before delivery can begin. The Authority
algebra cannot faithfully represent common product behavior, its lexical gate
rejects valid domain language, and nearly all downstream consumers use the
result only as duplicated text or lineage metadata.

Attempt 30 proves that the extra transformation can block a complete String
Calculator Specification. The full provider-free reproduction and artifact
hashes remain in the issue #210 decision brief. The failure is not unique to
String Calculator vocabulary; it is a mismatch between open-ended product
behavior and a closed provider-selected invariant taxonomy.

No installed-data or migration requirement exists. Compatibility with stored
Authority artifacts is out of scope.

## Domain model

One semantic artifact governs delivery:

```text
Specification Source
  -> Specification Candidate
  -> human Specification decision
  -> Accepted Specification Version
  -> Backlog
  -> Roadmap
  -> Stories
  -> Sprint Plan
  -> Tasks and execution evidence
```

An **Accepted Specification Version** is the immutable canonical payload and
lineage targeted by one exact human `SpecificationDecision(decision="accepted")`.
Its content never changes. Its currentness may change from `approved` to
`superseded` when a later amendment is accepted.

The **Current Accepted Specification** is the sole `approved` Accepted
Specification Version for a Project. New planning always starts from it.
Superseded versions remain accepted historical contracts for already active or
completed work; they never become current by being selected by a caller.

**Delivery Lineage** is the exact Product Goal plus Accepted Specification
identity inherited by Backlog and every descendant. A descendant is current
only when that root lineage is current and every immediate parent is the
current accepted parent.

**Specification Evidence** is a stable Specification item ID attached to a
Backlog item, Story, Task, semantic finding, or packet. The host proves that the
ID exists in the exact pinned Specification and that child references stay
within their accepted parent boundary. Humans judge semantic relevance.

There is no `Compiled Authority`, `Accepted Authority`, Authority gap, or
separate delivery-activation artifact.

## Human control

The existing Specification review remains mandatory. Its Accept action means:

> Accept these exact canonical Specification bytes as the product-definition
> contract for delivery.

Acceptance exposes `backlog.generate`. It does not invoke a provider, generate
a Backlog, accept any later artifact, or begin execution. Backlog, Roadmap,
Story, and Sprint retain their own existing mandatory human decisions.

Optional semantic Story review remains an explicit provider action. Structural
Story validation remains provider-free. No planning or acceptance command
silently calls a provider.

## Accepted-Specification aggregate

### Relational acceptance proof

`SpecRegistry` becomes the durable version record, but it cannot establish
human acceptance by `status="approved"` alone.

The fresh schema adds `SpecRegistry.source_specification_decision_id` and this
exact composite relationship:

```text
SpecRegistry child columns
  (project_id,
   source_specification_decision_id,
   source_specification_candidate_id,
   source_specification_candidate_fingerprint)

SpecificationDecision parent columns
  (project_id,
   specification_decision_id,
   specification_candidate_id,
   candidate_fingerprint)
```

`SpecificationDecision` gains uniqueness on the parent tuple. The foreign key
proves the exact decision/candidate relationship; the sole deep loader also
requires `decision="accepted"`. No duplicated decision discriminator is added
to `SpecRegistry`. Registry approval metadata is removed: reviewer, rationale,
and decision time are read from the bound decision row.

The acceptance transaction performs these operations atomically:

1. Revalidate the exact candidate, candidate fingerprint, source manifest,
   accepted Vision, and active Product Goal.
2. Insert and flush one immutable accepted `SpecificationDecision`.
3. Require an amendment candidate's base version/hash to equal the current
   registry row. Change that row from `approved` to `superseded` and flush.
4. Insert the new `SpecRegistry` row bound to that decision and exact candidate.
5. Commit both records together.

A partial unique index permits at most one `status="approved"` registry row per
Project. Rejected or feedback decisions never create a registry row. Retrying
the exact request replays the existing decision and registry identity through
the existing idempotency contract. If two amendments race from the same base,
only the first may commit. The loser rereads current lineage and returns the
stable `STALE_SPECIFICATION` error; it does not leave a decision without its
paired registry row.

### Deep loading interface

One module, `services/specs/accepted_specification.py`, owns all joins and
integrity checks:

```python
@dataclass(frozen=True)
class AcceptedSpecification:
    project_id: int
    spec_version_id: int
    spec_hash: str
    status: Literal["approved", "superseded"]
    specification_decision_id: int
    canonical_specification_json: str
    payload: SpecificationPayload


def load_accepted_specification(
    session: Session,
    *,
    project_id: int,
    spec_version_id: int,
    spec_hash: str,
) -> AcceptedSpecification:
    """Load one exact accepted historical or current contract, or raise."""


def load_current_accepted_specification(
    session: Session,
    *,
    project_id: int,
) -> AcceptedSpecification | None:
    """Return the sole current accepted contract; None means not yet accepted."""
```

The exact loader accepts `approved` and `superseded` rows so historical packets
and active Sprints remain reproducible. It raises a typed integrity error for a
missing or mismatched ID/hash, wrong Project, missing or non-accepted decision,
candidate mismatch, invalid canonical bytes, payload-hash mismatch, or broken
Vision/Product Goal/source lineage.

Its closed error codes are `SPECIFICATION_NOT_FOUND`,
`SPECIFICATION_NOT_ACCEPTED`, `SPECIFICATION_IDENTITY_MISMATCH`,
`SPECIFICATION_CANONICAL_BYTES_INVALID`, and
`SPECIFICATION_LINEAGE_INVALID`. Current selection additionally uses
`CURRENT_SPECIFICATION_AMBIGUOUS`. Workflow boundaries map a valid historical
version used for new planning to `STALE_SPECIFICATION`; they do not relabel
corruption as staleness.

The current loader returns `None` only when the Project has no approved
Specification. It raises on ambiguity or corruption. Callers never order by ID
and choose a presumed latest row.

The loader reuses `load_candidate_contract()` and canonical Specification
serialization. It validates stored acceptance-time lineage; it does not compare
a historical Specification to the repository's current revision. Repository
work is expected to change after acceptance.

## Persistence and delivery lineage

### Root lineage

`BacklogArtifact` replaces `authority_id` and `authority_fingerprint` with:

- `spec_version_id`;
- `spec_hash`;
- a composite foreign key through `project_id`, `spec_version_id`, and
  `spec_hash` to `SpecRegistry`;
- the existing exact Product Goal ID and fingerprint.

The workflow fact reference is `specification`, containing exact version and
hash. `backlog.generate` and `RecordBacklogDraft` require the current accepted
Specification and current Product Goal. Wrong, foreign, superseded, ambiguous,
or mismatched references fail before a provider call or persistence.

Roadmap and Story artifacts inherit the root through their exact immutable
parents. They do not duplicate the Specification columns. Their loaders walk
the parent chain through one lineage service and fail closed on a broken or
mixed chain.

### Artifact lineage keys

Each replacement chain has one closed key. History lookup, version allocation,
content uniqueness, idempotent replay, current-parent selection, and
supersession validation use that same key:

```text
Backlog
  (project_id,
   product_goal_artifact_id, product_goal_fingerprint,
   spec_version_id, spec_hash)

Roadmap
  (project_id, backlog_artifact_id, backlog_artifact_fingerprint)

Story
  (project_id, source_backlog_artifact_id, backlog_item_id)

Sprint plan
  (project_id, spec_version_id, spec_hash, sprint_plan_stream_id)
```

For every chain, version uniqueness is `key + version_number` and content
uniqueness is `key + content_fingerprint`. A supersession parent must be the
immediately prior artifact in the same key. The first artifact in a new key has
version 1 and no parent. A new accepted Specification therefore starts a new
Backlog and Sprint-plan chain; an accepted replacement Backlog starts a new
Roadmap and Story chain. Cross-key supersession is rejected.

Within one linear root lineage, accepted artifact A is superseded when a later
accepted artifact B has A anywhere in its transitive `supersedes_*` ancestry.
Pending, feedback, and rejected artifacts do not independently displace A, but
they may be intermediate ancestors of B. The current accepted parent is the
sole accepted leaf under that rule. Zero or multiple accepted leaves is an
integrity error when an accepted parent is required, never a "latest ID"
tie-break. The host rejects cycles, cross-lineage ancestry, and branching
accepted leaves. This rule applies to Backlog, Roadmap, Story, and Sprint-plan
chains.

### Operational descendants

Backlog acceptance no longer creates placeholder `UserStory` rows. A Backlog is
ranked requirements, not a Story set. Story generation reads its exact Backlog
item through the accepted Roadmap reference.

Recording a Story draft persists only one immutable `StoryArtifact`. Its closed
canonical content contains host-minted Story item IDs and item fingerprints; it
does not create or mutate an operational `UserStory` row. Human review renders
that canonical content directly.

`StoryArtifact` replaces prose-keyed history with these exact lineage fields:

- `source_backlog_artifact_id` and `source_backlog_artifact_fingerprint`;
- `backlog_item_id`;
- `roadmap_artifact_id` and `roadmap_artifact_fingerprint`;
- `version_number`, `canonical_content_json`, and `content_fingerprint`;
- canonical `story_item_ids_json`;
- `supersedes_story_artifact_id`.

Story history, version uniqueness, idempotency, and current-parent lookup all use
`(project_id, source_backlog_artifact_id, backlog_item_id)`. Content uniqueness
is `(project_id, source_backlog_artifact_id, backlog_item_id,
content_fingerprint)`; version uniqueness replaces the final field with
`version_number`.
`supersedes_story_artifact_id` must target the immediately prior artifact in the
same tuple. The Roadmap parent may change inside that tuple when a replacement
Roadmap still points to the same exact Backlog item. Composite foreign keys bind
`(project_id, source_backlog_artifact_id,
source_backlog_artifact_fingerprint)` to the exact Backlog and `(project_id,
roadmap_artifact_id, roadmap_artifact_fingerprint)` to the exact Roadmap. The
host proves that the Roadmap's Backlog parent is the same Backlog and contains
that item.

The Story provider emits one to eight items without IDs. After validating the
provider result, the host preserves provider order, assigns `US-0001` through
`US-0008`, constructs each closed item, and calculates its fingerprint with
`workflow.fingerprints.canonical_hash()`. It then constructs the closed Story
content, serializes it with `workflow.fingerprints.canonical_json()`, calculates
the artifact fingerprint, and persists that exact content.
`story_item_ids_json` lists those IDs; it never stores operational database IDs.

The closed canonical Story item contains exactly:

```text
story_item_id
story_title
statement
persona                         # host-derived, never provider-authored
acceptance_criteria             # ordered exact strings
spec_item_ids                   # canonical sorted set
invest_assessment               # explainable 6-dimension assessment (independent, negotiable, valuable, estimable, small, testable)
estimated_effort
produced_artifacts
research_caveats
dependency_candidates
```

> [!NOTE]
> Per Issue #221 and the [Story Refinement to Sprint Selection Design Handoff](../../feedback/2026-08-24-story-refinement-to-sprint-selection-design-handoff.md), legacy `invest_score` (High/Medium/Low) and `decomposition_warning` are retired in favor of the explainable 6-dimension `invest_assessment` (`StoryInvestAssessment`).

The item fingerprint hashes that complete object; it is stored beside the item
in the Story artifact envelope and is not included recursively in its own hash.
The host derives `persona` with one shared parser used by provider-output
validation, activation, structural validation, and tests: trim the statement,
remove `*` Markdown emphasis, then case-insensitively match
`^as (a|an|the) <persona>,? i want `. The captured persona is Unicode-trimmed,
must contain 1 through 100 characters, and preserves its original non-emphasis
characters. No other persona parser exists.

Feedback and rejection persist only the decision and leave accepted operational
Stories unchanged. Accepting a Story artifact performs one transaction that:

1. inserts the accepted decision for the exact artifact fingerprint;
2. materializes one new immutable-core `UserStory` row per canonical Story item;
3. binds each row to its exact Story artifact/item and Backlog item lineage;
4. marks only prior accepted rows in the same Story chain superseded; and
5. commits the decision and rows together, with idempotent replay returning the
   same identities.

Each operational row stores exact `source_story_artifact_id` and fingerprint,
`source_story_item_id` and item fingerprint, `accepted_spec_version_id` and
`accepted_spec_hash`, canonical `spec_item_ids_json`, and the accepted title,
statement, derived persona, and acceptance criteria. Initial `story_points` is
the existing closed effort map `XS=1, S=2, M=3, L=5, XL=8`; initial `rank` is
the canonical parent Backlog priority times 100 plus the one-based Story item
ordinal, serialized as a decimal string.

> [!NOTE]
> Per Issue #222 and the [Story Refinement to Sprint Selection Design Handoff](../../feedback/2026-08-24-story-refinement-to-sprint-selection-design-handoff.md), pre-acceptance Story review surfaces (dashboard UI, CLI, and API projections) expose the complete planning proposal before acceptance: statement, persona, acceptance criteria, Specification evidence, estimated effort and derived story points, backlog order and rank, proposed dependencies, and the explainable 6-dimension INVEST assessment. Planning metadata values are visible and contestable recommendations before becoming operational state.

The remaining canonical Story-item
fields stay in the immutable artifact and are resolved from it for reviews and
packets rather than duplicated onto the operational row. The row is unique on
`(project_id, source_story_artifact_id, source_story_item_id)`, so acceptance
replay cannot create a duplicate. One composite relationship binds
`(project_id, accepted_spec_version_id, accepted_spec_hash)` to `SpecRegistry`;
a second binds `(project_id, source_story_artifact_id,
source_story_artifact_fingerprint)` to `StoryArtifact`. Backlog identity is
derived through that artifact, not duplicated on `UserStory`. The host proves
that the embedded item ID and fingerprint exist in the immutable Story artifact
and that the direct Specification identity equals the artifact's derived root.

`UserStory.acceptance_criteria_json` is required and contains the exact
canonical JSON array copied from the accepted Story item. The old display-text
`acceptance_criteria` column is removed. UI, prompts, packets, and exports parse
the canonical array and derive display text; they never reconstruct criteria by
splitting bullets or lines. Acceptance requires byte equality with the source
item after canonical reserialization. The provider must return one or more
criteria; the host rejects an item that is empty or whitespace-only but
otherwise preserves embedded newlines, bullet prefixes, and Unicode exactly.

Only rows whose exact Story artifact has an accepted decision are selectable.
Current planning additionally requires the current Delivery Lineage and
`is_superseded=False`. Accepted title, statement, acceptance criteria, evidence
IDs, and lineage are immutable; later execution changes are limited to explicit
status, resolution, completion, and evidence facts. `story_points` and `rank`
may change only through the existing explicit readiness-repair action before
Sprint membership. Validation evidence may be appended or replaced by a later
explicit validation attempt. No other writer may change accepted core fields.

The fresh `UserStory` contract makes the boundary explicit:

- immutable: Project, Story artifact/item identities and fingerprints,
  Specification identity, `spec_item_ids_json`, title, statement,
  `acceptance_criteria_json`, persona, and creation time;
- controlled pre-Sprint: `story_points`, `rank`, and validation evidence;
- controlled lifecycle: `is_superseded`, immutable replacement-artifact ancestry,
  status,
  resolution, completion notes/evidence/time, and update time.

Backlog-seed/refinement state, prose/slot identity, reset archives, and mutable
acceptance-criteria history do not exist in the fresh model.

The accepted-A -> feedback-B -> accepted-C seam must prove that B creates no
operational rows and never changes selectable A state. C changes selection only
inside C's acceptance transaction. Story validation is a post-acceptance
readiness check over C's immutable rows, not a prerequisite that requires draft
rows to exist.

Targeted Story correction remains supported, but prose and slot are no longer
identity. A caller may select an operational `story_id`; the host resolves it to
the exact accepted Story artifact/item/fingerprint. The provider returns one
replacement item. The host copies the other immutable items, replaces that one
item, and records a complete replacement `StoryArtifact` under the same chain.
The usual whole-artifact human decision then activates all replacement rows
atomically. `target_refinement_slot`, prose-keyed patch persistence, direct
`UserStory` mutation, and partial artifact acceptance are removed.

`SprintPlanArtifact` stores `spec_version_id` and `spec_hash` with the same
composite registry binding. Every selected Story must carry that exact identity.
`SprintStart` inherits it through the accepted immutable Sprint plan.

Task metadata stores the exact Sprint-plan Specification identity. Canonical
execution packets resolve that pinned version even when it is superseded.

### Operational Story coexistence

An amended lineage may coexist with an older active Sprint. New Story drafts
create only immutable artifacts. Accepting them creates new rows and never edits,
reuses, or marks prior-lineage rows. The `is_superseded` field remains scoped to
accepted Story replacement inside one exact Backlog-item lineage.

Operational rows do not carry a `superseded_by_story_id` pointer. A complete
replacement artifact may add, remove, split, or combine items, so a one-to-one
row pointer would invent continuity that the human never reviewed. Replacement
history is resolved through `StoryArtifact.supersedes_story_artifact_id`; prior
rows are marked `is_superseded=True` only when the replacement artifact is
accepted.

Current-planning queries filter `is_superseded=False` and the exact current
Specification version/hash. Active-Sprint execution resolves old Stories by
their `SprintStory` and `SprintStart` lineage. The Backlog replacement guard
examines only Stories in the Backlog artifact's exact Specification lineage, so
old pinned rows neither block nor disappear from new planning.

Dependency rows cannot cross Specification lineages. New-lineage planning may
proceed while an older Sprint is active, but the existing one-active-Sprint-per-
Project rule prevents the new Sprint from starting until the old Sprint closes.

## Specification-item evidence

The direct Specification remains plain, readable product language. Stable item
IDs provide references; they do not turn prose into a machine-enforcement
algebra.

### Stable Backlog item identity

Requirement prose is not identity. After validating provider output and unique
priorities, the host assigns artifact-scoped IDs in canonical priority order:

```text
PBI-000001, PBI-000002, ... PBI-999999
```

The durable identity is `(backlog_artifact_id, backlog_item_id)`. IDs never
claim continuity across a replacement Backlog artifact. Duplicate normalized
requirement text is rejected inside one Backlog because it would create
ambiguous Story history.

The provider emits at most 999,999 `BacklogAgentItem` values without IDs; output
beyond that bound is rejected. The exact normalization sequence is: validate
the provider result, require unique priorities and unique normalized requirement
text, sort by canonical priority, mint IDs, construct the closed host
`BacklogItem` sequence, serialize that sequence, calculate the artifact
fingerprint, then persist those exact bytes. Later agents consume only this
canonical form.

The hard-break requirement-text normalization rule is:
`str.casefold()`, split on Unicode whitespace, then join tokens with one ASCII
space. Punctuation is preserved. This value is used only for duplicate detection
and display/search compatibility; it is never a durable identity or foreign key.

Each Roadmap release stores ordered `backlog_item_id` references, not copied
Specification-ID arrays or requirement strings as identity. The host resolves
the immutable Backlog item for display and agent input. `StoryArtifact` stores
the exact `backlog_item_id` and remains bound to the Roadmap whose Backlog parent
contains it. `UserStory` reaches that parent identity through its exact
`StoryArtifact`; normalized requirement text remains Backlog display/search data
only.

Concretely, strict `RoadmapRelease.backlog_item_ids: tuple[str, ...]` replaces
`items: list[str]`. A complete persisted Roadmap must reference every exact
parent Backlog item once across all releases, with no unknown or duplicate ID.

### Evidence sets

- `BacklogItem.spec_item_ids` replaces `authority_ref` and
  `capability_hint`.
- `UserStoryItem.spec_item_ids` is a non-empty subset of the exact parent
  Backlog item's set.
- `UserStory.spec_item_ids_json` stores the accepted Story item's canonical set.
- `StructuredTaskSpec.relevant_spec_item_ids` is a non-empty subset of its
  parent Story's set.
- Semantic Story findings use the code-specific parent boundary defined below.
- Packets resolve the pinned sets; Roadmap does not own a duplicate evidence
  set.

Every Backlog, Story, and Task set must contain at least one item whose `type` is
`REQ`, `QUALITY`, `CONSTRAINT`, `INTERFACE`, or `DATA` and whose `level` is
`MUST`, `MUST_NOT`, `SHOULD`, or `MAY`. It may additionally cite other exact
accepted Specification items as context or boundaries. The host rejects an
empty set, duplicates, unknown IDs, cross-Project identity, IDs outside the
parent boundary, and identity/hash mismatches.

Set-like ID fields arrive in any order, reject duplicates, and are persisted
and hashed in lexicographic order. `referenced_spec_item_ids` is always a
derived lexicographically sorted union, never provider-authored.

The host does not parse wording to decide whether an ID is semantically
relevant. Before each decision, Backlog, Story, and Sprint review projections
render the cited item's exact title, statement, level, ordered acceptance
criteria, and verification method. Roadmap review resolves its Backlog-item
references to the same visible evidence without persisting another copy.

If a Story or Task needs a rule not cited by its parent, the parent is corrected
through its normal reviewed replacement path. The host does not silently widen
scope or invent a mapping.

## Agent interfaces

Every delivery agent receives the exact identity and canonical payload once:

```text
accepted_specification_version_id
accepted_specification_hash
accepted_specification_json
```

`technical_spec`, `compiled_authority`, `compiled_authority_cached`, and
`compiled_authority_json` are removed from the new contracts. A single explicit
field avoids presenting the same contract twice under different names.

Backlog, Roadmap, Story, Sprint, and semantic Story-review prompts state:

- the accepted Specification is the product-definition source of truth;
- each scope-creating Backlog item, Story, Task, and semantic finding must carry
  the evidence field defined by its schema;
- the model must not add unsupported behavior or report supported behavior as
  a generic gap;
- the host validates identity and references, while the human review validates
  semantic relevance.

This is object-level grounding, not a claim that every sentence of Roadmap
reasoning, Sprint goals, or selection rationale has a machine-proven citation.
Those narrative fields remain visible context within their exact reviewed
parent scope.

## Story validation

### Modes

The hard-break contract has two modes:

- `structural`: provider-free host checks only;
- `hybrid`: the same structural checks plus exactly one explicitly requested
  semantic provider review.

Validation remains an explicit non-workflow service action. No workflow node,
readiness projection, draft/acceptance transition, or default configuration may
invoke `hybrid` automatically.

The Authority-derived `FORBIDDEN_CAPABILITY` and `REQUIRED_FIELD` lexical checks
are deleted. Reconstructing them from prose would recreate the defect. No
provider-only mode may bypass host checks. Structural validation evaluates its
entire finite rule set and returns every applicable bounded failure in canonical
code order; it does not stop at the first failure.

The structural rule set is closed and ordered as follows. Every rule is
blocking; this contract defines no structural warning code. A rule whose parent
cannot be loaded after an earlier binding failure is inapplicable rather than a
second guessed cascade, but all other rules still run in the same pass.

| Order | Code | Deterministic predicate | Actionable correction |
| --- | --- | --- | --- |
| 1 | `STORY_ACCEPTANCE_INVALID` | The row does not resolve to one accepted `StoryArtifactDecision` for its exact Project/artifact/fingerprint. | Review and accept a complete valid Story artifact; never patch the row. |
| 2 | `STORY_ITEM_BINDING_INVALID` | The exact host Story item is missing, its fingerprint does not recompute, or any immutable operational field differs from that item. | Create and review a complete replacement Story artifact. |
| 3 | `SPECIFICATION_BINDING_INVALID` | Deep loading the pinned version/hash fails, or the Story/Backlog lineage does not resolve to that same exact Specification. | Regenerate from a valid current lineage; do not rebind the existing row. |
| 4 | `SPEC_ITEM_REFERENCES_INVALID` | `spec_item_ids_json` is not canonical, non-empty, known, equal to the source Story item, a subset of the parent Backlog item's set, or lacks a qualifying normative item. | Replace the Story with exact supported item references inside its parent evidence set. |
| 5 | `STORY_STATEMENT_INVALID` | After trimming, removing `*` Markdown emphasis, and case-folding, the immutable statement does not start with `as a `, `as an `, or `as the `, or lacks ` i want ` or ` so that `. | Replace the Story with the closed Story statement shape. |
| 6 | `ACCEPTANCE_CRITERIA_INVALID` | `acceptance_criteria_json` is not canonical JSON for the exact ordered source list, has no item, or contains a non-string or whitespace-only item. | Replace the Story with one or more observable non-empty criteria. |

`STORY_ITEM_BINDING_INVALID` compares exactly
`source_story_artifact_id`, `source_story_artifact_fingerprint`,
`source_story_item_id`, `source_story_item_fingerprint`, `title`,
`story_description`/statement, `persona`, `acceptance_criteria_json`, and
`spec_item_ids_json` with the exact canonical item and artifact. Specification
version/hash is Rule 3. Controlled `story_points` and `rank` are not Rule 2
failures, but their current values participate in the validation-input
fingerprint so an explicit readiness repair requires revalidation. The two
narrower content rules still report their more useful corrections when
applicable. The prior cue-based connectivity, zero-millisecond,
hard-coded scope-placeholder, and persona-warning rules are deleted with the
Authority lexical rules. They infer product meaning from English or duplicate
the closed Story contract.

Rule applicability is executable, not inferred:

- Rule 1 always runs from the operational row and exact decision lookup.
- Rule 2 always runs. Missing/unparseable Story artifact content or a missing
  item is itself `STORY_ITEM_BINDING_INVALID`; field comparisons run only when
  that item is available.
- Rule 3 runs only when Rule 2 loaded a parseable Story artifact with its exact
  Backlog parent fields. Missing/corrupt Specification or Backlog lineage then
  produces `SPECIFICATION_BINDING_INVALID`.
- Rule 4's row-local JSON, ordering, uniqueness, and non-empty checks always
  run. Equality-to-source runs when Rule 2 loaded the item. Known-ID,
  qualifying-normative, and parent-subset checks run only when Rule 3 loaded the
  exact Specification and Backlog parent. Any triggered subcheck produces the
  one Rule 4 finding.
- Rule 5 always runs from the operational statement.
- Rule 6's row-local JSON/type/non-empty/canonical checks always run.
  Equality-to-source runs only when Rule 2 loaded the item.

Therefore a missing Story artifact yields Rules 1 and 2, skips Rule 3 and Rule
4's parent-dependent subchecks, and still evaluates Rules 4/5/6 locally. A
missing Story item may still permit Rule 3 and the parent-dependent Rule 4
checks from its valid artifact, but skips only source-item equality. A failed
deep Specification or Backlog load yields Rule 3 and leaves Rule 4's
parent-dependent subchecks inapplicable. Matrix fixtures assert these exact
outputs; unavailable parents never fabricate extra findings.

Provider-free matrix tests trigger each rule alone and in valid combinations,
prove the exact order above, prove inapplicable dependent rules do not fabricate
findings, and prove any structural finding yields `ready_for_sprint=False`.

### Semantic provider result

The optional provider output is replaced with a bounded, source-bound schema:

```python
class StorySpecificationFinding(BaseModel):
    code: Literal[
        "SPEC_ITEM_CONTRADICTION",
        "SPEC_ITEM_OMISSION",
        "SPEC_ITEM_UNTESTABLE",
    ]
    spec_item_id: str
    message: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
    ]
    suggested_change: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
        ]
        | None
    ) = None


class StorySpecificationReviewOutput(BaseModel):
    schema_version: Literal["agileforge.story-specification-review.v1"]
    compliant: bool
    complete: bool
    findings: tuple[StorySpecificationFinding, ...]  # capped at 50
```

Every semantic finding is blocking and requires one exact item ID. Code defines
its allowed boundary:

- `SPEC_ITEM_CONTRADICTION` and `SPEC_ITEM_UNTESTABLE` must target the Story's
  own evidence set;
- `SPEC_ITEM_OMISSION` may target any item in the exact parent Backlog item's
  evidence set, whether the Story cited it or failed to cite it.

The host rejects duplicate `(spec_item_id, code)` pairs and sorts findings by
that tuple. `compliant` must equal `not findings`, `complete` must be true, and
the cap is 50. Unknown or out-of-bound IDs, truncation, malformed output, or
contract contradictions produce `STORY_SPECIFICATION_REVIEW_INVALID`. They are
never downgraded to advisory text and never repaired or inferred by the host.

### Persisted evidence

`ValidationEvidence` becomes a fresh-schema v2 contract containing:

- `schema_version="agileforge.story-validation-evidence.v2"`;
- exact Story artifact/item and Backlog artifact/item identities and
  fingerprints;
- exact `spec_version_id` and `spec_hash`;
- validation time, `story_validation_input_fingerprint`, validator version,
  mode, and overall result;
- all bounded structural failures; `structural_warnings` is the canonical empty
  tuple in v2 because the closed matrix defines no warning code;
- semantic review state `not_requested | valid | invalid`;
- all valid semantic findings;
- the canonical derived set `referenced_spec_item_ids`.

`story_validation_input_fingerprint` is the existing
`workflow.fingerprints.canonical_hash()` of one closed object with:

```text
schema_version = "agileforge.story-validation-input.v1"
project_id
story_id
source_story_artifact_id
source_story_artifact_fingerprint
source_story_item_id
source_story_item_fingerprint
source_backlog_artifact_id
source_backlog_artifact_fingerprint
source_backlog_item_id
spec_version_id
spec_hash
spec_item_ids                    # lexicographically sorted
title
statement                       # UserStory.story_description
persona
acceptance_criteria              # ordered canonical list, not display text
story_points
rank
```

The v1 invariant fields are deleted. A failed attempt may persist evidence for
diagnosis, but only a passing result makes the accepted Story ready for Sprint
planning. Persistence recomputes the fingerprint
from the exact row and parents, requires canonical byte equality for
`acceptance_criteria_json`, and compares the full row with the source Story item.
Readiness loading and Sprint planning recompute and compare it again;
evidence-only or lineage-only changes therefore stale the evidence. The Story
decision itself binds the artifact and item fingerprints in the same transaction
that creates the row, so no validation fingerprint exists or is required before
acceptance. Human Story review remains mandatory.

A structural failure or valid semantic finding leaves the accepted Story visible
but not ready for Sprint planning; correction requires a reviewed replacement
Story artifact. An invalid provider envelope also leaves it unready and may be
retried only by another explicit human validation action. Each hybrid attempt
makes exactly one provider call and never repairs or loops automatically.

The hybrid path makes one provider call. There is no repair call or model loop.

## Sprint and Task contract

`SprintPlannerInput` adds the exact Specification version, hash, and canonical
JSON once at the invocation root. Each `SprintPlannerStory` carries only its
canonical `spec_item_ids`; the host builds the input only from Stories whose
persisted version/hash equal the root. A mixed set is rejected before invocation.

`evaluated_invariant_ids`, compliance-boundary text derived from Authority, and
`validate_task_invariant_bindings()` are removed. They are replaced by
`relevant_spec_item_ids` validation against the parent Story and exact loaded
Specification.

Recording a Sprint-plan draft persists only an immutable
`SprintPlanArtifact`. It does not create, update, or delete `Team`, `Sprint`,
`SprintStory`, or `Task` rows. The artifact's canonical host envelope includes
`team_name`, `include_task_decomposition`, exact Specification identity,
`candidate_set_fingerprint`, and the validated `SprintPlannerOutput`; its
`plan_fingerprint` covers the entire envelope. Human review renders the exact
Task descriptions and resolved Specification evidence from that artifact.

The host mints `sprint_plan_stream_id` as `SPS-` plus 32 lowercase hexadecimal
UUID digits for the first plan in a planning cycle and replays it through
idempotency. Successors of feedback or rejected plans inherit the same stream.
A new stream is minted only after the prior stream's activated Sprint starts or
reaches a terminal state, or when a new Specification lineage begins. An
accepted but unstarted plan must be corrected inside its existing stream. A
rejected plan has no activated Sprint and therefore its replacement also stays
inside that stream.

The UUID is generated once for a new stream. A uniqueness collision fails
closed as `SPRINT_PLAN_STREAM_ID_COLLISION`; the host does not run a hidden
retry loop. An explicit caller retry may mint a new value, while an idempotent
replay reuses the already persisted value.

Stream selection and fingerprinting have one ordered host boundary. Preserve the
existing `WorkflowDomain` order: open the SQLite `BEGIN IMMEDIATE` writer
transaction, then authoritatively replay or claim the receipt inside it. A
completed exact receipt returns its stored result without entering the handler.
For a new claim, the same transaction continues into the lineage service, which
selects, reuses, or mints the stream from persisted state. Only then does the
Sprint phase construct the complete host envelope, compute `plan_fingerprint`,
and insert the artifact. Neither `sprint_plan_stream_id` nor
`plan_fingerprint` is accepted from the caller or provider. Concurrent distinct
idempotency keys serialize; after the first insert, the second observes the new
current artifact and either targets it as a legal same-stream successor or
fails stale. It cannot create a parallel current stream. No read-only receipt
preflight or second transaction protocol is added.

Feedback and rejection persist only the plan decision. Accepting the exact plan
atomically resolves or creates the Team, creates the planned Sprint on first
acceptance, materializes `SprintStory` and `Task` rows, and records the activated
`sprint_id` on the accepted `SprintPlanArtifactDecision`. Accepting a replacement
plan in the same stream may reuse and replace that Sprint only while it remains
unstarted and `PLANNED`, has no `SprintStart`, and has no Task execution log,
completion evidence, or terminal Story/Sprint fact. Otherwise replacement is
rejected. The accepted-A -> feedback-B -> accepted-C seam proves B leaves A
byte-identical and startable, while C replaces membership and Tasks only inside
C's acceptance transaction.

The rejected seam is rejected-A -> replacement-B -> accepted-B in the same
stream. If B receives feedback, accepted-C supersedes B in that same stream.

`SprintPlanArtifact` therefore stores `sprint_plan_stream_id`, not `sprint_id`.
An accepted decision stores required `activated_sprint_id`; feedback and rejected
decisions store null, enforced by a check constraint. `StartSprint` resolves the
sole current accepted plan in the stream and takes its Sprint identity from that
decision.

`TaskMetadata` becomes `task_metadata.v2` and contains:

```text
version = "task_metadata.v2"
spec_version_id
spec_hash
sprint_plan_stream_id
sprint_plan_artifact_id
sprint_plan_fingerprint
relevant_spec_item_ids
task_kind
artifact_targets
workstream_tags
checklist_items
```

There is no v1 fallback or dual reader. A missing or invalid v2 payload is an
integrity error at planning/execution boundaries, not a canonical-empty
fallback. `Task.metadata_json` becomes required and loses its context-free
default factory. Plan acceptance serializes v2 metadata from the immutable plan
artifact in the activation transaction. `StartSprint` then verifies every
Task's metadata, content fingerprint, plan identity, and `SprintStart` lineage
against the exact accepted plan before execution.

## Amendment and freshness policy

An accepted amendment creates a new current Delivery Lineage. Snapshot
isolation preserves work already executing; it does not permit new work on a
superseded contract.

| Object/state when amendment is accepted | Result | Allowed next action |
| --- | --- | --- |
| Draft, feedback, or rejected amendment | No effect on current lineage | Continue using the current accepted Specification |
| Prior Accepted Specification | Becomes `superseded`; bytes and decision remain immutable | Historical exact loads only |
| Unaccepted Backlog, Roadmap, or Story draft/review | Becomes stale by derived lineage; cannot be reviewed or accepted | Generate a replacement from the new current parent |
| Accepted Backlog, Roadmap, or Story artifact | Remains immutable historical evidence; no longer current | Start a new chain from the amended Specification |
| `UserStory` under prior lineage | Excluded from new planning by its pinned superseded version/hash; its same-lineage `is_superseded` marker is unchanged | Replace through the refreshed Backlog/Roadmap/Story chain; an active-Sprint row remains executable only there |
| Planned Sprint or accepted plan not started | `StartSprint` fails with `STALE_SPECIFICATION` | Generate and review a replacement plan |
| Current new-lineage plan while an old-lineage Sprint is active | Planning and review may finish, but every `StartSprint` entry point fails with `ACTIVE_SPRINT_EXISTS` | Close the active Sprint, then recheck exact plan freshness and start |
| Active Sprint | Keeps its pinned superseded Specification and existing Stories/Tasks | Complete existing Tasks; Tasks already cancelled remain terminal; review/close its Stories and close the Sprint; no add, regenerate, or replan |
| Completed Sprint, Story, or Task | Remains immutable historical evidence | Read/export only |
| Canonical packet | Always resolves the object's pinned exact version and shows `current` or `superseded` | Never substitute the current Specification |

An execution transition for superseded lineage is allowed only when it proves
membership in the matching active `SprintStart`. A loose old Story or Task
cannot use the active-Sprint exception. Specification acceptance mutates only
the prior registry status and creates the new registry. Cross-lineage
planning-currentness is always derived from version/hash; it never repurposes a
Story replacement marker or rewrites old Story/Task content.

## Fresh-schema hard break

No migration does not mean an old database may drift into a mixed schema.
Before `SQLModel.metadata.create_all()`, `_assert_current_business_schema()`
adds an issue-210 sentinel. An empty database is allowed and receives the fresh
schema. A non-empty database is rejected if any of these retired tables exist:

```text
compiled_spec_authority
spec_authority_acceptance
authority_feedback_attempts
authority_curation_attempts
```

It is also rejected if a retained table contains any retired column:

```text
backlog_artifacts.authority_id
backlog_artifacts.authority_fingerprint
spec_registry.approved_at
spec_registry.approved_by
spec_registry.approval_notes
story_artifacts.requirement_id
story_artifacts.story_ids_json
sprint_plan_artifacts.sprint_id
user_stories.acceptance_criteria
user_stories.source_requirement
user_stories.refinement_slot
user_stories.story_origin
user_stories.is_refined
user_stories.archived_reason
user_stories.archived_at
user_stories.archived_by
user_stories.archive_reset_attempt_id
user_stories.archive_previous_status
user_stories.original_acceptance_criteria
user_stories.ac_updated_at
user_stories.ac_update_reason
user_stories.superseded_by_story_id
```

The inspector compares `get_foreign_keys()` referred tables and both constrained
and referred columns, plus the column lists or SQL expressions returned by
`get_indexes()`, `get_unique_constraints()`, and `get_check_constraints()`.
Constraint identifier names alone do not decide compatibility. Any actual
reference to a retired table or column is rejected. This catches partially
transformed databases even when the four retired tables have already been
dropped.

Column presence is not sufficient. The sentinel builds normalized structural
signatures from table name, ordered constrained columns, referred table and
ordered referred columns, and a partial-index predicate or check expression
when present. It then requires these fresh business-critical signatures:

```text
UNIQUE / PARTIAL UNIQUE
  specification_decisions
    (project_id, specification_decision_id,
     specification_candidate_id, candidate_fingerprint)
  spec_registry
    (project_id, spec_version_id, spec_hash)
    (project_id, source_specification_candidate_id)
    (project_id) WHERE status = 'approved'
  backlog_artifacts
    (project_id, backlog_artifact_id, content_fingerprint)
    (project_id, product_goal_artifact_id, product_goal_fingerprint,
     spec_version_id, spec_hash, version_number)
    (project_id, product_goal_artifact_id, product_goal_fingerprint,
     spec_version_id, spec_hash, content_fingerprint)
  roadmap_artifacts
    (project_id, roadmap_artifact_id, content_fingerprint)
    (project_id, backlog_artifact_id, backlog_artifact_fingerprint,
     version_number)
    (project_id, backlog_artifact_id, backlog_artifact_fingerprint,
     content_fingerprint)
  story_artifacts
    (project_id, story_artifact_id, content_fingerprint)
    (project_id, source_backlog_artifact_id, backlog_item_id, version_number)
    (project_id, source_backlog_artifact_id, backlog_item_id,
     content_fingerprint)
  user_stories
    (project_id, source_story_artifact_id, source_story_item_id)
  sprint_plan_artifacts
    (project_id, sprint_plan_artifact_id, plan_fingerprint)
    (project_id, spec_version_id, spec_hash,
     sprint_plan_stream_id, version_number)
    (project_id, spec_version_id, spec_hash,
     sprint_plan_stream_id, plan_fingerprint)

FOREIGN KEY
  spec_registry
    (project_id, source_specification_decision_id,
     source_specification_candidate_id,
     source_specification_candidate_fingerprint)
      -> specification_decisions
         (project_id, specification_decision_id,
          specification_candidate_id, candidate_fingerprint)
  backlog_artifacts
    (project_id, spec_version_id, spec_hash)
      -> spec_registry (project_id, spec_version_id, spec_hash)
  roadmap_artifacts
    (project_id, backlog_artifact_id, backlog_artifact_fingerprint)
      -> backlog_artifacts
         (project_id, backlog_artifact_id, content_fingerprint)
  story_artifacts
    (project_id, source_backlog_artifact_id,
     source_backlog_artifact_fingerprint)
      -> backlog_artifacts
         (project_id, backlog_artifact_id, content_fingerprint)
    (project_id, roadmap_artifact_id, roadmap_artifact_fingerprint)
      -> roadmap_artifacts
         (project_id, roadmap_artifact_id, content_fingerprint)
  user_stories
    (project_id, accepted_spec_version_id, accepted_spec_hash)
      -> spec_registry (project_id, spec_version_id, spec_hash)
    (project_id, source_story_artifact_id,
     source_story_artifact_fingerprint)
      -> story_artifacts
         (project_id, story_artifact_id, content_fingerprint)
  sprint_plan_artifacts
    (project_id, spec_version_id, spec_hash)
      -> spec_registry (project_id, spec_version_id, spec_hash)

CHECK
  spec_registry
    status IN ('approved', 'superseded')
  sprint_plan_artifact_decisions
    (decision = 'accepted' AND activated_sprint_id IS NOT NULL) OR
    (decision IN ('feedback', 'rejected') AND activated_sprint_id IS NULL)
```

The implementation declares the complete expected signature set beside the
models and shares it with the sentinel tests; this document lists the subset
whose absence or mutation would break acceptance proof, exact lineage,
idempotent activation, or current-selection cardinality. Equivalent identifier
names are allowed, but a missing column, different column order, different
referred tuple, or broader/narrower predicate is incompatible.

The sentinel also explicitly rejects the retained-column project-global
uniques from the old model, even if every fresh column has been added:

```text
backlog_artifacts    (project_id, version_number)
backlog_artifacts    (project_id, content_fingerprint)
roadmap_artifacts    (project_id, version_number)
roadmap_artifacts    (project_id, content_fingerprint)
sprint_plan_artifacts (project_id, version_number)
sprint_plan_artifacts (project_id, plan_fingerprint)
```

Those constraints are not harmless extras. They prevent two valid lineages in
one Project from each starting at version 1 or from containing equal canonical
content. `create_all()` cannot replace constraints on existing tables, so any
one of them makes the database unsupported.

Finally, a non-empty database must contain these fresh columns. They are
non-nullable except where an explicit decision-dependent rule follows:

- `spec_registry.source_specification_decision_id`;
- `backlog_artifacts.spec_version_id` and `spec_hash`;
- `story_artifacts.source_backlog_artifact_id`,
  `source_backlog_artifact_fingerprint`, `backlog_item_id`, and
  `story_item_ids_json`;
- `user_stories.source_story_artifact_id`,
  `source_story_artifact_fingerprint`, `source_story_item_id`,
  `source_story_item_fingerprint`, `accepted_spec_version_id`,
  `accepted_spec_hash`, `spec_item_ids_json`, and `acceptance_criteria_json`;
- `sprint_plan_artifacts.spec_version_id`, `spec_hash`, and
  `sprint_plan_stream_id`;
- `sprint_plan_artifact_decisions.activated_sprint_id`, nullable only when the
  decision is not `accepted`;
- `tasks.metadata_json`.

The failure remains `UNSUPPORTED_BUSINESS_SCHEMA` and tells the operator to
create a fresh profile/database. `create_all()` never masks the break by adding
some new tables or columns beside old Authority data. Provider-free regression
tests open both the exact baseline #210-era schema and a partially transformed
schema with only retained-table remnants, and prove rejection occurs before
workflow reads or writes. Two focused mixed-schema fixtures go further: one has
every fresh column but retains the six project-global artifact uniques above;
the other has every fresh column and lineage constraint except the required
`user_stories(project_id, source_story_artifact_id, source_story_item_id)`
unique. Both must fail before `create_all()`. A parameterized structural test
removes or mutates each required signature in turn and proves the same failure.

## Removed Authority behavior

Authority compilation, normalization, repair, curation, review, projections,
workflow nodes, provider recipes, model roles, API routes, CLI commands, and UI
cards are removed. After Specification acceptance, Backlog is the next visible
phase.

The human's later decision supersedes the original constraints to retain one
#209 Authority repair call and add a gold compiled-Authority fixture: there is
no Authority call or artifact to repair. The remaining provider boundaries
retain the general #205/#207/#208/#209 safety principles—strict normalization,
stable identities, one bounded result, fail-closed persistence, and mandatory
human review—but not dead Authority-specific machinery.

## Fixtures and provider-free proof

The implementation preserves the exact attempt-30 audit evidence under
`tests/fixtures/issue_210/legacy_authority/`:

- outer request envelope;
- nested compiler input;
- nested Authority input;
- initial output;
- repaired output;
- a manifest with exact byte counts and SHA-256 values from the decision brief.

These fixtures have no production import. One provider-free integrity test
proves their bytes and hashes remain exact. They document the reproduced defect
after its runtime is deleted.

The replacement gold fixture is the complete human-accepted String Calculator
Specification candidate and canonical payload. Direct-Specification lifecycle
tests use it. Creating a new gold compiled Authority would contradict the
chosen architecture.

Required provider-free seams include:

1. Acceptance creates a registry bound to the exact accepted decision; missing,
   rejected, foreign, or mismatched decisions fail closed.
2. Specification acceptance makes `backlog.generate` available with zero
   provider calls.
3. Pending, absent, superseded, foreign, ambiguous, corrupt, or mismatched
   current Specifications block new Backlog work.
4. `RecordBacklogDraft` rejects a wrong Specification, Product Goal, item ID,
   or workflow reference before persistence.
5. Backlog, Roadmap, Story, Sprint, and validation inputs contain the exact gold
   Specification once and no Authority field.
6. Host-minted Backlog item IDs survive Roadmap and Story lookup without prose
   identity; Specification IDs are checked at Backlog -> Story -> Task boundaries.
7. Accepted A -> feedback B -> accepted C resolves C through transitive ancestry
   for Backlog, Roadmap, Story, and Sprint-plan chains; B alone does not displace A.
8. Story B creates no operational rows and cannot mutate accepted Story A;
   accepting C atomically activates only C's exact immutable items.
9. Story acceptance-criteria JSON round-trips embedded newlines, bullet prefixes,
   Unicode, and multiple items; empty or whitespace-only items fail before save.
10. Story-validation fingerprints change on content, evidence, Specification, or
   artifact-lineage changes and are rechecked before planning.
11. Duplicate, unbound, unknown, or code-out-of-bound semantic findings fail in
   one deterministic bounded pass; the host does not infer a replacement binding.
12. Story evidence and UserStory rows pin exact version and hash.
13. Accepted Sprint plan A remains byte-identical and startable after feedback B;
    accepted C atomically replaces only the unstarted operational plan.
14. A planned old Sprint cannot start after amendment; an already active Sprint
   can complete its pinned Tasks, treat already-cancelled Tasks as terminal,
   review/close its Stories, emit packets, and close, while a new-lineage Sprint
   cannot start concurrently.
15. Packets render exact pinned items and expose currentness without rebinding.
16. Workflow, application, API, CLI, dashboard, prompts, recipes, model roles,
    scripts, benchmarks, README, table metadata, and production imports expose
    no live Authority operation.
17. Fresh-schema metadata contains no Authority table or v1 task/evidence path;
    exact old and partially mixed schemas fail with `UNSUPPORTED_BUSINESS_SCHEMA`.

## Implementation sequence

This hard break is one coupled delivery-contract cutover, not a staged dual
reader. TDD still proceeds seam by seam, but no partial cutover is committed or
presented as green.

1. Preserve attempt-30 evidence, add the gold accepted-Specification fixture,
   and write the public-seam RED tests.
2. Build the new deep loader, canonical item-reference helpers, and review
   projections behind focused tests without changing the live workflow.
3. In one atomic hard-break working slice, change the schema guard and models;
   switch workflow facts/graph, Backlog, Roadmap, Story, validation, Sprint,
   Task, packets, application, and operator surfaces; and delete Authority
   production/schema surfaces. The repository-wide gate may remain red inside
   this working slice but must be green before any commit.
4. Run a bounded deletion inventory covering production imports, table metadata,
   workflow nodes, application methods, API/CLI, frontend, prompts, recipes,
   model roles, scripts, benchmarks, SQL/generated artifacts, README, and tests.
   In live production surfaces the prohibited names are
   `CompiledSpecAuthority`, `SpecAuthorityAcceptance`, `compiled_authority`,
   `authority_id`, `authority_fingerprint`, `relevant_invariant_ids`,
   `evaluated_invariant_ids`, and Authority workflow/action/CLI/API operations.
   Authority terms are allowed only in `CONTEXT.md`, ADRs, feedback/handoffs,
   this design, `tests/fixtures/issue_210/legacy_authority/`, the old-schema
   guard/tests, and deletion tests. This is a scoped live-surface inventory, not
   a brittle repository-wide word ban; README may not expose live Authority.
   The same cutover deletes obsolete Backlog-seed, prose/slot Story linkage,
   reset-archive, mutable acceptance-criteria, and targeted partial-write fields,
   services, projections, contracts, and tests. Targeted correction is rebuilt
   only as the full immutable replacement-artifact path defined above.
5. Verify fresh database creation and exact old-schema rejection. Run focused
   lifecycle tests and `uv run --frozen pyrepo-check --all`; obtain independent
   correctness, scope, and lean reviews before the single completed-fix commit.

## Rejected alternatives

### Store the whole rule as a compiled text Authority

Rejected. It is readable and lossless, but it duplicates the already readable
accepted Specification, adds a second identity and review, and has no proven
consumer that needs the copy.

### Expand a specialized invariant algebra

Rejected. It creates a cross-industry taxonomy maintenance burden and still
requires a model to reinterpret accepted meaning before work can begin.

### Add String Calculator cue words

Rejected. It treats one symptom and leaves lexical authorization as the
semantic gate.

### Add a separate delivery-activation decision

Rejected. Specification acceptance already targets exact immutable bytes.
Backlog generation remains a separate explicit human action, so another
semantic approval adds no safety.

### Infer machine checks from Specification prose

Rejected. It recreates the same ungrounded interpretation inside the host.
Future deterministic behavior must be represented by an explicit field added
to the Specification for a proven consumer and reviewed by the human.

## Main caveat

Direct Specification removes a false machine-readable guarantee. AgileForge
will prove identity, bytes, lineage, source references, and lifecycle freshness,
but it will not prove that arbitrary English product rules are semantically
satisfied. Optional model review can find grounded issues; mandatory human
reviews remain final.

This is intentional. If a future concrete tool needs executable grammar,
protocol, or policy data, extend `agileforge.spec.v2` with the smallest explicit
typed field required by that tool. Do not add a provider-compiled universal
taxonomy for every industry.

## Boundaries

Always:

- preserve exact source grounding, stable item IDs, canonical bytes, and
  Specification fingerprints;
- keep Specification, Backlog, Roadmap, Story, and Sprint human reviews;
- fail closed on decision, ownership, lineage, item-reference, byte, or
  freshness mismatch;
- remain provider-free during implementation and verification.

Never:

- parse prose to infer machine-enforcement types or semantic relevance;
- add an Authority compatibility layer, migration, dual read, or hidden
  fallback;
- introduce an automatic provider call, repair call, or model loop;
- mutate the protected Manual Test profile;
- start the Manual Test UI, transfer a profile, click Compile, push, merge,
  close #210, or modify `master`.

## Approval gate

The implementation gate was satisfied on 2026-08-21 after:

1. independent correctness and lean/scope preimplementation reviews have no
   unresolved blocker;
2. this document and ADR 0005 agree;
3. the human explicitly approves this corrected design.
