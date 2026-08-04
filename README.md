# AgileForge

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Google ADK](https://img.shields.io/badge/Google-ADK-orange.svg)](https://github.com/google/adk-python)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

> **A developer tool for agent-assisted agile planning and execution, governed by Spec Authority.**

AgileForge runs the agile backbone from product vision to sprint execution. It gives agents guarded Project workflows, durable SQLite facts, and a CLI for inspecting or applying exact workflow decisions.

This project began as a **TCC (Trabalho de Conclusão de Curso)** research initiative exploring how AI agents can autonomously orchestrate Agile workflows with a **Spec-Driven Architecture**.

---

## ✨ Features

### 🎯 Complete Agile Workflow Pipeline
```
Vision → Specification Authority → Initial Backlog → Roadmap → User Stories → Sprint Planning → Execution
```

### 🧠 Intelligent Agents
| Agent | Role | Capabilities |
|-------|------|--------------|
| **product vision Tool** | Project Owner | **Strategic Initiation:** Constructs a 7-component "True North" vision statement using the "Bucket Brigade" stateless pattern. |
| **Spec Authority Compiler** | Architect | **Feasibility Filter:** A non-conversational compiler that extracts deterministic "Definition of Done" constraints from technical specs. |
| **Backlog Primer** | Project Owner | **Pre-Planning:** Converts Vision into a prioritized list of Gross Requirements (not User Stories) using T-Shirt sizing. |
| **Roadmap Builder** | Project Owner | **Strategic Planning:** Maps requirements to time-based milestones, respecting technical dependencies and themes. |
| **User Story Writer** | PO Assistant | **Requirement Refinement:** Decomposes requirements into INVEST-ready "Vertical Slices" using the "Three Cs" protocol. |
| **Sprint Planner** | Scrum Master | **Tactical Planning:** Facilitates scope selection via a "Pull System" and auto-decomposes stories into technical tasks. |

### 🛠️ Key Capabilities
- **Spec-Driven Architecture**: Single source of truth via `SpecRegistry`. All downstream artifacts (stories, roadmap) are validated against compiled authority.
- **Bucket Brigade Architecture**: Agents are stateless processors that receive state, apply a "diff," and pass it forward. This ensures predictable behavior.
- **Strict Scrum Compliance**: All agents leverage *Scrum For Dummies, 2nd Edition* as the authoritative source for their logic (e.g., INVEST, Vertical Slicing, Pull Systems).
- **Draft → Review → Commit Pattern**: Artifacts are generated in a draft state and require explicit user confirmation before persistence.
- **WorkflowEvent Metrics**: Built-in tracking for TCC evaluation (NASA-TLX, cycle time).

---

## 🏗️ Architecture

`WorkflowDomain.position(project_id)` derives available, waiting, blocked,
invalid, or terminal nodes from durable Project facts. Guarded task-specific
commands submit typed requests through `WorkflowDomain.transition(request)`.
ADK recipes execute eligible agent work but do not own routing state.

A greenfield or brownfield intake opens a Project Shell first. Discovery,
repository inventory, curation, authority, planning, and execution records all
belong to that Project identity. Repository onboarding remains operator-led:
the operator selects the source, reviews inventory and curation artifacts, and
submits the exact guarded transition advertised by `workflow position`.

### Design Patterns
- **Derived Workflow Graph**: One immutable fact snapshot drives routing and transition guards.
- **Spec Authority Pattern**: Compiler pattern for deterministic invariants.
- **Durable Project Facts**: Restarts and deleted execution traces do not alter workflow position.
- **Schema-Driven Validation**: All I/O validated by Pydantic schemas.

---

## 🚀 Quick Start

### Prerequisites
- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- OpenRouter API key (for LLM access)

### Installation

```bash
# Clone the repository
git clone https://github.com/arduinitavares/agileforge.git
cd agileforge

# Install the locked environment
uv sync --frozen

# Set up environment variables
cp .env.example .env
# Then edit .env and set:
# - OPEN_ROUTER_API_KEY
# - AGILEFORGE_DB_URL
# - AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL
# - MODEL_CONFIG_PATH

# Initialize isolated state for this checkout
./agileforge-dev init --profile local --json
```

### Agent CLI

The CLI is the supported agent interface for workflow inspection and guarded
mutations. For the full contract, workflows, idempotency rules, and recovery
guidance, see [docs/agent-cli-manual.md](docs/agent-cli-manual.md).
For the Operator-run caRtola, ASA, and MyFinance acceptance evidence package,
see [docs/testing/workflow-graph-acceptance-checklist.md](docs/testing/workflow-graph-acceptance-checklist.md).
For the `grill-with-docs` -> `to-prd` -> AgileForge Scope Discovery handoff,
see [docs/scope-discovery-agent-runbook.md](docs/scope-discovery-agent-runbook.md).

Stable release: `agileforge workflow next --project-id 1`

Current checkout: `./agileforge-dev cli --profile local -- workflow next --project-id 1`

Current checkout UI: `./agileforge-dev ui --profile local --port auto`

Provenance: `./agileforge-dev info --profile local --json`

For branch and linked-worktree development, use only that checkout's
`./agileforge-dev`. Initialize its profile once, record `info --json` before
mutations, and keep the installed stable release separate from checkout-local
testing.

One `info --json` command is the complete redacted runtime preflight. Its
`configured_models` field lists typed model roles and IDs,
`provider_credentials` reports presence booleans, and
`child_runtime_environment` contains the exact derived non-secret child values.
It never emits credential values. Use optional `--secrets-file PATH` when the
same allowlisted provider source will be used by a later agentic command.

### Running the Application

```bash
# Start the managed checkout-local dashboard
./agileforge-dev ui --profile local --port auto

# Open the dashboard URL reported by the launcher
```

---

## 📖 Usage Examples

### 1. Create a New product vision

```
You: I want to build a recipe discovery app for home cooks

Agent: I'll help you define the product vision. Let me ask some clarifying questions:
- What should we call this product?
- What specific problem does it solve for home cooks?
...

You: Let's call it MealMuse...

Agent: Great! Vision saved. Now, do you want to define the Technical Specification?
```

### 2. Define Specification & Plan

```
You: Here is the technical spec for MealMuse... [Pastes Spec]

Agent: Spec compiled and Authority accepted.
I will now generate the Initial Project Backlog (Gross Requirements) before we build the Roadmap.

You: Proceed.

Agent: Backlog prioritized. Now building the Roadmap...
[Generates Milestones with Themes]
```

### 3. Execute Sprint Work

```
You: Mark story 35 as done

Agent: ✅ Story #35 updated: IN_PROGRESS → DONE
"Access app on iOS and Android"
```

---

## 📁 Project Structure

```
agileforge/
├── api.py                           # Deterministic FastAPI entry point
├── agile_sqlmodel.py                # Database schema (SQLModel/SQLAlchemy)
├── PLANNING_WORKFLOW.md             # Detailed workflow documentation
├── SPEC_DRIVEN_ARCHITECTURE_PLAN.md # Spec Authority Architecture
├── CLAUDE.md                        # TCC requirements and methodology
│
├── workflow/                        # Derived graph, facts, requests, handlers
├── adapters/adk/                    # Leaf-agent execution recipes
├── services/application.py          # Production WorkflowDomain boundary
│
├── tools/
│   ├── db_tools.py                  # Database utilities
│   └── spec_tools.py                # Spec persistence and authority tools
│
├── utils/
│   ├── schemes.py                   # Shared Pydantic schemas
│   └── helper.py                    # Instruction loading
│
└── tests/
    ├── conftest.py                  # Test fixtures
    └── test_*.py                    # Unit tests
```

---

## 🗄️ Database Schema

```
projects ─┬─> spec_registry ─> compiled_spec_authority
          │
          ├─> themes ─┬─> epics ─┬─> features
          │           │          │
          │           │          └─> user_stories ─┬─> sprint_stories
          │           │                            │
          └─> teams ──┴─> sprints ─────────────────┘
                              │
                              └─> workflow_events (metrics)
```

Key tables:
- **projects**: Top-level container
- **spec_registry**: Versioned technical specifications
- **compiled_spec_authority**: Deterministic invariants compiled from specs
- **user_stories**: INVEST-ready stories with spec validation
- **sprints**: Sprint planning with goals and dates

---

## 🔧 Technology Stack

| Category | Technology |
|----------|------------|
| **Agent Framework** | [Google ADK](https://github.com/google/adk-python) (Agent Development Kit) |
| **LLM Abstraction** | LiteLLM via OpenRouter API |
| **Model** | `openrouter/google/gemini-2.0-flash-exp` (or updated model) |
| **ORM** | SQLModel (0.0.27+) + SQLAlchemy |
| **Database** | SQLite (portable, zero-config) |
| **Schema Validation** | Pydantic v2 |
| **Session Management** | ADK DatabaseSessionService |

---

## 📊 TCC Evaluation Metrics

This system is designed for academic evaluation using:

| Metric | Method | Purpose |
|--------|--------|---------|
| **Cognitive Load** | NASA-TLX questionnaire | Measure mental demand reduction |
| **Artifact Quality** | Spec compliance validation | Ensure story quality |
| **Workflow Efficiency** | Cycle time & lead time | Track planning speed |
| **Baseline Comparison** | Solo developer with traditional tools | Validate improvement |

---

## 🧪 Testing

```bash
# Run all tests
pytest tests/

# Run with coverage (Minimum 80%)
pytest tests/ --cov=. --cov-report=html
```

---

## 🛣️ Roadmap

### ✅ Completed (v1.1)
- [x] product vision Tool (7-component gathering)
- [x] Specification Authority System (Compiler & Validation Gates)
- [x] Backlog Primer (Gross Requirements Generation)
- [x] Roadmap Builder (Now/Next/Later prioritization)
- [x] User Story Writer ("Three Cs" & INVEST validation)
- [x] WorkflowEvent metrics capture

### 🔜 Planned (v1.2)
- [ ] Sprint Planner (Scope "Pull" & Task Decomposition)
- [ ] Automated Spec Updates via Feedback
- [ ] Task breakdown from stories
- [ ] Burndown chart visualization

### 🔮 Future
- [ ] Multi-project portfolio view
- [ ] Integration with GitHub/Jira

---

## 📚 Documentation

- [PLANNING_WORKFLOW.md](PLANNING_WORKFLOW.md) - Detailed workflow documentation
- [SPEC_DRIVEN_ARCHITECTURE_PLAN.md](SPEC_DRIVEN_ARCHITECTURE_PLAN.md) - Spec Authority Architecture details
- [.github/copilot-instructions.md](.github/copilot-instructions.md) - AI agent coding guidelines

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 👤 Author

**Alexandre Tavares**
- GitHub: [@arduinitavares](https://github.com/arduinitavares)
