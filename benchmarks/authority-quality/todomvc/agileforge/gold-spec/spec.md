# TodoMVC Application Technical Spec

- Schema: agileforge.spec.v1
- Artifact id: SPEC.todomvc-app
- Status: accepted
- Version: 1.0
- Created: 2026-05-20
- Updated: 2026-05-20
- Markdown profile: agileforge.spec_markdown.v1

## Summary

Defines the TodoMVC application behavior, structure, dependencies, style constraints, persistence, and routing expected for framework examples.

## Problem Statement

TodoMVC examples need a consistent, framework-comparable implementation contract so each app looks and behaves like the template and other examples while still allowing framework best practices where the source permits them.

## Controlled Terms

### active todo

- Scope: artifact
- Definition: A todo whose completed value is false.

### completed todo

- Scope: artifact
- Definition: A todo whose completed value is true.

### editing mode

- Scope: artifact
- Definition: The item state activated by double-clicking a todo label, represented by the editing class on the item li.

## Items

### ASSUMPTION.source-is-complete - Source is complete for benchmark scope

- Type: ASSUMPTION
- Status: accepted
- Level: -
- Verification: -
- Tags: -

Statement:

The source document is treated as the full authority for this structured benchmark spec; linked external references provide context but do not add behavior not stated in the source.

Acceptance:

- None

### CONSTRAINT.browser-support - Supported browsers

- Type: CONSTRAINT
- Status: accepted
- Level: MUST
- Verification: manual-review
- Tags: compatibility

Statement:

The app must work in every browser supported by the TodoMVC project.

Acceptance:

- The implementation does not knowingly rely on features or dependencies that exclude a TodoMVC-supported browser.

### CONSTRAINT.code-style-rules - Hard code style rules

- Type: CONSTRAINT
- Status: accepted
- Level: MUST
- Verification: manual-review
- Tags: dependencies, style

Statement:

The implementation must follow the TodoMVC code style, use double-quotes in HTML, use single-quotes in JavaScript and CSS, use npm packages for third-party dependencies, manually remove third-party dependency files that are not required for the app to run, and use constants instead of direct keyCode values.

Acceptance:

- A reviewer can confirm that the implementation follows the TodoMVC code style.
- HTML attributes use double-quotes.
- JavaScript and CSS strings use single-quotes.
- Third-party dependencies are provided as npm packages.
- Third-party dependency files that are not required for the app to run are removed when dependency files are included with the app.
- Key codes are represented by named constants instead of direct numeric literals.

### CONSTRAINT.framework-best-practices - Framework best practices

- Type: CONSTRAINT
- Status: accepted
- Level: SHOULD
- Verification: manual-review
- Tags: structure

Statement:

The implementation should follow the selected framework's best practices for application structure, even when that differs from the recommended example file layout.

Acceptance:

- A reviewer can explain any structural deviation from the recommended TodoMVC layout in terms of the selected framework's best practices.

### CONSTRAINT.html-css-js-style - Template style guidance

- Type: CONSTRAINT
- Status: accepted
- Level: SHOULD
- Verification: manual-review
- Tags: style, template

Statement:

The implementation should keep HTML as close to the template as possible, reference base.css from the assets folder without touching it, use app.css for minimal style changes, update relative paths when using the template, and avoid preprocessors to reach the largest audience.

Acceptance:

- A reviewer can compare the app HTML with the template and identify any material divergence.
- Finished HTML has template comments removed unless a reviewer records a framework-specific reason.
- base.css is referenced from assets and is not modified by the app unless a reviewer records a framework-specific reason.
- Style changes, if any, are in app.css and are limited to changes needed by the implementation.
- Relative paths copied from the template resolve correctly.
- The implementation avoids Sass, CoffeeScript, or other preprocessors unless a reviewer records a framework-specific reason.

### CONSTRAINT.template-base - Template base

- Type: CONSTRAINT
- Status: accepted
- Level: SHOULD
- Verification: manual-review
- Tags: fidelity, template

Statement:

The implementation should use the TodoMVC app template as the base and should keep the app looking and behaving exactly like the template and other examples to support framework comparison.

Acceptance:

- A reviewer can identify whether the implementation is based on the TodoMVC app template.
- A reviewer comparing the app to the template and other examples can identify any visual or behavioral divergence and whether the divergence is required by the chosen framework.

### DATA.editing-state - Editing state persistence

- Type: DATA
- Status: accepted
- Level: MUST_NOT
- Verification: acceptance-test
- Tags: editing, persistence

Statement:

Editing mode must not be persisted.

Acceptance:

- Given a todo is in editing mode, when the app persists and reloads, then the todo is not restored in editing mode.

### DATA.localstorage-key - localStorage key

- Type: DATA
- Status: accepted
- Level: SHOULD
- Verification: inspection
- Tags: data, persistence

Statement:

The localStorage name should use the format todos-[framework].

Acceptance:

- The localStorage key used for todos follows the todos-[framework] format.

### DATA.todo-record - Todo record fields

- Type: DATA
- Status: accepted
- Level: SHOULD
- Verification: inspection
- Tags: data, persistence

Statement:

When possible, each persisted todo item should use the keys id, title, and completed.

Acceptance:

- Persisted todo records use id, title, and completed keys when the implementation can do so.

### EXAMPLE.counter-text - Counter text

- Type: EXAMPLE
- Status: accepted
- Level: -
- Verification: -
- Tags: -

Statement:

With two active todos, the counter can render as a strong-wrapped 2 followed by items left.

Acceptance:

- None

### EXAMPLE.package-json - package.json dependency example

- Type: EXAMPLE
- Status: accepted
- Level: -
- Verification: -
- Tags: -

Statement:

A package.json may be private and include framework dependencies alongside todomvc-app-css and todomvc-common.

Acceptance:

- None

### EXAMPLE.recommended-directory-structure - Recommended directory structure

- Type: EXAMPLE
- Status: accepted
- Level: -
- Verification: -
- Tags: -

Statement:

A recommended structure includes index.html, package.json, node_modules, css/app.css, js/app.js, framework-appropriate controllers and models folders, and readme.md.

Acceptance:

- None

### GOAL.consistent-todomvc-example - Consistent TodoMVC example

- Type: GOAL
- Status: accepted
- Level: -
- Verification: -
- Tags: -

Statement:

The app should be easy to compare with other TodoMVC examples by following the shared template and matching expected TodoMVC behavior.

Acceptance:

- None

### GOAL.framework-appropriate-implementation - Framework-appropriate implementation

- Type: GOAL
- Status: accepted
- Level: -
- Verification: -
- Tags: -

Statement:

The app should follow the selected framework's best practices when they affect structure, dependency management, persistence, or routing and do not conflict with the TodoMVC source requirements.

Acceptance:

- None

### NON_GOAL.customized-visual-design - Customized visual design

- Type: NON_GOAL
- Status: accepted
- Level: -
- Verification: -
- Tags: -

Statement:

The app is not intended to introduce a distinct visual design beyond minimal app.css changes needed for the implementation.

Acceptance:

- None

### NON_GOAL.preprocessor-based-source - Stricter preprocessor prohibition

- Type: NON_GOAL
- Status: accepted
- Level: -
- Verification: -
- Tags: -

Statement:

The gold spec is not intended to add a stricter preprocessor prohibition than the source's SHOULD-level guidance to write apps without preprocessors.

Acceptance:

- None

### REQ.clear-completed - Clear completed

- Type: REQ
- Status: accepted
- Level: MUST
- Verification: acceptance-test
- Tags: completion

Statement:

The Clear completed button must remove completed todos when clicked and must be hidden when there are no completed todos.

Acceptance:

- Given at least one todo is completed, when Clear completed is clicked, then completed todos are removed.
- Given there are no completed todos, the Clear completed button is hidden.

### REQ.component-organization - Component organization

- Type: REQ
- Status: accepted
- Level: SHOULD
- Verification: inspection
- Tags: components, structure

Statement:

Components should be split up into separate files and placed into folders where it makes the most sense, while keeping the selected framework's best practices for application structure first.

Acceptance:

- Components that represent separable application responsibilities are placed in separate files unless the selected framework's best practices call for a different structure.
- Component files are placed in folders that match their application role or the selected framework's preferred organization.
- A reviewer can trace any decision not to split a component into a separate file to the selected framework's best practices or to the component not being meaningfully separable.

### REQ.counter - Active todo counter

- Type: REQ
- Status: accepted
- Level: MUST
- Verification: acceptance-test
- Tags: counter

Statement:

The counter must display the number of active todos, wrap the number in a strong tag, and pluralize item correctly for zero, one, and multiple active todos.

Acceptance:

- When there are zero active todos, the counter text uses items.
- When there is one active todo, the counter text uses item.
- When there are two or more active todos, the counter text uses items.
- The numeric active count is wrapped in a strong tag.

### REQ.dependency-management - Dependency management

- Type: REQ
- Status: accepted
- Level: MUST
- Verification: inspection
- Tags: dependencies

Statement:

Unless it conflicts with the project's best practices, the example should use npm package management with package.json in the app root and must include todomvc-common and todomvc-app-css as dependencies.

Acceptance:

- If npm does not conflict with project best practices, package.json exists in the app root.
- package.json includes todomvc-common as a dependency.
- package.json includes todomvc-app-css as a dependency.
- Third-party dependencies are provided as npm packages.

### REQ.editing - Editing behavior

- Type: REQ
- Status: accepted
- Level: MUST
- Verification: acceptance-test
- Tags: editing

Statement:

Editing mode must hide the other controls, focus an input containing the todo title, save non-empty trimmed edits on blur or Enter, destroy the todo on an empty trimmed edit, remove the editing class after saving, and discard changes when Escape is pressed.

Acceptance:

- When editing mode activates, the other item controls are hidden.
- When editing mode activates, an input containing the todo title is focused.
- Given an edit input contains non-empty text after trimming, when it blurs, then the todo title is saved and the editing class is removed.
- Given an edit input contains non-empty text after trimming, when Enter is pressed, then the todo title is saved and the editing class is removed.
- Given an edit input is empty after trimming, when the edit is saved, then the todo is destroyed.
- Given the edit input has unsaved changes, when Escape is pressed, then editing mode exits and the changes are discarded.

### REQ.empty-state-visibility - Empty state visibility

- Type: REQ
- Status: accepted
- Level: SHOULD
- Verification: acceptance-test
- Tags: visibility

Statement:

When there are no todos, #main and #footer should be hidden.

Acceptance:

- Given the todo list is empty, #main is hidden.
- Given the todo list is empty, #footer is hidden.

### REQ.filtered-state - Filtered state behavior

- Type: REQ
- Status: accepted
- Level: MUST
- Verification: acceptance-test
- Tags: filtering, routing

Statement:

When the route changes, the todo list must be filtered at the model level, the selected class on filter links must be toggled, item updates in a filtered state must update visibility accordingly, and the active filter must persist on reload.

Acceptance:

- When the route changes to all, active, or completed, the list is filtered at the model level.
- When the route changes, the selected class is applied to the active filter link and removed from inactive filter links.
- Given the active filter is selected, when an active todo is checked complete, then it is hidden from the active list.
- After reload, the previously active filter is restored.

### REQ.item-interactions - Todo item interactions

- Type: REQ
- Status: accepted
- Level: MUST
- Verification: acceptance-test
- Tags: interaction, item

Statement:

Each todo item must support checkbox completion, label double-click editing activation, and hover-revealed removal.

Acceptance:

- Clicking a todo checkbox updates the todo completed value.
- Clicking a todo checkbox toggles the completed class on the parent li.
- Double-clicking a todo label toggles the editing class on the parent li.
- Hovering over a todo item shows the .destroy remove button.

### REQ.new-todo - New todo

- Type: REQ
- Status: accepted
- Level: MUST
- Verification: acceptance-test
- Tags: creation, input

Statement:

The top input must be focused when the page loads, pressing Enter in that input must create a todo from non-empty trimmed text, append it to the todo list, and clear the input.

Acceptance:

- When the page loads, the new-todo input is focused.
- Given the top input contains text with surrounding whitespace, when Enter is pressed, then a todo is created using the trimmed title.
- Given the top input is empty after trimming, when Enter is pressed, then no todo is created.
- After a todo is created, it is appended to the todo list and the top input is cleared.

### REQ.node-modules-pruning - Node modules pruning

- Type: REQ
- Status: accepted
- Level: SHOULD
- Verification: inspection
- Tags: dependencies

Statement:

The example should gitignore everything in node_modules except files actually used by the example, and documentation, READMEs, and tests from dependencies should not be included in the pull request.

Acceptance:

- node_modules content that is not required for the example to run is excluded.
- Dependency documentation, README files, and tests are not included in the submitted example unless required for runtime.

### REQ.persistence - Todo persistence

- Type: REQ
- Status: accepted
- Level: MUST
- Verification: acceptance-test
- Tags: persistence

Statement:

The app must dynamically persist todos to localStorage, using framework persistence capabilities when available and otherwise using vanilla localStorage.

Acceptance:

- When todos change, the changed todo data is persisted to localStorage.
- After reload, persisted todos are restored.
- If the framework provides persistence capabilities, the implementation uses them.
- If the framework does not provide persistence capabilities, the implementation uses vanilla localStorage.

### REQ.readme - README

- Type: REQ
- Status: accepted
- Level: MUST
- Verification: inspection
- Tags: documentation

Statement:

The example must include a README describing the framework, the general implementation, and the build process when a build process is required.

Acceptance:

- The app includes a README file.
- The README names and describes the framework.
- The README summarizes the general implementation.
- When the app requires a build process, the README describes that process.

### REQ.routing - Routing and filtering

- Type: REQ
- Status: accepted
- Level: MUST
- Verification: acceptance-test
- Tags: filtering, routing

Statement:

Routing is required and must support all, active, and completed routes; use framework routing when supported and otherwise use the Flatiron Director routing library located in the assets folder.

Acceptance:

- The app supports #/ as the all/default route.
- The app supports #/active as the active route.
- The app supports #/completed as the completed route.
- The equivalent #!/ route style is accepted where implemented.
- If the framework supports built-in routing, the implementation uses it.
- If the framework does not support built-in routing, the implementation uses the Flatiron Director routing library from the assets folder.

### REQ.toggle-all - Mark all as complete

- Type: REQ
- Status: accepted
- Level: MUST
- Verification: acceptance-test
- Tags: completion

Statement:

The mark-all checkbox must toggle all todos to the same completed state as itself, clear its checked state after completed todos are cleared, and reflect single-item completed state changes.

Acceptance:

- When the mark-all checkbox is checked, all todos become completed.
- When the mark-all checkbox is unchecked, all todos become active.
- After the Clear completed button removes completed todos, the mark-all checkbox is not checked.
- When every individual todo is completed, the mark-all checkbox is checked.
- When at least one individual todo is active, the mark-all checkbox is not checked.

## Relations

- CONSTRAINT.code-style-rules constrains GOAL.consistent-todomvc-example
- CONSTRAINT.framework-best-practices satisfies GOAL.framework-appropriate-implementation
- CONSTRAINT.html-css-js-style constrains GOAL.consistent-todomvc-example
- CONSTRAINT.template-base satisfies GOAL.consistent-todomvc-example
- DATA.editing-state constrains REQ.persistence
- REQ.component-organization depends_on CONSTRAINT.framework-best-practices
- REQ.dependency-management depends_on CONSTRAINT.framework-best-practices
- REQ.editing satisfies GOAL.consistent-todomvc-example
- REQ.filtered-state depends_on REQ.routing
- REQ.item-interactions satisfies GOAL.consistent-todomvc-example
- REQ.new-todo satisfies GOAL.consistent-todomvc-example
- REQ.persistence depends_on DATA.localstorage-key
- REQ.persistence depends_on DATA.todo-record
- REQ.persistence satisfies GOAL.consistent-todomvc-example
- REQ.routing satisfies GOAL.consistent-todomvc-example

<!-- agileforge-review-notes:start -->
<!-- agileforge-review-notes:end -->
