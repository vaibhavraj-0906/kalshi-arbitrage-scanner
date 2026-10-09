# karb

A structural arbitrage scanner for Kalshi event contracts, with a research kit for measuring what
it finds and a paper trader for testing whether the edge survives execution.

karb reads Kalshi's public market data and looks for baskets of contracts that pay more than they
cost in *every* way an event can settle. Each basket is:
- sized against real order-book depth;
- priced with Kalshi's exact fee rounding;
- checked in integer arithmetic in every settlement outcome before it is shown.

Scans can be recorded and replayed under any fee model. Opportunities can be paper-traded against
books fetched a latency later, held to settlement, and broken down dollar by dollar.

Python 3.12 · httpx · pydantic · SciPy (HiGHS) · DuckDB · Typer + Rich · public data only. No
credentials, and no code capable of placing an order.

**New here? Start with the [user guide](docs/guide.md).** Its offline tour exercises every feature
in a few minutes, with every expected number derived by hand.

## Status

All four milestones are complete. See [docs/architecture.md](docs/architecture.md) for the design
and [docs/decisions/](docs/decisions/) for the reasoning behind each choice.

| Milestone | Scope | Status |
|---|---|---|
| 1 | Exact units, fee rounding, strike intervals and outcome atoms, basket LP with exact verification, resilient REST client, three-tier scanner, CLI | ✅ Done |
| 2 | DuckDB recordings, exact replay through current code, frequency/lifetime/capacity statistics, fee sensitivity, candle-history screen | ✅ Done |
| 3 | Paper execution against later books, LP repair of half-filled baskets, settlement, exact P&L attribution, model-violation audit | ✅ Done |
| 4 | Self-contained HTML research report, offline demo, user guide, live research session and write-up | ✅ Done |

## Quick start

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
```

```bash
uv run pytest -q
```

Build the offline demo, then look at it:

```bash
uv run karb demo
```

```bash
uv run karb pnl --db data/demo.duckdb
```

```bash
uv run karb report --db data/demo.duckdb --out data/demo-report.html
```

Scan the live exchange once:

```bash
uv run karb scan --once
```

**Expect an empty table most of the time.** Kalshi's books are usually consistent after fees. A
scanner that reports arbitrage constantly is broken. The pass bar is zero false positives: the
property suite generates quotes explained by a probability distribution and requires the
detector to find nothing, even with fees switched off.

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
The same programme, started from a half-filled position, finds the cheapest repair (ADR-0008).

Each opportunity carries a **tier** saying what its guarantee rests on:
- `LOGICAL` holds for any outcome the event could settle to.
- `STRUCTURAL` also assumes the settlement value lies on the grid the strikes are written on.
- `ASSERTED` also assumes a mutually exclusive event's listed outcomes are complete, on your
  say-so.

## Commands

| Area | Commands |
|---|---|
| Scan | `scan` (`--once`, `--series`, `--json`, `--min-apr`, `--record`, `--paper`, `--duration`), `universe`, `events`, `audit`, `explain`, `history` |
| Research | `runs`, `replay`, `stats`, `sensitivity` |
| Paper trading | `scan --paper`, `paper-replay`, `settle`, `pnl` |
| Reporting | `report`, `demo` |

Every command is explained, with examples, in the [guide](docs/guide.md).

## Results from a live session

[docs/research.md](docs/research.md) records an hour of the full Kalshi universe with paper
trading on: about 14,300 groups screened, 19,107 snapshots priced at full depth.

- **Fees are what keep the books consistent.** Without fees the same books hold 191 arbitrage
  episodes worth $611. With them, one survives.
- **The survivor isn't worth taking.** A complete set of brackets on 2036 US GDP growth was offered
  for $19.25 against a guaranteed $20.00. That is +3.9%, but over eleven years, about 0.35% a year.
  It survives because its series charges no fee.
- **The paper trade filled in full.** Its orders arrived 10.5 s later and filled 100% at the
  planned prices.

## How it is checked

- **Exact money.** Integers at Kalshi's own scales, in memory and on disk (ADR-0001). Floats appear
  only in the LP, which proposes baskets and never prices them.
- **Property tests** for the core guarantees:
  - no false positives;
  - every reported basket pays its guarantee in every outcome;
  - settlement agrees with the outcome model.
- **Hand-priced tests** for fees, baskets, partial fills and repairs. Every expected figure is
  derived in a comment.
- **Live-data regressions** for the contract misreadings found on real markets (ADR-0006). The
  first full run "verified" 637 phantom opportunities before they were fixed.
- **Integration tests** for:
  - the client's failure modes;
  - recording and exact replay;
  - live and replayed paper trading, and settlement;
  - the report and the CLI;
  - the demo;
  - surviving a network outage mid-recording.

## Caveats

Read [docs/data-caveats.md](docs/data-caveats.md) before trusting a number. The most important:

- **Fee coefficient.** The 0.07 taker coefficient comes from Kalshi's fee-rounding documentation,
  not the fee schedule itself.
- **Bracket boundaries.** `between` brackets are not always inclusive at both ends, so brackets
  touching at a single point are excluded rather than guessed.
- **Implausibly large edges.** Treat a verified opportunity with an implausibly large edge as a
  bug in how a contract was read until `karb explain` proves otherwise. A settlement that pays
  less than the guarantee is flagged automatically.
- **Lifetimes and latency.** Recorded lifetimes are lower bounds. Replayed paper latency is the
  recording's cadence, so it is deliberately pessimistic.
- **Eligibility.** Trading on Kalshi requires US KYC. This project is research-only by design.
