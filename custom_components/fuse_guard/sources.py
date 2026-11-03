"""Pick one current per phase from several meters.

Pure Python (no Home Assistant imports) so it can be unit-tested directly.
A phase reads as the highest *fresh* reading among its meters, which is the
conservative choice for fuse protection.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Reading:
    """One meter's latest report."""

    entity_id: str
    state: str
    reported_ts: float  # when the meter last reported, even if unchanged
    restored: bool = False


@dataclass(frozen=True)
class PhaseSample:
    """The value chosen for one phase."""

    current: float | None
    source: str
    timestamp: float
    fresh_sources: int
    spread: float | None  # max - min across fresh readings; None if fewer than two
    # Every fresh reading as (current, reported_ts). Lets the controller ignore a
    # high reading from before its last cut while a newer one is available.
    readings: tuple[tuple[float, float], ...] = ()

    def max_since(self, ts: float) -> float | None:
        """Highest reading reported at or after ts, or None if there is none."""
        readings = self.readings or (
            ((self.current, self.timestamp),) if self.current is not None else ()
        )
        values = [value for value, reported in readings if reported >= ts]
        return max(values) if values else None


def _value(reading: Reading) -> float | None:
    try:
        value = float(reading.state)
    except ValueError:
        return None
    return value if value >= 0 else None


def select_phase(readings: list[Reading], now: float, max_age: float) -> PhaseSample:
    """Return the highest fresh reading, or an empty sample if none is fresh."""
    fresh = [
        (value, r)
        for r in readings
        if not r.restored
        and 0 <= now - r.reported_ts <= max_age
        and (value := _value(r)) is not None
    ]
    if not fresh:
        return PhaseSample(None, "", 0.0, 0, None)
    value, best = max(fresh, key=lambda item: item[0])
    values = [v for v, _ in fresh]
    spread = max(values) - min(values) if len(values) >= 2 else None
    return PhaseSample(
        value,
        best.entity_id,
        best.reported_ts,
        len(fresh),
        spread,
        tuple((v, r.reported_ts) for v, r in fresh),
    )
