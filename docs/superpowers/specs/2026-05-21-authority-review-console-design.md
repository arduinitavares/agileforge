# Authority Review Console Design

## Purpose

AgileForge already compiles a project specification into a pending authority
artifact and exposes a basic dashboard review card. The current card is useful
for debugging, but it is not yet a strong human review surface. A hybrid
reviewer should be able to scan the authority state quickly, inspect the source
and compiled artifacts, and make the accept or refinement decision from the UI
with the same backend authority decision flow used by the CLI.

This design upgrades the existing dashboard authority panel into a review
console. It does not add a separate pre-compilation specification approval gate.
The approval action remains acceptance of the compiled authority through the
existing `authority_accept` flow, which unlocks the Vision phase.

## Goals

- Show a clear decision summary for pending and accepted authority states.
- Let reviewers inspect specification source, compiled invariants, gaps,
  assumptions, findings, excluded features, eligible rules, and raw JSON.
- Keep product-friendly summary information visible without hiding technical
  provenance.
- Preserve existing accept and reject backend endpoints and FSM behavior.
- Keep all user-controlled artifact content DOM-safe.
- Keep the implementation incremental by enhancing the current tabbed panel.

## Non-Goals

- Do not add a distinct source-spec approval gate before authority compilation.
- Do not change the authority compiler or review semantics.
- Do not render Markdown or spec content as trusted HTML.
- Do not add a new persistence model for review packets.
- Do not replace the existing dashboard project setup flow.

## Reviewer Model

The target reviewer is hybrid:

- A product reviewer needs status, coverage, blockers, gaps, and a clear final
  decision.
- A technical reviewer needs invariant details, source references, spec hashes,
  authority fingerprints, and raw packet access.

The UI should present the product-level decision first, then allow deeper audit
without requiring a separate tool.

## Current System Context

The dashboard project page already uses:

- `GET /api/projects/{project_id}/authority/review` to load the pending or
  accepted authority review packet.
- `POST /api/projects/{project_id}/authority/accept` to accept pending
  authority.
- `POST /api/projects/{project_id}/authority/reject` to request refinement.

The current frontend already has:

- A setup status banner.
- A basic `authority-review-card`.
- Tabs for invariants, specification source, and raw JSON.
- Safe text rendering for source and authority content.

The new work should refine this foundation rather than replace it.

## Proposed UX

### 1. Decision Summary Band

At the top of the authority card, show a compact summary band with:

- Review state: `Accept Ready`, `Blocked`, or `Accepted`.
- Blocking finding count.
- Invariant count.
- Gap count.
- Assumption count.
- Spec path.
- Spec hash or disk hash when available.
- Pending or accepted authority fingerprint.
- Compiled timestamp and compiler version.
- Source inclusion state: full content included, excerpt shown, or content
  truncated.

The summary should be readable before a reviewer opens any tab. The state badge
drives action availability:

- `Accept Ready`: accept button enabled.
- `Blocked`: accept disabled unless existing backend rules allow an override
  path already supported by the current UI.
- `Accepted`: decision controls hidden and the panel becomes read-only.

### 2. Artifact Tabs

Keep the existing tab model and improve the content quality.

#### Overview Tab

The default tab for a hybrid reviewer.

Show:

- Review findings, grouped by blocking and non-blocking.
- Gaps.
- Assumptions.
- Excluded or out-of-scope features.
- Eligible feature rules.

Each list should have an explicit empty state such as `No blocking findings` or
`No compiler assumptions`. Empty sections should not look like loading failures.

#### Invariants Tab

Show compiled invariants as review cards.

Each invariant card should include:

- Invariant ID.
- Invariant text.
- Support status, such as `direct` or `inferred`.
- Source references when present.
- Source excerpt when present.

Add simple client-side filtering:

- Text search across ID, text, source refs, and source excerpt.
- Support filter for `all`, `direct`, and `inferred`.

This gives technical reviewers a fast way to spot-check generated authority
without leaving the dashboard.

#### Spec Source Tab

Show the linked source specification as escaped text.

Rules:

- Use `textContent` or equivalent DOM text node APIs.
- If full source content is present, show it.
- If content is truncated, show the provided excerpt and a truncation warning.
- If no content or excerpt is available, show a clear unavailable state with the
  resolved path and known hashes.

Do not render Markdown as HTML in this milestone.

#### Raw JSON Tab

Show the review packet as formatted JSON for audit and debugging.

The raw packet should include the same data used by the visual tabs. It should
not become the primary review surface.

### 3. Decision Rail

Place the decision controls in a sticky action area under the tabs.

Pending state:

- Primary action: `Accept Authority`.
- Secondary action: `Request Refinement`.
- Refinement requires a non-empty reason.
- If the review is blocked by non-overrideable findings, disable accept and show
  the blocking reason near the button.

Accepted state:

- Hide accept and refinement controls.
- Show read-only status text indicating the authority is accepted and Vision is
  unlocked.

Failure and stale state:

- If accept or reject fails due to stale authority or changed review state,
  reload the review packet and show the returned structured error.
- Preserve the current backend decision endpoints and guard token behavior.

## Data Mapping

The UI should read from the existing review packet shape:

- Project status:
  - `project.name`
  - `project.setup_status`
  - `project.fsm_state`
- Spec metadata:
  - `spec.resolved_path`
  - `spec.spec_hash`
  - `spec.disk_sha256`
  - `spec.content_included`
  - `spec.content_truncated`
  - `spec.source_content`
  - `spec.excerpt`
  - `spec.coverage_summary`
  - `spec.coverage_diagnostics`
- Authority metadata:
  - `pending_authority.authority_id`
  - `pending_authority.authority_fingerprint`
  - `pending_authority.compiler_version`
  - `pending_authority.compiled_at`
  - `pending_authority.review_findings`
  - `pending_authority.review_summary`
  - `pending_authority.artifact.domain`
  - `pending_authority.artifact.scope_themes`
  - `pending_authority.artifact.invariants`
  - `pending_authority.artifact.gaps`
  - `pending_authority.artifact.assumptions`
  - `pending_authority.artifact.rejected_features`
  - `pending_authority.artifact.eligible_feature_rules`
- Decision guards:
  - `guard_tokens.review_token`
  - existing explicit guard fields when provided by the packet
- Post-accept state:
  - `post_accept === true`

The renderer must tolerate missing optional fields and show explicit empty or
unavailable states.

## Security Requirements

- Render source text, invariant text, gaps, assumptions, findings, and raw JSON
  using `textContent` or DOM text nodes.
- Do not assign user-controlled spec or authority content to `innerHTML`.
- If markup is needed for static UI labels, build those labels from fixed
  strings only.
- Keep raw JSON in a `pre` element populated with text, not HTML.

## Accessibility Requirements

- Tabs should use buttons with clear labels.
- Active tab state should be visually distinct.
- Disabled decision buttons should include nearby explanatory text.
- Empty states should use text, not color alone.
- The decision summary should remain readable on narrow screens.

## Error Handling

- Review load failure should show the structured backend error message when
  present.
- Accept/reject stale errors should reload the review packet and show a concise
  explanation.
- Request refinement should reject empty reasons in the client before posting.
- If the review packet is missing source content because it was truncated, the
  UI should show the excerpt and truncation reason instead of an empty panel.

## Testing Strategy

Add focused tests around the UI and API behavior that matter for review safety:

- Dashboard API tests for pending and accepted review packet states.
- Dashboard API tests that structured setup errors remain available after state
  reloads.
- JavaScript DOM tests for:
  - summary counts and badges.
  - safe rendering of source/spec text containing `<script>`-like content.
  - empty states for gaps, assumptions, findings, and invariant lists.
  - accept/refinement visibility for pending, blocked, and accepted states.
  - invariant search and support filtering.

Final verification should include:

```bash
uv run --frozen pytest tests/test_api_dashboard.py -q
uv run --frozen node --test tests/test_authority_review_console.mjs
pyrepo-check
```

The JavaScript test should follow the existing repository convention used by
`tests/test_create_project_modal_required_fields.mjs`: `node:test`,
`node:assert/strict`, direct reads of `frontend/project.html`, and focused
assertions against exported or globally assigned dashboard functions.

## Rollout

Implement this as a dashboard-only enhancement. The backend authority decision
contract remains unchanged. The work should be reversible by restoring the prior
card rendering while leaving authority persistence and review endpoints intact.

The implementation is complete when a reviewer can open a project in
`authority_pending_review`, inspect the source and compiled authority artifacts,
accept or request refinement from the UI, and still inspect the accepted
authority read-only after Vision is unlocked.
