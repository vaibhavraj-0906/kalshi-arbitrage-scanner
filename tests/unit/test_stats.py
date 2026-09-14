from __future__ import annotations

from karb.store.stats import SightingRecord, build_episodes, quantile

SECOND = 1_000_000_000


def sighting(cycle: int, *, oid: str = "X", group: str = "G", pnl: int = 10_000) -> SightingRecord:
    return SightingRecord(
        cycle_no=cycle,
        group_key=group,
        opportunity_id=oid,
        kind="OVERROUND",
        tier="LOGICAL",
        observed_ns=cycle * 5 * SECOND,
        guaranteed_pnl=pnl,
        cost=100_000,
        contracts=500,
    )


def test_episodes_end_only_when_the_group_is_observed_without_the_opportunity() -> None:
    timelines = {"G": [1, 2, 3, 5], "H": [1, 2]}
    episodes = build_episodes(
        timelines,
        [sighting(1), sighting(2, pnl=30_000), sighting(5), sighting(2, oid="Y", group="H")],
    )
    assert [
        (e.group_key, e.opportunity_id, e.first_cycle, e.last_cycle, e.sightings, e.censored)
        for e in episodes
    ] == [
        ("G", "X", 1, 2, 2, False),  # observed without X in cycle 3: ended
        ("G", "X", 5, 5, 1, True),  # present at G's last observation: may have lasted longer
        ("H", "Y", 2, 2, 1, True),
    ]
    first = episodes[0]
    assert first.lifetime_seconds == 5.0
    assert first.best_guaranteed_pnl == 30_000


def test_a_cycle_without_an_observation_neither_ends_nor_extends_an_episode() -> None:
    episodes = build_episodes({"G": [1, 3]}, [sighting(1), sighting(3)])
    assert [(e.first_cycle, e.last_cycle, e.sightings) for e in episodes] == [(1, 3, 2)]


def test_quantile() -> None:
    assert quantile([], 0.5) is None
    assert quantile([3.0, 1.0, 2.0], 0.5) == 2.0
    assert quantile([1.0, 2.0, 3.0, 4.0], 0.9) == 4.0
    assert quantile([7.0], 0.0) == 7.0
