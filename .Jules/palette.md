
## 2024-04-20 - Ensure Modal Inputs Are Wrappped in Semantically Correct Forms
**Learning:** Generic div-based layouts for modals containing input fields bypass native browser accessibility validations (like `required` tooltips) and break standard interaction patterns such as Enter-key submission.
**Action:** Always wrap input fields within a `<form>` element, even inside modal dialogs, and ensure the submit button has `type="submit"` instead of relying solely on click handlers. Let native HTML5 validation handle required field tooltips instead of creating custom `alert()`s.
