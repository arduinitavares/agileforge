# TodoMVC Structured Spec Review

Verdict: gold_corrected

## Source-To-Spec Findings

- The source says, "Components should be split up into separate files and placed into folders where it makes the most sense." The generated spec represented this only as `EXAMPLE.split-components` with "may be split", which lost the SHOULD-level component organization requirement.

## Corrections Required For Gold Spec

- Gold spec adds `REQ.component-organization` with `level: SHOULD`, `verification: inspection`, and acceptance criteria preserving the component-splitting requirement and the framework-best-practices nuance. The misleading gold `EXAMPLE.split-components` item is removed.
