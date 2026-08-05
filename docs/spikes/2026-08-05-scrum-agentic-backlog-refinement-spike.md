# Scrum And Agentic Backlog Refinement Adversarial Spike

**Date:** 2026-08-05
**Status:** Research complete; design decision pending
**Scope:** Brownfield onboarding, product discovery, Product Backlog refinement,
repository assessment, agent context, freshness, cost, and human review

## Executive Verdict

The current proposal is directionally correct but should not be adopted exactly
as written.

AgileForge should remove mandatory project-wide brownfield onboarding from the
normal product lifecycle. It should not replace it with an equally rigid rule
that every idea immediately becomes a Product Backlog candidate or that every
item must pass the same depth of repository analysis.

The strongest design found in this spike is a **risk-adaptive evidence funnel**:

```text
Request Inbox
-> human value and Product Goal screening
-> cheap deterministic repository lookup
-> bounded current-state/gap assessment when needed
-> broader dependency or project assessment only when risk demands it
-> Product Owner admission, rewrite, archive, or discovery decision
-> Product Backlog refinement
-> Sprint selection from sufficiently ready items
-> freshness check against the current repository state
```

This keeps product discovery separate from delivery commitment, moves technical
assessment close to the item that needs it, and preserves a deliberate escape
hatch for cross-cutting, safety-critical, compliance, migration, acquisition,
or deeply undocumented work.

**Verdict:** adopt the central direction, but amend it before implementation.

**Confidence:** high for removing mandatory project-wide brownfield onboarding;
medium-high for the risk-adaptive replacement. The latter still needs a small
experiment on caRtola, ASA, and MyFinance.

## Research Execution And Limits

The spike was executed by a delegated research subagent using the adversarial
brief. The parent agent then independently checked the decisive Scrum,
agent-context, repository-analysis, small-batch, and agentic bug-flow sources
before accepting the synthesis.

This is still a single research spike rather than empirical proof. The proposed
workflow therefore remains subject to the falsification experiment below.

The spike used:

- the latest official Scrum Guide found, dated November 2020;
- current Scrum.org teaching materials, clearly separated from normative Scrum;
- primary guidance from OpenAI, Anthropic, Google DORA, GitHub Spec Kit, and
  Microsoft Research;
- peer-reviewed repository-level code research where available; and
- one very recent arXiv preprint, explicitly treated as emerging rather than
  settled evidence.

No provider-backed trial was run. No implementation code was changed.

## The AgileForge Problem Being Tested

The current hard-break design makes brownfield status a project origin and a
mandatory onboarding path:

```text
Open Project Shell
-> Repository Baseline
-> Git-Aware Inventory And Scan
-> Product Spec Curation
-> Human Initial Spec Decision
-> Initial Scope Registration
-> Authority Compile
-> Human Authority Decision
-> As-Built Assessment
-> Vision And Backlog Reconciliation
```

This is documented in the
[hard-break design](../superpowers/specs/2026-08-02-domain-workflow-graph-hard-break-design.md).
The operator acceptance checklist repeats that full sequence for caRtola, ASA,
and MyFinance, including a complete inventory and provider-backed curation
before ordinary product work can begin. See the
[workflow graph acceptance checklist](../testing/workflow-graph-acceptance-checklist.md).

The original need was narrower: prevent work that is already implemented,
partially implemented, duplicated, obsolete, or unsupported by current evidence
from being planned as if it were new work. Project-wide onboarding is one way to
answer that question, but it pays the highest analysis and coordination cost
before AgileForge knows which part of the repository matters.

## What Scrum Actually Supports

### Normative Scrum

The latest official Scrum Guide found during this spike remains the
[November 2020 Scrum Guide](https://scrumguides.org/docs/scrumguide/v2020/2020-Scrum-Guide-US.pdf)
(Ken Schwaber and Jeff Sutherland, November 2020).

It establishes that:

- Scrum is intentionally incomplete and can contain complementary practices.
- The Product Owner is accountable for the Product Goal and effective Product
  Backlog management.
- The Product Backlog is an emergent, ordered, single source of work for the
  Scrum Team.
- Product Backlog refinement is ongoing. Attributes vary by domain.
- Product Backlog Items that can be completed within one Sprint are deemed
  ready for selection, usually after refinement.
- Sprint Planning may refine selected items further, but its purpose is to
  establish why the Sprint is valuable, what can be done, and how the selected
  work will be delivered.
- Scrum includes verification, maintenance, experimentation, and research among
  the Scrum Team's product-related responsibilities.

Scrum therefore supports discovering that a proposed item is wrong, obsolete,
or already satisfied before Sprint selection. It does **not** prescribe:

- brownfield onboarding;
- a Request Inbox;
- a Definition of Ready checklist;
- `already_satisfied`, `duplicate`, or `needs_discovery` states;
- repository scans;
- authority compilation; or
- an automated gate that forbids selection.

Those are complementary AgileForge practices and must be justified by observed
value, not labeled as Scrum requirements.

### Scrum.org Guidance, Not Scrum Rules

Scrum.org's current
[Product Backlog refinement learning material](https://www.scrum.org/resources/product-backlog-refinement)
(accessed 2026-08-05) describes items evolving from vague ideas as information
is uncovered and emphasizes that refinement is not a prescribed Scrum event.

Scrum.org articles provide useful but non-normative cautions:

- [Product Backlog refinement: how far is too far?](https://www.scrum.org/resources/blog/product-backlog-refinement-how-far-too-far)
  (Mary Iqbal, 2022-02-23) warns against refining too far ahead in volatile
  environments because the work may change before delivery.
- [Ready or Not?](https://www.scrum.org/resources/blog/ready-or-not-demystifying-definition-ready-scrum)
  (Joanna Plaskonka, 2023-09-27) treats readiness as contextual and warns
  against excessive refinement or treating a Definition of Ready as immutable.
- [Product Discovery Is a Risk-Reduction Journey](https://www.scrum.org/resources/blog/product-discovery-risk-reduction-journey-not-just-phase)
  (Lavaneesh Gautam, 2025-06-16) argues that discovery is ongoing risk
  reduction, not a front-loaded phase.
- [27 Product Backlog and Refinement Anti-Patterns](https://www.scrum.org/resources/blog/27-product-backlog-and-refinement-anti-patterns)
  (Stefan Wolpers, 2022) identifies stale backlog hoarding, excessive upfront
  detail, and using the Product Backlog as an unfiltered idea store as
  anti-patterns.

The last point directly challenges the proposal to capture every idea as a
Product Backlog candidate. Scrum permits vague Product Backlog Items, but it
does not require every request or idea to become one. A lightweight discovery
or request inbox can protect Product Backlog transparency without creating a
second delivery backlog.

The
[Kanban Guide for Scrum Teams](https://www.scrum.org/resources/kanban-guide-scrum-teams)
(Scrum.org, current guide page accessed 2026-08-05) explicitly presents defined
workflow states and flow management as practices that complement Scrum. This
supports visualizing states such as assessing and ready, but it does not make
those states Scrum artifacts or justify a rigid gate by itself.

## Agentic Software-Development Evidence

### Progressive Disclosure Beats Full Upfront Context

Anthropic's
[Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
(2025-09-29) describes context as finite and recommends a hybrid model: small
stable context up front, with files and other evidence retrieved just in time.
It explicitly notes that tool-driven navigation can avoid stale-index and
oversized-context problems.

OpenAI's
[Harness engineering](https://openai.com/index/harness-engineering/)
(2026-02-11) reports that a large monolithic instruction corpus became stale,
hard to verify, and harmful to agent navigation. Its replacement was a small
map into versioned repository knowledge, progressive disclosure, mechanical
freshness checks, and continuous targeted cleanup. The same system validates
the current state, reproduces problems, and verifies changes before declaring
completion.

**Inference for AgileForge:** a repository map or index is useful, but a full
natural-language reconstruction of the product before every project can become
the same stale monolith these systems avoid.

### Bounded Does Not Mean Blindly Local

The peer-reviewed NAACL 2025 paper
[On the Impacts of Contexts on Repository-Level Code Generation](https://aclanthology.org/2025.findings-naacl.82/)
(Findings of NAACL 2025) found that retaining full dependency context produced
the best results on its RepoExec benchmark. Small context could look adequate
while omitting dependencies necessary for executable, correct code.

Microsoft Research's
[CodePlan](https://www.microsoft.com/en-us/research/publication/codeplan-repository-level-coding-using-llms-and-planning-2/)
(2024) treats repository-level changes as a planning problem and supplies each
edit with task-specific context derived from repository-wide dependency
information.

**Inference for AgileForge:** per-item assessment should begin narrowly, but it
must expand to the item's dependency closure and, when evidence indicates
cross-cutting impact, to a broader repository or multi-repository analysis.
"Always inspect only a few files" would be another unsafe fixed rule.

### Indexed Retrieval Can Beat Agent Delegation For Read-Only Questions

The recent preprint
[Deep Agentic Search for Repository-Level Code Question Answering](https://arxiv.org/abs/2608.01507)
(submitted 2026-08-02; under journal review) compared semantic retrieval with a
planner delegating repository exploration to a subagent. On its SWE-QA setup,
semantic retrieval answered 65.2% correctly versus 46.2% for delegated deep
search and cost less than half as much per correct answer. The authors report
that 41.8% of deep-search failures occurred at the planner/subagent handoff.

This is one new preprint on read-only repository questions, not a universal
result. It nevertheless challenges an assumption that every assessment should
start another agent. A deterministic or indexed retrieval step can be cheaper,
faster, and easier to audit. Agent search remains useful when retrieval is weak,
the repository cannot be indexed, or the question needs interactive reasoning.

### Verification Must Be Part Of The Loop

Anthropic's current
[Claude Code best practices](https://code.claude.com/docs/en/best-practices)
(original engineering guidance published 2025-04-18; current docs accessed
2026-08-05) recommends exploring before implementation and giving the agent a
deterministic check such as tests, builds, or visual comparison. OpenAI's
[Codex introduction](https://openai.com/index/introducing-codex/)
(2025-05-16) similarly emphasizes test, lint, terminal, and human-review
evidence.

GitHub Spec Kit's current, opt-in
[Agentic Bug Fix workflow](https://github.github.com/spec-kit/reference/agentic-bugfix.html)
(accessed 2026-08-05) uses a per-bug `assess -> fix -> test` lifecycle. Its
assessment is read-only and its final verdict is downgraded when the claimed
reproduction was not actually exercised. That is closer to AgileForge's
original need than mandatory whole-project onboarding.

**Inference for AgileForge:** repository search alone cannot prove that a
capability works. An assessment must distinguish source evidence from executed
verification and must never promote "code found" into "already satisfied"
without evidence appropriate to the acceptance criteria.

### Specifications Need Explicit Drift Handling

GitHub Spec Kit's
[Evolving Specs in Existing Projects](https://github.github.com/spec-kit/guides/evolving-specs.html)
and
[Spec Persistence Models](https://github.github.com/spec-kit/concepts/spec-persistence.html)
(current documentation accessed 2026-08-05) support several maintenance models
rather than one universal approach. They require changed intent, plans, tasks,
and implementation to be reconciled before downstream work resumes. The docs
name silent divergence as the principal risk of flexible flow-back maintenance
and duplication as a tradeoff of immutable flow-forward records.

Spec Kit lists brownfield bootstrap as an optional, community-maintained
extension rather than part of its required core workflow. Its built-in bug flow
and existing-project guidance are item/feature-oriented.

**Inference for AgileForge:** accepted authority can remain durable, but an
assessment must say which repository revision and which authority version it
evaluated. A repository-wide spec generated once at onboarding is not a
permanent statement of current implementation.

### Small Batches Matter More With AI

Google DORA's
[2025 State of AI-assisted Software Development](https://dora.dev/research/2025/dora-report/)
reports that AI amplifies the surrounding delivery system's strengths and
weaknesses. DORA's
[Working in small batches](https://dora.dev/capabilities/working-in-small-batches/)
(updated 2025-12-08) says small, independently testable work improves feedback
and acts as a countermeasure to AI-related delivery instability.

**Inference for AgileForge:** large mandatory onboarding batches delay feedback
and increase sunk cost. Smaller evidence and delivery batches are preferable,
provided the system can escalate when an item's true dependency scope is large.

## Comparable Designs

| Design | Strengths | Failure Modes | Best Fit | Verdict |
| --- | --- | --- | --- | --- |
| Project-wide brownfield onboarding | Creates a broad initial map; can expose systemic constraints; amortizes well when many changes share the same context | High upfront time and model cost; quickly stale; blocks value; creates false confidence that the whole repository is understood; poor human UX | Acquisition, compliance baseline, safety case, platform migration, security threat model, or deeply unknown legacy takeover | Keep as an explicit assessment mode, not the normal lifecycle |
| Sprint-Planning-only check | Almost no process overhead; uses the latest repository state | Discovery consumes planning time; creates pressure to accept weak work; leaves no time for a spike; repeats surprises; late cost decisions | Tiny, reversible, well-tested changes where the check is deterministic and minutes long | Useful only as a final freshness check |
| Fixed per-item assessment | Aligns cost with likely work; naturally finds already/partially implemented scope; supports evidence and audit | Can miss shared dependencies; repeated analysis across related items; a rigid gate can become a Definition-of-Ready bureaucracy | Normal feature, bug, and improvement work in a navigable repository | Good default, but incomplete |
| Risk-adaptive evidence funnel | Screens product value before spending on code analysis; reuses cheap repository maps; expands context only when evidence/risk demands it; separates discovery from delivery; preserves a full-scan escape hatch | More policy design; escalation rules can be wrong; needs freshness and provenance discipline; must avoid two competing backlogs | Mixed greenfield and existing products with human product decisions and agent execution | Recommended |

## Recommended Design

### 1. Replace Project Origin With Available Evidence

Do not make `greenfield` and `brownfield` mutually exclusive workflow
lifecycles. Every repository becomes "brownfield" after its first increment,
and even a new product can include reused components.

A Project may instead have optional evidence:

- repository identity and current revision;
- a cheap structural map or index;
- accepted product goals and specifications;
- prior item assessments;
- tests and runtime observations; and
- explicit broad assessments, when someone intentionally commissioned one.

The absence of a repository map should reduce confidence or trigger targeted
discovery. It should not prevent a human from creating the Project, stating a
Product Goal, or recording a request.

### 2. Keep Request Intake Distinct From The Product Backlog

Use one lightweight Request Inbox for raw ideas, defects, stakeholder requests,
and agent findings. This is a product discovery surface, not a second ordered
delivery backlog.

An inbox entry should have only enough information to preserve provenance and
enable a decision. It may be rejected without technical analysis when it does
not serve the Product Goal or no longer has value.

After value screening and sufficient evidence, the Product Owner may:

- admit a new Product Backlog Item;
- rewrite the request as the remaining gap;
- link it to an existing Product Backlog Item;
- archive it as already satisfied or no longer valuable; or
- commission a time-boxed discovery item.

This is an AgileForge complement to Scrum, not a new Scrum artifact.

### 3. Separate Observed Facts From Product Decisions

The proposed outcome enum mixes technical observations, relationships, product
decisions, and readiness. These should not be one state machine.

Use three concepts:

**Current-state assessment**

- `not_found`
- `partially_implemented`
- `implemented_unverified`
- `verified_satisfied`
- `conflicting_evidence`
- `unknown`

**Product decision**

- `admit`
- `rewrite_remaining_gap`
- `link_duplicate`
- `archive_no_longer_valuable`
- `archive_satisfied`
- `commission_discovery`
- `defer`

**Backlog readiness**

- emergent attributes on a Product Backlog Item;
- enough transparency and size to be selected within a Sprint; and
- never a claim that requirements cannot change.

An agent may author the current-state assessment. A human Product Owner owns the
product decision. Developers retain responsibility for sizing and feasibility
judgment. AgileForge may expose readiness evidence but should not pretend a
checklist replaces collaboration.

### 4. Use Adaptive Repository-Analysis Depth

Start with the cheapest defensible operation:

1. Search existing requests, Product Backlog Items, decisions, and assessments
   for duplicates.
2. Read deterministic repository metadata and an index keyed to the current
   commit.
3. Retrieve likely symbols, tests, documentation, history, and dependency
   neighbors for the requested behavior.
4. Ask an agent to evaluate the bounded evidence against explicit acceptance
   criteria.
5. Run available deterministic checks when the assessment claims behavior is
   satisfied.
6. Escalate to a dependency-closure, multi-repository, runtime, or full-project
   assessment when confidence is low or risk is high.

Escalation signals should include:

- security, privacy, financial, safety, or compliance impact;
- architecture, schema, protocol, or public API change;
- many affected packages or repositories;
- absent or unreliable tests;
- conflicting specs and implementation;
- unknown deployment behavior;
- low retrieval coverage or confidence; and
- several related requests that can share one broader assessment.

### 5. Make Freshness Explicit

Every assessment should record:

- repository and worktree identity;
- assessed commit;
- dirty-state policy;
- evidence paths and content hashes;
- authority/specification version;
- checks actually executed and their results;
- analysis depth and escalation reason;
- model/tool identity and cost metadata; and
- human decision and decision time.

Before Sprint selection, AgileForge should revalidate freshness cheaply:

- If the repository revision and authority are unchanged, reuse the assessment.
- If only unrelated files changed and the indexed dependency/evidence closure is
  unchanged, keep it valid with a refreshed proof.
- If relevant evidence changed, the repository is dirty beyond the accepted
  policy, or impact is unknown, mark the assessment stale and rerun the minimum
  necessary scope.

Do not rerun paid analysis merely because the global commit changed when the
relevant evidence can be proven unchanged.

### 6. Keep Human And Agent Interfaces Different

The human UI should answer:

- What is being requested and why?
- What evidence says about the current product?
- What remains to be done?
- How fresh and confident is that evidence?
- What decision can I make?

Human actions should be plain commands such as:

- **Add to Product Backlog**
- **Rewrite as remaining gap**
- **Already satisfied**
- **Link duplicate**
- **Investigate further**
- **Archive**

The UI should not require node IDs, fingerprints, repository hashes, or raw JSON
from the operator. Those are host-derived guards and provenance.

The agent CLI should expose task-specific structured operations, for example:

```text
request assess --request-id ...
request assessment show --request-id ... --json
request decide --request-id ... --decision ...
backlog readiness show --item-id ... --json
```

The CLI should return evidence, guards, cost, freshness, and typed failure
reasons. The same domain rules should serve UI and CLI, but their interaction
contracts should not be identical.

## When This Recommendation Would Be Wrong

The risk-adaptive design should be rejected or overridden when:

1. **A broad baseline is itself the product outcome.** Examples include a
   security threat model, license audit, regulatory traceability baseline,
   acquisition due diligence, platform migration inventory, or disaster
   recovery review.
2. **Local evidence cannot predict impact.** A highly dynamic system, extensive
   runtime configuration, generated code, hidden deployment state, or weak
   module boundaries may make item-level repository retrieval unreliable.
3. **The cost is genuinely amortized.** If dozens of imminent items require the
   same broad system understanding, one bounded project assessment can be
   cheaper and more consistent than repeated item assessments.
4. **A repository is small and stable.** A complete deterministic scan may be
   cheaper than building retrieval and escalation machinery.
5. **The Product Backlog is intentionally the discovery store.** A small team
   with very few ideas may not need a visible Request Inbox. The logical
   distinction can remain internal rather than becoming another UI lane.
6. **Assessment becomes bureaucracy.** If obvious, reversible, well-tested work
   requires expensive model review before selection, AgileForge has recreated a
   rigid Definition of Ready and should remove or bypass the gate.
7. **The freshness model cannot detect relevant change.** Reusing stale
   assessments would be worse than a broader scan.

## Minimal Falsification Experiment

Do not rewrite the workflow first. Run this in shadow mode against pinned
historical repository revisions.

### Sample

Select 12 real requests across caRtola, ASA, and MyFinance:

- three known to be absent;
- three already satisfied;
- three partially satisfied; and
- three duplicate, obsolete, or genuinely uncertain.

Include at least two cross-cutting requests and two requests whose result changes
between two commits.

Create a human-reviewed answer key from repository evidence and executable tests
without showing it to the assessment process.

### Compare

Run the same requests through:

1. one complete project-wide baseline plus item decisions;
2. fixed bounded per-item assessment; and
3. the proposed risk-adaptive funnel with deterministic retrieval first.

Sprint-Planning-only handling should be simulated by giving the operator no
assessment until the selection session and measuring interruption and decision
time. It does not need a separate assessment algorithm.

### Measure

- technical classification agreement with the answer key;
- false `verified_satisfied` and false `ready` decisions;
- missed cross-cutting dependencies;
- human decision time and number of required interactions;
- tokens, provider cost, wall time, and repeated-context cost;
- percentage resolved without a paid model call;
- evidence reuse across related requests; and
- correct invalidation or reuse after repository changes.

### Falsification Conditions

Reject the risk-adaptive recommendation before broad implementation if any of
these occur:

- it misses a material dependency or false-ready condition caught by the full
  baseline;
- it cannot distinguish `implemented_unverified` from `verified_satisfied`;
- its median provider cost is not materially lower than the full baseline;
- stale evidence survives a relevant code or authority change;
- the operator must interpret raw agent prose or hidden technical guards to make
  ordinary decisions; or
- escalation happens so often that the adaptive path is effectively the full
  onboarding path with extra steps.

The small sample cannot prove general correctness. It can cheaply disprove the
claim that the adaptive approach preserves quality while reducing friction and
cost.

## Final Recommendation

Proceed with a design revision, not implementation yet:

1. Remove `brownfield` as a mandatory project workflow origin.
2. Preserve project-wide repository assessment as an explicit, reusable tool.
3. Introduce a lightweight Request Inbox outside the ordered Product Backlog.
4. Screen product value before spending on repository analysis.
5. Add commit-bound, per-item current-state assessment with adaptive depth.
6. Separate technical assessment, Product Owner decision, and backlog readiness.
7. Use Sprint Planning for selection and freshness confirmation, not first-time
   repository discovery.
8. Run the shadow experiment before changing the durable graph or deleting the
   current implementation.

The original insight survives the challenge: AgileForge should avoid creating
work for behavior that already exists. The optimal mechanism is not a universal
brownfield onboarding lifecycle. It is a small, evidence-driven product
discovery and refinement loop that expands only when the risk justifies it.

## Source Register

Primary and high-trust sources:

1. [The Scrum Guide](https://scrumguides.org/docs/scrumguide/v2020/2020-Scrum-Guide-US.pdf),
   Ken Schwaber and Jeff Sutherland, 2020-11.
2. [Product Backlog Refinement](https://www.scrum.org/resources/product-backlog-refinement),
   Scrum.org, accessed 2026-08-05.
3. [Kanban Guide for Scrum Teams](https://www.scrum.org/resources/kanban-guide-scrum-teams),
   Scrum.org, current guide page accessed 2026-08-05.
4. [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents),
   Anthropic, 2025-09-29.
5. [Claude Code best practices](https://code.claude.com/docs/en/best-practices),
   Anthropic, original guidance 2025-04-18; current docs accessed 2026-08-05.
6. [Harness engineering](https://openai.com/index/harness-engineering/),
   OpenAI, 2026-02-11.
7. [Introducing Codex](https://openai.com/index/introducing-codex/), OpenAI,
   2025-05-16.
8. [State of AI-assisted Software Development 2025](https://dora.dev/research/2025/dora-report/),
   Google DORA, 2025.
9. [Working in small batches](https://dora.dev/capabilities/working-in-small-batches/),
   Google DORA, updated 2025-12-08.
10. [Evolving Specs in Existing Projects](https://github.github.com/spec-kit/guides/evolving-specs.html),
    GitHub Spec Kit, accessed 2026-08-05.
11. [Spec Persistence Models](https://github.github.com/spec-kit/concepts/spec-persistence.html),
    GitHub Spec Kit, accessed 2026-08-05.
12. [Agentic Bug Fix](https://github.github.com/spec-kit/reference/agentic-bugfix.html),
    GitHub Spec Kit, accessed 2026-08-05.
13. [CodePlan](https://www.microsoft.com/en-us/research/publication/codeplan-repository-level-coding-using-llms-and-planning-2/),
    Microsoft Research, 2024.
14. [On the Impacts of Contexts on Repository-Level Code Generation](https://aclanthology.org/2025.findings-naacl.82/),
    Findings of NAACL, 2025.
15. [Deep Agentic Search for Repository-Level Code Question Answering](https://arxiv.org/abs/2608.01507),
    arXiv preprint submitted 2026-08-02; under review.

Complementary practitioner sources used only for non-normative cautions:

1. [Product Backlog refinement: how far is too far?](https://www.scrum.org/resources/blog/product-backlog-refinement-how-far-too-far),
   Mary Iqbal, Scrum.org, 2022-02-23.
2. [Ready or Not?](https://www.scrum.org/resources/blog/ready-or-not-demystifying-definition-ready-scrum),
   Joanna Plaskonka, Scrum.org, 2023-09-27.
3. [Product Discovery Is a Risk-Reduction Journey](https://www.scrum.org/resources/blog/product-discovery-risk-reduction-journey-not-just-phase),
   Lavaneesh Gautam, Scrum.org, 2025-06-16.
4. [27 Product Backlog and Refinement Anti-Patterns](https://www.scrum.org/resources/blog/27-product-backlog-and-refinement-anti-patterns),
   Stefan Wolpers, Scrum.org, 2022.
