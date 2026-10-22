# Application model configuration

All eight production roles in `config/models.yaml` use
`openrouter/openai/gpt-6.1-sol`. Effort follows the task's obligations:

| Role | Effort | Reason |
| --- | --- | --- |
| Product Vision | medium | Interactive synthesis of bounded evidence and human clarification. |
| Product Goal | medium | Interactive definition of one future state and its boundaries. |
| Specification structuring | xhigh | Preserve the complete source contract and graph references. |
| Specification validation | high | Review one accepted Story against exact Specification evidence. |
| Backlog generation | high | Cover requirements and cite their exact Specification items. |
| Roadmap generation | xhigh | Order all Backlog items without duplication and respect dependencies. |
| Story generation and correction | high | Produce bounded vertical slices and acceptance criteria. |
| Sprint planning | high | Decompose an already selected, ordered Story cohort into Tasks. |

`reasoning.effort` is the shared fallback. `reasoning.by_role` overrides it using
the same role names as `models`. Supported efforts are `low`, `medium`, `high`,
`xhigh`, and `max`. For example:

```yaml
reasoning:
  effort: high
  by_role:
    product_goal: medium
    specification_structurer: xhigh
```

Existing profiles with only `reasoning.effort: max` remain valid. A profile with
no reasoning section omits that parameter. Complete profiles with role overrides
and no fallback must cover every active role. Unknown roles and malformed
reasoning settings fail before a provider request.

Each workflow Node Attempt records the effective effort for its own role.
Changing that effort changes its attempt identity. Specification validation is
a separate explicit one-shot Story review, outside these graph Node Attempts;
this configuration change does not invalidate existing accepted evidence.

The separate `config/models.test.yaml` profile retains the free model and no
reasoning parameter. The regression suite intercepts SDK HTTP requests and does
not call a paid provider.

`MODEL_CONFIG_PATH` selects an alternative configuration. Changing repository
defaults does not update an existing production profile. Apply profile changes
through the reviewed model-configuration maintenance procedure in
[the Linux container runbook](linux-containers.md#upgrade-an-existing-production-profile).

Provider references: [Sol 6.1](https://openrouter.ai/openai/gpt-6.1-sol) and
[reasoning parameters](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).
