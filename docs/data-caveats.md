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

## Recordings and history

- **Replay reproduces detection, not screening.** Recordings keep each confirmation's books,
  event payloads and tradeable markets, but not the Tier B listing refreshes that drive the
  screens. Recorded screen hits are reported as they happened; they cannot be recomputed under
  new rules.
- **Fees are as of discovery.** Observations store the series fee type and multiplier loaded at
  the latest discovery. A mid-run fee change appears from the next discovery.
- **Lifetimes are lower bounds, twice over.**
  - An episode is observed only when its group is confirmed: every ~5 s plus fetch time.
  - Only groups on the watchlist or hit by a screen are confirmed at all.
  - Episodes still live at their group's last observation are marked censored.
- **One writer per file.** DuckDB locks a database for writing. Stop `karb scan --record` before
  running `karb stats` against the same file, or read a copy.
- **Candles are coarse.** On 2026-09-13, `GET /markets/candlesticks` behaved as follows:
  - **Sparse.** It returned one candle per minute *with activity*; quiet minutes are simply
    absent.
  - **No sizes.** Each candle has YES bid and ask OHLC, but no sizes.
  - **Missing asks.** A missing ask is encoded as `1.0000`, where listings use `0.0000`.

  `karb history` carries each market's last close forward through quiet minutes, so a market that
  stops trading keeps its last quote. A historical hit is a pre-fee, top-of-book necessary
  condition; nothing about depth or executability can be inferred from it.
- **Settled history needs another endpoint.** Markets settled before Kalshi's historical cutoff
  are served by `/historical/markets/{ticker}/candlesticks`, which `karb history` does not use
  yet.

## Trading on the demo exchange

- **The demo exchange is not a market.** Its books are thin and quoted by test traders, so a scan
  of it verifies many "arbitrage" baskets, some with absurd edges. Trading them proves the
  machinery, not an edge, and its P&L is mock money. The real market's answer is in research.md.
- **Batches are not atomic.** Kalshi documents per-order results for a batch but not
  all-or-nothing behaviour. karb therefore treats each leg separately, and repairs whatever the
  batch left uneven.
- **Fees are measured, not assumed.** Each fill's `fee_cost` is recorded beside karb's model fee
  for the same fill. The documented per-fill rounding to whole cents is added, and the balance
  change is checked against both. If the exchange takes more than the model allowed, trading halts.
- **Netting.** Kalshi holds one signed position per market, so buying the other side of a held
  market closes pairs and returns $1 each at once. karb records that cash per trade. Settlement
  checks compare the exchange's settlement revenue plus that cash with the model's payout.
- **The account must be quiet.** The audit assumes the balance moves only because of the trade in
  flight. Trading the same demo account from elsewhere at the same time will show up as an audit
  failure and halt trading. That is deliberate.
- **Positions held elsewhere are left alone.** karb skips a basket if the account already holds
  any of its markets.
- **One trade per event group per run, one at a time.** Opportunities found while a trade is in
  flight wait their turn, so by the time they go out the book may have moved. The IOC limits cap
  the price, and the repair handles size.
- **Settlement waits for `finalized`.**
  - `determined` results can still be disputed or amended.
  - Settlement uses `settlement_value_dollars` when present, so voided or partially settled
    markets pay what the exchange paid.
  - Markets archived past Kalshi's historical cutoff (2026-08-10 when checked) are looked up in
    `/historical/markets/{ticker}`. That path is covered by a mock, not by a live archived
    market.
- **A model violation means karb's reading was wrong.** Treat it as a bug report against
  classification (see ADR-0006), not as bad luck.
- **Recordings from before Milestone 5 hold paper trades.** Those were simulated against fetched
  books and never reached an exchange. They migrate unchanged and are labelled as paper in
  `karb pnl`.

## Operations

- **DuckDB `executemany` is unusable for list columns.** On DuckDB 1.5.5 it took 44.6 s to write
  188 order books (about a quarter of a second per row), and even scalar-only rows ran at about
  10 ms each. Milestone 2 shipped with that write path; a full-universe recording could not keep
  up. Since Milestone 3 every bulk write is one JSON document unpacked by `from_json`. The same
  188 books take about 15 ms, and integers round-trip exactly.
- **Memory.** A full-universe scan holds the classified universe (about 12,000 groups) and, when
  recording, every event's structural JSON. Expect around 1 GB of resident memory.

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

## Eligibility and real money

Trading real money on Kalshi requires an eligible account (US KYC). karb does not do it: it
signs requests only for the demo exchange, refuses every production host, and has no
configuration that changes that (ADR-0010). A demo account and its API key are free.
