# Architecture

karb answers one question, continuously: *is there a basket of Kalshi contracts, buyable right
now, that pays more than it costs in every way its event can settle?* The research layers around
that question:
- Milestone 2 asks how often, for how long, how large, and how much survives fees.
- Milestone 3 asks how much a paper trader keeps once the books move.
- Milestone 4 reports all of it ([research.md](research.md) has an hour of live results).

## Pipeline

```
Tier A · discovery · 10 min      Tier B · screen · 90 s            Tier C · confirm · continuous
────────────────────────────     ──────────────────────────────    ─────────────────────────────────
GET /exchange/status ─┐          GET /markets (listings) ─┐        GET /markets/orderbooks ─┐
GET /series ──────────┼─► split + classify                ├─► screen_event ─► candidates ──┼─► detect
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

## Recording and replay

```
karb scan --record                         karb replay · stats · sensitivity
─────────────────────                      ──────────────────────────────────────────────────────
CycleReport ─► Recorder ─► DuckDB ───────► event payload ─► split ─► classify ─┐
               one txn     (append-only)   books ──────────────────────────────┼─► detect ─┐
               per cycle        │          tradeable · time · skew · fees ─────┘            │
                                │                                                           ▼
                                └──────► live conclusions ─────────────────► compare · episodes · totals
```

A recording keeps *inputs*: the exchange's event JSON (minus per-second fields), the books, and
the per-observation facts detection used. Replays re-derive everything else with the current
code, so a recording outlives the classification rules that were in force when it was made
(ADR-0007). Detection results are cached on their exact inputs, so the unchanged books of quiet
groups are not solved twice.

## Paper trading and reporting

```
opportunity on B0 ─► plan_trade (budget, exact) ─► orders ─┐
                                                           ▼
             latency ─► B1 (re-fetched / next observation) ─► IOC fills ─► worst case W1
                                                           │
             decide repair on B1 (LP with base payoff) ────┘
             latency ─► B2 ─► repair fills ─► worst case W2 ─► paper_trades / paper_orders
                                                           │
karb settle ─► finalized results ─► payout ─► realized ────┴─► attribution + model-violation audit
karb report ─► one HTML file: KPIs, funnel, fee sensitivity, lifetimes, P&L waterfall, trades
```

The same lifecycle runs live (`scan --paper`, with sleeps and re-fetches) and over recordings
(`paper-replay`, with later observations standing in for re-fetched books). See ADR-0008 and
ADR-0009.

## Modules

| Package | Responsibility | Floats? |
|---|---|---|
| `karb.core` | `Price` ($1e-4), `Qty` (0.01 contract), `Cash` ($1e-6); explicit rounding; injected clock | Never |
| `karb.wire` | Pydantic models of the REST payloads, candles included; JSON decoding with exact `Decimal` numbers | Never |
| `karb.market` | Domain market/event/series; price grids from `price_ranges`; order books (bids → implied asks); fees | Never |
| `karb.structure` | Strike intervals; outcome atoms; settlement-grid inference; participant splitting; classification and exclusions; tradeability | Never |
| `karb.arb.screen` | Integer necessary conditions (exclusive bids > $1, covering asks < $1, containment) | Never |
| `karb.arb.lp` | The basket LP (HiGHS). Proposes quantities | Yes: proposals only |
| `karb.arb.verify` | Exact re-pricing of a proposal: fills, fee rounding, payout in every atom | Never |
| `karb.arb.detect` | Tiers → LP → whole-contract candidates → verification → opportunity | Converts at the boundary |
| `karb.arb.opportunity` | Opportunities, stable ids, capital views, sighting lifecycle | Display ratios only |
| `karb.exchange` | Async client: token bucket, jittered retries, pagination; typed endpoints | n/a |
| `karb.scanner` | The three tiers and the loops that run them | n/a |
| `karb.store` | Exact codecs, the DuckDB recording (JSON bulk writes, schema upgrades), the recorder, cached replay, episodes and fee sensitivity | LP diagnostics only |
| `karb.paper` | Budgeted plans, IOC execution with persistent liquidity, LP repair, settlement, exact attribution; live trader and recorded replay | LP proposals and display ratios only |
| `karb.history` | Coarse historical screen from one-minute candles | Never |
| `karb.dashboard` | The self-contained HTML research report | Chart geometry only |
| `karb.render`, `karb.reports`, `karb.cli` | Rich tables, JSON records, the `karb` command | Display only |

## Invariants

1. **No float touches money.**
   - Prices, quantities and cash are integers at Kalshi's scales, in memory and in the
     recording.
   - Strikes and fee multipliers are decoded from JSON as `Decimal` and handled as exact
     rationals.
2. **Every reported number comes from `verify`.** The LP's objective value is shown by `audit`
   for diagnosis, labelled as pre-rounding, and never used as an opportunity's P&L.
3. **Every basket is checked in every atom.** Guaranteed P&L is the minimum over all
   settlement states. That includes the residual "none of the listed outcomes" state for
   categorical events whose exhaustiveness is unknown.
4. **Fail closed.** Each of these excludes the event or market and says why; nothing is guessed:
   - unknown fee types
   - invalid strikes
   - flags that contradict strikes
   - crossed or missing books
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
7. **Recordings hold inputs, not conclusions.** Replays re-derive structure and detection from
   recorded payloads and books. A replay under the recorded configuration must reproduce the live
   run exactly, and `karb replay` checks that it does.
8. **Paper fills never flatter the trader.**
   - Every stage acts on a book one step old.
   - Orders are IOC at limits set on the decision book.
   - Liquidity taken stays taken.
   - Missing or crossed books fill nothing.
9. **Settlement audits the model.** Attribution telescopes exactly to the realized P&L. A
   settlement below the guaranteed worst case is flagged as a model violation, never averaged in.

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
- `tests/integration/test_recording.py`: a scan with a planted opportunity, run end to end:
  - it is recorded;
  - it is replayed to identical baskets and exact P&L;
  - it is measured into one censored five-second episode;
  - it is replayed under each fee scenario.
- `tests/unit/test_codec.py`: decimals, books and configurations round-trip exactly, and event
  payloads hash identically regardless of quotes or market order.
- `tests/unit/test_stats.py`, `tests/unit/test_history.py`: episode boundaries and censoring;
  candles carried through quiet minutes, and both of Kalshi's missing-quote encodings.
- `tests/unit/test_paper_trade.py`: a half-filled overround priced by hand at every stage:
  - the plan is +$7.48;
  - after entry it is −$11.68;
  - after unwinding through the LP it is −$8.42.

  The file also covers budget scaling, liquidity that stays taken, and missed and flat trades.
- `tests/property/test_paper_properties.py`:
  - settling every market as any atom dictates pays exactly the atom model's payoff;
  - unchanged books execute exactly as planned, and repair never lowers the worst case.
- `tests/integration/test_paper.py`: end to end on the mock exchange.
  - Live paper trading thins one leg on arrival.
  - Settlement uses mocked final results, and waits while a held market is only `determined`.
  - Replayed paper trading covers both a missed trade and a model violation.
- `tests/unit/test_store.py`:
  - bulk writes round-trip 2⁶² and 2⁵³+1 exactly, and doubles bit for bit;
  - v1 recordings upgrade in place;
  - newer or foreign files are refused.
- `tests/integration/test_report_and_cli.py`: every report section renders, exchange labels are
  escaped, and the research commands work through the real CLI.
