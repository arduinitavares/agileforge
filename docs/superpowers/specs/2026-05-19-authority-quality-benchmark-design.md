# Authority Quality Benchmark Design

Date: 2026-05-19
Status: Draft for user review
Branch context: `dev/authority-coverage-matrix-phase-2e`

## Problem

Phase 2E removes deterministic prose extraction as the authority acceptance
judge. That makes the workflow usable again, but it also moves the burden of
semantic quality back where it belongs: LLM compilation plus human review.

AgileForge needs a small, project-agnostic benchmark that answers this question:

```text
Given a structured AgileForge spec, is the compiled authority faithful, useful,
and safe enough for downstream Vision, Backlog, Story, Sprint, and execution
agents?
```

The benchmark must avoid recreating the old failure mode. Host code should not
scan prose and decide whether requirements were covered. The benchmark should
produce evidence for human review and external LLM review.

## Goals

- Test authority quality across multiple spec styles, not only caRtola.
- Use small public fixtures that a human can review in one sitting.
- Compare a human review with three external LLM reviews.
- Keep semantic judgment outside deterministic host extraction.
- Identify missing, distorted, or over-promoted authority items before building
  CLI lifecycle commands on top of accepted authority.
- Produce benchmark artifacts that are easy to inspect, diff, and repeat.

## Non-Goals

- No deterministic requirement extraction from fixture prose.
- No automatic pass/fail score based on host semantic analysis.
- No claim that four reviews prove authority correctness.
- No formal verification of compiled authority.
- No requirement that every fixture reaches perfect authority on the first run.
- No hard-coded fixture-specific compiler fixes.

## Fixtures

### TodoMVC

Source: `https://raw.githubusercontent.com/tastejs/todomvc/refs/heads/master/app-spec.md`

Purpose:

- Product behavior benchmark.
- Tests UI behavior, editing, routing, persistence, counters, examples, and
  implementation guidance.
- Best first fixture because it is small and close to AgileForge's intended
  product-management use case.

Expected authority themes:

- empty todo state
- new todo creation
- trimmed empty input rejection
- mark-all-complete behavior
- individual todo completion
- edit-save, edit-cancel, and empty-edit deletion
- active todo counter and pluralization
- clear completed behavior
- localStorage persistence
- route filters for all, active, and completed todos

### OpenAPI Petstore

Source: `https://learn.openapis.org/examples/v3.0/petstore.html`

Purpose:

- API contract benchmark.
- Tests paths, methods, request bodies, response contracts, schemas, required
  fields, path parameters, and numeric constraints.

Expected authority themes:

- `GET /pets`
- `POST /pets`
- `GET /pets/{petId}`
- `limit` query parameter with maximum `100`
- `petId` required path parameter
- `Pet` schema with required `id` and `name`
- `Pets` array with `maxItems=100`
- `Error` schema with required `code` and `message`
- expected success and default error responses

### Gherkin/Cucumber

Source: `https://cucumber.io/docs/gherkin/reference/`

Purpose:

- Scenario and acceptance-criteria benchmark.
- Tests whether AgileForge preserves examples as behavioral evidence without
  turning every `Given` into a global invariant.

Expected authority themes:

- `Feature` as high-level feature context
- `Rule` as business rule grouping
- `Example`/`Scenario` as concrete behavior
- `Given`, `When`, `Then`, `And`, and `But` as scenario steps
- scenario outlines and examples tables
- doc strings and data tables as step arguments
- language/localization support as a tooling constraint

## Benchmark Artifact Layout

Each fixture lives under a committed benchmark directory:

```text
benchmarks/authority-quality/<fixture>/
  source/
    source.md
    source.url
    source.sha256
  agileforge/
    spec.json
    spec.md
    compiled-authority.json
    review-summary.json
  reviews/
    review-prompt.md
    human-review.md
    gpt-5.5-review.md
    claude-review.md
    gemini-review.md
    review-synthesis.md
```

Raw CLI envelopes, guard tokens, project IDs, and idempotency keys are local run
artifacts and are not committed. They live under:

```text
.agileforge/benchmark-runs/authority-quality/<fixture>/<run-id>/
```

The committed layout requirements are:

- source snapshot is preserved
- generated `agileforge.spec.v1` JSON is preserved
- rendered Markdown review view is preserved
- compiled authority is preserved separately from the full CLI packet
- review summary is sanitized and contains no guard tokens
- review reports are stored as plain Markdown
- synthesis records disagreements and the final human decision

## Per-Fixture Flow

For each fixture:

1. Save the source snapshot and hash.
2. Ask the spec-generation skill or LLM workflow to produce:
   - `spec.json`
   - `spec.md`
3. Validate the structured spec:

```bash
agileforge spec profile validate --spec-file specs/spec.json
```

4. Create an AgileForge project:

```bash
agileforge project create \
  --name "<fixture name>" \
  --spec-file specs/spec.json \
  --idempotency-key "<fixture>-authority-quality-<date>"
```

5. Review authority:

```bash
agileforge authority review \
  --project-id <project_id> \
  --include-spec full
```

6. Extract the compiled authority from the review packet.
7. Perform four semantic reviews:
   - human review
   - GPT-5.5 review
   - Claude review
   - Gemini review
8. Synthesize the review reports.
9. Accept only if the human reviewer decides the authority is good enough.

## Quality Rubric

The benchmark uses semantic review, not deterministic prose extraction.

Reviewers score:

- **Completeness:** major source requirements are represented.
- **Fidelity:** authority does not distort or invent source meaning.
- **Downstream Utility:** Vision, Backlog, Story, Sprint, and execution agents
  receive enough guidance to proceed.
- **Reviewability:** source references and authority organization are easy to
  audit.
- **Acceptance Confidence:** practical confidence that the authority can become
  canonical for the project.

Reviewers look especially for:

- missing core behavior
- examples promoted into universal rules
- sample values promoted into global limits
- unsupported source requirements hidden as assumptions
- vague or dishonest gaps
- invented constraints
- bloated low-value authority
- source references that are too broad to audit

## Shared External Review Prompt

Use this exact prompt for GPT-5.5, Claude, and Gemini so the reports are
comparable:

````text
You are reviewing an AgileForge compiled authority artifact against its source technical specification.

Context:
- AgileForge compiles a structured technical spec into a compact authority artifact.
- The authority artifact will guide later Vision, Backlog, Story, Sprint, and implementation agents.
- The host system should not use deterministic prose extraction as the semantic judge.
- Your job is semantic review: assess whether the compiled authority is faithful, useful, and safe enough for downstream planning.
- Do not demand perfection. The goal is practical project guidance, not formal verification.
- Do not recommend deterministic requirement extraction from prose as the solution.

Inputs I will provide:
1. Source spec as rendered Markdown.
2. Canonical structured spec JSON.
3. Compiled authority artifact JSON.
4. Sanitized authority review summary JSON.

Review Tasks:
1. Assess whether the compiled authority captures the core requirements of the source spec.
2. Identify important missing requirements, behaviors, constraints, data contracts, API contracts, or acceptance criteria.
3. Identify any authority items that distort the spec, overgeneralize examples, or turn sample values into universal rules.
4. Check whether assumptions and gaps are honest and useful, or whether they hide missing authority.
5. Check whether source references are specific and useful enough for a human reviewer.
6. Assess whether Vision/Backlog/Story/Sprint agents would receive enough guidance to continue safely.
7. Distinguish high-severity blockers from normal improvement suggestions.
8. Recommend whether a human should accept the authority, reject it, or accept it with noted risks.

Scoring:
- Completeness: 1-10
- Fidelity: 1-10
- Downstream Utility: 1-10
- Reviewability: 1-10
- Acceptance Confidence: 1-10

Output Format:

## Verdict
Choose one:
- ACCEPT
- ACCEPT_WITH_RISKS
- REJECT

Briefly explain why.

## High-Severity Blockers
List only issues that should block authority acceptance. If none, say "None."

For each blocker include:
- Issue
- Evidence from source spec
- Evidence from compiled authority
- Why it matters
- Recommended fix

## Medium/Low Findings
List useful improvements that should not necessarily block acceptance.

## Missing or Weak Authority Coverage
List major source requirements or behaviors that are missing, weak, or only indirectly represented.

## Over-Promoted or Distorted Authority
List authority items that appear to overfit examples, invent rules, or misclassify guidance.

## Assumptions and Gaps
Assess whether assumptions and gaps are accurate, honest, and actionable.

## Source Reference Quality
Assess whether source references are specific enough to audit.

## Downstream Readiness
Explain whether Vision, Backlog, Story, Sprint, and implementation agents can proceed with this authority.

## Scores
Completeness:
Fidelity:
Downstream Utility:
Reviewability:
Acceptance Confidence:

## Final Recommendation
Give a concise recommendation for the human reviewer.
````

## Review Synthesis

The synthesis is not a vote. Agreement is evidence, not proof.

The synthesis should record:

- each reviewer verdict
- score table
- blockers reported by more than one reviewer
- blockers reported by only one reviewer but supported by strong evidence
- disagreements and likely cause
- human decision: accept, accept with risks, reject, or regenerate spec/authority
- follow-up changes needed before end-to-end CLI lifecycle testing

## Acceptance Policy For Benchmark Runs

The benchmark may accept an authority artifact when:

- `agileforge authority review` reports no structural blockers
- the human reviewer has read the compiled authority
- high-severity blockers are absent or consciously accepted as risks
- source references are usable enough for practical audit
- downstream agents would receive enough guidance to start Vision/Backlog work

The benchmark should reject or regenerate when:

- multiple reviewers identify the same missing core behavior
- authority invents important constraints
- examples or sample values become global rules
- gaps or assumptions hide requirements that should be authority
- source references are too broad to audit
- the compiled authority would mislead downstream planning agents

## Success Criteria

The benchmark is successful when:

- all three fixtures can be converted to `agileforge.spec.v1`
- all generated specs validate structurally
- authority compilation produces reviewable artifacts
- external reviewers can assess authority without reading host internals
- the benchmark reveals actionable quality issues if authority is weak
- no benchmark step depends on deterministic prose extraction from source text
