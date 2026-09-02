# Windows Specification Source Implementation Plan

> **For agentic workers:** Use the installed Superpowers debugging, TDD, and verification skills. Execute inline in the assigned worktree; request an independent security review of the completed patch.

**Goal:** Fix independently reproduced issue #238 without changing source provenance or Specification semantics.

**Architecture:** Retain the POSIX traversal and add a strict Windows adapter over the existing native handle primitives and retained worktree anchor. Share byte validation and bundle construction in Specification registration, without inheriting Vision's optional-warning or internal-reparse policy. Project unsupported capability through the existing application, API, CLI, and UI availability surfaces.

**Tech Stack:** Native Windows Python 3.13.15, uv, NtCreateFile, pytest, SQLite test fixtures.

**Spec:** https://github.com/arduinitavares/agileforge/issues/238

## Constraints and audit

- Work only in the assigned worktree. No commits, pushes, PRs, deployment, live registration, providers, or backend edits.
- Retain exact bytes, strict UTF-8, document and bundle limits, physical duplicate rejection, provenance matching, double capture, and persistence revalidation.
- Reject every selected reparse, alternate stream/device path, and filename spelling alias. Retain root, directory, and leaf identities and revalidate after reads.
- Native Windows audit on base `41d8a53f` reproduced `UNSAFE_FILE` in a clean temporary Git repository containing source, CONTEXT.md, and an ADR. UI and CLI both call `AgileForgeApplication.register_specification_source`. Dirty is a warning; stale provenance is a separate earlier guard. Issue #237 is excluded.
- The shell launcher requires in-memory CRLF normalization under Git Bash. `info --profile issue-238-audit --json` reports that this new worktree has no such runtime profile. Tests use isolated fixtures and no app profile.

## Task 1: Strict Windows capture

Files: `services/specification_source_windows.py`, `services/specification_source_registration.py`, `tests/windows/test_specification_source_windows.py`.

- [x] Run the new exact-byte capture regression and record its Windows capability failure before implementation.
- [x] Add `open_windows_source_worktree(worktree)` and a retained worktree `capture(relative_path, byte_limit)` returning bytes plus volume/file identity or explicit absence. Use existing native root validation and component opens, reject reparses and unsafe spellings, and revalidate the full retained chain after reading.
- [x] Dispatch Windows to that adapter. Keep POSIX traversal and provenance/write checks intact. Both adapters feed the same strict byte-to-document conversion and bundle checks. Missing POSIX flags now receive the distinct unsupported-capability code.
- [x] Run focused exact-capture regressions, first failing and then passing.

## Task 2: Registration and safety verification

Files: the Windows regression file, `.github/workflows/ci.yml`, `services/application.py`, `api.py`, `cli/workflow_commands.py`, `frontend/project.js`, `tests/test_vision_interview_ui.mjs`, and this handoff.

- [x] Add real Windows missing/unsafe file, case/short-name/stream alias, duplicate hardlink, size, decoding, root/component/file replacement, and content mutation cases.
- [x] Exercise the real shared application with isolated SQLite databases: stale binding fails; explicit refresh of intentional dirty documentation allows registration; capture/write-time changes reject persistence. Transactional tests use temporary database files so nested reads have separate connections.
- [x] Extend the Windows CI job to cover Specification registration and its portable service suite.
- [x] Attempt all repository gates and run direct locked checks where the controller cannot run. Record limitations below.
- [x] Request independent Sol/xhigh review and fix the confirmed capability-projection gap. The reviewer found no residual issues and independently reran the two unsupported-filesystem regressions successfully.
- [x] Leave an uncommitted working-tree patch, with the running checkout unchanged.

## Audit verdict and changes

Confirmed defect: a clean, isolated Git fixture with source, root Context, and an ADR fails on native Windows Python 3.13.15 at `_capture_selected_documents -> _open_root -> _required_open_flags`. The fixture reported `os.name=nt`, no `O_NOFOLLOW` or `O_DIRECTORY`, and `UNSAFE_FILE: This platform cannot safely capture repository source files.` No production database or provider was involved.

The source reader now opens each component relative to retained native directory handles. It rejects reparses, wrong file types, traversal, reserved Windows paths, streams, and case/8.3 aliases. It compares retained and reopened path chains and native identities after reads, with root verification on exit. Exact bytes, SHA-256, UTF-8 validation, limits, duplicate detection, double capture, and write-time revalidation remain enforced.

The API endpoint and CLI registration handler already share `AgileForgeApplication.register_specification_source`. Both action projections now use its source-capability check. Unsupported capture has a distinct error code; the UI withholds the form and submission binding for locked actions. The Dirty warning remains informational, and registration still requires explicit refresh after provenance changes. No #237 recovery flow or input-retention redesign was added.

Changed production files: `services/specification_source_windows.py` (new), `services/specification_source_registration.py`, `services/application.py`, `api.py`, `cli/workflow_commands.py`, `frontend/project.js`.

Changed verification files: `tests/windows/test_specification_source_windows.py` (new), `tests/services/test_specification_source_registration.py`, `tests/test_vision_interview_ui.mjs`, `.github/workflows/ci.yml`, and this plan/handoff.

## Verification and limitations

All commands ran in the assigned worktree on native Windows, through uv where applicable. No Linux or macOS runtime tests were run; the CI configuration change has not been pushed or executed remotely.

| Check | Result |
| --- | --- |
| Initial exact-byte regression | Failed with the independently reproduced POSIX-only capability error before implementation |
| Unsupported-filesystem API/CLI and execution regressions | Both failed before the capability fix; both passed in independent review |
| Locked Specification frontend regression | Failed before the UI change, then passed |
| Missing POSIX flag classification regression | Failed before the error-code correction, then passed on Windows with the missing flag simulated |
| Specification final suite | 63 passed, 2 skipped, 4 dependency deprecation warnings in 80.34 seconds. Skips: Windows symlink creation privilege and unavailable POSIX FIFO creation |
| Existing Windows Vision, reader, API, and CLI suites | 207 passed; 7 warnings (dependency deprecations, blocked socket probe, and an invalid subprocess-handle cleanup warning) |
| Frontend suites | 100 passed |
| `uv lock --check` | Passed |
| `uv run --locked ruff check .` | Passed |
| Ruff format on all changed Python files | Passed |
| `uv run --locked ty check` | Passed |
| `uv run --locked bandit -c pyproject.toml -r cli db models repositories routers services tools utils workflow adapters` | Passed; no findings |
| `git diff --check` | Passed |
| Repository-wide Ruff formatting | 51 untouched existing files would be reformatted; no unrelated formatting applied |
| Pinned canonical pyrepo-check controller | Blocked before checks: its own safe configuration reader requires unavailable no-follow support on Windows. Its annotation gate and full canonical gate are not claimed as passed |
| Broad Windows pytest, `--maxfail=10` | Stopped at 10 failures; 15 passed, 1 skipped, 1 deselected. Failures involve Windows ProactorEventLoop socket creation blocked by pytest-socket, resulting ADK expectations, and CRLF-sensitive existing source fixtures |
| `uv run --locked python scripts/verify_distribution.py` | Failed during isolated `uv build`: PyPI setuptools resolution hit Windows DNS error 11003. No distribution smoke result is claimed |

Exact focused commands:

```text
uv run --locked pytest tests/windows/test_specification_source_windows.py tests/services/test_specification_source_registration.py tests/services/test_specification_source_application.py tests/workflow/test_specification_source_transitions.py -q --tb=short -ra
uv run --locked pytest tests/windows/test_vision_evidence_windows.py tests/services/test_vision_evidence_reader.py tests/adapters/test_vision_bootstrap_api.py tests/adapters/test_cli_workflow_domain.py -q --tb=short
node --test tests/test_workflow_position_display.mjs tests/test_create_project_modal_required_fields.mjs tests/test_vision_interview_ui.mjs
uv run --locked pytest -q --maxfail=10 --tb=short
uv tool run --from git+https://github.com/arduinitavares/pyrepo-check.git@40119c00d4efc469655dec16b1a976e1b3298d7d pyrepo-check ruff annotations ty bandit services/specification_source_registration.py services/specification_source_windows.py tests/windows/test_specification_source_windows.py tests/services/test_specification_source_registration.py
```

Raw local logs are under `.agileforge/issue-238/` (ignored, not part of the patch).

## Handoff

- Branch: `alex/issue-238-windows-specification-source`.
- Worktree: `C:\Users\atavares\.codex\worktrees\44ba\agileforge`.
- Base and unchanged HEAD: `41d8a53ff13a48dcf37707b7e65935757e62e316`.
- The source checkout `C:\Users\atavares\Projects\agileforge` remains clean on master. No commits, pushes, PRs, issue closure, live source registration, backend edits, database resets, or deployment were performed.
- Windows support is limited to the existing adapter's native 64-bit, local NTFS/ReFS handle contract. Unsupported filesystems fail closed. Symlink-creation privilege and POSIX FIFO cases cannot execute on this account; real junction and alias cases did execute.
- Next action: review this patch and obtain the outstanding supported-platform/canonical CI verification before applying it to the running checkout. Applying/merging the change and restarting the existing app require a separate authorized action. This worktree fix is not installed in the running AgileForge app.
