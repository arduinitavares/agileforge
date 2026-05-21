# Gherkin Gold Spec Change Log

## 2026-05-21

- Added the first human-reviewed Gherkin gold structured spec for the
  authority-quality benchmark.
- Modeled concrete Example, Scenario, and Scenario Outline blocks as accepted
  `REQ.*` items rather than introducing a new AgileForge `SCENARIO.*` item type.
- Preserved Feature and Rule context as a SHOULD-level structural mapping
  constraint, and preserved localized keyword handling as a SHOULD-level tooling
  constraint.
- Added explicit coverage for Given/When/Then/And step intent, scenario outline
  examples tables, doc strings, and data tables.
