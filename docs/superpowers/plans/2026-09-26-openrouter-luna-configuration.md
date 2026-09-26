# OpenRouter Luna and Sol Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Configure AgileForge once so routine provider-backed roles use Luna, roles needing deeper reasoning use GPT-6 Sol, both use maximum reasoning, and the stable CLI and dashboard receive the existing OpenRouter credential.

**Architecture:** Keep model and reasoning choices in the profile's `models.yaml`; send the shared reasoning setting through the existing OpenRouter request helper. Supply credentials through the installed runtime's private secret-file interface. Add a controlled maintenance operation for updating an existing profile's model configuration without bypassing its integrity checks.

**Tech Stack:** Python 3.13, uv, PyYAML, ADK/LiteLLM, OpenRouter, Linux Docker Compose through WSL.

**Spec:** User request in this conversation: fix the installation/configuration gap, use Luna with maximum reasoning through OpenRouter, and present the plan before implementation. The subsequent adjustment selects GPT-6 Sol with maximum reasoning for jobs needing deeper reasoning. These choices apply to AgileForge, not the Codex agents doing this work.

## Decisions and evidence

- Retain `openrouter/openai/gpt-5.6-luna` for five bounded roles. Select `openrouter/openai/gpt-6-sol` for the three roles identified below. Remove all Terra selections. This is an explicit assignment by role, without automatic model selection or escalation.
- Add a shared top-level setting `reasoning: {effort: max}` to `models.yaml`. OpenRouter's public model catalog confirmed `max` is supported by both `openai/gpt-5.6-luna` and `openai/gpt-6-sol` on 2026-09-26.
- `utils/model_config.py:get_openrouter_extra_body()` is used by all eight ADK adapters. It currently sends only privacy routing settings.
- The production profile has its own model file and records its SHA-256 in `runtime.json`. Editing only the YAML makes startup reject configuration drift. The current runtime has no model-configuration update command.
- The current Windows shim and dashboard omit `--secrets-file`; production mounts no credential file. The repository `.env` contains a nonempty `OPEN_ROUTER_API_KEY`, but also application settings which the strict runtime secret parser rejects.
- Adapters construct model wrappers at import, so the production service must restart after the model update.

### Role assignments

| Role key | Model | Reason for assignment |
| --- | --- | --- |
| `product_vision` | GPT-5.6 Luna, max | Bounded interview turns and draft repair; unresolved ambiguity becomes questions. |
| `product_goal` | GPT-5.6 Luna, max | Records one constrained Product Goal interview turn. |
| `specification_structurer` | GPT-6 Sol, max | Synthesizes Vision, Goal, source documents and feedback while preserving IDs, relations and requirement meaning. |
| `spec_validator` | GPT-6 Sol, max | Checks Story-to-Spec contradictions, omissions and testability against the exact accepted sources. |
| `backlog_primer` | GPT-5.6 Luna, max | Produces a bounded backlog from the accepted Specification and surfaces open questions. |
| `roadmap_builder` | GPT-6 Sol, max | Allocates backlog items exactly once and reasons about dependency-safe sequencing. |
| `user_story_writer` | GPT-5.6 Luna, max | Works within a selected backlog item and source boundaries; proposed effort and dependencies are advisory. |
| `sprint_planner` | GPT-5.6 Luna, max | Works on the Story cohort selected upstream, with capacity selection already fixed. |

This split is based on the current agent prompts and contracts, not a measured quality comparison. The role mapping remains explicit in YAML, so it can be adjusted later using observed results. Both models use `reasoning.effort=max`.

References: [OpenRouter model catalog](https://openrouter.ai/api/v1/models), [reasoning configuration](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens), `docs/linux-containers.md`.

## Global Constraints

- Plan only until the user has reviewed it; no configuration, image, credential, or production changes have been made for this plan.
- Implement in an isolated `alex/` branch/worktree; preserve the graph-storage prototype checkout.
- Run product commands and tests with uv inside Linux containers. Use the development checkout's own `./agileforge-dev`; run its `info --json` before CLI state mutations.
- Keep provider privacy requirements, credential allowlisting, interpolation disabled, and output redaction intact.
- Change the shared `default` profile, so the settings apply to both existing projects and future projects using that profile.
- Preserve project data, accepted artifacts, prior attempts, reviews, Sprint evidence, and repository bindings. Do not run Vision bootstrap as part of this installation repair.
- Use provider-free tests and dummy credentials. A real provider request requires a separately bounded smoke-test decision; credential presence does not prove the key is valid.
- Do not silently reduce reasoning, switch models, raise token budgets, or weaken privacy routing to make a test pass.

## Review Focus

1. Every adapter must actually send its assigned model and `reasoning.effort=max`; a correct YAML file alone is insufficient. Task 1 captures outgoing requests for both model families.
2. Existing configurations without the new setting must retain their existing behavior; malformed settings must fail clearly. Task 1 tests both.
3. New attempts must record effective reasoning in their configuration identity while old evidence remains readable and unchanged. Task 1 tests attempt capture and replay.
4. Interruption between publishing the model file and manifest must have a verified recovery path. Task 2 injects publication failures and retains rollback evidence.
5. Both launch routes must see only the credential, with no credential values in arguments, logs, receipts, backups, or images. Tasks 2 and 3 test with disposable sentinel values.

## Task 1: Central model and reasoning settings

**Files:**
- Modify `config/models.yaml`, `utils/model_config.py`, `services/application.py`.
- Test `tests/test_model_config_env.py`, `tests/test_openrouter_privacy_config.py`, `tests/workflow/test_agentic_node_lifecycle.py`, `tests/services/test_specification_authoring_application.py`.
- Add `tests/adapters/test_openrouter_reasoning.py` for request capture using the installed ADK/LiteLLM versions.

**Interfaces:**
- Add `get_model_reasoning_config() -> dict[str, str]`: return `{}` when absent, otherwise validate the configured effort and return `{"effort": "max"}` for this deployment.
- Extend `get_openrouter_extra_body() -> dict[str, Any]` to merge the optional `reasoning` mapping with the unchanged `provider` mapping.
- Extend `get_model_token_limit_args(model_id: str, token_limit: int) -> dict[str, int]` to handle the selected GPT-6 model with `max_completion_tokens`, matching the existing GPT-5 reasoning-model behavior. Preserve `max_tokens` for the unrelated test model.
- Include the same effective reasoning setting in `_agentic_execution_settings(...)` for new attempts, without recalculating historical rows.

- [ ] Add failing assertions for the exact eight-role mapping above and that both models' effective request bodies contain `reasoning.effort == "max"` with the existing privacy settings intact.
- [ ] Add assertions for absent reasoning, non-mapping values, invalid effort values, and no cross-config cache contamination. Keep synthetic/free-model test configurations independent of the production choice.
- [ ] Add provider-free transport capture covering both model IDs through the adapters' shared path, checking the wire model ID, reasoning field and existing token limits survive `drop_params=True` and SDK serialization. Assert `get_model_token_limit_args("openrouter/openai/gpt-6-sol", 4096) == {"max_completion_tokens": 4096}` alongside the existing Luna and free-model cases.
- [ ] Add attempt identity assertions: changing the role's model or effort changes a new attempt's captured configuration; loading/replaying an old attempt uses its stored settings and does not rewrite history.
- [ ] Run those tests in the Linux development container and confirm the expected failures before implementing the helper and configuration changes.
- [ ] Implement and rerun the focused tests until passing.
- [ ] Check existing token limits and timeout behavior with a synthetic response that consumes its reasoning budget or returns no visible content. Preserve limits and produce an actionable failure; do not automatically downgrade effort or increase spending limits. Maximum reasoning shares the completion budget, so provider-free success is not a live quality/performance claim.
- [ ] Commit the tested model configuration change.

## Task 2: Supported update of an existing production profile

**Files:**
- Add `cli/production_model_config.py` for the bounded update and recovery logic.
- Modify `cli/container_runtime.py`, with narrowly scoped helpers in `cli/production_state.py` if needed.
- Test `tests/container_runtime/test_production_state.py`, `tests/container_runtime/test_container_runtime.py`; add `tests/container_runtime/test_model_config_update.py`.
- Document the command and recovery steps in `docs/linux-containers.md`.

**Interfaces:**
- Installed runtime command: `configure-models --profile default --model-config <candidate.yaml> --backup-directory <new-private-directory> --json`.
- Validate all retained roles and the optional reasoning settings using the same configuration validation as Task 1; do not hard-code the chosen Luna/Sol mapping as the only models supported by the application.
- Successful output contains only profile identity, old/new configuration hashes, and the private recovery location.

- [ ] Add failing tests for a valid update, unchanged identity/provenance and business/trace/artifact hashes, invalid candidate, existing configuration drift, wrong ownership, and a held runtime fence.
- [ ] Add failure-injection tests at each publication boundary. Recovery must restore the exact old model/manifest pair, including after a process interruption. Normal startup must reject an incomplete update rather than accept mismatched files.
- [ ] Implement the update under the existing exclusive runtime fences. Read/validate current state, validate the complete candidate, stage private files, durably retain the old pair and operation state, publish, and validate the result before reporting success. Provide an explicit recovery route for an interrupted operation; never repair arbitrary drift automatically.
- [ ] Preserve profile/state IDs, creation metadata, backup provenance, repository relocations, and all business history. Require an unused recovery directory outside captured repositories and artifact trees.
- [ ] Rehearse update and recovery on a disposable profile with dummy credentials. Confirm all focused runtime tests pass and the existing drift checks still reject manual edits.
- [ ] Commit the tested maintenance operation and documentation.

## Task 3: Repair the local launchers and validate rollout

**Files outside committed source:**
- Existing credential source: `C:\Users\atavares\Projects\agileforge\.env` (read programmatically without printing values).
- Private credential destination: a Linux-owned file under `/home/arduinitavares/.config/agileforge/`, with parent access verified and container-visible UID 10001 and mode `0600`.
- Ignored local Compose configuration: `C:\Users\atavares\Projects\agileforge\compose.override.yaml`.
- Stable launcher: `C:\Users\atavares\.local\bin\agileforge.cmd`.

- [ ] Review the branch independently and pass `./agileforge-dev check` in the Linux test image. Build a candidate production image from the clean committed revision and retain its immutable identity.
- [ ] Rehearse `serve` and `cli` with a disposable credential file and test profile, checking read-only mounts, missing-file failures, permissions, credential allowlisting and sentinel redaction.
- [ ] Back up the current launcher/local override before changing them. Preserve any existing override entries and forwarding behavior.
- [ ] Copy only `OPEN_ROUTER_API_KEY` from the existing `.env` to the private Linux credential file, with dotenv interpolation disabled and without printing or embedding the value in shell commands. Do not mount the whole repository `.env`.
- [ ] Configure the production service's read-only mount at `/run/secrets/agileforge` and its `serve --profile default --secrets-file /run/secrets/agileforge` command. Use a fixed local secret path in the ignored override so ordinary startup does not require the user to re-enter configuration.
- [ ] Update the stable CLI shim to select the same mount and `cli --profile default --secrets-file /run/secrets/agileforge -- ...`. Document that configured models and reasoning are inherited across projects.
- [ ] Before rollout, confirm no active AgileForge action, inspect `info --json`, retain the current production image, and stop the affected volume writers for maintenance. Take and verify a fresh backup which includes project #2.
- [ ] Apply the model update with the candidate runtime and restart production with the new image and credential mount. Expect a brief dashboard interruption; preserve unrelated containers.
- [ ] Verify profile/build identity, the five Luna and three Sol role assignments, maximum reasoning for both, credentials loaded as a boolean only, dashboard readiness, and CLI reads. Verify project bindings and business/artifact history against the pre-change evidence.
- [ ] Record rollout and rollback commands with hashes and image IDs. If verification fails, restore the saved model/manifest pair and launcher/override, then restart the prior image. Avoid rolling back business data for a configuration-only failure.

## Completion criteria

New provider-backed actions in the production `default` profile inherit the configured Luna/Sol role assignment, maximum reasoning, and the existing credential through either entry point, without another model-selection prompt. Linux tests and the container rehearsal verify the actual request parameters for both models, safe configuration updates, and both launch routes. Project #2 remains ready for its separately authorized Vision bootstrap.

## Plan review

Self-review: user requirements map to Tasks 1 and 3; the profile integrity constraint requires Task 2. Historical evidence and provider privacy remain explicit constraints. A live provider success claim is outside provider-free verification, and no paid request or workflow transition is included in this plan.
