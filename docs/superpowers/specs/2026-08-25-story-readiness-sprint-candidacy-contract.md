# Story Readiness and Sprint Candidacy Contract (#223)

## Status and supersession

This is the current durable contract for Story eligibility, human Sprint scope,
dependency confirmation, and Sprint candidacy. It supersedes the
parent-requirement selection authority, whole-parent scope restriction, and
`story_completion_scope` candidate mechanism in
[the 2026-06-09 selection design](2026-06-09-story-selection-sprint-scope-design.md).
It does not change #224 team-name or team-default ownership.

The policy is a hard break for fresh profiles. There is no unversioned legacy
branch or compatibility alias. Missing, v2, malformed, tampered, or stale
evidence is reconciled explicitly; it never creates or infers human selection.

## Structural evidence and its boundary

Accepted Stories receive canonical provider-free
`agileforge.story-validation-evidence.v3` structural evidence inside the same
acceptance transaction. Expected structural rule failures commit the accepted
Story and its diagnostics atomically. Evaluator, persistence, or transaction
failures roll back acceptance and evidence atomically.

Current passing v3 evidence publishes this exact stable proof scope for that
accepted Story version:

- exact Story identity;
- immutable accepted Story artifact/item binding;
- accepted Backlog and Specification lineage;
- parent-bounded Specification references;
- required Story shape;
- non-empty acceptance criteria; and
- current evidence and input fingerprints.

It publishes this exact non-proof scope: semantic/model quality, product value,
human Sprint selection, dependency safety, Sprint candidacy, and
Sprint-generation readiness. Missing, v2, stale, and malformed evidence are
operationally reconciled through the explicit reconciliation surface. A fresh
canonical v3 evidence fingerprint is part of the scope identity, so
reconciliation cannot silently revive an old dependency decision.

The old operator mutation surfaces are removed without aliases:

- Application/API/CLI `validate_story` is removed.
- `POST /api/projects/{project_id}/story/validate` is removed.
- `agileforge story validate` is removed.

The retained explicit reconciliation surfaces are:

- `POST /api/projects/{project_id}/story/structural-eligibility/reconcile`
- `agileforge story eligibility reconcile [--story-id <id>]`

## Human Story selection

Each exact, active accepted Story has one append-only human intent projection:
`unselected`, `selected`, or `deferred`. Default `unselected` and every later
state have an exact-version state fingerprint. A real transition adds one
canonical selection event; an already-represented intent is a receipt-backed
no-op and adds no duplicate event.

Every real event is anchored to its creating completed
`apply_story_sprint_selection` receipt in the same transaction. Replay verifies
the canonical request JSON and fingerprint, Project/Story/actor/intent,
rationale and correlation ID where present, prior state fingerprint, exact
event identity/fingerprint/result state, and event lineage. Missing, malformed,
or mismatched anchors fail closed; there is no unanchored legacy reader.
Successful receipts contain only immutable transition identity: Project, Story,
resulting selection state and state fingerprint, plus the selection event ID and
fingerprint when a real event was added. Mutable eligibility, dependency, and
candidacy truth is never replayed from a receipt; clients reload the current
projection after the mutation.

| Intent | Allowed current state | Result |
| --- | --- | --- |
| `select` | `unselected` or `deferred` | `selected` |
| `remove` | `selected` or `deferred` | `unselected` |
| `defer` | `unselected` or `selected` | `deferred` |

`select` requires current passing v3 evidence. `remove` and `defer` remain
available when evidence is stale, so durable human intent can be corrected.
Selection is locked once that exact Story is bound to an accepted Sprint plan or
an active Sprint. Superseding a Story does not transfer its intent.

The selection mutation surfaces require the observed state fingerprint and are
the only scope writers:

- `POST /api/projects/{project_id}/story/sprint-selection`
- `agileforge story sprint-selection select|remove|defer --story-id <id> --expected-state-fingerprint <sha256>`

No API, CLI, UI, request parameter, reconciliation, or dependency operation
may infer selection from readiness, a parent requirement, a candidate list, or
an omitted selection event.

## Current scope, dependencies, and candidacy

The current selected scope is the exact active Stories with both `selected`
intent and current passing v3 evidence. Stories already attached to a completed
Sprint are excluded from the *current* planning scope and candidate pool without
clearing their durable human intent. After completed-Sprint post-triage, a newly
selected current scope with no exact dependency review exposes dependency review
when there is no active Sprint or pending accepted-plan boundary.

Dependency review and mutation are unavailable while an accepted plan is
unstarted, a Sprint is active, or a completed Sprint is awaiting post-Sprint
triage. The mutation rechecks this lifecycle lock in its own transaction; it
does not alter selected-scope rows while prior Sprint execution or history
depends on them.

Dependency confirmation is valid only for the exact selected Story IDs and one
canonical `selected_scope_fingerprint`, which covers accepted Story identities,
current evidence fingerprints, and selection state/event fingerprints. A
selection or evidence change changes that fingerprint. The review owns the exact
persisted dependency rows for selected dependents; external prerequisites remain
visible and must be safe, but unselected dependents do not become scope members.
The mutation request must echo both the IDs and fingerprint observed in the
dependency projection. The application compares both before dispatch and the
transactional handler rechecks the fingerprint; it never substitutes a newer
server-side fingerprint for stale operator input.

The only candidacy equation is:

```text
sprint_candidate = selected AND structurally_eligible AND dependency_safe
```

Candidate, pending-plan freshness, planning input, and start-time checks all
use that exact scope and its dependency evidence. Request Story IDs are exact
guards only; they cannot add a Story, bypass stale confirmation, or become
selection authority.

Sprint-plan generation always submits the exact projected candidate IDs. An
empty ID list is rejected; there is no automatic Story selector or dependency
promotion path. The requested IDs remain guards against the durable candidate
projection, not a second selection authority.

## Historical Sprint integrity

Completed execution history is independent from later current-scope changes.
The Sprint-start audit event stores a canonical immutable snapshot containing
every direct row owned by the selected dependents, regardless of status, plus
the active reachable external-prerequisite closure. Historical dependency
verification uses that snapshot together with the persisted dependency-review
source fingerprint; later edits to current project dependency rows cannot
rewrite earlier Sprint history. Snapshot order, row identity, endpoints,
Project ownership, and fingerprint are checked strictly and fail closed. Sprint
review and close fingerprints hash the immutable accepted-Story payload plus
their terminal task and completion facts, not mutable full `StoryFact`
projections. This event shape and fingerprint definition are part of the #223
fresh-profile hard break; no legacy interpretation is provided.

## API, CLI, and UI reconciliation rule

All selection and reconciliation clients render and submit server-projected
exact IDs and fingerprints, then reload the authoritative projection. Malformed
or unavailable eligibility, selection, selected-scope, dependency, or candidate
facts fail closed. A successful mutation whose reload fails remains locked; no
client guesses candidacy or selection locally.

Dependency reads include the canonical `selected_story_ids` and
`selected_scope_fingerprint`; clients submit those projected values rather than
rederiving a scope from visible Stories. `GET /api/stories/{story_id}` and
`agileforge story show --story-id <id>` expose the exact `StoryFact` structural
eligibility/status/failures, selection state and event/state fingerprints,
selected-scope fingerprint, dependency safety, candidacy, blockers, and the
same structural evidence proof/non-proof lists. They do not expose legacy
`ready_for_sprint` or conflated validation-status fields. A structurally
eligible but unselected Story is not a candidate.

The browser also submits the exact canonical candidate IDs with Sprint-plan
generation and blocks transport when the candidate and dependency projections
do not share one scope fingerprint. API and CLI dependency mutations require
the observed selected-scope fingerprint explicitly; omission is not a request
for the server to choose the latest scope.
