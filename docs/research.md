# Research: one hour of the live Kalshi universe

On Friday 2026-10-09, karb recorded the whole open Kalshi universe for one hour (16:34–17:34 UTC,
12:34–1:34 pm New York) with paper trading switched on. This page reports what it found. Every
number comes from that recording and can be regenerated with the commands at the end.

The self-contained HTML report for the same run is [research-report.html](research-report.html).
Download it and open it in a browser; GitHub shows HTML as source.

## Findings

1. **Fees are what keep Kalshi consistent.**
   - Without fees, the same books hold **191** distinct arbitrage episodes worth $611.40.
   - At half the taker fee, 40 remain.
   - At the real fee, **one** remains.
2. **The survivor is a zero-fee, eleven-year market.**
   - `KXGDPYEAR-36` ("US real GDP growth in 2036?") offered 20 complete sets of its 14 brackets
     for $19.25. They pay $20.00 at settlement, whatever the outcome.
   - That is +$0.75, or 3.9%, but only **0.35% a year**, far below what cash earns.
   - It is arbitrage in the accounting sense, not the economic one.
   - It survives because the series charges no trading fee.
3. **Rounding is the second filter.**
   - Six more groups were profitable in the solver after fees, but before whole contracts and
     Kalshi's per-fill rounding.
   - Rounding erased all six. Most were worth less than a cent.
4. **The paper trader kept the whole planned edge.**
   - It traded the survivor on first sighting. The orders arrived 10.5 s later and filled 100% at
     the planned prices.
   - A long-dated market's book barely moves.
   - The trade cannot settle before 2037.
5. **The scanner held up.**
   - 124 confirmation cycles, a median of 19.6 s apart.
   - 5,447 requests; the client retried through 572 connection resets.
   - Zero rate-limit responses, zero outages and zero feed-integrity violations.

## Setup

```bash
uv run karb scan --record data/research.duckdb --paper --duration 3600
```

Every setting was the default:

| Setting | Value |
|---|---|
| Taker fee | `ceil(0.07 × multiplier × C × P × (1 − P))` per fill, to $0.000001 |
| Fee rounding | `worst_case`: each fill's balance change aligned to $0.01 against the trader, no rebates |
| Tiers | `LOGICAL` and `STRUCTURAL`. No `ASSERTED` series |
| Confirmation | two consecutive sightings |
| Paper trading | $100 budget per basket, $10,000 capital, 1 s latency, repair on |

Kalshi's public API was reached from India.

## What the scanner saw

| | |
|---|---|
| Recording | 3,612 s; first discovery took 9.4 min, then 124 confirmation cycles over 50.5 min |
| Universe | ≈14,330 scannable groups screened per listing refresh; 17 untradeable |
| Confirmation | 151 groups per cycle on average (screen hits plus the volume watchlist) |
| Groups with full books recorded | 218, from 170 series |
| Recorded | 19,107 group snapshots, 290,452 book rows |
| Cycle spacing | median 19.6 s, 90th percentile 42.5 s; a cycle's fetches took a median 12.4 s |
| Snapshot skew | median 1.2 s, 90th percentile 4.7 s, 99th percentile 18.9 s, maximum 63.8 s |
| Network | 5,447 requests; 572 connection resets and 571 retries; 0 HTTP 429s; 1 failed fetch |
| Outages | 0. Listing refreshes ran in the background, 11 of them in 50 minutes |

Skew is the time from a snapshot's first book request to its last response. Most groups fit in
one batched request, which is why the median is about a second.

## The funnel

A listing screen checks top-of-book necessary conditions before fees. A miss is final, because
depth and fees only make a basket worse. Hits summed over every listing screen in the run:

| Screen rule | Basket it implies | Tier | Hits | Groups |
|---|---|---|---|---|
| Categorical YES bids sum above $1 | OVERROUND | LOGICAL | 583 | 71 |
| A narrower market's YES bid above a wider market's YES ask | MONOTONE | LOGICAL | 218 | 31 |
| Disjoint YES bids sum above $1 | DISJOINT / OVERROUND | LOGICAL and STRUCTURAL | 50 each | 17 |
| Covering YES asks sum below $1 | COVER / UNDERROUND | STRUCTURAL | 47 | 12 |

Confirmation priced each recorded snapshot at full depth:

| Stage | Snapshots | Groups |
|---|---|---|
| Recorded | 19,107 | 218 |
| LP-positive after fees, before rounding | 464 (310 `LOGICAL`, 154 `STRUCTURAL`) | 7 |
| Verified in exact integers, every outcome | 124 | 1 |
| Paper traded | 1 trade | 1 |

The groups the linear programme liked, and why six of them died:

| Group | Tier | Snapshots | Best LP profit | Verified |
|---|---|---|---|---|
| `KXGDPYEAR-36` | STRUCTURAL | 124 | $0.772 | **yes**, $0.75 |
| `KXBTCY-27JAN0100` | STRUCTURAL | 23 | $0.097 | no |
| `KXGOVTCUTS-28` | LOGICAL | 124 | $0.0040 | no |
| `KXCRITICSHAIR-27` | LOGICAL | 48 | $0.0025 | no |
| `KXUSDNOKAW-26OCT09` | LOGICAL | 15 | $0.0014 | no |
| `KXMIDTERMVOTETURN-FL25` | LOGICAL | 123 | $0.0002 | no |
| `KXHIGHTOKC-26OCT09` | STRUCTURAL | 7 | $0.0002 | no |

The LP prices fees linearly in fractional contracts. The verifier floors to whole contracts and
applies Kalshi's per-fill rounding. Each fill rounds up to $0.000001 and then aligns to $0.01, so
a basket with many fills pays up to a cent per fill. That is enough to erase a few cents of edge.

## The survivor: KXGDPYEAR-36

The event asks for US real GDP growth in 2036, in 14 mutually exclusive markets:
- `T0.1`, "0.0% or below";
- twelve half-point brackets from 0.1–0.5% up to 5.6–6.0%;
- `T6.0`, "6.1% or above".

Exactly one resolves YES, so a complete set of YES contracts pays $1.

| | First cycle (16:44 UTC) | Last cycle (17:34 UTC) |
|---|---|---|
| Sets bought | 20 (one YES on each of 14 markets, per set) | 20 |
| Cost, including fees | $19.25 | $19.45 |
| Guaranteed payout | $20.00 | $20.00 |
| Guaranteed P&L | **+$0.75** | +$0.55 |

The basket needed only:
- two price levels on two of the markets (`B1.8` at $0.28 and $0.29, `T0.1` at $0.10 and $0.11);
- one level everywhere else.

Its $0.0193 of fees are all balance rounding. `karb audit KXGDPYEAR-36` shows why: the series is
`quadratic ×0`, a fee multiplier of zero. The edge narrowed during the hour because the
`B4.3` ask moved from $0.04 to $0.05.

**What the guarantee rests on.** The tier is `STRUCTURAL`, not `LOGICAL`. Adjacent brackets leave
gaps, such as between 0.5% and 0.6%, and the basket assumes settlement never lands in one. That
holds because GDP growth is published to one decimal place: karb inferred a 0.1 grid from the
strikes, and every gap is exactly one grid step. If the source ever reported 0.55%, no bracket
would pay.

**Why nobody takes it.**
- **The return is too small for the wait.** The market expires on 2037-12-31, 11.2 years away.
  3.9% over 11.2 years is 0.35% a year, less than cash earns over the same period, so a trader
  with any cost of capital loses by taking it.
- **What `--min-apr` does.** It is the scanner's answer: `--min-apr 0.05` drops this basket.
- **Collateral.** The event is `MECNET`, so Kalshi's collateral-return programme might tie up
  less than the gross $19.25. karb deliberately does not count on that (ADR-0005); its APR uses
  the gross outlay.

**Persistence.** An earlier, shorter recording three hours before this one saw the same basket at
+$0.35 on $19.65. This run saw it in all 124 cycles: 3,024 s from first to last sighting, and still
there when the recording stopped. An audit twenty minutes later showed the asks still summing to
$0.96.

## What fees hide

`karb sensitivity` replays the identical recorded books under five fee models:

| Scenario | LP-positive snapshots | Verified snapshots | Episodes | Sum of best guaranteed P&L |
|---|---|---|---|---|
| As recorded | 464 | 124 | 1 | $0.75 |
| No fees, no rounding | 8,442 | 8,021 | **191** | $611.40 |
| Half the taker coefficient | 4,094 | 2,483 | 40 | $71.48 |
| Direct member ($0.0001 balances) | 464 | 130 | 4 | $0.92 |
| Rebate accumulator | 464 | 124 | 1 | $0.75 |

- **The taker fee is the binding constraint.** Kalshi's quotes are routinely inconsistent by less
  than the fee: 191 episodes across the hour without it. Halving the coefficient keeps a fifth of
  them. This is consistent with makers quoting just inside the fee.
- **Balance rounding is the second filter.** Exchange members whose balances round to $0.0001
  instead of $0.01 would see three more small episodes. All four together are worth $0.92.
- **Rebates change nothing.** The accumulator's rebates did not change any verdict here.

## Paper trading

| Session | Kind | Trades | Planned | After entry | After repair | Filled | Status |
|---|---|---|---|---|---|---|---|
| `…a4094a` | live, 1 s latency | 1 | $0.75 | $0.75 | $0.75 | 100% | open |
| `…8c79c7` | replay, next recorded cycle | 1 | $0.75 | $0.75 | $0.75 | 100% | open |

**The live session.**
- It decided on the first verified sighting.
- The orders arrived 10.5 s after the decision snapshot. That is the 1 s latency plus the rest of
  the scan cycle and the book fetch, which share one request budget.
- All 280 contracts across 14 legs filled at the planned limits, so the execution and hedging
  columns are zero.
- The 123 later sightings were not traded, because the trader takes one trade per group per run.

**The replayed session.** It ran the same lifecycle on the recording and reached the same result.

**Settlement.** `karb settle` checked both open trades across 14 markets and found none finalized.
They stay open until the market settles; its latest expiration is 2037-12-31. At settlement, the
outcome column must come out non-negative. A negative value is flagged as a model violation.

A trade that fills completely and waits eleven years is the least informative test of execution.
The demo ([guide §3.6](guide.md#36-paper-trading-and-pl-attribution)) shows what the trader does
when a leg vanishes.

## The run that failed first

The first attempt the same afternoon recorded only 14 minutes and 5 cycles. Two problems ended it:
- Listing refreshes ran in sequence before each cycle and took longer than their 90 s interval, so
  confirmation starved.
- A DNS failure during re-discovery exhausted the client's retries and ended the scan.

Both were fixed before this run (ADR-0004, amendment):
- refreshes now run in the background;
- failures become outages retried with backoff;
- a regression test reproduces the outage.

The result: 124 cycles in 50 minutes, with no outages.

## Limitations

- **One hour of one afternoon.** Activity varies with the news cycle, the time of day and the
  calendar. Episodes that open and close between cycles about 20 s apart are invisible, so
  lifetimes are lower bounds.
- **Polling, not streaming.**
  - Public data has no WebSocket feed.
  - Snapshots are consistent per batched request, but a group split across requests carries its
    measured skew, up to 64 s here.
  - The confirmation rule (two sightings) protects against stale books more than it measures
    speed.
- **Slow screening.** Listing refreshes completed about every 4.6 minutes on this link (11 in
  50 minutes) against a 90 s target. A violation outside the watchlist can wait that long to be
  screened.
- **The fee coefficient.** 0.07 comes from Kalshi's fee-rounding documentation, not the fee
  schedule. The half-coefficient row shows how much depends on it.
- **The paper-trading model.**
  - Fills assume the displayed size was real and the trader was alone at the touch.
  - No queue position, no hidden liquidity, no market impact beyond walking the book.
- **Settlement.** Both trades are open, so realized P&L is still unknown.

## Reproduce

```bash
uv run karb scan --record data/research.duckdb --paper --duration 3600
```

```bash
uv run karb stats --db data/research.duckdb
```

```bash
uv run karb sensitivity --db data/research.duckdb
```

```bash
uv run karb paper-replay --db data/research.duckdb
```

```bash
uv run karb settle --db data/research.duckdb
```

```bash
uv run karb report --db data/research.duckdb --out data/research-report.html
```

The recording is about 110 MB and is not committed; `data/` is ignored. Any new hour will differ:
the market moves on.
