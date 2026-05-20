# TodoMVC Structured Spec Review

Verdict: gold_corrected

## Source-To-Spec Findings

- The source says, "Components should be split up into separate files and placed into folders where it makes the most sense." The generated spec represented this only as `EXAMPLE.split-components` with "may be split", which lost the SHOULD-level component organization requirement.
- The generated and initial gold specs over-promoted several source SHOULD or soft constraints into MUST requirements. In particular, the source says the TodoMVC template "should be used" as the base, asks implementers to "try to keep the HTML as close to the template as possible", and says apps "should be written without any preprocessors"; these were represented as blanket MUST constraints.

## Corrections Required For Gold Spec

- Gold spec adds `REQ.component-organization` with `level: SHOULD`, `verification: inspection`, and acceptance criteria preserving the component-splitting requirement and the framework-best-practices nuance. The misleading gold `EXAMPLE.split-components` item is removed.
- Gold spec changes `CONSTRAINT.template-base` to `level: SHOULD`, refactors `CONSTRAINT.html-css-js-style` into SHOULD-level template/style guidance, adds `CONSTRAINT.code-style-rules` for the hard code-style rules, and softens `NON_GOAL.preprocessor-based-source` so it does not create a stricter preprocessor prohibition than the source.
