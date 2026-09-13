from __future__ import annotations

import json

from tests.golden.scan_summary import EXPECTED, scan_summary


def test_recorded_exchange_data_scans_to_the_golden_summary() -> None:
    assert EXPECTED.exists(), "run: uv run python -m tests.golden.regenerate"
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    assert json.loads(json.dumps(scan_summary(), sort_keys=True)) == expected
