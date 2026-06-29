# Require grill-with-docs for new scope

AgileForge requires `grill-with-docs` as the challenge producer before accepting new scope, including both greenfield project creation and existing project scope extension. AgileForge also requires `to-prd` as the PRD producer after the challenge artifact is ready. This deliberately favors process integrity and auditable product thinking over provider/tool portability: if either required producer is unavailable, AgileForge should hard-fail instead of silently accepting unchallenged scope or falling back to a weaker template.
