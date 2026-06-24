## 2026-04-21 - Modal Form Submission and Validation UX
**Learning:** In this application's design system, using generic `div` elements for modals bypasses standard Enter-key submission and native HTML5 form validation, leading to degraded UX and manual `alert()` popups for required fields.
**Action:** Always wrap modal input fields in a semantic `<form onsubmit="event.preventDefault(); handler();">`, set the submit button to `type="submit"`, and use native HTML5 validation attributes (like `required` and `pattern`) to provide accessible, consistent user feedback.
