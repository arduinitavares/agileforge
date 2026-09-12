# Lifecycle map

Use this map for orientation. It groups transitions and omits recovery branches;
it is not a command sequence to execute without rereading workflow authority.

```text
Create Project -> Vision -> Product Goal
  -> Specification: register exact source, structure, review
  -> Backlog -> Roadmap -> Stories
  -> Sprint: select scope, check readiness/dependencies, plan, review, start
  -> Execute Tasks and record evidence -> Close Stories
  -> Review Sprint -> Close Sprint -> Record post-Sprint triage
```

- Vision, Goal, Specification, Backlog, Roadmap, Stories, and Sprint plans have
  distinct review steps. Feedback or rejection may require a revised candidate.
  Prior authorization remains valid for its scope; acceptance of an old candidate
  does not accept changed content or lineage.
- Specification starts from exact registered source bytes, then provider-backed
  structuring and human review of `agileforge.spec.v2`. External preparation is
  evidence, not an accepted delivery contract or a separate Discovery gate.
- Story content acceptance alone does not make a Story Sprint-ready. Selection,
  sizing/rank, structural evidence, and dependency review also matter.
- Execution and Story closure can interleave within a Sprint. Work on the current
  Task until evidence is ready; record completion separately from Story closure.
  Resolve acceptance gaps within authorized scope or report them. A loop in the
  diagram does not authorize reopening a completed Task or rewriting evidence.
- Sprint review, Sprint closure, and post-Sprint triage are separate transitions.
  Record learning and impact honestly; triage does not itself revise a Backlog,
  amend a Specification, or start another Sprint.
- Continue under the same Project. Reuse current accepted planning where valid,
  select eligible remaining Stories, and follow readiness/dependency gates before
  another Sprint. Do not recreate the Project or repeat all planning by default.
- Changed requirements may need a source amendment, structuring, and review.
  Amendment availability requires a later completed Sprint and a lifecycle with
  no active Sprint/retry or unresolved reviews/triage. Inspect the current route;
  never infer permission to amend from a triage impact label alone.
- The diagram omits optional Goal/Vision changes and Sprint retry. A retry is a
  distinct attempt with fresh evidence and preserved history, not a next Sprint.
  Use only an exposed, eligible route under the user's authority.

Read both `workflow position` and `workflow next` before a mutation and after its
successful result. Use the returned command template. If the needed route is not
exposed, report the blocker instead of constructing a command from this map.

Use [specification-handoff.md](specification-handoff.md) for planning decisions,
[sprint-delivery.md](sprint-delivery.md) for execution and completion evidence,
and [approvals-and-recovery.md](approvals-and-recovery.md) for recovery boundaries.
