# ADR-0006: One underlying per ladder

**Status:** Accepted · Milestone 1 (amends ADR-0003)

## Context

ADR-0003 builds an event's outcome atoms from its markets' strikes, assuming every strike-typed
market in an event settles off the same value at the same moment. Kalshi's API does not state
that, and the offline suite could not have caught it: the property tests prove that the
arbitrage math is right *given* the outcome model, not that the model matches the contracts.

The first full-universe confirmation on live data (2026-09-13) exposed the gap.

- **The scale of it.** 758 events passed the top-of-book screens, and 637 baskets passed exact
  verification.
- **The worst of it.** The largest was a "guaranteed" $34,122 on $88,796 across 64 NFL
  receiving-yards markets, and some spreads showed 93% edges.
- **None of it was real.** Every large one came from markets that share strike fields but not
  an underlying.

| Event | What the markets are | Why one ladder is wrong |
|---|---|---|
| NFL receiving yards | 13 players' ladders ("Jalen Coker: 40+") | Different players; each market's `custom_strike` names a `football_player` |
| NFL spread | "Jacksonville wins by over 3.5", "Cleveland wins by over 3.5" | Different teams' margins, identical strikes |
| IShowSpeed subscribers | "≥ 100M before 2027 / 2028 / 2029 / 2030" | One strike reused for different deadlines |
| US climate goals | "≤ 4909.9 Mt by 2025", "≤ 3317.5 Mt by 2030" | Different years' emissions |

A related ambiguity surfaced in the same run. KXHOUSEPOPVOTEMARGIN writes adjacent brackets as
[0, 2] and [2, 4] in a mutually exclusive event. Under the documented inclusive reading, a
margin of exactly 2 would pay both, so some unstated half-open convention must apply.

## Decision

An interval event is reasoned about only when its markets demonstrably share one underlying.
Every check fails closed, with a stated exclusion reason.

1. **Split by participant.** Interval-typed markets whose `custom_strike` differs are grouped
   per participant. Each group becomes its own sub-event, keyed `EVENT#participant`, because one
   player's ladder *is* a genuine ladder. Categorical events are never split: their outcomes
   legitimately name different participants.
2. **Mixed participants.** A group that still mixes participants is excluded. This is defence in
   depth; the split should make it unreachable.
3. **Staggered settlement.** Markets whose `latest_expiration_time` differs are excluded. One
   value settles once.
4. **Duplicate intervals.** Two markets with the same YES-interval are excluded. They must differ
   in something the strike fields do not capture.
5. **Ambiguous boundaries.** Two markets whose YES-sets meet at exactly one shared endpoint are
   excluded. Which bracket owns the point cannot be known.
6. **The mutually exclusive contradiction check** from ADR-0003 remains for overlaps that are
   not mere touching.

The regression file `tests/unit/test_underlyings.py` pins each live shape.

## Consequences

- **Coverage drops, and that is the point.** Events whose ladders cannot be proven single-valued
  are no longer scanned. Player and team props come back per participant.
- **Opportunity keys can be sub-event keys** (`KXNFLRECYDS-26SEP13CHICAR#football_player:85b609a0`).
  Books are still fetched by market ticker, so nothing downstream changes.
- **Residual risk.** Markets with the same participant and the same expiration can still, in
  principle, reference different quantities that no API field distinguishes. The guard is
  empirical:
  - run the full-universe confirmation as an acceptance check after any change to
    classification;
  - treat any verified opportunity with an implausible edge as a bug report until
    `karb explain` shows otherwise.
- **The lesson generalises.** Exact arithmetic and exhaustive atoms make the scanner's reasoning
  sound. They cannot make its reading of a contract correct. Only live data can test that
  reading.
