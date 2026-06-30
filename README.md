# AgileForge

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Google ADK](https://img.shields.io/badge/Google-ADK-orange.svg)](https://github.com/google/adk-python)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

> **A developer tool for agent-assisted agile planning and execution, governed by Spec Authority.**

AgileForge orchestrates the full agile backbone from product vision to sprint execution. It gives agents a bounded workflow, a SQLite-backed project memory, and a read-only CLI for inspecting project context safely.

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
| **Product Vision Tool** | Product Owner | **Strategic Initiation:** Constructs a 7-component "True North" vision statement using the "Bucket Brigade" stateless pattern. |
| **Spec Authority Compiler** | Architect | **Feasibility Filter:** A non-conversational compiler that extracts deterministic "Definition of Done" constraints from technical specs. |
| **Backlog Primer** | Product Owner | **Pre-Planning:** Converts Vision into a prioritized list of Gross Requirements (not User Stories) using T-Shirt sizing. |
| **Roadmap Builder** | Product Owner | **Strategic Planning:** Maps requirements to time-based milestones, respecting technical dependencies and themes. |
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

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          Orchestrator Agent                              │
│           (Explicit FSM, Registry & Bucket Brigade Routing)              │
├──────────────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐  ┌────────────┐  │
│  │ Product      │   │ Spec Auth    │   │ Backlog      │  │ Roadmap    │  │
│  │ Vision Tool  │   │ Compiler     │   │ Primer       │  │ Builder    │  │
│  └──────────────┘   └──────────────┘   └──────────────┘  └────────────┘  │
│                               │                                          │
│                        ┌──────▼──────┐                                   │
│                        │ Spec Registry│                                  │
│                        │ & Authority  │                                  │
│                        └──────┬───────┘                                  │
│                               │                                          │
│  ┌────────────────────────────▼─────────────────────────────────────────┐│
│  │              Tactical & Execution Tools                              ││
│  │  (User Story Writer -> Sprint Planner -> Execution)                  ││
│  └──────────────────────────────────────────────────────────────────────┘│
├──────────────────────────────────────────────────────────────────────────┤
│                          SQLite Database                                 │
│  (Products, Specs, CompiledAuthority, Epics, Stories)                    │
└──────────────────────────────────────────────────────────────────────────┘
```

### Design Patterns
- **Explicit FSM**: Control flow logic separated from LLM reasoning; states defined in registry.
- **Spec Authority Pattern**: Compiler pattern for deterministic invariants.
- **Bucket Brigade Communication**: Agents pass structured state through the orchestrator.
- **Schema-Driven Validation**: All I/O validated by Pydantic schemas.
- **Tool Context Caching**: Read-only tools support transparent caching with TTL.

---

## 🚀 Quick Start

### Prerequisites
- Python 3.12+
- [Poetry](https://python-poetry.org/) or pip
- OpenRouter API key (for LLM access)

### Installation

```bash
# Clone the repository
git clone https://github.com/arduinitavares/agileforge.git
cd agileforge

# Install dependencies
pip install -e .
# or with Poetry
poetry install

# Set up environment variables
cp .env.example .env
# Then edit .env and set:
# - OPEN_ROUTER_API_KEY
# - AGILEFORGE_DB_URL
# - AGILEFORGE_SESSION_DB_URL
```

### Agent CLI

The CLI is the supported agent interface for workflow inspection and guarded
mutations. For the full contract, workflows, idempotency rules, and recovery
guidance, see [docs/agent-cli-manual.md](docs/agent-cli-manual.md).
For the `grill-with-docs` -> `to-prd` -> AgileForge Scope Discovery handoff,
see [docs/scope-discovery-agent-runbook.md](docs/scope-discovery-agent-runbook.md).
For direct spec-file project creation, create and validate an AgileForge profile
spec at `specs/spec.json`; Markdown specs are review views, not project-create
inputs.

```bash
uv run --frozen agileforge --help
uv run --frozen agileforge project list
uv run --frozen agileforge status --project-id 1
uv run --frozen agileforge context pack --project-id 1 --phase sprint-planning
uv run --frozen agileforge project create --name "Example" --spec-file specs/spec.json --idempotency-key create-example-001
```

### Running the Application

```bash
# Start the deterministic FastAPI interface
uvicorn api:app --reload

# Open the dashboard at http://localhost:8000/dashboard
```

---

## 📖 Usage Examples

### 1. Create a New Product Vision

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
I will now generate the Initial Product Backlog (Gross Requirements) before we build the Roadmap.

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
├── orchestrator_agent/
│   ├── agent.py                     # Root agent with all tools
│   ├── instructions.txt             # State machine routing
│   └── agent_tools/
│       ├── product_vision_tool/           # Vision gathering (Stage 1)
│       ├── spec_authority_compiler_agent/ # Spec Compiler (Feasibility)
│       ├── backlog_primer/                # Gross Requirements (Pre-Planning)
│       ├── roadmap_builder/               # Roadmap (Stage 2)
│       ├── user_story_writer_tool/        # Story Refinement ("Three Cs")
│       └── sprint_planner_tool/           # Sprint Planning (Scope & Tasks)
│
├── tools/
│   ├── orchestrator_tools.py        # Read-only query tools
│   ├── db_tools.py                  # Database mutation tools
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
products ─┬─> spec_registry ─> compiled_spec_authority
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
- **products**: Top-level container
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
- [x] Product Vision Tool (7-component gathering)
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
