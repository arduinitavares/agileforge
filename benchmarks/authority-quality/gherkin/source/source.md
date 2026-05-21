# Checkout Discounts Gherkin Source

```gherkin
Feature: Checkout discounts
  Checkout should explain discount behavior through concrete Gherkin examples.

  Rule: Discounts are only applied when eligibility is established

    Example: Member discount is applied to eligible carts
      Given a signed-in member has an eligible cart totaling 100.00
      When the checkout total is calculated
      Then a 10 percent member discount is applied
      And the final total is shown as 90.00

    Scenario Outline: Expired coupons are rejected
      Given coupon "<code>" expired on "<expired_on>"
      When the shopper applies the coupon
      Then the coupon is rejected
      And the error message is "<message>"

      Examples:
        | code     | expired_on | message        |
        | SPRING10 | 2026-03-31 | Coupon expired |
        | SAVE20   | 2026-04-30 | Coupon expired |

    Scenario: Shipping address arguments are retained
      Given the shopper provides this shipping address:
        """
        100 Market Street
        Springfield, CA 90000
        """
      And the cart contains line items:
        | sku   | quantity |
        | SKU-1 | 2        |
        | SKU-2 | 1        |
      When the order is validated
      Then the shipping address doc string is retained
      And the line item data table is retained
```

Tooling should support localized Gherkin keywords when a feature file declares a
non-English language header.
