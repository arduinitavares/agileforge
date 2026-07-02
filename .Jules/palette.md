## 2025-01-20 - HTML5 Form Validation
**Learning:** Manual JS alert validations on inputs within a custom `div` modal create an inaccessible, disjointed user experience. Users cannot use the Enter key to submit, and manual alerts interrupt the flow.
**Action:** Always wrap modal inputs within a `<form>` element. Use native HTML5 attributes like `required`, `pattern=".*\S+.*"`, and `title` to provide contextual, accessible tooltip validation, and ensure native Enter-key submission by utilizing `onsubmit` and setting appropriate button types (`type="submit"` and `type="button"`).
