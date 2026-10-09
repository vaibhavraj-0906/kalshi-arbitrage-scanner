# ADR-0004: REST-only tiered polling

**Status:** Accepted · Milestone 1

## Context

- **No push data.** Kalshi streams order books over WebSockets, but the handshake requires
  authentication, and this project runs on public data only.
- **The universe is large.** At least 20,000 open markets and 8,000 open events, served through
  cursor pagination (200 events or 1,000 markets per page).
- **Full-depth polling is too expensive.** Order-book depth costs one request per 100 markets.
  Doing that for everything, continuously, would mean hundreds of requests per cycle over a
  link that resets connections often.
- **Snapshots are not atomic.** Books fetched seconds apart can disagree with each other in
  ways the live exchange never did, and a detector fed such a snapshot reports phantom
  arbitrage.

## Decision

Three tiers with different cadences.

1. **Discovery** (10 min):
   - `GET /series` returns every series' fee metadata in a single unpaginated response.
   - `GET /events?with_nested_markets=true` returns every open event.
   - Classification is cached, because structure changes rarely.
2. **Screen** (90 s):
   - Refresh market listings (top-of-book) and run integer necessary conditions over every
     eligible event. Depth and fees can only make things worse, so a miss here is final.
   - Hits become candidates; 24-hour volume ranks a watchlist.
3. **Confirm** (continuous):
   - Fetch books for candidates and the watchlist through `GET /markets/orderbooks`, **packing
     whole events into single requests** whenever they fit.
   - Each snapshot records its skew: first request sent to last response received.
   - An opportunity is confirmed only after consecutive sightings (default 2).

Every request is a GET behind one token bucket (8 requests/s by default). Transport failures,
429s and 5xx are retried with full-jitter exponential backoff. Other 4xx statuses fail
immediately.

## Consequences

- **Latency is seconds, not milliseconds.** This is a research scanner measuring how often and
  how long structural violations are observable. It is not a latency-competitive trader, and
  its lifetimes say so (docs/data-caveats.md).
- **Consistency comes mostly from the batch endpoint.** One response per event covers the common
  case. Events wider than 100 markets pay a measured skew.
- **Adding WebSockets later is contained.** Credentials would replace Tier C polling with deltas
  without touching detection, which takes a snapshot and does not care how it was assembled.

## Amendment (Milestone 3): background tiers and outage recovery

The first hour-long research recording exposed two flaws in running the tiers in sequence:

- **Starvation.** On the development network a full listing refresh took longer than its
  90-second interval. It therefore ran before every cycle, and the 10-second confirmation tier
  managed only five cycles in 14 minutes.
- **Fragility.** A DNS failure during a re-discovery exhausted the client's retries, and the
  exception ended the scan.

Continuous scans now run discovery and listing refreshes as background tasks:

- Confirmation keeps running against the previous universe, and each refresh is applied when it
  completes.
- A failed refresh is recorded as an outage and retried with exponential backoff (15 s doubling
  to 5 min).
- Discovered event data and fee schedules are merged rather than replaced. A cycle already in
  flight can then still record a group whose event closed during the refresh.

`tests/integration/test_scanner_resilience.py` reproduces the outage.
