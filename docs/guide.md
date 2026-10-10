# karb user guide

This guide takes you from a fresh clone to trying every feature:
1. offline;
2. against the live exchange's public data;
3. trading on Kalshi's demo exchange with mock funds.

Every number quoted in the offline tour is what the commands actually print. Each one is derived
by hand below, and `tests/integration/test_demo.py` fails if any of them drift.

**What karb does.** It reads Kalshi's market data and looks for *structural arbitrage*: a basket
of contracts in one event whose payout beats its cost in **every** way the event can settle. Each
basket is sized against real order-book depth, priced with Kalshi's exact fee rounding, and
checked in integer arithmetic in every possible outcome. Around that core karb can:
- record what it sees;
- replay recordings under different fee models;
- trade what it finds with real signed orders on Kalshi's demo exchange;
- settle trades and audit them against the exchange's own records;
- write an HTML research report.

**What karb will not do: trade real money.** Orders go only to Kalshi's demo exchange, which runs
on mock funds, or to a simulated exchange inside karb. The client refuses to sign a request for
any production host (ADR-0010).

---

## 1. Install

You need [uv](https://docs.astral.sh/uv/) and git. uv fetches Python 3.12 if you don't have it.
Sections 1–4 need no account and no keys; only section 5 needs a (free) Kalshi demo account.

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

You should see `254 passed, 2 deselected` (the deselected two are the live tests in §7).

Lint, format and type checks run exactly as CI runs them:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

## 3. The offline tour: `karb demo`

`karb demo` runs the real scanner, recorder, trader and settlement code against a small simulated
exchange:
- the trader sends real signed orders;
- the simulated exchange checks the signatures, matches the orders against its order books,
  charges Kalshi's fees and keeps an account.

It finishes in a second or two and writes `data/demo.duckdb`, which the rest of this section
explores. The simulated exchange lists three events:

| Event | What is wrong with its prices | What karb should do |
|---|---|---|
| `DEMO-WINNER` | Three mutually exclusive outcomes A, B, C, each with YES bid at $0.40, which sums to $1.20. At most one can win, so the bids should sum to $1 or less. | Buy NO on all three: an **overround** basket. |
| `DEMO-LADDER` | "Above 20" is bid at $0.60, but "above 10" is offered at $0.50. "Above 20" implies "above 10", so it can never be worth more. | Buy YES "above 10" and NO "above 20": a **monotone** basket. |
| `DEMO-RANGE` | Nothing. Three brackets priced consistently: bids sum to $0.90 and asks to $1.08. | Find nothing. This is the control. |

Two twists test the trader:
- The moment its first DEMO-WINNER order lands, someone takes every YES bid on outcome C, so NO on
  C can no longer be bought.
- Two simulated months later the markets settle: **B wins**, and the ladder's underlying settles
  at **25**.

### 3.1 Build the demo

```bash
uv run karb demo
```

```
demo recording written to data/demo.duckdb
  run 20260105T150000Z-…: 3 cycles over 3 simulated events
  trading session 20260105T150000Z-…: 2 trades, 2 settled, realized -$6.46
  simulated account balance $9993.54 (from $10,000.00)
```

The simulated account fell by exactly the realized P&L: the exchange's books and karb's agree
to the cent. Run it again with `--overwrite` to start fresh. `--overwrite` refuses to delete
anything that isn't a karb recording.

### 3.2 See the recorded run

```bash
uv run karb runs --db data/demo.duckdb
```

You should see one run with 3 cycles, 9 observations (three groups, three cycles each) and
**2 live opportunities**. Each basket was seen once, in the first cycle. Its trade then took the
size behind it, so the next two cycles no longer showed it.

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

`--min-apr` is the annualised hurdle, applied as `edge ÷ years until expiry`:
- The winner earns 8.1% over the 54 days to expiry, about **54% a year**, and passes.
- The ladder earns 7.0%, about **47% a year**, and is filtered out.

On the real exchange this filter matters (see [research.md](research.md)): the edges that survive
fees tend to sit in markets that settle years from now.

### 3.4 Statistics: how often, how long, how large

```bash
uv run karb stats --db data/demo.duckdb
```

- **Funnel:** 2 snapshots LP-positive, 2 verified, **2 distinct episodes**.
- **Lifetime:** each episode was seen once, so its observed lifetime is 0 s. None was still live
  at the end, because karb's own trades removed them. Trading runs cut lifetimes short; measure
  lifetimes with `karb scan --record`, which never trades.
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

### 3.6 Trading and P&L attribution

`karb demo` traded both opportunities as they were found, one at a time, then settled them. See
the trades:

```bash
uv run karb pnl --db data/demo.duckdb
```

| Trade | Planned | Execution | Hedging | Outcome | Realized | Filled |
|---|---|---|---|---|---|---|
| DEMO-LADDER, MONOTONE | $1.96 | $0.00 | $0.00 | $0.00 | **$1.96** | 100% |
| DEMO-WINNER, OVERROUND | $7.48 | −$19.16 | +$3.26 | $0.00 | **−$8.42** | 67% |

The two rows sum to a realized total of **−$6.46**. Each row splits its result into four parts
that add up exactly: planned + execution + hedging + outcome = realized.

The ladder trade's two orders filled exactly as planned, and the market settled at 25. YES
"above 10" paid $30 and NO "above 20" paid nothing: $30 for $28.04.

The winner trade is the instructive one:

1. **Planned:** +$7.48 on the decision book.
2. **Execution, −$19.16:** all three orders go out in one batch. NO A and NO B fill ($61.68), but
   C's YES bids vanish just before C's order is matched, so NO C fills nothing. If A or B wins,
   only one NO pays $50: $50 − $61.68 = **−$11.68**.
3. **Hedging, +$3.26:** karb fetches the books again and repairs the half-filled basket. The
   repair uses the same linear programme that found it, started from the position's payoff. The
   cheapest repair is to buy YES A and YES B at $0.45 ($23.37 each with fees). The position then
   pays exactly $100 whatever happens, for $108.42 in total: **−$8.42**, the least-bad position
   the book allowed.
4. **Outcome, $0.00:** B wins. NO A pays $50 and YES B pays $50, $100 as guaranteed. The outcome
   column is what settlement paid above the guaranteed worst case. It can never be negative if
   karb read the contracts correctly. A negative value is flagged **MODEL VIOLATION**.

A partial fill can turn a riskless plan into a loss, and the attribution shows exactly where it
happened.

### 3.7 What the exchange said

```bash
uv run karb pnl --db data/demo.duckdb --orders
```

This lists every order as the exchange recorded it:
- the exchange's order id;
- contracts ordered and filled;
- cash out;
- **fees charged next to the fees karb's model predicts for the same fills**.

They match to the micro-dollar: $0.53 and $0.51 on the ladder, $0.84 and $0.87 per leg on the
winner. Kalshi keeps one signed position per market, so buying YES on A and B, where karb held NO,
closed those positions at once and returned **$100.00**. The caption shows it with the balance
change of −$8.42. The model keeps holding both sides until settlement, where they pay the same
$100, so the two accounts agree.

After each trade, karb checks fees, balance and positions against the fills. A disagreement halts
trading (§5.6). Settlement is cross-checked too: the exchange's settlement records must match
the model's payout once the netted cash is counted.

### 3.8 The research report

```bash
uv run karb report --db data/demo.duckdb --out data/demo-report.html
```

Open `data/demo-report.html` in any browser; it needs no network. You will find:
- headline figures;
- a funnel from snapshots to trades;
- fee sensitivity;
- a histogram of opportunity lifetimes;
- a waterfall from the planned $9.44 to the realized −$6.46;
- the trade table;
- screen hits;
- the method.

Every chart has a table view, and tooltips work with the keyboard. The page follows your light or
dark theme.

---

## 4. Live use: the real exchange, read only

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

### 4.6 A research recording

Record an hour of the full universe:

```bash
uv run karb scan --record data/research.duckdb --duration 3600
```

Then study the run:

```bash
uv run karb stats --db data/research.duckdb
```

```bash
uv run karb sensitivity --db data/research.duckdb
```

```bash
uv run karb report --db data/research.duckdb
```

A recording survives network outages: failed background refreshes are retried with backoff, and
the outage count shows on the status line. DuckDB allows one writer per file, so stop a recording
before running analysis commands on the same file.

---

## 5. Trading on Kalshi's demo exchange

The demo exchange is Kalshi's test environment. It has:
- the same API as the real exchange;
- its own markets and accounts;
- **mock funds**.

karb trades only there. This section needs about ten minutes of setup.

### 5.1 Create a demo account and an API key

You do these two steps yourself, on Kalshi's site:

1. Sign up at [demo.kalshi.co](https://demo.kalshi.co). It is separate from a real Kalshi account
   and holds play money.
2. In the demo site's account settings, create an **API key**. Kalshi shows a **key id** and
   offers a **private key file** to download. Save the file; it cannot be downloaded again. See
   Kalshi's [API keys guide](https://docs.kalshi.com/getting_started/api_keys.md).

Keep the private key **outside this repository**, for example in a `.kalshi` folder in your home
directory. (`*.pem` and `*.key` files are git-ignored anyway.)

### 5.2 Tell karb where the key is

karb reads two environment variables and nothing else: no key on the command line, nothing
written to disk.

In PowerShell:

```powershell
$env:KALSHI_DEMO_KEY_ID = "your-key-id"
```

```powershell
$env:KALSHI_DEMO_KEY_FILE = "$HOME\.kalshi\demo-key.pem"
```

In bash or zsh:

```bash
export KALSHI_DEMO_KEY_ID="your-key-id"
```

```bash
export KALSHI_DEMO_KEY_FILE="$HOME/.kalshi/demo-key.pem"
```

These last for the terminal session. Put them in your shell profile to keep them.

### 5.3 Check the account

```bash
uv run karb account
```

This prints the key id prefix and the key type, the demo balance, open positions and recent
fills. An HTTP 401 here means the key id, the key file or your computer's clock is wrong (§8).

### 5.4 One order

Find a demo market. `--demo` points the read-only commands at the demo exchange, whose events
differ from the real one's:

```bash
uv run karb events --demo --series KXNEWPOPE
```

```bash
uv run karb audit --demo KXNEWPOPE-70
```

The audit lists each market's ticker and its current bid and ask. Pick one with a YES ask and buy
one contract with an immediate-or-cancel order. It fills now, at your limit or better, or not at
all, so set the limit at or above the ask:

```bash
uv run karb order MARKET_TICKER --side yes --qty 1 --limit 0.30
```

karb asks before sending. It then shows the exchange's order id, each fill, and the fee charged
next to what its own fee model predicts. A limit below the ask is simply cancelled unfilled. Run
`karb account` to see the new position.

### 5.5 Exercise the whole path: `--exercise`

The exercise shows the full multi-leg path on demand, on a basket whose payout is known in
advance: one **complete set**, a group of legs that pays out exactly once whatever happens, so
exactly $1 per set. karb tries two kinds, in order:
1. YES on brackets that partition the outcomes: a ladder's brackets and both tails.
2. YES **and** NO on the one market where that pair is cheapest. Demo ladders rarely quote every
   bracket, so this is the usual case there. Kalshi nets the pair at once and returns the $1,
   which exercises the netting reconciliation.

```bash
uv run karb trade --exercise KXNEWSCOTUSCONF-29JAN20 --sets 1
```

Any open demo event works if some market in it quotes both sides; karb explains when none does.
karb prints the orders and their exact cost before asking. A complete set usually costs more than
$1, so the guaranteed P&L is a small, known loss; it is mock money. The trade shows as kind
`EXERCISE`. Then karb:
- sends every leg at once;
- repairs any leg that fails to fill;
- reconciles fees, balance and positions with the exchange;
- records the trade in `data/trading.duckdb`.

### 5.6 Scan and trade

```bash
uv run karb trade --max-trades 5 --duration 600
```

This scans the demo exchange's markets continuously and trades every verified opportunity on its
first sighting, one trade at a time:

1. It checks the account: no position already held in the basket's markets, and enough cash.
2. It sends every leg at once as immediate-or-cancel buys, and reads back the exact fills.
3. If the legs filled unevenly, it fetches fresh books and sends the repair the LP finds.
4. It audits the trade against the account.

The scan and every order are recorded in `data/trading.duckdb` (`--db` to change it).

**Expect plenty of "arbitrage" on the demo exchange.** Its books are thin and quoted by test
traders, so their prices are often inconsistent, some absurdly so. Trading them shows the machinery
working. It says nothing about the real market, where an hour turned up one opportunity worth
having in theory and none in practice ([research.md](research.md)).

Limits and stops:
- `--max-trade-cost` is the budget per basket; the default is $100. Larger baskets are scaled
  down.
- `--capital` is the total that open positions may tie up; the default is $10,000.
- `--max-trades N` stops trading after N trades. The scan carries on.
- `--no-hedge` holds whatever fills instead of repairing it.
- `--min-apr` and `--min-profit` filter as in §4.4.
- **Halts.** Trading stops for the rest of the session, while the scan continues, when:
  - an order error cannot be resolved;
  - the exchange charges more fees than the model allowed;
  - positions disagree with the fills;
  - the stop file exists. Create `data/STOP` (or `--stop-file PATH`) to halt trading at once.
- **Ctrl+C** stops the scan, but karb first finishes the trade in flight, so it never abandons a
  half-filled basket mid-repair.

### 5.7 Review and settle

```bash
uv run karb pnl --db data/trading.duckdb --orders
```

```bash
uv run karb report --db data/trading.duckdb
```

When the demo markets settle, run:

```bash
uv run karb settle --db data/trading.duckdb
```

This settles every trade whose markets have finalized and runs two checks:
- the payout against the model's guarantee (a shortfall is a **MODEL VIOLATION**);
- the payout against the exchange's own settlement records, which must agree.

### 5.8 What protects you

- **No real money, by construction.** The client refuses to sign a request for any production
  Kalshi host, even if told to allow one, and a test pins that rule.
- **The key stays out of everything karb writes.** It is read from the key file when needed and
  never logged, printed or stored.
- **Orders are never doubled.** Every order carries an id derived from its trade, phase and leg.
  If a connection drops after the exchange accepted an order, the retry is refused as a duplicate
  and karb looks up the original instead.
- **Nothing rests on the book.** Every order is immediate-or-cancel.

---

## 6. Feature checklist

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
| Signed orders, repair, attribution | `karb pnl` | −$19.16 execution and +$3.26 hedging on DEMO-WINNER |
| Fees checked against the exchange | `karb pnl --orders` | fees equal model fees on every order |
| Netting reconciled | `karb pnl --orders` | $100.00 returned at once, balance −$8.42 |
| Settlement audit | `karb pnl` | outcome $0.00 and no MODEL VIOLATION |
| Demo-exchange account | `karb account` | your demo balance and positions |
| Demo-exchange trading | `karb trade --exercise EVENT` | a recorded trade with exchange order ids |
| Production refused | `tests/unit/test_auth.py` | every production host is refused, even when allowed |
| HTML report | `karb report` | the waterfall from $9.44 to −$6.46 |
| Survives network loss | `tests/integration/test_scanner_resilience.py` | discovery fails ten times, scanning continues |

## 7. The test suite

| Layer | Where | What it proves |
|---|---|---|
| Property tests | `tests/property/` | the guarantees listed below hold for randomly generated events and books |
| Hand-priced units | `tests/unit/test_detect.py`, `test_trading_plan.py` | every expected cost and P&L is derived in a comment |
| Signing and the wire | `tests/unit/test_auth.py`, `test_orders.py` | signatures verify; NO is sent as an ask at the complement; production is refused |
| Live-shape regressions | `tests/unit/test_underlyings.py` | the contract misreadings found on live data (ADR-0006) stay fixed |
| Golden data | `tests/golden/` | recorded live Kalshi data scans into a fixed summary |
| Integration | `tests/integration/` | client failure modes, recording, replay, trading against the simulated desk (lost responses, fee and position audits, stops), settlement, report, CLI, demo, outages |

The property tests are the core guarantees:
- **No false positives.** When one probability distribution explains every quote, nothing is
  reported, even with fees off.
- **Soundness.** Every reported basket pays its guarantee in every outcome, re-derived from the
  strike rules directly.
- **Settlement matches the model.** Settling every market as an outcome dictates pays exactly
  what the outcome model says.
- **The exchange's account matches the attribution.** Orders on an unchanged book fill as planned,
  and the balance moves by exactly the cost.
- **Repairs never hurt.** A repair executed on the book it was decided on never lowers the worst
  case.

Two smoke tests hit the real public API and are opt-in:

```bash
uv run pytest -m live -q
```

CI (`.github/workflows/ci.yml`) runs lint, format, type checks and the offline suite on every
push to `main` and on every pull request.

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `set KALSHI_DEMO_KEY_ID and KALSHI_DEMO_KEY_FILE …` | the trading commands need your demo key | §5.1–5.2 |
| `the private key is not an unencrypted PEM key` | wrong file, or a key with a passphrase | use the file Kalshi gave you, unmodified |
| HTTP 401 from `account` or `trade` | wrong key id, a key from the real exchange, or your clock is off | check both variables; demo and real keys differ; sync your system clock (signatures carry the time) |
| `refusing to sign a request to …` | something pointed karb at a non-demo host | expected: karb trades on the demo exchange only |
| `TRADING HALTED: …` | an audit found the exchange disagreeing with the model, or the stop file exists | read the reason; `karb pnl --orders` shows the trade; delete `data/STOP` if you created it |
| `cannot open …: … (is a recording still writing to it?)` | DuckDB allows one writer per file | stop the recording, or copy the file and read the copy |
| `requests … (retries N, resets M …)` with large N | the connection to Kalshi resets often; the client retries | nothing; persistent failures show as outages |
| `Kalshi API unavailable` from a one-shot command | the network is down | retry later; continuous scans keep going by themselves |
| No opportunities on the real exchange | an efficient market | normal; try `karb sensitivity` to see what fees hide |
| `MODEL VIOLATION` in `pnl` | the exchange settled in a way karb's outcome model ruled out | treat it as a bug in contract reading; open `karb explain` on the event |

## 9. Command reference

| Command | Purpose |
|---|---|
| `karb scan` | discover, screen and confirm on the real exchange, read only; `--once`, `--series`, `--json`, `--record`, `--duration`, `--min-apr` |
| `karb universe` | classify open events; show what is excluded and why |
| `karb events --series S` | list open events in a series, with classification; `--demo` |
| `karb audit EVENT` / `karb explain EVENT` | how one event is modelled / its best basket, fill by fill; `--demo` |
| `karb history EVENT` | top-of-book screens over one-minute candles |
| `karb runs` | list recorded runs |
| `karb replay` | re-run detection over a recording; `--save`, fee overrides, `--min-apr` |
| `karb stats` | funnel, lifetimes and capacity of a recorded run |
| `karb sensitivity` | the same books under five fee models |
| `karb account` | demo balance, positions and recent fills |
| `karb order TICKER` | one immediate-or-cancel buy on the demo exchange; `--side`, `--qty`, `--limit` |
| `karb trade` | scan the demo exchange and trade it; `--exercise EVENT`, `--max-trades`, `--max-trade-cost`, `--capital`, `--no-hedge`, `--stop-file`, `--duration` |
| `karb settle` | settle open trades against finalized results and the exchange's records |
| `karb pnl` | trades and their attribution; `--orders` |
| `karb report` | write the HTML research report |
| `karb demo` | build the offline demo recording |

`uv run karb COMMAND --help` lists every option. The design is explained in
[architecture.md](architecture.md), each decision has an ADR in [decisions/](decisions/), the
known limits are in [data-caveats.md](data-caveats.md), and results from a live session are in
[research.md](research.md).
