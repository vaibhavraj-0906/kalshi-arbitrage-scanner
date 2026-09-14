# karb

A structural arbitrage scanner for Kalshi event contracts, and a research kit for measuring what it
finds.

karb reads Kalshi's public market data and looks for baskets of contracts whose payout exceeds
their cost in *every* way an event can settle. Each basket is sized against real order-book depth,
priced with Kalshi's exact fee rounding, and verified in integer arithmetic across every
settlement state before it is shown. Scans can be recorded and replayed to measure how often
violations appear, how long they last, how much they hold, and how much survives fees.

Python 3.12 · httpx · pydantic · SciPy (HiGHS) · DuckDB · Typer + Rich · public data only, no
credentials, no order placement.

## Status

**Milestone 2 complete**: recording, replay and research statistics on top of the live scanner.
See [docs/architecture.md](docs/architecture.md) for the design and
[docs/decisions/](docs/decisions/) for the reasoning behind each choice.

| Milestone | Scope | Status |
|---|---|---|
| 1 | Exact units, fee rounding, strike intervals and outcome atoms, basket LP with exact verification, resilient REST client, three-tier scanner, CLI | ✅ Done |
| 2 | DuckDB recordings of books and event payloads, exact replay through current code, frequency/lifetime/capacity statistics, fee sensitivity, candle-history screen | ✅ Done |
| 3 | Paper execution: latency-aware pessimistic fills, settlement tracking, P&L attribution | Planned |
| 4 | Research write-up and dashboard | Planned |

## What it finds

Every market in a Kalshi event settles off the same outcome, so the markets constrain each
other. When their prices violate those constraints by more than fees, a basket locks in a profit:

| Kind | Basket | Why it cannot lose |
|---|---|---|
| OVERROUND | NO on mutually exclusive markets whose YES bids sum above $1 | At most one resolves YES, so all but one NO pay |
| UNDERROUND | YES on markets covering every outcome, asks summing below $1 | Exactly one resolves YES |
| MONOTONE | YES on "above 10" and NO on "above 20" when "above 20" bids more than "above 10" asks | "Above 20" implies "above 10" |
| DISJOINT, COVER | The two-market cases of the above | |
| COMBO | Any other basket the solver finds | |

All of these are one optimisation (ADR-0003): a linear program over the event's settlement
states, which is profitable exactly when no probability distribution can explain every quote.

Each opportunity carries a **tier** saying what its guarantee rests on:

- `LOGICAL`: holds for any outcome the event could possibly settle to.
- `STRUCTURAL`: also assumes the settlement value lies on the grid the strikes are written on.
  Kalshi tiles ranges as `[7225, 7249.9999]`, `[7250, 7274.9999]`, …; covering the sliver
  between them requires that assumption.
- `ASSERTED`: also assumes a mutually exclusive event's listed outcomes are complete, because
  you said so with `--assert-exhaustive`.

## Quick start

Requires [uv](https://docs.astral.sh/uv/). No API keys.

```bash
uv sync --extra dev
```

Run the tests:

```bash
uv run pytest -q
```

Lint and type-check exactly as CI does:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Live smoke tests against the public API are opt-in:

```bash
uv run pytest -m live -q
```

## Run it

One full pass (discovery, screen, two confirmation snapshots), then exit:

```bash
uv run karb scan --once
```

A live table that keeps confirming the watchlist (Ctrl+C to stop):

```bash
uv run karb scan
```

Restrict to series, or emit JSON lines for downstream research:

```bash
uv run karb scan --once --series KXINX --series KXBTCD --json
```

What is scannable right now, and exactly why the rest is not:

```bash
uv run karb universe
```

How one event is modelled (intervals, outcome spaces, fees, screens, solver result), and its
best basket priced fill by fill:

```bash
uv run karb audit KXINX-26SEP14H1600
```

```bash
uv run karb explain KXINX-26SEP14H1600
```

**Expect an empty table most of the time.** Kalshi's books are usually consistent after fees;
a scanner that reports arbitrage constantly is broken. The pass bar is zero false positives:
the property suite generates quotes explained by a probability distribution and requires
the detector to find nothing, even with fees switched off.

## Research on recordings

Record every confirmation cycle to a DuckDB file. That covers the books, the event payloads
behind them, and what was found (ADR-0007):

```bash
uv run karb scan --record data/karb.duckdb
```

List recorded runs:

```bash
uv run karb runs
```

Replay the latest run. With the recorded fee model, the replay re-classifies every event with
today's code and must reproduce the live run's baskets and exact P&L; it says so when it does.
Pass `--taker-coefficient`, `--rounding`, `--direct-member` or `--min-profit` for a
counterfactual, and `--save` to keep its results.

```bash
uv run karb replay
```

How often violations appeared, how long they lasted, and how much they held:

```bash
uv run karb stats
```

The same recorded books under five fee scenarios:
- as recorded
- no fees and no rounding
- half the taker coefficient
- direct-member balance precision
- the rebate accumulator

```bash
uv run karb sensitivity
```

A coarse historical screen from one-minute candles. It sees top of book only, so it finds pre-fee
necessary conditions, never opportunities:

```bash
uv run karb history KXINX-26SEP14H1600 --hours 24
```

DuckDB allows one writer per file: stop a recording before reading the same file.

## How it works

1. **Discovery** (every 10 min):
   - `GET /series` for fee schedules and `GET /events` for every open event.
   - Events are split per participant and classified into interval events (strike ladders and
     ranges) or categorical events (mutually exclusive outcomes).
   - Everything else is excluded with a stated reason.
2. **Screen** (every 90 s): top-of-book quotes, and integer checks of the necessary conditions.
   Depth only worsens prices and fees only add cost, so anything that fails here cannot be an
   arbitrage.
3. **Confirm** (continuously):
   - Whole events' books arrive via `GET /markets/orderbooks`: 100 markets per request, one
     near-simultaneous snapshot.
   - Each snapshot goes through the basket LP, exact verification, and tracking of what persists
     across snapshots.
4. **Record** (optional): each cycle's inputs, written in one transaction, ready for exact replay.

Money is exact integers at Kalshi's own scales (ADR-0001). Floats appear only in the LP, which
proposes baskets and never prices them.

## First live run

A full pass over Kalshi on 2026-09-13:

- **Discovery:** 13,467 open events became 15,959 participant groups, of which 12,119 were
  scannable.
- **Screening:** 79 groups passed the top-of-book screens, and their books were confirmed.
- **Solving:** 8 had a positive guaranteed profit in the LP after linear fees.
- **Verification:** none survived exact fee rounding, whole-contract sizing and the $0.01
  minimum.

The first version of that run reported **637 "verified" opportunities**, including a
"guaranteed" $34,122 on NFL player props. Every one was a phantom, created by reading thirteen
players' yardage ladders (and opposite teams' spreads, and deadline questions sharing one strike)
as a single strike ladder. [ADR-0006](docs/decisions/ADR-0006-one-underlying-per-ladder.md)
records what was wrong. `tests/unit/test_underlyings.py` pins every shape found.

## Caveats

Read [docs/data-caveats.md](docs/data-caveats.md) before trusting a number. The most important:

- **Fee coefficient.** The 0.07 taker coefficient comes from Kalshi's fee-rounding documentation
  rather than the fee schedule itself.
- **Bracket boundaries.** `between` brackets are not always inclusive at both ends. Brackets
  that touch at a single point are excluded rather than guessed.
- **Contract reading.** A verified opportunity with an implausibly large edge should be treated
  as a bug in how a contract was read until `karb explain` proves otherwise.
- **Lifetimes.** Recorded lifetimes are lower bounds, observed only every confirmation cycle.
- **Eligibility.** Trading on Kalshi requires US KYC. This project is research-only by design and
  contains no code capable of placing an order.
