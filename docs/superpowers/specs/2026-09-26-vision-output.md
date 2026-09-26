# Vision output capacity and failure handling

User-approved scope: resolve issue #283 by giving Vision a 128,000 completion-token default, retaining the configured model and maximum reasoning, allowing longer reasoning, preserving safe response diagnostics, and validating one live bootstrap after deployment. Approval does not accept the resulting Vision or authorize downstream planning.

The production failure was incomplete JSON. A provider-free reproduction through the actual ADK Vision Agent and recipe proves that EOF parsing bypasses semantic repair and loses finish reason and usage. Budget exhaustion is a hypothesis, not a historical fact. Both STOP and MAX_TOKENS synthetic responses reproduce the metadata loss.

Required behavior:
- Vision primary and semantic-repair agents use the same 128,000-token default, with existing explicit token overrides honored. Other role defaults and configured model/reasoning choices stay intact.
- Vision has a 600-second total recipe deadline and a 660-second attempt lease. Provider timeout must not introduce the old shorter deadline. Production recipe construction and new attempt provenance use the same settings.
- Record the token limit, timeout and actual retry/repair policy on new Vision attempts. One generation attempt and at most one existing semantic repair; no automatic malformed-output retry. Historical attempts stay unchanged.
- Inspect the response before ADK schema parsing. MAX_TOKENS, empty output and syntactic EOF return VISION_OUTPUT_INCOMPLETE; other invalid JSON/schema returns INVALID_VISION_PAYLOAD. Do not claim a token limit was reached without that finish reason. Preserve strict schema and semantic validation.
- Attach safe bounded diagnostics: stage, code, finish reason, available integer token counts, response byte count and hash. No generated text, validation-input excerpts, credentials or arbitrary provider metadata. Metadata absence remains null/unknown.
- Durable attempt outcome, idempotent replay, CLI/API and dashboard preserve the precise error. Real provider errors remain distinct. No partial Vision facts, approval, or downstream progression on failure.
- Deterministic tests cover the actual ADK leaf plus application/transport and persistence. Valid output, genuine provider failure, repair-stage failure and explicit successful later retry are controls.
- After verification and independent review, deploy from a clean committed source, retain a fresh backup and exact rollback image, then perform the single approved project-2 bootstrap. Keep its draft pending human review.

No graph prototype changes, schema migration, model downgrade, broad framework refactor, unrelated role-budget increases or live calls by delegated workers.
