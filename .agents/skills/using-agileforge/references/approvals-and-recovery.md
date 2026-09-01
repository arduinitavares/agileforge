# Approvals, defects, and recovery

Use this reference whenever authority, state, or failure handling is unclear.

## Authority rules

Proceed without another approval for read-only local inspection and for an action the user already named precisely. Examples include creating a missing local AgileForge project after the user said to create it, or editing the target repository after the user said to implement the active Task. Do not turn step-by-step guidance into repeated approval for the same granted action.

Do not choose these decisions for the user:

- product clarification or acceptance decisions
- Story selection, deferral, dependencies, or sizing corrections
- Sprint scope, plan acceptance, start, review, close, or triage

Require explicit authority for:

- provider-backed calls, unless a bounded series was already authorized
- creating or changing an external issue, unless the user authorized ongoing issue filing for the campaign
- creating a separate Codex task to fix AgileForge
- local commits when not already requested or required by the agreed workflow
- merge, rebase, push, branch deletion, worktree removal, release, or deployment
- destructive database or profile operations

Never interpret skill invocation as blanket authority.

## Stale state and retries

When a mutation reports a stale or changed workflow position:

1. Stop using the old command.
2. Read `workflow position` and `workflow next` again.
3. Review the new reason code and template.
4. Use a fresh idempotency key for the new distinct request.

When a transport result is uncertain, retry only the exact same request with the same idempotency key. A changed semantic field requires a new key.

Save potentially large CLI JSON to a temporary file and print only selected scalar fields, key names, counts, short error text, and command templates. Do not dump nested packets merely to inspect them, and do not delete a response before parsing it.

For provider rate limits or transient upstream failures, preserve the provider's safe error category and retry guidance. Do not report the event as an AgileForge product defect merely because the provider failed. It is an AgileForge defect when AgileForge masks the cause, violates its retry policy, corrupts state, or gives no safe recovery path.

## Dogfooding defects

First reproduce the behavior with bounded CLI and UI evidence. Separate AgileForge behavior from target-repository, configuration, provider, and environment failures.

Treat a finding as blocking when any of these apply:

- the required workflow action cannot complete safely
- state, provenance, binding, or acceptance evidence is uncertain
- the only workaround bypasses a required decision or corrupts durable state
- continuing would invalidate the active Task or destroy useful evidence

Treat it as non-blocking only when a verified workaround preserves workflow state and the active Task contract. Continue the target work and keep the finding visible.

An issue should include:

- exact observed and expected behavior
- minimal reproduction
- CLI JSON, UI screenshot, or test evidence
- AgileForge runtime/profile, project, and reason code
- target repository branch and HEAD when relevant
- blocking classification and workaround
- secrets and protected data explicitly excluded

If ongoing issue filing was not authorized, draft the issue and ask before creating it. Do not edit AgileForge from the target-repository task. Propose a separate Codex task and create it only when the user asks.

## CLI and UI disagreement

After confirming both views use the same runtime, profile, and project, the fresh CLI route remains authoritative. The UI never overrides it. Preserve both views, avoid further mutation when the mismatch affects decisions or durable state, and register the disagreement as a defect. A cosmetic mismatch may be non-blocking; a control that advertises an invalid or unsafe action is blocking.
