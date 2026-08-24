# String Calculator Negative Diagnostic Structured Spec Review

Verdict: gold_corrected

Expected judgment for `weakened-begins-with`: `semantically_unacceptable`

## Source-To-Spec Finding

- The generated candidate changes complete diagnostic-message equality into a
  prefix-only requirement. Its examples do not restore the missing prohibition
  against extra diagnostic text. This weakens the registered source contract.

## Correction Required For Gold Spec

- The gold candidate requires the complete `ValueError` message to equal the
  fixed prefix plus every canonical negative value in encounter order, using
  comma-space separators and preserving duplicates.

This human-reviewed fixture is a quality oracle only. It has no runtime or
lifecycle effect. Human review remains the final semantic decision.
