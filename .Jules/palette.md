## 2026-04-17 - Added tooltip to dynamically disabled buttons
**Learning:** Dynamically disabled buttons without native tooltips lose context for screen readers and mouse users. The UI hint explaining why a button is disabled must also be applied to its native `title` attribute to ensure the explanation is universally accessible.
**Action:** Ensure any dynamically updated disabled state sets `button.title` to the explanation text, not just visually updating an adjacent element.
