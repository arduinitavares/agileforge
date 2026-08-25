# Story Draft Quality Contract Design

> [!IMPORTANT]
> **Supersession Notice (2026-08-24 / Issue #221):**
> The aggregate INVEST scoring mechanism described below (`invest_score`: High/Medium/Low, `decomposition_warning`, and the aggregate all-`Low` save gate) is **SUPERSEDED** by AgileForge Issue #221 and the [Story Refinement to Sprint Selection Design Handoff](../../feedback/2026-08-24-story-refinement-to-sprint-selection-design-handoff.md).
>
> **Updated INVEST Contract & Operational Semantics:**
> - Aggregate `invest_score` and `decomposition_warning` are retired and removed (hard break).
> - Each Story item requires an explainable `StoryInvestAssessment` evaluating all six dimensions: `independent`, `negotiable`, `valuable`, `estimable`, `small`, `testable`.
> - Each dimension contains: `result` (`pass` | `concern` | `fail`), `rationale` (non-blank string), and `evidence` (non-blank string).
> - **Operational Semantics:** Per-dimension results are advisory model recommendations provided as evidence for human review, subject to human discretion.
> - **Host Gate & Failure Safety:** Deterministic host checks establish bounded structural completeness. Missing, malformed, or blank assessment dimensions fail closed and disable review acceptance in Browser UI and CLI. Semantic quality ratings (`concern` / `fail`) do not silently block or approve state; they are surfaced as review evidence for human decision-making.

## Goal

Make Story draft generation honest about coverage and quality. A bounded story
writer attempt must no longer silently return eight low-quality stories and let
the Story phase treat them as complete, reusable, and saveable.

## Non-Goals

- Do not add story pagination or multi-batch orchestration in this change.
- Do not remove the per-attempt story cap.
- Do not add ASA-specific thresholds or product names to Story phase logic.
- Do not change saved Story persistence schemas in this first pass.

## Current Problem

The Story writer has a hard eight-story output cap in schema and prompt. That cap
is useful for output stability, but today it can conflict with refinement
requests that explicitly ask for more decomposition. The runtime also treats any
schema-valid output as reusable, and save eligibility only checks structural
completeness plus a narrow merge-recommendation exception. As a result, a draft
can be complete and saveable even when every story rates itself `Low` or when the
user asked for more decomposition than the attempt can represent.

`invest_score` is also overloaded. Normal research uncertainty currently forces
stories to `Low`, even when the story format and decomposition are otherwise
valid. Research caveats should remain visible, but should not be the same field
as decomposition failure.

## Design

Add an explicit story quality contract to the Story writer output:

- `quality_schema_version`: fixed value `agileforge.story_quality.v1`
- `coverage_status`: `complete`, `partial_capacity_limited`, or
  `needs_clarification`
- `remaining_scope`: concrete uncovered terms or requested slices
- `quality_findings`: structured findings with `code`, `severity`, `message`,
  and optional story indexes/titles

Add `research_caveats` to each draft story. Research caveats are advisory risk notes.
Under Issue #221, `invest_score` and `decomposition_warning` are replaced by the
explainable six-dimension `invest_assessment`.

Add deterministic server-side quality evaluation in the Story runtime:
- `coverage_status` is not `complete`
- a refinement request asks for more stories than the per-attempt cap and the
  output returns only the capped count as complete
- `remaining_scope` is empty for capacity-limited or clarification-needed output
- story INVEST quality assessment must be structurally complete across all 6 dimensions


Tie `is_reusable` to this quality gate. A successful model response can still be
recorded as an attempt, but it is not a reusable draft unless the quality gate
passes. Non-reusable quality failures keep the phase in `STORY_INTERVIEW`; they
do not expose save guards.

Surface the quality summary in `story generate` and `story retry` responses:

- top-level `attempt_id`
- top-level `artifact_fingerprint`
- `story_count`
- `invest_score_counts`
- `is_reusable`
- `quality`
- existing nested `current_draft` and `save` fields remain for compatibility

## Server-Side Coverage Rules

The first pass only parses low-risk refinement intent from text already captured
in the request payload. It extracts requested story count from phrases such as
`15 stories`, `~15 smaller stories`, and `about 12 sub-stories`. If the requested
count exceeds the per-attempt cap and the model reports `complete`, the runtime
adds a blocking `REQUESTED_STORY_COUNT_EXCEEDS_CAP` finding and marks the draft
non-reusable unless `coverage_status` is `partial_capacity_limited` with concrete
`remaining_scope`.

The gate does not infer arbitrary missing scope from prose. It only uses explicit
count requests and emitted quality fields. Broader term coverage can be added
later once the request model has a typed coverage-intent field.

## Save & Review Gate

The story review gate requires complete and quality-valid story drafts:

- `is_complete` is true
- `coverage_status` is `complete`
- no `quality_findings` have `severity=blocking`
- all draft stories contain a valid, complete `invest_assessment` across all 6 dimensions
- no merge recommendation exists

Per-dimension `concern` or `fail` ratings remain advisory evidence for human review, empowering the human operator to make an informed decision (Accept, Request changes, or Reject) rather than relying on an opaque automated all-`Low` block. Missing or malformed assessments fail closed.

## Regression Fixture

Use ASA only as a regression fixture. The product behavior is generic:

- if a refinement asks for about fifteen smaller stories, the draft either
  returns quality-saveable complete coverage or explicitly reports capacity
  limitation with concrete remaining scope
- it must not silently return low-quality stories as complete/saveable

## Acceptance Criteria

- Story writer schema accepts the new quality contract, research caveats, and 6-dimension `invest_assessment`.
- Each story provides explainable `result`, `rationale`, and `evidence` across all six INVEST dimensions.
- Missing or malformed INVEST assessments fail closed and disable review acceptance in Browser UI and CLI.
- Complete output that hits an explicit requested story-count over the cap is not
  reusable unless it reports partial coverage with concrete remaining scope.
- `story generate` and `story retry` responses expose the save guards and quality
  summary at top level.
- Existing save guard checks still reject stale or mutated artifacts.
