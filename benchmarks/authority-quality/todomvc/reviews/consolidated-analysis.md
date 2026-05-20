# TodoMVC Authority Quality — External Review (Consolidated Synthesis)

**Verdict**: **REJECT**

This review represents a consolidated synthesis of human feedback and automated models (Gemini, Claude Opus, and GPT). It identifies a **semantic failure despite structural `accept_ready`**. While the compiled authority is not entirely devoid of signal (correctly identifying high-level themes, editing-state non-persistence, and general constraints), it suffers from **unsafe compression**, severe behavioral omissions, scrambled references, and modality over-promotion.

---

## Verdict

**REJECT**

The compiled authority is not ready to guide downstream agents. If treated as the primary planning input, it will lead to non-conforming, broken skeletal implementations. Downstream Story, Sprint, and Implementation agents would remain blind to the actual user interactions and state transitions required for a compliant TodoMVC application.

---

## High-Severity Blockers

1. **Unsafe Behavior Compression (The "Too Shallow" Critic)**:
   The authority compresses detailed, state-driven user interactions and database persistence rules into trivial `REQUIRED_FIELD` existence assertions (e.g., `README`, `routing`, `localStorage`, `new-todo input`). 
   * A Story or Implementation agent working only from this authority would have zero guidance on:
     * **Todo Creation**: Trimming input, Enter-key creation, clearing input, and preventing empty creations.
     * **Todo Lifecycle**: Checkbox toggling, class syncing (`.completed`), and hover-reveal of the `.destroy` button.
     * **Editing Mode**: Focus on activation, save-on-blur/Enter, escape-to-discard, and empty-input destruction.
     * **Counter Behavior**: Displaying exact active counts wrapped in `<strong>` and pluralizing "item"/"items" correctly.
     * **Persistence Shape**: Key name format (`todos-[framework]`), and field contracts (`id`, `title`, `completed`).
     * **Routing Filters**: Model-level filtering on hash changes and selected state filter links.

2. **Scrambled Source References (Cross-Wired Provenance)**:
   Several invariants are cross-wired, linking correct behavioral text to semantically unrelated source requirements. This is a severe compiler bug that blocks auditability:
   * **`INV-be7b91ef55f0d4bf`**: Cites browser support (`CONSTRAINT.browser-support`) as the source for empty-state DOM visibility (`#main hidden when todo list empty`).
   * **`INV-28d0075a5a3994f1`**: Cites clear-completed button behavior (`REQ.clear-completed`) as the source for filter-link class toggling (`selected class on filter link <= route state`).
   * **`INV-ac2c6fae40d791c8`**: Cites model-level route filtering (`REQ.filtered-state`) as the source for active todo count limits (`active count <= number of active todos`).

3. **Modality Over-Promotion (SHOULD to MUST)**:
   * **`INV-f036fa9d50476c33` & `INV-0368a4c1626c33dc`**: Promote a `SHOULD`-level preprocessor style guideline (`CONSTRAINT.html-css-js-style`) into hard `FORBIDDEN_CAPABILITY` invariants for Sass and CoffeeScript. This violates the compiler's directive to only issue absolute bans for explicit forbids in the specification.

4. **False Positive Safety (`accept_ready` Optimism)**:
   * The compiler review summary reports `accept_ready` with zero blocking findings because it only evaluates structural integrity and host validation, failing to perform any item-level semantic adequacy checks.

---

## Medium/Low Findings

### Medium
1. **Example-to-Requirement Derivation**:
   * **`INV-a959ee3b23ba81ca`** derives `REQUIRED_FIELD:field_name=package.json` from `EXAMPLE.package-json.title` instead of the actual normative requirement `REQ.dependency-management`.
2. **Conditional Fallback Flattening**:
   * **`INV-b9930a143d0d0ac3`** flattens a conditional requirement—using vanilla `localStorage` only as a fallback when the framework lacks native persistence—into a flat `REQUIRED_FIELD` invariant.
3. **Silent Omission of Gold Corrections**:
   * Critical structural corrections in the gold spec, such as splitting component organization (`REQ.component-organization`) and enforcing hard code-style rules (double quotes in HTML, single quotes in JS/CSS, Named keyCode constants), are silently dropped with no gaps recorded.

---

## Missing or Weak Authority Coverage

| Gold Spec Item | Severity in Gold | Authority Coverage | Status |
|---|---|---|---|
| `REQ.component-organization` | SHOULD | None | **Missing** |
| `CONSTRAINT.code-style-rules` | MUST | None (except preprocessor bans) | **Missing** |
| `REQ.new-todo` | MUST | Existence of input only | **Weak** |
| `REQ.toggle-all` | MUST | Existence of checkbox only | **Weak** |
| `REQ.item-interactions` | MUST | None | **Missing** |
| `REQ.editing` | MUST | Persistence ban only; no interaction contracts | **Weak** |
| `REQ.counter` | MUST | RELATIONAL_CONSTRAINT only; no strong/pluralization | **Weak** |
| `REQ.persistence` | MUST | Existence of storage only; no sync/shape rules | **Weak** |
| `REQ.routing` | MUST | Existence only; no route specs | **Weak** |
| `DATA.todo-record` | SHOULD | None | **Missing** |
| `DATA.localstorage-key` | SHOULD | None | **Missing** |

---

## Over-Promoted or Distorted Authority

* **Preprocessor Bans (`Sass`, `CoffeeScript`)**: Promoted from SHOULD to MUST.
* **`package.json` Requirement**: Sourced from an illustrative `EXAMPLE` block.
* **Scrambled Mappings**: The cross-wiring in `INV-be7b91ef55f0d4bf`, `INV-28d0075a5a3994f1`, and `INV-ac2c6fae40d791c8` severely distorts the review trail.

---

## Assumptions and Gaps

* **Assumptions (`ASM-1`, `ASM-2`)**: Mathematically clean and contextually honest.
* **Gaps (`GAP-1` to `GAP-3`)**: Reasonable, but structurally insufficient. By focusing on framework flexibility and numeric limits, the gap list effectively hides the massive omission of explicit, core behavioral requirements.

---

## Source Reference Quality

**Poor**. Auditing is highly frustrating. The source maps pair correct semantic assertions with completely incorrect item IDs and excerpts. Human auditors must manually re-verify all invariants against the gold spec to identify scrambled mappings.

---

## Downstream Readiness

**NOT READY**. Downstream Story and Implementation agents will produce broken, non-conforming skeletons if they rely solely on this authority artifact.

---

## Scores

* **Completeness**: 2/10
* **Fidelity**: 3/10
* **Downstream Utility**: 2/10
* **Reviewability**: 2/10
* **Acceptance Confidence**: 1/10

---

## Likely Root Causes

1. **Narrow Invariant Vocabulary**:
   The compiler's target schemas (`REQUIRED_FIELD`, `FORBIDDEN_CAPABILITY`, `MAX_VALUE`, `RELATION_CONSTRAINT`) are designed for static data constraints. They are incapable of expressing state-driven user interactions, event handlers, and data transformations, causing the compiler to squeeze complex behaviors into bad structural shapes.
2. **Weak Source-Map Validator**:
   The validator (e.g. `normalizer.py`) checks simple token existence or proximity, failing to enforce semantic and conceptual alignment between the invariant text and the referenced source items.
3. **Superficial Gating**:
   `accept_ready` status in `authority_review.py` is calculated purely from structural/host findings, leaving the system blind to severe semantic coverage loss.

---

## Best Improvements for the Compiler System

1. **Expand Invariant Schema Vocabulary**:
   Introduce behavioral types to model interactive applications accurately:
   * `USER_INTERACTION` (e.g., double-click, hover, focus)
   * `STATE_TRANSITION` (e.g., save-on-blur, escape-to-discard, delete-on-empty)
   * `DATA_CONTRACT` (e.g., record shapes, keys, naming rules)
   * `ROUTE_CONTRACT` (e.g., hash schemas, filtering logic)
   * `VISIBILITY_RULE` (e.g., toggle `#main` and `#footer` on empty state)

2. **Preserve Metadata Hierarchy**:
   Enforce trace retention for every authority item:
   * `source_item_id`, `source_kind`, `source_level` (MUST/SHOULD/MAY), and `authority_strength`. Prevent the silent promotion of `SHOULD` to a hard invariant.

3. **Establish Coverage Enforcement**:
   For every `MUST`-level spec item, the compiler must prove it is either:
   * Covered by an explicit behavioral invariant.
   * Explicitly declared in the `gaps` block.
   * Formally flagged as "Requires structured spec detail".
   * *Missing MUST-level coverage must block `accept_ready`.*

4. **Add Context-Aware Source-Map Validation**:
   Build semantic validation into the compiler's gating suite. If the semantic distance between the mapped spec item (e.g., browser-support) and the generated invariant text (e.g., `#main visibility`) is too large, the compilation must fail.

5. **Introduce TodoMVC Regression Assertions**:
   Add test assertions specifically for the TodoMVC benchmark (covering trim, save, escape, pluralization, and route-filtering) to verify semantic fidelity before declaring the artifact ready for downstream consumption.
