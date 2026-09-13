# ADR-0003: Arbitrage as an LP over outcome atoms

**Status:** Accepted · Milestone 1 · amended by [ADR-0006](ADR-0006-one-underlying-per-ladder.md)

> The interval reasoning below applies only to markets that share one underlying. Live data
> showed that many Kalshi events do not, and ADR-0006 describes how that is established before
> any atoms are built.

## Context

The obvious design is a detector per pattern: YES asks summing below $1, NO baskets, monotone
ladders, butterflies, and so on. Every pattern needs its own sizing logic and fee handling. Each
also has its own traps:

- "Buy every YES" is only riskless if the listed outcomes are exhaustive, and the API cannot say
  whether they are.
- A range ladder written as [7225, 7249.9999] and [7250, …] covers every outcome only if the
  settlement value lives on a 0.0001 grid.
- Mixed ladders, with thresholds and ranges in one event, admit baskets that no single named
  pattern describes.

## Decision

Model each event's settlement as a finite set of **atoms**, and find baskets with one linear
program.

- **Interval events.** Every strike endpoint is an atom, and so is every open gap between
  endpoints, and both tails. A market's YES-set is an interval, so it pays on a contiguous run of
  atoms.
- **Categorical events.** One atom per listed outcome, plus a **residual** atom for "none of the
  listed outcomes". Exhaustiveness then needs no special case: a basket that relies on it fails
  in the residual atom.
- **The LP.** Maximise `t − cost` subject to `payout(atom) ≥ t` for every atom. The variables are
  quantities per market, side and ask level, with pre-rounding fees in the costs.
  - By the finite-state fundamental theorem of asset pricing, a positive optimum means no
    probability distribution over the atoms fits the fee-adjusted quotes.
  - Every named pattern is a special case of that condition.
- **Tiers** are different atom sets for the same event, solved weakest-assumption first:
  - `LOGICAL` uses every atom.
  - `STRUCTURAL` drops uncovered one-grid-step gaps between covered endpoints.
  - `ASSERTED` drops the residual atom for events the user vouches for.

  The first tier that yields a verified opportunity wins, so one opportunity is never reported
  twice.
- **Float LP proposes, integer verifier decides.** HiGHS solves in floats, and the proposal is
  turned into whole-contract candidates at several scalings. `verify_basket` then sweeps each
  book, applies exact fee rounding, and computes the payout in every atom. Only the best
  verified candidate's exact worst-case P&L is ever reported.
- **Naming comes last.** A basket's kind (OVERROUND, MONOTONE, …) is read off its shape after
  verification, and is descriptive only.

## Consequences

- **One code path** handles every structural class, sizing across depth included.
- **The false-positive guarantee is testable directly.** Generate quotes from a distribution;
  the LP must find nothing. The soundness check re-derives payouts from strike rules without
  using atoms, so a bug in atom construction cannot hide behind itself.
- **The screens remain.** They are cheap integer necessary conditions that decide which events
  are worth a book fetch. They are no longer the detector.
- **Cost is acceptable.** A 188-strike threshold ladder produces 377 atoms and a few thousand
  level variables. HiGHS solves that in milliseconds, which is far below network latency.
