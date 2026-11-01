# Recover a stale, unstarted Sprint Plan

An accepted Sprint Plan can become stale when its reviewed dependencies or
planning facts change. `workflow next` then reports `SPRINT_PLAN_STALE`, and
start refuses the obsolete plan. The dashboard offers **Retire stale Sprint
Plan** when the accepted Sprint is still unstarted.

Retirement is explicit. Select the displayed plan, enter a rationale, and submit
the retirement action. AgileForge preserves the accepted artifact and decision,
dependency reviews, Sprint membership, Tasks, and audit receipts. The old Sprint
remains historical and can never start. A started or completed Sprint cannot use
this recovery.

To use the CLI from a development checkout, inspect the selected profile first:

```sh
./agileforge-dev info --profile local --json
./agileforge-dev cli --profile local -- workflow next --project-id 1
```

Use the returned `sprint plan retire` command. It contains the exact plan,
acceptance decision, Sprint, plan fingerprint, and current decision fingerprint.
Supply your actor, rationale, and a new idempotency key. Repeating the exact
request with the same key replays its original outcome. Reload after a stale
guard rejection instead of substituting another plan's identifiers.

After retirement, explicitly reconcile the dependencies and choose the next
scope. Generate a replacement plan, review it, and accept it before starting
its new Sprint. Retirement does not confirm dependencies or accept a replacement.

The API exposes the same action at
`POST /api/projects/{project_id}/sprint/retire`. Its JSON body requires
`sprint_plan_artifact_id`, `sprint_plan_artifact_decision_id`, `sprint_id`,
`plan_fingerprint`, `expected_decision_fingerprint`, `actor`, `rationale`, and
`idempotency_key`. Obtain the binding from the current Workflow Position.

Sprint status reports `effective_status: "retired"` and the retirement evidence.
The original Sprint row and acceptance remain unchanged. Sprint history includes
the retirement records beside the existing planning and execution history.
