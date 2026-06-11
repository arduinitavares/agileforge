## 2026-05-06 - Native Form Validation
**Learning:** Replaced manual JS alerts with native HTML5 form validation by wrapping inputs in a `<form>` and adding `required`, `pattern=.*\S+.*`, and `title`. This improves keyboard accessibility (Enter to submit) and provides standard screen-reader friendly tooltips.
**Action:** Always wrap modal inputs in a `<form>` element and use native validation attributes before resorting to custom JS validation logic.
