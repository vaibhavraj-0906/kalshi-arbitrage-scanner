"""JSON decoding that never lets a float into the money path."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

__all__ = ["load_json"]


def load_json(payload: bytes | str) -> Any:
    """Decode JSON with every non-integer number as an exact ``Decimal``.

    Kalshi sends strike values (``7249.9999``) and fee multipliers as JSON *numbers*. A float
    cannot hold ``7249.9999`` exactly, and interval reasoning on a 0.0001 grid is precisely
    where that error would surface as a phantom arbitrage.
    """
    return json.loads(payload, parse_float=Decimal)
