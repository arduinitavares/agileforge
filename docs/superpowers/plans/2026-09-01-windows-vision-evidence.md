# Secure Windows Vision Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect Vision bootstrap repository evidence safely on supported Windows filesystems without weakening POSIX behavior or crossing the provider boundary on preflight failure.

**Architecture:** Keep evidence selection and policy in `VisionEvidenceCollector`, then delegate only capability probing and bounded handle-backed reads to a platform reader. Extract the current POSIX descriptor implementation unchanged and add an isolated Windows adapter that validates native APIs, traverses with retained parent handles, and compares handle identities before and after reads.

**Tech Stack:** Python 3.13.15, stdlib `ctypes`, Win32/NT native file APIs, pytest, GitHub Actions, uv.

**Spec:** `docs/superpowers/specs/2026-09-01-windows-vision-evidence-design.md`

## Global Constraints

- Preserve the exact existing evidence allowlist, source policies, ordering, bounds, warnings, and model-facing schema.
- Preserve current Linux/macOS descriptor behavior and approved compatible in-worktree symlink semantics.
- Windows reads must be retained-parent and handle-anchored; `Path.resolve()` is policy selection only.
- Keep all Windows-native imports, structures, API loading, and status mapping in `services/vision_evidence_windows.py`.
- Validate every required Windows API and explicit `argtypes`/`restype` at runtime.
- Fail with `REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE` when the native or filesystem contract is unavailable.
- Never call a provider, access a protected profile, or use non-synthetic repository data.
- Do not add a runtime dependency.
- Windows runtime behavior is verified only by the added GitHub Actions Windows job.

---

### Task 1: Platform Reader Contract And POSIX Extraction

**Files:**
- Create: `services/vision_evidence_reader.py`
- Create: `services/vision_evidence_posix.py`
- Modify: `services/vision_evidence.py`
- Modify: `tests/services/test_vision_evidence.py`
- Create: `tests/services/test_vision_evidence_reader.py`

**Interfaces:**
- Produces: `RepositoryEvidenceCapability(available: bool, code: str | None, message: str | None)`.
- Produces: `RepositoryEvidenceReader.capability(worktree: Path) -> RepositoryEvidenceCapability`.
- Produces: `RepositoryEvidenceReader.open(worktree: Path) -> AbstractContextManager[RepositoryEvidenceWorktree]`.
- Produces: `RepositoryEvidenceWorktree.read(resolved_path: str, source_path: str, warnings: list[VisionEvidenceWarning], byte_limit: int) -> bytes | None`.
- Produces: `repository_evidence_reader() -> RepositoryEvidenceReader` selecting Windows only on `sys.platform == "win32"`.
- Consumes: existing `VisionEvidenceCollectionError`, `VisionEvidenceErrorCode`, and `VisionEvidenceWarning` without changing public evidence content.

- [ ] **Step 1: Write the reader selection and POSIX characterization tests**

```python
def test_reader_factory_selects_posix_without_loading_windows(monkeypatch):
    monkeypatch.setattr(reader_module.sys, "platform", "darwin")
    reader = repository_evidence_reader()
    assert isinstance(reader, PosixRepositoryEvidenceReader)


def test_posix_reader_retains_approved_internal_link_behavior(...):
    # Reuse the existing docs/spec/spec.md -> specs/spec.md fixture.
    bundle = collector.collect(project_id)
    assert specification.relative_path == "docs/spec/spec.md"
    assert specification.content == "Approved technical specification."
```

Move the current descriptor race assertions to patch
`services.vision_evidence_posix.os`, not collector internals. Each test must
still name the same escape or replacement break it catches.

- [ ] **Step 2: Run the new and existing evidence tests and confirm the intended red failures**

Run:

```bash
uv run --frozen pytest tests/services/test_vision_evidence_reader.py tests/services/test_vision_evidence.py -q
```

Expected: reader module imports fail because the contract and POSIX adapter do
not exist. Existing tests remain independently runnable before production code
moves.

- [ ] **Step 3: Implement the platform-neutral contract and extract POSIX code**

```python
@dataclass(frozen=True)
class RepositoryEvidenceCapability:
    available: bool
    code: str | None = None
    message: str | None = None


class RepositoryEvidenceReader(Protocol):
    def capability(self, worktree: Path) -> RepositoryEvidenceCapability: ...
    def open(self, worktree: Path) -> AbstractContextManager[RepositoryEvidenceWorktree]: ...


class RepositoryEvidenceWorktree(Protocol):
    def read(
        self,
        resolved_path: str,
        source_path: str,
        warnings: list[VisionEvidenceWarning],
        byte_limit: int,
    ) -> bytes | None: ...
```

Extract `_open_worktree_descriptor`, `_open_descriptor`,
`_open_regular_leaf`, `_open_parent_descriptor`, `_component_is_safe`, and
`_file_identity` into `PosixRepositoryEvidenceReader`. Keep the sequence of
`os.stat`, `os.open`, `os.fstat`, `os.read`, and close operations unchanged.
Have `VisionEvidenceCollector._read_text()` resolve and policy-check the logical
path, then ask its injected/default reader for bytes before decoding and parsing.

- [ ] **Step 4: Run the focused tests and verify green**

Run the Task 1 command again. Expected: all selected tests pass on macOS and the
existing symlink/race behavior is unchanged.

- [ ] **Step 5: Review the extraction diff for accidental policy movement**

Run:

```bash
git diff --stat
git diff -U12 -- services/vision_evidence.py services/vision_evidence_posix.py services/vision_evidence_reader.py
```

Verify the collector still owns `_resolve_internal_path`, UTF-8 handling,
parsing, warnings, limits, and fingerprints.

### Task 2: Windows Native Handle Adapter

**Files:**
- Create: `services/vision_evidence_windows.py`
- Create: `tests/windows/test_vision_evidence_windows.py`
- Modify: `services/vision_evidence_reader.py`

**Interfaces:**
- Consumes: `RepositoryEvidenceCapability` and `RepositoryEvidenceReader` from Task 1.
- Produces: `WindowsRepositoryEvidenceReader(api: _WindowsApi | None = None)`.
- Produces: internal `_WindowsApi.load() -> _WindowsApi` that validates functions and signatures.
- Produces: internal `_FileIdentity(volume_serial: int, file_id: bytes, size: int, creation_time: int, last_write_time: int, change_time: int, attributes: int)`.
- Produces: internal `_open_relative(parent_handle: int, component: str, *, directory: bool) -> _Handle` using `NtCreateFile` and `OBJECT_ATTRIBUTES.RootDirectory`.

- [ ] **Step 1: Write Windows runtime tests first**

Mark the module with `pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires Windows handle semantics")`. Use only temporary Git repositories.

Add these tests:

```python
def test_windows_reader_collects_ordinary_repository_evidence(...): ...
def test_windows_reader_rejects_external_junction_escape(...): ...
def test_windows_reader_rejects_leaf_replacement_after_resolution(...): ...
def test_windows_reader_retains_parent_when_intermediate_is_replaced(...): ...
def test_windows_reader_detects_change_during_bounded_read(...): ...
def test_windows_reader_allows_compatible_internal_link_when_supported(...): ...
def test_windows_reader_reports_missing_native_api_as_capability_unavailable(...): ...
def test_windows_reader_reports_unusable_file_identity_as_capability_unavailable(...): ...
```

Create junctions with a bounded `cmd /c mklink /J` helper. Attempt symbolic-link
creation only for the internal-link test and skip that test when Windows denies
the privilege. Race tests monkeypatch `_open_relative` or `_read_handle` at the
narrow adapter seam and assert real bytes/results, not mock call counts.

- [ ] **Step 2: Run collection to prove non-Windows discovery is safe and Windows tests are not falsely passing**

Run on macOS:

```bash
uv run --frozen pytest tests/windows/test_vision_evidence_windows.py --collect-only -q
```

Expected: all Windows tests collect and are marked skipped at execution. Import
must not attempt to load Windows DLLs.

Run one selected test normally and confirm it is skipped, not passed.

- [ ] **Step 3: Implement fixed-width native structures and runtime API loading**

Define explicit structures for `UNICODE_STRING`, `OBJECT_ATTRIBUTES`,
`IO_STATUS_BLOCK`, `FILE_ID_128`, `FILE_ID_INFO`, `FILE_BASIC_INFO`,
`FILE_STANDARD_INFO`, and `FILE_ATTRIBUTE_TAG_INFO`. Validate 64-bit pointer
size and load native DLLs only inside `_WindowsApi.load()`.

Assign every required signature, for example:

```python
nt_create_file.argtypes = [
    POINTER(HANDLE), ACCESS_MASK, POINTER(OBJECT_ATTRIBUTES),
    POINTER(IO_STATUS_BLOCK), POINTER(LARGE_INTEGER), ULONG,
    ULONG, ULONG, ULONG, PVOID, ULONG,
]
nt_create_file.restype = NTSTATUS
```

Any unavailable symbol, unsupported pointer size, or capability query returns
the closed capability result without exposing native error text.

- [ ] **Step 4: Implement retained-parent traversal and bounded handle reads**

Use `CreateFileW` only for the strictly resolved root and its final identity
reopen. Use `NtCreateFile` for every component and leaf relative to the retained
parent. Apply `OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE`,
`FILE_OPEN_REPARSE_POINT`, directory/non-directory create options, synchronous
I/O, and read/write/delete sharing.

Before reading, reject a reparse attribute or wrong type. Read at most the
provided limit with `ReadFile`. Compare before/after leaf identity, reopen the
leaf relative to the retained parent, and compare the root identity after all
work. Map change/replacement to the existing changed-during-collection error;
map unavailable APIs or identity/query support to the new capability error.

- [ ] **Step 5: Run all locally executable reader tests**

Run:

```bash
uv run --frozen pytest tests/services/test_vision_evidence_reader.py tests/services/test_vision_evidence.py tests/windows/test_vision_evidence_windows.py -q
```

Expected on macOS: platform-neutral and POSIX tests pass; every Windows runtime
test is explicitly skipped.

### Task 3: Capability Error, Application Preflight, And Transport Projection

**Files:**
- Modify: `services/vision_evidence.py`
- Modify: `services/vision_input.py`
- Modify: `services/application.py`
- Modify: `workflow/contracts.py`
- Modify: `api.py`
- Modify: `cli/workflow_commands.py`
- Modify: `tests/services/test_vision_input.py`
- Modify: `tests/adapters/test_vision_bootstrap_api.py`
- Modify: `tests/adapters/test_vision_bootstrap_cli.py`
- Modify: `tests/adapters/test_cli_workflow_domain.py`

**Interfaces:**
- Produces: `VisionEvidenceErrorCode.REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE`.
- Produces: `WorkflowErrorCode.REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE`.
- Produces: `VisionInputService.bootstrap_capability(project_id: int) -> RepositoryEvidenceCapability`.
- Produces: `AgileForgeApplication.vision_bootstrap_capability(project_id: int) -> RepositoryEvidenceCapability`.
- Produces: CLI `blocked_commands` entries with `node_id`, `reason_code`, and `message`, never an executable `command`.

- [ ] **Step 1: Add failing error mapping and provider-nonexecution tests**

Extend the existing preflight parameterization with the new code. Add a fake
Vision input whose `bootstrap_capability()` returns unavailable and whose
`build_bootstrap()` fails if called. Assert:

```python
result = app.bootstrap_vision(request)
assert result.error.code is WorkflowErrorCode.REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE
assert vision_input.build_calls == 0
assert app.execution_calls == []
```

The production change that makes these tests fail is either omitting capability
preflight or mapping it to repository-changed.

- [ ] **Step 2: Add failing API action-projection test**

For an available `vision.bootstrap` decision and unavailable capability, call
the position endpoint. Assert the action retains its semantic identity but has:

```python
{
    "node_id": "vision.bootstrap",
    "request_kind": "generate_vision_bootstrap",
    "availability": "locked",
    "reason_code": "REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE",
}
```

The generic frontend already refuses actions with `availability == "locked"`;
assert no new UI coercion/default is introduced.

- [ ] **Step 3: Add failing CLI workflow-next projection test**

Call `workflow_next(application=application, project_id=PROJECT_ID)` with the
same decision and capability. Assert `commands == []` and
`blocked_commands == [{"node_id": ..., "reason_code": ..., "message": ...}]`.
Assert a capable reader retains the exact existing command spelling.

- [ ] **Step 4: Run the new focused tests and confirm red for missing capability behavior**

Run:

```bash
uv run --frozen pytest tests/services/test_vision_input.py tests/adapters/test_vision_bootstrap_api.py tests/adapters/test_vision_bootstrap_cli.py tests/adapters/test_cli_workflow_domain.py -q
```

- [ ] **Step 5: Implement one shared capability preflight**

Have `VisionEvidenceCollector.capability(project_id)` load the same active
binding context used by collection. Project-only collection returns available.
Attached projects call the selected reader capability on the resolved root.

`VisionInputService.bootstrap_capability()` delegates to that collector.
`AgileForgeApplication.bootstrap_vision()` performs replay first, selects the
current decision, then checks capability before `build_bootstrap()`. Map the new
error code exhaustively in `_vision_evidence_workflow_error_code()`.

Use `getattr` only at transport projection compatibility seams for lightweight
test applications. Production `AgileForgeApplication` exposes the typed method.
API locked action and CLI blocked command must call the same method and preserve
existing behavior when it is absent or returns available.

- [ ] **Step 6: Run the Task 3 focused tests and verify green**

Run the Task 3 command again. Confirm direct CLI/API mutations return identical
codes and no input-builder/execution call occurs.

### Task 4: Required Windows CI Contract

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_ci_contract.py`

**Interfaces:**
- Produces: required `windows-vision-evidence` GitHub Actions job.
- Consumes: uv `0.12.8`, Python `3.13.15`, provider-free pytest suites.

- [ ] **Step 1: Write the failing CI structural contract**

Update `test_workflow_has_required_runtimes` to require:

```python
"windows-vision-evidence": ("windows-latest", CANONICAL_PYTHON)
```

Add a test requiring one command equivalent to:

```text
uv run --locked pytest tests/windows/test_vision_evidence_windows.py tests/services/test_vision_evidence_reader.py tests/adapters/test_vision_bootstrap_api.py tests/adapters/test_cli_workflow_domain.py -q
```

Assert no secrets, provider variables, live markers, profile initialization, or
network-backed provider flags occur in the job.

- [ ] **Step 2: Run the CI contract test and confirm red**

Run:

```bash
uv run --frozen pytest tests/test_ci_contract.py -q
```

Expected: missing `windows-vision-evidence` job.

- [ ] **Step 3: Add the Windows job**

Use pinned existing checkout/setup-uv actions, install Python `3.13.15`, and run
only the focused provider-free command. Use PowerShell-compatible command
syntax and no protected environment.

- [ ] **Step 4: Run the CI contract test and verify green**

Run the Task 4 command again.

### Task 5: Integrated Verification, Independent Review, And Final Commit

**Files:**
- Modify only files required by reviewer findings.

**Interfaces:**
- Consumes: all prior tasks and the approved design.
- Produces: one locally committed implementation on `dev/issue-234-windows-vision-evidence`.

- [ ] **Step 1: Run focused evidence and transport suites**

```bash
uv run --frozen pytest \
  tests/services/test_vision_evidence_reader.py \
  tests/services/test_vision_evidence.py \
  tests/services/test_vision_input.py \
  tests/windows/test_vision_evidence_windows.py \
  tests/adapters/test_vision_bootstrap_api.py \
  tests/adapters/test_vision_bootstrap_cli.py \
  tests/adapters/test_cli_workflow_domain.py \
  tests/test_ci_contract.py -q
```

Record pass and skip counts. On macOS, identify every Windows-only skip.

- [ ] **Step 2: Run formatting and diff checks**

```bash
git diff --check
uv lock --check
```

- [ ] **Step 3: Run the canonical full gate serially**

```bash
./agileforge-dev check
```

Redirect full output to a temporary log if noisy, preserve the exit code, and
report a bounded summary. Do not run another full repository gate concurrently.

- [ ] **Step 4: Commit the implementation checkpoint**

```bash
git add services/vision_evidence.py services/vision_evidence_reader.py \
  services/vision_evidence_posix.py services/vision_evidence_windows.py \
  services/vision_input.py services/application.py workflow/contracts.py \
  api.py cli/workflow_commands.py tests/services/test_vision_evidence.py \
  tests/services/test_vision_evidence_reader.py tests/services/test_vision_input.py \
  tests/windows/test_vision_evidence_windows.py \
  tests/adapters/test_vision_bootstrap_api.py \
  tests/adapters/test_vision_bootstrap_cli.py \
  tests/adapters/test_cli_workflow_domain.py tests/test_ci_contract.py \
  .github/workflows/ci.yml docs/superpowers/plans/2026-09-01-windows-vision-evidence.md
git commit -m "fix: support secure Windows vision evidence"
```

- [ ] **Step 5: Request independent correctness/security review**

Dispatch one read-only GPT-5.6 Sol reviewer at `xhigh`, Standard context, over
base `d8d8fc52a428e98b64e17eae2385f4670e12b7d5` through implementation HEAD.
Require special attention to native signatures, handle ownership, reparse
semantics, error mapping, action projection, test realism, and provider
nonexecution. Fix every Critical or Important finding with a new failing test
where behavior changes, rerun focused tests and the full gate, then commit the
review fixes.

- [ ] **Step 6: Run final fresh verification**

After all review fixes, rerun the focused command, `uv lock --check`,
`git diff --check`, and `./agileforge-dev check`. Confirm `git status --short`
is empty and record final branch/base/commit SHAs.

- [ ] **Step 7: Report bounded completion evidence**

Report:

- exact worktree, branch, base SHA, and final commit SHA;
- synthetic/provider-free data boundary;
- focused macOS pass/skip counts;
- canonical full-gate result;
- independent review verdict and fixes;
- clean status;
- explicit statement that Windows runtime tests require the new GitHub Actions
  job and did not run locally on macOS;
- no push, merge, issue mutation, profile mutation, or provider call.
