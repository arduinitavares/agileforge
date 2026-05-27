# OpenSpec and AgileForge Workflow Scheme

This scheme captures the working hypothesis for using OpenSpec as the change/spec layer and AgileForge as the planning, reconciliation, and sprint execution layer.

OpenSpec remains fluid and change-oriented. AgileForge should consume OpenSpec artifacts, reconcile them against product authority and repo evidence, produce ordered backlog/sprint work, and feed implementation results back into OpenSpec when delivered.

```mermaid
flowchart TD
    A["User intent / product request"] --> B{"Project type?"}

    B -->|"Greenfield"| C["Create initial OpenSpec specs or first OpenSpec change"]
    B -->|"Brownfield"| D["Read existing OpenSpec specs + code/tests/docs evidence"]

    C --> E["AgileForge imports OpenSpec capability/change map"]
    D --> F["AgileForge reconciles Product Spec vs OpenSpec vs repo evidence"]

    F --> G{"Capability status"}
    G -->|"Already implemented + evidenced"| H["No work backlog item"]
    G -->|"Implemented but missing tests/evidence"| I["Create verification/hardening backlog item"]
    G -->|"Implemented but conflicts with accepted spec"| J["Create authority-fix backlog item"]
    G -->|"Partial or missing"| K["Create product backlog item"]

    E --> K
    H --> L["Capability map only"]
    I --> M["Product Backlog"]
    J --> M
    K --> M

    M --> N["Product Owner orders backlog"]
    N --> O["Sprint Planning selects ordered, ready work"]
    O --> P["AgileForge Sprint task tickets"]

    P --> Q{"Implementation engine"}
    Q -->|"AgileForge agent task loop"| R["Task next/show/update/story close/sprint close"]
    Q -->|"Superpowers / Codex / Claude / human / other"| S["External implementation using same tickets"]

    R --> T["Verification evidence"]
    S --> T

    T --> U{"Change delivered?"}
    U -->|"No"| P
    U -->|"Yes"| V["Archive/sync OpenSpec change"]
    V --> W["OpenSpec specs updated as current behavior"]
    W --> X["AgileForge reconciliation baseline updated"]

    Y["New feature not in tech spec"] --> Z["/opsx:propose new-feature"]
    Z --> AA["OpenSpec change folder: proposal.md + specs + design.md + tasks.md"]
    AA --> AB["AgileForge imports change as backlog candidate"]
    AB --> M
```

## Working Rules

- Product authority defines what should be true.
- OpenSpec specs describe current or proposed system behavior.
- Repo evidence includes code, tests, docs, CLI output, and runtime behavior.
- AgileForge should not create a work backlog from the product spec alone for brownfield repositories.
- New feature requests should enter as OpenSpec changes first, then AgileForge can import and reconcile them into backlog candidates.
- `/opsx:apply` is optional because implementation may happen through AgileForge task tickets, Superpowers, Codex, Claude, humans, or another engine.
- OpenSpec archive/sync remains important after delivery because it updates the repo behavior spec.
