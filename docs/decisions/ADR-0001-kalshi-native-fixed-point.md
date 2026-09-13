# ADR-0001: Kalshi-native fixed point

**Status:** Accepted · Milestone 1

## Context

Structural arbitrage lives in small numbers. A typical violation is a few tenths of a cent per
contract before fees. A float that reads $0.9999999 as below $1 manufactures an opportunity out
of nothing, and a scanner that does so even occasionally has told its user something false.

Kalshi's own representations set the scales:

- **Prices:** fixed-point dollar strings with up to four decimals. Grids go down to $0.0001
  ticks.
- **Quantities:** fixed-point strings with two decimals. Fractional contracts are supported
  everywhere, with a minimum of 0.01.
- **Fees:** Kalshi computes them at six decimals, since price × quantity can occupy six.

Strikes such as `7249.9999` arrive as JSON *numbers*, and a float cannot hold that value exactly.

`tengine` (ADR-0002 there) uses one scale of 9 for every asset. That would work here, but it
throws away the property that matters most: Kalshi's scales multiply exactly.

## Decision

Three integer unit types that do not mix:

| Type | Unit | Range |
|---|---|---|
| `Price` | $0.0001 | [0, 10,000] |
| `Qty` | 0.01 contract | ≥ 0 |
| `Cash` | $0.000001 | signed |

- **Exact arithmetic.** `Price.raw × Qty.raw` is exactly a `Cash.raw`: a notional never rounds.
- **Strict parsing.** Parsing rejects any value that is not exactly representable, rather than
  rounding it. Trailing zeros beyond the scale are accepted, since responses may emit six
  decimals.
- **Named rounding.** Rounding happens only through `div_round` with a named mode. Costs round
  against the trader.
- **Exact decoding.** JSON is decoded with `parse_float=Decimal`, so strikes and fee multipliers
  are exact. Multipliers become `Fraction`s.
- **Units do not mix silently.** Combining two types raises `TypeError`. `bool` and numpy
  integers are rejected as raw values.

## Consequences

- **Exact comparisons.** Screens compare integer sums against `PRICE_SCALE`; there is no
  epsilon anywhere in the money path.
- **Floats stay out of the money path.** The LP is the only float consumer, and it only
  proposes (ADR-0003).
- **Conversions are explicit.** Display and JSON output convert at the edge: `Cash.dollars()`
  and `str()`.
- **New precision fails loudly.** If Kalshi ever emits a fifth price decimal, parsing fails
  loudly instead of silently truncating.
