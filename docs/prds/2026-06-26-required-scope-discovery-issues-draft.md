# Required Scope Discovery Issue Drafts

Status: published to GitHub issues
Source PRD: `docs/prds/2026-06-26-required-scope-discovery-prd.md`

Published issues:

- #163: https://github.com/arduinitavares/agileforge/issues/163
- #164: https://github.com/arduinitavares/agileforge/issues/164
- #165: https://github.com/arduinitavares/agileforge/issues/165
- #166: https://github.com/arduinitavares/agileforge/issues/166
- #167: https://github.com/arduinitavares/agileforge/issues/167
- #168: https://github.com/arduinitavares/agileforge/issues/168
- #169: https://github.com/arduinitavares/agileforge/issues/169
- #170: https://github.com/arduinitavares/agileforge/issues/170
- #171: https://github.com/arduinitavares/agileforge/issues/171

These are vertical-slice issue drafts. Each slice should leave AgileForge in a
demoable state across persisted state, service behavior, command metadata, CLI
wiring, workflow guard behavior, and tests.

## MVP Breakdown

1. Persist minimal Challenge Artifacts with `grill-with-docs` provenance
2. Validate rich Challenge Artifact evidence and Project Glossary readiness
3. Draft PRDs from ready Challenge Artifacts with required `to-prd` provenance
4. Accept, reject, and version PRDs without mutating accepted PRDs
5. Record and validate agent-generated Spec Amendment Drafts from accepted PRDs
6. Start Project Scope Extension only from accepted validated Spec Amendments
7. Route exhausted existing projects through Scope Discovery in `workflow next`
8. Surface discovery provenance through authority review and execution blockers

## Post-MVP Breakdown

9. Apply required Scope Discovery to greenfield project creation

Greenfield remains part of the product decision, but it should not be in the
first implementation batch. It needs a provisional discovery context because no
project ID exists yet. Existing project scope extension validates the core
`grill-with-docs -> to-prd -> Spec Amendment -> Authority` pipeline first.

## Draft Issue 1: Persist Minimal Challenge Artifacts with `grill-with-docs` Provenance

**Blocked by**: None - can start immediately

**User stories covered**: 1, 3, 5, 6, 8, 13, 14, 36, 37, 38, 39, 40, 41

### What to build

Add the smallest complete Scope Discovery path for existing AgileForge projects:
a command that records a Challenge Artifact produced by `grill-with-docs`,
stores it in AgileForge-owned project state, and returns a stable artifact ID.

This slice should prove the persisted state, service boundary, app facade,
command metadata, CLI wiring, producer hard-fail rule, idempotency behavior, and
basic status handling end to end. It should not yet enforce every rich evidence
or Project Glossary rule; that is the next slice.

The completed slice is demoable by saving a minimal Challenge Artifact for an
existing project and seeing AgileForge return an artifact ID, readiness, producer
provenance, and next-action guidance.

### Acceptance criteria

- [ ] AgileForge can persist a minimal Challenge Artifact for an existing project.
- [ ] The artifact records project ID, original idea, producer provenance, readiness, content payload, created metadata, and idempotency metadata.
- [ ] Readiness is limited to `blocked`, `needs_answers`, and `ready_for_prd`.
- [ ] Artifacts that do not declare `grill-with-docs` as the Challenge Producer are rejected.
- [ ] The command returns a structured envelope with Challenge Artifact ID, readiness, producer, and next action.
- [ ] Mutating saves are idempotent and retry-safe.
- [ ] Command schema and CLI tests cover required inputs, mutating behavior, idempotency, and producer error codes.
- [ ] Service tests cover successful minimal persistence and producer rejection.

## Draft Issue 2: Validate Rich Challenge Artifact Evidence and Project Glossary Readiness

**Blocked by**: Draft Issue 1

**User stories covered**: 5, 10, 11, 12, 13, 14, 15, 16, 38

### What to build

Add the rich validation that makes a Challenge Artifact trustworthy enough to
drive PRD creation. `ready_for_prd` should mean the artifact contains sufficient
challenge evidence and has resolved Project Glossary obligations.

This slice should keep the same command path introduced in Issue 1, but harden
validation for questions, answers, reviewed evidence, evidence conflicts,
assumptions, non-goals, risks, open questions, and glossary changes. It should
fail closed when an artifact claims readiness while unresolved evidence or
glossary work remains.

The completed slice is demoable by saving a rich `ready_for_prd` artifact, then
seeing invalid ready artifacts rejected with structured blocking issues.

### Acceptance criteria

- [ ] A Challenge Artifact records questions, answers, reviewed evidence, evidence conflicts, assumptions, non-goals, risks, open questions, and glossary changes.
- [ ] A `ready_for_prd` artifact is rejected when required evidence fields are missing.
- [ ] A `ready_for_prd` artifact is rejected when open questions remain unresolved.
- [ ] A `ready_for_prd` artifact is rejected when evidence conflicts remain unresolved.
- [ ] A `ready_for_prd` artifact is rejected when it declares new or changed glossary terms without Project Glossary update evidence.
- [ ] Non-ready artifacts can be saved with blocking reasons and remediation.
- [ ] Structured errors identify the missing evidence or glossary blocker.
- [ ] Service, command schema, and CLI tests cover valid rich artifacts and each readiness rejection.

## Draft Issue 3: Draft PRDs from Ready Challenge Artifacts with Required `to-prd` Provenance

**Blocked by**: Draft Issue 2

**User stories covered**: 2, 4, 7, 8, 9, 17, 21, 22, 36, 37, 38, 39, 40, 41

### What to build

Add the PRD draft path from a ready Challenge Artifact. AgileForge should accept
the PRD output produced by `to-prd`, verify that it references a
`ready_for_prd` Challenge Artifact, record producer provenance, and persist a
draft PRD in AgileForge-owned state.

This slice should enforce the agreed hard-fail rule: PRD drafts for Scope
Discovery cannot be saved from generic/manual producers. The completed path is
demoable by saving a `to-prd` PRD draft from a ready Challenge Artifact and
seeing the PRD linked back to that artifact.

### Acceptance criteria

- [ ] AgileForge can persist a PRD draft sourced from a Challenge Artifact.
- [ ] PRD creation fails unless the source Challenge Artifact exists and has readiness `ready_for_prd`.
- [ ] PRD creation fails unless the PRD declares `to-prd` as the PRD Producer.
- [ ] PRD records source Challenge Artifact ID, producer provenance, status, version, content, and created metadata.
- [ ] New PRDs start with status `draft`.
- [ ] PRD markdown export is optional and non-authoritative.
- [ ] The command returns a structured envelope with PRD ID, source Challenge Artifact ID, status, version, and next action.
- [ ] Mutating PRD draft creation is idempotent and retry-safe.
- [ ] Service, command schema, and CLI tests cover successful creation and all hard-fail cases.

## Draft Issue 4: Accept, Reject, and Version PRDs Without Mutating Accepted PRDs

**Blocked by**: Draft Issue 3

**User stories covered**: 18, 19, 20, 21, 22, 23, 38, 39, 41

### What to build

Add the human review lifecycle for PRDs. A reviewer can accept or reject a draft
PRD. Accepted PRD versions are immutable. Any later change creates a new draft
version that can supersede the prior accepted version.

This slice should keep PRD acceptance separate from Spec Amendment Drafting.
Accepting a PRD means product intent is approved; it does not create accepted
scope and does not trigger authority compilation. The completed path is demoable
by creating a PRD draft, accepting it, failing an attempted in-place edit, and
creating a superseding draft version.

### Acceptance criteria

- [ ] A draft PRD can be accepted with reviewer identity and acceptance notes.
- [ ] A draft PRD can be rejected with reviewer identity and rejection notes.
- [ ] Accepted PRDs cannot be modified in place.
- [ ] Changes after acceptance create a new draft PRD version linked to the accepted version it may supersede.
- [ ] A rejected PRD cannot produce a Spec Amendment Draft.
- [ ] PRD acceptance does not create a Spec Amendment Draft automatically.
- [ ] The command response reports PRD status, version, supersession links, and next action.
- [ ] Mutating review commands are idempotent and guard against conflicting repeated decisions.
- [ ] Service, command schema, and CLI tests cover accept, reject, immutable edit rejection, and superseding draft creation.

## Draft Issue 5: Record and Validate Agent-Generated Spec Amendment Drafts from Accepted PRDs

**Blocked by**: Draft Issue 4

**User stories covered**: 24, 25, 26, 27, 28, 29, 32, 42, 44, 45, 50

### What to build

Add the explicit transition from Accepted PRD to Spec Amendment Draft, with
AgileForge acting as the recorder and validator for an agent-generated draft.
For MVP, AgileForge should not own generative prompting for the amendment body.
The agent creates the draft in the workspace; AgileForge records it, links it to
the accepted PRD and Challenge Artifact, and runs Spec Amendment Validation
before human acceptance is available.

For existing project scope extension, validation should reuse the current
additive scope-extension validation behavior rather than creating a parallel
spec validator. The completed path is demoable by taking an accepted PRD,
recording an agent-generated Spec Amendment Draft, validating it against the
current accepted spec, and seeing invalid/non-additive drafts blocked before
human acceptance.

### Acceptance criteria

- [ ] A Spec Amendment Draft can be recorded only from an accepted PRD.
- [ ] Draft recording requires an agent-generated amendment payload or file reference.
- [ ] Draft recording records PRD provenance and Challenge Artifact provenance.
- [ ] Draft recording does not mutate accepted scope.
- [ ] Spec Amendment Validation runs before human amendment acceptance is available.
- [ ] Existing additive validation blocks modified, removed, or non-additive accepted source items for existing project scope extension.
- [ ] Invalid drafts return blocking issues and remediation without creating accepted scope.
- [ ] Valid drafts can be marked ready for human amendment acceptance.
- [ ] Service, command schema, CLI, and existing scope-extension validation tests cover valid and invalid drafts.

## Draft Issue 6: Start Project Scope Extension Only from Accepted Validated Spec Amendments

**Blocked by**: Draft Issue 5

**User stories covered**: 29, 30, 31, 32, 33, 42, 43, 45, 46, 50

### What to build

Integrate accepted validated Spec Amendments with the existing Project Scope
Extension bridge into the authority pipeline. Existing project scope extension
should no longer start from an arbitrary amended spec file in normal guided
flow; it should start from an accepted validated Spec Amendment with preserved
PRD and Challenge Artifact provenance.

This slice must keep Accepted Authority as the only source of executable work.
The completed path is demoable by moving from accepted PRD to accepted Spec
Amendment, starting scope extension, compiling/reviewing/accepting authority,
and verifying downstream backlog/roadmap/story/sprint work remains blocked until
authority is accepted.

### Acceptance criteria

- [ ] Project Scope Extension start can consume an accepted validated Spec Amendment.
- [ ] Scope Extension start rejects PRD drafts, accepted PRDs, unvalidated Spec Amendment Drafts, and rejected Spec Amendments.
- [ ] Scope Extension start preserves provenance from Spec Amendment to PRD to Challenge Artifact.
- [ ] Authority compilation runs only after Spec Amendment acceptance.
- [ ] Backlog, roadmap, story, task, and sprint generation remain blocked until authority is accepted.
- [ ] Existing completed work, sprint history, evidence, and accepted authority records are preserved.
- [ ] Replay/idempotency behavior prevents duplicate scope-extension starts from the same accepted amendment.
- [ ] Integration tests cover the discovery-to-authority handoff and no-bypass invariant.

## Draft Issue 7: Route Exhausted Existing Projects Through Scope Discovery in `workflow next`

**Blocked by**: Draft Issues 1, 2, 3, 4, 5

**User stories covered**: 1, 2, 3, 4, 13, 17, 18, 23, 24, 27, 30, 35, 36, 37, 38

### What to build

Update workflow guidance for exhausted existing projects so users and agents see
the required Scope Discovery gate before scope extension. `workflow next` should
report the next missing prerequisite: missing Challenge Artifact, Challenge
Artifact not ready, missing PRD, PRD not accepted, missing Spec Amendment Draft,
invalid Spec Amendment Draft, pending Spec Amendment acceptance, pending
authority compilation, or pending authority acceptance.

This slice should not bypass existing scope-extension availability checks. It
should make the current state legible and return runnable commands or blocked
commands with concrete remediation. The completed path is demoable by moving an
exhausted project through each discovery state and observing `workflow next`
change guidance.

### Acceptance criteria

- [ ] Exhausted existing projects surface Scope Discovery as the guided next path.
- [ ] `workflow next` distinguishes missing Challenge Artifact from non-ready Challenge Artifact.
- [ ] `workflow next` distinguishes missing PRD from draft/unaccepted PRD.
- [ ] `workflow next` distinguishes missing Spec Amendment Draft from invalid or unaccepted Spec Amendment Draft.
- [ ] `workflow next` continues to block scope extension when active/planned sprint work or unresolved executable work remains.
- [ ] Responses include command names, runnable flags, reasons, and remediation.
- [ ] Dashboard/API projections expose the same guidance without treating draft artifacts as accepted scope.
- [ ] Tests cover each major discovery state transition.

## Draft Issue 8: Surface Discovery Provenance Through Authority Review and Execution Blockers

**Blocked by**: Draft Issue 6

**User stories covered**: 31, 35, 41, 42, 43, 45, 46

### What to build

Expose Challenge Artifact and PRD provenance in the authority review and
downstream execution gates for existing project scope extension. Reviewers
should see the product reasoning behind new scope: source Challenge Artifact,
source PRD, producer provenance, assumptions, non-goals, risks, evidence
conflicts, and open questions status.

This slice should also harden downstream blockers so backlog, roadmap, stories,
tasks, and sprints cannot run from draft discovery artifacts even if an agent
tries to call lower-level commands directly. The completed path is demoable by
reviewing authority for discovered scope and seeing provenance in the review
payload while direct execution attempts from drafts are blocked.

### Acceptance criteria

- [ ] Authority review payloads include Challenge Artifact and PRD provenance for discovered scope.
- [ ] Review payloads expose assumptions, non-goals, risks, evidence conflicts, and readiness status relevant to the accepted scope.
- [ ] Downstream backlog, roadmap, story, task, and sprint paths reject draft Challenge Artifacts, draft PRDs, accepted PRDs without accepted authority, and Spec Amendment Drafts.
- [ ] Error responses explain that executable work requires Accepted Authority.
- [ ] Tests prove direct lower-level command calls cannot bypass Scope Discovery and Authority Gate rules.
- [ ] Existing authority/backlog/roadmap/story/sprint tests remain compatible with the provenance additions.

## Post-MVP Draft Issue 9: Apply Required Scope Discovery to Greenfield Project Creation

**Blocked by**: MVP Draft Issues 1-8

**User stories covered**: 1, 2, 3, 4, 24, 25, 26, 27, 29, 30, 31, 34, 35

### What to build

Extend the Scope Discovery gate to greenfield project creation after the
existing-project pipeline has shipped. Because greenfield discovery can happen
before a project exists, AgileForge needs a provisional discovery context that
can hold Challenge Artifacts, PRDs, and Spec Amendment Drafts until project
creation consumes an accepted validated initial spec artifact.

This slice should enforce the same hard-fail rules as existing project scope
extension: no greenfield project can be created from raw idea text, PRD draft, or
unaccepted Spec Amendment Draft. The completed path is demoable by taking a
greenfield idea through required discovery and creating a project only after
accepted validated scope is available.

### Acceptance criteria

- [ ] Greenfield discovery can store artifacts before a project ID exists.
- [ ] Greenfield project creation requires completed Scope Discovery under the required producer rules.
- [ ] Greenfield project creation rejects raw ideas, Challenge Artifacts alone, PRD drafts, accepted PRDs alone, and unaccepted Spec Amendment Drafts.
- [ ] Accepted validated greenfield scope can create the initial project/spec path without bypassing authority.
- [ ] The resulting project preserves provenance to Challenge Artifact and PRD.
- [ ] `workflow next` or equivalent setup guidance reports missing greenfield discovery prerequisites.
- [ ] Tests cover successful greenfield creation and each no-bypass rejection.

## Review Decisions Applied

- Split the original Challenge Artifact issue because minimal persistence and rich readiness validation are separate vertical slices with different failure modes.
- Moved greenfield support to post-MVP because provisional discovery context is a separate architectural problem from existing project scope extension.
- Tightened Spec Amendment Draft scope for MVP: AgileForge records and validates agent-generated drafts instead of owning generative prompting.
- Corrected dependencies so PRD drafting waits for rich `ready_for_prd` validation, and `workflow next` waits for the states it reports.
