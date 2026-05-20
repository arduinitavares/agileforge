# TodoMVC Authority Quality — External Review

**Reviewer**: Claude Opus 4.6 (Thinking)
**Date**: 2026-05-20
**Inputs reviewed**:

| Artifact | Path |
|---|---|
| Source spec | `source/source.md` |
| Gold structured spec | `agileforge/gold-spec/spec.json` |
| Compiled authority | `agileforge/compiled-authority.json` |
| Review summary | `agileforge/review-summary.json` |
| Gold spec change log | `agileforge/gold-spec/change-log.md` |
| Generated spec | `agileforge/generated-spec/spec.json` |
| Structured spec review | `agileforge/generated-spec/structured-spec-review.md` |
| Run manifest | `agileforge/run-manifest.json` |

---

## Verdict

**ACCEPT_WITH_RISKS**

The compiled authority captures the highest-priority behavioral invariants from
the source spec and provides honest assumptions and gaps. However, it loses a
significant amount of behavioral and structural guidance present in both the
source spec and the gold spec due to the narrow invariant vocabulary it employs
(`REQUIRED_FIELD`, `FORBIDDEN_CAPABILITY`, `RELATION_CONSTRAINT`). Downstream
agents can proceed but will need to consult the structured spec directly for
most acceptance criteria and behavioral detail.

---

## High-Severity Blockers

None that should block acceptance outright, but two issues come close:

1. **Invariant vocabulary is too narrow for behavioral requirements.** The
   authority expresses 14 invariants, but 6 of them are `REQUIRED_FIELD` items
   (README, package.json, new-todo input, mark-all checkbox, localStorage,
   routing). These assert existence of named concepts, not their behavioral
   contracts. The gold spec has ~25 behavioral acceptance criteria across
   `REQ.new-todo`, `REQ.editing`, `REQ.toggle-all`, `REQ.item-interactions`,
   `REQ.counter`, `REQ.clear-completed`, `REQ.filtered-state`, and
   `REQ.persistence` — almost none of which survive into the authority as
   testable behavioral statements. A Vision or Story agent reading only this
   authority would not know that pressing Escape during editing discards
   changes, that empty trimmed edits destroy the todo, or that the counter must
   pluralize "item"/"items".

2. **Cross-wired source excerpts on some RELATION_CONSTRAINT invariants.** Two
   invariants have source excerpts that do not match the invariant text they
   support:
   - `INV-be7b91ef55f0d4bf` cites "The app must work in every browser
     supported by the TodoMVC project" (`CONSTRAINT.browser-support`) but its
     invariant text is about `#main hidden when todo list empty`. The browser
     support constraint is semantically unrelated to empty-state visibility.
   - `INV-28d0075a5a3994f1` cites `REQ.clear-completed` (clear completed
     button behavior) but its invariant text is about `selected class on filter
     link <= route state`, which is a filtered-state concern from
     `REQ.filtered-state`.

   These misattributions are confusing for audit but do not introduce incorrect
   behavioral requirements — they link the right behavioral statement to the
   wrong source section.

---

## Medium/Low Findings

### Medium

1. **REQUIRED_FIELD for package.json derived from EXAMPLE, not REQ.** Invariant
   `INV-a959ee3b23ba81ca` derives its `REQUIRED_FIELD:field_name=package.json`
   from `EXAMPLE.package-json.title` rather than from
   `REQ.dependency-management`, which is the actual normative source. The
   example is illustrative; the requirement is what mandates `package.json`.

2. **Missing `REQ.component-organization` entirely.** The gold spec explicitly
   added `REQ.component-organization` (a SHOULD-level requirement for splitting
   components into separate files) after finding the generated spec had demoted
   it to an example. The compiled authority contains no invariant, gap, or
   assumption referencing component organization. This is a conscious gold-spec
   correction that the authority silently drops.

3. **Missing `CONSTRAINT.code-style-rules`.** The gold spec splits the
   generated spec's over-broad `CONSTRAINT.html-css-js-style` into two items:
   SHOULD-level template style guidance and MUST-level code style rules. The
   authority captures the preprocessor prohibition via `FORBIDDEN_CAPABILITY`
   but does not capture the MUST-level code style requirements (double-quotes
   in HTML, single-quotes in JS/CSS, named constants for keyCodes, npm for
   third-party deps).

4. **Modality drift on `CONSTRAINT.template-base`.** The gold spec corrects
   `CONSTRAINT.template-base` to SHOULD level (matching the source's "should
   be used"). The generated spec (which the authority compiler consumed) has it
   as MUST. The authority doesn't record requirement levels, so this modality
   over-promotion is invisible but latent. Downstream agents working from the
   generated spec would treat it as MUST.

### Low

5. **`eligible_feature_rules` and `rejected_features` are empty.** This is
   expected given the source spec's narrow scope but should be noted for
   completeness — no feature gating logic was extracted.

6. **Scope themes are reasonable** but purely descriptive. They align well with
   the source's major sections and would help a Vision agent orient.

7. **Invariant IDs use truncated hashes** (`INV-15c181b028494489`). These are
   not human-readable, but they are deterministic and traceable through the
   source map. Acceptable for machine-driven workflows.

---

## Missing or Weak Authority Coverage

The following gold-spec items have no corresponding authority invariant and are
not acknowledged as gaps:

| Gold spec item | Level | Authority coverage |
|---|---|---|
| `REQ.component-organization` | SHOULD | **Missing entirely** |
| `CONSTRAINT.code-style-rules` | MUST | **Missing** — only preprocessor prohibition extracted |
| `REQ.new-todo` full behavior | MUST | Weak — only "input exists" captured, not trim/empty/append/clear |
| `REQ.toggle-all` full behavior | MUST | Weak — only "checkbox exists" captured, not toggle-all sync logic |
| `REQ.item-interactions` | MUST | **Missing** — no invariant for checkbox completion, double-click edit, hover destroy |
| `REQ.editing` full behavior | MUST | Partial — editing persistence prohibition captured, but save-on-blur, save-on-enter, escape-discard, empty-edit-destroy all missing |
| `REQ.counter` | MUST | Weak — active count relation captured, but `<strong>` wrapping and pluralization missing |
| `REQ.clear-completed` full behavior | MUST | Partial — removal captured, hidden-when-none missing |
| `REQ.persistence` full behavior | MUST | Weak — localStorage existence captured, but dynamic persistence, framework-preference, reload-restore missing |
| `REQ.filtered-state` full behavior | MUST | Partial — route-filtering and selected-class captured, but in-filter-update visibility and filter-persist-on-reload not individually expressed |
| `REQ.readme` detail | MUST | Weak — README existence captured, but "describes framework, implementation, build process" missing |
| `REQ.dependency-management` | MUST | Weak — package.json existence captured via wrong source ref; todomvc-common and todomvc-app-css dependency requirements missing |
| `REQ.node-modules-pruning` | SHOULD | **Missing** |
| `REQ.empty-state-visibility` | SHOULD | Captured via two RELATION_CONSTRAINT invariants ✓ |
| `DATA.todo-record` | SHOULD | **Missing** — id/title/completed key convention |
| `DATA.localstorage-key` | SHOULD | Weak — localStorage existence captured, but `todos-[framework]` naming format not expressed |
| `DATA.editing-state` | MUST_NOT | Captured ✓ |
| `CONSTRAINT.browser-support` | MUST | Misattributed (see High-Severity #2), but the invariant text exists elsewhere |
| `CONSTRAINT.template-base` | SHOULD (gold) | Not captured as invariant (reasonable given SHOULD level) |
| `CONSTRAINT.framework-best-practices` | SHOULD | Not captured (reasonable) |

**Summary**: Of the gold spec's ~25 MUST-level items, the authority directly and
correctly captures roughly 4–5 behavioral contracts. The remaining coverage is
either absent, reduced to field-existence assertions, or misattributed.

---

## Over-Promoted or Distorted Authority

1. **`INV-a959ee3b23ba81ca` promotes an EXAMPLE to a REQUIRED_FIELD.** The
   invariant derives `REQUIRED_FIELD:field_name=package.json` from
   `EXAMPLE.package-json.title`. In the source spec, the example JSON snippet
   is illustrative. The actual requirement for package.json comes from
   `REQ.dependency-management`. The invariant is correct in substance but
   sourced from the wrong item type, which could mislead an auditor into
   thinking examples are normative.

2. **`INV-be7b91ef55f0d4bf` attributes empty-state visibility to browser
   support.** The `RELATION_CONSTRAINT` about `#main hidden when todo list
   empty` is attributed to `CONSTRAINT.browser-support` (browser compatibility).
   This is a source-map error; the behavior belongs to
   `REQ.empty-state-visibility`. The invariant text is not distorted, but the
   provenance is wrong.

3. **`INV-28d0075a5a3994f1` attributes filter-link class toggling to
   clear-completed.** Similarly cross-wired: the invariant about
   `selected class on filter link <= route state` is attributed to
   `REQ.clear-completed` when it should reference `REQ.filtered-state`.

4. **No fabricated requirements detected.** All invariant texts correspond to
   real behaviors from the source. The authority does not invent rules that the
   source doesn't support.

---

## Assumptions and Gaps

### Assumptions

| ID | Assessment |
|---|---|
| `ASM-1` | **Accurate and honest.** Directly mirrors `ASSUMPTION.source-is-complete` from both the generated and gold specs. Appropriate for a benchmark where external references are context, not authority. |
| `ASM-2` | **Accurate and useful.** Correctly notes that SHOULD-level guidance is not promoted to hard invariants. This is a deliberate design choice that aligns with the gold spec's corrections (which demoted several over-promoted MUSTs back to SHOULDs). |

### Gaps

| ID | Assessment |
|---|---|
| `GAP-1` | **Reasonable but vague.** "Comparison to other examples is treated as non-authoritative style guidance" is fair — the source says "the app should look and behave exactly like the template" which is a SHOULD, not a MUST. However, the gap doesn't specify which normative guidance is affected. |
| `GAP-2` | **Accurate and honest.** Framework-specific deviations are inherently context-dependent and cannot produce static invariants. This is a legitimate architectural limitation of the authority format. |
| `GAP-3` | **Accurate but obvious.** The spec doesn't have numeric maxima, so this is trivially true. Low value but not harmful. |

**Missing gaps the authority should acknowledge:**
- The authority should note that behavioral acceptance criteria (editing
  lifecycle, counter pluralization, toggle-all sync, etc.) are not represented
  as invariants and must be sourced from the structured spec.
- The authority should note that MUST-level code style rules (quotes, named
  constants) are not captured.

---

## Source Reference Quality

**Mixed.** The source map provides per-invariant traceability, but the quality
varies:

- **Good**: `INV-29a2d43f83b2e1f3` → `DATA.editing-state.statement` is precise
  and correct.
- **Good**: `INV-f036fa9d50476c33` and `INV-0368a4c1626c33dc` →
  `CONSTRAINT.html-css-js-style.acceptance[5]` is precise with array indexing.
- **Problematic**: `INV-15c181b028494489` → `REQ.readme.title` cites only the
  title field, not the statement or acceptance criteria. A reviewer would need
  to look up the full item.
- **Wrong**: `INV-be7b91ef55f0d4bf` → `CONSTRAINT.browser-support` for an
  empty-state-visibility invariant.
- **Wrong**: `INV-28d0075a5a3994f1` → `REQ.clear-completed` for a
  filtered-state invariant.
- **Weak**: `INV-a959ee3b23ba81ca` → `EXAMPLE.package-json.title` — correct
  that the example exists, but the normative authority is
  `REQ.dependency-management`.

References use the structured spec's item ID namespace (e.g.,
`REQ.new-todo.acceptance[0]`), which is specific enough for auditing when
correct. The two cross-wired references are the main concern.

---

## Downstream Readiness

### Vision Agent
**Can proceed with caveats.** The scope themes and domain field provide enough
orientation. The authority's invariants convey the high-level shape (todo CRUD,
persistence, routing, no preprocessors) but a Vision agent would miss the
nuanced behavioral contracts (editing lifecycle, toggle-all bidirectional sync,
counter pluralization).

### Backlog Agent
**Can proceed but will under-specify stories.** The 14 invariants map to
roughly 6–7 feature areas, which is a reasonable backlog skeleton. However,
acceptance criteria for most stories would need to be sourced from the
structured spec, not the authority. The authority alone would produce stories
with incomplete acceptance criteria.

### Story Agent
**Needs structured spec supplementation.** A Story agent working only from the
authority would produce stories missing critical edge cases (empty-edit
destroys todo, escape-key discards, filter persists on reload, toggle-all
reflects individual changes). The authority's `REQUIRED_FIELD` and
`RELATION_CONSTRAINT` invariants are too coarse for story-level acceptance.

### Sprint Agent
**Can proceed.** Sprint planning is primarily about sequencing and capacity, not
behavioral detail. The authority's scope themes and invariant groupings provide
enough structure for sprint organization.

### Implementation Agent
**Cannot rely on authority alone.** An implementation agent would need the full
structured spec (gold or generated) to implement correctly. The authority
provides useful guardrails (no preprocessors, editing not persisted) but not
enough behavioral specification for correct implementation.

---

## Scores

| Dimension | Score | Rationale |
|---|---|---|
| **Completeness** | 4/10 | Only ~5 of ~25 MUST-level behavioral requirements are faithfully represented. Most coverage is field-existence or relational, not behavioral. |
| **Fidelity** | 6/10 | No fabricated requirements. Two cross-wired source references and one example-promoted-to-requirement reduce fidelity. Assumptions and gaps are honest. |
| **Downstream Utility** | 5/10 | Useful as a guardrail layer and orientation aid, but insufficient as the sole authority for Story or Implementation agents. |
| **Reviewability** | 6/10 | Source map exists and uses precise item IDs, but two misattributions and shallow title-only references hurt auditability. |
| **Acceptance Confidence** | 5/10 | The authority is safe (no fabricated rules, honest gaps) but incomplete enough that downstream agents must also consult the structured spec. |

---

## Final Recommendation

**Accept with noted risks.** The compiled authority is honest about what it
captures and does not fabricate requirements. Its assumptions and gaps are
accurate. However, it captures only a fraction of the source spec's behavioral
contracts, and downstream agents (especially Story and Implementation) cannot
rely on it as their sole source of truth.

**For the human reviewer:**

1. **Accept this authority** as a guardrail/orientation layer — it correctly
   identifies the domain, scope themes, key constraints (no preprocessors,
   editing not persisted), and core feature areas.

2. **Require downstream agents to also consult the structured spec** for
   behavioral acceptance criteria. The authority should not be the only input
   to Story generation.

3. **File two bugs against the authority compiler:**
   - Cross-wired source references (`INV-be7b91ef55f0d4bf` cites browser
     support for empty-state visibility; `INV-28d0075a5a3994f1` cites
     clear-completed for filter-link class toggling).
   - Example-to-requirement promotion (`INV-a959ee3b23ba81ca` derives
     package.json requirement from `EXAMPLE.package-json` instead of
     `REQ.dependency-management`).

4. **Consider expanding the invariant vocabulary** beyond `REQUIRED_FIELD`,
   `FORBIDDEN_CAPABILITY`, and `RELATION_CONSTRAINT` to include behavioral
   invariants that can express acceptance criteria directly (e.g.,
   `BEHAVIOR_CONTRACT`, `STATE_TRANSITION`, `USER_INTERACTION`).
