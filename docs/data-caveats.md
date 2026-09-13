# Data caveats

What karb assumes, where each assumption came from, and what would break if it were wrong.
Everything here was checked against the live API and documentation on 2026-09-13.

## Fees

- **Taker coefficient 0.07 (×1 multiplier on most series).** Kalshi's fee schedule PDF was
  unreachable from the development network (bot wall, then connection refused). The coefficient
  is instead corroborated by Kalshi's own [fee rounding](https://docs.kalshi.com/getting_started/fee_rounding.md)
  worked example: a $0.055 buy has a model fee of $0.00363825, which is exactly
  0.07 × 1 contract × 0.055 × 0.945. It is configurable (`--taker-coefficient`), and the unit
  test pins that example.
- **Fee types.**
  - `quadratic`, `quadratic_with_maker_fees` and `quadratic_with_combo_maker_fees` share the
    taker curve. They differ only in maker fees, which are irrelevant to a taker-only scanner.
  - `flat` refers to a table that could not be read, so events using it are excluded.
  - On 2026-09-13, `GET /series` listed 13,850 quadratic, 160 quadratic_with_maker_fees, 3 combo
    and 0 flat series.
- **Event overrides.** `fee_type_override` and `fee_multiplier_override` are honoured. If only
  one of the two is set, the event is excluded.
- **Fee waivers are ignored.** A waived fee would make real edge larger than reported, never
  smaller.
- **Rounding.** The default `worst_case` mode aligns every fill against the trader at $0.01 and
  never assumes a rebate. Two modes are more generous and closer to reality:
  - `--direct-member` aligns balances at $0.0001.
  - `--rounding accumulator` simulates the documented rebate accumulator.
- **Fee schedule changes** (`/series/fee_changes`) are not tracked. Series metadata is reloaded
  on every discovery, so a change is picked up within one discovery interval.

## Strikes and outcomes

- **Strictness comes from `strike_type`.**
  - `greater` means X > floor and `greater_or_equal` means X ≥ floor.
  - `less` means X < cap and `less_or_equal` means X ≤ cap.
  - `between` means floor ≤ X ≤ cap.
  - Confirmed against the subtitles of one live S&P range ladder (KXINX-26SEP14H1600): `less`
    cap 7225 is "7,224.9999 or below", and `greater` floor 7924.9999 is "7,925 or above".
  - `karb audit` prints each market's interval beside its subtitle so this can be re-checked
    for any event.
- **One settlement value per ladder is checked, not assumed.** The first full-universe run
  treated every strike market in an event as a strike on one number and "verified" 637 phantom
  baskets. The largest was a "guaranteed" $34,122 on NFL receiving-yards props. The events
  involved included:
  - thirteen players' ladders listed in one event;
  - spreads for opposite teams with identical strikes;
  - one strike reused for different deadlines.

  karb now handles these cases as follows (ADR-0006):
  - it splits interval events by `custom_strike` participant;
  - it excludes events whose markets settle at different times;
  - it excludes events where two markets share a YES-interval;
  - it excludes events where two brackets touch at a single point.

  Markets with the same participant and settlement time could still, in principle, measure
  different quantities that no API field distinguishes. Treat an implausibly large verified
  edge as a bug report until `karb explain` shows otherwise.
- **`between` bounds are not always inclusive.** KXHOUSEPOPVOTEMARGIN-27NOV03 writes its brackets
  as [0, 2] and [2, 4] in a mutually exclusive event, so an unstated half-open convention must
  apply there. Brackets that touch at one point are excluded rather than guessed.
- **The STRUCTURAL tier infers a settlement grid.** Kalshi writes brackets as
  [7225, 7249.9999] and [7250, 7274.9999], leaving an uncovered open gap (7249.9999, 7250). A
  gap is dropped only when it meets all three conditions:
  - no market covers it;
  - both of its endpoints are covered;
  - it is exactly one grid step wide, where the grid is taken from the most decimals any strike
    uses.

  If a settlement source could produce 7249.99995, a STRUCTURAL underround would not be
  risk-free. That is why the tier is labelled separately.
- **Exhaustiveness of categorical events is unknowable from the API.** "Who will be the next DNC
  chair?" lists named people, but someone else could win. A residual outcome is always included
  unless you pass `--assert-exhaustive`.
- **Scalar, functional, structured and multivariate markets are excluded.**

## Quotes and books

- **Listing quotes encode "no bid" and "no ask" as a price of 0.0000.** A YES ask of $0 would be
  a free contract, so both are read as absent.
- **REST snapshots are not atomic.** Books for up to 100 markets arrive in one response, and
  events are packed so they are not split when they fit. Events with more than 100 markets are
  split, and the time between first request and last response is reported as *skew*.
- **An opportunity counts as confirmed only after consecutive sightings** (default 2). Its
  reported lifetime measures how long it was *observable*. It does not show that it was
  *executable*: someone faster may have taken it within milliseconds.
- **Crossed books are data errors.** Best YES bid plus best NO bid ≥ $1 cannot persist on a live
  matching engine, so the market is dropped from that snapshot and the issue is reported.
- **Sizing is in whole contracts.** Kalshi supports 0.01-contract granularity; whole contracts
  are conservative.

## Access and network

- **WebSockets require authentication** (an unauthenticated handshake returns
  `401 token_authentication_failure`), so karb polls REST.
- **Connections from the development network reset often** (HTTP 000 before any response, in
  some bursts for most requests). Retries with jittered exponential backoff are built into the
  client.
- **Unauthenticated rate limits are undocumented.** Kalshi publishes token budgets for
  authenticated tiers only. The default of 8 requests/s produced no 429s during development.
- **The universe is large.**
  - **Probing.** Early probing on 2026-09-13 counted at least 20,000 open non-multivariate markets
    and 8,000 open events before hitting its pagination cap. Sixty sequential pages took 116 s.
  - **Full discovery.** The complete discovery later that day found 13,467 open events. Splitting
    by participant turned them into 15,959 groups, of which 12,119 were scannable: 7,357 interval
    and 4,762 categorical, with 172 carrying a STRUCTURAL tier.

## Capital and settlement

- **Capital is gross cash outlay.**
  - Kalshi's collateral return (MECNET/DIRECNET netting) can release collateral early.
  - However, it is off by default, locked per event at the first order, and can block selling.
  - Netted capital is therefore shown only as a sensitivity column.
- **APR runs to the latest expiration among a basket's markets.** Early settlement is not
  assumed.
- **Exchange-rule risk is not modelled.** Voided markets, disputes (`disputed`, `amended`
  statuses) and rule interpretations all fall outside the scanner. Structural arbitrage within
  one event shares one rulebook, which is the main reason it is safer than cross-venue
  arbitrage, but it is not immune.

## Eligibility

Trading on Kalshi requires US KYC. karb is research-only by design: it reads public data and
contains no code capable of authenticating or placing an order.
