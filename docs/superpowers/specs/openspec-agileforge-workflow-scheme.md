# OpenSpec and AgileForge Workflow Scheme

This scheme captures the current working model for spec-driven Scrum in
AgileForge.

AgileForge remains the Scrum workflow authority. It owns intake, Product
Authority, as-built assessment, evidence-aware reconciliation, Product Backlog,
Product Owner ordering, Sprint Planning, Sprint task tickets, evidence, story
close, and sprint close.

Superpowers is the preferred implementation methodology after AgileForge issues
a work ticket.

OpenSpec is optional. In this model, `/opsx:propose` may draft a behavior-change
proposal when a backlog candidate needs a clearer behavior contract. AgileForge
does not use `/opsx:apply` or `/opsx:archive` as workflow gates.

```mermaid
flowchart TD
    A["User or product request"] --> B["AgileForge intake"]
    B --> C["Product Authority / accepted intent"]
    B --> D{"Behavior unclear or non-trivial?"}

    D -->|"Yes, optional"| E["/opsx:propose drafts behavior proposal"]
    E --> F["PO / reviewer reviews proposal"]
    F --> G["AgileForge links reviewed proposal as context"]
    D -->|"No"| H["No OpenSpec artifact required"]

    C --> I{"Project mode?"}
    I -->|"Greenfield"| J["Generate backlog from Product Authority"]
    I -->|"Brownfield"| K["Build bounded evidence pack"]
    K --> L["As-Built Assessment Agent"]
    L --> M["AsBuiltAssessment in workflow state"]
    M --> Z["Assessment-aware backlog generation"]
    J --> N["Backlog candidates"]
    Z --> N
    G --> N
    H --> N

    N --> O["Product Owner reviews and orders Product Backlog"]
    O --> P["Sprint Planning selects ready work"]
    P --> Q["AgileForge Sprint task ticket"]

    Q --> R["Superpowers execution workflow"]
    R --> S["Design / plan / worktree / TDD / review / finish branch"]
    S --> T["Artifacts and validation evidence"]
    T --> U["agileforge sprint task update"]
    U --> V{"Story or Sprint done?"}
    V -->|"No"| Q
    V -->|"Yes"| W["Story close / Sprint close in AgileForge"]
    W --> X["Future evidence baseline updated by next As-Built Assessment"]
    W --> Y["If behavior contract changed, update technical spec deliberately"]
```

## Working Rules

- Product authority defines what should be true.
- The original technical spec provides context and must declare whether it is
  `current_state`, `desired_state`, or `proposed_change`.
- Repo evidence describes what can be observed in the implementation.
- AgileForge must not create a brownfield work backlog from Product Authority
  alone. It should build a bounded evidence pack, run the As-Built Assessment
  Agent, and feed the resulting `AsBuiltAssessment` to backlog generation.
- Greenfield projects do not require evidence collection before the first
  backlog.
- User requests enter AgileForge intake first, not OpenSpec.
- OpenSpec `/opsx:propose` is optional behavior-contract drafting context for
  unclear or non-trivial behavior changes.
- OpenSpec `tasks.md` is not the canonical execution tracker.
- `/opsx:apply` and `/opsx:archive` are not AgileForge workflow gates.
- AgileForge Sprint task tickets are the canonical work tickets.
- The deterministic `ReconciliationReport` / `implementation_evidence_cached`
  collector is compatibility evidence only. Brownfield backlog generation should
  prefer `as_built_assessment_cached` when present and fresh.
- Superpowers is the preferred execution workflow after a task ticket exists.
- Delivery evidence returns to AgileForge through task updates, story close, and
  sprint close.
- Technical specs are living behavior contracts. If implementation intentionally
  diverges from the technical spec while still satisfying Product Authority, the
  spec should be updated deliberately rather than drift silently.
