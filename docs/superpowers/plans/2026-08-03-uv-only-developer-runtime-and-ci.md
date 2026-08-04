# UV-Only Developer Runtime And CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide a branch-neutral, uv-only launcher and automated CI so humans and Codex can run any AgileForge worktree's CLI and UI without manual runtime exports or ambiguous global commands.

**Architecture:** A repository-owned `./agileforge-dev` bootstrap resolves its own checkout and runs a lightweight developer CLI from that checkout's locked uv environment. The developer CLI owns worktree-local runtime profiles, delegates all product behavior to the existing `agileforge` CLI/API, and exposes deterministic provenance; GitHub Actions invokes the same uv-owned quality and artifact checks in clean environments.

**Tech Stack:** Python 3.12 and 3.13, uv 0.10.12, Pydantic 2, SQLModel/SQLite, FastAPI/Uvicorn, Git, Node's built-in test runner, GitHub Actions, pytest, Ruff, `ty`, Bandit, and `pyrepo-check` pinned through uv.

## Global Constraints

- Every setup, execution, test, build, installation, and CI command is uv-only.
- Stable `agileforge` remains separate from branch-local `agileforge-dev`.
- The launcher derives identity from its own checkout and contains no hardcoded branch, worktree, commit, profile, database, or port.
- Runtime profiles live only under the selected worktree's `.agileforge/dev/profiles/` directory.
- Business and ADK execution-trace SQLite paths are absolute, distinct, nonsymlinked, and contained by the selected profile root.
- Development profiles may follow commits but fail closed on schema-source drift; acceptance profiles require an exact commit.
- Acceptance profile initialization, load, and use require a clean Git worktree; tracked and nonignored untracked source changes are refused while ignored `.agileforge` state is allowed.
- No provider credential is copied, persisted, logged, hashed, or included in JSON output.
- No database migration, compatibility layer, or legacy database reuse is introduced.
- caRtola, ASA, and MyFinance remain unaccessed and `not_run` throughout implementation.
- Task 19 remains unstarted until revised Operator acceptance evidence or one concrete acceptance failure returns.
- No Docker, additional task runner, routing policy, typing suppression, or unrelated refactor is added.
- The shell owns only checkout and uv isolation policy and has no application, database, or routing policy.
- Every task uses TDD, ends in a commit, passes focused checks, and receives an independent review before the next task starts.

## File Structure

- `agileforge-dev`: dependency-free shell bootstrap that resolves its own directory and invokes the project script with uv.
- `cli/dev_profiles.py`: typed profile model, checkout provenance, path containment, fingerprints, atomic persistence, environment derivation, and reset safety.
- `cli/dev_main.py`: developer command parser and orchestration for `init`, `info`, `cli`, `ui`, `check`, and `reset`.
- `cli/dev_server.py`: loopback port selection, Uvicorn child lifecycle, readiness, and termination.
- `cli/dev_checks.py`: ordered local/CI quality and distribution command runner.
- `scripts/verify_distribution.py`: source/wheel build, isolated uv installation, CLI/resource/API smoke verification.
- `frontend/__init__.py`: package marker for installed frontend resources.
- `tests/dev_runtime/`: focused profile, launcher, CLI, UI, check, and cross-worktree tests.
- `tests/test_distribution_smoke.py`: unit contracts for isolated distribution verification.
- `tests/test_ci_contract.py`: structural CI workflow contract.
- `tests/test_uv_only_docs.py`: uv-only documentation and branch-command contract.
- `.github/workflows/ci.yml`: pinned, read-only pull-request/default-branch/manual CI.

---

### Task 1: Build The Worktree-Local Runtime Profile Core

**Files:**
- Create: `cli/dev_profiles.py`
- Create: `tests/dev_runtime/__init__.py`
- Create: `tests/dev_runtime/test_profiles.py`

**Interfaces:**
- Consumes: `workflow.contracts.GRAPH_VERSION`, the checkout's `config/models.yaml`, Git command output, and the current model source files.
- Produces: `ProfileMode`, `CheckoutProvenance`, `RuntimeProfile`, `ProfilePaths`, `resolve_checkout_root()`, `profile_paths()`, `initialize_profile_record()`, `load_profile()`, `touch_profile_last_used()`, `profile_environment()`, and `reset_profile()`.

- [ ] **Step 1: Write profile-path and branch-neutral provenance tests**

Cover valid names, rejected traversal/control-character names, canonical checkout discovery from a nested path, branch and detached-HEAD provenance, and identical profile names under two checkout roots producing distinct absolute paths.

```python
def test_same_profile_name_is_isolated_by_checkout(tmp_path: Path) -> None:
    first = profile_paths(checkout_root=tmp_path / "one", profile_name="local")
    second = profile_paths(checkout_root=tmp_path / "two", profile_name="local")

    assert first.root != second.root
    assert first.business_database != second.business_database
    assert first.trace_database != second.trace_database
```

- [ ] **Step 2: Run the path/provenance tests and verify RED**

Run:

```bash
uv run --locked pytest tests/dev_runtime/test_profiles.py -q
```

Expected: collection fails because `cli.dev_profiles` does not exist.

- [ ] **Step 3: Define the frozen profile contracts**

Implement these public shapes with `ConfigDict(frozen=True, extra="forbid")`:

```python
class ProfileMode(StrEnum):
    DEVELOPMENT = "development"
    ACCEPTANCE = "acceptance"


class CheckoutProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    root: Path
    branch: str | None
    commit: str


class RuntimeProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["1"]
    name: str
    mode: ProfileMode
    checkout: CheckoutProvenance
    expected_commit: str | None
    graph_version: str
    python_version: str
    uv_version: str
    business_database: Path
    trace_database: Path
    model_config_path: Path
    model_config_sha256: str
    schema_source_sha256: str
    created_at: datetime
    last_used_at: datetime
```

`ProfileMode.ACCEPTANCE` requires a 40-character lowercase hexadecimal expected commit equal to the current commit. Development mode requires `expected_commit is None`.

- [ ] **Step 4: Implement checkout resolution and safe profile paths**

`resolve_checkout_root(anchor)` runs fixed-argv `git -C <anchor> rev-parse --show-toplevel`; provenance separately reads `HEAD` and `--abbrev-ref HEAD`. Validate profile names with `^[a-z0-9][a-z0-9._-]{0,63}$`. Build paths as:

```python
root = checkout_root / ".agileforge" / "dev" / "profiles" / profile_name
```

Use `lstat()` and `Path.is_relative_to()` before any read, write, or removal. Reject symlinked state ancestors and any resolved path outside the checkout-local profiles directory.

- [ ] **Step 5: Write fingerprint, manifest, and drift tests**

Tests must prove deterministic SHA-256 values, atomic JSON persistence, unknown-field rejection, model-config drift rejection, schema-source drift rejection, acceptance commit rejection, development commit advancement acceptance, distinct DB enforcement, and secret-shaped fields being forbidden.

The schema-source fingerprint is the canonical hash of relative path plus bytes for `agile_sqlmodel.py` and every tracked `models/*.py` file, sorted by POSIX path. This deliberately fails safe on any model-layer change.

- [ ] **Step 6: Implement atomic profile persistence and validation**

Write `profile.json` to a same-directory temporary file with mode `0o600`, flush and `fsync`, then replace atomically. `load_profile()` validates containment, current checkout, graph version, model-config hash, schema-source hash, and mode-specific commit rules before returning data. `touch_profile_last_used()` atomically advances only `last_used_at` after a successful `info`, `cli`, or `ui` profile validation while preserving `created_at` and every immutable field.

`profile_environment()` returns exactly:

```python
{
    "AGILEFORGE_DB_URL": f"sqlite:///{profile.business_database.as_posix()}",
    "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL": (
        f"sqlite:///{profile.trace_database.as_posix()}"
    ),
    "MODEL_CONFIG_PATH": str(profile.model_config_path),
}
```

- [ ] **Step 7: Implement reset refusal and owned cleanup**

`reset_profile(checkout_root, profile_name, confirmation)` requires exact name equality, revalidates every path, refuses symlinks, and removes only the validated profile root. Return the removed paths for reporting.

- [ ] **Step 8: Run focused static and profile tests**

Run:

```bash
uv run --locked ruff check cli/dev_profiles.py tests/dev_runtime/test_profiles.py
uv run --locked ty check
uv run --locked pytest tests/dev_runtime/test_profiles.py -q
git diff --check
```

Expected: all commands pass with no suppression.

- [ ] **Step 9: Commit Task 1**

```bash
git add cli/dev_profiles.py tests/dev_runtime
git commit -m "feat: add worktree-local runtime profiles"
```

---

### Task 2: Add The UV-Owned Developer Bootstrap, Init, Info, And Reset Commands

**Files:**
- Create: `agileforge-dev`
- Create: `cli/dev_main.py`
- Create: `tests/dev_runtime/test_dev_main.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: Task 1 profile contracts and `agile_sqlmodel.py` schema bootstrap.
- Produces: project script `agileforge-dev = "cli.dev_main:main"`, executable root bootstrap, `build_parser()`, `main()`, `init`, `info`, and `reset`.

- [ ] **Step 1: Write parser and bootstrap contract tests**

Require all designed commands and options, structured exit codes, JSON output,
and an executable bootstrap whose final execution line is exactly:

```sh
exec uv --directory "$ROOT" run --locked agileforge-dev "$@"
```

The bootstrap derives `ROOT` from its own canonical directory, rejects a
symlinked bootstrap path, and contains no branch, worktree, database, profile,
port, or user-home literal. Before exec it removes hostile source, project,
workdir, environment, interpreter, and sync selectors: `PYTHONHOME`,
`PYTHONPATH`, `PYTHONUSERBASE`, `VIRTUAL_ENV`, `UV_NO_EDITABLE`, `UV_PROJECT`,
`UV_PROJECT_ENVIRONMENT`, `UV_NO_SYNC`, `UV_WORKING_DIR`,
`UV_WORKING_DIRECTORY`, `UV_NO_PROJECT`, `UV_CONFIG_FILE`, `UV_ENV_FILE`,
`UV_FROZEN`, `UV_ISOLATED`, `UV_LOCKED`, `UV_MANAGED_PYTHON`, `UV_NO_CONFIG`,
`UV_NO_MANAGED_PYTHON`, and `UV_PYTHON`. It retains harmless uv cache, offline,
and certificate settings. Tests must set hostile values and prove the checkout's
`cli.dev_main`, project, lock, and environment still execute.

- [ ] **Step 2: Run launcher tests and verify RED**

Run:

```bash
uv run --locked pytest tests/dev_runtime/test_dev_main.py -q
```

Expected: failures because the bootstrap, parser, and project script are absent.

- [ ] **Step 3: Add the project script and root bootstrap**

Add to `[project.scripts]`:

```toml
agileforge = "cli.main:main"
agileforge-dev = "cli.dev_main:main"
```

The shell bootstrap uses `set -eu`, canonicalizes its directory without
consulting caller CWD, sanitizes only the source and uv isolation selectors
listed above, and delegates to uv. It owns only checkout and uv isolation
policy, with no application, database, or routing policy. Make it executable with
`chmod +x agileforge-dev`.

- [ ] **Step 4: Implement the developer parser without eager production imports**

`cli/dev_main.py` may import stdlib, Pydantic result contracts, and `cli.dev_profiles` at module import time. It must lazily import application/database modules only after a profile environment is installed.

Define subcommands and exact required options from the specification. `info`,
`cli`, and `ui` accept an optional `--secrets-file PATH`; `cli` accepts `--json`;
`ui` accepts `--ephemeral`, `--port`, `--reload`, `--json`, and
`--ready-timeout`. Add injectable protocols for command execution and clocks so
unit tests do not run uv or touch production databases. Keep
`profile_environment()` at exactly three profile keys, then add the fixed
non-secret launcher-child dotenv-disable control to every schema, CLI, and UI
child environment.

- [ ] **Step 5: Write init atomicity tests**

Prove that `init` runs `uv lock --check`, creates private state, launches schema bootstrap with separate DB environment, verifies expected tables, writes the manifest only after success, removes incomplete state after failure, and is idempotent only for an exact match.

Expected business tables include `projects`, `spec_registry`, and `workflow_events`; removed `products`, `sessions`, and the deleted mutation ledger remain absent.

- [ ] **Step 6: Implement init and schema verification**

Use fixed argv rooted at the checkout:

```python
("uv", "lock", "--check")
(sys.executable, str(checkout_root / "agile_sqlmodel.py"))
```

Open the resulting SQLite file read-only for table verification. Do not create the ADK trace DB during business bootstrap; reserve its distinct path for ADK execution.

- [ ] **Step 7: Implement info and reset output**

`info --json` emits the complete redacted profile provenance, current checkout
commit, validation status, typed `configured_models`, boolean-only
`provider_credentials`, and exact non-secret `child_runtime_environment`.
Optional `--secrets-file` uses the existing descriptor-safe allowlist and
precedence path. Human output is concise and sends no credential values. Keep
imports lazy. `reset` calls Task 1's owned cleanup only after exact confirmation.

- [ ] **Step 8: Run focused checks and update the lock**

Run:

```bash
uv lock
uv lock --check
uv run --locked ruff check cli/dev_main.py cli/dev_profiles.py tests/dev_runtime
uv run --locked ty check
uv run --locked pytest tests/dev_runtime/test_profiles.py tests/dev_runtime/test_dev_main.py -q
./agileforge-dev --help
git diff --check
```

Expected: all commands pass and help names `init`, `info`, `cli`, `ui`, `check`, and `reset`.

- [ ] **Step 9: Commit Task 2**

```bash
git add agileforge-dev cli/dev_main.py pyproject.toml uv.lock tests/dev_runtime
git commit -m "feat: add uv-owned developer launcher"
```

---

### Task 3: Forward Agent CLI Commands With Provenance And Prove Cross-Worktree Isolation

**Files:**
- Modify: `cli/dev_main.py`
- Modify: `cli/main.py`
- Create: `tests/dev_runtime/test_cli_forwarding.py`
- Create: `tests/dev_runtime/test_cross_worktree.py`
- Modify: `tests/adapters/test_cli_workflow_domain.py`

**Interfaces:**
- Consumes: validated runtime profiles and the production `cli.main` parser.
- Produces: `agileforge-dev cli`, stable `agileforge --version`, raw JSON forwarding, combined launcher JSON envelopes, and full linked-worktree isolation evidence.

- [ ] **Step 1: Write CLI forwarding and version tests**

Require `agileforge --version` to return installed package version without composing the production application. For developer forwarding, verify exact argument preservation after `--`, fresh one-shot execution, profile-derived environment, checkout-root CWD, stdout preservation, stderr provenance, exit-code preservation, and JSON envelope redaction.

```python
def test_cli_forwarding_ignores_path_selected_agileforge(
    run_dev_cli: DevCliRunner,
) -> None:
    result = run_dev_cli(["cli", "--profile", "local", "--", "project", "list"])
    assert result.argv[:3] == (sys.executable, "-m", "cli.main")
    assert "/Users/aaat/.local/bin/agileforge" not in result.argv
```

- [ ] **Step 2: Run forwarding tests and verify RED**

Run:

```bash
uv run --locked pytest tests/dev_runtime/test_cli_forwarding.py tests/adapters/test_cli_workflow_domain.py -q
```

Expected: failures because forwarding and `--version` are absent.

- [ ] **Step 3: Implement lazy fresh-process forwarding**

Build the child argv as:

```python
(sys.executable, "-m", "cli.main", *forwarded_arguments)
```

Use the validated checkout as CWD and overlay only profile-owned non-secret variables plus the allowlisted `OPEN_ROUTER_API_KEY` provider credential. If `--secrets-file PATH` is supplied, parse it with `dotenv_values()`, reject symlinks and non-files, import only `OPEN_ROUTER_API_KEY`, and let an already-present invoking-environment value win. Ignore every database, checkout, model-path, and runtime-control key. Never place the key value in a manifest, log, hash, JSON envelope, exception, or child argv. Raw mode does not parse or rewrite production stdout. JSON mode parses one production JSON object and wraps it with checkout, commit, profile, database, command, and exit-code provenance.

- [ ] **Step 4: Add stable version output**

Use `services.agent_workbench.version.agileforge_version()` for `agileforge --version`. Parser help and all existing command behavior remain unchanged.

- [ ] **Step 5: Write a real linked-worktree integration test**

Create a local temporary clone of the current repository. In that clone, create one launcher-containing commit, then a second harmless launcher-containing commit by changing a tracked test fixture. Add one linked worktree at each commit, initialize profile name `local` in each, and call each worktree's own `./agileforge-dev info --profile local --json` and `cli --profile local -- project list`.

Assert different canonical checkout roots, commits, profile roots, business DBs, and trace DBs. Assert both CLIs report their own checkout commit and neither profile tree changes while the other command runs. The test uses only local Git data and temporary SQLite files.

- [ ] **Step 6: Run forwarding and cross-worktree tests**

Run:

```bash
uv run --locked pytest \
  tests/dev_runtime/test_cli_forwarding.py \
  tests/dev_runtime/test_cross_worktree.py \
  tests/adapters/test_cli_workflow_domain.py -q
uv run --locked ty check
git diff --check
```

Expected: all pass; no command resolves the user-level shim.

- [ ] **Step 7: Commit Task 3**

```bash
git add cli/dev_main.py cli/main.py tests/dev_runtime tests/adapters/test_cli_workflow_domain.py
git commit -m "feat: run branch cli with explicit provenance"
```

---

### Task 4: Add Managed UI Lifecycle And Package Frontend Assets

**Files:**
- Create: `cli/dev_server.py`
- Modify: `cli/dev_main.py`
- Create: `frontend/__init__.py`
- Modify: `api.py`
- Modify: `pyproject.toml`
- Create: `tests/dev_runtime/test_dev_server.py`
- Modify: `tests/dev_runtime/test_cross_worktree.py`
- Create: `tests/test_frontend_package_resources.py`

**Interfaces:**
- Consumes: validated profile environments and packaged `frontend` resources.
- Produces: `select_loopback_port()`, `start_ui()`, `wait_for_readiness()`, `stop_ui()`, `agileforge-dev ui`, and package-safe dashboard mounting.

- [ ] **Step 1: Write server lifecycle and resource tests**

Cover loopback-only binding, automatic port selection, bounded retry, readiness
timeout, foreground reload arguments, non-reloading agent mode, graceful
terminate/kill fallback, per-launch nonce authentication for reload and
non-reload readiness, JSON readiness output, ephemeral cleanup after both
success and failure, preservation across restart for a non-ephemeral
development profile, and frontend resources available through
`importlib.resources.files("frontend")`.

Mark live loopback tests with the repository's explicit localhost socket marker. No external socket is enabled.

- [ ] **Step 2: Run server tests and verify RED**

Run:

```bash
uv run --locked pytest \
  tests/dev_runtime/test_dev_server.py \
  tests/test_frontend_package_resources.py -q
```

Expected: failures because lifecycle helpers and package resources are absent.

- [ ] **Step 3: Implement package-safe frontend resolution**

Make `frontend` a package, include `*.html` and `*.js` as package data, and replace the CWD-relative mount with:

```python
from importlib.resources import files

_FRONTEND_ROOT = files("frontend")
app.mount(
    "/dashboard",
    StaticFiles(directory=str(_FRONTEND_ROOT), html=True),
    name="frontend",
)
```

Add a build-system declaration using `setuptools.build_meta`; retain existing package discovery and explicitly include the frontend package.

- [ ] **Step 4: Implement UI child management**

Launch fixed argv with `sys.executable -m uvicorn api:app`, checkout CWD,
validated profile environment, host `127.0.0.1`, and selected port. Generate a
fresh non-secret launch nonce for every attempt, pass it only to the UI child or
reload supervisor, expose it in dashboard config, and require it in readiness
identity. Agent mode waits on `/api/dashboard/config`, emits readiness JSON, and
keeps the child attached until interrupted. Human `--reload` mode stays
foreground. On shutdown, terminate, wait with a finite timeout, then kill only
the tracked child if necessary. `--ephemeral` creates a unique launcher-owned
acceptance child profile, never reuses the named parent databases, and removes
that child profile in a `finally` block without removing the parent profile.

- [ ] **Step 5: Extend cross-worktree isolation to concurrent UI processes**

Start one non-reloading UI per temporary worktree using the same profile name and `--port auto`. Assert distinct ports, commits, roots, and databases; query both dashboard configs; then terminate both and assert no child remains.

- [ ] **Step 6: Run focused CLI/API/frontend checks**

Run:

```bash
uv run --locked pytest \
  tests/dev_runtime/test_dev_server.py \
  tests/dev_runtime/test_cross_worktree.py \
  tests/test_frontend_package_resources.py \
  tests/adapters/test_api_workflow_domain.py -q
node --test tests/test_workflow_position_display.mjs tests/test_create_project_modal_required_fields.mjs
uv run --locked ty check
git diff --check
```

Expected: all pass with no external network access.

- [ ] **Step 7: Commit Task 4**

```bash
git add cli/dev_server.py cli/dev_main.py frontend api.py pyproject.toml uv.lock tests/dev_runtime tests/test_frontend_package_resources.py
git commit -m "feat: run branch ui with isolated profiles"
```

---

### Task 5: Pin The UV Quality Gate And Verify Built Distributions

**Files:**
- Create: `cli/dev_checks.py`
- Modify: `cli/dev_main.py`
- Create: `scripts/verify_distribution.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/dev_runtime/test_dev_checks.py`
- Create: `tests/test_distribution_smoke.py`

**Interfaces:**
- Consumes: Tasks 1-4 launcher, UI, packaging, and profile commands.
- Produces: `agileforge-dev check`, immutable uv ownership of `pyrepo-check`, and an isolated source/wheel verification command.

- [ ] **Step 1: Pin pyrepo-check through uv**

Add this immutable development requirement and refresh the lock:

```toml
"pyrepo-check @ git+https://github.com/arduinitavares/pyrepo-check.git@8f88465e1ca88bf29b508f3c0f4eb96f4de31623"
```

Run `uv lock` and prove `uv run --locked pyrepo-check --all` resolves the locked command rather than `/Users/aaat/.local/bin/pyrepo-check`.

- [ ] **Step 2: Write ordered check-runner tests**

With an injected fixed-argv runner, require this fail-fast order:

```text
uv lock --check
uv run --locked pyrepo-check --all
node --test tests/test_workflow_position_display.mjs tests/test_create_project_modal_required_fields.mjs
git diff --check
uv run --locked python scripts/verify_distribution.py
```

No shell interpolation or PATH-selected quality executable is permitted.

- [ ] **Step 3: Run check tests and verify RED**

Run:

```bash
uv run --locked pytest tests/dev_runtime/test_dev_checks.py -q
```

Expected: failure because `cli.dev_checks` and `check` orchestration are absent.

- [ ] **Step 4: Implement the fail-fast quality runner**

Return a typed result containing command, exit code, elapsed time, and failed stage. Human mode streams output. JSON mode captures bounded summaries and artifact paths without swallowing the underlying command's nonzero status.

- [ ] **Step 5: Write isolated distribution verification tests**

Test command construction and safety with temporary `UV_TOOL_DIR`, `UV_TOOL_BIN_DIR`, build output, state root, and working directory outside the checkout. Create ignored stale `build/` and egg-info state, then prove a clean Git-indexed working-tree snapshot excludes it, retains tracked working-tree bytes, and leaves the checkout unchanged. Require source and wheel archives to contain model config and frontend resources. Require isolated installation of each artifact to expose `agileforge --help`, `agileforge --version`, current graph parsers, fresh schema bootstrap, dashboard readiness, `/position`, and no `/state` route.

- [ ] **Step 6: Implement `scripts/verify_distribution.py`**

The script uses Git to copy only tracked paths with their current working-tree bytes into a stdlib temporary directory, excluding ignored build and egg-info contamination without mutating the checkout. It then uses only uv subprocesses for build and installation. It runs this sequence once for the wheel and once for the source distribution, with a fresh tool directory and bin directory for each artifact:

```text
uv build --no-sources --out-dir <temporary-dist>
uv tool install --force <artifact>
<temporary-bin>/agileforge --help
<temporary-bin>/agileforge --version
```

It asserts that exactly one wheel and one source distribution were built. For each artifact, it then starts the installed API in a fresh subprocess with separate temporary DBs and queries readiness/OpenAPI over loopback. It always terminates its child and leaves the source checkout unchanged.

- [ ] **Step 7: Run the complete local check command**

Run:

```bash
uv lock --check
./agileforge-dev check
git status --short
```

Expected: all quality, tests, frontend, whitespace, build, installed CLI, and installed UI checks pass; tracked worktree remains clean except intended Task 5 changes before commit.

- [ ] **Step 8: Commit Task 5**

```bash
git add cli/dev_checks.py cli/dev_main.py scripts/verify_distribution.py pyproject.toml uv.lock tests/dev_runtime tests/test_distribution_smoke.py
git commit -m "build: add uv-owned quality and artifact checks"
```

---

### Task 6: Add Pinned Pull-Request CI

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `tests/test_ci_contract.py`

**Interfaces:**
- Consumes: `./agileforge-dev check`, the locked uv project, and fresh launcher profiles.
- Produces: automatic pull-request/default-branch/manual verification on Python 3.12, Python 3.13, Node, Ubuntu, and macOS.

- [ ] **Step 1: Write structural CI tests**

Parse workflow YAML with a loader that preserves the literal `on` key. Require:

- `pull_request`, default-branch `push`, and `workflow_dispatch` triggers;
- top-level `permissions: contents: read`;
- concurrency keyed by workflow and ref with cancellation;
- Python 3.12 and 3.13 Ubuntu jobs;
- one macOS 3.13 smoke job;
- no provider secrets or live integration marker;
- full-SHA action pins;
- uv version `0.10.12`;
- `uv lock --check` and repository-owned checks;
- artifact smoke outside the source checkout.

- [ ] **Step 2: Run CI tests and verify RED**

Run:

```bash
uv run --locked pytest tests/test_ci_contract.py -q
```

Expected: failure because `.github/workflows/ci.yml` is absent.

- [ ] **Step 3: Implement the pinned workflow**

Use these verified immutable action revisions:

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
- uses: astral-sh/setup-uv@11f9893b081a58869d3b5fccaea48c9e9e46f990 # v8.3.2
- uses: actions/setup-node@249970729cb0ef3589644e2896645e5dc5ba9c38 # v6.5.0
```

The Python 3.12 job runs the locked repository test/quality gate. Python 3.13 runs `./agileforge-dev check`, including package verification. The Node job runs the two frontend suites. The macOS job initializes an ephemeral profile and verifies `info`, `cli project list`, and non-reloading UI readiness.

- [ ] **Step 4: Run workflow contracts and local equivalents**

Run:

```bash
uv run --locked pytest tests/test_ci_contract.py -q
uv run --locked pyrepo-check --all
node --test tests/test_workflow_position_display.mjs tests/test_create_project_modal_required_fields.mjs
git diff --check
```

Expected: all pass.

- [ ] **Step 5: Commit Task 6**

```bash
git add .github/workflows/ci.yml tests/test_ci_contract.py
git commit -m "ci: verify uv-owned branch runtime"
```

---

### Task 7: Replace Manual Guidance And Regenerate The Acceptance Package

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `AGENTS.md`
- Modify: `docs/agent-cli-manual.md`
- Modify: `docs/testing/workflow-graph-acceptance-checklist.md`
- Modify: `tests/test_workflow_acceptance_document.py`
- Create: `tests/test_uv_only_docs.py`
- Modify: `.superpowers/sdd/task-18-report.md`
- Modify: `.superpowers/sdd/task-18-review.md` during independent review only

**Interfaces:**
- Consumes: the completed launcher, profile evidence, quality gate, CI, and distribution behavior.
- Produces: uv-only human/agent documentation, durable fresh-session branch guidance, and a newly reviewed Task 18 acceptance package at the final HEAD.

- [ ] **Step 1: Write documentation contract tests**

Require installation and development sections to use only uv, require every worktree acceptance example to use that checkout's `agileforge-dev`, require `info --json` before mutation, require separate database provenance, and require the exact cross-worktree rule in `AGENTS.md`.

Construct forbidden installer names inside the test from string fragments so the repository's current documentation scan does not contain stale executable examples.

- [ ] **Step 2: Run documentation tests and verify RED**

Run:

```bash
uv run --locked pytest tests/test_uv_only_docs.py tests/test_workflow_acceptance_document.py -q
```

Expected: failures against the current README, incomplete environment example, plain worktree commands, and old checklist setup block.

- [ ] **Step 3: Rewrite current operating documentation**

Document these exact boundaries:

```text
Stable release: agileforge workflow next --project-id 1
Current checkout: ./agileforge-dev cli --profile local -- workflow next --project-id 1
Current checkout UI: ./agileforge-dev ui --profile local --port auto
Provenance: ./agileforge-dev info --profile local --json
```

Remove every forbidden non-uv installation instruction. Add both required DB variables and `MODEL_CONFIG_PATH` to `.env.example` without embedding real credentials or legacy paths.

- [ ] **Step 4: Register the fresh-session rule in `AGENTS.md`**

Add a concise repository rule:

```markdown
## Development Branch Runtime

Use only uv. For a development branch or linked worktree, invoke that checkout's
`./agileforge-dev`; never use a bare or user-level `agileforge` shim. Run
`info --json` before mutations. Each worktree owns separate profiles, business
and ADK trace databases, and UI ports. Older branches must merge or rebase the
launcher change before using it.
```

- [ ] **Step 5: Replace repeated acceptance exports with profile evidence**

Keep the exact Operator evidence and safety requirements, but initialize one SHA-pinned acceptance profile per repository. Every step records `agileforge-dev info --json`, the exact forwarded CLI argv, and the production JSON result. Acceptance remains `not_run`; do not execute against external repositories.

- [ ] **Step 6: Run complete verification**

Run:

```bash
./agileforge-dev check
uv run --locked pytest tests/test_uv_only_docs.py tests/test_workflow_acceptance_document.py -q
rg -n "AGILEFORGE_SESSION_DB_URL|/Users/aaat/.local/bin/agileforge" \
  README.md .env.example AGENTS.md docs/agent-cli-manual.md \
  docs/testing/workflow-graph-acceptance-checklist.md cli tests
git diff --check
git status --short
```

Expected: full gate passes; scans return no current guidance using removed session configuration or the user-level shim; external repositories remain untouched.

- [ ] **Step 7: Commit Task 7 implementation**

```bash
git add README.md .env.example AGENTS.md docs/agent-cli-manual.md \
  docs/testing/workflow-graph-acceptance-checklist.md \
  tests/test_workflow_acceptance_document.py tests/test_uv_only_docs.py
git commit -m "docs: adopt uv-only branch runtime"
```

- [ ] **Step 8: Regenerate and independently review Task 18 evidence**

Finish and commit all implementation, tests, and operating documentation first;
name the last such commit `IMPLEMENTATION_HEAD`. Run the complete gate at that
clean commit. Generate the immutable binary diff from `cb3e32c` through
`IMPLEMENTATION_HEAD` and record its SHA-256. Then create one evidence-only
commit that updates `.superpowers/sdd/task-18-report.md` with the exact
implementation coordinate, package path/hash, launcher/CI/package evidence,
full gate, and explicit `not_run` status. The report and review evidence commits
are outside the implementation package; do not chase a self-referential report
commit hash.

Expected reviewer verdict before handoff:

```text
0 Critical, 0 Important, 0 Minor
caRtola: not_run
ASA: not_run
MyFinance: not_run
Task 19: not started
```

Stop after the approved checklist handoff. Do not begin external acceptance or Task 19 in the implementation session.

### Final Review Remediation Verification

- Add direct typed-failure tests for malformed initial-spec content and content
  fingerprint mismatch. Both must return `INITIAL_SPEC_DRAFT_INVALID`.
- Add one real bounded Unix-local composition test around the production
  `ProcessGroup`, `LocalRuntime.stop_ui`, and `dev_server.stop_ui`. A stubborn
  process group ignores TERM; the test proves timeout, KILL, final reap, and no
  surviving process or group.
- Add real child-process regressions for checkout dotenv isolation, hostile uv
  controls, dirty acceptance source, redacted operator preflight, and foreign
  same-coordinate reload readiness.
