# Story Save State Mismatch

Type: bug / workflow / agent ergonomics

AgileForge platform HEAD: `45f8d69`

Reproduction repo: `/Users/aaat/projects/asa-deep-process-control-experiments`

Project: `3`

## Project-agnostic scope

This is AgileForge platform feedback. The ASA project is used only as a concrete dogfood reproduction fixture. Do not implement an ASA-specific fix.

Date: 2026-06-22

## Observed Workflow

`workflow next` advertised:

```bash
agileforge story pending --project-id 3
agileforge story generate --project-id 3 --parent-requirement <parent_requirement>
```

Because `Technology and Model Research Spike` had prior attempts, plain generation failed with:

- `ok=false`
- `INVALID_COMMAND`
- `User input is required to refine an existing story.`

After retrying with explicit input, Story generation/refinement returned:

- `ok=true`
- parent requirement: `Technology and Model Research Spike`
- `attempt_id=attempt-10`
- `artifact_fingerprint=sha256:5ca69869d52df6b201277432f34c7a7a91e9cc302bb5384e89330154f5609e0a`
- `story_count=8`
- `quality.coverage_status=complete`
- `quality.blocking_findings=[]`
- `quality.remaining_scope=[]`
- `quality.saveable=true`
- `is_reusable=true`
- `save.available=true`
- `save.expected_state=STORY_REVIEW`
- `fsm_state=STORY_INTERVIEW`

The agent then ran:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache-agileforge agileforge story save \
  --project-id 3 \
  --parent-requirement "Technology and Model Research Spike" \
  --attempt-id attempt-10 \
  --expected-artifact-fingerprint sha256:5ca69869d52df6b201277432f34c7a7a91e9cc302bb5384e89330154f5609e0a \
  --expected-state STORY_REVIEW \
  --idempotency-key asa-story-save-tech-research-authority8-20260622-001
```

## Actual Behavior

Save failed:

- `ok=false`
- first error code: `INVALID_COMMAND`
- message: `story save requires FSM state STORY_REVIEW`

After failure, `workflow next` still advertised only:

```bash
agileforge story pending --project-id 3
agileforge story generate --project-id 3 --parent-requirement <parent_requirement>
```

No valid transition to `STORY_REVIEW` or save command was advertised.

## Repeat Evidence

After refreshing live state on the same AgileForge HEAD, `story pending` still reported:

- `total_count=21`
- `saved_count=0`
- `Technology and Model Research Spike` status `Attempted`, `runs=10`

`story history --project-id 3 --parent-requirement "Technology and Model Research Spike"` reported:

- `attempt_id=attempt-10`
- `artifact_fingerprint=sha256:5ca69869d52df6b201277432f34c7a7a91e9cc302bb5384e89330154f5609e0a`
- `story_count=8`
- `quality.coverage_status=complete`
- `quality.saveable=true`
- `blocking_findings=[]`
- `remaining_scope=[]`
- `save.available=true`
- `save.expected_state=STORY_REVIEW`

Following the only `workflow next` route, the agent reran `story generate` with explicit current-scope refinement input. AgileForge returned the same saveable `attempt-10` and still reported:

- `fsm_state=STORY_INTERVIEW`
- `save.available=true`
- `save.expected_state=STORY_REVIEW`

The immediately subsequent `workflow next` still advertised only:

```bash
agileforge story pending --project-id 3
agileforge story generate --project-id 3 --parent-requirement <parent_requirement>
```

This confirms the blocker is not stale local memory; the current CLI route loops on generation and does not expose the save transition.

Additional checkout verification:

- local AgileForge branch: `master`
- local AgileForge platform HEAD: `45f8d69164d54343054bc2179d0fb62836d7ede2`
- `origin/master`: `45f8d69164d54343054bc2179d0fb62836d7ede2`

So this reproduction is against current local/origin master, not an unpulled or stale AgileForge checkout.

## Expected Behavior

If a generated/refined Story draft is complete and saveable:

1. The command response should either transition the FSM to `STORY_REVIEW`, or
2. `story save` should accept the current `STORY_INTERVIEW` state when using the returned `save.expected_state`, or
3. `workflow next` should advertise the required intermediate command to enter `STORY_REVIEW`.

The response should not return `save.available=true` with `save.expected_state=STORY_REVIEW` while the current FSM remains `STORY_INTERVIEW` and save rejects that exact state.

## Why It Matters

The agent followed the returned save metadata and still hit a guard failure. This blocks ASA Story progress and violates the "drive next action from workflow next" ritual.

## Suggested Fix

Align Story generation, workflow routing, and Story save guards:

- When `quality.saveable=true`, make `workflow next` include the exact `story save` command with current valid guards.
- Ensure `save.expected_state` matches the actual FSM state required by `story save`.
- If an intermediate review/transition command is required, expose it in both the generation response and `workflow next`.

## Platform Impact

Can block any AgileForge project at Story phase when a saveable Story draft is advertised but the persisted FSM remains outside `STORY_REVIEW`. The ASA project is the observed reproduction fixture, not the scope of the bug.
