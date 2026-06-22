# Story Generate Existing Attempt Needs Input

Type: bug / UX / workflow

AgileForge platform HEAD: `45f8d69`

Reproduction repo: `/Users/aaat/projects/asa-deep-process-control-experiments`

Project: `3`

## Project-agnostic scope

This is AgileForge platform feedback. The ASA project is used only as a concrete dogfood reproduction fixture. Do not implement an ASA-specific fix.

Date: 2026-06-22

## Observed Workflow

After saving the corrected roadmap, `workflow next` advertised:

```bash
agileforge story pending --project-id 3
agileforge story generate --project-id 3 --parent-requirement <parent_requirement>
```

`story pending` showed `Technology and Model Research Spike` as `Attempted` with `10` prior runs and no saved current story coverage.

The agent followed the advertised generation route:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge agileforge story generate \
  --project-id 3 \
  --parent-requirement "Technology and Model Research Spike"
```

## Actual Behavior

The command failed:

- `ok=false`
- first error code: `INVALID_COMMAND`
- message: `User input is required to refine an existing story.`

No attempt id or artifact id was returned.

## Expected Behavior

`workflow next` or `story pending` should make the required command shape clear when a requirement already has attempts.

Possible expected command:

```bash
agileforge story generate \
  --project-id 3 \
  --parent-requirement "Technology and Model Research Spike" \
  --input <specific refinement feedback>
```

Alternatively, if an attempted requirement has a reusable/saveable current draft, `workflow next` should advertise the corresponding save command instead of a generate command that fails without input.

## Why It Matters

The agent followed AgileForge's advertised next route, but the command failed because hidden state made `--input` mandatory. This breaks the "drive from workflow next" ritual and forces undocumented recovery knowledge.

## Suggested Fix

When a parent requirement has prior story attempts and new input is mandatory:

1. `workflow next` should advertise `story generate --input <feedback>` or explain that feedback is required.
2. `story pending` should expose whether the next action is first generation, refinement with required feedback, or save existing reusable draft.
3. The `INVALID_COMMAND` response should include a suggested command shape and the latest attempt id/status.

## Platform Impact

Can block any AgileForge project where `workflow next` advertises `story generate` while hidden prior-attempt state requires explicit refinement input. The ASA project is the observed reproduction fixture, not the scope of the bug.
