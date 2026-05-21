# Gherkin Checkout Discounts Technical Spec

- Schema: agileforge.spec.v1
- Artifact id: SPEC.gherkin-checkout-discounts
- Status: accepted
- Version: 1.0
- Created: 2026-05-21
- Updated: 2026-05-21
- Markdown profile: agileforge.spec_markdown.v1

## Summary

Defines a compact Gherkin checkout-discounts fixture for authority-quality benchmarking.

## Problem Statement

Gherkin-style behavior specifications need compiled authority that preserves scenario flow, step intent, and explicit step arguments without requiring a new AgileForge SCENARIO item type.

## Controlled Terms

### scenario

- Scope: artifact
- Definition: A concrete Gherkin behavior example encoded as a REQ item in this fixture.

### step

- Scope: artifact
- Definition: A Given, When, Then, And, or But clause that contributes required scenario behavior.

### step argument

- Scope: artifact
- Definition: A Gherkin scenario outline examples table or a step argument table/doc string.

## Items

### CONSTRAINT.gherkin-structure - Gherkin structural mapping

- Type: CONSTRAINT
- Status: accepted
- Level: SHOULD
- Verification: manual-review
- Tags: bdd, gherkin, structure

Statement:

The structured spec should preserve Feature as high-level context, Rule as business-rule grouping, and Example or Scenario blocks as concrete behavior represented by REQ items with step-level acceptance criteria.

Acceptance:

- The source feature context is Checkout discounts.
- The source business rule grouping is Discounts are only applied when eligibility is established.
- Concrete Example and Scenario blocks are represented as REQ scenario items.
- Given, When, Then, And, and But step intent is preserved in scenario acceptance criteria.

### CONSTRAINT.localized-keywords - Localized Gherkin keywords

- Type: CONSTRAINT
- Status: accepted
- Level: SHOULD
- Verification: manual-review
- Tags: gherkin, localization, tooling

Statement:

Tooling should support localized Gherkin keywords when a feature file declares a non-English language header.

Acceptance:

- Tooling can recognize localized Gherkin keywords when a feature file declares a non-English language header.
- Localized keyword support is treated as tooling compatibility rather than runtime product behavior.

### GOAL.behavior-readable-discounts - Behavior-readable discount scenarios

- Type: GOAL
- Status: accepted
- Level: -
- Verification: -
- Tags: -

Statement:

The fixture should preserve checkout discount behavior as readable Gherkin scenario authority for downstream planning agents.

Acceptance:

- None

### NON_GOAL.full-gherkin-parser - Full Gherkin parser

- Type: NON_GOAL
- Status: accepted
- Level: -
- Verification: -
- Tags: -

Statement:

The fixture is not intended to model every Gherkin grammar feature beyond Feature, Rule, Scenario or Example, Scenario Outline, step keywords, examples tables, doc strings, data tables, and language keyword support.

Acceptance:

- None

### REQ.expired-coupon-outline - Expired coupon scenario outline

- Type: REQ
- Status: accepted
- Level: MUST
- Verification: acceptance-test
- Tags: bdd, examples-table, given-when-then, scenario-outline

Statement:

The expired-coupon Scenario Outline must preserve its placeholders, rejection flow, error-message assertion, and examples table rows for SPRING10 and SAVE20.

Acceptance:

- The Scenario Outline uses placeholders &lt;code&gt;, &lt;expired_on&gt;, and &lt;message&gt;.
- Given a coupon identified by &lt;code&gt; expired on &lt;expired_on&gt;.
- When the shopper applies the coupon.
- Then the coupon is rejected.
- And the error message is &lt;message&gt;.
- The Examples table includes SPRING10 with 2026-03-31 and Coupon expired.
- The Examples table includes SAVE20 with 2026-04-30 and Coupon expired.

### REQ.member-discount-scenario - Member discount scenario

- Type: REQ
- Status: accepted
- Level: MUST
- Verification: acceptance-test
- Tags: bdd, example, given-when-then, scenario

Statement:

The member-discount Example must preserve the full Given/When/Then/And flow: a signed-in member with an eligible 100.00 cart, checkout total calculation, a 10 percent member discount, and final total 90.00.

Acceptance:

- Given a signed-in member has an eligible cart totaling 100.00.
- When the checkout total is calculated.
- Then a 10 percent member discount is applied.
- And the final total is shown as 90.00.

### REQ.shipping-address-arguments - Shipping address step arguments

- Type: REQ
- Status: accepted
- Level: MUST
- Verification: acceptance-test
- Tags: bdd, data-table, doc-string, given-when-then, scenario

Statement:

The shipping-address Scenario must preserve the doc string address, line item data table, order-validation trigger, and retention outcomes for both step arguments.

Acceptance:

- Given the shopper provides a shipping address doc string containing 100 Market Street and Springfield, CA 90000.
- And the cart contains a line item data table with sku and quantity columns.
- The line item data table includes SKU-1 with quantity 2.
- The line item data table includes SKU-2 with quantity 1.
- When the order is validated.
- Then the shipping address doc string is retained.
- And the line item data table is retained.

## Relations

- CONSTRAINT.gherkin-structure clarifies REQ.expired-coupon-outline
  - Rationale: The structure guidance explains why scenario outlines are encoded as REQ items.
- CONSTRAINT.gherkin-structure clarifies REQ.member-discount-scenario
  - Rationale: The structure guidance explains why scenarios are encoded as REQ items.
- CONSTRAINT.gherkin-structure clarifies REQ.shipping-address-arguments
  - Rationale: The structure guidance explains why argument-heavy scenarios are encoded as REQ items.
- REQ.expired-coupon-outline satisfies GOAL.behavior-readable-discounts
  - Rationale: The scenario outline provides parameterized behavior examples for checkout discounts.
- REQ.member-discount-scenario satisfies GOAL.behavior-readable-discounts
  - Rationale: The scenario provides a concrete behavior example for checkout discounts.
- REQ.shipping-address-arguments satisfies GOAL.behavior-readable-discounts
  - Rationale: The scenario preserves step arguments that downstream agents must understand.

<!-- agileforge-review-notes:start -->
<!-- agileforge-review-notes:end -->
