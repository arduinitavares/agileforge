# UV-Only Developer Runtime And CI Design

## Status

Approved direction, pending implementation planning.

This design pauses external acceptance until AgileForge has a repository-owned,
uv-only developer launcher and automated CI. It does not start Task 19 and does
not claim acceptance for caRtola, ASA, or MyFinance.

## Problem

The reviewed workflow-graph implementation is isolated in a Git worktree, but
the current local `agileforge` executable is a hand-written user-level shim that
always points at the main checkout. It is not managed by `uv tool`, does not
enforce the lockfile, and can silently run a different branch and database than
the operator intended.

The reviewed acceptance checklist avoids that ambiguity by spelling out the
worktree, commit, database paths, schema bootstrap, and every environment
variable. Those primitives are appropriate evidence, but they are too manual
for normal development, UI use, or repeated Codex-agent interaction.

The repository also has no tracked CI workflow, no repository-owned developer
launcher, no runtime provenance command, and no automated distribution smoke
test. Current installation documentation includes forbidden non-uv guidance,
and the environment example omits settings required by the graph runtime.

## Decisions

1. AgileForge development, testing, building, installation, and CI are uv-only.
2. Stable `agileforge` and branch-local `agileforge-dev` are separate surfaces.
3. Branch development runs source from the selected checkout through its locked
   uv project environment. A global editable branch installation is not used.
4. A repository-owned launcher creates and owns local runtime profiles. Humans
   and agents do not manually export database variables for routine work.
5. Development profiles persist across restarts. Acceptance and CI profiles can
   be disposable and are always isolated from development and legacy databases.
6. CI invokes repository-owned commands. Workflow YAML does not duplicate
   application setup or routing logic.
7. Docker Compose is not introduced. AgileForge currently has one Python
   process and embedded SQLite databases, so native uv execution is the smaller
   and more transparent boundary.
8. The prior no-migration hard break remains unchanged.
9. The launcher is branch-neutral. It derives identity from the checkout that
   contains it and has no hardcoded branch, worktree, commit, profile, or port.
   Once merged into the default branch, every future branch and linked worktree
   inherits the same development interface.

## Goals

- Run the selected branch's CLI and UI with one explicit command.
- Make the active checkout, commit, graph version, model configuration, and
  database paths machine-readable.
- Give Codex a stable JSON-first execution surface with no shell-session state.
- Prevent accidental use of the legacy database or the wrong linked worktree.
- Make local and CI quality gates use the same uv-locked command.
- Test source execution, CLI packaging, UI packaging, fresh schema bootstrap,
  and API readiness before merge.
- Preserve the exact evidence needed by Operator acceptance.

## Non-Goals

- No database migration or import of legacy AgileForge data.
- No mutation of caRtola, ASA, or MyFinance.
- No execution of the three repository acceptance runs in this task.
- No release publication or production deployment pipeline yet.
- No container platform, service orchestrator, or additional task-runner
  dependency.
- No routing policy in the launcher, CI, API, frontend, or shell bootstrap.
- No secret values in profile manifests, logs, JSON output, or CI artifacts.

## Command Surfaces

### Stable CLI

`agileforge` is the consumer/operator command. After the workflow-graph branch
passes acceptance and is merged, it is installed from an immutable wheel or
versioned Git commit with `uv tool install`. It must not point at a mutable
feature worktree.

The existing user-level shim is not modified automatically by repository code.
After merge, the operator can replace it with the uv-managed stable artifact.
CI must prove that the built artifact installs and exposes `agileforge` before
that replacement is documented as supported.

### Branch-Local Launcher

The repository adds an executable root entrypoint:

```text
./agileforge-dev
```

It resolves its own real checkout root and invokes only:

```text
uv run --locked agileforge-dev ...
```

Before the final exec, the shell owns only checkout and uv isolation policy. It
removes caller-provided `PYTHONHOME`, `PYTHONPATH`, `PYTHONUSERBASE`,
`VIRTUAL_ENV`, `UV_NO_EDITABLE`, `UV_PROJECT`, `UV_PROJECT_ENVIRONMENT`,
`UV_NO_SYNC`, `UV_WORKING_DIR`, `UV_WORKING_DIRECTORY`, `UV_NO_PROJECT`,
`UV_CONFIG_FILE`, `UV_ENV_FILE`, `UV_FROZEN`, `UV_ISOLATED`, `UV_LOCKED`,
`UV_MANAGED_PYTHON`, `UV_NO_CONFIG`, `UV_NO_MANAGED_PYTHON`, and `UV_PYTHON` so
a caller cannot replace `cli.dev_main` or select another project, source root,
working directory, interpreter environment, configuration, or sync/lock policy.
It preserves harmless cache, offline, and certificate controls. The shell has
no application, database, or routing policy.

The installed project script dispatches to a lightweight developer-runtime
module that loads profile configuration before importing the production
application.

From another repository, including a Codex task rooted in a consumer project,
the agent uses the absolute launcher path. It never relies on `PATH`:

```text
/absolute/path/to/agileforge/agileforge-dev cli --profile NAME -- workflow next --project-id ID
```

### Required Commands

```text
./agileforge-dev init --profile NAME [--mode development|acceptance]
./agileforge-dev info --profile NAME [--secrets-file PATH] [--json]
./agileforge-dev cli --profile NAME -- <agileforge arguments>
./agileforge-dev ui --profile NAME [--port auto|PORT] [--reload] [--json]
./agileforge-dev check
./agileforge-dev reset --profile NAME --confirm NAME
```

Acceptance initialization additionally requires the reviewed commit:

```text
./agileforge-dev init --profile NAME --mode acceptance --expect-sha FULL_SHA
```

CI uses an ephemeral profile and non-reloading server mode:

```text
./agileforge-dev init --profile ci --mode acceptance --expect-sha FULL_SHA
./agileforge-dev ui --profile ci --ephemeral --port auto --json --ready-timeout 15
```

## Runtime Profiles

Profiles live under the selected worktree's already-ignored state directory:

```text
.agileforge/dev/profiles/<profile>/
  profile.json
  business.sqlite3
  adk-trace.sqlite3
  artifacts/
  logs/
```

`profile.json` contains non-secret provenance only:

- profile schema version;
- profile name and mode;
- canonical checkout root;
- branch name and initialization commit;
- expected commit for acceptance mode;
- workflow graph version;
- Python and uv versions;
- absolute business and ADK trace database paths;
- model configuration path and SHA-256;
- schema fingerprint;
- creation and last-use timestamps.

The launcher derives and passes these environment variables to child processes:

```text
AGILEFORGE_DB_URL
AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL
MODEL_CONFIG_PATH
```

`profile_environment()` continues to return exactly those three profile-owned
keys. Every launcher-owned schema, CLI, and UI child additionally receives the
fixed non-secret `AGILEFORGE_LAUNCHER_CHILD=1` control. That control makes
`utils.runtime_config` skip implicit checkout `.env` loading, so credentials,
database URLs, model paths, and runtime controls cannot bypass the validated
child environment. Direct stable execution, where the launcher control is not
set, retains its existing dotenv behavior.

The two database paths must be distinct, absolute, contained by the profile
directory, and absent before first initialization. The launcher fails closed on
path escape, symlink substitution, malformed manifest data, profile ownership
mismatch, schema mismatch, or acceptance SHA mismatch.

Development mode permits the worktree commit to advance. Every invocation
records and reports the current commit. A changed schema fingerprint blocks use
until the profile is explicitly reset or a supported future migration exists.

Acceptance mode is immutable. Every initialization, load, and use must match
`expected_sha` and a clean Git worktree. Tracked changes and nonignored
untracked files are refused. Ignored launcher-owned `.agileforge` state does not
make the worktree dirty. Development profiles may run from a dirty worktree.

## Secrets

Profiles never copy or persist provider credentials. Agentic commands receive
credentials from the invoking environment or an optional explicitly selected
secrets file. When a secrets file is used, the launcher reads only documented
allowlisted secret keys and ignores database, checkout, model-path, and runtime
control variables from that file.

Secret presence may be reported as a boolean. Secret values must never be
printed, hashed into evidence, written to manifests, or uploaded by CI.

Non-agentic CLI reads, schema bootstrap, local tests, and UI startup must work
without a provider credential.

`info --json` is the operator preflight. It returns typed configured model roles
and non-secret IDs in `configured_models`, boolean-only credential presence in
`provider_credentials`, and the exact derived non-secret
`child_runtime_environment`. Optional `--secrets-file` uses the same
descriptor-safe allowlist and invoking-environment precedence as `cli` and
`ui`. Credential values are never emitted. The command remains import-lazy.

## Initialization

`init` performs these steps atomically:

1. Resolve and validate the launcher's checkout root.
2. Run `uv lock --check` and fail if `pyproject.toml` and `uv.lock` differ.
3. Validate the profile name and create a new private profile directory.
4. Compute checkout, graph, model-config, and schema provenance.
5. Allocate separate business and ADK trace SQLite paths.
6. Run the current schema bootstrap with the profile environment.
7. Verify both database boundaries and the expected business tables.
8. Write `profile.json` by atomic replace only after successful bootstrap.
9. Emit human-readable output or a stable JSON result.

Re-running `init` is idempotent only when the complete existing profile matches
the requested mode and immutable inputs. It never silently resets data.

## CLI Execution

`cli` validates the profile before every invocation, loads its non-secret
runtime environment, and executes the production `agileforge` parser in a fresh
one-shot process. It preserves stdout for the production JSON response and sends
launcher provenance or diagnostics to stderr unless `--json` requests a single
combined envelope.

The launcher does not inspect workflow state, choose commands, substitute graph
guards, or retry mutations. `WorkflowDomain.position()` remains the only
routing authority.

The JSON envelope contains at least:

```json
{
  "checkout": "...",
  "commit": "...",
  "profile": "...",
  "profile_mode": "development",
  "business_database": "...",
  "trace_database": "...",
  "command": ["workflow", "next", "--project-id", "1"],
  "exit_code": 0,
  "result": {}
}
```

No field may contain a credential.

## UI Execution

`ui` uses the same validated profile and checkout as `cli`. It starts Uvicorn on
`127.0.0.1`, never `0.0.0.0` by default. `--port auto` selects an available
loopback port with bounded retry. The launcher waits for application readiness,
then prints the dashboard URL, checkout commit, and profile.

Human mode remains foreground and supports `--reload`. Agent and CI mode uses a
single non-reloading child process, a readiness timeout, JSON output, and clean
termination. `--ephemeral` creates a child profile and removes only that
launcher-owned state after the process exits.

Each UI launch generates a fresh non-secret nonce. Only that UI child and its
reload supervisor receive it. `/api/dashboard/config` exposes the nonce, and
launcher readiness requires it for reload and non-reload launches in addition
to checkout, commit, databases, and process identity. A foreign server using
the same explicit port, checkout, and profile cannot authenticate a new launch.
Direct installed API smoke remains compatible when no launcher nonce is set.

## Reset Safety

`reset` is the only destructive profile operation. It requires an exact profile
name confirmation, refuses paths outside `.agileforge/dev/profiles`, refuses
symlinked profile roots, and prints the paths it will remove. Acceptance evidence
must be exported before reset. The launcher never removes legacy databases or
files outside its own profile root.

## Quality Gate

`./agileforge-dev check` is the local and CI entrypoint. It runs, in order:

1. `uv lock --check`;
2. the uv-locked `pyrepo-check --all` gate;
3. frontend Node tests;
4. `git diff --check`;
5. a fresh-profile CLI/schema/API smoke test;
6. `uv build`;
7. wheel and source-distribution installation smoke tests in isolated uv
   environments.

`pyrepo-check` must be pinned through uv at an immutable source revision. The
launcher must not invoke the existing global editable executable.

The gate remains offline except for dependency synchronization in a clean
environment. Live provider evaluations remain explicit opt-in integration jobs.

## CI

Add one GitHub Actions workflow triggered by pull requests, pushes to the default
branch, and manual dispatch. It uses read-only repository permissions, cancels
superseded branch runs, pins actions by full commit SHA, and pins the uv version.

Required jobs:

1. Python 3.12 quality and tests on Ubuntu.
2. Python 3.13 quality and tests on Ubuntu.
3. Frontend Node tests.
4. Package build plus isolated CLI and UI artifact smoke test.
5. One macOS smoke job for the supported local operator platform.

Python 3.14 may begin as a non-blocking scheduled compatibility job. It becomes
required only after the complete gate passes and all dependencies formally
support it.

CI uses fresh temporary profile roots and synthetic data. It never accesses the
operator's local repositories, databases, provider credentials, or acceptance
artifacts.

The workflow invokes repository commands rather than restating bootstrap logic
in YAML.

## Packaging And Stable Installation

CI builds both source and wheel distributions with uv. It then installs the
wheel into an isolated uv tool directory and verifies:

- `agileforge --help`;
- parser availability for graph commands;
- packaged model configuration;
- packaged frontend assets;
- fresh schema initialization;
- API readiness and `/api/projects/{project_id}/position` availability;
- absence of the removed `/api/projects/{project_id}/state` route.

Only after this artifact test and Operator acceptance may documentation instruct
the operator to replace the existing hand-written shim with a uv-managed stable
installation. Updating stable AgileForge then means installing or upgrading an
immutable versioned artifact, not repointing a global command at a feature
worktree.

## Documentation Changes

- Replace all installation and execution guidance with uv-only commands.
- Correct `.env.example` for the separate business and ADK trace databases.
- Document stable versus branch-local invocation.
- Make the agent manual use `agileforge-dev cli` during branch acceptance.
- Keep the explicit acceptance evidence protocol, but replace repeated setup
  exports with launcher-produced profile evidence.
- Document how to inspect runtime provenance before any agent mutation.

Documentation tests must reject forbidden installation guidance and examples
that use an ambiguous bare `agileforge` during worktree acceptance.

## Testing Strategy

### Launcher Unit Tests

- profile-name validation and path containment;
- distinct database enforcement;
- manifest atomicity and schema validation;
- development versus acceptance commit behavior;
- model-config and schema fingerprint drift;
- clean acceptance initialization, load, and use with tracked and nonignored
  untracked source changes;
- secret allowlisting and redaction;
- launcher-child dotenv isolation and complete redacted info preflight;
- reset refusal outside owned state;
- deterministic JSON envelopes.

### Launcher Integration Tests

- initialize a fresh profile;
- run `project list` through `cli`;
- start the UI on an automatically selected port;
- wait for readiness and query current API routes;
- reject same-coordinate UI readiness without the per-launch nonce;
- terminate cleanly without leaked child processes;
- compose the production `ProcessGroup` adapter with `LocalRuntime.stop_ui` and
  `dev_server.stop_ui` around a bounded Unix-local TERM-immune process, proving
  timeout, KILL, final reap, and no survivor;
- preserve a development profile across restart;
- remove only an ephemeral profile;
- reject a stale acceptance SHA;
- prove no dependency on the user-level `agileforge` shim.

Initial-spec read tests corrupt persisted draft content and its fingerprint
directly. Both cases must return typed `INITIAL_SPEC_DRAFT_INVALID` failures
without overstating production evidence.

### Cross-Worktree Isolation Test

Create two linked worktrees that both contain the launcher, initialize the same
profile name in each, and prove that they report different canonical checkout
roots, commits, profile roots, business databases, ADK trace databases, and
automatically selected UI ports. A CLI command launched from each worktree must
execute that worktree's source. Neither invocation may read or modify the other
worktree's profile state.

An older branch that predates the launcher must merge or rebase the launcher
change before using this interface. A launcher from another checkout must not be
used to impersonate or execute that older branch.

### CI Contract Tests

- action and uv versions are pinned;
- required triggers and read-only permissions exist;
- the supported Python matrix is exact;
- CI invokes the repository quality command;
- provider network tests are excluded;
- artifact smoke tests run outside the source checkout.

### Hard-Break Retention

Existing graph, absence, routing, typing, security, and full repository tests
remain required. No typing suppression is permitted to make the new launcher or
CI pass.

## Acceptance Boundary

Completing this work changes the reviewed AgileForge commit, so Task 18's
acceptance package must be regenerated and independently reviewed at the new
HEAD. caRtola, ASA, and MyFinance remain `not_run` until the operator executes
that revised package.

Task 19 begins only after complete acceptance evidence or one concrete failure
from those Operator-run tests is returned.

## Sources

- uv project environments and lock behavior:
  <https://docs.astral.sh/uv/concepts/projects/sync/>
- uv tool execution and installation:
  <https://docs.astral.sh/uv/guides/tools/>
- uv GitHub Actions integration:
  <https://docs.astral.sh/uv/guides/integration/github/>
- Git worktrees:
  <https://git-scm.com/docs/git-worktree.html>
- GitHub Actions pull-request behavior:
  <https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows>
- Uvicorn development and production settings:
  <https://www.uvicorn.org/settings/>
