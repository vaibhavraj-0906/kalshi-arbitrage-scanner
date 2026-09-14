"""Candlestick payloads: per-minute OHLC of each market's YES bid and YES ask.

Quote encoding differs from market listings. Candles report a missing bid as ``0.0000`` and a
missing ask as ``1.0000`` (listings use ``0.0000`` for both), so prices go through
``karb.market.model.listed_price``, which treats both extremes as "no quote".
"""

from __future__ import annotations

from pydantic import Field

from karb.wire.models import WireModel

__all__ = ["BatchCandlesWire", "BidAskWire", "CandleWire", "MarketCandlesWire"]


class BidAskWire(WireModel):
    open_dollars: str = "0.0000"
    low_dollars: str = "0.0000"
    high_dollars: str = "0.0000"
    close_dollars: str = "0.0000"


class CandleWire(WireModel):
    end_period_ts: int
    """Inclusive end of the candle's period, Unix seconds."""
    yes_bid: BidAskWire
    yes_ask: BidAskWire
    volume_fp: str = "0.00"
    open_interest_fp: str = "0.00"


class MarketCandlesWire(WireModel):
    market_ticker: str
    candlesticks: list[CandleWire] = Field(default_factory=list)


class BatchCandlesWire(WireModel):
    """``GET /markets/candlesticks``: up to 100 tickers and 10,000 candles per response."""

    markets: list[MarketCandlesWire] = Field(default_factory=list)
