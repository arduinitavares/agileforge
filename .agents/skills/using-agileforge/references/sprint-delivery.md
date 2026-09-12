# Sprint delivery

Use this procedure only after AgileForge reports an active Sprint execution action.

## Bind work to the exact Task

Reread `workflow position` and `workflow next`. Inspect the advertised Task and its parent Story through current read commands such as `sprint task show`, `sprint tasks`, and `story show`. Do not select a Task from rank, UI order, or a previous session.

Record:

- Sprint, Story, and Task identities
- Task checklist
- parent Story acceptance criteria
- relevant dependencies and known gaps
- current target repository branch, HEAD, and dirty state

If the Task packet is missing, stale, or inconsistent with the accepted Story, stop. Do not implement from a title alone.

## Implement in the target repository

The target repository's `AGENTS.md`, local skills, build system, and test commands govern implementation. AgileForge does not replace them.

When the user has asked to implement active Sprint work:

1. Protect unrelated changes and use the repository's worktree policy.
2. Make a narrow plan for the current Task.
3. For behavior changes, establish a failing test first when the repository supports automated testing.
4. Write only the code needed for the Task and Story acceptance criteria.
5. Run focused checks, then the repository's required completion gate.
6. Review the diff against the Task packet.

Do not invent generic commands such as `pytest`, `npm test`, or `./agileforge-dev check` for the target repository. Discover and use its documented commands.

Commit only when the user request or target workflow authorizes a local commit. Never merge or push under that authority.

## Record completion

Before any completion mutation, reread both workflow routes. Use the exact advertised completion command and report only evidence that exists:

- verified commit or working-tree state
- files or artifacts actually changed
- fresh test and quality-gate results
- checklist results using the exact checklist text
- partial acceptance or known gaps

Parse the result. Do not discard unparsed JSON. Then reread both routes.

Do not close a Story, review or close a Sprint, or record triage merely because all visible Tasks appear complete. Perform those actions only when currently advertised and authorized.

## Continue after triage

Use the [lifecycle map](lifecycle.md) to distinguish another Sprint, upstream
revision, and a retry. Continue under the same Project and reread both routes.
Triage records learning and impact; it does not itself apply follow-up changes.
Existing accepted planning may remain usable, but Story selection, readiness,
and dependency checks still govern the next Sprint. Amend requirements only
through the eligible source registration, structuring, and review route.
