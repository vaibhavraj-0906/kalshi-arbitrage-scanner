# karb user guide

This guide takes you from a fresh clone to trying every feature, offline first, then against the
live exchange. Every number quoted in the offline tour is what the commands actually print. Each
one is derived by hand below, and `tests/integration/test_demo.py` fails if any of them drift.

**What karb does.** It reads Kalshi's public market data and looks for *structural arbitrage*: a
basket of contracts in one event whose payout beats its cost in **every** way the event can
settle. Each basket is sized against real order-book depth, priced with Kalshi's exact fee
rounding, and checked in integer arithmetic in every possible outcome. Around that core karb can:
- record what it sees;
- replay recordings under different fee models;
- paper-trade what it finds and settle it against real results;
- write an HTML research report.

**What karb will not do.** It never logs in and never places an order: there is no code that
could. Paper trades are simulations against fetched order books. Trading on Kalshi itself requires
US KYC.

---

## 1. Install

You need [uv](https://docs.astral.sh/uv/) and git. uv fetches Python 3.12 if you don't have it.
You don't need API keys, a Kalshi account, or Docker.

```bash
git clone https://github.com/vaibhavraj-0906/kalshi-arbitrage-scanner.git
```

```bash
cd kalshi-arbitrage-scanner
```

```bash
uv sync --extra dev
```

Every command below is run as `uv run karb …` from the repository root. `uv run karb --help` lists
them all.

## 2. Check the installation

Run the test suite. It never touches the network.

```bash
uv run pytest -q
```

You should see `211 passed, 2 deselected` (the deselected two are the live tests in §6).

Lint, format and type checks run exactly as CI runs them:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

## 3. The offline tour: `karb demo`

`karb demo` runs the real scanner, recorder, paper trader and settlement code against a small
simulated exchange. It finishes in a second or two and writes `data/demo.duckdb`, which the rest
of this section explores. The simulated exchange lists three events:

| Event | What is wrong with its prices | What karb should do |
|---|---|---|
| `DEMO-WINNER` | Three mutually exclusive outcomes A, B, C, each with YES bid at $0.40, which sums to $1.20. At most one can win, so the bids should sum to $1 or less. | Buy NO on all three: an **overround** basket. |
| `DEMO-LADDER` | "Above 20" is bid at $0.60, but "above 10" is offered at $0.50. "Above 20" implies "above 10", so it can never be worth more. | Buy YES "above 10" and NO "above 20": a **monotone** basket. |
| `DEMO-RANGE` | Nothing. Three brackets priced consistently: bids sum to $0.90 and asks to $1.08. | Find nothing. This is the control. |

Two twists test the paper trader:
- By the time its orders arrive, every YES bid on outcome C has gone, so NO on C can no longer be
  bought.
- Two simulated months later the markets settle: **B wins**, and the ladder's underlying settles
  at **25**.

### 3.1 Build the demo

```bash
uv run karb demo
```

```
demo recording written to data/demo.duckdb
  run 20260105T150000Z-…: 3 cycles over 3 simulated events
  paper session 20260105T150000Z-…: 2 trades, 2 settled, realized -$6.46
```

Run it again with `--overwrite` to start fresh. `--overwrite` refuses to delete anything that
isn't a karb recording.

### 3.2 See the recorded run

```bash
uv run karb runs --db data/demo.duckdb
```

You should see one run with 3 cycles, 9 observations (three groups, three cycles each) and
**6 live opportunities**: both baskets, in each of the three cycles.

### 3.3 Replay it, exactly and counterfactually

```bash
uv run karb replay --db data/demo.duckdb
```

The replay re-parses the recorded event data, re-classifies it with today's code, restores the
order books and runs detection again. Look for:

- `identical to the live run: same baskets, same exact costs and P&L`
- **DEMO-WINNER**, OVERROUND, LOGICAL tier: cost **$92.52**, guaranteed **$7.48**.
  - Each NO costs $0.60 (one minus the $0.40 YES bid).
  - Fifty contracts cost $30.00, plus a fee of 0.07 × 50 × 0.60 × 0.40 = $0.84, so $30.84 a leg.
  - Three legs cost $92.52.
  - Whatever happens, at least two of the NOs pay $50 each: $100 back, a $7.48 profit.
- **DEMO-LADDER**, MONOTONE: cost **$28.04**, guaranteed **$1.96**.
  - YES "above 10": $15.00 plus a $0.525 fee, rounded up to the cent: $15.53.
  - NO "above 20": $12.00 plus a $0.504 fee: $12.51.
  - Below 10, the NO pays $30. Above 20, the YES pays $30. In between, both pay $60. The worst
    case is $30 for $28.04.
- **DEMO-RANGE** never appears. Its prices are consistent, so a probability can be assigned to
  each bracket that fits every quote, and no basket can lock in a profit.

Replays also answer what-ifs on identical data:

```bash
uv run karb replay --db data/demo.duckdb --taker-coefficient 0
```

Without fees both baskets are richer: the winner makes $10.00 and the ladder $3.00.

```bash
uv run karb replay --db data/demo.duckdb --min-apr 0.5
```

`--min-apr` is the annualised hurdle, applied as `edge ÷ years until expiry`. The winner earns
8.1% over the 54 days to expiry, about **54% a year**, and passes. The ladder earns 7.0%, about
**47% a year**, and is filtered out. On the live exchange this filter matters (see
[research.md](research.md)): the edges that survive fees tend to sit in markets that settle years
from now.

### 3.4 Statistics: how often, how long, how large

```bash
uv run karb stats --db data/demo.duckdb
```

- **Funnel:** 6 snapshots LP-positive, 6 verified, **2 distinct episodes**.
- **Lifetime:** each episode was seen in all 3 cycles over 14 seconds. "Still live at end" is 2:
  both were still there when the run stopped, so 14 s is a lower bound.
- **Capacity:** best guaranteed P&L $7.48 and $1.96, $9.44 in total.

### 3.5 Fee sensitivity

```bash
uv run karb sensitivity --db data/demo.duckdb
```

The same recorded books are replayed under five fee models:

| Scenario | Sum of best guaranteed | Why |
|---|---|---|
| as recorded | $9.44 | $7.48 + $1.96 |
| no fees, no rounding | $13.00 | $100 − $90 and $30 − $27 |
| half the taker coefficient | $11.21 | |
| direct member ($0.0001 balances) | $9.45 | exactly $9.451: balances round to $0.0001, not $0.01 |
| rebate accumulator | $9.44 | these fills land on whole cents, so nothing is rebated |

### 3.6 Paper trading and P&L attribution

`karb demo` paper-traded both opportunities as they were found, then settled them. See the trades:

```bash
uv run karb pnl --db data/demo.duckdb
```

| Trade | Planned | Execution | Hedging | Outcome | Realized | Filled |
|---|---|---|---|---|---|---|
| DEMO-LADDER, MONOTONE | $1.96 | $0.00 | $0.00 | $0.00 | **$1.96** | 100% |
| DEMO-WINNER, OVERROUND | $7.48 | −$19.16 | +$3.26 | $0.00 | **−$8.42** | 67% |

The two rows sum to a realized total of **−$6.46**. Each row splits its result into four parts
that add up exactly: planned + execution + hedging + outcome = realized.

The ladder trade arrived to an unchanged book, filled exactly as planned, and settled at 25. YES
"above 10" paid $30 and NO "above 20" paid nothing: $30 for $28.04.

The winner trade is the instructive one:

1. **Planned:** +$7.48 on the decision book.
2. **Execution, −$19.16:** the orders arrive one latency later. NO A and NO B fill ($61.68), but C's
   YES bids are gone, so NO C cannot be bought. If A or B wins, only one NO pays $50:
   $50 − $61.68 = **−$11.68**.
3. **Hedging, +$3.26:** the half-filled basket is repaired with the same linear programme that
   found it, started from the position's payoff. The cheapest repair is to buy YES A and YES B at
   $0.45 ($23.37 each with fees). The position then pays exactly $100 whatever happens, for
   $108.42 in total: **−$8.42**, the least-bad position the book allowed.
4. **Outcome, $0.00:** B wins. NO A pays $50 and YES B pays $50, $100 as guaranteed. The outcome
   column is what settlement paid above the guaranteed worst case. It can never be negative if
   karb read the contracts correctly. A negative value is flagged **MODEL VIOLATION**.

A partial fill can turn a riskless plan into a loss, and the attribution shows exactly where it
happened.

### 3.7 Paper trading a recording

```bash
uv run karb paper-replay --db data/demo.duckdb
```

This runs the same trade lifecycle over the recording. A decision in one cycle fills on the
group's book from the next recorded cycle, and repairs fill on the one after. The recording holds
the scanner's view of the books, in which C's bids never vanished, so both trades fill 100%:
planned $9.44, both still **open**.

`karb demo` settles its own trades from the simulated exchange's results, but replayed trades stay
open: `karb settle` asks the real exchange, which has never heard of `DEMO-*`. Try counterfactuals
here too:

```bash
uv run karb paper-replay --db data/demo.duckdb --taker-coefficient 0 --no-hedge
```

Without fees the plans are worth $10.00 and $3.00. `--no-hedge` holds whatever fills instead of
repairing it.

### 3.8 The research report

```bash
uv run karb report --db data/demo.duckdb --out data/demo-report.html
```

Open `data/demo-report.html` in any browser; it needs no network. You will find:
- headline figures;
- a funnel from snapshots to paper trades;
- fee sensitivity;
- a histogram of opportunity lifetimes;
- a waterfall from the planned $9.44 to the realized −$6.46;
- the trade table;
- screen hits;
- the method.

Every chart has a table view, and tooltips work with the keyboard. The page follows your light or
dark theme.

---

## 4. Live use

These commands read Kalshi's public API, so they need internet access but no account. Expect an
empty opportunities table most of the time: Kalshi's books are usually consistent once fees are
counted. A scanner that reported arbitrage constantly would be broken.

### 4.1 What is scannable right now

```bash
uv run karb universe --series KXINX --series KXBTCD
```

Drop the `--series` options to classify every open event, which takes one to three minutes. You
get the scannable groups (interval ladders and categorical events) and each exclusion with its
reason and an example.

### 4.2 Find an event ticker

```bash
uv run karb events --series KXINX --series KXBTCD
```

This lists open events, for example `KXINX-26OCT09H1600`, with how karb classifies each. Use any
of them below.

### 4.3 How one event is modelled

```bash
uv run karb audit KXINX-26OCT09H1600
```

The audit shows, for each market:
- the YES interval karb derived from its strike fields, beside Kalshi's own subtitle, so you can
  check the reading;
- both quotes;
- whether it can be traded.

It also shows the outcome spaces by tier and the LP's best pre-rounding profit (zero means the
quotes are consistent). `karb explain` does the same, then prices the best basket fill by fill
and shows its payoff in every outcome.

### 4.4 Scan

One full pass: discovery, screening, then two confirmation snapshots.

```bash
uv run karb scan --once --series KXINX --series KXBTCD
```

A live table, refreshing until Ctrl+C:

```bash
uv run karb scan
```

The table columns are:
- the basket;
- cost and fees;
- the **guaranteed** P&L, meaning the worst case after every fee and rounding;
- edge and APR;
- time to expiry;
- how many consecutive snapshots it has been seen in;
- the skew between the first and last book fetch.

Useful options:
- `--json` emits JSON lines.
- `--min-apr 0.05` hides baskets earning less than 5% a year.
- `--min-profit 0.05` raises the per-basket minimum.
- `--assert-exhaustive SERIES` vouches that a categorical event lists every possible outcome.
  Without it, karb never assumes this.

### 4.5 History from candles

```bash
uv run karb history KXINX-26OCT09H1600 --hours 6
```

This runs the top-of-book screens over every minute of Kalshi's one-minute candles. Candles carry
no depth, so a hit here is only a pre-fee necessary condition, never a verified opportunity.

### 4.6 A research session: record, paper-trade, settle, report

Record an hour of the full universe, paper-trading every new opportunity:

```bash
uv run karb scan --record data/research.duckdb --paper --duration 3600
```

When it stops, it prints the paper session's trades. Days or months later, once the markets have
finalized, settle the open trades:

```bash
uv run karb settle --db data/research.duckdb
```

Then study the run:

```bash
uv run karb stats --db data/research.duckdb
```

```bash
uv run karb sensitivity --db data/research.duckdb
```

```bash
uv run karb pnl --db data/research.duckdb
```

```bash
uv run karb report --db data/research.duckdb
```

Paper options:
- `--latency` is seconds between seeing a book and the orders arriving; the default is 1.
- `--max-trade-cost` is the budget per basket; the default is $100.
- `--capital` is the total that open positions may tie up; the default is $10,000.
- `--no-hedge` holds whatever fills instead of repairing it.

A recording survives network outages: failed background refreshes are retried with backoff, and
the outage count shows on the status line. DuckDB allows one writer per file, so stop a recording
before running analysis commands on the same file.

---

## 5. Feature checklist

| Feature | Try it | What shows it works |
|---|---|---|
| Exact pricing with Kalshi's fee rounding | `karb replay --db data/demo.duckdb` | $92.52 and $7.48, matching the arithmetic in §3.3 |
| Every basket checked in every outcome | `karb explain <event>` | a payoff table whose worst row is the guaranteed P&L |
| No false positives | `karb replay` on the demo | DEMO-RANGE never appears |
| Exhaustiveness is never assumed | `karb audit KXNEXTDNCCHAIR-45` (any categorical event) | Outcome spaces: atoms = markets + 1, with 1 hole, the residual "none of the listed outcomes" |
| One underlying per ladder (ADR-0006) | `karb events --series KXNFLRECYDS` (NFL season) | player props split into per-participant groups |
| Recording and exact replay | `karb replay` | `identical to the live run` |
| Fee sensitivity | `karb sensitivity` | the five rows in §3.5 |
| Annualised hurdle | `karb replay --min-apr 0.5` | the ladder drops out and the winner stays |
| Paper execution, repair, attribution | `karb pnl` | −$19.16 execution and +$3.26 hedging on DEMO-WINNER |
| Settlement audit | `karb pnl` | outcome $0.00 and no MODEL VIOLATION |
| Replayed paper trading | `karb paper-replay` | two open trades, filled 100% |
| HTML report | `karb report` | the waterfall from $9.44 to −$6.46 |
| Survives network loss | `tests/integration/test_scanner_resilience.py` | discovery fails ten times, scanning continues |

## 6. The test suite

| Layer | Where | What it proves |
|---|---|---|
| Property tests | `tests/property/` | the guarantees listed below hold for randomly generated events and books |
| Hand-priced units | `tests/unit/test_detect.py`, `test_paper_trade.py` | every expected cost and P&L is derived in a comment |
| Live-shape regressions | `tests/unit/test_underlyings.py` | the contract misreadings found on live data (ADR-0006) stay fixed |
| Golden data | `tests/golden/` | recorded live Kalshi data scans into a fixed summary |
| Integration | `tests/integration/` | client failure modes, recording, replay, paper trading, settlement, report, CLI, demo, outages |

The property tests are the core guarantees:
- **No false positives.** When one probability distribution explains every quote, nothing is
  reported, even with fees off.
- **Soundness.** Every reported basket pays its guarantee in every outcome, re-derived from the
  strike rules directly.
- **Settlement matches the model.** Settling every market as an outcome dictates pays exactly
  what the outcome model says.

Two smoke tests hit the real public API and are opt-in:

```bash
uv run pytest -m live -q
```

CI (`.github/workflows/ci.yml`) runs lint, format, type checks and the offline suite on every
push to `main` and on every pull request.

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `cannot open …: … (is a recording still writing to it?)` | DuckDB allows one writer per file | stop the recording, or copy the file and read the copy |
| `requests … (retries N, resets M …)` with large N | the connection to Kalshi resets often; the client retries | nothing; persistent failures show as outages |
| `Kalshi API unavailable` from a one-shot command | the network is down | retry later; continuous scans keep going by themselves |
| No opportunities | an efficient market | normal; try `karb sensitivity` to see what fees hide |
| `MODEL VIOLATION` in `pnl` | the exchange settled in a way karb's outcome model ruled out | treat it as a bug in contract reading; open `karb explain` on the event |
| `--paper needs --record` | paper trades live in the recording | add `--record data/…duckdb` |

## 8. Command reference

| Command | Purpose |
|---|---|
| `karb scan` | discover, screen and confirm; `--once`, `--series`, `--json`, `--record`, `--paper`, `--duration`, `--min-apr` |
| `karb universe` | classify open events; show what is excluded and why |
| `karb events --series S` | list open events in a series, with classification |
| `karb audit EVENT` / `karb explain EVENT` | how one event is modelled / its best basket, fill by fill |
| `karb history EVENT` | top-of-book screens over one-minute candles |
| `karb runs` | list recorded runs |
| `karb replay` | re-run detection over a recording; `--save`, fee overrides, `--min-apr` |
| `karb stats` | funnel, lifetimes and capacity of a recorded run |
| `karb sensitivity` | the same books under five fee models |
| `karb paper-replay` | paper-trade a recording; `--latency-cycles`, `--max-trade-cost`, `--capital`, `--no-hedge` |
| `karb settle` | settle open paper trades against finalized results |
| `karb pnl` | paper trades and their attribution |
| `karb report` | write the HTML research report |
| `karb demo` | build the offline demo recording |

`uv run karb COMMAND --help` lists every option. The design is explained in
[architecture.md](architecture.md), each decision has an ADR in [decisions/](decisions/), the
known limits are in [data-caveats.md](data-caveats.md), and results from a live session are in
[research.md](research.md).
