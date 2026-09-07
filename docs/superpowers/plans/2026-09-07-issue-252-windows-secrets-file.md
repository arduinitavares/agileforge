# Issue 252 Windows secrets file implementation plan

> **For agentic workers:** Use superpowers:executing-plans for inline execution with test-driven-development. No implementation delegation, commits, publication, runtime changes, or worktree cleanup is authorized.

**Goal:** Safely load an explicit provider file on Windows through the development launcher.

**Architecture:** Isolate secure descriptor acquisition in `cli/dev_secrets.py`. Preserve the existing parser and precedence in `cli/dev_main.py`; all three callers (info, CLI, UI) use the same reader. Use Win32 leaf opening and retained handle validation, following the ownership pattern in the existing Windows evidence adapter without its repository traversal policy.

**Tech Stack:** Python 3.13.15, standard-library ctypes/msvcrt/os, existing python-dotenv, uv and pytest.

**Spec:** User repair constraints, open GitHub issue https://github.com/arduinitavares/agileforge/issues/252, and owner follow-up https://github.com/arduinitavares/agileforge/issues/252#issuecomment-5570681553 (read live).

## Constraints and audit evidence

- Base: `fecc9c0abeea5cf1f0c89d68f6bfa1a385517f79`, verified clean local master.
- Repair: `.worktrees/alex-issue-252-windows-secrets-file`, branch `alex/issue-252-windows-secrets-file`. Preserve this worktree and issue 251.
- No real credentials or existing runtime state. Only disposable sentinel files, fixtures, and the new worktree's `issue-252-audit` profile.
- Real Windows reproduction before code changes: Python 3.13.15, `O_NOFOLLOW_available=false`, `is_regular=true`, `is_symlink=false`. Direct reader and `sh ./agileforge-dev info --profile issue-252-audit --secrets-file <disposable-provider-file> --json` both fail: `secrets file must be a regular file: <disposable-provider-file>` (exit 1).
- Baseline: two requested dev-runtime suites: 42 passed, 2 skipped. Four existing assertions accept Windows failure, so this is not proof of support.
- Native APIs consulted through find-docs/Context7 and Microsoft Learn: [CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew), [GetFileType](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfiletype), [CRT handle ownership](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/open-osfhandle).

## Actual security contract

The final component must open as a regular file without following a leaf symlink (POSIX) or any leaf reparse point (Windows). Validate the opened object and read that same retained object; never validate a pathname and then reopen it for parsing. Replacement before the atomic open selects the object validated by that open. Replacement afterwards must not redirect the read. Windows requests read sharing only, denying ordinary writes, deletion, and replacement while retained. POSIX retains the original inode if the name is replaced; concurrent in-place writes are outside the original contract.

Parent components are trusted. Existing POSIX O_NOFOLLOW protects only the leaf; it does not anchor or reject parent symlinks. Windows likewise permits normal parent traversal, including junctions. Neither implementation authenticates the file's owner, enforces permissions/ACLs, rejects hard links, or promises an adversarial filesystem cannot lie. No repository-containment, exact-spelling, or chain-revalidation claims are made. Windows device namespaces (including extended `\\?\` spellings), reserved device names, and alternate data streams are rejected before opening; GetFileType validates disk type, and os.fstat validates attributes and regular-file status on the retained descriptor. Unsupported native capability fails closed with an accurate, content-free error.

Credentials enter only the allowlisted child environment or boolean preflight. Preserve `dotenv_values(..., interpolate=False, verbose=False)` and invoking-environment precedence (including an explicitly empty value). Decode/read failures must expose no source content or chained exception to command output. Dotenv's existing malformed-line behavior remains tolerant, with diagnostics limited to line numbers. Raw CLI stdout must receive the same credential redaction already used for stderr/JSON. Provider-generated artifacts and live UI output are outside this provider-free validation; UI routing is tested with a controlled child.

## Task 1: Reader and disclosure regressions

- [x] Remove the branches accepting no-O_NOFOLLOW failures in the requested tests; retain real platform behavior. Replace the POSIX-only swap operation with a platform-specific assertion: Windows replacement is denied while reading, POSIX reads the retained inode.
- [x] Run the preflight/forwarding regressions before production edits; require failures from the reported guard.
- [x] Add reader tests for types, links/reparses, retention, non-inheritance, failure cleanup, unsupported capability, malformed input, precedence, allowlist, and interpolation. Use native Windows execution, not a POSIX mock fallback.

```python
selected.write_text("OPEN_ROUTER_API_KEY=disposable-sentinel\n", encoding="utf-8")
assert dev_main._provider_environment(selected) == {
    "OPEN_ROUTER_API_KEY": "disposable-sentinel"
}
```

## Task 2: Scoped implementation

- [x] Add `open_secrets_file(path: Path) -> Iterator[TextIO]` to `cli/dev_secrets.py`, with `SecretsFileError` for fixed safe errors. POSIX uses O_NOFOLLOW and O_NONBLOCK before fstat (avoids FIFO hangs). Windows uses CreateFileW/OPEN_EXISTING/FILE_FLAG_OPEN_REPARSE_POINT, validates disk type, then transfers ownership using msvcrt.open_osfhandle. Set O_NOINHERIT; fstat rejects non-regular/reparse objects using st_mode/st_file_attributes, and fdopen reads that same descriptor. Close each resource exactly once on every exit.
- [x] Keep `_provider_environment` parsing within the retained stream context; wrap read/decode failures safely and preserve parser options and precedence.
- [x] Redact raw CLI stdout through `_redact_text(result.stdout, secret_values)`; add a sentinel regression first.
- [x] Test shared reader routing through preflight, provider-free CLI forwarding, and controlled UI startup; scan all disposable manifests/databases/artifacts and captured output for sentinels.

## Task 3: Verification and independent review

- [x] Extend the existing Windows CI test list only as needed to execute the new regressions. Make native success mandatory on Windows.
- [x] Run focused suites, `uv lock --check`, and `sh ./agileforge-dev check --json`. Current gate runs lock, pyrepo-check, frontend tests, whitespace, distribution verification in order, stopping at first failure. Report a blocked gate honestly; do not modify tooling or suppress failures. The canonical attempt was incomplete; this checkbox records execution, not a passing gate.
- [x] Run direct relevant quality commands if the controller cannot execute, preserving the canonical failure as evidence.
- [x] Request one fresh-context, read-only `gpt-5.6-sol` / `xhigh` / Standard review focused on disclosure, handle ownership, path races, platforms and coverage; no nested delegation. Verify every finding and rerun affected checks.
- [x] Save final evidence here; retain uncommitted work. No CI success claims without an actual completed CI run. Distinguish local Windows execution from unexecuted Linux/macOS and active-runtime integration.

## Task 4: Follow-up corrections from independent review

- [x] Add case-insensitive `IPC$` UNC-share boundary regressions that fail if native API loading is reached, then reject the share before any native open.
- [x] Add real local-child regressions for CR, LF, and CRLF credential values from both the explicit file and invoking environment, covering normalized raw stdout and stderr without changing parser or precedence semantics.
- [x] Cover Windows text-writer newline expansion separately, while retaining existing JSON redaction and no-secret byte-for-byte behavior.
- [x] Add `cli/dev_secrets.py` to the committed cross-worktree fixture overlay and rerun the previously failing isolation test against the uncommitted candidate.
- [x] Rerun the focused suite and current repository gates from one stable source state. Record complete outputs, final hashes, blocked snapshot checks, and unexecuted platform/CI checks.

### Follow-up baseline hashes before incremental edits

These SHA-256 values distinguish the retained implementation from this follow-up. All ten retained files were unstaged at HEAD `fecc9c0abeea5cf1f0c89d68f6bfa1a385517f79`; `test_cross_worktree.py` was unchanged from HEAD.

| File | SHA-256 |
| --- | --- |
| `.github/workflows/ci.yml` | `4e8b132ffc9914b0acdedcc1e0a639ac031eb834dd9736d5584fb2cb26d49c93` |
| `cli/dev_main.py` | `a5ad4f93af71a211fb61b9f86206ac37c20fb35ecb5195b0ae98c307bc75dc31` |
| `tests/dev_runtime/test_cli_forwarding.py` | `615820b9e8e24bbc1b21422364e8f6cc5e2253fe94a90b052dead468c3913bf2` |
| `tests/dev_runtime/test_dev_main.py` | `0235919eb34c5c378f7337927d0427335b7d0190d0ec1e43fd738d7ac93f059f` |
| `tests/dev_runtime/test_dev_server.py` | `49f114e1b5aa2b015e4e18cdad6c8fff9c293a167c3e99c24c7e700aa28edd3a` |
| `cli/dev_secrets.py` | `2fa8030d3ed7bd3e8f126d4987d02e70615f8c6dd78388b1db0665d31bec1f48` |
| `docs/superpowers/plans/2026-09-07-issue-252-windows-secrets-file.md` | `907b23a847059bb0eb9bb5b124c9200fc5161f65d70a679305dc69dad5e5297c` |
| `tests/dev_runtime/test_dev_secrets_file.py` | `e4b39bfa7f3f0e8ecb49ea4f18340a14b82f0681185df7c60efa47a3f02c39fa` |
| `tests/dev_runtime/test_dev_secrets_posix.py` | `97c0bb2dc0af531e1513eefdda46b043ac1586e7d9d4566ff8de8607c1c96ae4` |
| `tests/windows/test_dev_secrets_windows.py` | `934b35404e3dee2912066b09c96a6a2a0d958f33271c25a10189ad5290337fbc` |
| `tests/dev_runtime/test_cross_worktree.py` | `a9c16d16610eb44cb34d2a35c17ffc301fa8bdee53a913d7be726b66f393d4e4` |

## Follow-up verification record (2026-09-07)

- Finding A red: the existing native API boundary test produced **2 failures, 7 passes**. Both `\\server\IPC$\name` and mixed-case `iPc$` reached the patched `_WindowsApi.load`; no native open occurred. Green after adding `ipc$` to the case-folded UNC share set: **9 passed**.
- Finding B red: a real local dummy child received the accepted credential and wrote it to both stdout and stderr. File and invoking-environment cases covered CR, LF, and CRLF with byte writes and Windows text writes. Exact-only redaction produced **8 expected failures, 4 LF controls passed**. Green after replacing exact and capture-equivalent spellings, longest first: **12 passed**. Existing JSON and no-secret behavior ran in the final focused suite.
- Finding C red: `test_same_profile_name_is_fully_isolated_across_linked_worktrees` failed with `ModuleNotFoundError: No module named 'cli.dev_secrets'`. Green after adding the module to the copied-and-committed fixture overlay: **1 passed**. The final complete file run was **4 passed** in 83.94 seconds.
- Final required focused command: **150 passed, 6 skipped** in 150.09 seconds. The increase from the retained 136 is the 12 newline cases and 2 `IPC$` spellings. Four skips are POSIX-only; two Windows file-symlink cases lack SeCreateSymbolicLinkPrivilege. Full output: `.agileforge/issue-252-followup-focused-final.log`; JUnit: `.agileforge/issue-252-followup-focused-final.xml`.
- Exact Windows UI runtime CI test list: **196 passed, 5 skipped** in 310.84 seconds. Its XML assertion found the real Windows UI ownership case and real Windows secrets-file case exactly once each, both unskipped. The other skips require unavailable Windows symlink privileges. Full output and JUnit use the `issue-252-followup-windows-ui-runtime` names under ignored `.agileforge`.
- Final direct checks passed: `uv lock --check`; Ruff formatting for all nine changed/new Python files; repository `ruff check .`; repository `ty check`; Bandit for `cli/dev_main.py` and `cli/dev_secrets.py`; `git diff --check`; 12 CI-contract tests; and all 114 frontend tests.
- Repository-wide `ruff format --check .` still reports **65 files**. `git diff --exit-code HEAD -- <all 65 flagged paths>` returned 0, proving all remain unchanged from the repair base. No broad formatting or suppression was applied.
- The canonical `pyrepo-check --all` and distribution verification were not rerun. Five required repair files, including `cli/dev_secrets.py`, remain untracked and are absent from `git ls-files`; those snapshot-based checks cannot verify this final candidate until the parent-authorized staging step. Staging is outside this worker's permission. Distribution also remains subject to separate issue #254; no distribution code or test was changed here.
- No full aggregate was started. The prior **883 passed, 1 failed, 4 skipped, 1 deselected** aggregate remains historical evidence only; the fixture failure it exposed is now covered by the final complete cross-worktree run. Linux, macOS, completed GitHub Actions, and privileged Windows symlink cases remain unexecuted here.

## Verification record (2026-09-07)

- Red: strengthened preflight, precedence, and retained-reader tests failed at the original O_NOFOLLOW guard (3 failures). Eight additional reader cases failed at that guard; the separate raw-output sentinel regression failed because stdout echoed the credential.
- Green: real `sh ./agileforge-dev info` and provider-free `sh ./agileforge-dev cli -- project list`, using only the new `issue-252-audit` profile and a temporary sentinel file, both exited 0. Preflight emitted `OPEN_ROUTER_API_KEY: true`; CLI returned zero projects. Sentinel absent from captured stdout/stderr, command arguments, profile manifest and business database. No trace artifact created. The temporary credential file was removed; the disposable profile is retained in this repair worktree.
- Final focused command: `uv run --locked pytest tests/dev_runtime/test_dev_main.py tests/dev_runtime/test_cli_forwarding.py tests/dev_runtime/test_dev_secrets_file.py tests/dev_runtime/test_dev_secrets_posix.py tests/dev_runtime/test_dev_server.py tests/windows/test_dev_secrets_windows.py tests/test_ci_contract.py -q --tb=short --junitxml=.agileforge/issue-252-focused.xml`. Result: **136 passed, 6 skipped**, zero failures/errors, 122.27 seconds. Four skips require POSIX; two pre-existing symlink tests require unavailable Windows SeCreateSymbolicLinkPrivilege. All 20 Windows-specific credential cases executed. Native Windows junction tests do execute; non-directory reparse metadata and failure paths are fault-injected. No claim of local Linux/macOS or privileged Windows symlink execution.
- Changed Python lint, formatting, ty, Bandit, whitespace and `uv lock --check` passed. All 114 frontend tests passed. The new CI paths pass the unchanged 12 CI contract tests; a filename collision with its `secrets.` guard was resolved by naming the file `test_dev_secrets_file.py`, without weakening the guard.
- Repository-wide `uv run --locked ruff check .`, `uv run --locked ty check`, and Bandit over the application packages also passed. Repository-wide `ruff format --check .` failed on **65 files outside this repair**; `git diff --exit-code HEAD -- <all 65 flagged paths>` returned 0, proving those files are unchanged from the base. They were not reformatted or suppressed. All eight changed/new Python files pass formatting.
- Distribution verification (`uv run --locked python scripts/verify_distribution.py`) failed at its isolated `uv build --no-sources`: resolving `setuptools>=77` from `https://pypi.org/simple/setuptools/` failed with Windows DNS error 11003 after three retries. No configuration change, dependency bypass, or relaxed gate was applied. This is not a completed distribution check.
- Canonical gate: `sh ./agileforge-dev check --json` started at 01:49:36 UTC and returned after 2809.10 seconds (46m49s). The owned pytest process was stopped after exceeding the stated 45-minute budget, matching the CI job's time allowance. Its PID, unique launcher path and process creation time were verified before stopping; no other runtime process was stopped. Launcher process exit was 1, with JSON `exit_code: 2`, `failed_stage: python-quality`. Lock passed; pyrepo-check returned `error (strict aggregate, incomplete)`, reporting changed tracked repository state, unfinalized pytest evidence and untrusted/incomplete coverage. Production/test edits had continued during this earlier-started run, so it cannot verify the final source state even independently of termination. Final focused tests above ran after the review correction. Downstream canonical stages were not reached; separately executed commands are reported separately.
- The incomplete aggregate output reached about 90% and included four failure markers in the unchanged `tests/windows/test_vision_evidence_windows.py`, without finalized failure details. A fresh isolated `uv run --locked pytest tests/windows/test_vision_evidence_windows.py -q --tb=short` completed **11 passed** in 13.78 seconds. The suite and its implementation were not changed by this repair. The aggregate-only failures remain unexplained; the isolated pass does not establish a clean full suite. No suppressions, test weakening or unrelated fixes were made.
- Independent review: one fresh-context `gpt-5.6-sol`, `xhigh`, Standard review, read-only, no nested delegation. It found one P2: UNC named-pipe paths could reach CreateFileW before disk-type rejection. Root reproduced three failures using an API boundary guard (no real IPC/network open), then added case-insensitive UNC pipe/mailslot share rejection. The 30-case Windows/reader subset, lint and ty passed. The same reviewer inspected the correction and confirmed the finding resolved, with no other actionable defect.
- Windows IPC references: [Pipe Names](https://learn.microsoft.com/en-us/windows/win32/ipc/pipe-names), [Mailslot Names](https://learn.microsoft.com/en-us/windows/win32/ipc/mailslot-names). Context7 returned no snippets for that detail; the official pages confirmed the syntax.
- Last account check: **12% weekly remaining**; reset 2026-09-07 08:42 Europe/Brussels. Main Codex five-hour window unavailable. No resets, credits, scheduling or publishing used.

## Handoff and remaining acceptance work

The reported Windows defect is confirmed and the scoped repair passes final focused verification in the isolated worktree. Full acceptance is blocked by the incomplete canonical run, the 65 unchanged formatting failures and distribution-build DNS failure. No completed CI run exists for these uncommitted changes. Linux/macOS behavior and privileged Windows file-symlink cases remain unexecuted locally; Windows junction behavior, native regular-file success and Windows replacement denial were exercised.

For the next decision, review the retained diff and decide how to resolve the existing gate/environment blockers. A subsequent canonical run must use a stable source snapshot, retain complete pytest failure output, and investigate any recurring Windows vision-suite failures. Complete Linux/Windows CI and distribution verification before treating the entire quality gate as passed. Do not broaden this security repair or integrate it into the active runtime without a later decision.

Final read-only repository checks: repair branch remains at the stated base with ten changed/new files, all unstaged. Main master is still clean at `fecc9c0abeea5cf1f0c89d68f6bfa1a385517f79`; both issue worktrees remain present. Issue 251 is at `b810211ae3e621acb23f309369275cf8ea19e156` when last inspected and was not modified by this task. The disposable audit profile and focused JUnit report remain under this repair worktree's ignored `.agileforge` directory. No temporary credential file is retained.

## Integration boundary

Changes remain uncommitted on the repair branch. The main checkout is not modified by this task; issue 251 is not modified by this task. No providers, real credentials, actual profiles, backend state, deployed servers or operational databases were used for this repair. A verified isolated-worktree fix does not repair the active AgileForge runtime. Integration and operational verification require a later decision.
