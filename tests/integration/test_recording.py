"""Recording a scan, replaying it, and measuring it -- end to end, offline.

The mock exchange (tests/integration/planted.py) serves the recorded S&P range event alongside a
planted three-way event whose YES bids sum to $1.20, so the recording holds a real (planted)
opportunity to reproduce.
"""

from __future__ import annotations

from pathlib import Path

from karb.store.codec import detect_config_from_json
from karb.store.database import LIVE_SOURCE, RecordStore
from karb.store.replay import compare_with_live, replay_run
from karb.store.stats import fee_scenarios, fee_sensitivity, run_statistics
from tests.integration.planted import PLANTED, record_scan
from tests.support import captured_at


async def test_recording_captures_every_cycle(tmp_path: Path) -> None:
    path = tmp_path / "karb.duckdb"
    run_id = await record_scan(path)
    with RecordStore(path, read_only=True) as store:
        (run,) = store.runs()
        assert (run.run_id, run.cycles, run.observations, run.live_opportunities) == (
            run_id,
            2,
            4,
            2,
        )
        assert run.finished_ns is not None
        counts = store.table_counts()
        assert counts["event_payloads"] == 2  # stored once each, referenced by both cycles
        assert counts["books"] == 2 * (len(PLANTED) + 30)
        live = store.opportunities(LIVE_SOURCE, run_id)
        assert len({row.opportunity_id for row in live}) == 1
        assert {row.guaranteed_pnl for row in live} == {7_480_000}


async def test_replay_reproduces_the_live_run_exactly(tmp_path: Path) -> None:
    path = tmp_path / "karb.duckdb"
    run_id = await record_scan(path)
    with RecordStore(path) as store:
        recorded = detect_config_from_json(store.run(run_id).config["detect"])
        outcome = replay_run(store, run_id, recorded)
        assert (outcome.observations, outcome.replayed, dict(outcome.skipped)) == (4, 4, {})
        assert outcome.cache_hits == 2  # both groups' books were unchanged in cycle 2
        assert compare_with_live(store, outcome).identical

        saved = replay_run(store, run_id, recorded, save=True, now=captured_at())
        assert saved.replay_id is not None
        assert len(store.opportunities(saved.replay_id, run_id)) == 2


async def test_statistics_and_fee_sensitivity(tmp_path: Path) -> None:
    path = tmp_path / "karb.duckdb"
    run_id = await record_scan(path)
    with RecordStore(path, read_only=True) as store:
        stats = run_statistics(store, run_id)
        assert (stats.cycles, stats.rescreens, stats.observations, stats.groups) == (2, 1, 4, 2)
        assert stats.verified_observations == 2
        (episode,) = stats.episodes
        assert (episode.kind, episode.sightings, episode.censored) == ("OVERROUND", 2, True)
        assert episode.lifetime_seconds == 5.0
        rules = {(tier, rule) for tier, rule, _hits, _groups in stats.screen_hits}
        assert ("LOGICAL", "bids over $1") in rules

        recorded = detect_config_from_json(store.run(run_id).config["detect"])
        rows = fee_sensitivity(store, run_id, recorded)
        assert [row.label for row in rows] == [label for label, _ in fee_scenarios(recorded)]
        as_recorded, free = rows[0], rows[1]
        assert (as_recorded.verified_observations, as_recorded.episodes) == (2, 1)
        assert as_recorded.best_total == 7_480_000
        assert free.best_total == 10_000_000  # $100 guaranteed for $90 of NO, no fees at all
