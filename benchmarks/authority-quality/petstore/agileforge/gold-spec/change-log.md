# Petstore Gold Spec Change Log

## 2026-05-21

- Added the first human-reviewed Petstore gold structured spec for the
  authority-quality benchmark.
- Preserved the reviewed benchmark concepts as explicit typed item IDs covering
  the three operations, the `limit` maximum, the required `petId` path
  parameter, the `Pet`, `Pets`, and `Error` schemas, and expected success and
  default error responses.
- Scoped the fixture to compact API-contract semantics and recorded full
  Swagger Petstore coverage as a non-goal.
