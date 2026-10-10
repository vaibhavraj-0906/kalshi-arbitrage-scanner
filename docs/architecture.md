# Architecture

karb answers one question, continuously: *is there a basket of Kalshi contracts, buyable right
now, that pays more than it costs in every way its event can settle?* The research layers around
that question:
- Milestone 2 asks how often, for how long, how large, and how much survives fees.
- Milestone 3 asks how much a trader keeps once the books move.
- Milestone 4 reports all of it ([research.md](research.md) has an hour of live results).
- Milestone 5 trades it with real signed orders on Kalshi's demo exchange (ADR-0010).

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

## Trading and reporting

```
opportunity on B0 ─► plan_trade (budget, exact) ─► IOC orders, one per leg
                                                     │  signed, batched (≤10), client_order_id
                     account check ─► POST /portfolio/events/orders/batched
                     (no position, cash)              │
                                                     ▼
                     GET /portfolio/fills ─► exact LegFills ─► worst case W1
                                                     │
                     re-fetch books ─► repair LP (base payoff) ─► repair orders ─► worst case W2
                                                     │
                     balance + positions ─► audit (fees ≤ model, balance, positions) ─► trades / trade_orders
                                                     │                                  └─► halt on disagreement
karb settle ─► finalized results ─► payout ─► realized ─► attribution, model-violation audit,
                                                          GET /portfolio/settlements cross-check
karb report ─► one HTML file: KPIs, funnel, fee sensitivity, lifetimes, P&L waterfall, trades
```

`karb trade` runs this against Kalshi's demo exchange, scanning the demo's own markets. Tests and
`karb demo` run it against `SimulatedDesk`, which answers the same signed endpoints. The
authenticated client signs only for allow-listed hosts and refuses every production host. See
ADR-0008, ADR-0009 and ADR-0010.

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
| `karb.exchange` | Async client: read and write token buckets, jittered retries, idempotent order POSTs, signed requests for demo hosts only, pagination; typed endpoints | n/a |
| `karb.scanner` | The three tiers and the loops that run them | n/a |
| `karb.store` | Exact codecs, the DuckDB recording (JSON bulk writes, schema upgrades), the recorder, cached replay, episodes and fee sensitivity | LP diagnostics only |
| `karb.trading` | Request signing; the order wire format; budgeted plans; placing, recovering and reading orders; LP repair; the audited trader; complete-set exercises; settlement with exchange cross-checks; the simulated desk | LP proposals and display ratios only |
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
8. **No real money.** Requests are signed only for allow-listed hosts, and every production
   host is refused even if allow-listed. Keys come from the environment and are never stored.
9. **The exchange's account is the arbiter.** Fills are priced from what the exchange reported.
   Every trade's fees, balance change and positions are compared with the model, and a
   disagreement halts trading rather than being reconciled away.
10. **An order is never doubled.** `client_order_id` is deterministic per trade, phase and leg, so
    retries are idempotent, and an order without a clean result is looked up before karb decides
    what it holds.
11. **Settlement audits the model.** Attribution telescopes exactly to the realized P&L. A
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
- `tests/unit/test_trading_plan.py`: a half-filled overround priced by hand at every stage, from
  fills shaped like the exchange's:
  - the plan is +$7.48;
  - after entry it is −$11.68;
  - after unwinding through the LP it is −$8.42, with $100 netted back.
- `tests/unit/test_auth.py`, `tests/unit/test_orders.py`:
  - signatures verify for Ed25519 and RSA-PSS;
  - every production host is refused;
  - NO goes out as an ask at the complement;
  - exchange fills price exactly, rounding included.
- `tests/unit/test_engine.py`: the post-trade audit (fees, balance, positions, errors) and
  complete sets for the exercise.
- `tests/property/test_trading_properties.py`:
  - settling every market as any atom dictates pays exactly the atom model's payoff;
  - orders on the decision book fill as planned and the exchange's balance reconciles;
  - a repair on its own book never lowers the worst case.
- `tests/integration/test_trading.py`: end to end against the simulated desk:
  - partial fill, repair, netting, settlement and the exchange's settlement records;
  - a dropped connection after an accepted batch, with no duplicate orders;
  - fee and position audits that halt trading;
  - stop files, budgets, balance, existing positions, Ctrl+C and the exercise.
- `tests/unit/test_store.py`:
  - bulk writes round-trip 2⁶² and 2⁵³+1 exactly, and doubles bit for bit;
  - v1 recordings upgrade in place, and v2 paper tables migrate to v3 or read through views;
  - newer or foreign files are refused.
- `tests/integration/test_report_and_cli.py`: every report section renders, exchange labels are
  escaped, and the research commands work through the real CLI.
