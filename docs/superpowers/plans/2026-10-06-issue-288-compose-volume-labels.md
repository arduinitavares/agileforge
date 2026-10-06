# Issue 288 Compose volume ownership implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Prevent Compose ownership warnings in documented fresh installations and safely silence them for existing installations without replacing volume data.

**Architecture:** Keep `compose.yaml` volumes Compose-managed by default. Create service containers without starting or recreating them before standalone helpers can create volumes. Existing installations with unlabeled volumes use a persistent local Compose override declaring only their existing affected volumes external; names and contents stay intact.

**Tech Stack:** Docker Engine / Compose, Markdown, pytest, PyYAML, uv, Ruff.

**Spec:** User task and [issue #288](https://github.com/arduinitavares/agileforge/issues/288). Baseline: `8bd3107b625955a253ae5b977e37896f80caa88b`.

## Global Constraints

- Surgical scope: only #288 and directly related documentation/tests.
- Git is read-only: no commits, branches, stash, staging, pushes, PRs, or GitHub comments. Leave all changes uncommitted.
- Record task files, verification, and review decisions in the external SDD ledger supplied by the coordinator; per-task ledger entries replace commits.
- Runtime/launcher are Linux-only; native pytest through `uv run --frozen pytest` is allowed. Use only uv for Python commands.
- Reproduction uses only `af288repro*` resources. Never inspect, mount, relabel, remove, or otherwise touch `agileforge-*` volumes or containers. The coordinator owns Docker experiments and cleanup.
- Preserve volume data. No volume deletion/recreation, destructive relabeling, or blanket `external: true` in the shipped Compose file.
- Fresh Codex implementer per task; task review must return both spec and quality verdicts; fresh independent Astra whole-diff final review. Workers must not delegate.

## Review Focus

- Standalone test-gate helpers: Compose must own workspace/cache before the first helper mount; existing running development containers must not be recreated.
- Migration copying/chown: both durable volumes must be created before any copy/helper; production must remain stopped and run as its normal image user later.
- Nondefault project names: creation, helper volume names, restore, attach, and startup must select the same namespace.
- Legacy repair: apply only to volumes verified to exist; inherited volume names and data remain unchanged; default fresh installations remain managed.
- Existing overrides and later maintenance: merge the repair into an existing local override without overwriting secret settings; keep it installed for subsequent Compose calls; document external volume lifecycle.

## Evidence and routing

Docker 29.8.2 / Compose 5.5.1 reproduced both warnings after standalone helper creation (`Labels=null`). `docker compose -p af288reprofresh create production` created labeled volumes with service status `created`. A temporary external override for `af288repro` removed both warnings and preserved `state-288` / `workspace-288` sentinel contents. Docker's [volume reference](https://docs.docker.com/reference/compose-file/volumes/) and [create reference](https://docs.docker.com/reference/cli/docker/compose/create/) support the approach. Context7's required outside-sandbox route is unavailable; primary Docker docs were used.

These are bounded documentation/test changes with a known isolated cause, suitable for explicitly configured native Sol medium implementers. No substantial application implementation is planned. Codex five-hour/weekly allowance and Google quota are not exposed to this session; neither is assumed unlimited or exhausted.

### Task 1: Make fresh helper workflows Compose-first

**Files:**
- Modify: `docs/linux-containers.md` test-gate and development-to-production restore sections.
- Create/test: `tests/test_container_docs.py`.

**Interfaces:**
- Consumes: `compose.yaml` managed `workspace`, `cache`, `production-state` definitions and the runbook's `project_name` shell variable.
- Produces: explicit project-scoped `create --no-recreate development` (with development profile) before the test helper; `create --no-recreate production` before restore copying/helper; project-scoped restore/attach/up commands. A small test module that Task 2 extends.

- [x] Write parametrized failing regression tests against shell-command order in the two relevant runbook sections: project-scoped Compose create before a plain Docker mount, correct service/profile, `--no-recreate`, and managed shipped volume declarations. Assert creation precedes migration copying, not merely chown. Require project selection on subsequent restore/attach/startup commands.
- [x] RED: `uv run --frozen pytest tests/test_container_docs.py -q` (redirect verbose output to the external workspace); expected failure is missing Compose-first commands.
- [x] Make the minimal runbook edit. Explain why helpers must not create volumes, why `create` does not start the app or populate a missing development checkout, and retain the ownership helper and backup/maintenance safeguards.
- [x] GREEN: `uv run --frozen pytest tests/test_container_docs.py tests/test_container_ci.py tests/test_container_build.py tests/container_runtime/test_container_transport.py -q`.
- [x] Lint: `uv run --frozen ruff check tests/test_container_docs.py` and `uv run --frozen ruff format --check tests/test_container_docs.py`.
- [x] Self-review and write task report including exact RED/GREEN commands, exit codes, focused output, and files; coordinator records ledger entry instead of committing.
- [x] Coordinator: task-scoped fresh reviewer verifies spec + quality against the uncommitted diff and actual reports before Task 2.

### Task 2: Document safe one-time configuration repair for existing volumes

**Files:**
- Modify: `docs/linux-containers.md` existing-volume guidance near packaged startup.
- Modify/test: `tests/test_container_docs.py`.

**Interfaces:**
- Consumes: managed default Compose definitions and Task 1's Compose-first runbook.
- Produces: a local `compose.override.yaml` example setting `external: true` for existing `workspace` and `production-state`, inheriting their exact names; inspection and verification commands using the same `project_name`.

- [x] Add a failing regression that extracts/parses the documented YAML override and shipped Compose YAML. Assert only the two existing durable volumes become external in the override, shipped volumes remain managed, and names inherit from the base. Guard existence/label inspection and persistent local override instructions.
- [x] RED: `uv run --frozen pytest tests/test_container_docs.py -q`; expected failure is absent repair section/override.
- [x] Document inspection of the two selected volumes, a persistent local override merged into any existing override, consistent project selection and explicit `-f` usage when applicable. Explain this is a configuration repair, not label mutation; no copying/deletion/recreation, apply only to already-existing volumes, keep it for future calls, and external volumes survive Compose volume cleanup. Link primary Docker documentation. Cover partial cases by marking only affected existing volumes external; never apply this override to an empty installation.
- [x] GREEN and lint: repeat Task 1's exact bounded pytest and Ruff commands after the added test.
- [x] Self-review/report and coordinator ledger entry instead of a commit; fresh task review with both verdicts.

## Final acceptance (coordinator)

- [x] Read `verification-before-completion`; inspect the full uncommitted diff, including new files, and run `git diff --check`.
- [x] Verify the final runbook commands with isolated projects `af288repro` (existing unlabeled) and `af288reprofresh` (fresh managed). Use local reviewed production image for helper shell commands; no host profile, provider, or application-state operations are needed.
- [x] Legacy: inspect volume labels/identity before and after, apply the exact documented override, run a Compose command twice, assert neither stderr contains `not created by Docker Compose`, and assert both sentinel contents unchanged.
- [x] Fresh: run documented project-scoped production creation, helper mounts, and subsequent Compose shell twice; inspect project/volume labels and confirm no warning. Also verify the documented development create command using the local production image as an isolated service-image stand-in, without starting application processes.
- [x] Generate a whole-diff package (tracked diff plus new test and plan) and request fresh independent Astra review with requirements and actual verification evidence. Resolve findings and re-review the affected change if needed.
- [x] Remove every task-created `af288repro*` container, volume, and network by exact project/name; list the prefix to prove none remain. Never use broad prune or touch unrelated resources.
- [x] Preserve the external SDD ledger/evidence; final report pastes the ledger and gives exact before/after commands/results, tests, review disposition, cleanup, risks, and a Conventional Commit message with `Fixes #288`.
