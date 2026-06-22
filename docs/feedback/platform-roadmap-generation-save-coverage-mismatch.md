# Roadmap Generation/Save Coverage Mismatch

Type: bug / workflow / agent ergonomics

AgileForge platform HEAD: `45f8d69`

Reproduction repo: `/Users/aaat/projects/asa-deep-process-control-experiments`

Project: `3`

## Project-agnostic scope

This is AgileForge platform feedback. The ASA project is used only as a concrete dogfood reproduction fixture. Do not implement an ASA-specific fix.

Date: 2026-06-22

## Observed Command

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge agileforge roadmap generate --project-id 3
```

Generated:

- `ok=true`
- `attempt_id=roadmap-attempt-3`
- `artifact_fingerprint=sha256:4aabe107c96433011cdd45899aa178f29954bd675dbbe8a0076cb0d34f689a8a`
- `is_complete=true`
- `fsm_state=ROADMAP_REVIEW`
- 3 releases
- 0 clarifying questions

Then guarded save failed:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge agileforge roadmap save \
  --project-id 3 \
  --attempt-id roadmap-attempt-3 \
  --expected-artifact-fingerprint sha256:4aabe107c96433011cdd45899aa178f29954bd675dbbe8a0076cb0d34f689a8a \
  --expected-state ROADMAP_REVIEW \
  --idempotency-key asa-roadmap-save-authority8-20260622-001
```

Result:

- `ok=false`
- first error code: `INVALID_COMMAND`
- message: `Roadmap coverage mismatch`
- missing:
  - `Canonical Process Event Record Definition and Validation`
  - `Delayed Temperature Reward Scoring`
  - `Offline Recommender System`
  - `Python Project Scaffold and uv Management Setup`
  - `Raw Data Ingestion Pipeline`
  - `State Window Feature Generation`
  - `Technology and Model Research Spike`
  - `pyrepo-check Quality Gate Integration`

## Expected Behavior

Roadmap generation should not return `is_complete=true` if the produced roadmap cannot pass the guarded save coverage check.

At minimum, the generated draft should either:

- include all required backlog items, or
- return incomplete / needs-refinement with the same missing coverage list surfaced before save.

## Actual Behavior

Generation produced a saveable-looking complete draft, but the save guard rejected it for missing backlog items.

The roadmap appeared to treat the scope extension as "Milestone 4-6" and omitted already-foundational backlog items, while the save guard required full current backlog coverage.

## Why It Matters

This makes the agent/user trust the generated roadmap, then fail at save time. It causes extra refinement loops and makes it unclear whether AgileForge wants:

- a full current backlog roadmap, or
- an additive scope-extension roadmap that builds on already-completed base milestones.

## Suggested Fix

Align roadmap generation and roadmap save coverage rules:

1. If save requires all current backlog items, inject that coverage requirement into the roadmap prompt and validate before returning `is_complete=true`.
2. If scope-extension roadmaps are allowed to cover only new/additive items, make the save guard scope-aware and expose the expected coverage set in `workflow next` or the roadmap generation response.
3. Surface the missing coverage list in the generation response as a quality finding instead of waiting until save.

## Platform Impact

Can block any AgileForge project at `ROADMAP_REVIEW` when roadmap generation and roadmap save disagree on required coverage. The ASA project is the observed reproduction fixture, not the scope of the bug.
