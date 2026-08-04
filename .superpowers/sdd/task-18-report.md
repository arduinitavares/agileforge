# Task 18 Report: UV-Only Runtime Final Remediation

## Verdict

PASS for the complete local implementation package and second final-review fix
wave. Fresh independent review is still required. Operator acceptance has not
run: caRtola, ASA, and MyFinance remain `not_run`, and Task 19 has not started.
No GitHub-hosted CI run is claimed.

## Immutable Coordinates

Package base:
`cb3e32c4144866e81bf367f073984905abce77e9`

NEW_IMPLEMENTATION_HEAD:
`08810bc0cc48df5510a6331c0f9f7816563d1520`

Exact endpoint range:
`cb3e32c4144866e81bf367f073984905abce77e9..08810bc0cc48df5510a6331c0f9f7816563d1520`

Immutable binary package:
`.superpowers/sdd/review-cb3e32c..08810bc.diff`

Package size:
`459382` bytes

Package SHA-256:
`d0a13a084506a760f426233825ac74e4bf3d03cba58c2fa6699f67b2476ec1f7`

The package was generated with `git diff --binary` from the exact endpoint
range above. Hashing a fresh diff stream produced the same SHA-256. The patch
has no file-delta header for `.superpowers/sdd/task-18-report.md` or
`.superpowers/sdd/task-18-review.md`.

The current report evidence commit and any current review evidence commit are
outside the implementation package endpoint. The final reviewer must read the
report and review artifacts separately. This avoids a self-referential report
hash.

## Package Boundary Commits

- `d97fcc7`: source-provenance, secrets-forwarding, clean-distribution fixes,
  tests, and operating-document updates.
- `ffc8eaa`: normal revert of evidence-only commit `33ba229`; no reset, rebase,
  force update, or published-history rewrite was used.
- `08810bc`: removes the older tracked Task 18 report from the endpoint because
  the package base contains no Task 18 report. The new report is evidence-only.

Previous implementation remediation remains present through
`fc6cc2089d14d865a39011d110623ccc0bc8d44e`. All previously fixed dotenv,
acceptance cleanliness, operator preflight, process-group escalation,
typed-failure, and UI launch-ownership invariants remain covered by the full
gate.

## Second-Wave Fixes

### Launcher source provenance

The root `agileforge-dev` now removes caller-controlled `PYTHONHOME`,
`PYTHONPATH`, `PYTHONUSERBASE`, `VIRTUAL_ENV`, and `UV_NO_EDITABLE` before its
fixed checkout-local `uv run --locked` exec. Existing hostile uv project,
workdir, environment, interpreter, configuration, and sync controls remain
sanitized.

Useful cache, offline, native-TLS, and certificate settings remain preserved.
The shell still owns only checkout/source and uv isolation policy; it contains
no application, database, profile, port, or routing policy.

The real regression installs a hostile `cli.dev_main` shadow package through
`PYTHONPATH` while setting `UV_NO_EDITABLE=1`. Before the fix, that module ran
and returned exit 71. After the fix, the checkout's real developer CLI supplied
help, the hostile output was absent, and its import marker was never created.

### Operator credential forwarding

Every provider-backed acceptance `info` and `cli` prefix now passes:

```text
--secrets-file "$AGILEFORGE_SECRETS_FILE"
```

This includes Project Shell creation, workflow reads, initial-spec and authority
reads, restart reads, and the fixed prefix used for every graph-authored
execution. Parser tests expand the variable to a non-secret fixture path and
parse both launcher and forwarded production argv.

The README branch-local quick start no longer instructs launcher users to rely
on checkout `.env`. It selects an operator-owned regular secrets file outside
the checkout, runs the redacted `info --json` preflight, and forwards the same
file to provider-backed CLI commands. No credential value appears in the docs,
argv evidence, package report, or test output. Stable direct execution retains
its existing implicit dotenv behavior.

### Distribution source isolation

`scripts/verify_distribution.py` now creates a fresh temporary source root from
Git-tracked paths and copies their current working-tree bytes. Deleted tracked
files remain absent; modified and newly tracked files retain current bytes.
Ignored and other untracked `build/` and `*.egg-info` state is never copied.
Both wheel and sdist are built from that snapshot, outside the checkout.

The regression creates ignored stale build and egg-info content plus a tracked
working-tree marker. A local adversarial uv builder proves the stale content
would enter artifacts when run against the live checkout. The same production
build orchestration runs it against the clean snapshot and proves both artifacts
exclude the stale payload, retain the tracked marker, and leave checkout bytes
and exact Git status unchanged. The full gate separately builds and installs
real uv/setuptools wheel and sdist artifacts.

## TDD Evidence

Focused RED command covered the seven new launcher, checklist/docs, and
distribution contracts. Result before implementation: `7 failed`.

Focused GREEN result after implementation: `7 passed`.

Expanded launcher and operating-document suite:
`34 passed`.

Expanded real distribution suite:
`11 passed`.

Touched-file Ruff format, Ruff lint, and ty checks all passed without typing
suppressions.

## Full Local Gate

The tracked worktree was clean at
`08810bc0cc48df5510a6331c0f9f7816563d1520` before running:

```text
./agileforge-dev check
```

Results:

- Locked uv resolution: `147` packages resolved.
- Ruff repository lint: PASS.
- Ruff annotation-only gate: PASS.
- ty: PASS.
- Bandit: PASS, `0` issues at every severity and confidence level across
  `189858` scanned lines.
- Python pytest: `1969 passed`, `2 skipped`, `2 deselected`, `17 warnings` in
  `204.06s`.
- Node frontend tests: `9 passed`, `0 failed`, `0 skipped`.
- Wheel verification: `agileforge-0.1.0-py3-none-any.whl` verified after an
  isolated uv tool install and installed API/schema/resource smoke.
- Source-distribution verification: `agileforge-0.1.0.tar.gz` verified after an
  isolated uv tool install and installed API/schema/resource smoke.

`git diff --check` passed. The tracked worktree remained clean after the gate
and before package generation.

## Boundaries

No command accessed caRtola, ASA, or MyFinance. No provider-backed workflow or
external acceptance command ran. No external repository was cloned, read, or
mutated. No provider credential was used. Task 19 was not started.

External acceptance status:

- caRtola: `not_run`
- ASA: `not_run`
- MyFinance: `not_run`
- Task 19: `not_started`

The full gate was local only. GitHub-hosted CI was not run and is not claimed.
