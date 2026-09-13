# ADR-0005: Taker-only, hold to settlement, gross capital

**Status:** Accepted · Milestone 1

## Context

An arbitrage is only as real as the assumptions about executing and financing it:

- Resting orders on some legs would earn better prices, but they leave a half-built basket
  exposed until the rest fill (legging risk).
- Exiting before settlement assumes liquidity that thin books do not promise.
- Kalshi's collateral return (MECNET/DIRECNET netting) can hand back most of a hedged basket's
  collateral immediately. But it is off by default, locked per event at the first order, and can
  prevent selling ([help centre](https://help.kalshi.com/en/articles/13823816-collateral-return)).

## Decision

- **Taker only.** Every leg crosses the spread against displayed depth, cheapest level first.
  There is no queue position and no resting orders.
- **Hold to settlement.** A basket's value is its payout in the worst settlement atom. No exit
  price is ever assumed.
- **Gross capital.** Return on capital uses the full cash outlay, fees included.
  - Netted capital, `max(0, cost − guaranteed payout)`, is shown only for MECNET/DIRECNET events,
    as a sensitivity.
  - APR runs to the latest expiration among the basket's markets.
- **Research only.** There is no authentication and no order code. Paper execution (Milestone 3)
  will simulate fills against re-fetched books after a latency delay, in the spirit of
  `tengine`'s ADR-0007.

## Consequences

- **Reported edge is conservative on every axis:** execution, financing and exit.
- **Reported APR understates the edge** of baskets that could use collateral return. The netted
  column shows by how much.
- **Maker and legging strategies are out of scope.** They are different research questions,
  with inventory risk that structural arbitrage is meant to avoid.
