# karb

A structural arbitrage scanner for Kalshi event contracts. It comes with a research kit for
measuring what it finds, and a trader that executes it with real signed orders on Kalshi's demo
exchange, which runs on mock funds.

karb reads Kalshi's market data and looks for baskets of contracts that pay more than they cost
in *every* way an event can settle. Each basket is:
- sized against real order-book depth;
- priced with Kalshi's exact fee rounding;
- checked in integer arithmetic in every settlement outcome before it is shown.

Scans can be recorded and replayed under any fee model. Opportunities can be traded on the demo
exchange through Kalshi's own trading API. Each trade is then audited against the exchange's
fills, fees, balance and positions, held to settlement, and broken down dollar by dollar.

Python 3.12 · httpx · pydantic · SciPy (HiGHS) · DuckDB · Typer + Rich · cryptography.

**No real money, by construction.** Scanning reads public data. Trading signs requests with your
*demo* API key, and the client refuses to sign a request for any production Kalshi host
([ADR-0010](docs/decisions/ADR-0010-demo-exchange-execution.md)).

**New here? Start with the [user guide](docs/guide.md).** Its offline tour exercises every feature
in a few minutes, with every expected number derived by hand. Section 5 sets up demo trading.

## Status

All five milestones are complete. See [docs/architecture.md](docs/architecture.md) for the design
and [docs/decisions/](docs/decisions/) for the reasoning behind each choice.

| Milestone | Scope | Status |
|---|---|---|
| 1 | Exact units, fee rounding, strike intervals and outcome atoms, basket LP with exact verification, resilient REST client, three-tier scanner, CLI | ✅ Done |
| 2 | DuckDB recordings, exact replay through current code, frequency/lifetime/capacity statistics, fee sensitivity, candle-history screen | ✅ Done |
| 3 | Execution model, LP repair of half-filled baskets, settlement, exact P&L attribution, model-violation audit | ✅ Done |
| 4 | Self-contained HTML research report, offline demo, user guide, live research session and write-up | ✅ Done |
| 5 | Real orders on Kalshi's demo exchange: signed requests, batched immediate-or-cancel orders with idempotent retries, exact fills, fee/balance/position audits, netting, a signature-checking simulated exchange for tests; paper trading retired | ✅ Done |

## Quick start

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
```

```bash
uv run pytest -q
```

Build the offline demo (a simulated exchange that accepts real signed orders), then look at it:

```bash
uv run karb demo
```

```bash
uv run karb pnl --db data/demo.duckdb --orders
```

```bash
uv run karb report --db data/demo.duckdb --out data/demo-report.html
```

Scan the real exchange once, read only:

```bash
uv run karb scan --once
```

**Expect an empty table most of the time.** Kalshi's books are usually consistent after fees. A
scanner that reports arbitrage constantly is broken. The pass bar is zero false positives: the
property suite generates quotes explained by a probability distribution and requires the
detector to find nothing, even with fees switched off.

Trade on Kalshi's demo exchange: create a free demo account and API key, set two environment
variables ([guide §5](docs/guide.md#5-trading-on-kalshis-demo-exchange)), then:

```bash
uv run karb account
```

```bash
uv run karb trade --max-trades 5 --duration 600
```

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

## How a trade works

1. **Plan.** A verified basket becomes one immediate-or-cancel limit buy per leg, each limited
   to the worst price the decision book needed. Buying NO goes out as selling YES at the
   complementary price, the only form Kalshi's order API takes.
2. **Check the account.** No position already held in the basket's markets, and enough cash.
3. **Enter.** Every leg goes out at once in batched orders. Each order carries a deterministic
   `client_order_id`, so a request retried after a dropped connection can never fill twice.
4. **Read the fills.** Exact prices and fees come back per fill.
5. **Repair if needed.** If the legs filled unevenly, fresh books go into the same LP to find
   the cheapest repair, which is sent the same way.
6. **Audit.** The trade is checked against the exchange: fees charged against karb's fee model,
   the balance change against the costs, and positions against the fills. Any disagreement halts
   trading for the session.
7. **Settle.** At settlement the payout is checked twice: against the model's guarantee, and
   against the exchange's own settlement records.

## Commands

| Area | Commands |
|---|---|
| Scan (read only) | `scan` (`--once`, `--series`, `--json`, `--min-apr`, `--record`, `--duration`), `universe`, `events`, `audit`, `explain`, `history` |
| Research | `runs`, `replay`, `stats`, `sensitivity` |
| Demo trading | `account`, `order`, `trade` (`--exercise`, `--max-trades`, `--stop-file`), `settle`, `pnl` (`--orders`) |
| Reporting | `report`, `demo` |

Every command is explained, with examples, in the [guide](docs/guide.md).

## Results from a live session

[docs/research.md](docs/research.md) records an hour of the full real Kalshi universe: about
14,300 groups screened and 19,107 snapshots priced at full depth.

- **Fees are what keep the books consistent.** Without fees the same books hold 191 arbitrage
  episodes worth $611. With them, one survives.
- **The survivor isn't worth taking.** A complete set of brackets on 2036 US GDP growth was offered
  for $19.25 against a guaranteed $20.00. That is +3.9%, but over eleven years, about 0.35% a year.
  It survives because its series charges no fee.

The demo exchange is the opposite. Its thin test books are full of inconsistent quotes, so karb
finds and trades "arbitrage" there constantly. That shows the machinery works, not that the edge
exists.

## How it is checked

- **Exact money.** Integers at Kalshi's own scales, in memory and on disk (ADR-0001). Floats appear
  only in the LP, which proposes baskets and never prices them.
- **Property tests** for the core guarantees:
  - no false positives;
  - every reported basket pays its guarantee in every outcome;
  - settlement agrees with the outcome model;
  - an exchange's balance agrees with the attribution;
  - repairs never lower the worst case.
- **Hand-priced tests** for fees, baskets, partial fills and repairs. Every expected figure is
  derived in a comment.
- **Live-data regressions** for the contract misreadings found on real markets (ADR-0006). The
  first full run "verified" 637 phantom opportunities before they were fixed.
- **Signing tests.** Ed25519 and RSA-PSS signatures verify, and every production host is refused,
  even when explicitly allowed.
- **Integration tests** against a simulated exchange that verifies every signature. They cover:
  - full and partial fills, repairs and netting;
  - a dropped connection after an accepted batch, which must not double the orders;
  - fees above the model, and positions that don't match;
  - stop files, budgets and settlement records;
  - recording and exact replay, the report, the CLI, the demo, and surviving a network outage
    mid-recording.

## Caveats

Read [docs/data-caveats.md](docs/data-caveats.md) before trusting a number. The most important:

- **Fee coefficient.** The 0.07 taker coefficient comes from Kalshi's fee-rounding documentation,
  not the fee schedule itself. Every demo trade now checks it against the fees actually charged.
- **The demo exchange is not a market.** Profits there are mock money on unrealistic books.
- **Bracket boundaries.** `between` brackets are not always inclusive at both ends, so brackets
  touching at a single point are excluded rather than guessed.
- **Implausibly large edges.** Treat a verified opportunity with an implausibly large edge on
  the real exchange as a bug in how a contract was read until `karb explain` proves otherwise. A
  settlement that pays less than the guarantee is flagged automatically.
- **Lifetimes and latency.** Recorded lifetimes are lower bounds, and a trading run cuts them
  shorter still, because its own orders take the size.
- **Real money.** Trading on Kalshi itself needs an eligible account (US KYC). karb does not
  trade real money and has no code path that could.
