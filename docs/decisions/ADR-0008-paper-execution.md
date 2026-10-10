# ADR-0008: Paper execution, repair, settlement and attribution

**Status:** Accepted · Milestone 3. The simulated fills are superseded by real orders on the demo exchange (ADR-0010). Planning, repair, settlement and attribution stand.

## Context

A verified opportunity is a statement about one snapshot of the books: *if* every leg could be
bought at those prices, the basket would lock in a profit. Whether the profit survives depends
on things the snapshot cannot show:

- **Time.** The books move between seeing an opportunity and an order arriving. On a public
  REST connection that takes seconds.
- **Partial fills.** If only some legs fill, a riskless basket becomes a directional bet.
- **Contract reading.** The guaranteed P&L rests on karb's reading of the contracts. ADR-0006
  showed that reading can be wrong in ways no snapshot reveals.

A paper trader must measure all three, and measure them honestly. tengine's ADR-0007 lists the
generous shortcuts that make simulators lie, such as filling at a level that was only touched or
ignoring finite size. They all bias the result in the trader's favour.

## Decision

### Lifecycle: every stage acts on a book one step old

| Book | When | What happens |
|---|---|---|
| B0, decision | the confirmation snapshot | detection sees the opportunity; the basket is scaled to budget and priced exactly |
| B1, arrival | one latency later (live: re-fetched; replay: the group's next observation) | entry orders fill |
| B2, repair | one latency after B1 | repair orders, decided on B1, fill |

- **Orders are immediate-or-cancel taker buys.** Each leg is limited to the worst price B0
  needed, and anything unfilled is cancelled.
- **Liquidity stays taken.** A trade remembers every contract it bought and subtracts it from
  every later book of that trade. A paper trader cannot move the market, so size still showing
  in B2 at a price we took in B1 is assumed to be the size we bought.
- **One paper trade per event group per run.** A lasting violation would otherwise be "traded"
  every cycle against the same displayed size.
- **Trade on the first sighting.** The arrival book already confirms the opportunity, so waiting
  for a second sighting would only add latency.

### Repair is the same LP, started from the position

The basket LP (ADR-0003) gains an optional *base payoff*: the payoff, in every atom, of a
position already held. Maximising the guaranteed payoff of base plus new trades, minus their cost,
does two jobs:

- **Find a basket:** with no base, it is the arbitrage search.
- **Repair a position:** with a base, it finds the trades that most raise a half-filled
  position's worst case. That means completing missing legs, unwinding filled ones by buying
  their other side, or a mix, whichever the book makes cheapest.

Doing nothing is always a candidate, and a repair is sent only if it strictly improves the exact,
fee-rounded worst case. A repair may trade only markets already held, because a fresh, unrelated
arbitrage would otherwise be booked as "hedging".

### Settlement and attribution

- **Final results only.** Results come from `GET /markets?tickers=…`, falling back to the
  historical archive. A market counts only when `finalized`; `determined` can still be disputed.
- **Payout.** YES pays `settlement_value_dollars` (which also covers voided and scalar
  outcomes) and NO pays the complement.
- **Attribution.** Every trade's P&L splits into four exact amounts that sum to the realized
  result:

```
realized = planned + execution + hedging + settlement outcome
```

- **planned:** the guaranteed P&L of the budgeted basket on B0.
- **execution** (worst case after entry minus planned): latency, price moves, missing size and
  leg imbalance.
- **hedging** (worst case after repair minus worst case after entry).
- **settlement outcome** (realized minus worst case after repair): never negative if the outcome
  model is right, because every settlement the model allows pays at least the worst case. A
  negative outcome is recorded as a **model violation**, meaning the exchange settled in a way
  karb considered impossible.

## Consequences

- **The research question becomes measurable:** how much of a detected edge survives latency,
  finite size and leg risk. Replays answer it deterministically for any fee model or latency.
- **Replayed latency is coarse.** It is the recording's confirmation cadence (seconds), which
  deliberately overstates live latency. Live paper trading uses a configurable latency (1 s by
  default) plus real fetch time.
- **Results are conservative.** Size taken stays taken, and limits are set from the decision
  book. A paper trade that consumes most of a level is unlikely to be repeated.
- **Settlement is an external audit.** A property test proves that for any position, settling
  every market as any atom dictates pays exactly the atom model's payoff. The violation flag can
  therefore only fire when the exchange contradicts the model.
- **Capital is reserved until settlement.** Open positions count against `--capital`, and flat
  or missed trades release it at once.
