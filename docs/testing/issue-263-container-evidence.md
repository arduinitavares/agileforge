# Issue 263 container validation

This records the implementation and synthetic verification. It does not approve
live-profile inventory, data transfer, provider use, deployment, or publication.
Issue #263 remains open until native validation, approved cutover, native removal,
and final acceptance are complete.

## Revision and environment

- Branch: `dev/issue-263-linux-containers`.
- Original baseline: `7e193694c1da0746f257e3b48ce63b326e709cca`.
- Reviewed application/production revision: `a76a74f63d220d548ad90e10055e19d87b590b96`.
  A subsequent test-only correction waits for the renderer used by the browser
  test; the final gate revision and result remain to be recorded.
- Docker 29.8.0 on a Linux ARM64 VM: 24 visible CPUs, 8,316,473,344 bytes RAM.
  These `linux/amd64` runs use emulation; they are not native amd64 benchmarks.
- Runtime UID/GID: `10001:10001`; Python `3.13.15`; uv `0.12.8`; Node `24.21.0`.
- Controller: `pyrepo-check` commit `ac41bbe8e8588b8f4232979c47d26be37d78d413`.
- Exact OS, binary, and image pins: [containers/pins.json](../../containers/pins.json).

The production image is
`sha256:d5daca169918f6ad0671f763fe70cdc558a05046b10ff6725a8908cb6ed3c076`.
Its source archive hash is
`60d14667640434463c964ef07de6672d10e28dc118c4cc96f9e5d28e413e1cab`;
lock hash is `79499c43279fdc745431e428edfeab6d3a17c15a466f9585d35e66dafe12c987`.
The test toolchain image is
`sha256:4b8631ebbff399fac468b752d8622ee00a499d5f7f227f1cfe8a13763d917d81`.
The application checkout is imported separately by a clean committed Git bundle.

Docker's recorded build elapsed times were 2m17s for the development image,
4m54s for the final test toolchain, and 28.7s for the reviewed production image.
Those builds reused 2, 5, and 7 cached steps respectively; they are not cold-build
benchmarks and are separate from test and service startup timings.

## Verification

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
is unconfirmed. A fresh browser suite and canonical gate are required before
this validation is complete. The original failed evidence is retained.

The installed-production rehearsal passed at the reviewed revision:

- Missing profiles fail before initialization; explicit initialization succeeds.
- Product CLI runs with provider access disabled. A synthetic credential loads
  from a private tmpfs file with UID 10001 and mode 0600 and stays out of output.
- The running API prevents backup through the lifetime maintenance fence.
- A stopped-service backup restores into a new profile, preserving the durable
  state ID, exact artifact bytes, and absence of an unused trace database.
- The second service actually starts the restored profile and reports its
  restored database paths. A new container has a fresh launch nonce.
- Both services shut down cleanly and their loopback endpoints disappear.
- Observed startup: 6.799 and 6.781 seconds; shutdown: 0.846 and 0.785 seconds.
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
The candidate has not been pushed, so its new native amd64 container CI job has
not run. No speedup or runner-cost reduction is established.

Some early delegated focused checks ran on the host. Those are supplementary,
not the acceptance gate. The final gate and both workflow rehearsals run in
Linux. All test state is synthetic; no protected profile, database, `.env`, or
credential was inventoried or copied, and no provider was called.

Full local logs and reports are retained under
`.superpowers/sdd/2026-09-16-issue-263-linux-containers/` in the task worktree.
The [operator runbook](../linux-containers.md) states the separate inventory,
copy/cutover, and final-switch approvals and the rollback boundary.

## Post-cutover removal checklist

After approved live cutover and native amd64 acceptance, inventory and retire the
macOS smoke plus Windows evidence, UI-runtime, and full-gate CI jobs. Review
`tests/windows/`, Windows branches in the evidence/source adapters and secret
loader, and interpreter/process ownership branches in the development launcher.
Keep the POSIX evidence/security guarantees, profile/secret/lifecycle regressions,
the isolated distribution verifier, and the unchanged coverage threshold. Move
the launcher smoke to the container workflow before removing its native jobs.
Only then add early unsupported-platform rejection and remove native setup docs.
