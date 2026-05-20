# Authority Quality External Review Prompt

You are reviewing an AgileForge compiled authority artifact against its source
technical specification.

Context:

- AgileForge compiles a structured technical spec into a compact authority
  artifact.
- The authority artifact will guide later Vision, Backlog, Story, Sprint, and
  implementation agents.
- The host system should not use deterministic prose extraction as the semantic
  judge.
- Your job is semantic review: assess whether the compiled authority is
  faithful, useful, and safe enough for downstream planning.
- Do not demand perfection. The goal is practical project guidance, not formal
  verification.
- Do not recommend deterministic requirement extraction from prose as the
  solution.

Inputs I will provide:

1. Source spec as rendered Markdown.
2. Human-reviewed gold structured spec JSON.
3. Compiled authority artifact JSON.
4. Sanitized authority review summary JSON.

## Review Tasks

1. Assess whether the compiled authority captures the core requirements of the
   source spec.
2. Identify important missing requirements, behaviors, constraints, data
   contracts, API contracts, or acceptance criteria.
3. Identify any authority items that distort the spec, overgeneralize examples,
   or turn sample values into universal rules.
4. Check whether assumptions and gaps are honest and useful, or whether they
   hide missing authority.
5. Check whether source references are specific and useful enough for a human
   reviewer.
6. Assess whether Vision/Backlog/Story/Sprint agents would receive enough
   guidance to continue safely.
7. Distinguish high-severity blockers from normal improvement suggestions.
8. Recommend whether a human should accept the authority, reject it, or accept
   it with noted risks.

## Output Format

### Verdict

Choose one:

- ACCEPT
- ACCEPT_WITH_RISKS
- REJECT

### High-Severity Blockers

List only issues that should block authority acceptance. If none, say `None`.

### Medium/Low Findings

List useful improvements that should not necessarily block acceptance.

### Missing or Weak Authority Coverage

List major source requirements or behaviors that are missing, weak, or only
indirectly represented.

### Over-Promoted or Distorted Authority

List authority items that appear to overfit examples, invent rules, or
misclassify guidance.

### Assumptions and Gaps

Assess whether assumptions and gaps are accurate, honest, and actionable.

### Source Reference Quality

Assess whether source references are specific enough to audit.

### Downstream Readiness

Explain whether Vision, Backlog, Story, Sprint, and implementation agents can
proceed with this authority.

### Scores

- Completeness:
- Fidelity:
- Downstream Utility:
- Reviewability:
- Acceptance Confidence:

### Final Recommendation

Give a concise recommendation for the human reviewer.
