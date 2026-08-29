# Specification Source State Plan

**Issue:** #211

**Base:** `04fcf28df7cc25d5589854679e457b2dc1142f10`

## Objective

Show the currently registered Specification source as durable browser state. Keep registration of a future revision clearly secondary once a source exists.

## Design

Render a presentation-only `Current registered Specification source` summary from the existing `projection.source` data:

- repository-relative source path;
- ADR paths, or `No ADRs`;
- preparation capability;
- an accessible current/registered status.

When a current source exists, place the existing blank registration form in a closed native `<details>` disclosure labelled `Register a revised source`. Initial registration remains directly visible when no source exists.

The summary remains visible before structuring, while structuring is busy, after structuring failure, during Feedback recovery, and during human review. While structuring is busy, close and disable the secondary disclosure and its controls. Restore it after failure.

Do not display source fingerprints, internal IDs, repository revision data, source bytes, or lineage internals.

## State Contract

| State | Current source | Registration | Primary action |
| --- | --- | --- | --- |
| No source | Absent | Initial form visible | Register source |
| Registered, no candidate | Visible | Closed secondary disclosure | Structure Specification |
| Structuring busy | Visible | Closed, inert, disabled | Busy status; structure disabled |
| Structuring failure | Visible | Secondary disclosure restored | Retry/recovery |
| Feedback | Visible | Secondary disclosure only for revision | Retry or structure revised source |
| Pending review | Visible | Only when replacement is advertised | Human review controls |

## Task 1: RED Browser Contract

Modify `tests/e2e/test_single_project_lifecycle_ui.py`.

1. Make the fake source projection match the production `source` shape.
2. Add an issue #211 lifecycle test proving path, ADRs or `No ADRs`, preparation capability, and accessible status.
3. Prove the revised-source form is hidden until its labelled disclosure opens.
4. Prove busy structuring keeps the summary visible while closing and disabling revision registration.
5. Prove failure, Feedback, and pending review retain the summary.
6. Extend the existing issue #204 busy/failure test only where needed to lock the shared behavior.

Run and capture the expected failure before production edits:

```bash
uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py -k 'issue_211 or issue_204'
```

## Task 2: GREEN Renderer

Modify `frontend/project.js`.

1. Add a focused, escaped current-source summary helper.
2. Add a native revised-registration disclosure helper.
3. Compose the summary into every Specification panel branch that has a current source.
4. Extend existing busy handling to close and disable only the secondary registration disclosure and controls.
5. Preserve all bindings, payloads, expected-decision headers, retry modes, workflow-state decisions, and live status behavior.

## Verification

```bash
uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py -k 'issue_211 or issue_204'
uv run --frozen pytest -q tests/test_frontend_package_resources.py
node --test tests/*.mjs
git diff --check
```

After focused checks pass, commit locally so the checkout is clean, then run:

```bash
./agileforge-dev check --json
```

## Expected Production Scope

- `frontend/project.js`
- `tests/e2e/test_single_project_lifecycle_ui.py`

No provider, API, projection, persistence, schema, source identity, lineage, retry, or human-decision changes are expected.
