"""A long scan survives the network dropping out, and the slow tiers never starve the fast one.

The research session behind docs/research.md first died after 14 minutes: DNS failed during a
background re-discovery, the request exhausted its retries, and the exception ended the scan. It
had also managed only five confirmation cycles, because each 90-second listing refresh took longer
than 90 seconds and so ran before every cycle.
"""

from __future__ import annotations

from karb.scanner.service import CycleReport, ScanConfig, Scanner
from tests.integration.planted import PlantedExchange, make_client


async def test_a_failed_rediscovery_is_retried_and_confirmation_never_stops() -> None:
    # The first discovery succeeds; the next one fails all ten attempts of GET /events.
    exchange = PlantedExchange(failing_event_calls=frozenset(range(2, 12)))
    client, sleep = make_client(exchange)
    config = ScanConfig(
        watchlist_size=10, discovery_interval=30.0, screen_interval=1_000.0, confirm_interval=5.0
    )
    cycles: list[CycleReport] = []
    async with client:
        scanner = Scanner(client, config, sleep=sleep)
        await scanner.run_forever(cycles.append, stop_after=300.0)

    assert any("discovery failed" in outage for outage in scanner.outages)
    assert "getaddrinfo failed" in scanner.outages[0]
    assert exchange.event_calls > 11  # discovery came back after the outage
    assert scanner.universe is not None and len(scanner.universe.structures) == 2
    # Confirmation kept running throughout, roughly every confirm_interval of (fake) time.
    assert len(cycles) >= 40
    assert all(set(report.detections) == {"PLANT-1", "KXINX-26SEP14H1600"} for report in cycles)


async def test_scanning_stops_cleanly_while_discovery_is_unreachable() -> None:
    exchange = PlantedExchange(failing_event_calls=frozenset(range(1, 10_000)))
    client, sleep = make_client(exchange)
    async with client:
        scanner = Scanner(client, ScanConfig(), sleep=sleep)
        await scanner.run_forever(lambda report: None, stop_after=120.0)
    assert scanner.universe is None
    assert scanner.outages and all("discovery failed" in outage for outage in scanner.outages)
