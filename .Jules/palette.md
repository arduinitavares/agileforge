## 2025-01-20 - Adding Accessible Hint Text for Disabled Buttons
**Learning:** Adding a `title` attribute to natively `disabled` HTML buttons is an accessibility anti-pattern because disabled elements typically do not receive pointer events (hover) or keyboard focus, making the tooltip inaccessible to screen-reader and keyboard-only users.
**Action:** Use adjacent accessible hint text to explain disabled states instead.
