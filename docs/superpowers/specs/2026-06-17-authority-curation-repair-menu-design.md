# Authority Curation Repair Menu Design

**Date:** 2026-06-17
**Status:** Accepted
**Spec mode:** proposed_change
**Owner:** AgileForge maintainers
**Scope:** `authority curate` v2 contract, bounded repair application, replay fixtures, curation diff validation, and safe curation tracing

## Revision History

- 2026-06-17: Drafted after repeated ASA authority curation retries showed that
  model-authored patch syntax is the wrong trust boundary.
- 2026-06-17: Revised after external review to bind menu handles to exact
  target fields, defer structural/parameter repairs from v2 initial scope,
  require per-collection diff targets, and make v1 patch/full-candidate
  mutation paths forbidden for new v2 attempts.
- 2026-06-17: Accepted for implementation planning after review concerns were
  resolved in-document.

## Summary

`authority curate` must stop asking an LLM to emit AgileForge's internal patch
API. The current contract lets the model choose target ids, patch operations,
JSON paths, and sometimes full authority JSON. Different models produce
different valid-looking mutation dialects, and AgileForge discovers contract
edges only during live project retries.

Replace model-authored patches with a host repair menu:

```text
host builds repair menu from blocking feedback
-> model selects host-minted handles and supplies prose only when needed
-> host resolves handles to authority targets
-> host applies deterministic bounded repairs
-> host validates invariants, assumptions, and gaps
-> human reviews the curated candidate
```

The model may decide wording and whether feedback is unresolvable. The host owns
target resolution, field/path selection, id recomputation, lineage, diff
bounding, idempotency, and persistence.

## Problem

ASA authority curation showed a stable root cause: the LLM is being asked to
serialize private authority internals.

Observed failures included:

- `target_id=authority:7`, which is too broad for a bounded repair;
- `target_id=assumptions[10]`, which is a collection-index alias rather than a
  durable target;
- `path=$.source_authority_json.assumptions[10]`, which is a global JSONPath
  rather than a target-local repair;
- `path=/text`, which can be semantically reasonable but was a model-authored
  structural choice;
- `path=/parameters/rule`, which is valid only for specific typed invariants;
- malformed `replace_value` operations missing required `path` or `value`;
- full `candidate_authority_json`, which can change unrelated authority
  content;
- unresolved feedback after repair attempts;
- recovery and migration defects in the new curation path.

The failures differ by provider/model. That pattern points to a leaky boundary,
not a single bad model.

Current repo evidence also shows two independent safety gaps that the v2 design
must close:

- `services/specs/authority_curation_diff.py::build_authority_diff` currently
  diffs invariants only.
- `services/agent_workbench/authority_curation.py::_candidate_authority_from_workflow_result`
  accepts full `candidate_authority_json` before patch authorization.

Those gaps mean a broad candidate can bypass per-patch target authorization and
avoid full bounding for assumptions and gaps.

## Goals

- Make target resolution host-owned and deterministic.
- Prevent the model from authoring `target_id`, `target_kind`, `op`, `path`,
  JSON Pointer, JSONPath, collection index, or full authority JSON.
- Keep repair output small enough for cheap models to follow.
- Preserve targeted repair and fail-closed behavior.
- Validate curated diffs across invariants, assumptions, and gaps.
- Store enough safe trace data to debug failures without logging private spec or
  authority text by default.
- Add offline replay fixtures for the ASA failure modes before more live ASA
  retries.
- Keep human authority review and accept/reject as explicit guarded decisions.

## Non-Goals

- Do not auto-accept curated authority.
- Do not weaken source-map, provenance, or stale-chain guards.
- Do not back-propagate every authority repair into the source spec.
- Do not require a new model provider.
- Do not add a cloud observability backend or require OpenTelemetry in v1.
- Do not make broad full regeneration the preferred answer to targeted human
  rejection.
- Do not implement the code in this spec document.

## Current Behavior

Current `authority curate` behavior is patch-based:

```text
authority reject
-> authority feedback record
-> ADK curation workflow emits patches or candidate authority
-> host tries to apply/validate result
-> candidate becomes pending review or command fails closed
```

Patch output can include fields like:

```json
{
  "target_kind": "invariant",
  "target_id": "INV-943d18f5ecffcd3c",
  "op": "replace_value",
  "path": "/parameters/rule",
  "value": "Use qualified observational language."
}
```

This gives the model too much structural authority. The host now contains
multiple compatibility adapters for model-emitted aliases and paths. Those
adapters are symptoms of the same boundary error.

## Proposed Approach

### Repair Menu Contract

Before invoking the repair agent, the host builds a repair menu from the
recorded blocking feedback and the rejected source authority.

Menu entries are deterministic and host-minted:

```json
{
  "handle": "R3",
  "feedback_id": "AFB-language-1",
  "target_kind": "invariant",
  "target_id": "INV-943d18f5ecffcd3c",
  "target_field": "text",
  "target_review_label": "INV-943d18f5ecffcd3c",
  "overlay_target_key": "CONSTRAINT.no-causal-or-optimal-control-claims:invariant:text:0",
  "allowed_repair_kinds": ["replace_text", "mark_unresolvable"],
  "target_content_hash": "sha256:..."
}
```

The menu is part of the model input. It may include redacted summaries, hashes,
or bounded snippets according to existing privacy policy, but the durable trace
must not require raw text.

Each `replace_text` handle is bound to one exact `target_field` at menu
generation time. The host must not select or guess the field during repair
application. If a target has multiple independently repairable text fields, the
host emits separate handles, one per field. If the host cannot determine a safe
field, it emits no repair handle and records a deterministic `not_repairable`
reason.

The model returns selections only:

```json
{
  "repairs": [
    {
      "feedback_id": "AFB-language-1",
      "target_handle": "R3",
      "repair_kind": "replace_text",
      "replacement_text": "Reports must use qualified observational language and must not make positive unsupported causal, guaranteed, optimal, or production-ready claims."
    }
  ]
}
```

The model must not output:

- `target_id`
- `target_kind`
- `op`
- `path`
- `value`
- `candidate_authority_json`
- JSON Pointer or JSONPath
- collection-index aliases such as `assumptions[10]`

### Minimal Repair Kinds

The v2 enum must stay small. It is not one enum value per feedback issue type.

Required v2 initial repair kinds:

| Repair kind | Model payload | Host action |
| --- | --- | --- |
| `replace_text` | `replacement_text` | Replace the exact `target_field` bound to the selected handle. |
| `mark_unresolvable` | `reason` | Leave authority unchanged and report feedback as unresolved. |

Deferred repair kinds:

- `modify_parameters` is deferred to v2.1 because it changes typed invariant
  semantics, exercises id recomputation, and requires a parameter contract.
- `remove_item` is deferred to v2.1 because removal can change coverage and may
  require a spec amendment rather than an authority overlay.

If feedback asks for parameter changes or item removal in v2 initial, the menu
must expose `mark_unresolvable` and a deterministic `not_repairable` reason
instead of giving the model a structural mutation handle.

### Deterministic Host Application

For each repair selection, the host must:

1. Validate `feedback_id` exists in the recorded feedback attempt.
2. Validate the feedback is blocking or explicitly repairable.
3. Validate `target_handle` exists in the host-built menu for that feedback id.
4. Resolve the handle to `(target_kind, target_id, target_field)` without using
   model-authored ids or paths.
5. Validate `repair_kind` is in the menu entry's `allowed_repair_kinds`.
6. Load the target from the rejected source authority.
7. Use the exact `target_field` stored on the menu entry.
8. Apply exactly one bounded edit.
9. Recompute invariant id only when type, parameters, or provenance inputs
   change.
10. Record lineage for text-only repairs even when the target id does not
    change.
11. Validate final diff across invariants, assumptions, and gaps.
12. Fail closed before publishing if any unrelated content changed.

Field binding must be explicit:

- The menu entry's `target_field` is the only field the repair may write.
- The host may emit one handle per repairable field.
- The host must not silently fall back to a synthetic `text` field.
- `summary`, `reason`, and other commentary fields are not repairable in v2
  initial unless a future design explicitly makes them repair targets.
- If no safe field is available, menu generation records
  `not_repairable: field_ambiguous`.

### Diff Bounding

`build_authority_diff` must cover these collections:

- `invariants`
- `assumptions`
- `gaps`

Each collection needs:

- stable collection-local target keys;
- its own targeted-key allowlist derived from selected menu handles;
- changed ids/items;
- added ids/items;
- removed ids/items;
- untargeted changes;
- lineage where applicable.

String-only assumptions and gaps need review-visible ids such as `ASM-11` or
`GAP-3` derived by the host for display, plus content-derived keys for replay
and diffing. Positional display ids alone are not durable enough for overlay
replay because ordering can change after a recompile.

Full `candidate_authority_json` output is forbidden in v2. Any workflow result
containing `candidate_authority_json` must fail with `full_candidate_forbidden`.
Any workflow result containing legacy `patches` must fail with
`legacy_patch_forbidden`. All authority changes must pass through repair menu
selections.

### Overlay And Source Spec Relationship

Authority curation repairs are not automatically product-spec amendments.

Rule:

- If feedback changes product meaning, the correct action is spec amendment.
- If feedback fixes compiler interpretation of an already-correct spec, the
  correct action is a curation overlay/feedback ledger entry.

Persist accepted curation repairs as an overlay ledger linked to:

- project id;
- spec version id;
- source authority id and fingerprint;
- feedback attempt id and fingerprint;
- menu contract version;
- repair selection fingerprint;
- overlay target key;
- pre-repair target content hash;
- post-repair target content hash;
- resulting candidate authority id and fingerprint;
- lineage and diff summary.

v2 initial must persist overlay metadata for audit. Automatic overlay replay is
not required in v2 initial, but any replay attempt must use this strict order:

1. Match the original `target_id` and `target_field` in the current authority.
2. If the id no longer exists, match a unique target by
   `(source_item_id, target_kind, target_field, relative_sequence)` when
   `source_item_id` is present.
3. For string-only assumptions or gaps with no `source_item_id`, match only
   when the pre-repair content hash is unique in the current collection.
4. If no unique target exists, or more than one target matches, mark the overlay
   `broken_by_upstream_change` and require human review.

Spec amendment is still required when feedback changes product meaning.

## Data Contract

### Repair Menu Entry

```python
class RepairMenuEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    handle: str
    feedback_id: str
    target_kind: Literal["invariant", "assumption", "gap"]
    target_id: str
    target_field: str
    target_review_label: str
    overlay_target_key: str
    allowed_repair_kinds: list[
        Literal["replace_text", "mark_unresolvable"]
    ]
    target_content_hash: str | None = None
```

### Model Repair Output

```python
class RepairSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    feedback_id: str
    target_handle: str
    repair_kind: Literal[
        "replace_text",
        "mark_unresolvable",
    ]
    replacement_text: str | None = None
    reason: str | None = None


class RepairSelectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    repairs: list[RepairSelection]
```

Validation rules:

- `replacement_text` is required only for `replace_text`.
- `reason` is required only for `mark_unresolvable`.
- `target_handle` must be a menu handle, not a target id.
- Resolved and unresolved feedback ids are host-derived from `repairs`.
- Blocking feedback omitted from `repairs` keeps the curation gate failed.

## Functional Requirements

| ID | Requirement | Acceptance Criteria | Priority |
| --- | --- | --- | --- |
| FR-001 | Build a host repair menu from blocking authority feedback. | Every repairable feedback item has at least one host-minted handle bound to an exact `target_field`, or a deterministic `not_repairable` reason. | Must |
| FR-002 | Reject model-authored target ids and paths. | Repair output schema has no `target_id`, `target_kind`, `op`, `path`, `value`, or `candidate_authority_json` fields. Extra fields fail validation. | Must |
| FR-003 | Apply repairs through deterministic host handlers. | Host resolves `target_handle` to a real target and applies only the advertised repair kind. | Must |
| FR-004 | Bound diffs across invariants, assumptions, and gaps. | Untargeted changes in any of the three collections fail before candidate publication, using per-collection target allowlists derived from selected handles. | Must |
| FR-005 | Remove the full-candidate bypass. | Any workflow result containing `candidate_authority_json` or legacy `patches` is rejected in v2. | Must |
| FR-006 | Persist safe per-repair trace data. | Trace includes handle, feedback id, resolved target kind/id, repair kind, result, reject reason, model id, and attempt ids. | Must |
| FR-007 | Preserve human review. | Curated authority can only become `authority_pending_review`; no curation path accepts authority. | Must |
| FR-008 | Persist overlay metadata for future replay. | Accepted repairs store overlay target key and content hashes; replay attempts either resolve uniquely or become `broken_by_upstream_change`. | Should |

## Feedback Issue Mapping

Menu generation maps feedback issue types to v2 repair kinds in host code. The
model does not choose this mapping.

| Feedback issue family | v2 initial menu behavior |
| --- | --- |
| Overstrong, brittle, unclear, or stale wording on an existing invariant, assumption, or gap | Offer `replace_text` for each safe target field and `mark_unresolvable`. |
| False/stale assumption wording | Offer `replace_text` and `mark_unresolvable`. |
| Near-duplicate or over-split content requiring deletion/merge | Offer `mark_unresolvable`; route deletion/merge to future v2.1 or spec amendment. |
| Missing invariant, missing gap, or coverage issue | Offer `mark_unresolvable`; do not synthesize new authority in v2 initial. |
| Parameter/type/provenance correction | Offer `mark_unresolvable`; route to v2.1 `modify_parameters` or source spec amendment. |
| Feedback that changes product meaning | Offer no repair handle; record `not_repairable: spec_amendment_required`. |

## Error Handling

New curation v2 error codes:

| Code | Retryable | Meaning |
| --- | --- | --- |
| `AUTHORITY_REPAIR_INTENT_INVALID` | false | Model selection payload is structurally valid JSON but violates menu, feedback, or repair-kind rules. |
| `AUTHORITY_REPAIR_TARGET_NOT_FOUND` | false | Host-minted handle resolves to a target that is no longer present in the rejected source authority. |

| Case | Required Behavior | Error / Status |
| --- | --- | --- |
| Unknown `target_handle` | Reject before mutation. | `AUTHORITY_REPAIR_INTENT_INVALID`, reason `target_handle_unknown` |
| Feedback id not in attempt | Reject before mutation. | `AUTHORITY_REPAIR_INTENT_INVALID`, reason `feedback_not_found` |
| Feedback not blocking/repairable | Reject before mutation. | `AUTHORITY_REPAIR_INTENT_INVALID`, reason `feedback_not_repairable` |
| Repair kind not allowed for handle | Reject before mutation. | `AUTHORITY_REPAIR_INTENT_INVALID`, reason `repair_kind_not_allowed` |
| Target missing from source authority | Reject before mutation. | `AUTHORITY_REPAIR_TARGET_NOT_FOUND` |
| Menu cannot bind a safe field | Do not emit repair handle; expose only `mark_unresolvable`. | `not_repairable`, reason `field_ambiguous` |
| Workflow result contains `candidate_authority_json` | Reject before diff validation. | `AUTHORITY_REPAIR_INTENT_INVALID`, reason `full_candidate_forbidden` |
| Workflow result contains legacy `patches` | Reject before diff validation. | `AUTHORITY_REPAIR_INTENT_INVALID`, reason `legacy_patch_forbidden` |
| Untargeted invariant/assumption/gap change | Reject before publication. | `AUTHORITY_CURATED_DIFF_UNBOUNDED` |
| Blocking feedback unresolved | Fail curation gate without publishing candidate. | `SPEC_COMPILE_FAILED` or curation gate failure with unresolved ids |
| Idempotency key replay with different request or menu fingerprint | Reject as stale/mismatched replay. | `IDEMPOTENCY_KEY_REUSED` or stale guard error |

Every rejection after model output must persist a normalized rejected selection
vector containing handles, repair kinds, reject reasons, and hashes/lengths for
private text fields. Raw replacement text remains redacted by default.

## Observability

Trace events must be safe by default. Standard traces must not record raw
`replacement_text`, authority item body text, or full feedback instruction text.

Safe indexed fields:

- `mutation_event_id`
- `curation_attempt_id`
- `feedback_attempt_id`
- `feedback_id`
- `target_handle`
- resolved `target_kind`
- resolved `target_id`
- `repair_kind`
- host-bound `target_field`
- `result`
- `reject_reason`
- `requested_model_id`
- `compiler_version`
- `prompt_hash`
- `menu_fingerprint`
- `selection_fingerprint`
- content length/hash fields where useful
- normalized rejected selection vector when a selection is rejected

Private fields may be captured only behind an explicit local diagnostic flag.
That flag must be off by default and must be visible in trace metadata.

## Replay Fixtures

Before v2 is used against ASA again, add offline replay fixtures for the real
failure classes:

| Fixture | Input Shape | Expected Result |
| --- | --- | --- |
| broad authority target | Legacy patch uses `authority:7`. | v2 rejects as forbidden legacy target or unknown handle. |
| collection alias | Legacy patch uses `assumptions[10]`. | v2 rejects as forbidden legacy target or unknown handle. |
| global JSONPath | Legacy patch uses `$.source_authority_json.assumptions[10]`. | v2 rejects; host menu repair uses handle instead. |
| text JSON pointer | Legacy patch uses `/text`. | v2 has no path; `replace_text` succeeds through host field selection. |
| parameter JSON pointer | Legacy patch uses `/parameters/rule`. | v2 rejects/delegates to `mark_unresolvable`; v2.1 may add `modify_parameters`. |
| malformed replace value | Legacy patch omits `path` or `value`. | v2 schema rejects extra/legacy patch shape before host mutation. |
| full candidate JSON | Workflow returns full authority JSON. | v2 rejects `full_candidate_forbidden`. |
| unresolved feedback | Model omits blocking feedback. | Gate fails with unresolved feedback ids. |
| untargeted assumption/gap change | Candidate changes assumption/gap outside menu. | Diff validation catches it. |
| text-only invariant repair | `replace_text` edits invariant text. | Id remains stable unless hash inputs changed; lineage records text-only repair. |
| idempotency replay mismatch | Same idempotency key, different request/menu fingerprint. | Replay is rejected; first attempt is not overwritten. |
| thin gate detail | Any rejected selection. | Trace records reject reason and normalized rejected selection vector. |

These fixtures must run without model calls or live ASA database state.

## Alternatives Considered

### Keep Patch Contract And Add More Adapters

Reject. This repeats the current failure loop. Each model can invent another
valid-looking addressing scheme.

### Free-Form `repair_intent` With Model-Authored `target_id`

Reject. It removes paths but keeps target hallucination. The target must be a
host-minted handle.

### Full Regenerate With Feedback In Prompt

Reject as default. Useful as a fallback, but it can disturb unrelated authority
and recreate the same defect.

### Back-Propagate Every Repair Into Source Spec

Reject as default. Some feedback fixes compiler interpretation, not product
scope. Source spec amendment is required only when feedback changes product
meaning.

### Remove Authority Compilation

Reject for this design. Authority still provides the structured ids, source
maps, and diff targets needed for fail-closed review. The problem is curation
boundary design, not the whole authority concept.

## Relationship To Existing Curation Loop Spec

The prior authority curation loop design still owns the ADK workflow substrate
and human review checkpoint. This spec narrows the repair node contract:

```text
TargetedRepairCompiler -> RepairSelectionPayload
```

The repair node must not emit patches or full authority JSON. The host menu
builder and repair applier are deterministic service boundaries around that
node. Collapsing planner and repair compiler into one smaller node is allowed
during implementation planning, but is not required by this spec.

## Security And Privacy

- Model prompts may contain private spec/authority content. Existing external
  model export approval rules remain unchanged.
- Standard traces must not store raw replacement text or full source authority
  content.
- Menu handles are opaque and local to one curation attempt.
- Selection fingerprints must be deterministic but not reversible into private
  text.
- Failure artifacts must follow the same redaction policy as trace events.

## Reliability And Recovery

- Curation remains a guarded mutation with idempotency.
- Request hash is computed before model invocation and must include source
  authority fingerprint, feedback attempt fingerprint, menu contract version,
  and menu fingerprint.
- `selection_fingerprint` is computed after model output and stored on the
  attempt for audit and recovery. It is not part of the initial request hash.
- If a retry reuses the same idempotency key with a different request or menu
  fingerprint, it must fail rather than silently replay stale repair output.
- If recovery discovers a stored selection fingerprint and a newly produced
  selection fingerprint for the same idempotency key, they must match exactly or
  recovery fails closed.
- If candidate publication succeeds but final workflow transition fails,
  recovery must reconcile to the published candidate or return explicit
  recovery-required metadata.
- No recovery path may publish a second candidate for the same idempotency key.

Lineage reason vocabulary:

| Reason | Meaning |
| --- | --- |
| `text_only_no_id_change` | `replace_text` changed body text while stable id inputs stayed the same. |
| `id_recomputed` | A future structural repair changed type, parameters, or provenance inputs. |
| `removed_by_repair` | A future removal repair intentionally removed a target. |
| `unchanged` | Target is unchanged by this curation attempt. |

`new_id = null` may only mean `removed_by_repair`. Text-only repairs must keep
`new_id = old_id` and record old/new content hashes.

## Migration And Compatibility

Use contract versioning:

```text
authority_curation.v1 = legacy patch/candidate contract
authority_curation.v2 = repair menu selection contract
```

Migration rules:

- Additive DB fields may record `contract_version`, `menu_fingerprint`,
  `selection_fingerprint`, and overlay status.
- Existing v1 curation attempts remain audit history.
- v2 should be enabled only after replay fixtures pass.
- New v2 attempts must hard-reject `candidate_authority_json` and legacy
  `patches`; neither is a fallback mutation path.
- Existing v1 patch/candidate rows remain read-only audit history during one
  compatibility window.
- After the compatibility window, legacy mutation helpers may be removed once
  no supported command can start a v1 mutation.
- Add a kill switch that forces curation to return a deterministic
  `fail_no_candidate` result without invoking the repair agent.
- Resolve duplicate lineage storage before implementation planning: either
  collapse `candidate_lineage_json` and host `lineage_json` to one source of
  truth, or document why both remain.

## Success Metrics

| Metric | Target | Source |
| --- | --- | --- |
| Replay fixture pass rate | 100% before ASA retry | Offline tests |
| Live curation unsafe patch failures | 0 known legacy patch-shape failures after v2 | Curation trace |
| Untargeted diff escapes | 0 for invariants, assumptions, and gaps | Diff tests and trace |
| Private raw text in default traces | 0 occurrences | Trace tests |
| Human review preserved | 100% curated candidates stop at pending review | Workflow tests |

## Resolved Design Decisions

| Decision | Resolution |
| --- | --- |
| Include `modify_parameters` in v2 initial? | No. Defer to v2.1. v2 initial repairs text or marks feedback unresolvable. |
| Include `remove_item` in v2 initial? | No. Defer to v2.1 or route through spec amendment because removal can change authority coverage. |
| Use runtime text-field priority? | No. Menu handles are bound to exact `target_field`; the host emits one handle per safe repairable field. |
| Overlay replay after ids change? | Persist target key and content hashes now; replay only on unique strict/fallback match, otherwise `broken_by_upstream_change`. Automatic replay is not required in v2 initial. |
| Remove or disable v1 patch contract? | Disable v1 mutation for new v2 attempts immediately; keep existing v1 rows read-only for audit during a compatibility window. |

## Open Questions

No blocking open questions remain for design review. Implementation planning may
still choose exact DB column names, CLI flag names, and compatibility-window
duration.

## Acceptance Criteria For This Design

- A reviewer can explain why model-authored `target_id` and `path` are no longer
  part of the contract.
- A reviewer can identify how each ASA failure fixture is handled without a live
  model call.
- The design explicitly closes the full-candidate bypass.
- The design explicitly requires diff bounding for assumptions and gaps, not
  only invariants.
- The design states when spec amendment is required versus when curation overlay
  is valid.
- Design decisions formerly listed as open questions are resolved or explicitly
  deferred.
