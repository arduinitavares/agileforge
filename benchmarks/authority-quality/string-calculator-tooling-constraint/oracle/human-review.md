# String Calculator Tooling Constraint Authority Review

Verdict: gold_corrected

Expected judgment for `attempt-28-invalid`: `semantically_unacceptable`

- `CONSTRAINT.001` specifies a Python version and the uv project-management
  tool. It does not define a persisted or exchanged data shape.
- The invalid candidate invents the subject `project configuration and
  dependency management`; that phrase is absent from the source item.
- No current Authority invariant type faithfully represents the complete
  tooling requirement, so `CONSTRAINT.001` must become an exact item-ID gap.
- `CONSTRAINT.002` is different: it states a literal numeric maximum and maps
  faithfully to `MAX_VALUE` using exact source-grounded parameters.

This human-reviewed fixture is a provider-quality oracle only. It has no
runtime or lifecycle effect. Human review remains the final semantic decision.
