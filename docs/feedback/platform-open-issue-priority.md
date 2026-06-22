# AgileForge Open Issue Priority

Date: 2026-06-22

Scope: all currently open GitHub issues in `arduinitavares/agileforge`.

Ranking basis:

- Does it block an active dogfood project now?
- Can it make `workflow next` advertise an unrunnable path?
- Can it create durable side effects with unclear recovery?
- Does it affect many projects or only a narrower workflow?
- Is it a foundation/strategic feature rather than a current blocker?

## Priority Ranking

### P0 - Fix Now

#### #143 Story phase reuses stale draft attempts after scope extension and blocks save/generate routing

URL: https://github.com/arduinitavares/agileforge/issues/143

Why this is first:

- Blocks the current ASA dogfood run.
- Breaks the core AgileForge agent contract: `workflow next`, `story pending`,
  `story history`, and `story save` disagree.
- Platform-wide risk for any project that uses scope extension, roadmap refresh,
  or same-name requirements.
- Likely has a focused fix: provenance-aware Story working-state checks.

Next action:

- Implement #143 before resuming ASA Story work.

### P1 - Fix Next

#### #144 Roadmap generation can return complete draft that roadmap save rejects for coverage mismatch

URL: https://github.com/arduinitavares/agileforge/issues/144

Why this is second:

- Breaks generation/save contract.
- Sends agents into false review state: roadmap appears complete but cannot be
  saved.
- ASA already worked around it, so it is not the active blocker.
- Needs product-contract decision: full current backlog coverage vs additive
  scope-extension coverage.

Next action:

- Fix after #143 unless the next ASA loop hits roadmap coverage again first.

### P2 - High Severity, Not Current ASA Blocker

#### #140 authority curate can publish candidate authority but leave mutation ledger unreconciled

URL: https://github.com/arduinitavares/agileforge/issues/140

Why this is high:

- Durable side effect can happen while command reports failure.
- Recovery/ledger ambiguity is dangerous for guarded mutations.
- Affects authority curation reliability and operator trust.

Why not above #143/#144:

- Current ASA is past authority review and blocked in Story phase.
- Authority curation is not on the current ASA path unless new authority repairs
  are needed.

Next action:

- Keep as next authority-curation hardening item after current Story/Roadmap
  blockers.

### P3 - Migration / Readiness Robustness

#### #142 authority curate can fail on legacy curation table missing mutation_event_id

URL: https://github.com/arduinitavares/agileforge/issues/142

Why this matters:

- Runtime schema drift can crash authority curation before candidate generation.
- Existing users/databases may hit this when code expects a newer table shape.

Why lower than #140:

- #140 is a durable side-effect/recovery ambiguity.
- #142 is narrower: migration/readiness compatibility for existing DBs.

Next action:

- Audit whether current migrations already resolve this. If fixed, close with
  evidence. If not, add migration/readiness test and repair path.

### P4 - Targeted Feedback Completeness

#### #141 authority feedback rejects review-visible assumption targets

URL: https://github.com/arduinitavares/agileforge/issues/141

Why this matters:

- Blocks targeted assumption repair.
- Pushes users toward broad authority regeneration, which is riskier and less
  controlled.

Why lower:

- Current ASA is past the authority curation path.
- Later repair-menu work may already have fixed or changed this behavior; needs
  re-verification before implementation.

Next action:

- Reproduce on current `master`. If fixed, close with evidence. If still broken,
  add assumption-target lookup tests.

### P5 - Strategic Feature / Not Current Blocker

#### #129 Add brownfield setup mode with product-spec curation before authority compilation

URL: https://github.com/arduinitavares/agileforge/issues/129

Why this matters:

- Important platform feature for brownfield projects.
- Prevents raw implementation inventory from becoming product authority.

Why last:

- Large feature/design area, not a current ASA blocker.
- Does not block current greenfield/scope-extension dogfood flow.

Next action:

- Keep as roadmap-level platform work after current workflow correctness bugs are
  fixed.

## Recommended Execution Order

1. Fix #143.
2. Resume ASA Story phase enough to prove #143 is fixed.
3. Fix or re-test #144.
4. Audit #142 and #141 for current-master status; close if already fixed.
5. Fix #140 before relying heavily on more authority curation.
6. Schedule #129 as strategic platform work.

## Notes

- Do not treat ASA details as implementation assumptions. ASA is evidence only.
- Any fix must remain project-agnostic.
- Before closing any old authority-curation issue, reproduce or disprove it on
  current `master`; do not close from memory.
