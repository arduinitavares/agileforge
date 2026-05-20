# Authority Quality Benchmark

This benchmark evaluates AgileForge compiled authority quality without
reintroducing deterministic prose extraction as a semantic judge.

Each fixture has two tracks:

1. Source-to-structured-spec generation quality.
2. Gold-structured-spec-to-authority compilation quality.

Authority quality is judged only from the human-reviewed gold structured spec.
The raw generated spec is preserved separately so failures can be attributed to
the right stage.

## Fixtures

- `todomvc`: small product behavior specification.
- `petstore`: small OpenAPI/API-contract specification.
- `gherkin`: scenario and acceptance-criteria specification.

## Artifact Rules

- Commit normalized source artifacts, human-reviewed gold specs, compiled
  authority, sanitized review summaries, review reports, and synthesis.
- Do not commit raw CLI envelopes, guard tokens, project IDs, idempotency keys,
  or raw LLM response payloads.
- Keep oracle notes out of generator, compiler, and external reviewer prompts.

## Local Run Artifacts

Raw CLI outputs belong under:

```text
.agileforge/benchmark-runs/authority-quality/<fixture>/<run-id>/
```
