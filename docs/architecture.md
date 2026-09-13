# Architecture

karb answers one question, continuously: *is there a basket of Kalshi contracts, buyable right
now, that pays more than it costs in every way its event can settle?*

## Pipeline

```
Tier A · discovery · 10 min      Tier B · screen · 90 s            Tier C · confirm · continuous
────────────────────────────     ──────────────────────────────    ─────────────────────────────────
GET /exchange/status ─┐          GET /markets (listings) ─┐        GET /markets/orderbooks ─┐
GET /series ──────────┼─► classify_event                  ├─► screen_event ─► candidates ──┼─► detect
GET /events ──────────┘     │                             │                                  │    │
                            ▼                             └─► volume ranking ─► watchlist ───┘    ▼
                      EventStructure                                                OpportunityTracker
                  (outcome spaces per tier)                                                 │
                                                                                            ▼
                                                                                   CLI table / JSON
```

Discovery is expensive (tens of pages over a lossy link) but structure barely changes, so
classification is cached. Screening is cheap and runs over every eligible event. Confirmation
fetches full books only for screen hits and a watchlist, packing each event into a single
`/markets/orderbooks` request whenever it fits.

## Modules

| Package | Responsibility | Floats? |
|---|---|---|
| `karb.core` | `Price` ($1e-4), `Qty` (0.01 contract), `Cash` ($1e-6); explicit rounding; injected clock | Never |
| `karb.wire` | Pydantic models of the REST payloads; JSON decoding with exact `Decimal` numbers | Never |
| `karb.market` | Domain market/event/series; price grids from `price_ranges`; order books (bids → implied asks); fees | Never |
| `karb.structure` | Strike intervals; outcome atoms; settlement-grid inference; classification and exclusions; tradeability | Never |
| `karb.arb.screen` | Integer necessary conditions (exclusive bids > $1, covering asks < $1, containment) | Never |
| `karb.arb.lp` | The basket LP (HiGHS). Proposes quantities | Yes: proposals only |
| `karb.arb.verify` | Exact re-pricing of a proposal: fills, fee rounding, payout in every atom | Never |
| `karb.arb.detect` | Tiers → LP → whole-contract candidates → verification → opportunity | Converts at the boundary |
| `karb.arb.opportunity` | Opportunities, stable ids, capital views, sighting lifecycle | Display ratios only |
| `karb.exchange` | Async client: token bucket, jittered retries, pagination; typed endpoints | n/a |
| `karb.scanner` | The three tiers and the loops that run them | n/a |
| `karb.render`, `karb.cli` | Rich tables, JSON records, the `karb` command | Display only |

## Invariants

1. **No float touches money.** Prices, quantities and cash are integers at Kalshi's scales.
   Strikes and fee multipliers are decoded from JSON as `Decimal` and handled as exact
   rationals.
2. **Every reported number comes from `verify`.** The LP's objective value is shown by `audit`
   for diagnosis, labelled as pre-rounding, and never used as an opportunity's P&L.
3. **Every basket is checked in every atom.** Guaranteed P&L is the minimum over all
   settlement states, including the residual "none of the listed outcomes" state for
   categorical events whose exhaustiveness is unknown.
4. **Fail closed.** Unknown fee types, invalid strikes, flags that contradict strikes, crossed
   or missing books: each excludes the event or the market and says why. Nothing is guessed.
5. **Screens are necessary conditions.** A screen can only produce candidates; it can never
   produce an opportunity.
6. **One underlying per ladder.** Strike markets are reasoned about together only when they meet
   all of these conditions (ADR-0006):
   - they share a participant;
   - they share a settlement time;
   - their YES-intervals are distinct;
   - no two of them touch at a single boundary point.

   Multi-participant events are split per participant first. This invariant exists because
   the first live run broke it.

## What the tests prove

- `tests/property/test_arbitrage_properties.py`:
  - **No false positives.** When one probability distribution explains every quote, nothing is
    reported. This is checked with fees and rounding off, where the bound is tight.
  - **Planted arbitrage is found.**
  - **Soundness.** Every reported basket's payout is re-derived from the markets' strike rules
    directly, bypassing the atom machinery, and must meet its guarantee in every outcome.
- `tests/unit/test_detect.py`: planted baskets whose costs and guarantees are derived by hand in
  the comments.
- `tests/unit/test_fees.py`: Kalshi's own fee-rounding worked example, reproduced exactly.
- `tests/unit/test_underlyings.py`: every live shape that broke the one-underlying assumption,
  pinned as a regression. That covers player props, opposite-team spreads, deadline questions
  and multi-year targets.
- `tests/golden/`: recorded live data (an S&P range ladder, a BTC threshold ladder, a
  mutually exclusive custom event) scanned end to end into a deterministic summary.
- `tests/integration/test_client.py`: connection resets, 429s without Retry-After, 5xx,
  truncated bodies, cursors, and repeated query parameters.
