# Issue 263 container validation

This records the implementation and synthetic verification. It does not approve
live-profile inventory, data transfer, provider use, deployment, or publication.
Issue #263 remains open until native validation, approved cutover, native removal,
and final acceptance are complete.

## Revision and environment

- Branch: `dev/issue-263-linux-containers`.
- Original baseline: `7e193694c1da0746f257e3b48ce63b326e709cca`.
- Reviewed implementation: `a76a74f63d220d548ad90e10055e19d87b590b96`.
- Final acceptance candidate: `3edd7bb051ddd5468af65239f5da657725e33757`.
  This adds the reviewed browser-readiness test correction and documentation;
  application code is unchanged from the reviewed implementation.
- Docker 29.8.0 on a Linux ARM64 VM: 24 visible CPUs, 8,316,473,344 bytes RAM.
  These `linux/amd64` runs use emulation; they are not native amd64 benchmarks.
- Runtime UID/GID: `10001:10001`; Python `3.13.15`; uv `0.12.8`; Node `24.21.0`.
- Controller: `pyrepo-check` commit `ac41bbe8e8588b8f4232979c47d26be37d78d413`.
- Exact OS, binary, and image pins: [containers/pins.json](../../containers/pins.json).

The production image is
`sha256:ba206f442639e1f68443b1ac7794d17a634764fd32926af33fddd8467c029502`.
Its source archive hash is
`1a6d30274f83f6412a3c11e62708ec512eab4a4f798b079772a0a43fb55836e2`;
lock hash is `79499c43279fdc745431e428edfeab6d3a17c15a466f9585d35e66dafe12c987`.
The test toolchain image is
`sha256:4b8631ebbff399fac468b752d8622ee00a499d5f7f227f1cfe8a13763d917d81`.
The application checkout is imported separately by a clean committed Git bundle.

Docker's recorded build elapsed times were 2m17s for the development image,
4m54s for the final test toolchain, and 38.6s for the final production image.
Those builds reused 2, 5, and 6 cached steps respectively; they are not cold-build
benchmarks and are separate from test and service startup timings.

## Verification

The final canonical `./agileforge-dev check --json` passed at
`3edd7bb051ddd5468af65239f5da657725e33757` in a fresh Linux checkout, which
remained clean after the run. Total elapsed time was 57m25.6s.

| Stage | Result | Elapsed |
| --- | --- | --- |
| Locked dependency check | Passed | 0.33s |
| Pinned Python quality and tests | Passed | 56m54.9s |
| Seven Node test suites | Passed | 1.45s |
| Whitespace | Passed | 0.03s |
| Installed wheel and sdist verification | Passed | 28.87s |

Python results: 3,408 passed, including all 58 browser cases; 87 skipped and one
integration case deselected under the existing selection. There were no failed
reports, collection errors, or early termination. Node results: 248 passed.
Ruff, annotations, Ty, and Bandit passed. Coverage.py measured 82.66% combined
line/branch coverage against the unchanged 80% threshold (85.77% statements and
71.52% branches). Detailed test and coverage JSON were retained before cleanup.

The first canonical gate ran at the reviewed revision in a fresh named Linux
workspace for 60m40s. It recorded 3,406 passing tests, two failing browser cases,
87 skipped tests, and one deselected integration case. Ruff, annotations, Ty,
and Bandit passed. The gate stopped before frontend and distribution stages;
all seven Node suites passed separately (248 tests).

The missing JavaScript renderer failure reproduced in isolation. An immediate
browser probe showed that `networkidle` returned with the document still parsing
and the renderer undefined, although both local scripts returned HTTP 200.
The test now waits for the exact function it invokes, preserving its assertions
and timeouts. Independent review approved that correction. The other failure,
a second-tab navigation timeout, did not reproduce in focused runs; its cause
is unconfirmed. All 58 browser tests passed after the correction in 187.21s;
focused pinned quality checks and separate distribution verification passed.
Both previously failing cases also passed in the final canonical run. The
original failed evidence is retained alongside the successful run.

The installed-production rehearsal passed at the final acceptance candidate:

- Missing profiles fail before initialization; explicit initialization succeeds.
- Product CLI runs with provider access disabled. A synthetic credential loads
  from a private tmpfs file with UID 10001 and mode 0600 and stays out of output.
- The running API prevents backup through the lifetime maintenance fence.
- A stopped-service backup restores into a new profile, preserving the durable
  state ID, exact artifact bytes, and absence of an unused trace database.
- The second service actually starts the restored profile and reports its
  restored database paths. A new container has a fresh launch nonce.
- Both services shut down cleanly and their loopback endpoints disappear.
- Observed startup: 6.799 and 6.926 seconds; shutdown: 0.860 and 0.822 seconds.
  These measurements include local Docker overhead and amd64 emulation.

Production imports resolve inside the installed wheel's `site-packages`, with
no `/build/api.py` or checkout Git directory. The fixed build manifest is owned
by root and mode 0444. A read-only inspection of all six production image layers
covered 23,886 regular files and found no captured checkout/state/credential
paths or synthetic source-export canary. This is a bounded build-context check,
not a claim to detect every possible embedded secret.

The fresh development workflow passed at implementation revision `ec14574a`:
bundle import, fenced stdin edit and diff, linked-worktree creation, independent
same-name profiles, stable Git/profile paths after recreation, refusal while a
writable consumer exists, and one-shot backup/verify/restore after stopping it.
The task's development container and volumes were removed after retaining logs.
The final correction commit does not change that controller workflow.

Synthetic transfer fixtures cover current business schemas and a real local ADK
trace store, accepted Specification bytes, historical bindings, active/completed
Sprint history, audit/idempotency records, unlinked sessions, dirty main/linked
worktrees, safe remotes, and exact allowed database deltas after guarded attach.
No historical SQL rewriting or accepted-artifact regeneration occurs.

The independent review requested five fixes: exact development attachment paths,
per-Project production attachment completion, restored-profile API startup,
serialized credential redaction, and backup/source overlap rejection. Regression
tests reproduced the defects; the corrected delta received an independent
approval with no remaining P0-P2 findings. Focused correction checks passed
Ruff, annotations, Ty, Bandit, 58 runtime/transfer cases, and five credential cases.

## Baseline and limits

[Baseline CI run 35089329884](https://github.com/arduinitavares/agileforge/actions/runs/35089329884)
passed at `7e193694`, before this branch. Its Linux full-gate job took 61m09s;
Windows full-gate job took 119m29s; macOS launcher smoke took 27s. These are job
elapsed times, not isolated test durations or same-revision container comparisons.
At the initial handoff, the candidate had not been pushed and its new native
amd64 container CI job had not run. Subsequent publication is recorded in
[PR #273](https://github.com/arduinitavares/agileforge/pull/273).
No speedup or runner-cost reduction is established.

Some early delegated focused checks ran on the host. Those are supplementary,
not the acceptance gate. The final gate and both workflow rehearsals run in
Linux. All test state is synthetic; no protected profile, database, `.env`, or
credential was inventoried or copied, and no provider was called.

Full local logs and reports are retained under
`.superpowers/sdd/2026-09-16-issue-263-linux-containers/` in the task worktree.
The initial handoff at `869dbbbe88fcac93239c85785568f02940a83bca` updated only
documentation after the tested candidate. The user subsequently approved a
local fast-forward of `master` and publication of the feature branch as PR #273.
The later Compose startup changes are validated separately below.
The [operator runbook](../linux-containers.md) states the separate inventory,
copy/cutover, and final-switch approvals and the rollback boundary.

## Post-cutover removal checklist

The maintainer approved retiring the macOS smoke plus Windows evidence,
UI-runtime, and full-gate CI jobs separately from live cutover. The launcher
smoke now runs through the host transport inside the Linux container gate.
Shared security, profile, secret, and lifecycle tests remain in the full suite.

After approved live cutover, review
`tests/windows/`, Windows branches in the evidence/source adapters and secret
loader, and interpreter/process ownership branches in the development launcher.
Keep the POSIX evidence/security guarantees, profile/secret/lifecycle regressions,
the isolated distribution verifier, and the unchanged coverage threshold.
Only then add early unsupported-platform rejection and remove native setup docs.

## Compose startup follow-up

The user requested ordinary Docker startup for the packaged app. Against the
published `869dbbb` configuration, a fresh `docker compose run --rm production
init --profile default` failed because the external production-state volume did
not exist. The corrected configuration makes production the default service,
puts development behind an opt-in profile, and lets Compose create named volumes.
Standard `-p` project selection controls volume names and keeps installations
separate. The image already contains Tini, so the redundant Compose init wrapper
was removed.

The real `scripts/verify_compose.py` rehearsal passed against the production
image recorded above (application revision `3edd7bb`) on Docker Compose 5.5.1:

- Missing state remained an error until explicit initialization.
- Fresh volumes and the default profile were created through Compose alone.
- Plain `up -d` started only the packaged production service.
- Init was refused with a running service and with an existing stopped profile.
- Shutdown was graceful and the endpoint disappeared.
- `down` followed by `up -d` retained the state ID and exact synthetic artifact
  bytes, while the launch nonce changed.
- The final rehearsal left no containers or volumes in its disposable namespace.

An altered configuration with an unexpected volume name was rejected before
runtime mutation. The development controller also started only the requested
development service and let Compose create correctly labeled volumes.
Ten focused container-build and transport tests passed in Linux. Ruff,
formatting, annotations, Ty, and Bandit passed for the changed Python paths.
An independent review approved this startup delta with no findings.

All state was synthetic and no provider was called. No live data was migrated.
Logs are retained as `compose-startup-red.log`, `compose-startup-green.log`,
`compose-rehearsal-final.log`, `compose-volume-refusal.log`, and
`compose-focused-tests.log` under the local evidence directory above. CI now
runs the Compose rehearsal after building and checking the production image.
The full canonical gate was not repeated locally for this packaging-only change.
The separate existing PR review findings and macOS CI failure remain unresolved;
this startup verification does not establish complete PR or cutover acceptance.

## Native CI retirement follow-up

[Master CI run 35119800233](https://github.com/arduinitavares/agileforge/actions/runs/35119800233)
tested `e912ddb4443aa2545b49d89c9b5441bfb4ec3900`. The Linux container canonical
gate, installed-image rehearsal, and Compose startup rehearsal passed. The
native Linux full gate, frontend suites, and all three Windows jobs also passed.
Only the macOS launcher smoke failed, reporting `cleanup failed` during shutdown.
The log did not identify the failing cleanup predicate.

The maintainer then approved retiring the four native Windows/macOS jobs and
moving the unchanged launcher lifecycle checks into the Linux container gate.
The new step runs before the full gate so a lifecycle regression fails early.
The native Linux and Node jobs remain. No test assertions, coverage threshold,
Windows adapters, or platform-specific test files were removed.

The real launcher smoke passed through the host transport at the baseline
revision above, using the existing Linux/amd64 test image and fresh disposable
workspace/cache volumes. All 13 launcher smoke regression tests passed in
24.04 seconds. YAML validation confirmed that the four native jobs were removed,
the smoke step was inserted before the container full gate, and all retained
steps and workflow triggers were unchanged. These local checks used amd64
emulation on the ARM64 host; native execution of the new step remains a CI check.
Logs are retained as `native-ci-launcher-smoke.log` and
`native-ci-smoke-tests.log` under the local evidence directory above.

This supersedes the original design's sequencing for CI-job retirement only.
It does not resolve the native macOS defect, authorize live-data migration, or
establish complete acceptance of #263.

## CI contract correction

[Run 35132294349](https://github.com/arduinitavares/agileforge/actions/runs/35132294349)
at `e66869f` passed the Linux launcher smoke, but both full gates failed because
eight structural tests in `tests/test_ci_contract.py` still required the retired
jobs and their artifact action. The container run reported 3,400 other Python
tests passing. The original focused validation omitted this contract suite.

The correction updates the contract to the three retained jobs, preserves action
and runtime pins, and checks the container source import, exact launcher command,
canonical gate, installed-image rehearsal, and Compose rehearsal in order.
The obsolete expectations reproduced as eight failures in an isolated Linux
container. The corrected suite passed all 14 tests with the repository-pinned
pytest and PyYAML versions; seven deliberately broken workflow variants were
also rejected. Ruff and formatting passed, and independent review found no
issues. Full CI must pass on the correction before reporting CI as green.
