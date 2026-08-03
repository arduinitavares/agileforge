# Task 18 Report: Prepare Operator-Led Acceptance Checklist

## Verdict

PASS. The Operator-run acceptance package is implemented. Acceptance execution
has not started: caRtola, ASA, and MyFinance remain `not_run`.

## Scope

Starting HEAD:
`cb3e32c4144866e81bf367f073984905abce77e9`

Task 18 changed only:

- `docs/testing/workflow-graph-acceptance-checklist.md`
- `tests/test_workflow_acceptance_document.py`
- `README.md`
- `.superpowers/sdd/task-18-report.md`

No command inspected deeply, edited, branched, created a worktree in, or mutated
caRtola, ASA, MyFinance, or another external repository. No AgileForge
acceptance command or provider-backed action ran.

## TDD Evidence

### RED

The documentation-contract test was created before the checklist or README
update.

```text
uv run --frozen pytest tests/test_workflow_acceptance_document.py -q
7 failed, 5 warnings in 0.99s
```

Six failures were the expected missing-checklist `FileNotFoundError`. The seventh
was the expected missing README link assertion.

### GREEN

The first checklist implementation produced `5 passed, 2 failed`; both remaining
failures were required literal phrase wrapping/case mismatches. After correcting
the document, the focused contract passed. The final focused run was:

```text
uv run --frozen pytest tests/test_workflow_acceptance_document.py -q
7 passed, 5 warnings in 0.92s
```

The seven tests cover:

- exactly the three selected repository roots;
- the verbatim MyFinance feature statement, synthetic-only evidence, isolated
  test environment, and Operator ownership;
- exact base evidence keys plus parseable per-step command/result/guard/failure
  YAML;
- `not_run` and the Task 19 stop boundary, with no per-repository pass claim;
- current WorkflowDomain/guard/facts-only-read wording and forbidden command
  absence;
- every literal checklist `agileforge` example parsed through the live parser;
- the README checklist link.

## CLI And Runtime Evidence

Live help was checked successfully for:

```text
uv run --frozen agileforge --help
uv run --frozen agileforge project create --help
uv run --frozen agileforge workflow position --help
uv run --frozen agileforge workflow next --help
uv run --frozen agileforge authority review --help
```

The checklist uses the current Project Shell contract, WorkflowDomain
`position`/`next`, graph/fact/decision/instance guards, graph-returned commands,
and facts-only reads. It does not author task-specific mutation commands.

The fresh-schema instruction is backed by the current `agile_sqlmodel.py`
entrypoint and separate `AGILEFORGE_DB_URL` configuration. The test-only ADK
trace reset is bounded to the separately configured
`AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL`; no session deletion command was
invented.

## Full Verification

The first full gate found seven Ruff D103 errors in the new public test functions.
All 1,804 selected tests passed during that run, but the gate correctly returned
failure. Test docstrings were added and the complete gate was rerun.

```text
uv run --frozen pyrepo-check --all
Ruff: pass
annotation checks: pass
ty: pass
Bandit: 0 issues across 121,395 lines of code
pytest: 1,804 passed, 2 skipped, 2 deselected, 17 warnings in 107.91s
```

```text
git diff --check
clean
```

Warnings are the existing ADK/Pydantic, Starlette/httpx, resumability, and
network-guard warnings. They caused no failures.

## Stop Boundary And Concern

Checklist preparation is not acceptance execution. No evidence supports a pass
claim for caRtola, ASA, or MyFinance. Task 19 must not start until the Operator
returns completed evidence or a concrete acceptance failure.

An independent Task 18 review is still the next gate. This implementation worker
did not represent self-review as independent review. After that review accepts
the package, hand the checklist to the Operator and stop at the plan boundary.
