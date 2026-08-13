## Problem Statement

Software developers need a usable, bounded String Calculator whose behavior is
explicit and verifiable. Delivery practitioners also need clear evidence that
the product moved from reviewed intent through incremental TDD, verification,
and human-owned review. Ad hoc exercises and repositories containing finished
solutions do not provide both the usable result and its traceable delivery
journey.

## Solution

Deliver a String Calculator through one public Python operation and an installed
command-line interface. The product sums supported Number Lists, handles empty
input explicitly, accepts comma and line-feed Delimiters, and rejects all
negative values together. Both interfaces expose one shared calculation path,
and the release includes repeatable public-contract verification and
human-reviewable TDD evidence.

## User Stories

1. As a Python developer, I want to import the public `add` operation from the
   `string_calculator` package, so that I can use the calculator without
   depending on internal modules.
2. As a Python developer, I want `add` to accept one string and return an
   integer, so that its public contract is small and predictable.
3. As a user, I want an empty Number List to produce zero, so that the absence
   of values has an explicit result.
4. As a user, I want one non-negative Integer Token to return its value, so that
   the simplest non-empty input works.
5. As a user, I want comma-separated Integer Tokens to be summed, so that I can
   calculate a total from a conventional Number List.
6. As a user, I want line feeds to delimit Integer Tokens, so that a Number List
   can span lines.
7. As a user, I want commas and line feeds to coexist in one Number List, so
   that mixed supported input works consistently.
8. As a user, I want any number of Integer Tokens to be accepted without an
   arbitrary product-level count limit, so that valid lists are not restricted
   to introductory examples.
9. As a user, I want zero and leading-zero spellings to retain their numeric
   meaning, so that decimal spelling does not change the sum.
10. As a user, I want negative zero to be treated as zero, so that rejection is
    based on numeric value.
11. As a user, I want any negative value to reject the entire Number List, so
    that no partial sum can be mistaken for success.
12. As a user, I want a rejection to identify every negative occurrence in
    encounter order, so that I can map the error back to my input.
13. As a user, I want repeated negative values retained in the rejection, so
    that no invalid occurrence is hidden.
14. As a user, I want rejected values rendered canonically, so that equivalent
    negative spellings have consistent diagnostics.
15. As a command-line user, I want to pass one Number List as a positional
    argument, so that the calculator is directly scriptable.
16. As a command-line user, I want actual shell-quoted line feeds to work as
    Delimiters, so that command-line behavior matches the Python operation.
17. As a command-line user, I want a successful invocation to print only its
    sum and exit successfully, so that other programs can consume it reliably.
18. As a command-line user, I want negative input reported as an error with a
    failing exit status, so that automation cannot mistake rejection for a sum.
19. As a delivery practitioner, I want both interfaces to share one calculation
    path, so that their behavior cannot drift independently.
20. As a delivery practitioner, I want tests to exercise public behavior, so
    that implementation details can change without invalidating the contract.
21. As a reviewer, I want supported behavior partitions covered explicitly, so
    that acceptance does not depend on a few representative examples.
22. As a reviewer, I want local and hosted verification to run the same quality
    gate, so that their evidence is comparable.
23. As a reviewer, I want the verified commit identified in the evidence, so
    that the reviewed result is unambiguous.
24. As a reviewer, I want visible red-green progression at public seams, so
    that the claimed incremental TDD journey is auditable.
25. As a Product Owner, I want later kata stages and unrelated product
    capabilities excluded, so that this release remains bounded around the
    agreed integer-list use cases.

## Implementation Decisions

- Target Python 3.13 or newer and manage the project exclusively with uv.
- Export `add(numbers: str) -> int` directly from the public
  `string_calculator` package interface.
- Define a Number List as either empty or one or more Integer Tokens separated
  by exactly one comma or actual line-feed Delimiter. Both Delimiter forms may
  appear in one Number List.
- Define an Integer Token as ASCII decimal digits with an optional leading minus
  sign and no whitespace or leading plus sign.
- Return zero for an empty Number List. Do not impose an arbitrary product-level
  maximum number of Integer Tokens.
- Interpret leading zeros numerically. Treat negative zero as zero.
- Reject the entire Number List when any parsed value is below zero. The public
  Python operation raises `ValueError` rather than returning a partial sum.
- Format rejection text as `negative numbers not allowed: ` followed by every
  canonical negative value in encounter order, separated by comma and space.
  Preserve duplicate occurrences.
- Install the `string-calculator` command with one positional Number List for
  supported invocations.
- On success, write only the decimal sum and one trailing newline to standard
  output, write nothing to standard error, and exit zero.
- On negative-number rejection, write the Python error text and one trailing
  newline to standard error, write no sum to standard output, and exit nonzero.
- Make the command-line adapter delegate to the public `add` operation rather
  than implementing another parser or calculation path.
- Leave Malformed Input outside the promised product contract. Do not establish
  stable behavior for whitespace, a leading plus sign, adjacent or trailing
  Delimiters, a literal backslash followed by `n`, or missing or extra
  command-line arguments.
- Prefer the Python standard library. Add a runtime dependency only when a
  concrete requirement cannot reasonably be met without it.
- Provide one uv-only quality gate for local use and GitHub Actions without
  typing suppressions.

## Testing Decisions

- Test externally visible behavior rather than parser structure, helper calls,
  or module organization.
- Use the public Python `add` operation as the primary test seam. Cover empty
  input, one and many values, comma and actual line-feed Delimiters, mixed
  Delimiters, zero, leading zeros, negative zero, and negative rejection order
  and duplication.
- Use the installed `string-calculator` command as the secondary seam through a
  small subprocess suite. Verify successful output and status, actual line-feed
  handling, and negative standard-error and nonzero behavior without duplicating
  every Python case.
- Do not assert a stable result for Malformed Input because it is outside this
  release's contract.
- Demonstrate incremental TDD with captured failing and passing public-contract
  test evidence. Do not require a particular commit cadence.
- Run the same release gate locally and in GitHub Actions:
  `uv lock --check`, `uv run --frozen ruff check .`,
  `uv run --frozen ty check`, and `uv run --frozen pytest`.
- Identify the exact verified commit and successful gate results in completion
  evidence for human review.

## Out of Scope

- Custom Delimiters.
- Ignoring or otherwise giving special meaning to values above 1000. Such
  values remain ordinary Integer Tokens in this release.
- Arithmetic expressions, general expression evaluation, or additional
  calculator operations.
- A web or graphical interface.
- Persistence, accounts, networking, or services.
- Stable handling or diagnostics for Malformed Input.
- Stable behavior for the two-character sequence `\n`.
- Sprint duration, story-point capacity, task decomposition, or implementation
  sequencing. AgileForge planning owns those decisions after Specification and
  Authority acceptance.

## Further Notes

- The product domain is based on Roy Osherove's String Calculator Kata, but the
  accepted Project Vision, active Product Goal, glossary, and this specification
  control scope. Later kata stages are not implicit requirements.
- The repository begins without calculator behavior, tests, or a command-line
  interface so that delivery can proceed from the reviewed contract.
- Human review owns acceptance of the Specification, Authority, planning
  artifacts, and delivered release.
