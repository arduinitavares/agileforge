## 2026-05-13 - Form Wrapper and Native Validation
**Learning:** Manual JS alert-based validation for modals breaks keyboard accessibility (Enter key to submit doesn't work out of the box) and creates a jarring UX. Native HTML5 form validation is preserved only if modal inputs are properly wrapped in a `<form>` element.
**Action:** Always wrap modal inputs in a `<form>` tag with `onsubmit` preventing default behavior, use `type="submit"` for primary buttons, and `type="button"` for cancels to preserve accessibility, Enter key submission, and native browser tooltips.
