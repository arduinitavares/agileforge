# Issue #210 Authority Contract Decision Brief

Date: 2026-08-20
Status: Direction and corrected design approved on 2026-08-21
Branch: `dev/context-grounded-vision-bootstrap`
Audited baseline: `916e9ff55bdcd39b2a28c197c13c91b35d141b15`

## Decision Summary

Issue #210 is confirmed. The accepted `to-spec` Specification is already
sufficient to govern delivery. No migration or compatibility requirement
exists for this fake acceptance project.

The selected direction removes Authority compilation, the duplicate Authority
artifact, and the separate Authority review. Exact Specification acceptance
directly exposes Backlog generation without making a provider call. The
corrected proposed contract is in
`docs/superpowers/specs/2026-08-21-accepted-specification-delivery-contract-design.md`
and ADR 0005.

Production implementation must not begin until that design passes independent
preimplementation review and receives explicit human approval.

## Audit Verdict

The required checkout was on the required branch and exact baseline. The only
pre-existing working-tree difference was the expected untracked handoff:

`docs/feedback/2026-08-20-issue-210-authority-ir-validation-handoff.md`

The protected profile and String Calculator repository were inspected
read-only. No provider call, UI process, profile transfer, compile click, push,
merge, issue mutation, or master-branch mutation occurred.

Attempt 30 was reproduced provider-free through the production
`normalize_compiler_output(...)` boundary using the exact artifacts:

| Artifact | Bytes | SHA-256 | Result |
| --- | ---: | --- | --- |
| Outer trace request envelope | 10,154 | `5e18990c5e304782e14b18b3d119bf49e70a310659ad3c3d665ee4394a3457eb` | Carries project/model metadata and compiler input |
| Nested `compiler_input` | 9,943 | `111a61e61d5bdeb801e510a9defe41158632338512d617d108835c076a3b7467` | Carries the complete Authority input |
| Nested `authority_input` | 9,728 | `34bbc82966ce3bd05123e039667c7ee2fa40b9b9a4a30dff42877fbb57ee9a19` | Contains 22 eligible items |
| Initial output | 8,749 | `88f091dc2cde24bd0113d954018cefd8a0b8f2e99eea1bc39d04dfadaa81a1c6` | `INELIGIBLE_INVARIANT_SOURCE` |
| Repaired output | 8,721 | `4670cc02da585c64b140d017b0387241dba0a58856a4677621fa46c066ab7594` | `INELIGIBLE_INVARIANT_SOURCE` |

The initial and repaired outputs each contain 13 invariants, nine explicit
gaps, and 13 source-map entries. All 13 invariants fail the current validation
contract. The first reported failure is not the complete defect set.

| Source | Emitted type | Complete-input audit |
| --- | --- | --- |
| `DATA.001` | `DATA_CONTRACT` | Exact values; the lexical gate rejects a grammar contract. |
| `DATA.002` | `DATA_CONTRACT` | Exact values; the lexical gate rejects a token contract. |
| `INTERFACE.001` | `DATA_CONTRACT` | A callable signature is not a persisted or exchanged data shape. |
| `INTERFACE.002` | `ROUTE_CONTRACT` | A CLI command is not a web route. |
| `REQ.001` | `DATA_CONTRACT` | Public operation behavior is not a data contract. |
| `REQ.002` | `STATE_TRANSITION` | Parameters split or paraphrase statement and acceptance text. |
| `REQ.003` | `DATA_CONTRACT` | Parameters combine phrases from separate source fields. |
| `REQ.004` | `DATA_CONTRACT` | Parameters borrow or combine semantics outside one source field. |
| `REQ.005` | `STATE_TRANSITION` | Error behavior is not a lifecycle transition and is paraphrased. |
| `REQ.006` | `DATA_CONTRACT` | Exact diagnostic equality, order, and duplicates are not a data shape. |
| `REQ.007` | `ROUTE_CONTRACT` | CLI delegation parameters are not grounded in one source field. |
| `REQ.008` | `ROUTE_CONTRACT` | Stdout, stderr, status, and newline behavior are not routing. |
| `REQ.009` | `ROUTE_CONTRACT` | Command rejection I/O behavior is not routing. |

The nine gaps cover `CONSTRAINT.001`, `CONSTRAINT.002`, `QUALITY.001`, and
`REQ.010` through `REQ.015`. Supported product behavior must not be relabeled as
a generic gap merely to pass normalization.

## Proven Root Cause

Three defects interact:

1. The v3 invariant algebra has no faithful shapes for grammar, callable,
   command, process I/O, exact diagnostics, exit status, or trailing-newline
   behavior.
2. `_INVARIANT_TYPE_CUES` uses English substrings as semantic authorization.
   It rejects exact grounded values and cannot prove that a model-selected
   invariant family is faithful.
3. Validation raises on the first source failure and returns an opaque string.
   The sole bounded #209 repair call cannot see or correct the remaining
   independent violations.

Removing cue words alone is unsafe. It would still accept type distortions such
as CLI behavior encoded as `ROUTE_CONTRACT`. Adding String Calculator vocabulary
would preserve the same defective contract.

## Chosen Direction

The exact human-accepted Specification Version is the delivery contract.
Backlog, Roadmap, Story, Sprint, Task, validation, and packets use its exact
version/hash and stable item identities directly.

The host proves human-decision binding, canonical bytes, ownership, lineage,
item-reference integrity, and lifecycle freshness. It does not infer executable
types or semantic relevance from prose. Mandatory human reviews remain final.

The direct-Specification design also defines:

- stable host-minted Backlog item identity rather than requirement prose as a
  parent key;
- immutable Story candidates whose operational rows appear only on exact human
  acceptance, so feedback cannot overwrite accepted planning state;
- immutable Sprint-plan candidates whose Tasks appear only on plan acceptance;
- exact Specification evidence sets through Backlog, Story, and Task;
- one optional bounded semantic Story review whose findings cite allowed parent
  items;
- snapshot isolation for already active Sprints;
- early rejection of every old Authority-bearing database;
- one atomic hard-break cutover with no migration or dual reader.

## Rejected Alternatives

### Store each whole rule as text in Authority

Readable and lossless, but still a duplicate of the already readable accepted
Specification. It adds another identity and review without a proven consumer.

### Expand a specialized behavioral algebra

Could represent String Calculator behavior, but would create a cross-industry
taxonomy maintenance burden and keep a provider reinterpretation as the gate to
delivery.

### Add cue words or a compatibility table

Treats the symptom. The closed algebra still misrepresents open-ended product
behavior.

### Add another delivery-activation decision

Unnecessary. Specification acceptance already targets immutable bytes, while
Backlog generation remains a separate explicit human action.

## Main Caveat

Direct Specification removes a false guarantee. AgileForge can prove identity,
bytes, references, and freshness; it cannot deterministically prove that
arbitrary English product rules are semantically satisfied. Optional grounded
model review and mandatory human reviews carry that responsibility.

If a future concrete tool needs executable grammar, protocol, or policy data,
add the smallest explicit human-reviewed field to the Specification for that
consumer. Do not create a universal provider-compiled industry taxonomy.

## Protected State Comparison

Profile: `manual-string-calculator-209-916e9ff`

| State | Value |
| --- | --- |
| Business database file SHA-256 | `06451db4f683e0c12926457ee5d036920babe8ca7d4d02ca04cd2582259fcb78` |
| Trace database file SHA-256 | `1f7380fcf578cf01b480cade14020bc13f1b9d1c6e16c3172880b7fdb2226afc` |
| Profile JSON file SHA-256 | `a03bd4770baa65230115039a21c0fab3b04a426685a63160fafd9e47705be422` |
| Business logical digest | `9d32f30c9bf11ec83fac6894c960a36bf417a8de6176e724e0caecdd749ed728` |
| Trace logical digest | `405a86882139e0fe98cc19aefd61babee54907924131d5396df8fd78129d740a` |
| Business integrity / foreign keys | `ok` / `0` violations |
| Trace integrity / foreign keys | `ok` / `0` violations |
| Business tables / rows | `53` / `161` |
| Trace tables / rows | `5` / `137` |

String Calculator repository:

| State | Value |
| --- | --- |
| Branch | `dev/string-calculator-v1` |
| Commit | `1594e28671d247d711558a66495df1a53d1d2d1b` |
| Tree | `11534d6979e3fdf9654e741a699004205a8fb415` |
| Worktree | clean |

End-of-audit read-only verification matched the checkpoint exactly:

- the three protected file SHA-256 values were unchanged;
- both databases returned `integrity_check = ok` and zero foreign-key rows;
- business counts remained sources `1`, candidates `2`, decisions `2`, registry
  `1`, compiled Authority `0`, acceptance `0`, node attempts `30`, and attempt
  outcomes `30`;
- trace counts remained events `104` and sessions `30`;
- no process was listening on TCP port `52337`;
- the String Calculator branch, commit, tree, and clean-worktree state were
  unchanged;
- the AgileForge branch and HEAD remained the audited branch and baseline.

These values must match again before any later profile transfer or paid manual
acceptance attempt.
