# Model generation policy

AgileForge delegates output capacity to the selected provider. It preserves the
configured model, reasoning effort, privacy routing, complete input, tools and
output schema. It does not lower output or reasoning budgets to save cost, switch
models after failure, truncate a prompt, or accept a partial structured result.

## Coverage

The same provider boundary serves these eight retained model roles:

| Model role | Workflow and other instances |
| --- | --- |
| `product_goal` | Product Goal interview |
| `product_vision` | Vision bootstrap/interview, primary and repair |
| `specification_structurer` | Specification structuring |
| `backlog_primer` | Backlog generation and helper |
| `roadmap_builder` | Roadmap generation and artifact helper |
| `user_story_writer` | Story generation, batch helper and schema repair |
| `sprint_planner` | Sprint planning and helper |
| `spec_validator` | Specification semantic review |

There are eight graph generation nodes: Vision has two nodes and semantic review
is a helper. Development profiles, the checkout launcher and the installed
application use the same agent and provider policy. Launcher profiles no longer
inject a Specification token budget.

The following legacy environment settings are retired and ignored, including
when inherited by a launcher child:

- `VISION_INTERVIEWER_MAX_TOKENS`
- `SPECIFICATION_STRUCTURER_MAX_TOKENS`
- `SPEC_VALIDATOR_MAX_TOKENS`
- `BACKLOG_PRIMER_MAX_TOKENS`
- `ROADMAP_BUILDER_MAX_TOKENS`
- `STORY_WRITER_MAX_TOKENS`
- `SPRINT_PLANNER_MAX_TOKENS`

New attempt settings record the nonsecret `agileforge.generation-policy.v1`
policy. `./agileforge-dev info --profile NAME --json` reports that policy without
contacting a provider. Resolved capacities belong to individual provider calls,
not to a graph/status read or a mutable global setting. Existing attempt and
receipt bytes are retained; replay consults durable receipts before live policy
or capacity resolution.

## Published capacities and the remaining limitation

For each new OpenRouter model call, AgileForge reads the public endpoint metadata
at `https://openrouter.ai/api/v1/models/AUTHOR/SLUG/endpoints`. It validates positive
integer capacities and exact returned model identity, then requests the largest
known published endpoint completion
capacity with the model's supported token-parameter spelling. Unknown endpoints
are counted explicitly. No usable capacity produces
`PROVIDER_CAPACITY_UNAVAILABLE` before an inference send. A model alias whose
returned canonical identity differs cannot yet be verified by this resolver; it
fails with that same explicit code instead of authorizing an unrelated capacity.

The per-try audit stores the selected model, requested completion ceiling,
maximum published context, known/unknown endpoint counts, metadata URL, retrieval
time and snapshot SHA-256. Transient retries reuse the exact resolved request and
snapshot. A separate semantic repair call resolves again with its complete
changed input. Metadata requests do not count as inference tries.

**This is not verified full allowance for the actual prompt.** The public
contracts checked do not establish authoritative counting of the final prompt,
tools and schema, or negotiation of all remaining context. Published endpoint
metadata also does not establish which endpoint is eligible for the unchanged
privacy route. Both `actual_prompt_allowance: unverified` and
`route_eligibility: provider_enforced_unverified` remain visible in policy and
call evidence. A large published ceiling can still be rejected near the context
limit or by a smaller eligible route. Issue #302 remains open for that requirement;
the repair must not be described as full resolution of #302.

AgileForge does not subtract an estimated prompt size, take the smallest ceiling
across unrelated routes, parse provider prose for guessed budgets, omit an
explicit ceiling and claim a full default, or weaken privacy routing to obtain a
larger endpoint. No paid probe is used to discover limits.

## Failures and recovery

Raw final responses are checked before schema conversion or helper extraction.
A token-limit finish reason wins even when the text looks like valid JSON.
Empty or prematurely ended output has a specific incomplete-output code;
complete malformed/schema-invalid output retains strict validation. Intermediate
tool-call and partial streaming events are not mistaken for final artifacts.

Product Goal reports `PRODUCT_GOAL_OUTPUT_INCOMPLETE` or
`INVALID_PRODUCT_GOAL_PAYLOAD`; Vision and Specification retain their specific
codes. Other generation paths report `GENERATION_OUTPUT_INCOMPLETE` or
`INVALID_GENERATION_OUTPUT`. Safe output diagnostics contain allowlisted finish
reason, nonnegative usage counts when provided, response byte count/hash and
nonsecret settings. Provider usage is evidence, not a prompt-accounting preflight.
Raw prompts, responses, credentials and arbitrary provider error prose are not
included in these failure messages.

An owned response with a recognized structured context-limit code produces
`PROVIDER_CONTEXT_LIMIT_EXCEEDED`. Other HTTP 400/413/422 rejections produce
`PROVIDER_REQUEST_REJECTED` without asserting an unconfirmed cause. These errors
report HTTP status, the requested published ceiling and the unverified allowance
limitation. Transient rate-limit/unavailable responses retain the existing
bounded retry protocol; authentication and privacy requirements are preserved.

Inspect the current workflow status and recovery command. After addressing the
provider/request constraint, retry with a **new idempotency key**. Reusing the
failed key replays the recorded failure without another lookup or inference.
A successful new generation creates only a reviewable candidate; existing human
acceptance, lineage and validation gates remain required.

## Generation lifetime and recovery ownership

Healthy generation has no application read/write/total deadline or ADK call-count
ceiling. HTTP connection and pool acquisition remain bounded at ten seconds;
public metadata availability has a separate thirty-second deadline. The elapsed
retry budget limits admission of another failed-call retry, not completion of an
already admitted healthy call. Explicit legacy deadline contexts remain supported
for callers that deliberately supply them; new host-owned generation uses none.
Semantic repair counts, schema validation, transient retry counts/backoff and
bounded diagnostic excerpts remain validation/recovery controls.

A separate durable ownership record renews a live attempt's recovery lease.
It does not rewrite the immutable attempt, original lease, fingerprint or receipt.
Acquire, renewal and final publication share the serialized database writer
boundary. Lost, replaced or invalid authority cancels and awaits local metadata,
inference or backoff work and prevents late publication. A dead owner stops
renewing and recovery becomes available after expiry. Local cancellation cannot
guarantee that a remote provider has stopped work or billing.

Startup applies only the registered additive ownership schema upgrade to a
recognized predecessor. Unknown schemas still fail closed. This repair's tests
use isolated databases; they do not migrate or recover any production database.

## Documentation evidence

Public provider contracts were checked on 2026-10-21 against the OpenRouter
[OpenAPI schema](https://openrouter.ai/openapi.json),
[endpoint metadata API](https://openrouter.ai/api/v1/models/openai/gpt-6-sol/endpoints).
Those sources describe published capacities, shared context and request fields;
they do not prove the remaining actual-prompt or route-eligibility requirement.

The installed stack used for repair verification is ADK 2.2.0, LiteLLM 1.78.3,
HTTPX 0.28.1 and OpenAI SDK 2.5.0. HTTP timeout behavior follows the
[HTTPX 0.28.1 timeout contract](https://github.com/encode/httpx/blob/0.28.1/docs/advanced/timeouts.md).
Deterministic tests exercise the installed ADK, LiteLLM and owned HTTPX boundary
with synthetic public metadata and inference responses. Passing those tests is
repair verification, not live-provider or production recovery acceptance.
