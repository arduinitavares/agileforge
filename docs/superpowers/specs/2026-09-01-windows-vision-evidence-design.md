# Secure Windows Vision Evidence Design

## Purpose

Allow Vision bootstrap to collect the existing bounded repository evidence on
Windows without weakening the repository containment, reparse-point, file
identity, or provider-preflight guarantees already enforced on POSIX systems.

This design fixes GitHub issue #234. It does not expand the evidence allowlist,
change model-facing evidence, call a provider during collection, or alter the
approved human workflow.

## Current Failure

`VisionEvidenceCollector` retains the worktree and traverses evidence paths with
`os.open(..., dir_fd=...)`, `O_NOFOLLOW`, and `O_DIRECTORY`. CPython does not
provide the required `openat`/`dir_fd` operations on Windows. The current root
open therefore fails before any evidence file is read and reports the unrelated
`REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION` code.

Removing the flag check is not a fix. Later traversal and replacement checks
also depend on directory-relative operations, and a path-only
`Path.resolve()`/`lstat()` implementation would introduce a time-of-check to
time-of-use gap.

## Decision

Introduce one platform-neutral repository evidence-reader boundary with two
implementations:

- `PosixRepositoryEvidenceReader` owns the existing descriptor-relative logic.
  Its behavior and approved internal-link semantics remain unchanged.
- `WindowsRepositoryEvidenceReader` owns all `ctypes`, Windows handle, native
  structure, status-code, and API-loading details. No other collector module
  imports or calls Windows-native APIs.

The collector continues to own evidence selection, source-policy compatibility,
parsing, warnings, bounds, ordering, and fingerprints. A reader owns only:

1. capability validation for one worktree;
2. retaining a trusted worktree anchor;
3. handle-anchored traversal of one already-approved resolved path;
4. bounded reads and file identity/change detection.

## Alternatives Rejected

### Absolute Win32 opens plus final-path validation

`CreateFileW` and `GetFinalPathNameByHandleW` can validate the object returned by
one absolute open, but documented Win32 file APIs do not accept a retained
directory handle as the root for a relative child open. Rebuilding each child
path from strings would lose the retained-parent property when an intermediate
directory is replaced.

### Path resolution and `lstat`

Resolving and inspecting a path before reopening it leaves a replacement window
between validation and use. Path resolution remains useful only for choosing
which compatible allowlisted target a stable internal link denotes. It is not
trusted as the read safety mechanism.

### Disable repository evidence on Windows

Vision bootstrap is the required workflow action and ordinary Windows
repositories can satisfy the safety contract. Declaring Windows unsupported
would preserve the blocking defect.

## Windows Compatibility Boundary

The adapter uses documented Windows structures and semantics through a narrow
user-mode `NtCreateFile` compatibility boundary exported by `ntdll.dll`.
Microsoft documents that user-mode callers use `NtCreateFile`, that
`OBJECT_ATTRIBUTES.RootDirectory` makes the object name relative to a retained
directory handle, and that `OBJ_DONT_REPARSE` prevents reparse traversal.

The adapter loads and validates these functions at runtime:

- `kernel32.CreateFileW`
- `kernel32.CloseHandle`
- `kernel32.GetFileInformationByHandleEx`
- `kernel32.GetFinalPathNameByHandleW`
- `kernel32.GetVolumeInformationByHandleW`
- `kernel32.ReadFile`
- `ntdll.NtCreateFile`
- `ntdll.NtClose`
- `ntdll.RtlNtStatusToDosError`

Every function receives explicit `argtypes` and `restype`. The required
structures use fixed-width Windows types rather than host-sized Python aliases.
The adapter rejects unavailable functions, unexpected pointer sizes, unusable
signatures, or failed capability probes with
`REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE`.

Importing and executing POSIX code never loads `kernel32`, `ntdll`, `msvcrt`, or
Windows-only types. The Windows adapter module can be imported for test
discovery on other platforms, but native loading occurs only when its factory is
selected on `sys.platform == "win32"`.

## Supported Windows Filesystems

The supported contract is a local NTFS or ReFS worktree on a supported
64-bit Python 3.13 Windows runtime. The capability decision requires both an
NTFS/ReFS filesystem-name check and successful handle-based identity,
final-path, relative-open, and query operations.

The adapter requires:

- stable `FILE_ID_INFO` values for root, directories, and files;
- `FILE_BASIC_INFO`, `FILE_STANDARD_INFO`, and
  `FILE_ATTRIBUTE_TAG_INFO` queries;
- `FILE_REMOTE_PROTOCOL_INFO` rejection plus UNC final-path rejection;
- normalized final paths for retained handles;
- directory-relative `NtCreateFile` opens;
- read, write, and delete sharing so ordinary Git operations do not create an
  artificial lock conflict.

SMB/network shares, FAT, exFAT, third-party filesystems, or filters that cannot
provide the complete contract fail closed as capability-unavailable. The error
does not claim that the repository changed.

## Worktree And Path Semantics

The logical evidence allowlist remains unchanged. For each logical source:

1. Open the logical source into a retained metadata-only identity sentinel.
2. Resolve it strictly while that sentinel remains open to determine its target.
3. Require the resolved target to stay inside the resolved worktree.
4. Require the resolved repository-relative target to have the same declared
   source policy as the logical source.
5. Traverse the resolved target from the retained root handle. Never traverse
   the logical reparse point again.

This preserves the approved behavior where, for example,
`docs/spec/spec.md` may link to `specs/spec.md` while retaining the logical
evidence identity. External targets, incompatible allowlisted targets, and
unapproved targets remain warnings and never become evidence.

Windows symbolic links and junctions are reparse points. Stable internal links
are allowed only through the resolution-and-compatible-target rule above.
Traversal rejects any reparse point encountered in the resolved target path,
including unknown third-party tags, mount points, junctions, and symbolic links.
No reparse data is sent to a provider.

## Handle-Anchored Traversal

The Windows reader performs these operations:

1. Open the strictly resolved worktree path with `CreateFileW`, directory backup
   semantics, open-reparse-point behavior, and read/write/delete sharing.
2. Reject a root handle that is not a directory, is itself a reparse point, has
   no usable file identity, is remote, or cannot return a normalized final path.
3. Exercise a directory-relative `NtCreateFile` reopen of the retained root as
   part of capability probing and require the same object identity and final
   path.
4. Before path-policy resolution, open the logical allowlisted source once with
   `CreateFileW` as a retained metadata-only identity sentinel. Follow its
   existing compatible internal link, but never read bytes through this
   absolute handle.
5. Duplicate traversal logically by retaining the root handle and opening each
   intermediate component with `NtCreateFile` relative to the current parent
   handle. Use `OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE`,
   `FILE_OPEN_REPARSE_POINT`, and `FILE_DIRECTORY_FILE`.
6. Open the leaf relative to the retained parent with the same no-reparse
   contract plus `FILE_NON_DIRECTORY_FILE` and read-data access.
7. Require the handle-anchored leaf to have the same complete identity and exact
   final path as the retained sentinel.
8. Reject non-regular/device-like leaves using handle attributes and standard
   information before reading.
9. Read at most `MAX_EVIDENCE_ITEM_BYTES + 1` through the retained leaf handle.

An intermediate directory rename or replacement after its handle is retained
cannot redirect the subsequent child open. A reparse point introduced before a
component is retained is rejected by the native open. The absolute leaf handle
is an identity sentinel only; all evidence bytes still come from the
handle-anchored traversal.

## Identity And Change Detection

One Windows identity snapshot contains:

- volume serial number and 128-bit file ID from `FILE_ID_INFO`;
- end-of-file size from `FILE_STANDARD_INFO`;
- creation, last-write, and change times plus attributes from
  `FILE_BASIC_INFO`.

The reader compares:

1. the absolute sentinel identity and exact final path with the first
   handle-anchored leaf open;
2. leaf identity immediately before and after the bounded read;
3. the original leaf identity with a fresh no-reparse reopen relative to the
   retained parent;
4. root object identity and final path with a fresh open of the resolved
   worktree path before closing the retained root. Root content timestamps are
   intentionally excluded so retained-parent traversal survives child entry
   changes.

Identity, size, timestamp, type, or path changes return
`REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION`. Capability/API failures return
`REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE`. Optional absent or unreadable
allowlisted files keep their existing warning behavior.
Absence is optional only before the first retained leaf open. Disappearance,
type change, reparse replacement, or reopen failure after a successful read is
a repository-change error.

The existing repository probe still runs before and after all reads and the
active repository binding is still rechecked after bundle construction.

## Capability And Action Projection

Capability is project-specific:

- Projects without a repository remain capable because they collect Project
  metadata only.
- Attached repositories require the selected platform reader and a successful
  root capability probe.

`VisionInputService` exposes the provider-free capability result.
`AgileForgeApplication` uses that same result for execution and transport
projection.

When capability is unavailable:

- the API position action remains visible only as `availability: "locked"`
  with reason code `REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE` and no enabled
  UI mutation;
- `workflow next` omits the executable bootstrap command and reports the same
  blocked capability reason separately;
- direct API or CLI bootstrap returns the same closed workflow error;
- input building and provider execution do not start.

Supported Windows repositories continue to advertise and execute the same
`vision.bootstrap` action used by Linux and macOS.

Project or repository-binding errors raised while deriving capability remain
their existing closed workflow errors in direct execution, API action
projection, and CLI blocked-command projection; they never escape as transport
exceptions.

## Provider And Data Boundary

Capability checks, repository probes, path resolution, handle opens, file reads,
and contract parsing are host-only and provider-free. A failed preflight never
calls the recipe runner or provider. Tests use only temporary synthetic Git
repositories and deterministic fake runners.

No protected profile, real repository evidence, credentials, repository
remotes, or provider data is used by this change.

## Testing

Tests are written before production changes and demonstrate their red failure.

Platform-neutral coverage:

- reader selection and capability result mapping;
- new workflow error mapping;
- locked API action projection;
- omitted CLI executable plus explicit blocked reason;
- identical API/CLI mutation failure;
- zero input-builder, runner, and provider calls after capability failure;
- unchanged POSIX evidence suite.

Windows-only coverage on a real GitHub-hosted Windows runner:

- ordinary repository evidence succeeds;
- external junction/reparse escape is rejected without reading outside bytes;
- a leaf replaced after resolution is never followed;
- a leaf deleted or changed to a reparse point after reading fails as changed;
- an intermediate directory replaced after its handle is retained cannot
  redirect the read;
- modification during a bounded read returns the changed-during-collection
  error;
- a stable internal compatible link works when link creation is supported;
- missing native capability, remote worktrees, and unsupported filesystem
  behavior fail with the capability-unavailable code;
- no provider call occurs on preflight failure.

GitHub Actions gains a required `windows-vision-evidence` job on
`windows-latest`, installs uv `0.12.8` and Python `3.13.15`, and runs the focused
provider-free Windows and projection suites. Linux retains the canonical full
gate and macOS retains its launcher smoke job.

Local macOS verification cannot prove Windows runtime behavior. Completion must
report Windows execution as pending until the new job runs on GitHub Actions.

## Documentation Sources

- [Python `os` platform capability sets](https://github.com/python/cpython/blob/v3.13.9/Doc/library/os.rst)
- [Microsoft `OBJECT_ATTRIBUTES`](https://learn.microsoft.com/en-us/windows/win32/api/ntdef/ns-ntdef-_object_attributes)
- [Microsoft `NtCreateFile`](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/nf-ntifs-ntcreatefile)
- [Microsoft `CreateFileW`](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)
- [Microsoft `GetFileInformationByHandleEx`](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex)
- [Microsoft `FILE_REMOTE_PROTOCOL_INFO`](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_remote_protocol_info)
- [Microsoft `FILE_ID_INFO`](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info)
- [Microsoft reparse points](https://learn.microsoft.com/en-us/windows/win32/fileio/reparse-points)
- [Microsoft ReFS feature comparison](https://learn.microsoft.com/en-us/windows-server/storage/refs/refs-overview)

## Acceptance Criteria

1. Vision bootstrap safely collects ordinary repository evidence on supported
   Windows systems.
2. Windows traversal is handle-anchored and never degrades to path-only safety.
3. External or late-added reparse points cannot redirect evidence reads.
4. Stable compatible internal-link behavior matches POSIX semantics.
5. Leaf, content, and root replacement or modification is detected.
6. Unsupported native/filesystem capability has its own closed error code.
7. UI and CLI projections do not expose an executable action that preflight
   already knows cannot run.
8. API and CLI return identical capability failure semantics.
9. No provider executes on any evidence or capability preflight failure.
10. Existing Linux/macOS behavior and tests remain unchanged.
11. Required provider-free Windows CI coverage is present.
12. Focused tests, `git diff --check`, and the canonical full gate pass locally;
    Windows runtime verification is reported only after a real Windows run.
