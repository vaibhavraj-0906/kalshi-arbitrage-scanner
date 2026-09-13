"""A market's valid price grid, derived from ``price_ranges``.

Kalshi assigns every market a grid of valid prices as bands of ``{start, end, step}``. The
bands are the source of truth: the ``price_level_structure`` label exists for humans, and new
structures are introduced over time, so nothing here ever keys off the label.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import pairwise

from karb.core.fixed import FixedPointError, Price, Rounding, div_round

__all__ = ["GridError", "PriceBand", "PriceGrid"]


class GridError(ValueError):
    """A ``price_ranges`` payload does not describe a usable grid."""


@dataclass(frozen=True, slots=True)
class PriceBand:
    start: Price
    end: Price
    step: int
    """Tick size in raw price units ($0.0001)."""

    def __post_init__(self) -> None:
        if self.step <= 0:
            raise GridError(f"non-positive step {self.step}")
        if self.end <= self.start:
            raise GridError(f"empty band [{self.start}, {self.end}]")
        if (self.end.raw - self.start.raw) % self.step:
            raise GridError(f"band [{self.start}, {self.end}] is not a whole number of steps")

    def contains(self, price: Price) -> bool:
        return self.start <= price <= self.end and (price.raw - self.start.raw) % self.step == 0


@dataclass(frozen=True, slots=True)
class PriceGrid:
    bands: tuple[PriceBand, ...]

    def __post_init__(self) -> None:
        if not self.bands:
            raise GridError("no price bands")
        for left, right in pairwise(self.bands):
            if left.end != right.start:
                raise GridError(f"bands are not contiguous at {left.end} / {right.start}")

    @classmethod
    def from_strings(cls, ranges: Sequence[tuple[str, str, str]]) -> PriceGrid:
        try:
            bands = tuple(
                PriceBand(Price.parse(start), Price.parse(end), Price.parse(step).raw)
                for start, end, step in ranges
            )
        except FixedPointError as exc:
            raise GridError(str(exc)) from exc
        return cls(bands)

    @property
    def low(self) -> Price:
        return self.bands[0].start

    @property
    def high(self) -> Price:
        return self.bands[-1].end

    def contains(self, price: Price) -> bool:
        return any(band.contains(price) for band in self.bands)

    def snap(self, price: Price, rounding: Rounding) -> Price | None:
        """The nearest valid price at or below (``FLOOR``) or at or above (``CEIL``).

        ``None`` when no valid price lies on that side.
        """
        if rounding is Rounding.HALF_EVEN:
            raise ValueError("snap rounds toward a side; HALF_EVEN has no side")
        if price < self.low:
            return self.low if rounding is Rounding.CEIL else None
        if price > self.high:
            return self.high if rounding is Rounding.FLOOR else None
        for band in self.bands:
            if band.start <= price <= band.end:
                steps = div_round(price.raw - band.start.raw, band.step, rounding)
                return Price(band.start.raw + steps * band.step)
        raise AssertionError("unreachable: contiguous bands cover [low, high]")

    def prices(self) -> Iterator[Price]:
        """Every valid price, ascending, each exactly once."""
        last: int | None = None
        for band in self.bands:
            for raw in range(band.start.raw, band.end.raw + 1, band.step):
                if raw != last:
                    yield Price(raw)
                last = raw
