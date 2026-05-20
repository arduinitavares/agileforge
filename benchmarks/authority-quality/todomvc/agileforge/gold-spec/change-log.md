# TodoMVC Gold Spec Change Log

## 2026-05-20

- Gold spec corrected after review found that the generated spec demoted the source component-splitting SHOULD requirement to a non-normative example.
- Added `REQ.component-organization` to the gold spec with SHOULD level, inspection verification, and concrete acceptance criteria that preserve the source requirement and framework-best-practices nuance.
- Removed the misleading `EXAMPLE.split-components` item from the gold spec.
- Corrected source modality for template and style constraints: `CONSTRAINT.template-base` is now SHOULD, `CONSTRAINT.html-css-js-style` is SHOULD-level template/style guidance, and hard code-style rules moved to `CONSTRAINT.code-style-rules` with MUST level.
- Softened `NON_GOAL.preprocessor-based-source` so the gold spec does not impose a stricter preprocessor prohibition than the source's SHOULD guidance.
