# Authority Quality Benchmark Design

Date: 2026-05-19
Last revised: 2026-05-20
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

## Benchmark Questions

The benchmark separates two quality questions:

1. **Spec generation quality:** Can an LLM/spec-generation workflow convert a
   public source fixture into a valid, faithful `agileforge.spec.v1` structured
   spec?
2. **Authority compilation quality:** Given a reviewed structured spec, does
   AgileForge compile faithful, useful, reviewable authority?

Authority compilation quality must be judged from the reviewed gold structured
spec, not from the first raw generated spec. Otherwise, a bad authority artifact
could be caused by a bad `spec.json`, not by the authority compiler.

Each fixture therefore keeps both:

- a raw generated structured spec
- a human-reviewed gold structured spec

The gold structured spec may be identical to the generated one when review finds
no material issues. Any correction must be recorded in the fixture changelog.

## Fixtures

### TodoMVC

Source: `https://raw.githubusercontent.com/tastejs/todomvc/refs/heads/master/app-spec.md`

Purpose:

- Product behavior benchmark.
- Tests UI behavior, editing, routing, persistence, counters, examples, and
  implementation guidance.
- Best first fixture because it is small and close to AgileForge's intended
  product-management use case.

Oracle notes:

These notes are benchmark oracles for human synthesis. They are not provided to
the spec generator, authority compiler, or external LLM reviewers.

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

Oracle notes:

These notes are benchmark oracles for human synthesis. They are not provided to
the spec generator, authority compiler, or external LLM reviewers.

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

Oracle notes:

These notes are benchmark oracles for human synthesis. They are not provided to
the spec generator, authority compiler, or external LLM reviewers.

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
    raw/
      source.raw.<ext>
    source.md
    source.sha256
    source.meta.json
  oracle/
    oracle-notes.md
  agileforge/
    generated-spec/
      spec.json
      spec.md
      structured-spec-review.md
    gold-spec/
      spec.json
      spec.md
      change-log.md
    compiled-authority.json
    review-summary.json
    run-manifest.json
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

- raw source snapshot is preserved
- normalized `source.md` is preserved
- `source.sha256` is the SHA-256 of normalized `source.md`
- `source.meta.json` records source URL, fetch date, raw artifact path, raw
  hash, normalization method, normalized hash, and license note
- oracle notes are separated from generator/compiler/reviewer inputs
- raw generated `agileforge.spec.v1` JSON is preserved
- reviewed gold `agileforge.spec.v1` JSON is preserved
- rendered Markdown review views are preserved for both generated and gold specs
- compiled authority is preserved separately from the full CLI packet
- review summary is sanitized and contains no guard tokens
- `run-manifest.json` records sanitized reproducibility metadata
- review reports are stored as plain Markdown
- synthesis records disagreements and the final human decision

`run-manifest.json` contains no secrets, guard tokens, raw project IDs, or raw
LLM response payloads. It records:

- AgileForge commit and branch
- schema version
- compiler version
- spec-generation model/provider
- authority-compiler model/provider
- prompt or skill version identifiers
- normalized source hash
- gold spec hash
- compiled authority hash
- create/review/extraction commands with local-only IDs redacted
- generation and compile timestamps
- acceptance mutation status: `not_run`, `accepted`, `rejected`, or
  `negative_control`

## Per-Fixture Flow

For each fixture:

1. Fetch the source fixture and save the raw artifact under `source/raw/`.
2. Normalize the source into `source/source.md`.
3. Write `source/source.meta.json` and `source/source.sha256`.
4. Create `oracle/oracle-notes.md` from the fixture notes in this design. Do not
   pass oracle notes to the spec generator, authority compiler, or external LLM
   reviewers.
5. Ask the spec-generation skill or LLM workflow to produce the raw generated
   spec:
   - `agileforge/generated-spec/spec.json`
   - `agileforge/generated-spec/spec.md`
6. Validate the raw generated structured spec:

```bash
agileforge spec profile validate \
  --spec-file benchmarks/authority-quality/<fixture>/agileforge/generated-spec/spec.json
```

7. Review the raw generated spec against `source/source.md` and record the
   result in `agileforge/generated-spec/structured-spec-review.md`.
8. If the raw generated spec has material fidelity issues, correct it into the
   gold structured spec and record changes in `agileforge/gold-spec/change-log.md`.
   If no material issues exist, copy the generated spec and Markdown into
   `agileforge/gold-spec/`.
9. Validate the gold structured spec:

```bash
agileforge spec profile validate \
  --spec-file benchmarks/authority-quality/<fixture>/agileforge/gold-spec/spec.json
```

10. Create an AgileForge project from the gold structured spec:

```bash
agileforge project create \
  --name "<fixture name>" \
  --spec-file benchmarks/authority-quality/<fixture>/agileforge/gold-spec/spec.json \
  --idempotency-key "<fixture>-authority-quality-<date>"
```

11. Review authority:

```bash
agileforge authority review \
  --project-id <project_id> \
  --include-spec full
```

12. Extract the compiled authority and sanitized review summary from the review
    packet.
13. Write `agileforge/run-manifest.json`.
14. Perform four semantic authority reviews:
    - human review
    - GPT-5.5 review
    - Claude review
    - Gemini review
15. Synthesize the review reports.
16. Record a benchmark verdict.

The authority compiler benchmark uses only `agileforge/gold-spec/spec.json`.
The raw generated spec review is a separate measurement of spec-generation
quality.

## Structured Spec Review Gate

The structured spec review gate happens before authority compilation. Its job is
to keep spec-generation defects from being misattributed to the authority
compiler.

The reviewer compares `source/source.md` with
`agileforge/generated-spec/spec.md` and `agileforge/generated-spec/spec.json`.
The review records:

- missing major source behavior or contract
- source behavior assigned to the wrong item type
- examples promoted into normative requirements
- unresolved source ambiguity represented as accepted scope
- `proposed` or `open_question` items that would become enforceable authority
  without human promotion
- source material intentionally excluded from the gold spec

The gate outputs either:

- `generated_accepted_as_gold`: generated spec is copied to `gold-spec/`
- `gold_corrected`: generated spec is corrected before authority compilation
- `source_unsuitable`: fixture source needs better normalization before spec
  generation can be judged

## Source Acquisition And Normalization

Every fixture must be reproducible from the committed source artifacts.

`source/source.meta.json` uses this shape:

```json
{
  "source_url": "https://example.test/spec",
  "fetched_at": "2026-05-20T00:00:00Z",
  "raw_artifact": "source/raw/source.raw.md",
  "raw_sha256": "sha256:...",
  "normalized_artifact": "source/source.md",
  "normalized_sha256": "sha256:...",
  "normalization": {
    "method": "raw-markdown-copy",
    "tool": "manual",
    "tool_version": "n/a",
    "notes": "Line endings normalized to LF."
  },
  "license_note": "Short source/license attribution note."
}
```

Normalization rules:

- convert line endings to LF
- preserve source wording
- remove navigation, cookie banners, generated site chrome, and unrelated page
  furniture from HTML pages
- preserve headings, code blocks, tables, examples, and API contracts
- do not rewrite requirements into AgileForge vocabulary during normalization
- hash `source/source.md`, not the live URL

The normalized source is the benchmark input given to spec-generation and review
workflows. Live URLs are retained only as provenance.

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
2. Human-reviewed gold structured spec JSON.
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

The benchmark verdict is separate from the AgileForge acceptance mutation.

Benchmark verdicts:

- `ACCEPT`: no high-severity blockers; authority is suitable for canonical
  acceptance.
- `ACCEPT_WITH_RISKS`: useful authority with known risks; record risks, but do
  not run `agileforge authority accept` on a canonical benchmark project unless
  the risks are explicitly non-blocking for downstream planning.
- `REJECT`: authority should be regenerated or the gold spec should be revised.
- `NEGATIVE_CONTROL`: intentionally accept or reject in a disposable project to
  test CLI behavior; never treat the resulting authority as benchmark-approved.

The benchmark may run `agileforge authority accept` only when:

- `agileforge authority review` reports no structural blockers
- the human reviewer has read the compiled authority
- high-severity blockers are absent
- source references are usable enough for practical audit
- downstream agents would receive enough guidance to start Vision/Backlog work

The benchmark should record `ACCEPT_WITH_RISKS` or `REJECT`, without canonical
acceptance mutation, when:

- multiple reviewers identify the same missing core behavior
- authority invents important constraints
- examples or sample values become global rules
- gaps or assumptions hide requirements that should be authority
- source references are too broad to audit
- the compiled authority would mislead downstream planning agents

## Success Criteria

The benchmark is successful when:

- all three fixtures have reproducible normalized `source/source.md` artifacts
- all three fixtures can be converted to `agileforge.spec.v1`
- all generated and gold specs validate structurally
- structured spec review produces either `generated_accepted_as_gold` or
  `gold_corrected`
- authority compilation produces reviewable artifacts
- external reviewers can assess authority without reading host internals
- the benchmark reveals actionable quality issues if authority is weak
- authority quality is judged from gold specs, not raw generated specs
- no benchmark step depends on deterministic prose extraction from source text
