
## 2026-04-24 - Use Native Forms for Modal Submission
**Learning:** Custom 'onclick' validation for modals bypasses standard HTML5 validation features (like tooltips for required fields and regex patterns). By using a proper `<form>` tag with an `onsubmit` handler and `event.preventDefault()`, you preserve native UX accessibility, standard Enter-key submission, and validation tooltips.
**Action:** When creating or updating modal dialogs with input fields, always wrap the content within a `<form>` element, ensure the submit button has `type="submit"`, and use an `onsubmit` handler instead of an `onclick` handler on the button.
