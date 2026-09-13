# karb

A structural arbitrage scanner for Kalshi event contracts.

karb reads Kalshi's public market data and looks for baskets of contracts whose payout exceeds
their cost in *every* way an event can settle. Each basket is sized against real order-book
depth, priced with Kalshi's exact fee rounding, and verified in integer arithmetic across every
settlement state before it is shown.

Python 3.12 · httpx · pydantic · SciPy (HiGHS) · Typer + Rich · public data only, no credentials,
no order placement.

## Status

**Milestone 1 complete**: the live scanner. See [docs/architecture.md](docs/architecture.md) for
the design and [docs/decisions/](docs/decisions/) for the reasoning behind each choice.

| Milestone | Scope | Status |
|---|---|---|
| 1 | Exact units, fee rounding, strike intervals and outcome atoms, basket LP with exact verification, resilient REST client, three-tier scanner, CLI | ✅ Done |
| 2 | Recorder: DuckDB snapshots of watchlist books and opportunities, replay, frequency/lifetime/capacity statistics | Planned |
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

## How it works

1. **Discovery** (every 10 min): `GET /series` for fee schedules, `GET /events` for every open
   event, and classification into interval events (strike ladders and ranges) or categorical
   events (mutually exclusive outcomes). Everything else is excluded with a stated reason.
2. **Screen** (every 90 s): top-of-book quotes, and integer checks of the necessary conditions.
   Depth only worsens prices and fees only add cost, so anything that fails here cannot be an
   arbitrage.
3. **Confirm** (continuously): whole events' books via `GET /markets/orderbooks` (100 markets
   per request, one near-simultaneous snapshot), the basket LP, exact verification, and
   tracking of what persists across snapshots.

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
- **Eligibility.** Trading on Kalshi requires US KYC. This project is research-only by design and
  contains no code capable of placing an order.
