# Task 18 Report: UV-Only Runtime Final Remediation

## Verdict

PASS for implementation self-review and the complete repository gate. All five
Important and three Minor final-review findings are remediated in the immutable
implementation package below. Fresh independent review is still required.

External acceptance has not run:

- caRtola: `not_run`
- ASA: `not_run`
- MyFinance: `not_run`
- Task 19: not started

## Durable Coordinates

Original Task 18 base:
`cb3e32c4144866e81bf367f073984905abce77e9`

Final non-evidence commit (`IMPLEMENTATION_HEAD`):
`fc6cc2089d14d865a39011d110623ccc0bc8d44e`

Implementation commit:
`fc6cc2089d14d865a39011d110623ccc0bc8d44e` -
`fix: harden uv-only developer runtime`

Complete implementation range:
`cb3e32c4144866e81bf367f073984905abce77e9..fc6cc2089d14d865a39011d110623ccc0bc8d44e`

Immutable implementation package:
`.superpowers/sdd/review-cb3e32c..fc6cc20.diff`

Package size:
`454239` bytes

Package SHA-256:
`603643a9d34a439e10671ccd5f68045a350fc6e61a446c902bb7500a2b443e25`

The package was generated only after the full gate passed at the clean
`IMPLEMENTATION_HEAD`. Regenerating the exact binary diff produced the same
SHA-256. This report commit and any later review evidence commits are outside
the implementation package. The report is read separately; no self-referential
report commit hash is required.

## Scope And Safety

Implementation started from
`1c5e7920b0163356b22617edcd43c83a311652ce` in the requested worktree. The
protected `.superpowers/sdd/task-18-review.md` was not edited. No typing
suppression was added. Python execution and verification used uv only.

No command inspected deeply, edited, branched, created a worktree in, or
otherwise mutated caRtola, ASA, MyFinance, or another external repository. No
external acceptance command or provider-backed workflow ran. Tests used only
the AgileForge checkout, temporary Git repositories, temporary SQLite files,
and Unix-local processes/loopback endpoints.

## Remediation Matrix

### 1. Launcher-Child Dotenv Isolation

`utils.runtime_controls` defines the fixed non-secret
`AGILEFORGE_LAUNCHER_CHILD=1` control. Schema bootstrap, production CLI, and UI
children receive it in addition to the unchanged exact three-key
`profile_environment()` result. `utils.runtime_config` skips implicit dotenv
loading only for that marked child. Direct stable execution retains the prior
dotenv behavior.

Unit and real-subprocess regressions place provider credentials, business/trace
database controls, model controls, and another runtime flag in checkout `.env`.
Launcher children use only validated profile values, and every poison value is
absent from stdout and stderr.

### 2. Hostile uv Controls

The root bootstrap removes caller controls that can redirect or bypass project,
workdir, config, interpreter environment, sync, or lock selection before its
fixed `uv --directory "$ROOT" run --locked` exec. The list includes
`UV_PROJECT`, `UV_PROJECT_ENVIRONMENT`, `UV_NO_SYNC`, `UV_WORKING_DIR`,
`UV_WORKING_DIRECTORY`, `UV_NO_PROJECT`, `UV_CONFIG_FILE`, `UV_ENV_FILE`,
`UV_FROZEN`, `UV_ISOLATED`, `UV_LOCKED`, `UV_MANAGED_PYTHON`,
`UV_NO_CONFIG`, `UV_NO_MANAGED_PYTHON`, and `UV_PYTHON`.

The bootstrap preserves cache, offline, native-certificate, and certificate
file controls. A real hostile-project/hostile-environment launcher regression
proves the requested checkout command and lock environment execute.

### 3. Exact Acceptance Source

Acceptance profile preparation, finalization, load, timestamp touch, and runtime
environment use require a clean Git worktree. Tracked changes and nonignored
untracked files are refused with one generic path-free error. Ignored
launcher-owned `.agileforge` state remains allowed. Development profiles retain
dirty-worktree behavior.

Regressions cover dirty launcher, CLI, lock, service, and untracked source.
Refusal occurs before new profile state and preserves every byte of an existing
profile.

### 4. Operator Preflight

`info --json` now emits fixed typed `configured_models`,
`provider_credentials`, and `child_runtime_environment` contracts. Model roles
and IDs are non-secret. Provider state is boolean-only. The child environment
is the exact four-key launcher-child shape and rejects extra fields.

Optional `--secrets-file` reuses the existing descriptor-safe regular-file,
no-follow, allowlist, and invoking-environment precedence path. Neither JSON nor
human output emits credential values. Production application/database imports
remain lazy. README, agent manual, design, plan, and Operator checklist document
one-command preflight.

### 5. Durable Evidence Coordinates

All code, tests, design, plan, and operating docs were committed first. The full
gate ran at that immutable clean commit. The package is exactly
`cb3e32c..IMPLEMENTATION_HEAD`; this evidence-only report commit is excluded.

### 6. Real Process-Group Escalation

A bounded Unix-local test launches a real new-session process group whose
process ignores TERM. It composes production `ProcessGroup` through
`LocalRuntime.stop_ui` into `dev_server.stop_ui`, proves the finite timeout,
KILL escalation, final reap, absent process group, and absent process.

### 7. Initial-Spec Typed Failures

Direct production tests corrupt active draft canonical content and content
fingerprint independently. Both return typed `INITIAL_SPEC_DRAFT_INVALID` with
project and draft coordinates. The earlier report claim is now directly
supported.

### 8. UI Launch Ownership

Every UI launch attempt receives a fresh non-secret nonce. It is added only to
the UI child/supervisor environment, exposed through dashboard config, and
required by `ExpectedUIRuntime` for reload and non-reload readiness. Direct
installed API smoke remains compatible with no nonce. An explicit-port,
same-checkout/profile foreign-server regression proves a stale server cannot
authenticate a new reload launch.

## TDD Evidence

The first behavior-focused RED run produced 15 intended failures and 82 passes
across runtime config, profiles, developer launcher, and CLI forwarding. The
failures were the missing child control, dirty acceptance refusal, fixed info
fields/secrets option, hostile uv sanitization, and real child isolation.

A second focused RED run failed at the new launch-nonce, dashboard-config,
operator-doc, and checklist assertions. No production behavior was changed for
the process-group escalation or malformed initial-spec findings: both behaviors
already existed, while direct evidence did not. Their RED was the confirmed
absence of a production-composition test and direct corruption tests. New tests
were then added and run against the actual adapters.

Final focused GREEN was split only to respect acceptance clean-source behavior:

```text
affected runtime/docs suite:
161 passed, 2 deselected, 4 warnings

clean-checkout real launcher lifecycle:
2 passed, 4 warnings

targeted hostile uv/dotenv/clean-use/process composition:
5 passed
```

Ruff, Ruff format, `ty`, POSIX shell syntax, and `git diff --check` passed before
the implementation commit.

## Full Verification

Command run at `IMPLEMENTATION_HEAD`:

```text
./agileforge-dev check
```

Results:

```text
Python: 3.13.12
Ruff: pass
Ruff annotation checks: pass
ty: pass
Bandit: 0 issues across 129690 lines of code
pytest: 1966 passed, 2 skipped, 2 deselected, 17 warnings in 198.19s
Node: 9 passed, 0 failed
wheel: verified agileforge-0.1.0-py3-none-any.whl
sdist: verified agileforge-0.1.0.tar.gz
git diff --check: clean
tracked worktree after implementation commit: clean
```

Warnings were existing dependency deprecation/experimental notices and the
guarded network-test warning. They caused no failures.

## Stop Boundary

This package is ready for fresh independent review. Checklist preparation and
self-review are not external acceptance. caRtola, ASA, and MyFinance remain
`not_run`, and Task 19 has not started.
