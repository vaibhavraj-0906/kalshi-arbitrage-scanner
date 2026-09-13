# ADR-0002: Fee model and rounding

**Status:** Accepted · Milestone 1

## Context

Fees decide whether most structural violations are real.

- **The curve peaks mid-price.** A taker pays `rate × C × P × (1 − P)`, which is 1.75¢ per
  contract at 50¢, often more than the violation itself.
- **Rounding adds more.** A basket of six legs pays that fee six times, plus rounding on each.

The inputs available:

- **Documented rounding mechanics**
  ([fee rounding](https://docs.kalshi.com/getting_started/fee_rounding.md)):
  - the trade fee is rounded up to $0.000001 per fill;
  - a rounding fee aligns the balance to $0.01 (non-direct members) or $0.0001 (direct);
  - a per-order accumulator rebates overpayment in whole balance units, capped so no fill's net
    fee goes negative.
- **Fee type and multiplier.** Each series has a `fee_type` (`quadratic`,
  `quadratic_with_maker_fees`, `quadratic_with_combo_maker_fees` or `flat`) and a
  `fee_multiplier`. Events may override both.
- **The coefficient.** The fee schedule PDF, which defines the coefficient, was unreachable. The
  fee-rounding page's worked example pins it anyway: a model fee of $0.00363825 on a $0.055 buy
  of one contract is exactly 0.07 × 0.055 × 0.945.

## Decision

- **Formula.** Taker fee per fill = `ceil_6dp(0.07 × multiplier × C × P × (1 − P))`, computed
  with exact rationals.
- **Source of the schedule.** The event's override when both override fields are present, else
  the series. Unknown types, `flat`, half-specified overrides and missing series fail closed:
  the event is excluded, and the reason is shown by `karb universe`.
- **Two rounding modes.**
  - `worst_case` (default) aligns every fill independently against the trader and assumes no
    rebates. This is an upper bound on what Kalshi charges.
  - `accumulator` simulates the documented mechanics exactly.
- **Balance unit.** $0.01 by default, the less favourable of the two documented precisions;
  `--direct-member` selects $0.0001.
- **Fee waivers** (`fee_waiver_expiration_time`) are ignored. This only ever understates edge.
- **In the LP.** The fee is linear in quantity at a fixed price, so the LP uses the pre-rounding
  rate. Rounding is applied exactly in verification.

## Consequences

- **Reported P&L is a lower bound** under the documented fee model: it is never flattered by
  rounding.
- **Some real arbitrage goes unreported.** The ones worth a fraction of a cent after rounding
  are exactly the ones a researcher should not trust anyway.
- **The coefficient is configurable** (`--taker-coefficient`). If the schedule changes, the unit
  test reproducing Kalshi's worked example will need its expectations revisited.
