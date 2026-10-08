# Issue #230 Provider Transient Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task under the phase-2 approval recorded below. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every retained OpenRouter LLM invocation inherits bounded transient retries, durable per-try audit, and a consistent temporary-failure projection without repeating the logical action.

**Architecture:** Introduce one model factory that injects an AgileForge-owned `LiteLLMClient` implementation into ADK `LiteLlm`. Retry only one provider request at this boundary, with host-bound action context and independently committed audit records in the existing `WorkflowEvent` table. Keep business outcome recording, lineage checks, and idempotency receipts outside the retry loop.

**Tech Stack:** Python 3.13.15; `google-adk==2.2.0`, `litellm==1.78.3`, `openai==2.5.0`, `httpx==0.28.1`, `tenacity==9.1.4` in `uv.lock`; existing SQLModel/SQLite, FastAPI, CLI JSON, plain JavaScript/Node, and Linux Playwright suites. No dependency upgrade or new provider.

**Spec:** The **DESIGN** section below is the design specification for this plan; source requirements are [issue #230 and its comments](https://github.com/arduinitavares/agileforge/issues/230). Design and plan share this file at the user's explicit request.

## Global Constraints

- **Phase 2 approved:** Claude Opus 5.5 returned APPROVE WITH CHANGES; the user approved implementation with the seven required revisions incorporated here. Git remains read-only: no commit, branch, stash, push, PR, or GitHub comment.
- Use fresh ultra implementers, task-scoped spec/compliance and quality review, observed RED before GREEN, and a fresh whole-diff review. The latest phase-2 model instruction supersedes earlier provider-routing restrictions. All agents are explicitly configured, use scoped context, and have no descendants.
- Preserve the supplied worktree. It was clean and detached at `b3a4fb415de75e92fdfa5709986a6ba690eb6b51`, not the brief's `07654b8`. `git diff --name-only 07654b8 HEAD` contains only `AGENTS.md` and three `docs/agents/` documents; production, tests, and lockfile are identical. Recheck provenance before phase 2; do not reset Git.
- macOS: run provider-free unit tests through `uv run --frozen pytest`. Product/launcher processes are Linux-only; never invoke a bare/user-level `agileforge`. Real process and browser checks belong to isolated Linux CI.
- Use only disposable test databases and in-memory ADK sessions; never use home profiles/databases or existing `agileforge-*` Docker resources. Keep sockets disabled in provider-free Python tests.
- Retry only OpenRouter 429 and provider-confirmed 500/502/503/504. Never retry 400/401/403, other statuses, connection/timeouts without a confirmed eligible response, output/Pydantic validation, or recipe logic under this policy. No call-site retry loops or fallback provider/model.
- Preserve typing and repository-relative first-line path banners. Do not introduce the retired words `authority` or `invariant`, including comments/docstrings, in `api.py`, `cli/main.py`, `cli/workflow_commands.py`, `services/application.py`, or `services/read_projections.py`.
- Every native subagent launched for this task uses explicit `reasoning_effort="ultra"`, per the user's override. Use fresh scoped context, explicit permitted model, no descendants, and centrally enforced concurrency. The phase-1 evidence worker requested `gpt-6.1-sol`/`ultra`; the spawn response exposed its identity but no independent runtime-setting confirmation. Usage windows were not exposed; do not assume unlimited allowance.

## Review Focus

1. A long or malformed `Retry-After` must never cause an early request, unbounded wait, or loss of the temporary failure; Tasks 1 and 3 test this.
2. A lease/source change or cancellation during backoff must prevent another provider request and retain the existing safe terminal behavior; Tasks 3 and 4 test this.
3. Audit write failure must not permit an unrecorded request, another retry, or business publication; Task 2 injects start/finish write failures and Task 4 checks outcomes.
4. A temporary failure in hybrid Story validation must preserve previous validation evidence and survive the thread-pool bridge; Task 5 tests both bridge branches and preservation of prior evidence.
5. A replay, reload, concurrent duplicate, or delayed browser response must retain the right action/failure identity without a second provider loop or misleading UI; Tasks 4, 6, and 7 test these.

---

## DESIGN

### Follow-up: shutdown regression and executable schema transitions, 2026-10-08

Keep the verified exclusive fence, full rollback bundle, authenticated journal,
idempotency, live-WAL/sealed-snapshot distinction, strict unknown-schema rejection
and actionable selected-project upgrade hint. The three new findings extend only
the shutdown and migration/registry contracts. Test seams are the real ADK failure
and cleanup path, the registered-database upgrade/restore boundary with disposable
SQLite files, and the finite registry plus independently frozen release fixtures.
No launcher timeout change or provider call is allowed. The Linux process test
remains required CI evidence; native tests must reproduce its underlying failure.

**Shutdown diagnosis:** investigate authentication classification, wait lifetime
and post-failure cleanup independently. Durable terminal failure alone does not
prove command completion. Record the observed root cause and add a host-runnable
RED regression before changing the responsible production path. Preserve the
SPECIFICATION_PRODUCER_FAILED code and fixed safe message for authentication.

Independent host reproduction resolved the cause: #230's provider_retry and
provider_models imports force LiteLLM 1.78.3 to load during CLI composition.
That import synchronously fetches the external model-cost map with a 5-second
HTTP timeout. Master preserves ADK 2.2.0's lazy SDK import and makes no fetch in
the provider-free action. A synthetic 5-second fetch reproduced the exact symptom:
at 8.004 seconds the process remains alive with SPECIFICATION_PRODUCER_FAILED
already durable; natural exit is 8.179 seconds. The same master probe exits in
2.876 seconds without HTTP, and the same HEAD probe with a bundled cost-map
override exits in 3.125 seconds. Those overrides are diagnostic controls only.
No authentication retry or owned-trace cleanup hang was observed. The launcher's
output is buffered until child exit, and the test checks durability after the
deadline, so neither fact establishes a separate post-commit hang. Linux process
execution remains unverified on this host.

Restore lazy SDK initialization in provider_retry and provider_models, including
the default transport construction if it loads the SDK. Lightweight action state,
timing policy and control-plane imports must not fetch provider metadata. Resolve
SDK exception classes and SDK settings only when the physical provider path is
used, retaining ADK's existing lazy import semantics. Apply debug suppression at
that real SDK boundary. Do not force LITELLM_LOCAL_MODEL_COST_MAP globally or
change pricing metadata, authentication classification, retry budgets or shutdown
deadlines. A fresh-process behavioral regression must intercept HTTP before
imports and prove that importing CLI composition and constructing models makes
zero metadata requests; it must also exercise the provider-free authentication
outcome through the actual application seam with the fixed code/message. Keep
the existing structural routing/fail-closed and real translated transport tests.

**Executable transitions:** each SchemaTransition owns a typed migration callable
accepting the caller-owned SQLAlchemy Connection. Resolve the ordered exact-hash
path to CURRENT_SCHEMA_ID first. Under one explicit BEGIN IMMEDIATE, recheck the
source, run each callable in order and verify that step's exact output full hash
and registered structural fingerprint before continuing. Hash and structure reads
must use that same connection so uncommitted DDL is visible. Reuse the established
schema-hash serialization, preserving all registered hashes. Any failed step or
mismatch rolls back the complete chain, leaving the durable recovery journal and
pre-upgrade backup intact. Callables must not commit, open a separate connection,
or depend on the changing CURRENT manifest for their historical source/target.
The issue230 step installs only its frozen canonical provider indexes.

Production migrations no longer call ensure_business_db_ready as a dispatcher.
That bootstrap continues to create fresh databases and retains its existing
development-only PRE_RETRY handling/current readiness checks. #300 must append
one SchemaRelease (independent complete hashes and structural fingerprint), one
SchemaTransition from the previous current release with its migration callable
and exact hash pairs, then select the new CURRENT_SCHEMA_ID. It must preserve
historical entries, supply its frozen fixture evidence and tests, and must not
extend a generic latest-schema bootstrap to recognize all production releases.

**Registry proof:** every release variant must have one acyclic route to current;
all transition hash endpoints must be registered under their declared releases,
and exact source/target mappings must be unique. Every release fingerprint is
checked against a database materialized from its independent fixture, covering
both master encodings. Historical fingerprints are declared per release, without
a shared mutable fingerprint alias. Inspect all tables actually present in each
registered state; historical recognition must not filter its structures through
the latest CURRENT manifest's table list. A future table removal/rename must not
make the exact registered source unrecognizable before its migration runs. Keep
the final CURRENT equality and unknown/retired-schema rejection gates intact.
A simulated later release with a distinct
structure proves master -> issue230 -> later execution and restore, intermediate
output verification and rollback on a bad hash/callable failure.

**TDD tasks and review checkpoints:**

1. Fresh implementer: executable migration hooks and registry proofs (items 2/3).
   Files: cli/production_schema_upgrade.py, cli/production_state.py (only a shared
   connection-aware hash seam if needed), models/db.py (historical schema
   inspection only; bootstrap migration behavior unchanged),
   tests/container_runtime/test_production_schema_upgrade.py and a
   focused new registry/step test module plus frozen release fixture, operator
   docs and docs/provider-retry-audit.md. Observe RED for ordered two-step
   migration, same-transaction DDL visibility, wrong intermediate hash and rollback;
   then GREEN. Add pure-data coverage of reachability, unique/registered endpoints
   and fixture fingerprints, including invalid registry controls. Run frozen
   targeted container_runtime tests, ty, Ruff/ANN/format and diff checks. Fresh
   reviewer returns separate spec-compliance and quality verdicts before acceptance.
2. Fresh implementer after diagnosis: terminal authentication shutdown (item 1).
   Files chosen from concrete evidence: adapters/adk/provider_retry.py,
   adapters/adk/provider_models.py, adapters/adk/provider_transport.py only if the
   default transport needs lazy initialization, and a focused cold-start regression
   module plus affected provider tests. The Linux test/fixture remain unchanged
   unless concrete injection evidence requires correction. Observe host RED before
   the cause fix, then GREEN for authentication and non-authentication controls.
   Keep 8-second launcher deadline unchanged. Run affected ADK/CLI failure suites,
   collect the unchanged Linux launcher test, and obtain fresh spec/quality review.
3. Root integration and fresh independent whole-diff review. Run repo-wide ty
   (must print All checks passed!), Ruff and ANN, format all changed/new Python,
   diff check, exact CI Node command, full container_runtime plus provider
   retry/audit/transport/boundary/schema and agent-failure code paths, dedicated
   production read surfaces, and e2e/dev_runtime collection. Record all RED/GREEN
   receipts, native capability limits and preserved original changes. Leave all
   edits uncommitted, with no Git mutation or real product/provider/Docker state.

### Production profile upgrades — maintainer scope change, 2026-10-07

The provider indexes exposed a broader missing contract: production profiles and
restores accept only the current structural schema, while their manifest hashes
every SQLite schema row, including indexes. An older profile cannot reach the
existing exact-baseline migrations. The approved follow-up now adds one general
production upgrade mechanism; issue #289/PR #300 will register its next state on
that mechanism. The already reviewed cancellation audit, canonical index DDL,
indexed queries and ordered deletion test remain unchanged.

**Chosen interface:** explicit `production upgrade --profile default`. Normal
serve/CLI/info continue to hold shared runtime fences; they never perform schema
maintenance. A registered older profile fails with an actionable message naming
`production upgrade --profile default` and a Compose invocation requiring the
selected project explicitly. A pending upgrade names the same recovery command.
Backup remains able to capture a fully validated
registered prior profile, so operators have an independent rollback option.
Automatic upgrade was considered and rejected: explicit maintenance makes the
backup, schema-write downtime and downgrade boundary visible and avoids changing
shared-fence concurrency for already-current readers.

**Registry and extension point:** keep a small append-only registry of reviewed
schema states with immutable identity, complete `database_schema_sha256` and the
expected structural fingerprint. The frozen master pre-index state reproduced by
the issue-260 and issue-230 fixtures is registered (master b3a4fb4; complete hash
begins `3f1d81bb`). The index state is the new current target (hash begins
`f9bd5c17`). Direct construction also exposed two exact DDL encodings: frozen
fixtures omit SQLAlchemy's trailing spaces on table lines, whereas a freshly
created master database retains them. Register both finite variants explicitly:
frozen `3f1d81bbdec3c5f1f699877cc82f154c0b8e4fb1b2c3dd4824e51603098d0a88`
→ `f9bd5c17713d15174727cd79e235a1dd62a7e11bac5392c3fbdeb9bf85a339c2`, and raw
master `d257a8ce162334434d2adc069228ef004db1eb04978db2543a410b7be4a8f29c`
→ fresh `66df69cc4eece12fe24d9645558682134454f1c69060f7d09966af3bb755fd64`.
Tests must independently construct the raw master schema as well as the frozen
fixtures. Do not normalize arbitrary DDL whitespace or admit unregistered hashes.
An entry's hash identifies the entire schema, not an exemption for
all nonunique indexes. Migration dispatch follows registered executable steps
inside BEGIN IMMEDIATE, verifying each exact intermediate hash and structure and
the final CURRENT_BUSINESS_SCHEMA_MANIFEST. Future changes append a state and its
transition callable, preserve prior identities and register
the previous current state as a source. A behavioral guard builds the current
schema and upgrades frozen predecessors; a changed current structural/full hash
without a new registered state fails. Do not infer the expected target hash from
the database being upgraded.

**Upgrade algorithm:** under the existing exclusive runtime fence, validate
ownership, paths, startup/model markers, model hash, aliases and relocations.
Load with `validate_current_schema=False`, requiring the recorded full schema
hash to equal the file hash before any normal upgrade. Require an exact registered
source. Capture and verify a pre-upgrade bundle using existing backup machinery,
in a private deployment-owned upgrade-backup directory outside the profile, then
durably publish an upgrade journal identifying the profile/state, source manifest
digest, source/target schema hashes and verified backup/receipt. Run the reviewed
migration transaction, verify exact current structural and complete hash, and
atomically republish the production manifest through `_publish_manifest`, changing
only `business_schema_sha256`. Retain a completed journal and rollback bundle as
upgrade evidence. An already-current second upgrade performs no DDL, no new
backup and no manifest rewrite.

**Crash recovery:** only the explicit upgrade command may resume a durable
journal. Revalidate its owned paths, source manifest, profile identity and backup
evidence. The only accepted database hashes are the journal's registered source
and exact registered current target. If migration committed but manifest still
records the source, reverify the full target and republish. If publication already
completed, verify the exact resulting manifest and finish the journal. Interrupted
SQLite migration rolls back or is rejected; partial/unknown states, unrelated
indexes, column drift and user_version drift are never normalized. Public
`load_production_state` keeps its strict default checks; any recovery-only hash
allowance is bounded to a verified upgrade journal and registered source/target.

**Model-configuration operations:** applying/recovering markers still require
explicit `recover-models` before upgrading. Fully verified complete/recovered
markers belong to the pre-upgrade schema's rollback pair. The explicit upgrade
archives their exact bytes in the verified rollback evidence and retires their
active startup marker through the durable journal, rather than rewriting saved
receipts or accepting arbitrary manifest changes. Existing saved model-operation
files remain untouched. After a schema upgrade, rollback across that schema
boundary uses the full pre-upgrade bundle with the prior image; old model-only
recovery cannot restore an obsolete schema manifest. Journal recovery covers a
crash during marker retirement. Document this consequence in the operator guide.

**Restore:** first verify the bundle/provenance unchanged, including its recorded
schema hash. Accept exact registered older states as well as the exact current
state. Migrate only the installed restored database under exclusive fences,
verify current structure and expected complete hash, then finalize the rebased
manifest. Never mutate the bundle. The retained source bundle is the rollback
point; restored state keeps its source-backup provenance.

**Development:** dev profiles hash schema source files rather than SQLite schema
rows. Index installation does not cause the production-manifest problem there.
Keep existing source-drift rejection and the checkout-local launcher contract.
Audit all direct and indirect business_schema_sha256 verification paths, including
API readiness, product CLI and production model configuration.

**Live databases and sealed snapshots:** final independent review reproduced two
WAL gaps in the rollback contract. Live repository discovery must read committed
WAL bindings, matching the SQLite backup API's snapshot, so the existing complete
capture and fence checks cannot be bypassed. Live and installed schema readers
also remain WAL-aware. Complete-hash and structural readers accept an explicit
immutable mode only at an already verified, sealed bundle boundary; source
provenance, rollback evidence and restore preflight use that mode to avoid adding
sidecar files or changing bundle inventory. Close owned hash connections
deterministically. Do not checkpoint or rewrite a sealed bundle to repair it.
SQLite's [URI rules](https://www.sqlite.org/uri.html) and
[read-only WAL rules](https://www.sqlite.org/wal.html#read_only_databases) support
this distinction; disposable reproductions establish the worktree failures.
All executable upgrade examples must select the same Compose project explicitly.
Startup and pending-journal hints also require the selected project. Because a
runtime hint cannot discover its caller's Compose namespace, explain how to set
`project_name` and use a guarded shell expansion that refuses unset/empty values
before invoking Docker. Test the projected hint and fake command execution;
never operate a Docker resource to verify it.

**Provider-free test seams:** runtime command with fake serve/CLI, strict profile
load, backup/provenance verification, restore, migration/journal publication fault
injection, registered schema construction and real runtime fences. Use disposable
databases and the independently frozen master fixtures. No LLM calls, real home
profiles, Docker resources or native Linux product processes on macOS.

#### Follow-up TDD tasks (execute before final review)

1. **Registry and explicit profile upgrade.** Files: new
   `cli/production_schema_upgrade.py`, `cli/container_runtime.py`, minimal
   `cli/production_state.py`/`cli/production_model_config.py` seams if required,
   `cli/state_transfer.py` for shared side-effect-controlled read-only schema
   inspection without changing bundle verification,
   `tests/container_runtime/test_provider_index_manifest.py` (rework the interrupted
   partial test), focused schema-upgrade tests. Observe RED on frozen-master
   profile → explicit upgrade → strict info/fake serve/fake CLI → backup
   verification. Add vertical RED/GREEN slices for actionable startup errors,
   exact registry guard, second-upgrade no-op, durable journal recovery around
   migration/manifest/marker publication, unknown states and genuine drift.
   Preserve existing `test_database_schema_drift_is_rejected` unchanged. Include
   a clean-interpreter regression without AGILEFORGE_DB_URL, verifying scoped
   import/environment/cache restoration. Explicit model recovery on a registered
   prior schema must finish before upgrade; model recovery must refuse a pending
   schema journal before it writes. Run
   `uv run --frozen pytest tests/container_runtime/test_provider_index_manifest.py
   tests/container_runtime/test_production_state.py
   tests/container_runtime/test_model_config_update.py -q`, plus any new focused
   test file. Fresh spec-compliance and quality review before Task 2.
2. **Registered restore and operator contract.** Files:
   `cli/container_runtime.py`, `scripts/container.py`,
   `tests/container_runtime/test_container_transport.py`,
   `tests/container_runtime/test_provider_index_manifest.py`
   or a focused restore test, `docs/linux-containers.md`,
   `docs/provider-retry-audit.md`. Observe RED for an independently frozen
   master-era bundle → restore → current strict load → backup verification.
   Preserve bundle provenance checks and current restore identity. Add `upgrade`
   to the checkout controller's explicit maintenance-command allowlist with a
   fake-transport RED/GREEN test; the direct Compose command remains the public
   operator example. Document
   rebuild image → stop → upgrade → up, backup/journal location, crash recovery,
   downgrade with prior image/full rollback bundle, terminal model-operation
   handling and the small registry extension needed by #300. Run
   `uv run --frozen pytest tests/container_runtime/test_provider_index_manifest.py
   tests/container_runtime/test_container_runtime.py
   tests/container_runtime/test_state_transfer.py -q`. Fresh spec/quality review.
3. **Resolve final-review WAL rollback defects.** Files:
   `cli/production_state.py`, `cli/production_schema_upgrade.py`,
   `cli/container_runtime.py`, `cli/state_transfer.py`,
   focused tests in `tests/container_runtime/`, `docs/provider-retry-audit.md`.
   Observe RED before source edits: WAL-mode prior profile upgrade and interrupted
   journal recovery must retain a byte-identical verified rollback bundle; restore
   from a WAL-mode master bundle must leave that bundle unchanged and create no
   sidecars. A committed WAL-only active repository binding must be discovered,
   fully captured for a fenced synthetic repository, and rejected before schema
   writes if outside the fence or explicitly omitted. Preserve live-WAL schema
   visibility and all genuine drift checks; no changes to backup provenance or
   inventory validation. Fix the short audit guide's Compose project selector
   without prose/regex tests. The runtime startup/recovery hint is another
   operator callsite: observe RED for project selection in projected errors and
   fake command execution with selected versus unset/empty project names. Run
   `uv run --frozen pytest tests/container_runtime/test_production_schema_upgrade.py
   tests/container_runtime/test_registered_schema_restore.py
   tests/container_runtime/test_state_transfer.py
   tests/container_runtime/test_schema_upgrade_review_regressions.py -q`, plus the
   new focused regression file. Fresh implementer, spec/quality review, then a
   fresh final whole-diff fix review. Findings and observed RED/GREEN receipts
   stay in the execution ledger.
4. **Final independent whole-diff review and verification.** Verify all new work
   together while preserving the previously reviewed follow-up. Run repo-wide
   ty (must print `All checks passed!`), ruff and ANN, changed-Python format,
   git diff --check, exact CI Node command, affected container_runtime (including
   production_state/state_transfer), provider retry/audit/transport/boundary,
   schema, db_tools, API deletion and production_read_surfaces suites. Collect
   all tests/e2e and tests/dev_runtime; report Linux-only process execution as
   unavailable on this macOS host. Use fresh ultra implementers per task,
   independent spec/quality reviewers between tasks and fresh final whole-change
   review. No commits, pushes or external comments; retain changes uncommitted.

### 1. Verified current behavior and root cause

Evidence is at the actual SHA above and applies to the brief's SHA because the runtime source is unchanged:

| Boundary | Current evidence and consequence |
| --- | --- |
| Model construction | `adapters/adk/agents/vision.py:94-101`, `backlog.py:25`, `story.py:31-41`, `specification_author.py:53-58`, and `spec_validator.py:16-25` construct raw `LiteLlm` separately. Product Goal, Roadmap, and Sprint do the same. There is no application-owned common model factory. |
| Common transport seam | Installed ADK 2.2.0 `google/adk/models/lite_llm.py:588-612` defines injectable `LiteLLMClient.acompletion`; `LiteLlm.llm_client` is a model field at 2283. Constructor removes `llm_client` from forwarded request arguments at 2302. Both non-streaming (2566) and streaming (2456) use the async client. |
| Recipe execution | `adapters/adk/recipes.py:674-708` wraps leaf execution **and output validation** in `RetryConfig`. Generic settings are `timeout_seconds=120, max_attempts=2` (`services/application.py:1760-1764`); Vision uses one recipe attempt plus one semantic repair (`1771-1802`), with timeout/lease 600/660 seconds (`utils/runtime_controls.py:9-10`). Generic lease is 300 seconds. These are not a shared status-aware transport policy. |
| Durable action | `adapters/adk/runner.py:261-328` records one `StartNodeAttempt`, reads persisted input, runs one recipe, then records completion/failure. Replay exits before provider execution. Keys retain the original key and existing `:completion`/`:failure` suffixes. `models/workflow.py:634-723` stores one leased Node Attempt and one terminal outcome. |
| Error collapse | `adapters/adk/runner.py:500-509` records `ADK_EXECUTION_FAILED` and returns `EXTERNAL_EXECUTION_FAILED` with `ADK recipe execution or output validation failed.` Specification has a separate generic `SPECIFICATION_PRODUCER_FAILED` branch at 492-499. |
| Replay collapse | `workflow/domain.py:1064-1086` retains only an allowlist of precise failure codes. It writes the final error into the **original start receipt** at 1093-1098/1623-1643. New provider failures need both first-response and stored-replay handling. `services/node_attempt_replay.py:333-339` reuses the stored failed result. |
| API / CLI | `api.py:540-560` maps typed failures to HTTP 409 except project-not-found; `cli/main.py:2530-2532` prints the result and exits 1. |
| UI reload | `services/read_projections.py:168-176,2528-2548` recognizes only selected Vision failure codes. `frontend/project.js:824-826` renders the escaped last-failure message; 5181-5191 retains response code/message but throws away other result metadata. |
| Other LLM paths | `tools/spec_tools.py:40-51` invokes the retained hybrid Spec Validator through `utils/adk_runner.py:181-224`, outside `AdkWorkflowRunner`. Story and Roadmap helpers use the same utility (`services/story_runtime.py:137-153`, `services/roadmap_runtime.py:103-109`). The utility wraps failures in `AgentInvocationError`; Story can describe them as schema failures, Roadmap writes failure artifacts, and hybrid validators catch every exception as `STORY_SPECIFICATION_REVIEW_INVALID` (`services/specs/story_validation_service.py:827-832,884-889`). |

**Provider-free reproduction performed:** an external throwaway script used the real Backlog `LlmAgent`/`LiteLlm`, patched the ADK module's completion function with typed synthetic LiteLLM failures, blocked sockets, injected the existing test engine/session fixtures, and exercised the real recipe/domain runner. It filtered attempts/outcomes to `backlog.generate`; seeded parent attempts were excluded. It made no real request and used no real trace database.

```text
HTTP 429, recipe max_attempts=1 -> transport_calls=1, EXTERNAL_EXECUTION_FAILED,
  ADK_EXECUTION_FAILED, 1 logical attempt, 1 terminal outcome, 0 artifacts
HTTP 500, recipe max_attempts=1 -> same result
HTTP 400, recipe max_attempts=1 -> same result
HTTP 429, recipe max_attempts=2 -> same result, transport_calls=1
HTTP 400, recipe max_attempts=2 -> same result, transport_calls=1
message: ADK recipe execution or output validation failed.
```

The probe demonstrates this execution path, not every dependency transport branch. Initial probe setup was corrected to initialize ADK's lazy LiteLLM import **before** patching and to filter Node Attempt rows; only the corrected output above is evidence. Script/output are outside Git at `/var/folders/fh/0bmky89j2d54xdptjs_1mrjm0000gn/T/issue230-repro.qNdcNjlP8V/{repro.py,output.log}`. Re-run from the repository with `PYTHONPATH="$PWD" uv run --frozen python <that-script>`; the script itself sets synthetic configuration and blocks sockets.

Existing checks also passed: `uv run --frozen pytest tests/adapters/test_adk_workflow_runner.py::test_provider_sdk_failure_records_failure_and_returns_external_error tests/adapters/test_adk_workflow_runner.py::test_output_validation_failure_records_failure_without_business_fact -q` → **2 passed**. This is diagnosis evidence, not feature verification.

### 2. Dependency retry support, pinned-version limits, and alternatives

The `find-docs` workflow was applied using the available Context7 connector: resolve library IDs, then query `/berriai/litellm` and `/google/adk-python`. Context7 did **not** index the exact pinned versions. Latest snippets were used only as discovery pointers; installed distributions from the frozen lock were inspected to establish actual signatures and behavior. CLI execution outside the sandbox was unavailable, so the connector supplied the same resolve/query procedure without an installation or permission change.

- **ADK 2.2.0:** `LiteLlm` accepts an injected `llm_client` and completion kwargs. [Pinned ADK model source](https://github.com/google/adk-python/blob/v2.2.0/src/google/adk/models/lite_llm.py) supports the injection seam. [Pinned `RetryConfig`](https://raw.githubusercontent.com/google/adk-python/v2.2.0/src/google/adk/workflow/_retry_config.py) exposes max attempts, delay, factor, jitter, and exception-name filtering. Its default exception filter covers all exceptions; it operates on nodes, not provider responses, and has no `Retry-After` field.
- **LiteLLM 1.78.3:** installed `litellm/utils.py:875-895,1582-1628` supports `num_retries`/retry policy and invokes `acompletion_with_retries`; `litellm/main.py:3738-3800` uses Tenacity, including an exponential strategy. That helper does not supply this issue's narrow classification, per-try durable action audit, or shared projections. The [official SDK reliability documentation](https://docs.litellm.ai/docs/completion/reliable_completions) confirms retry support generally; its current examples are not proof of the pinned async path.
- **Transport nuance:** pinned OpenRouter uses LiteLLM's HTTP handler (`litellm/main.py:2660-2722`), rather than assuming every request is sent by the OpenAI SDK. OpenAI 2.5.0's default two retries therefore cannot be treated as proof of OpenRouter behavior. A provider-free HTTP-handler test must count actual sends through the pinned OpenRouter translation, including status/header preservation and any parameter-repair branch.
- [OpenRouter error documentation](https://openrouter.ai/docs/api_reference/errors-and-debugging#retry-after-header) documents retry guidance. [RFC 9110 §10.2.3](https://www.rfc-editor.org/rfc/rfc9110.html#name-retry-after) permits delta-seconds or an HTTP date. Current provider documentation also describes other statuses; **402 is outside this issue's allowlist even if it has `Retry-After`**.

| Design | Evaluation against acceptance criteria |
| --- | --- |
| **Chosen: shared factory + injected client retry policy** | Owns exact status classification, fake clock/transport, per-try audit, and typed exhaustion before ADK validation. Covers graph, repair, helper, and hybrid calls. Requires explicit action-context plumbing and factory adoption in all retained agents. |
| LiteLLM `num_retries` / Router only | Less local loop code, but pinned helpers are broader, use different attempt semantics, and do not guarantee one durable record per actual send. Router adds deployment/fallback machinery that this task does not need. Would still need audit/projection integration and custom policy. |
| ADK `RetryConfig` or retrying `_run_recipe` | Convenient hooks, but re-executes recipe/validation, can multiply calls, misses helper/hybrid invocations, and cannot classify `Retry-After` at the HTTP seam. Rejected. |

### 3. Shared boundary and classification contract

Add `adapters/adk/provider_models.py::create_openrouter_model(*, model_id: str, **completion_kwargs: object) -> LiteLlm`. It preserves current model ID, token limits, timeout, `drop_params`, and OpenRouter routing/body settings while injecting `RetryingOpenRouterClient` from `adapters/adk/provider_retry.py`. All eight retained agent modules use it, including both Story builders, Vision repair, and Spec Validator. Never globally monkeypatch LiteLLM, and never choose a replacement provider/model.

Structural enforcement discovers production Python packages and top-level modules from `pyproject.toml` packaging declarations, including tools and future declared packages/modules. Only the exact three boundary-owner paths are excluded. Behavioral disposable-tree tests inject prohibited calls/models/client replacements and verify offending paths, with clean and approved-owner controls; no hard-coded file counts or source/prose matching. Task 7 review found that the initial manually listed roots omitted packaged code, so this discovery rule closes regression enforcement without a production behavior change.

`RetryingOpenRouterClient` subclasses the pinned public `LiteLLMClient` seam and delegates to a supplied one-send completion callable. Classification happens **only around transport execution**, before ADK response conversion, callbacks, output schemas, or recipe validation. Use typed SDK/HTTP errors and structured status/provider evidence from their response/cause chain. Selected `openrouter/` model plus an OpenRouter translation error with a real status is acceptable; a generic exception containing the text `429` or `500` is not evidence. Normalize structured integer or numeric-string status codes, rejecting booleans and nonnumeric values. In pinned LiteLLM 1.78.3, OpenRouter 504 maps to `Timeout(status_code=504)`; 408/None timeouts remain non-retryable. 500/502 map to `APIError(status_code=...)`, including the issue message `OpenrouterException - The server had an error processing your request.`. HTTP 200 error bodies use `error.code`, which can be an integer or numeric string (`convert_dict_to_response.py:436-451`; `exception_mapping_utils.py:2130-2222`). Read `litellm_response_headers` as well as response headers: Timeout receives guidance through that attribute (`exception_mapping_utils.py:2272-2284`). Do not infer transient 5xx from `APIError`'s class name alone. Cyclic cause chains must terminate.

| Failure at the boundary | Retry? | Terminal projection |
| --- | --- | --- |
| OpenRouter confirmed 429 | Yes, while count/time/host checks permit | `EXTERNAL_PROVIDER_TEMPORARY`, reason `rate_limited` when exhausted |
| OpenRouter confirmed 500/502/503/504 | Yes, under identical limits | `EXTERNAL_PROVIDER_TEMPORARY`, reason `unavailable` when exhausted |
| 400/401/403, 402, 404, 408, 409, 422, 501, other statuses | No, even with retry guidance | Existing non-transient error path; no new claim of retryability |
| Connection error, timeout except confirmed OpenRouter 504, unknown provider/status, cancellation | No new retry | Existing infrastructure/cancellation handling; cancellation propagates |
| Pydantic/output validation, incomplete JSON, semantic/sentinel failure, recipe logic | No transport retry | Existing typed validation/recipe behavior |
| Audit persistence failure or stale/expired host action | No | Safe local failure/obsolescence; never present it as a provider outage |

For confirmed transient exhaustion, raise `ProviderTransientFailure(RuntimeError)` carrying only a typed sanitized `ProviderFailureSummary`. Any ADK `DynamicNodeFailError.error` wrapping must be explicitly traversed in addition to normal cause/context chains; wrapper text is never parsed. Failure translation takes precedence over Specification's generic producer mapping and helper schema mappings.

Pinned LiteLLM 1.78.3 exception_mapping_utils.py:188-196 prints feedback/debug banners to stdout unless `litellm.suppress_debug_info` is true. The shared factory configures that non-secret diagnostic setting once, with a real translated-error stdout test; never redirect process stdout per call or emit deferred progress.

Disable SDK retry/fallback multiplication at construction/request boundaries with explicit `num_retries=0`, `max_retries=0`, and no client-level fallback configuration. Pinned LiteLLM's global retry policy can override request settings (`litellm/utils.py:875-895`): validate effective controls before sending and fail closed on unsupported global retry/fallback overrides rather than resetting process globals used by another action. Preserve the generic recipe `max_attempts=2` and all existing recipe/semantic-repair budgets. Characterize real ADK execution with `ProviderTransientFailure` under max_attempts=2: total sends must equal the configured provider tries. Only if that fails, filter `RetryConfig(exceptions=...)` to exclude typed provider/audit failures without lowering the recipe budget. The pinned wire test is decisive: if these controls do not yield one HTTP send per audited try for the eligible and excluded statuses, stop and resolve that transport seam before integration. Do not claim control of OpenRouter's internal provider routing or billing.

Current retained calls use no StreamingMode and are non-streaming. Both `stream=True` and synchronous `completion` fail closed immediately before any send. Future streaming support requires a separate reviewed contract for chunk delivery, per-try audit and partial-response behavior; never silently inherit raw streaming or synchronous clients.

**Pinned hidden transport resend:** `litellm==1.78.3` installed `llms/custom_httpx/http_handler.py:315-334,546-550` resends connection/protocol failures independently of retry kwargs, and its default client follows redirects (`244-251`). Add `adapters/adk/provider_transport.py` with an owned `AsyncHTTPHandler` subtype overriding this behavior and a redirects-disabled HTTPX client/transport with retries zero and no resend-capable auth/hooks. Pass that handler through LiteLLM's typed `client` seam (`llm_http_handler.py:423-426`); preserve real OpenRouter translation/error mapping. Own and close its resources per call/try so shared models do not retain an event-loop-bound transport across `asyncio.run` calls. Wire tests must prove one send for connection/protocol failure and redirects as well as mapped statuses. Preserve OpenRouter's existing downstream routing/body behavior; no new claim about its internal provider routing/billing.

The owned handler retains same-try transport provenance. Pinned SDK translation can fabricate APIError500 for raw connection/protocol/read-timeout failures; restore the current raw connection/protocol error or noneligible Timeout408 instead of treating that synthetic status as a provider response. For actual HTTP200 errors, only valid structured integer/numeric-string `error.code` supplies an error status; malformed success/choices/schema conversion stays nonretryable and cannot inherit an earlier eligible status. Restore only parsed classification timing from the actual response's Retry-After carrier, including HTTP200 error-body translation. No raw response/exception/header data enters audit or terminal summary.

Independent Task 3 review reproduced the same synthetic500 problem for local request preparation before dispatch. An SDK error without this try's actual response must remain nonretryable, including build/preparation errors; an exception status alone cannot invent provider confirmation. A local failed preparation has zero physical sends and must not schedule a retry.

Effective wire ownership must also survive SDK overrides: reject reserved model/messages replacement in request extra_body, unsupported mutable OpenrouterConfig globals, and a non-OpenRouter endpoint inherited through OPENROUTER_API_BASE. Check immediately before dispatch; do not reset shared SDK globals. Preserve the repository's existing explicit OpenRouter provider-routing/reasoning body settings. A model/messages/global change between tries must never send under the old fingerprint or audited provider/model.

Validate effective request/global retry policy, `model_fallbacks`, fallback/routing overrides and aliases immediately before each physical try, without resetting process policy globals. Some declared fallback globals belong to routers rather than the direct path; reject unsupported controls conservatively without claiming they all actively trigger direct retries. Retain positive scalar timeout semantics: OpenRouter's pinned path collapses HTTPX timeout objects to read timeout and stock HTTPX timeout maps to synthetic408. The outer owned total-budget cap has local provenance, never fabricated provider504.

### 4. Configuration, timing, and deterministic seams

Define a frozen `ProviderRetryConfig` in `services/contracts/provider_retry.py`; parse/cache non-secret environment overrides in `utils/runtime_config.py::get_provider_retry_config() -> ProviderRetryConfig` and clear it with existing configuration caches.

| Environment knob | Default | Valid range / meaning |
| --- | --- | --- |
| `OPENROUTER_RETRY_MAX_ATTEMPTS` | `3` | Integer 1–10, includes the first request; 1 disables extra tries |
| `OPENROUTER_RETRY_BASE_DELAY_SECONDS` | `1.0` | Finite float >0 and ≤60 |
| `OPENROUTER_RETRY_MAX_DELAY_SECONDS` | `8.0` | Finite float ≥base delay and ≤60; caps computed backoff only |
| `OPENROUTER_RETRY_MAX_ELAPSED_SECONDS` | `60.0` | Finite float >0 and ≤120; retry window starts at the first transient failure and includes subsequent sends/waits |
| `OPENROUTER_RETRY_MIN_REMAINING_SECONDS` | `1.0` | Finite float >0 and ≤max elapsed; minimum remaining budget needed to start a retry |

Backoff after failed try `n` (1-based) is full jitter `uniform(0, min(max_delay, base_delay * 2**(n-1)))`. Valid `Retry-After` supplies a **minimum** delay: `delay=max(jittered_backoff, retry_after)`. Header lookup is case-insensitive across the pinned response/header carriers. Accept nonnegative integer delta-seconds and valid timezone-aware HTTP dates; past dates mean zero. Malformed, negative, or non-finite non-integer values fall back to backoff. A valid huge delay is **not clipped** to the backoff cap: if it cannot fit the remaining retry/action budget, finish immediately with temporary failure and reason `retry_after_exceeds_budget`, retaining finite numeric guidance. A numeric integer above the exact JSON/JavaScript-safe bound `2**53-1` also refuses retry; its projected numeric guidance is null, rather than falling back and sending early. Bound parsing work on oversized input; numeric oversized guidance or an undecidable oversized whitespace prefix conservatively refuses retry; clearly malformed oversized input falls back. Bounded normalization must not discard permitted padding on ordinary numeric guidance. Never send earlier to satisfy the local cap.

Bind a `ProviderActionContext` through a `ContextVar`: host project/action identity, optional Node Attempt ID/fingerprint and idempotency-key digest, captured policy, action deadline/lease limit, audit port, pre-try host check. The singleton client never stores mutable per-action counters. Every model request receives a fresh `call_id`; physical tries reuse it and the immutable request fingerprint. Separate semantic repair/model calls have distinct call IDs under the same action. Reset bindings in `finally`; propagate context explicitly through the spec tool's thread-pool bridge.

Use one injected clock interface (`monotonic() -> float`, `utc_now() -> datetime`, `async sleep(seconds: float) -> None`), an injected `uniform(low: float, high: float) -> float`, and one injected async completion callable. Tests advance virtual time and use scripted responses; no real sleeps or sockets. Use wall time only for HTTP dates/audit timestamps; budgets use monotonic time.

The host's recipe deadline and captured lease remain hard bounds (120/300 seconds generally; 600/660 for Vision). Reserve 2 seconds before the recipe deadline for failure unwinding/outcome persistence. At each try, cap transport timeout by its existing setting and the remaining host/retry budget; do not extend the first request's existing timeout. Before and after every sleep, recheck count, monotonic deadline, and host validity. No retry starts when remaining budget after its delay is below the configured minimum. A first-request timeout remains non-retryable. A local timeout caused by the capped retry budget on try >=2 after an eligible transient ends as `EXTERNAL_PROVIDER_TEMPORARY`, `termination_reason=retry_budget_exhausted`; preserve the last confirmed eligible status. A provider-originated non-transient retry response still stops through its existing error path. Fake-clock tests script 500 followed by a slow retry that reaches the cap.

### 5. Durable per-try audit without SQL migration

Preserve durable attempt fingerprints as produced by `workflow/fingerprints.py::canonical_hash` (`sha256:<64 lowercase hex>`). Audit validation uses a separate strict annotation for this field, accepting existing bare-hex fixtures, while request and idempotency digests remain exactly 64 lowercase hex. Compare full values with the persisted attempt; do not trim or rewrite history.

Use existing `models/events.py::WorkflowEvent`, adding enum names `PROVIDER_TRY_STARTED` and `PROVIDER_TRY_FINISHED` in `models/enums.py`. Both names fit the current `VARCHAR(27)`. The pinned metadata has no enum CHECK constraint (`create_constraint=False`), and `models/db.py:1092-1107` records the existing columns/FKs only. **No table, column, index, constraint, or manifest rewrite is planned.** Tests must prove exact schema compatibility; if that proof fails, stop for review of a migration design.

Add `repositories/provider_attempts.py::ProviderAttemptAuditRepository` with independently committed, bounded writes and a terminal-summary cross-check. Do not reuse the ADK diagnostic helper that swallows append failures. Each actual transport try has a start record committed **before sending**, and a finish record committed before waiting, retrying, returning a response, or exposing a terminal failure. No business Session/transaction remains open across a provider wait.

Canonical allowlisted metadata (`schema_version="agileforge.provider-try.v1"`) includes:

- `action_id`, `call_id`, project ID; Node Attempt ID/fingerprint/node/instance when present; digest of an existing idempotency key, never a invented replacement key for a graph action.
- Same sanitized model ID/request fingerprint for all physical tries; try ordinal/max attempts; effective non-secret retry configuration. Fingerprint only content/generation/routing semantics; credentials and transport headers are excluded even from hash inputs.
- Start/finish UTC timestamps, duration, confirmed HTTP status or null, retry classification, disposition (`success`, `retry_scheduled`, `exhausted`, `non_retryable`, `cancelled`, `not_sent`), termination reason, selected delay and next eligible retry time when scheduled.
- No prompts, responses, exception strings, credential/header dumps, provider raw bodies, endpoint query strings, or unrelated project data. Only parsed `Retry-After` timing crosses the header boundary.

The start/finish pair is **one logical send reservation with an explicit outcome** for audit consumers, keyed by `(action_id, call_id, try_ordinal)`. Normally it represents one physical send. If the synchronous start write consumes the minimum remaining retry budget, finish that final reservation as `not_sent`: ordinal >=2, all HTTP/classification/guidance/scheduling fields null, and `termination_reason=retry_budget_exhausted`. Every preceding pair must be complete and the immediately preceding finish must be an eligible `retry_scheduled` response. No later start is permitted. Exclude this reservation from summary `attempts`; retain status and guidance from the prior actual send. A first-send interruption without prior eligible evidence stays local. Each writer is invoked once; repeated identical repository appends are idempotent under the repository's serialized write transaction, conflicting identities are rejected. There is no new SQL uniqueness constraint, so this is an application guarantee tested with independent SQLite connections. A crash can leave a started record without a finish; retain that incomplete audit evidence, never invent success or silently reissue it. User-facing activity/progress projections are deferred.

The audit terminal cross-check accepts an optional carried `expected_summary`. Revalidate it and reconstruct all its identity/count/status/guidance/key fields from the complete ordered call. A final `retry_scheduled` finish is accepted only with the host-owned `retry_budget_exhausted` reason supplied by that summary, covering oversleep or finish-write cost **before** the next reservation; without a supplied summary it remains nonterminal. Other terminal reasons still require corresponding durable exhausted evidence. Missing/incomplete/cancelled/permanent/successful evidence or any mismatch fails locally. This seam preserves immutable per-try finishes and the required temporary projection for budget stops between sends.

Start-write failure means zero sends; finish-write failure means no subsequent send and no business artifact publication. Propagate a local `ProviderAuditError`, not a temporary-provider classification. A successful provider response whose finish cannot be recorded is withheld from the recipe. Audit records survive recipe/validation failure and live in the business database, not the optional ADK trace database. Project deletion already deletes all project events; project ID is therefore mandatory for production contexts. Validate association with the host-owned action before writing; provider output cannot choose identities.

Audit events must not enter business fact/decision fingerprints, affect readiness, replace existing transition receipts, or trigger business metrics. Adding enum vocabulary changes event compatibility for old application binaries; rollback retains data, but an old reader of all enum rows may need a filtered export. This is a review risk, not an automatic database-reset proposal.

### 6. Idempotency, stale checks, and non-graph calls

R15/R16 review correction: a completed attempt replays the frozen terminal result in its original Start receipt, including successful Specification candidate output and position. Intermediate successful preflight/revalidation receipts must never replace it. Any genuinely necessary legacy reconstruction must match durable terminal evidence and preserve historical request bytes/hashes. A revalidation that itself recorded the terminal stale outcome is eligible; excluding every revalidation would break that supported legacy shape. Cover real successful Specification first/replay equality and retained-initial legacy stale replay, with zero replay sends, sleeps, or audit writes.

For graph actions, begin context only **after** `StartNodeAttempt` wins its existing duplicate/lease checks, using persisted normalized input and captured settings. Save retry policy inside `execution_settings["provider_retry"]` before starting the action so configuration participates in its identity. Preserve graph/fact/decision/input fingerprints, actor, model, existing key, and delivery lineage across tries; provider request generation arguments remain fixed, apart from bounded transport timeout. Deep-copy the frozen request before each send because SDK translation may mutate its inputs.

Expose a read-only `WorkflowDomain.check_provider_attempt(*, project_id: int, attempt_id: int, attempt_fingerprint: str) -> WorkflowError | None` that checks live lease, terminal/latest attempt, graph/business fingerprints, and persisted input using existing checks. The retry client invokes an injected host guard before every actual send. Specification also invokes its existing source re-probe/revalidator at every try, including after waiting. On a stale check, stop; use existing failure/obsolete recording to settle the action, preserving an already-recorded outcome and returning the existing stale/obsolete result. Do not introduce a new business transition for each try.

One approved action can create multiple provider audit pairs, but exactly one graph Node Attempt and one terminal outcome, and at most one downstream artifact. Concurrent duplicates do not start a second provider loop. Success/failure replay under the original key performs zero sends, sleeps, or audit writes. Configuration changes after completion cannot alter that receipt. Manual retry after exhaustion uses a new caller key and the existing currently available action; never reuse the exhausted key to trigger execution. Existing crash recovery remains at-least-once upstream, not exactly-once provider billing.

**Non-graph paths are included:** outer Story/Roadmap helpers and hybrid validation create or reuse host action context with the supplied/loaded project ID, selected engine, and a host-generated action ID. They get the same client policy and audit store, not another retry loop. Existing helper and hybrid inputs do not acquire a new user-facing idempotency field. Hybrid currently has no receipt; each explicit tool invocation remains a new action, all its internal tries share one generated action ID, and no new cross-invocation replay guarantee is claimed.

The shared utility must preserve typed temporary/audit failures before wrapping general exceptions as `AgentInvocationError`; wrappers must not convert them into schema errors or start semantic repair. On hybrid temporary exhaustion, return `success=False`, `ready_for_sprint=False`, `error.code="EXTERNAL_PROVIDER_TEMPORARY"`, a safe message, and `provider_failure`; do not overwrite previous `validation_evidence`. An audit error also takes precedence over the generic semantic-invalid catch: return the existing `EXTERNAL_EXECUTION_FAILED` code, `success=False`, and safe copy `Provider request audit could not be recorded. No new result was saved.`, without a temporary-provider summary. Prior evidence remains available for a separate currentness check, and is not reported as a fresh successful review. Structural validation stays provider-free. In-session production callers use `mode="structural"`; this issue does not add an in-session hybrid audit contract. The retained helper entry points and spec tool have no production callers but remain covered by factory/fail-closed enforcement; bind a host action context when they are explicitly invoked. Copy ContextVars into the running-loop `executor.submit(asyncio.run, ...)` bridge. Do not add caller-transaction management or a separate audit database.

Non-graph binding decisions for Task 5: bind after existing input/structural checks permit provider work. New helper contexts use the shared clock with a 120-second action deadline unless an existing tighter limit applies, no lease, and one generated action ID across permitted repairs. Reuse a same-project binding without resolving new configuration or engine; reject cross-project reuse. Non-session hybrid uses its already-selected Engine for independent audit. An unsupported Connection/None audit binding fails safely before callback/provider dispatch, preserves prior evidence, and does not add an in-session hybrid feature. Structural validation remains free of provider policy/context resolution.

### 7. Stable API / CLI / UI projection

Add `WorkflowErrorCode.EXTERNAL_PROVIDER_TEMPORARY`. Durable outcome code and transport code use the same value. Safe terminal copy is fixed:

- 429: `OpenRouter is temporarily rate-limited. Automatic retries stopped. Retry this action with a new idempotency key.`
- Eligible 5xx: `OpenRouter is temporarily unavailable. Automatic retries stopped. Retry this action with a new idempotency key.`
- Non-keyed helper/hybrid variant: replace the last sentence with `Retry this action later.`

Keep `WorkflowError`'s existing fields. Put new graph failure details in the existing `TransitionResult.output["provider_failure"]`, only for the new code:

```json
{
  "schema_version": "agileforge.provider-failure.v1",
  "provider": "openrouter",
  "category": "external_temporary",
  "retryable": true,
  "reason": "rate_limited",
  "termination_reason": "attempts_exhausted",
  "http_status": 429,
  "call_id": "host-generated-call-id",
  "attempts": 3,
  "max_attempts": 3,
  "retry_after_seconds": null,
  "manual_retry_requires_new_key": true
}
```

`reason` is `rate_limited` or `unavailable`; `termination_reason` is `attempts_exhausted`, `retry_after_exceeds_budget`, or `retry_budget_exhausted`. `attempts` is **actual sends in the exhausted call**, not recipe attempts or total action calls. `ProviderTransientFailure` carries the sanitized exhaustion summary through the runner. Cross-check it against independently committed terminal audit before settling the outcome. Add an optional internal `FailNodeAttempt.provider_failure` typed field (absent on ordinary failures); preserve serialization/fingerprints of requests without this field using Pydantic 2.12.3 `Field(exclude_if=...)` (verified in the frozen distribution and through find-docs). Domain validates the typed summary/code pairing and freezes it in the original start receipt; the runner returns that recorded result so first response and replay are identical. The domain must not depend solely on querying the audit inside its failure transaction. Missing/mismatched audit is a local audit error. Legacy receipts remain readable and gain no fabricated summary.

**API:** preserve HTTP 409, mutation request fields, envelope, and other status mappings. Expose the new code, fixed safe message and `output.provider_failure` on first response and replay. No API Retry-After or POST retry middleware.

**CLI:** preserve stdout JSON and exit 0/1. Exhaustion uses the same result envelope; success after retry exits 0. No progress output.

**UI:** retain code and `output.provider_failure` in existing frontend error conversion (`frontend/project.js:5181-5191`). Existing error rendering remains responsible for display. Vision reload adds the new code and summary to `last_failure`, with no phantom candidate. No new DOM/rendering/default-visibility changes are intended. Existing rendering will receive the new safe failure message; audit its current e2e error locators without progress-related locator churn. If implementation reveals a necessary text/DOM/visibility change, audit `tests/e2e/test_single_project_lifecycle_ui.py` locators and update affected expectations in the same change.

### 8. Contract changes and boundaries

**Will change:** eligible provider-request retries/backoff; all production model construction; fail-closed missing-context/stream/sync/bypass behavior; captured provider policy in execution-settings JSON; new error code and optional `output.provider_failure`; optional internal failure-request summary; event enum vocabulary and versioned JSON audit; helper/hybrid temporary-failure projection preserving prior evidence; Vision reload and frontend error metadata retention.

**Will not change:** SQL schema/manifest, pins/provider/model selection/routing, generic recipe max_attempts=2, semantic-repair budgets, content/output schema validation, graph/readiness/artifacts, mutation request fields/keys, original-key replay, HTTP 409, CLI stdout/exit conventions, historical non-provider errors, structural validation, or crash recovery's at-least-once upstream limitation.

### Deferred follow-up

Defer `/provider-activity`, 1s UI polling, pending-panel progress/status, other panels through activity projection, CLI stderr progress, `tests/test_provider_retry_ui.mjs` and CI/dev_checks wiring, and progress-related e2e locator changes. The orchestrator will file the follow-up issue. No related production code or tests belong to this implementation.

### Reviewer resolutions

All seven required changes are adopted. Both suggestions are adopted: streaming/sync fail closed, and exhaustion summaries travel with the typed failure and are cross-checked against audit. Confirmed decisions are 3 tries, 1–8s full jitter, 60s window, Retry-After as minimum with budget refusal, configurable minimum remaining budget (default 1s), WorkflowEvent pairs without DDL, independent audit commits without business transactions over waits, and host-generated helper action IDs.

---

## TDD implementation plan

Run Tasks 1–7 sequentially with one implementation writer. Each task requires observed RED, minimal GREEN, targeted checks, and separate spec/compliance and code-quality verdicts from a fresh task reviewer. Leave all changes uncommitted.

### Task 1: Define classification, timing policy, and configuration

**Files:** Create `services/contracts/provider_retry.py`, `adapters/adk/provider_retry.py`, `tests/adapters/test_provider_retry.py`; modify `utils/runtime_config.py`, `tests/test_model_config_env.py`.

**Interfaces:** Frozen `ProviderRetryConfig` with defaults/ranges in DESIGN §4; `ProviderFailureSummary` with fields in DESIGN §7; `TransientProviderResponse(status: int, retry_after_value: str | None)`; `RetryAfterGuidance(delay_seconds: float | None, refuses_retry: bool)`; `classify_openrouter_failure(error: BaseException, *, model_id: str) -> TransientProviderResponse | None`; `parse_retry_after(value: str | None, *, now: datetime) -> RetryAfterGuidance`; `retry_delay(*, failed_try: int, config: ProviderRetryConfig, retry_after_seconds: float | None, uniform: Callable[[float, float], float]) -> float`; `get_provider_retry_config() -> ProviderRetryConfig`. Clock Protocol exposes monotonic, utc_now, async sleep. Typed failures may be defined here for later tasks.

- [x] RED classification across 429/500/502/503/504, `APIError` with int/numeric-string status, Timeout504 with `litellm_response_headers`, excluded 400/401/403/402/408/501, Timeout408/None, generic APIError status None/nonnumeric, foreign provider, generic RuntimeError text, Pydantic and cyclic/wrapped exceptions. Assert structured values, not parsed messages.
- [x] RED timing: delta/date Retry-After minimum, malformed/negative guidance fallback, past-date zero, huge guidance refusal rather than clipping, full jitter [0,1]/[0,2]/cap [0,8], nonfinite/bool/range config rejection and cache clear.
- [x] Run `uv run --frozen pytest tests/adapters/test_provider_retry.py tests/test_model_config_env.py -q`; capture the missing-behavior failure before implementation.
- [x] GREEN implement typed contracts, classification, parser, timing/config only. No transport loop yet.
- [x] Repeat targeted command, then ty/ruff for changed code; record RED/GREEN log paths and review evidence.

### Task 2: Persist safe per-try audit records

**Files:** Create `repositories/provider_attempts.py`, `tests/adapters/test_provider_retry_audit.py`; modify `models/enums.py`; extend schema/deletion tests only as needed (`tests/workflow/test_fresh_project_schema.py`, `tests/adapters/test_api_project_deletion.py`). Never change manifest/DDL.

**Interfaces:** `ProviderTryAuditRecord`, `ProviderAuditError` and audit Protocol in the contract module; `ProviderAttemptAuditRepository(engine: Engine)` with `append_started(record)`, `append_finished(record)` and `terminal_failure(...) -> ProviderFailureSummary | None` for typed summary cross-check. Use immutable allowlisted metadata from DESIGN §5. No activity endpoint/read feature. For budget-capped try >=2, the finish may have no HTTP status while the carried summary retains the prior confirmed eligible status; cross-check all paired tries in that same call, preserving truthful per-try statuses/counts.

- [x] RED durable pair after database reopen, identical append idempotency/conflict rejection with independent connections, wrong-project/identity rejection, independent commits surviving unrelated rollback, no business fingerprint changes, safe metadata without prompts/raw headers/exception text. Reject contradictory finish facts and isolate malformed foreign-project JSON while rejecting owning-call corruption.
- [x] RED complete scheduled-call verification with carried budget summary, exact mismatch rejection, and final `not_sent` reservation excluded from actual sends; enforce its null fields/prior eligible pair/no-later-start rules and local finish-write failure.
- [x] RED start/finish write failures raise ProviderAuditError. Existing compatible schema opens unchanged; fresh schema manifest matches; project deletion removes provider events while preserving another project's rows.
- [x] Run `uv run --frozen pytest tests/adapters/test_provider_retry_audit.py tests/workflow/test_fresh_project_schema.py tests/adapters/test_api_project_deletion.py -q`; observe failure, then GREEN independent serialized writes and terminal-summary cross-check, and repeat.

### Task 3: Install the single provider boundary in every model

**Files:** Modify `adapters/adk/provider_retry.py`; create `adapters/adk/provider_models.py`, `adapters/adk/provider_transport.py`, `tests/adapters/test_provider_boundary_routes.py`, `tests/adapters/test_provider_transport_contract.py`; modify the eight `adapters/adk/agents/{vision,product_goal,specification_author,backlog,roadmap,story,sprint,spec_validator}.py` modules and `tests/dev_runtime/test_agent_failure_shutdown.py`.

**Interfaces:** `ProviderActionContext` with host project/action/optional attempt identity, policy, deadlines, audit port, guard; `bind_provider_action(context)` ContextManager; `ProviderTransientFailure(summary)`, `ProviderAttemptStopped`; `RetryingOpenRouterClient` implementing pinned LiteLLMClient; `create_openrouter_model(*, model_id: str, **completion_kwargs: object) -> LiteLlm`. Inject clock, completion, random and guard. Distinct call ID per model call, stable within retries. Honor DESIGN §3–5; preserve all generation kwargs.

- [x] RED scripted retry-success, exhaustion, 429→503→success, 429→401 stop; assert real sends, stable fingerprints/action/call IDs, audited pairs, virtual waits and summary.
- [x] RED Retry-After actual send times, oversized guidance refusal, minimum remaining budget prevents retry, cancellation/expiry/guard stops, simultaneous contexts do not mix, missing context sends zero.
- [x] RED 500 then slow retry hitting capped timeout produces temporary failure with `retry_budget_exhausted`, preserving 500; initial timeout and provider non-transient responses keep existing classification. Oversleep before a next start yields one-send exhaustion via the carried-summary cross-check; start-write overhead blocks transport and records `not_sent`, attempts=1, with prior Retry-After retained.
- [x] RED pinned wire test `test_pinned_openrouter_one_http_send_per_audited_try`: real OpenRouter translation through fake HTTP transport with sockets blocked, covering 504→Timeout, HTTP500 exact issue message, HTTP200 body `{"error":{"code":500}}`, HTTP200 code `"502"`, 429/503, Retry-After carrier, and excluded 400/401/403/422. Assert one actual send per audited try and preserved body/model/routing; fail closed on retry/fallback global overrides without resetting globals.
- [x] RED audit-start failure sends zero, audit-finish failure permits no retry/response publication, and stream=True/sync completion fail closed before transport.
- [x] RED real fake-HTTP connection/protocol errors and redirect responses each cause one send through the owned handler, no hidden SDK resend/redirect; handler closes and works across separate asyncio.run loops. Effective retry/fallback/route overrides fail closed before any send.
- [x] RED real SDK local preparation failure cannot borrow synthetic500 or retry; request/global body model/messages overrides and inherited non-OpenRouter API base fail closed before dispatch. A global mutation between tries preserves truthful physical-send count/audit and cannot reuse an old fingerprint for a changed wire request.
- [x] RED structural AST/alias checker rejects raw LiteLlm constructors/imports/direct completion, string Agent/LlmAgent models, llm_client replacement kwarg or `model_copy(update={"llm_client": ...})` outside factory. Tests may inject clients. Exercise synthetic bypass ASTs including aliases/dynamic imports. Runtime inspection covers retained agents/builders, repairs and registry roles derived from catalogs, not brittle counts.
- [x] Update Linux launcher test injection anchor from raw LiteLlm to the factory call. Its injected Issue201LauncherModel BaseLlm must still execute; the production boundary missing-context check remains intact. Audit disposable checkout copying so all new dependencies are present without touching Git in this worktree.
- [x] Run `uv run --frozen pytest tests/adapters/test_provider_retry.py tests/adapters/test_provider_boundary_routes.py tests/adapters/test_provider_transport_contract.py -q` for RED/GREEN. Linux CI additionally runs `uv run --frozen pytest tests/dev_runtime/test_agent_failure_shutdown.py -q`; collect it locally, do not run product processes on macOS.

### Task 4: Integrate graph actions, replay, and stable exhaustion

**Files:** Modify `adapters/adk/runner.py`, `adapters/adk/errors.py` if needed, `services/application.py`, `services/node_attempt_replay.py`, `services/contracts/provider_retry.py`, `workflow/contracts.py`, `workflow/domain.py`, `workflow/requests/attempts.py`; extend `tests/adapters/test_adk_workflow_runner.py`, `tests/workflow/test_node_attempts.py`, `tests/adapters/test_adk_specification_authoring_failures.py`, and audit contract tests as needed. Modify recipes only if characterization proves filter necessary; do not lower max_attempts.

R14 integration correction: audit `attempt_fingerprint` preserves the durable `canonical_hash` format (`sha256:<64 lowercase hex>`), accepting the existing bare-hex audit fixtures for compatibility. Use a separate strict fingerprint annotation; request/idempotency digests remain exactly 64 lowercase hex. Compare the full stored fingerprint without trimming, rewriting history, or changing schema. Verify real graph audit linkage and reject malformed fingerprints.

Also update the isolated lease/configuration test double in `tests/services/test_specification_authoring_application.py` to expose the new provider execution port while retaining its mocked runner. Unused audit/guard/lease methods must fail if called; do not add a production fallback for incomplete domain adapters.

**Interfaces:** Bind context after the durable winning StartNodeAttempt, capture provider_retry policy in execution_settings, maintain host identity and guard/source checks. Add EXTERNAL_PROVIDER_TEMPORARY. Optional internal FailNodeAttempt.provider_failure is absent for ordinary failures and must preserve their serialization/fingerprints. Carry typed summary through wrapping, cross-check against independent audit before failure transition, validate and persist it with original receipt; return recorded result.

- [x] RED real recipe retry-success/exhaustion: one logical attempt/outcome, at most one artifact, stable original key, durable pairs, precise safe code/summary; failure takes precedence over Specification generic producer/output paths.
- [x] Characterize `max_attempts=2` with ProviderTransientFailure and assert exactly the configured provider sends. Keep existing config if PASS; only if it fails filter RetryConfig exceptions. Record evidence and decision.
- [x] RED first/replay equality and zero replay sends/sleeps/audit appends, concurrent duplicates own one loop, changed runtime settings cannot change completed receipt, legacy request/receipt compatibility.
- [x] R15 fix-round RED successful Specification replay with real preflight receipts: prefer the original frozen terminal Start result, preserve legacy compatibility, and prove zero replay sends/sleeps/audit appends before GREEN.
- [x] R16 fix-round RED legacy initial Start followed by the first revalidation settling stale: preserve its actual terminal error/output/position, exclude successful intermediate preflight, and keep request/result bytes and fingerprints unchanged.
- [x] RED lease/source change during sleep blocks next send and retains safe stale/obsolete settlement; audit failures publish no artifact; nonretryable/output/recipe errors retain existing paths and semantic repair budgets.
- [x] Run `uv run --frozen pytest tests/adapters/test_adk_workflow_runner.py tests/workflow/test_node_attempts.py tests/adapters/test_adk_specification_authoring_failures.py -q` for RED/GREEN; then `uv run --frozen pytest tests/adapters/test_adk_graph_recipes.py tests/adapters/test_vision_recipe.py tests/services/test_specification_authoring_application.py -q`.

### Task 5: Preserve typed failures in helpers and non-session hybrid validation

**Files:** Modify `utils/adk_runner.py`, `services/story_runtime.py`, `services/roadmap_runtime.py`, `tools/spec_tools.py`, `services/specs/story_validation_service.py`; add a public bound-context accessor to `adapters/adk/provider_retry.py` if needed; extend `tests/test_adk_runner.py`, `tests/test_story_runtime.py`, `tests/test_roadmap_runtime.py`, `tests/test_spec_validation_modes.py`.

**Interfaces:** Utility preserves ProviderTransientFailure/ProviderAuditError/stop errors before AgentInvocationError wrapping. Helper entry points bind loaded host project and generated action ID. Non-session hybrid returns success=False, ready_for_sprint=False, temporary code/safe message/provider_failure while leaving prior validation_evidence unchanged. Audit error is local EXTERNAL_EXECUTION_FAILED without provider summary. Copy context into executor.submit(asyncio.run,...) using contextvars.copy_context(). No in-session hybrid audit feature.

- [x] RED invoke_agent_to_text preserves exact typed failures; explicit helper invocation gets context and same shared factory/retry behavior, without schema-repair misclassification. No raw exception messages leak.
- [x] RED non-session hybrid exhaustion preserves prior evidence and reports temporary failure; retry-success is normal review; structural mode sends zero. Cover no-running-loop and running-loop/thread-pool context propagation.
- [x] Run `uv run --frozen pytest tests/test_adk_runner.py tests/test_story_runtime.py tests/test_roadmap_runtime.py tests/test_spec_validation_modes.py -q` for RED/GREEN; then `uv run --frozen pytest tests/test_story_validation_service.py tests/services/test_story_validation_application.py -q`.

### Task 6: Retain terminal failure across API, CLI and UI reload

**Files:** Modify `services/read_projections.py`, `frontend/project.js`; modify api/cli/application only where needed. Extend `tests/adapters/test_api_workflow_domain.py`, `tests/adapters/test_cli_workflow_domain.py`, `tests/services/test_vision_failure_projection.py`, existing Node suites `tests/test_dashboard_bundle.mjs` and/or `tests/test_vision_interview_ui.mjs`. No new endpoint/test suite/CI wiring/progress features.

- [x] RED API HTTP409, fixed safe code/message/output.provider_failure, identical first/replay payload, unchanged other failures; CLI stdout JSON/exit1 and retry-success exit0.
- [x] RED Vision reload retains temporary code/summary and no phantom candidate, legacy failures unchanged. Node behavioral test executes existing checkedResponsePayload and verifies retained code/summary; no prose/source regex.
- [x] Run `uv run --frozen pytest tests/adapters/test_api_workflow_domain.py tests/adapters/test_cli_workflow_domain.py tests/services/test_vision_failure_projection.py tests/adapters/test_production_read_surfaces.py -q` and `node --test tests/test_dashboard_bundle.mjs tests/test_vision_interview_ui.mjs` for RED/GREEN.
- [x] GREEN minimal terminal metadata projection; preserve existing rendering. Audit existing lifecycle e2e Vision/error locators against the new safe terminal text and unchanged renderer. Update only expectations demonstrably broken in the same change; document why no progress-related locator churn is needed.

### Task 7: Verify complete provider-free coverage and whole diff

**Files:** Existing provider-boundary/transport/runner tests only if a demonstrated coverage gap remains; `tests/workflow/test_contracts.py` to retain its closed-enum assertion with the approved `EXTERNAL_PROVIDER_TEMPORARY` member after the affected gate demonstrated the missing expectation; all changed files for checks. No redundant test or unrelated refactor.

- [x] Reconcile acceptance matrix: retry-success, exhaustion, status exclusions, Retry-After, capped slow retry, min budget, replay/concurrency, durable audit/failpoints, API/CLI/Vision/frontend projection, structural every-path/bypass checks, missing-context enforcement.
- [x] If a gap exists, observe RED before minimal fix, rerun owning task checks and scoped review. If no gap exists, record coverage without manufacturing a test.
- [x] Run every required gate below, archive bounded results, collect Linux-only suites, and obtain fresh whole-diff review. Once task fixes are approved and source is frozen, independent source review may overlap the long complete-suite runs; final acceptance requires their actual results. Final findings receive one fix dispatch and one scoped re-review; adjudicate residuals without silent budget extension.

## Required verification gates

Use temporary logs and bounded summaries; no provider calls or real profiles. Run all targeted commands above plus affected suites.

Native sandbox test precondition: CLI tests reach the checkout's existing home-default maintenance fence before their handlers. Use an external temporary pytest fixture to map only this checkout's default home fence to an owned temporary directory, retaining the real lock implementation. Preserve explicit test roots, fake checkout roots, and production selections; do not override HOME, change repository tests for the sandbox, or access an existing home lock. Record the fixture path/hash and verify the dedicated runtime-ownership tests with it. The gate command arguments below remain unchanged; this test-only isolation is passed through the pytest plugin environment.

```sh
uv run --frozen ty check
uv run --frozen ruff check .
uv run --frozen ruff check --select ANN .
uv run --frozen ruff format --check <all changed .py files>
git diff --check
node --test $(sed -n '91,100p' .github/workflows/ci.yml | grep -oE 'tests/[^ ]+\.mjs')
uv run --frozen pytest tests/adapters/test_production_read_surfaces.py -q
uv run --frozen pytest tests/adapters tests/workflow tests/services tests/test_adk_runner.py tests/test_story_runtime.py tests/test_roadmap_runtime.py tests/test_spec_validation_modes.py tests/test_story_validation_service.py tests/test_model_config_env.py -q
uv run --frozen pytest tests/e2e --collect-only -q
uv run --frozen pytest tests/dev_runtime/test_agent_failure_shutdown.py --collect-only -q
```

`ty check` must be repo-wide, exit0 and say **All checks passed!**. `# type: ignore` does not work; `# ty: ignore[rule]` only as explained last resort. Follow repo convention for pytest.fail:

```python
pytest.fail(
    "specific failure message",  # ty: ignore[invalid-argument-type]
)
```

Required isolated **Linux evidence**: `uv run --frozen pytest tests/dev_runtime/test_agent_failure_shutdown.py -q`, `uv run --frozen pytest tests/e2e/test_single_project_lifecycle_ui.py -q`, and CI canonical gate. These cannot execute natively on macOS; collection is local evidence only. Do not touch the user's containers/volumes. Native launcher exit2 is expected. Any new Linux-only limitation must be reported explicitly.

## Phase-2 execution and review ledger

Create the plan-scoped SDD workspace with the skill script; first ledger line names this plan. Record task briefs/reports, RED/GREEN logs, before/after hashes and task diff snapshots, separate spec/quality verdicts, fix rounds and rulings. Git read-only overrides skill commit/branch-finishing instructions: no Git mutations, and preserve local evidence rather than deleting the only recovery map. Five task fix rounds (rounds1–3 same implementer,4–5 fresh stronger model); one final fix dispatch/re-review. Fresh native ultra implementer per task and fresh native ultra task/whole-diff reviewer, explicit settings/no descendants, at most four agents including coordinator. Reviewer reads scoped packet, not implementation history. Latest user phase-2 model controls take precedence over earlier external-route restrictions.

## Remaining risks

- Pinned wire translation must prove no hidden resend, including error bodies and422 parameter-repair behavior; resolve at the boundary without upgrading/broadening retry scope.
- Application-level audit serialization has no new SQL unique constraint; independent connection tests and strict pairing are required. Old binaries reading all WorkflowEvent enum rows may not recognize new vocabulary.
- Crash recovery retains existing at-least-once upstream behavior; same-key terminal replay guarantees do not imply exactly-once provider billing.
- Linux process/browser execution evidence must come from Linux CI; macOS collection and provider-free unit tests do not establish it.
- Structural path discovery is verified for the current literal/root.* declarations and newly declared literal packages/modules. A future cross-separator include glob such as `pkg*deep.*` requires a conservative root-prefilter update and a matching disposable-tree test before adoption; current packaged code is covered.

## Phase-1 evidence retained

Only this plan was written in phase1; no production/test code or provider calls. The verified dependency/source mapping and provider-free reproduction in DESIGN §1–2 remain the diagnosis evidence. Phase2 starts after the approved revisions above.
