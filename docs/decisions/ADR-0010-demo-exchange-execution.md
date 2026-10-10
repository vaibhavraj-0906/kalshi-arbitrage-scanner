# ADR-0010: Real orders on Kalshi's demo exchange

**Status:** Accepted · Milestone 5. Replaces the simulated fills of ADR-0008 and ends "public
data only" for trading. Scanning still reads public data.

## Context

Milestones 1–4 found and measured structural arbitrage, and "paper traded" it by walking fetched
order-book snapshots. The fills were always assumptions: no exchange ever saw an order.

The goal changed to a working model that trades through Kalshi's actual trading API, without
real money. Kalshi runs a **demo exchange** for exactly this:
- the same Trade API v2 at `https://external-api.demo.kalshi.co/trade-api/v2`;
- mock funds;
- credentials kept separate from production.

The facts that shaped the design come from docs.kalshi.com, checked on 2026-10-10:

- **Signing.** Requests carry three headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (in
  milliseconds) and `KALSHI-ACCESS-SIGNATURE`. The signature covers timestamp + method + path,
  where the path includes `/trade-api/v2` and excludes the query string. Keys are Ed25519, or RSA
  signed with PSS over SHA-256.
- **Orders.** Created with `POST /portfolio/events/orders`.
  - Orders speak only of the YES leg: `bid` buys YES and `ask` sells it. Selling YES is the same
    exposure as buying NO.
  - Prices are YES-leg prices.
  - A repeated `client_order_id` is refused with HTTP 409.
- **Batches.** The batch endpoint reports results per order, but its atomicity is undocumented.
  Each order costs 10 write tokens, and the Basic tier's write bucket holds 100.
- **Fills.** Reported exactly, per fill: price, count and `fee_cost`.
- **Positions.** One signed number per market, so opposite contracts net immediately.

## Decision

1. **Demo only, enforced in code.** The authenticated client signs only for allow-listed hosts,
   which default to the two demo hosts. It refuses every production host even when told to allow
   one. The production host is not wired in anywhere, and a test pins all of this. Trading real
   money would need a deliberate code change and its own review; it is out of scope.

2. **Credentials.**
   - They come from `KALSHI_DEMO_KEY_ID` and `KALSHI_DEMO_KEY_FILE`, never from the command line.
   - They are never logged and never stored in a recording, and their `repr` hides the key.
   - `*.pem` and `*.key` are git-ignored.

3. **Orders.**
   - Every order is an immediate-or-cancel limit buy, so nothing ever rests on the book.
   - Buying NO at no more than `q` is sent as an `ask` at `1 - q`.
   - A basket's legs go out together, in batches of at most ten.
   - Each order's `client_order_id` is derived from trade, phase and leg, so a retried request is
     the same order.
   - An order without a clean result is looked up on the exchange by that id before karb decides
     what it holds. That covers a per-order error, a lost response, and a whole batch that failed
     after its retries.

4. **Exact fills.**
   - Fills become `LegFill`s priced with the exchange's trade fees, plus the documented per-fill
     rounding to the balance precision.
   - karb's fee model is applied to the same fills.
   - Attribution (ADR-0008) is unchanged, but now measures real execution.

5. **One trade at a time, audited.** Before each trade the trader checks two things:
   - the account holds no position in the basket's markets;
   - the balance covers the cost.

   After the trade it compares the exchange's account with the fills. It halts trading for the
   session if:
   - an order error stays unresolved;
   - fees plus any unexplained balance shortfall exceed what the fee model allowed (the
     guarantees would then be overstated);
   - positions differ from the fills.

   A stop file halts trading without stopping the scan.

6. **Repair against post-trade books.** A half-filled basket is repaired by the same LP as before
   (ADR-0008). It runs on books fetched after the entry, which already lack the size the entry
   took, so the simulator's memory of taken liquidity is no longer needed. Repairs often buy the
   other side of a held market. Kalshi then nets the pair and returns $1 each at once. The model
   keeps both sides until settlement, where they pay the same $1, and each trade records the cash
   netted so the two accounts reconcile.

7. **A simulated desk for everything offline.** `karb.trading.simulator` answers the same signed
   endpoints:
   - it verifies signatures;
   - it matches orders against its host exchange's books and consumes the size it fills;
   - it charges the documented fees and floors balances to cents;
   - it nets positions and refuses duplicate ids.

   Tests and the offline `karb demo` trade against it, so the whole path, signatures included,
   runs in CI.

8. **Paper trading is removed.** That covers `scan --paper`, `paper-replay` and the simulated
   fills. Schema v3 moves recorded paper trades into the new trade tables unchanged, and karb
   reads v2 recordings read-only through views.

## Consequences

- **The demo exchange is not a market.** Its books are thin and quoted by test traders, so
  scanning it verifies many "arbitrage" baskets, some absurdly profitable. They show that the
  path works, not that the edge exists. The real market's answer is in docs/research.md.
- **Liquidity is consumed.** A trade takes the size behind an opportunity, so later cycles no
  longer see it. Lifetimes in a trading run are cut short by karb itself.
- **Settlement needs the demo markets to settle.** That can take as long as a real market. The
  exercise mode (`karb trade --exercise`) buys a complete set whose payout is known, so a
  settlement can be checked against the model as soon as the event resolves.
- **Fees get tested.** Every order compares Kalshi's charged fees with the 0.07 coefficient (see
  docs/data-caveats.md); a discrepancy halts trading instead of passing silently.

## Alternatives considered

- **Keep paper trading beside real orders.** Rejected at the user's request. The simulated desk
  keeps the offline dry run.
- **Production behind a flag.** Rejected: a flag is one typo from real money, and real-money
  trading needs eligibility and risk controls this project does not have.
- **WebSocket order books and fills.** Possible now that requests are signed, but polling is
  enough for one trade at a time. Deferred.
