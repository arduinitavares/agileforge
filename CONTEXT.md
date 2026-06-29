# AgileForge

AgileForge is a product-workflow system that keeps product intent, authority,
backlog, roadmap, stories, and sprints aligned through explicit review gates.

## Language

**Project Scope Extension**:
A guarded workflow for adding new accepted product scope to an existing project
after the current executable scope is exhausted.
_Avoid_: project reset, project fork, backlog refill

**Challenge Artifact**:
A saved record of the questions, answers, evidence, assumptions, non-goals, and
risks resolved before a PRD is drafted.
_Avoid_: chat transcript, informal notes

**Discovery Artifact Store**:
AgileForge-owned project storage for challenge artifacts and PRDs; exported
markdown may mirror these artifacts, but workflow gates read AgileForge state.
_Avoid_: markdown-only workflow state, chat-only provenance

**Challenge Producer**:
The required challenge mechanism that creates a challenge artifact for scope
discovery; AgileForge requires `grill-with-docs` before accepting new scope.
_Avoid_: optional brainstorming helper, untracked chat

**Project Glossary**:
The stable shared language for the AgileForge project, captured in
`CONTEXT.md` and updated when challenge sessions settle new domain terms.
_Avoid_: challenge artifact, product spec

**Spec Amendment**:
A proposed change to the accepted product specification that must be reviewed
and accepted before AgileForge generates new execution work from it.
_Avoid_: new project spec, direct backlog input

**Spec Amendment Draft**:
A generated draft translation of an accepted PRD into the specification shape
that AgileForge can validate and compile.
_Avoid_: accepted spec amendment, PRD

**Accepted Spec Amendment**:
A human-reviewed spec amendment that AgileForge may pass through authority
compilation and acceptance before creating executable work.
_Avoid_: generated amendment draft, accepted PRD

**Spec Amendment Validation**:
The structural and policy check that proves a spec amendment draft is acceptable
for human review.
_Avoid_: authority acceptance, PRD acceptance

**PRD**:
A product requirements document that captures clarified product intent before it
is promoted into a spec amendment.
_Avoid_: issue list, implementation ticket batch, authority source

**PRD Producer**:
The required mechanism that turns a ready challenge artifact into a PRD draft;
AgileForge requires `to-prd` for this step.
_Avoid_: generic PRD generator, manual issue synthesis

**Accepted PRD**:
A PRD that a human has approved as product intent and that AgileForge may use to
draft a spec amendment.
_Avoid_: draft PRD, accepted authority

**PRD Version**:
An immutable revision of a PRD; accepted PRDs are changed by creating a new
draft version that may supersede the prior accepted version.
_Avoid_: in-place accepted PRD edit

**Authority Gate**:
The review boundary where an accepted specification or spec amendment is
compiled and accepted as the source for downstream backlog, roadmap, story, and
sprint work.
_Avoid_: automatic backlog generation, unchecked generation

**Accepted Authority**:
The reviewed authority state that AgileForge may use to create or extend
backlog, roadmap, story, and sprint work.
_Avoid_: draft authority, generated suggestion

**Executable Work**:
Backlog, roadmap, story, task, or sprint work that AgileForge is allowed to
plan or run from accepted authority.
_Avoid_: idea, draft, proposal
