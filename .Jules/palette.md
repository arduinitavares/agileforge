## 2023-10-24 - Modal Form Validation
**Learning:** Modals with input fields in this app previously relied on manual JS `alert()` for validation and lacked `<form>` wrappers, preventing standard Enter-key submission and accessible native HTML5 validation tooltips.
**Action:** Always wrap modal inputs in a `<form>` element, use `pattern=".*\S+.*"` to prevent whitespace-only submissions, assign `type="button"` to cancel actions, and handle submission via `onsubmit` event to preserve native browser accessibility.
