"""Rewrite tests/golden/expected_scan.json from the recorded fixtures.

Run only after an intentional behaviour change, and review the diff before committing it.
"""

from __future__ import annotations

import json

from tests.golden.scan_summary import EXPECTED, scan_summary

if __name__ == "__main__":
    EXPECTED.write_text(
        json.dumps(scan_summary(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {EXPECTED}")
