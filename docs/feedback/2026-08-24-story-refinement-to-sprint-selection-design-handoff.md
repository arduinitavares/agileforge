# Story Refinement to Sprint Selection Design Handoff

**Date:** 2026-08-24
**Status:** Agreed product direction; implementation split across issues
**Current acceptance branch:** `dev/issue-218-progressive-story-readiness`
**Current acceptance commit:** `383845005966da3c3848f8dab48e8b033e64b5e0`

## Purpose

Define the intended AgileForge workflow from generated Story proposals through
human refinement, automatic structural checks, explicit Sprint selection, and
solo-operator Sprint planning.

This handoff records the product decision exposed by the String Calculator
manual acceptance test. It does not authorize implementation, provider-backed
generation, Sprint generation, merge, push, or issue closure.

## Product Thesis

AgileForge is a guardrail and evidence system for one accountable human using
powerful agents to develop software. Agents may propose, analyze, revise, and
execute work through the CLI or UI. Generation never equals acceptance. The
human remains accountable for the accepted product and planning decisions,
including when the human explicitly delegates execution to an agent.

Authentication and detailed delegation provenance are future concerns. They
are not part of the present workflow correction.

## Scrum Accountability in Solo Operation

AgileForge preserves useful Scrum accountabilities without pretending that an
agent is a human Scrum role:

- Product Backlog order and rank are Product Owner decisions.
- Story sizing and points are Developer decisions.
- Scrum defines no team-leader accountability for these decisions.
- In solo operation, one human wears both Product Owner and Developer hats.
- Agents provide recommendations and perform delegated actions; they do not
  silently become Product Owner, Developer, or Scrum Master.

## Agreed Workflow

```text
Generate Story proposal
  -> Refine the complete proposal
  -> Human accepts the exact proposal
  -> Run provider-free structural checks automatically
  -> Show eligible Stories
  -> Human selects a Sprint subset
  -> Human confirms scope-specific dependencies
  -> Generate and review the Sprint plan
```

### 1. Generate and refine

The model proposes a complete, reviewable Story package. Before acceptance,
the browser and CLI must expose at least:

- Story statement and acceptance criteria;
- accepted Specification evidence;
- proposed effort and derived points, with rationale;
- proposed rank or order, with rationale;
- proposed dependencies and their reasons;
- an explainable INVEST assessment.

The human can request a precise revision through an agent or use an installed
structured adjustment path. Any changed proposal must be reviewed as a new,
exact candidate. Hidden planning metadata must not become trusted merely
because the Story wording was accepted.

### 2. INVEST assessment

Retain the INVEST concept, but do not treat one unexplained High, Medium, or
Low label as proof of Story quality.

The review contract must address Independent, Negotiable, Valuable, Estimable,
Small, and Testable separately. Each retained assessment needs an explicit
result, rationale, and evidence that a human can inspect and challenge.

AgileForge cannot honestly guarantee semantic value, negotiability, or
estimability through a deterministic check. It can guarantee that:

- every required dimension was assessed;
- bounded structural evidence was checked where possible;
- concerns and uncertainty remain visible;
- no opaque model label silently becomes trusted state;
- the accountable human accepted the exact assessment and Story proposal.

The detailed schema, correction semantics, and operational effect belong to
issue #221.

### 3. Acceptance and automatic structural checks

Human Story acceptance approves the exact visible Story package, including its
planning metadata. It remains distinct from deterministic validation.

After acceptance, provider-free structural checks run automatically and record
auditable evidence. The checks establish integrity and freshness, not product
value or Sprint intent. They may verify exact Story identity, immutable item
binding, accepted Backlog and Specification lineage, parent-bounded references,
required Story shape, non-empty acceptance criteria, and current evidence
fingerprints.

Failures must be visible with precise remediation. A manual button labelled
**Validate Story** must not double as a hidden selection mechanism.

### 4. Eligibility and Sprint selection

A Story becomes eligible only after human acceptance and successful automatic
checks. Eligibility is a machine-derived fact. Sprint selection is a separate,
reversible human planning decision.

The operator selects one or more eligible Stories for Sprint consideration.
Unselected, unvalidated, or unrefined Stories remain available for later work.
The workflow must not require complete refinement of every Story or Backlog
item before Sprint planning.

Visible actions should express planning intent, for example **Select for
Sprint** and **Remove from Sprint selection**, rather than **Validate Story**.

### 5. Dependency confirmation

Proposed Story dependencies are visible during refinement. After the operator
selects a Sprint subset, AgileForge presents the selected Stories and their
scope-specific dependency edges for a final human decision.

The dependency review must identify external prerequisites, cycles, excluded
dependencies, and blockers in human-readable form. Sprint generation remains
blocked when the selected scope is not dependency-safe.

### 6. Solo Sprint ownership

The immediate product workflow assumes one accountable human using agents.
That operator must not need to invent a fictional team name.

Sprint generation must resolve a deterministic, visible solo-owner default and
allow an explicit named-team override when a real team exists. Ownership must
remain durable and human-readable. Agents are collaborators, not artificial
team members or Scrum accountabilities.

The detailed defaulting, persistence, compatibility, and terminology contract
belongs to issue #224.

## State and Language Contract

The UI, CLI, API, and persisted evidence must distinguish:

- **Proposed:** generated and awaiting refinement or human decision;
- **Accepted:** the exact Story package was accepted by the accountable human;
- **Structurally eligible:** automatic provider-free checks currently pass;
- **Selected for Sprint:** the human chose the Story for the planning scope;
- **Dependency confirmed:** the selected scope passed human dependency review;
- **Sprint candidate:** selected, structurally eligible, and dependency-safe.

No one status or button may represent more than one of these decisions.

## Existing Design Conflicts to Resolve Explicitly

### Opaque INVEST score

`docs/superpowers/specs/2026-06-08-story-draft-quality-contract-design.md`
uses aggregate `invest_score` values and makes all-Low output a save blocker.
Issue #221 must decide how that gate changes when INVEST becomes an explainable
per-dimension assessment. Adding the old score to the UI is insufficient.

### Parent-requirement-only Sprint selection

`docs/superpowers/specs/2026-06-09-story-selection-sprint-scope-design.md`
selects scope by saved parent requirement and explicitly excludes selecting an
individual subset of its Stories. The agreed progressive workflow selects
eligible individual Stories. Implementation must reconcile or supersede that
older restriction explicitly; two invisible selection contracts must not
coexist.

### Required team name

`docs/superpowers/specs/2026-06-11-agentic-sprint-capacity-planning-design.md`
retains required `team_name` ownership. Issue #224 must preserve durable
ownership while removing the need for a solo operator to invent a team name.
The capacity-points decision remains unchanged.

## Issue Map

- [#221](https://github.com/arduinitavares/agileforge/issues/221): define an
  explainable and reviewable INVEST contract.
- [#222](https://github.com/arduinitavares/agileforge/issues/222): restore human
  ownership of Story effort, points, order, and dependency metadata before
  acceptance.
- [#223](https://github.com/arduinitavares/agileforge/issues/223): separate
  automatic structural eligibility from explicit human Sprint selection.
- [#224](https://github.com/arduinitavares/agileforge/issues/224): make
  solo-operator Sprint ownership a first-class default.
- [#218](https://github.com/arduinitavares/agileforge/issues/218): current
  progressive-readiness implementation and acceptance baseline. Keep it open
  until the successor issue boundaries determine whether its current manual
  validation semantics are amended or superseded.

## Recommended Sequence

1. Resolve #221 so Story quality metadata has a defensible meaning.
2. Resolve #222 so the accepted Story package is complete and human-owned.
3. Resolve #223 so automatic checks and Sprint selection have separate states
   and controls.
4. Resolve #224 so Sprint planning has coherent solo ownership.
5. Prepare a fresh commit-pinned acceptance profile.
6. Repeat the partial-refinement scenario and stop before provider-backed Sprint
   generation until the exact selected scope and resolved owner are visible.

## Manual Acceptance Scenarios

### Story refinement evidence

- A generated Story review shows content, acceptance criteria, Specification
  evidence, effort, points, rank or order, dependency proposals, and INVEST
  rationale before acceptance.
- Requesting a precise change produces a new exact candidate and preserves
  decision lineage.
- Acceptance records the exact visible package; no planning values appear only
  after acceptance.

### Automatic structural evidence

- Accepting a Story triggers provider-free structural checks without a second
  approval-like click.
- Passing checks make the Story eligible but do not select it.
- Failing checks show exact diagnostics and keep the Story ineligible.
- Stale evidence invalidates eligibility deterministically.

### Partial Sprint selection

- One eligible Story can be selected while accepted siblings remain unselected
  and other Backlog items remain unrefined.
- Selection and removal are explicit and visible after reload.
- The candidate pool contains only selected, eligible, dependency-safe Stories.

### Solo ownership

- A solo operator reaches Sprint planning without inventing a team name.
- The resolved owner is visible before provider-backed Sprint generation.
- An explicit real-team override remains available and produces clear durable
  ownership evidence.

## Protected Boundaries

- No provider call is needed to implement or verify the structural and UI
  contracts above.
- Do not generate a Sprint during issue implementation or provider-free review.
- Preserve exact lineage, freshness, idempotency, stale-action, and duplicate
  submission guards.
- Do not auto-accept model output.
- Do not represent agents as human Scrum accountabilities.
- Keep future authentication and delegation-attribution work outside this issue
  sequence.
