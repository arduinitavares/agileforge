## 2024-05-24 - HTML5 Native Form Validation in Modals
**Learning:** Adding a `form` tag to wrap modal content, mapping the primary action to a `type="submit"` button, and setting `onsubmit="function(event)"` preserves native HTML5 input validation tooltips and standard Enter-key submission without custom JS `alert()` validation.
**Action:** Always wrap modal inputs in a `<form>` tag and use standard HTML form validation (`required`, `pattern`, `title`) rather than manual JS validations to ensure a robust, accessible experience.
