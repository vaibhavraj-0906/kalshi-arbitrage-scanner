# ADR-0007: Record what the exchange sent; replay it through today's code

**Status:** Accepted · Milestone 2

## Context

Milestone 1 answers "is there an arbitrage right now?". Research needs questions about many
snapshots over time: how often violations appear, how long they last, how much they hold, and how
much survives fees. Kalshi publishes no historical order books, so answering them means recording
our own.

Two lessons shape *what* to record.

- **The model changes.** ADR-0006 needed `custom_strike`, a field the first model discarded.
  A recording of karb's decoded events could never have been replayed through that fix.
- **Conclusions must stay re-derivable.** A recording that stores what the detector concluded
  bakes the detector's bugs into history. tengine reached the same conclusion for fills in its
  ADR-0006 (replay-first market data).

## Decision

One DuckDB file, append-only, written as one transaction per confirmation cycle.

| Table | Holds | Why |
|---|---|---|
| `event_payloads` | Each event's JSON as Kalshi sent it, minus per-second fields (quotes, volume), keyed by SHA-256 | Structure is re-derived at replay; an unchanged event is stored once |
| `observations` | Per confirmed group per cycle: payload hash, series fee type and multiplier, tradeable markets, observation time, snapshot skew | Every detection input the books themselves do not carry |
| `books` | Both bid ladders per market per cycle, as exact integer lists | The market data |
| `screen_hits`, `solver_results`, `opportunities` | What a run concluded, tagged by `source` (`live` or a replay id) | Comparison and statistics; never an input to replay |
| `runs`, `cycles`, `replays` | Configuration and timing | Provenance |

Conventions follow tengine's store:
- **Money** is stored as integers, never `DOUBLE`. The only `DOUBLE` column is the LP's
  diagnostic objective.
- **Time** is stored as integer nanoseconds.

**Replay** rebuilds every observation from first principles:
- It re-parses the payload with today's wire models.
- It re-splits participants and re-classifies the event.
- It restores the books and runs `detect`.

Under the recorded configuration, a replay must reproduce the live run exactly: same basket ids,
same exact costs and guaranteed P&L. `karb replay` checks this whenever it runs with the recorded
configuration. Under any other configuration, a replay is a counterfactual on identical data;
`karb sensitivity` replays five fee scenarios.

**Statistics** group sightings into *episodes*, following three rules:
- An episode ends only when its group is observed *without* the opportunity.
- A cycle in which the group was not observed neither ends nor extends an episode.
- An episode still present at the group's last observation is *censored*.

## Consequences

- **Recordings outlive classification changes.** A replay after a rule change reports which
  observations the new rules exclude, rather than silently disagreeing with the live run.
- **Detection is replayable; screening is not.** Listing refreshes (Tier B) are not recorded, so
  screen hits are kept as they happened.
- **Lifetimes are lower bounds** limited by the confirmation cadence and by which groups were
  watched.
- **One writer per file.** DuckDB allows one writer at a time: stop a recording before reading
  it, or read a copy.
- **History is coarse.** Kalshi's one-minute candles support a historical screen (top of book at
  each minute close, quiet minutes carried forward) but carry no depth. History can therefore
  find pre-fee necessary conditions and nothing more (`karb history`).
