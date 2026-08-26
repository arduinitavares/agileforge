# Solo-Operator Sprint Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a durable project-scoped solo owner the default for Sprint planning while preserving explicit named Teams and all existing Sprint controls.

**Architecture:** A shared provider-free ownership service resolves and validates a reserved project owner before attempts. New node attempts fingerprint `owner_kind`; the unchanged v1 Sprint envelope retains the resolved label. Review, acceptance, and packets reload the kind from the attempt/receipt/outcome chain, and acceptance repeats reserved-Team validation transactionally.

**Tech Stack:** Python 3.13, Pydantic, SQLModel/SQLite, FastAPI, argparse, browser JavaScript, Node test runner, Playwright, pytest.

**Spec:** `docs/superpowers/specs/2026-08-26-solo-operator-sprint-ownership-design.md`

## Global Constraints

- Work only in `/Users/aaat/projects/agileforge/.worktrees/issue-224-solo-operator-sprint-ownership` on `dev/issue-224-solo-operator-sprint-ownership` from `085e9ac3003d4178f7ee6c8c62cc8f3222f1f404`.
- Keep `SprintPlanEnvelope v1` and the database schema unchanged.
- Do not invoke providers, generate a Sprint manually, mutate an acceptance profile, push, merge, rebase, or touch protected #223 state.
- Preserve named-Team sharing and all #223 selection, dependency, freshness, lineage, capacity, and human-review controls.
- Use RED, GREEN, REFACTOR and commit only after the full verification and independent-review gates.

---

### Task 1: Shared Ownership Contract and Early Resolution

**Files:**
- Create: `services/sprint_ownership.py`
- Modify: `workflow/contracts.py`
- Modify: `api.py`
- Modify: `services/application.py`
- Modify: `cli/main.py`
- Modify: `cli/workflow_commands.py`
- Test: `tests/services/test_sprint_ownership.py`
- Test: `tests/adapters/test_api_workflow_domain.py`
- Test: `tests/adapters/test_cli_workflow_domain.py`
- Test: `tests/adapters/test_command_renderer.py`

**Interfaces:**
- Produces: `ResolvedSprintOwner`, `resolve_sprint_owner(...)`, `is_reserved_sprint_owner_name(...)`, `SPRINT_OWNER_UNAVAILABLE`, and `SPRINT_OWNER_CONFLICT`.
- Produces: optional public `team_name`; resolved owner is available before replay and attempt creation.

- [ ] **Step 1: Write resolver RED tests**

Add literal-behavior tests for the exact solo key/label, explicit named Team,
blank Project name, reserved override, control-character Project name, and every
preflight collision-matrix row. Each failure asserts the exact error code and
that the Session contains no new Team or ProjectTeam row.

- [ ] **Step 2: Verify resolver tests fail for the missing module**

Run:

```bash
uv run --frozen pytest tests/services/test_sprint_ownership.py -q
```

Expected: collection/import failure because `services.sprint_ownership` does
not exist.

- [ ] **Step 3: Implement the minimal resolver and closed errors**

Implement the approved namespace and a frozen result equivalent to:

```python
class ResolvedSprintOwner(FrozenModel):
    kind: Literal["solo_project", "named_team"]
    key: str
    label: str
```

The resolver accepts one Session, `project_id`, and `team_name: str | None`,
returns the literal result above, and performs no writes.

- [ ] **Step 4: Verify resolver GREEN**

Run the Task 1 resolver test command and require zero failures.

- [ ] **Step 5: Write API/application/CLI RED tests**

Add exact tests proving omission and JSON null resolve solo, explicit named Team
is preserved, whitespace is 422, reserved input is 409
`SPRINT_OWNER_CONFLICT`, CLI `--team-name` is optional, generated workflow
commands omit the placeholder, and every early failure leaves the captured
agent-request list empty.

- [ ] **Step 6: Verify the public-boundary tests fail for the old required contract**

Run the four Task 1 adapter test files with selectors for the new tests. Require
failures caused by required `team_name`, absent error codes, or an attempted
agent request.

- [ ] **Step 7: Implement public optional input and resolve before replay**

Use `SemanticText | None` so omitted/null defaults while an explicit blank
fails validation. Call the shared resolver at the start of
`generate_sprint`; pass its resolved label and kind to replay/build. Keep the
provider contract unchanged.

- [ ] **Step 8: Verify Task 1 GREEN and refactor once**

Run the Task 1 resolver and adapter tests. Remove duplicated namespace or label
logic, then rerun the same commands.

### Task 2: Durable Kind, Legacy Replay, and Artifact Reload

**Files:**
- Modify: `adapters/adk/recipes.py`
- Modify: `services/application.py`
- Modify: `services/node_attempt_replay.py`
- Modify: `services/sprint_ownership.py`
- Modify: `services/read_projections.py`
- Test: `tests/adapters/test_adk_graph_recipes.py`
- Test: `tests/workflow/test_node_attempts.py`
- Test: `tests/services/contracts/test_sprint.py`
- Test: `tests/services/test_durable_product_definition_projections.py`

**Interfaces:**
- Consumes: resolved owner kind and label from Task 1.
- Produces: `load_sprint_owner_evidence(...)` and review projection kinds
  `solo_project`, `named_team`, or `legacy_named_team`.

- [ ] **Step 1: Write durable-evidence RED tests**

Add tests proving new normalized input and its start receipt both contain
`owner_kind`, legacy named replay keeps the old fingerprint, omitted solo cannot
replay legacy, kind/label mismatch conflicts, and the v1 envelope exact six-field
bytes/fingerprint remain unchanged.

- [ ] **Step 2: Verify RED**

Run the focused recipe, node-attempt, and Sprint-contract selectors. Require
failures because owner kind is absent or legacy replay changes identity.

- [ ] **Step 3: Implement legacy-compatible attempt payload and replay merge**

Add optional `owner_kind` parsing to `_SprintRecipePayload`, require a non-null
kind for newly built inputs, and add the Sprint-only replay rule from the spec.
Do not change `RecordSprintPlan` or the v1 envelope.

- [ ] **Step 4: Verify attempt/replay GREEN**

Run the Step 2 selectors and require zero failures.

- [ ] **Step 5: Write owner-evidence reload RED tests**

Create real attempt, receipt, outcome, and artifact rows. Assert exact
`solo_project`, `named_team`, and `legacy_named_team` projection after a fresh
Session. Add malformed, duplicate, missing-one-side, wrong-team-name, wrong
Project, and wrong-plan-fingerprint cases that fail closed.

- [ ] **Step 6: Implement the evidence loader and Sprint review v2**

Join artifact -> successful outcome -> attempt -> start receipt, revalidate all
fingerprints and exact identities, and return kind/key/label. Sprint review
emits `agileforge.planning-artifact-review.v2`; other planning reviews remain
v1.

- [ ] **Step 7: Verify Task 2 GREEN and refactor once**

Run all Task 2 focused tests. Consolidate duplicated evidence checks in the
shared ownership service and rerun.

### Task 3: Transactional Acceptance and Canonical Packets

**Files:**
- Modify: `services/agent_workbench/sprint_phase.py`
- Modify: `workflow/handlers/planning.py`
- Modify: `services/packets/canonical.py`
- Modify: `services/packet_renderer.py`
- Modify: `services/read_projections.py`
- Test: `tests/workflow/test_planning_transitions.py`
- Test: `tests/test_canonical_packets.py`
- Test: `tests/test_packet_renderer.py`

**Interfaces:**
- Consumes: `load_sprint_owner_evidence(...)` and the reserved collision checker.
- Produces: atomic solo Team projection and versioned packet ownership fields.

- [ ] **Step 1: Write acceptance collision RED tests**

Cover absent, current-only, unlinked, other-only, current-plus-other,
multiple-other, and defensive duplicate Team states. Add the exact race: save a
solo artifact with no Team, insert a foreign reserved Team/link, then accept.
Every conflict asserts no current ProjectTeam, Sprint, task, membership, or
decision write. Add forced post-Team projection failure and assert total
rollback. Keep a named shared-Team acceptance/start regression.

- [ ] **Step 2: Verify acceptance RED**

Run the new planning-transition selectors and require the old `_ensure_team`
path to fail the strict collision expectations.

- [ ] **Step 3: Implement transactional revalidation**

Load artifact owner evidence inside acceptance. For `solo_project`, repeat the
exact collision matrix and create/reuse only the exclusive reserved Team. For
`named_team` and `legacy_named_team`, retain `_ensure_team`. Keep all writes in
the existing acceptance transaction/savepoint.

- [ ] **Step 4: Verify acceptance GREEN**

Run the Step 2 selectors and existing acceptance rollback/concurrency tests.

- [ ] **Step 5: Write packet RED tests**

Assert Story `v3` and Task `v4` exact closed shapes with `owner_kind`,
`owner_key`, and unchanged `team_name`; legacy artifacts project
`legacy_named_team`; tampered evidence fails closed; renderer says `Sprint
owner` with kind and exact label.

- [ ] **Step 6: Implement packet versions and rendering**

Load owner evidence from the accepted artifact into packet context. Add the two
new exact fields, advance only the specified versions, and preserve all other
lineage and task identity fields.

- [ ] **Step 7: Verify Task 3 GREEN and refactor once**

Run planning-transition, canonical-packet, packet-renderer, and production-read
tests. Remove no compatibility paths.

### Task 4: Browser Contract, Documentation, and Full Verification

**Files:**
- Modify: `frontend/project.js`
- Modify: `CONTEXT.md`
- Test: `tests/test_workflow_position_display.mjs`
- Test: `tests/e2e/test_single_project_lifecycle_ui.py`

**Interfaces:**
- Consumes: provider-free `sprint_owner` projection and optional API override.
- Produces: visible owner before transport and exact default/override request bodies.

- [ ] **Step 1: Write JavaScript and Playwright RED tests**

Assert the exact solo owner is visible before any request; blank optional input
omits `team_name`; an explicit named Team posts the trimmed exact value; a
reserved value never reaches transport; unavailable/conflicting ownership
blocks generation; reload renders durable kind and label.

- [ ] **Step 2: Verify browser RED**

Run:

```bash
node --test tests/test_workflow_position_display.mjs
uv run --frozen pytest tests/e2e/test_single_project_lifecycle_ui.py -q
```

Expected: failures from the old required Team field and missing ownership
projection/rendering.

- [ ] **Step 3: Implement the minimal browser UI**

Render `Sprint owner` plus the projected default and one optional `Named team
override` input. Omit the request property when blank. Preserve exact candidate
vector, dependency, freshness, duplicate-submission, and stale-action guards.

- [ ] **Step 4: Update timeless domain documentation**

Document solo owner, named Team, legacy named Team, and the fact that a reserved
Team row is an internal persistence carrier rather than a multi-person Scrum
Team.

- [ ] **Step 5: Verify focused GREEN**

Run every focused Python, Node, and Playwright test changed by Tasks 1-4.

- [ ] **Step 6: Run full provider-free gates**

Run exactly:

```bash
uv run --frozen pyrepo-check --all
node --test tests/*.mjs
```

Require both exit zero.

- [ ] **Step 7: Independent reviews and fixes**

Run separate specification, correctness/code-quality, and lean-scope reviews
over the exact branch diff. Resolve every confirmed finding with a new RED test
where behavior changes, then rerun affected focused tests and both full gates.

- [ ] **Step 8: Final integrity and one local commit**

Recheck exact worktree, branch, HEAD ancestry, protected #223 hashes, and full
diff. Stage only #224 files, inspect staged diff and secret patterns, then create
one descriptive local commit. Verify the committed worktree is clean and report
the exact SHA.
