# Story Feedback Quality Soft Gate Design

## Goal

Improve Story refinement feedback before it reaches the Story writer. Vague
feedback such as "make this more INVEST" should not start another generation run
by default, because it usually produces another broad, low-quality draft. The
system should explain what is missing and provide a rewrite template so the user
can submit actionable feedback.

## Non-Goals

- Do not change the Story writer output cap.
- Do not add story batching or pagination.
- Do not add ASA-specific rules, thresholds, or terms.
- Do not replace the existing Story draft quality gate.
- Do not block first-time Story generation with no feedback.
- Do not require perfect prose; short structured feedback is enough.

## Problem

The Story draft quality gate now blocks partial, over-broad, or all-Low drafts.
However, refinement input is still raw text. The CLI and UI accept weak feedback
without checking whether it tells the agent where the problem is, what should
change, and how success will be judged.

This creates a loop:

1. The draft is blocked by quality findings.
2. The user gives broad feedback.
3. The next generation repeats broad decomposition.
4. The draft remains low quality or partial.

The missing product behavior is a feedback quality check before regeneration.

## Design

Add a project-agnostic feedback quality contract for Story refinement input:

- `schema_version`: fixed value `agileforge.story_feedback_quality.v1`
- `needs_revision`: whether feedback should be revised before generation
- `can_force`: whether an expert override can bypass the soft gate
- `score`: integer from 0 to 100
- `present_fields`: detected feedback fields
- `missing_fields`: fields needed for actionable feedback
- `warnings`: reader-facing warnings
- `suggested_template`: structured rewrite template
- `suggested_example`: optional example filled from known Story quality context

The expected feedback fields are:

- `target`: exact requirement, attempt, story, or section
- `issue`: observable problem
- `evidence`: quality finding, remaining scope, source constraint, or
  contradiction
- `required_change`: exact add/remove/split/rewrite/preserve instruction
- `acceptance_criteria`: observable success conditions
- `scope_limit`: what not to change
- `priority`: must, should, or optional

The evaluator should be deterministic and local. It may use simple field-label
detection plus lightweight phrase heuristics, but it must not call an LLM.

## Soft Gate Behavior

Only Story refinement input is gated. A first Story generation with no feedback
continues to run normally.

When feedback quality is good enough:

- generation runs normally
- the response includes `feedback_quality` for transparency
- feedback is appended to the Story runtime as it is today

When feedback is weak:

- return `ok=true`
- do not run the Story writer
- do not append a Story attempt
- do not mark feedback as absorbed
- include `generation_ran=false`
- include `feedback_quality.needs_revision=true`
- include `feedback_quality.missing_fields`
- include `feedback_quality.suggested_template`
- keep the phase in `STORY_INTERVIEW`

When the user passes an explicit override:

- generation runs
- `feedback_quality.needs_revision` remains visible
- response records `feedback_quality.forced=true`
- the feedback entry stores that it was forced

The CLI override should be explicit, for example:

```bash
agileforge story generate \
  --project-id 3 \
  --parent-requirement "Technology and Model Research Spike" \
  --input "..." \
  --force-feedback
```

## Feedback Evaluation Rules

The first pass should not overfit to one project or try to parse arbitrary
natural language perfectly. It should catch common weak inputs and guide users
toward a reliable structure.

Inputs that should need revision:

- "make this better"
- "make it more INVEST"
- "fix the Low stories"
- "try again"
- feedback shorter than a small minimum and missing target/action language
- feedback with only a complaint and no required change
- feedback with required change but no target or acceptance criteria

Inputs that should pass:

- structured feedback using the target/issue/evidence/change/criteria shape
- short feedback that still names a target, a concrete slice, and a clear
  required change
- feedback that explicitly narrows to one remaining-scope item

Recommended minimum required fields for normal pass:

- `target`
- `issue` or `evidence`
- `required_change`
- `acceptance_criteria`
- `scope_limit`

`priority` is recommended but not required for pass.

## Example

Weak feedback:

```text
Make this more INVEST.
```

Soft-gate response should suggest:

```text
Target:
Technology and Model Research Spike, attempt-6

Issue:
Draft is partial_capacity_limited and not saveable.

Evidence:
quality.blocking_findings includes PARTIAL_CAPACITY_LIMITED; remaining_scope
includes delay horizon.

Required change:
Refine only delay-horizon validation.

Acceptance criteria:
- Stories cover only delay-horizon validation.
- Each story has one user goal.
- Each story has testable acceptance criteria.
- Draft returns coverage_status=complete for the narrowed slice.

Scope limit:
Do not cover state-window, stack, action-set, or recovered-code work.

Priority:
Must fix.
```

## CLI Surface

`story generate` should include feedback quality details in the normal JSON
envelope when weak feedback is submitted:

```json
{
  "ok": true,
  "data": {
    "generation_ran": false,
    "feedback_quality": {
      "schema_version": "agileforge.story_feedback_quality.v1",
      "needs_revision": true,
      "can_force": true,
      "missing_fields": ["target", "acceptance_criteria", "scope_limit"],
      "suggested_template": "..."
    }
  }
}
```

Human-readable CLI output should lead with the actionable next step:

```text
Feedback needs revision before Story generation.
Missing: target, acceptance_criteria, scope_limit.
Use --force-feedback to run anyway.
```

## UI Surface

The Story refinement textarea should make the expected feedback shape visible
without adding clutter:

- update placeholder to mention target, issue, evidence, required change,
  acceptance criteria, and scope limit
- when weak feedback is entered, show a warning panel with missing fields and a
  copyable template
- do not show a saved/run attempt when generation did not run
- keep existing quality findings visible so users can cite them in feedback

## Data Flow

1. User submits Story refinement input.
2. Story phase service evaluates feedback quality before appending feedback or
   invoking the Story writer.
3. If feedback needs revision and no force flag is present, service returns a
   soft-gate response.
4. If feedback passes or force flag is present, service appends feedback and
   runs the existing Story generation path.
5. Story output quality gate remains the final save/review gate.

## Error Handling

The feedback soft gate is not an error. It returns `ok=true` because the system
handled the request and produced next-step guidance.

Only malformed command input, missing project/requirement, unavailable state, or
runtime failures should return existing error envelopes.

## Testing

Add focused tests proving:

- vague feedback does not run the Story writer
- response includes `generation_ran=false`
- response includes missing fields and suggested template
- specific structured feedback runs generation
- force flag bypasses the soft gate and records `forced=true`
- first-time generation without feedback still runs
- no Story attempt is appended when generation does not run
- UI/CLI response preserves existing fields for normal Story generation
- no ASA-specific strings or thresholds are required by evaluator code

## Acceptance Criteria

- Weak Story refinement feedback is intercepted before model generation.
- The response gives concrete missing fields and a rewrite template.
- Strong structured feedback continues through existing Story generation.
- Expert override is available and explicit.
- Story output quality gate remains unchanged and still blocks bad drafts.
- Existing saved Story drafts are not modified by feedback evaluation.
