## 2024-04-16 - Add explanatory tooltips to disabled buttons
**Learning:** For dynamically disabled buttons (e.g., state-management functions in frontend/project.js), applying explanatory text solely to an adjacent visual hint element isn't enough; we need to add the native 'title' attribute to the button itself for better accessibility.
**Action:** Always map the text from the adjacent hint element to the button's native title attribute for disabled state buttons.
