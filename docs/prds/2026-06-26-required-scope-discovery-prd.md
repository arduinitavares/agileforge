# Required Scope Discovery PRD

Status: draft
Date: 2026-06-26

## Problem Statement

AgileForge gives strong downstream Scrum and spec-driven guardrails once accepted scope exists: authority, backlog, roadmap, stories, tasks, and sprints all depend on explicit gates. The weak point appears before those gates. When a project exhausts its current backlog or when a greenfield project begins, the user still has to answer a hard product question: how does a vague idea become accepted scope without bypassing Scrum discipline or AgileForge authority?

Today, a user can reason manually with `grill-with-docs` and `to-prd`, then separately hand that output back into AgileForge. That loses workflow state. AgileForge cannot prove that a new scope idea was challenged, that shared language was updated, that a PRD came from the accepted challenge context, or that a spec amendment came from an accepted PRD. This makes it too easy for agents to jump from an idea directly into backlog, sprint, or implementation work.

The user wants AgileForge to adopt the preferred product-thinking pattern directly:

```text
grill-with-docs -> to-prd -> AgileForge authority pipeline
```

The important rule is that Matt Pocock's downstream `to-issues -> tdd` path is not used. Once a PRD exists, AgileForge resumes control through spec amendment, authority compilation, backlog generation, roadmap, stories, and sprints.

## Solution

AgileForge will add a required Scope Discovery gate before any new scope can become executable work. Scope Discovery creates and stores first-class Challenge Artifacts and PRDs in AgileForge-owned project state. It hard-fails if the required external producers are unavailable:

- `grill-with-docs` is the required Challenge Producer.
- `to-prd` is the required PRD Producer.

The canonical flow is:

```text
idea
-> grill-with-docs
-> Challenge Artifact
-> Project Glossary updates when new terms are settled
-> to-prd
-> PRD draft
-> human accepts PRD
-> explicit Spec Amendment Draft command
-> Spec Amendment Validation
-> human accepts Spec Amendment
-> Authority Gate
-> Accepted Authority
-> backlog, roadmap, stories, tasks, sprints
```

AgileForge must enforce that new executable work never comes directly from an idea, chat transcript, PRD draft, or spec amendment draft. It may only come from Accepted Authority.

The end state should support both existing project scope extension and greenfield project creation. The first implementation batch should ship existing project scope extension because it validates the core `grill-with-docs -> to-prd -> Spec Amendment -> Authority` pipeline without introducing provisional greenfield context. The same hard-fail rule still applies to all new scope once each path is implemented.

## User Stories

1. As a product owner, I want AgileForge to require `grill-with-docs` before new scope is accepted, so that vague ideas are challenged before they influence execution work.
2. As a product owner, I want AgileForge to require `to-prd` after the challenge is ready, so that clarified ideas become product-owner-readable PRDs before entering the spec pipeline.
3. As a product owner, I want AgileForge to hard-fail when `grill-with-docs` is unavailable, so that unchallenged scope cannot slip into the project.
4. As a product owner, I want AgileForge to hard-fail when `to-prd` is unavailable, so that PRDs are produced by the agreed process rather than a weaker substitute.
5. As a product owner, I want a Challenge Artifact saved after `grill-with-docs`, so that future review can inspect questions, answers, assumptions, evidence, risks, non-goals, and readiness.
6. As a product owner, I want Challenge Artifacts stored in AgileForge project state, so that workflow gates can query readiness without depending on chat history or loose files.
7. As a product owner, I want PRDs stored in AgileForge project state, so that PRD status and provenance drive workflow gates.
8. As a product owner, I want optional markdown export for Challenge Artifacts and PRDs, so that humans can review them comfortably without making markdown the source of truth.
9. As a product owner, I want a PRD to reference its source Challenge Artifact, so that I can trace product scope back to the interview and evidence that produced it.
10. As a product owner, I want a Challenge Artifact to record which documents, specs, ADRs, code evidence, and workflow artifacts were reviewed, so that I can detect stale or missing evidence.
11. As a product owner, I want a Challenge Artifact to record evidence conflicts, so that contradictions are resolved before PRD drafting.
12. As a product owner, I want a Challenge Artifact to record open questions, so that AgileForge blocks premature PRD generation when decisions are unresolved.
13. As a product owner, I want a Challenge Artifact readiness state, so that only `ready_for_prd` artifacts can drive PRD creation.
14. As a product owner, I want Challenge Artifact readiness to be `blocked`, `needs_answers`, or `ready_for_prd`, so that the next action is explicit.
15. As a product owner, I want AgileForge to require Project Glossary updates when `grill-with-docs` settles new terms, so that PRDs and later specs use stable language.
16. As a product owner, I want AgileForge to distinguish Project Glossary updates from Challenge Artifacts, so that stable language is not mixed with case-specific reasoning.
17. As a product owner, I want a PRD draft status, so that generated PRDs do not become accepted product intent automatically.
18. As a product owner, I want to accept or reject a PRD explicitly, so that human product judgment remains the gate before spec amendment drafting.
19. As a product owner, I want accepted PRDs to be immutable, so that later edits cannot rewrite the audit trail.
20. As a product owner, I want PRD changes after acceptance to create a new PRD version, so that supersession is explicit.
21. As a product owner, I want PRDs to use `draft`, `accepted`, and `rejected` statuses, so that downstream commands can enforce simple state rules.
22. As a product owner, I want AgileForge to prevent PRD acceptance when the source Challenge Artifact is missing or not ready, so that product intent cannot bypass challenge evidence.
23. As a product owner, I want accepting a PRD to not automatically create a Spec Amendment Draft, so that product intent acceptance and spec translation stay separate.
24. As a product owner, I want a separate command to draft a Spec Amendment from an accepted PRD, so that the transition into authority-compilable specification shape is explicit.
25. As a product owner, I want Spec Amendment Drafts to be generated by an agent from accepted PRDs, so that I do not have to hand-write structured spec artifacts.
26. As a product owner, I want generated Spec Amendment Drafts to remain drafts, so that agent output never becomes accepted scope without review.
27. As a product owner, I want Spec Amendment Validation to run before human acceptance, so that reviewers are not asked to accept known-invalid drafts.
28. As a product owner, I want Spec Amendment Validation to check current accepted spec state, so that new scope cannot modify or remove previously accepted scope in unsupported ways.
29. As a product owner, I want a valid Spec Amendment Draft to be human-accepted before authority compilation, so that structural validity does not replace product review.
30. As a product owner, I want authority compilation to run only after Spec Amendment acceptance, so that the authority pipeline remains the source of downstream execution work.
31. As a product owner, I want backlog, roadmap, stories, tasks, and sprints generated only from Accepted Authority, so that Scrum execution remains tied to reviewed scope.
32. As a product owner, I want current scope extension commands to remain the bridge into the existing authority pipeline, so that this feature adds upstream discovery without replacing proven downstream gates.
33. As a product owner, I want existing project scope extension to preserve completed work, sprint history, evidence, and authority records, so that adding new scope does not reset the project.
34. As a product owner, I want greenfield project creation to follow the same discovery gate, so that new projects start with challenged scope rather than raw ideas.
35. As a product owner, I want the workflow UI or `workflow next` output to show missing discovery prerequisites, so that I know whether I need a challenge session, PRD, PRD acceptance, or spec amendment.
36. As an agent, I want command metadata for discovery commands, so that I can discover required inputs, mutating behavior, idempotency rules, and possible errors.
37. As an agent, I want clear error codes when required producers are unavailable, so that I do not guess or silently fall back.
38. As an agent, I want Challenge Artifact and PRD commands to return structured envelopes, so that downstream automation can inspect status and next actions.
39. As an agent, I want creation commands to be idempotent where they mutate AgileForge state, so that retries do not duplicate artifacts.
40. As an agent, I want draft-generation commands to capture producer provenance, so that future agents can tell whether `grill-with-docs` and `to-prd` produced the artifacts.
41. As an agent, I want PRD and Challenge Artifact IDs in downstream responses, so that I can wire later commands without scraping text.
42. As an agent, I want Spec Amendment Drafts to preserve PRD provenance, so that later authority review can show which accepted PRD created the amendment.
43. As an agent, I want authority review to surface PRD and Challenge Artifact provenance, so that a reviewer sees risks, non-goals, and assumptions before accepting authority.
44. As an AgileForge maintainer, I want discovery artifacts modeled separately from spec registry entries, so that PRD review and spec authority review stay distinct.
45. As an AgileForge maintainer, I want existing spec registry and authority acceptance behavior reused after Spec Amendment acceptance, so that this feature does not fork the downstream pipeline.
46. As an AgileForge maintainer, I want high-level service tests for the full discovery-to-authority handoff, so that the no-bypass rule is covered at the workflow boundary.
47. As an AgileForge maintainer, I want CLI tests for discovery commands, so that users and agents get stable command behavior.
48. As an AgileForge maintainer, I want command schema tests for discovery commands, so that the command registry remains accurate.
49. As an AgileForge maintainer, I want migration/model tests for Challenge Artifacts and PRDs, so that persisted state supports provenance and versioning.
50. As an AgileForge maintainer, I want existing scope-extension tests to remain valid, so that upstream discovery does not weaken additive spec validation.

## Implementation Decisions

- AgileForge will use `grill-with-docs` as the mandatory Challenge Producer for all new scope.
- AgileForge will use `to-prd` as the mandatory PRD Producer after a Challenge Artifact is ready.
- Missing required producers are hard failures, not warnings and not fallback-to-template cases.
- The hard-fail policy applies to greenfield project creation and existing project scope extension.
- Challenge Artifacts are first-class AgileForge artifacts.
- PRDs are first-class AgileForge artifacts.
- Challenge Artifacts and PRDs are stored in AgileForge-owned project state. Markdown export is optional and non-authoritative.
- A Challenge Artifact records the original idea, questions, answers, reviewed evidence, evidence conflicts, assumptions, non-goals, risks, open questions, glossary changes, producer provenance, and readiness.
- Challenge Artifact readiness is `blocked`, `needs_answers`, or `ready_for_prd`.
- PRD creation requires a source Challenge Artifact with readiness `ready_for_prd`.
- New or changed domain terms discovered during grilling must be reflected in the Project Glossary before the Challenge Artifact can become `ready_for_prd`.
- A PRD records source Challenge Artifact ID, producer provenance, status, version, content, acceptance data, and supersession data.
- PRD status is `draft`, `accepted`, or `rejected`.
- Accepted PRD versions are immutable. Changes create a new draft PRD version that may supersede the accepted version.
- PRD acceptance does not automatically create a Spec Amendment Draft.
- Spec Amendment Draft creation is a separate explicit command from PRD acceptance.
- Spec Amendment Drafts are agent-generated from accepted PRDs.
- Spec Amendment Drafts remain drafts until human review.
- Spec Amendment Validation runs before a Spec Amendment Draft can be accepted.
- Human acceptance of a valid Spec Amendment is required before authority compilation.
- Accepted Authority remains the only source for new executable work.
- Matt Pocock's `to-issues -> tdd` path is intentionally not part of this AgileForge flow. AgileForge takes over after PRD through its authority, backlog, roadmap, story, and sprint pipeline.
- The implementation should reuse existing command registry, application facade, workflow next, spec registry, authority, and scope-extension concepts instead of creating a parallel execution pipeline.
- The highest useful product boundary is a discovery service that owns Challenge Artifact and PRD state transitions and exposes commands through the existing app facade and command registry.
- Existing scope-extension validation and start behavior should remain the downstream bridge for accepted spec amendments.

## Testing Decisions

- Tests should cover external behavior and workflow gates rather than implementation details.
- The highest-priority tests should prove the no-bypass invariant: ideas, Challenge Artifact drafts, PRD drafts, and Spec Amendment Drafts cannot create executable work.
- Service-level tests should cover Challenge Artifact creation, readiness transitions, producer provenance, and the hard-fail behavior when required producers are missing.
- Service-level tests should cover PRD draft creation from a ready Challenge Artifact, PRD acceptance, PRD rejection, immutable accepted PRD versions, and superseding PRD versions.
- Service-level tests should cover rejection of PRD creation when the source Challenge Artifact is missing, blocked, or still needs answers.
- Service-level tests should cover rejection of PRD acceptance when required challenge evidence or Project Glossary updates are missing.
- Service-level tests should cover Spec Amendment Draft creation only from accepted PRDs.
- Service-level tests should cover rejection of Spec Amendment acceptance until Spec Amendment Validation passes.
- Workflow routing tests should first cover existing project scope extension requiring discovery gates before accepted scope. Greenfield discovery routing should be covered when the provisional greenfield context is added.
- Command schema tests should cover new discovery commands, required inputs, optional inputs, mutating flags, idempotency policy, and error codes.
- CLI tests should cover command wiring and structured envelopes for challenge, PRD, and spec amendment draft operations.
- Existing scope-extension validation tests should remain the regression suite for additive amendment behavior.
- Authority gate tests should prove Accepted Authority is still required before backlog, roadmap, stories, tasks, or sprints can be generated.
- Migration/model tests should verify persisted Challenge Artifact and PRD fields, indexes, relationships, statuses, timestamps, and versioning constraints.
- Recovery/idempotency tests should cover retrying mutating discovery commands without creating duplicate artifacts.
- Dashboard/API tests should verify next-action guidance reports missing Challenge Producer, missing PRD Producer, missing ready Challenge Artifact, unaccepted PRD, invalid Spec Amendment Draft, and pending authority acceptance.

## Out of Scope

- Do not implement Matt Pocock's `to-issues` workflow.
- Do not implement Matt Pocock's `tdd` workflow as an AgileForge downstream path.
- Do not allow PRD markdown to become authority directly.
- Do not allow a PRD draft to produce backlog, roadmap, stories, tasks, or sprints.
- Do not allow a Spec Amendment Draft to produce authority or executable work before validation and human acceptance.
- Do not make markdown files or chat transcripts the source of truth for workflow state.
- Do not support fallback challenge templates when `grill-with-docs` is missing.
- Do not support fallback PRD generators when `to-prd` is missing.
- Do not silently mutate accepted scope during challenge or PRD creation.
- Do not rewrite existing completed work, sprint history, or accepted authority during scope extension.
- Do not solve removal or deprecation of old accepted scope in the MVP.
- Do not introduce project-specific vocabulary such as backend, ML, training, snapshots, or consent into the core feature.
- Do not create issue tracker tickets from this PRD without explicit user approval.

## Further Notes

This PRD came from a `grill-with-docs` interview and the resulting Project Glossary and ADRs. The key product decision is that AgileForge should become stricter, not more portable, at the new-scope boundary. New scope must be challenged by `grill-with-docs`, expressed by `to-prd`, accepted by a human, translated into a spec amendment, validated, accepted again, compiled into authority, accepted as authority, and only then used to create executable work.

The immediate dogfood case is existing project scope extension after exhausted backlog. The policy also applies to greenfield project creation, but greenfield support should follow after the existing-project pipeline is stable because it needs a provisional discovery context before a project ID exists.

The primary implementation risk is over-coupling AgileForge core to local skill paths. The accepted product decision is still to hard-fail when the required producers are unavailable. Implementation should express this as a producer capability check and explicit error behavior, not as silent path assumptions hidden inside unrelated commands.
