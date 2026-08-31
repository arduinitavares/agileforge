# String Calculator Lab Dogfooding Plan

## Authority

This file is the sole operational plan for the current AgileForge dogfooding
campaign. It supersedes every earlier acceptance, testing, recovery, or
dogfooding plan for this campaign.

The campaign uses only:

- the AgileForge checkout identified in this file
- the synthetic String Calculator Lab repository
- the String Calculator first-release specification
- acceptance profiles created specifically for this campaign
- issues and Codex tasks created from findings in this campaign

Historical plans, unrelated repositories, unrelated profiles, and unrelated
project evidence are not inputs to this campaign. Current repository evidence,
current CLI and UI state, and this file control the work.

Changes to this plan require explicit human approval.

## Goal

Use the synthetic String Calculator Lab to exercise AgileForge end to end while
also delivering the calculator described by its first-release specification.

During the campaign:

1. Test each AgileForge lifecycle step through both CLI and UI evidence.
2. Make one human-visible decision at a time.
3. Record every confirmed bug and improvement as a GitHub issue.
4. Start a separate Codex task for each approved issue.
5. Pause for blocking findings and continue past non-blocking findings.
6. Integrate fixes only at explicit checkpoints.
7. Finish with a working calculator and an auditable record of AgileForge
   findings, fixes, decisions, and verification.

## Scope

### In scope

- AgileForge lifecycle behavior used by the String Calculator Lab
- browser, API, CLI, workflow, persistence, and launcher behavior observed by
  this campaign
- usability problems that make the next valid action unclear
- correctness problems that prevent or corrupt the next valid action
- isolated fixes, tests, reviews, and local integration for confirmed findings
- calculator implementation work selected through the accepted AgileForge
  lifecycle

### Out of scope

- any unrelated repository, project, profile, or user data
- importing evidence from a previous campaign run
- manual edits to AgileForge business or trace databases
- bypassing lifecycle guards to force progress
- provider calls, artifact decisions, Sprint decisions, merges, pushes, issue
  closure, or cleanup without explicit human approval
- changing calculator scope outside the accepted first-release specification

## Roles

### Human operator

The human operator owns:

- provider-call approval
- accept, request-changes, and reject decisions
- Product Goal and Sprint decisions
- approval to create an issue or Codex task
- approval to integrate a fix
- approval to push, merge, close issues, or clean branches and worktrees
- any decision to reuse, clone, reset, or abandon campaign state

### Codex guide

The Codex guide owns:

- exact checkout, SHA, profile, and database preflight
- bounded CLI inspection before and after each mutation
- matching the CLI result to the visible UI
- explaining exactly one next action in plain language
- collecting reproducible evidence for findings
- drafting issues and dispatching approved Codex tasks
- monitoring blocking tasks and checking completed task claims
- maintaining the current checkpoint in this file

The guide does not silently perform a human-owned action.

### Codex fix task

Each fix task:

- is scoped to one approved GitHub issue
- starts from the exact approved AgileForge base SHA
- uses an isolated branch and worktree when repository work is required
- follows the repository model-selection policy explicitly
- uses synthetic, provider-free evidence unless the human separately approves a
  provider or manual acceptance action
- adds focused regression coverage and runs the required repository gate
- reports its parent SHA, final SHA, branch, worktree, tests, and remaining
  boundaries
- does not push, merge, close the issue, mutate the campaign profile, or clean
  campaign branches without approval

## Evidence Rules

Use evidence in this order:

1. Current checkout identity and profile provenance.
2. Current `workflow position` and `workflow next` CLI results.
3. Current browser projection for the same project and lifecycle state.
4. Current API responses when CLI and UI need contract-level comparison.
5. Read-only database integrity or identity checks when persistence is at issue.
6. Code, tests, and task reports pinned to the exact observed SHA.

Current evidence wins over conversation history, memory, screenshots from an
older run, or a task's completion claim.

Do not print or store credential values. Keep large JSON responses in temporary
files and report only bounded fields, counts, IDs, fingerprints, decisions, and
errors.

## Current Campaign Checkpoint

Recorded on 2026-08-31.

| Item | Current value |
| --- | --- |
| AgileForge checkout | `/Users/aaat/projects/agileforge` |
| AgileForge branch | `dev/issue-218-progressive-story-readiness` |
| AgileForge SHA | `14b1a9002cd9976b0ed8f78ac90ee037f75dc826` |
| Acceptance profile | `manual-string-calculator-e2e-14b1a90` |
| Profile mode | `acceptance` |
| Project ID | `1` |
| Dashboard | `http://127.0.0.1:60857/dashboard/project.html?id=1` |
| Calculator repository | `/Users/aaat/projects/string-calculator-lab` |
| Calculator branch | `dev/string-calculator-v1` |
| Calculator SHA | `d93e1af369ea3e924b1e320f6c65eb7ff584767e` |
| Calculator specification | `/Users/aaat/projects/string-calculator-lab/docs/spec/string-calculator-first-release.md` |
| Repository state | Clean |
| Project state | Fresh project created and repository attached |
| Available workflow node | `vision.bootstrap` |
| Invalid workflow nodes | None |
| Current provider state | Server running without provider credentials |
| Next mutation | Generate the first Vision draft after explicit provider approval |

No finding is open in the fresh campaign run.

## One-Action Operating Loop

Repeat this loop for every lifecycle mutation.

### 1. Preflight

Before proposing a mutation, Codex records:

- AgileForge branch, full SHA, and clean or dirty state
- calculator branch, full SHA, and clean or dirty state
- acceptance profile name, mode, expected SHA, and database paths
- current Project ID
- `workflow position` available, blocked, waiting, and invalid nodes
- `workflow next` recommended command
- matching UI action and visible state
- whether the action invokes a provider or requires a human decision

The standard reads use the checkout-local launcher:

```sh
./agileforge-dev info \
  --profile manual-string-calculator-e2e-14b1a90 \
  --json

./agileforge-dev cli \
  --profile manual-string-calculator-e2e-14b1a90 \
  --json -- \
  workflow position --project-id 1

./agileforge-dev cli \
  --profile manual-string-calculator-e2e-14b1a90 \
  --json -- \
  workflow next --project-id 1
```

When credentials are required, pass the approved external secrets file through
`--secrets-file "$AGILEFORGE_SECRETS_FILE"`. Never inspect or record its
contents.

### 2. Explain

Codex states:

- what the next action does
- why it is the current valid action
- whether it calls a provider
- what durable state it should create or change
- what the UI should show while running and after completion
- what human decision will follow

Codex gives one instruction, not a sequence of clicks.

### 3. Decide

The human either:

- approves Codex to perform the action
- performs the visible UI action and reports completion
- declines the action
- asks for more evidence

No approval is inferred from an earlier lifecycle step.

### 4. Verify

After the action, Codex checks:

- command or API outcome
- updated `workflow position`
- updated `workflow next`
- exact artifact, attempt, decision, and fingerprint identities when present
- the corresponding browser state
- whether either repository changed
- whether replaying the same request is safe when replay behavior matters

The next step is not proposed until CLI and UI agree or the disagreement is
classified as a finding.

### 5. Record and continue

Codex updates the campaign checkpoint at lifecycle boundaries and records any
finding using the rules below.

## Finding Classification

### Blocking finding

A finding is blocking when any of these is true:

- no safe valid next action is available
- the advertised action cannot execute against the current durable state
- the action mutates the wrong project, artifact, item, decision, or attempt
- state, identity, lineage, fingerprint, replay, or tamper guarantees fail
- CLI and UI disagree in a way that prevents a safe human decision
- continuing could corrupt state, hide evidence, or invalidate the campaign
- the only apparent workaround bypasses a lifecycle guard or edits a database

Response:

1. Freeze mutations in the campaign profile.
2. Preserve exact CLI, UI, API, server, SHA, and profile evidence.
3. Draft one narrowly scoped GitHub issue.
4. Ask for approval to create the issue and a separate Codex task.
5. Wait for the task when no safe campaign action remains.
6. Verify and integrate the fix only through the checkpoint process.

### Non-blocking bug

A bug is non-blocking only when durable state is correct, the next action is
safe and unambiguous, and continuing cannot invalidate later evidence.

Response:

1. Preserve reproduction evidence.
2. Draft one narrowly scoped GitHub issue.
3. Ask for approval to create the issue and Codex task.
4. Continue the current pinned campaign without integrating the fix.
5. Review the completed task at the next explicit integration checkpoint.

### Improvement

An improvement is behavior that is correct and safe but unnecessarily unclear,
inefficient, or difficult to audit.

Improvements follow the non-blocking flow unless the lack of clarity prevents a
safe human decision, in which case they are blocking.

### Unclear classification

Treat an unclear finding as blocking until CLI, UI, API, and code evidence prove
that continuing is safe.

## GitHub Issue Contract

Every campaign issue contains:

- a concise observed-behavior title
- exact AgileForge branch, parent SHA, and observed SHA
- exact acceptance profile and Project ID
- exact calculator SHA when relevant
- lifecycle node and advertised command
- preconditions
- minimal reproduction steps
- expected behavior
- actual behavior and exact error code or message
- bounded CLI and API evidence
- screenshot or visible UI evidence when applicable
- blocker, non-blocker, or improvement classification with rationale
- focused acceptance criteria
- explicit data and authority boundaries

Codex drafts the issue first. The human approves issue creation. Issue creation
does not authorize implementation, provider access, integration, push, merge,
closure, or cleanup.

## Codex Task Contract

After issue approval, Codex creates one separate task with:

- the issue URL and exact scope
- the required base SHA
- the expected branch prefix `dev/`
- the reproduction evidence
- acceptance criteria
- focused and full verification requirements
- independent correctness and lean-review requirements
- explicit prohibitions on provider calls, campaign-profile mutation, push,
  merge, issue closure, and cleanup

For a blocking issue, the campaign waits for task completion or a request for
human input. For a non-blocking issue or improvement, the campaign may continue
at the current pinned SHA while the task runs.

A task's final message is a claim to verify, not integration authority.

## Fix Review and Integration Checkpoint

When a fix task reports completion:

1. Verify its branch, worktree, parent SHA, final SHA, and cleanliness.
2. Inspect the focused diff and issue acceptance criteria.
3. Confirm focused tests and the full repository gate passed.
4. Confirm independent reviews have no unresolved findings.
5. Run fresh human acceptance when the change affects visible or interactive
   behavior.
6. Ask the human to approve or reject local integration.
7. Stop the campaign server before changing the tested AgileForge SHA.
8. Integrate only the approved commit or commits.
9. Record the new exact AgileForge SHA.
10. Create a new acceptance profile for the new SHA. Never repin an existing
    acceptance profile silently.

Profile-state policy after integration:

- Use a fresh empty profile and replay for workflow, persistence, identity,
  binding, guard, or state-transition changes.
- A database may be cloned into the new profile only for a change proven not to
  alter those contracts, after read-only integrity checks and explicit human
  approval.
- Never hand-edit or transplant individual database rows.

After acceptance passes, ask separately for approval to update or close the
issue. Preserve campaign branches and worktrees until campaign completion or
explicit cleanup approval.

## Lifecycle Campaign Tasks

### Phase 0: Fresh foundation

- [x] Preserve the first-release calculator specification.
- [x] Reset the old synthetic campaign database.
- [x] Create a fresh acceptance profile pinned to the reviewed AgileForge SHA.
- [x] Create Project `1` for String Calculator Lab.
- [x] Attach the clean calculator repository.
- [x] Verify CLI and UI agree that `vision.bootstrap` is next.

### Phase 1: Project Vision

- [ ] Approve and start a credentialed server for the Vision provider action.
- [ ] Preflight CLI and UI immediately before generation.
- [ ] Generate exactly one Vision draft.
- [ ] Verify draft identity, provenance, CLI state, and UI rendering.
- [ ] Make one human accept, request-changes, or reject decision.
- [ ] Verify the resulting durable Vision state.

### Phase 2: Product Goal

- [ ] Verify the accepted Vision is the exact Product Goal input.
- [ ] Generate exactly one Product Goal draft.
- [ ] Verify identity, provenance, CLI state, and UI rendering.
- [ ] Make one human decision.
- [ ] Verify the active accepted Product Goal.

### Phase 3: Specification

- [ ] Confirm the registered source is the calculator first-release
  specification in the attached repository.
- [ ] Register the exact Specification source through the advertised command.
- [ ] Structure exactly one Specification candidate.
- [ ] Verify the complete typed payload, source lineage, fingerprints, and
  deterministic review projection.
- [ ] Make one human decision.
- [ ] Verify the exact accepted Specification becomes the delivery contract.

### Phase 4: Backlog

- [ ] Generate Backlog from the accepted Product Goal and Specification.
- [ ] Verify every proposed item is grounded in the accepted contract.
- [ ] Make one human decision.
- [ ] Verify the accepted Backlog identity and lineage.

### Phase 5: Roadmap

- [ ] Generate Roadmap from the accepted Backlog and Specification.
- [ ] Verify ordering, scope, lineage, and review rendering.
- [ ] Make one human decision.
- [ ] Verify the accepted Roadmap.

### Phase 6: Stories and readiness

- [ ] Generate Stories for one accepted Backlog item at a time.
- [ ] Verify Story shape, source binding, INVEST evidence, sizing, and ordering.
- [ ] Make one human decision for each Story set.
- [ ] Verify dependency evidence and structural readiness.
- [ ] Select only the human-approved Stories for the next Sprint.
- [ ] Confirm dependencies for the exact selected scope.

### Phase 7: Sprint planning

- [ ] Verify the exact candidate pool and total points.
- [ ] Generate one Sprint plan for the approved candidate scope.
- [ ] Verify ownership, capacity, dependencies, source identities, and review
  rendering.
- [ ] Make one human decision.
- [ ] Start the Sprint only after separate human approval.

### Phase 8: Calculator delivery

- [ ] Use the active Sprint tasks to drive calculator implementation.
- [ ] Keep implementation changes in the calculator repository only.
- [ ] Preserve visible red-green evidence at the public Python and CLI seams.
- [ ] Verify task completion evidence before each Done transition.
- [ ] Close Stories only when their accepted criteria are satisfied.

### Phase 9: Verification and review

- [ ] Run the calculator's specified uv-only quality gate.
- [ ] Record the exact verified calculator commit.
- [ ] Review and close the Sprint through human decisions.
- [ ] Record post-Sprint learning and issue dispositions.
- [ ] Fulfill or explicitly continue the Product Goal according to current
  workflow evidence.

### Phase 10: Campaign completion

- [ ] Confirm the calculator satisfies the accepted first-release contract.
- [ ] Confirm every campaign finding has an issue and explicit disposition.
- [ ] Record final AgileForge and calculator SHAs.
- [ ] Record final test and acceptance evidence.
- [ ] Ask for separate approval before push, merge, issue closure, server stop,
  profile removal, branch deletion, or worktree cleanup.

## Campaign Checkpoints

Stop for a human checkpoint:

- after every generated artifact is ready for review
- after every human artifact decision
- when CLI and UI do not agree
- when any finding is discovered
- before creating an issue or Codex task
- before integrating a completed fix
- before changing AgileForge SHA or acceptance profile
- before starting or closing a Sprint
- before any push, merge, issue closure, or cleanup

At each checkpoint, Codex reports only:

1. What happened.
2. What CLI proves.
3. What UI proves.
4. Whether a finding exists.
5. The single next decision.

## Campaign Finding Register

The fresh campaign currently has no recorded findings.

When a finding is approved, add one row:

| Issue | Classification | Lifecycle step | Blocks campaign | Fix SHA | Acceptance status |
| --- | --- | --- | --- | --- | --- |

Do not add unconfirmed suspicions or findings imported from another run.

## Definition of Done

The campaign is complete only when:

- the String Calculator Lab satisfies its accepted first-release Specification
- the public Python operation and installed CLI pass the specified quality gate
- the AgileForge lifecycle reaches the human-approved completion state
- every campaign finding has an explicit issue disposition
- exact AgileForge and calculator SHAs are recorded
- provider, human-decision, integration, and cleanup boundaries were preserved
- the human explicitly approves campaign completion and subsequent cleanup

## Next Approved Decision

The current server is intentionally running without provider credentials. The
next decision is whether to stop it and start the same acceptance profile with
the approved external secrets file so that exactly one Vision draft can be
generated. No provider action is approved by this document alone.
