"""What the guard did over the last 24 hours, for "how did last night go?".

Pure Python (no Home Assistant imports). Kept per minute, so the window rolls
smoothly and survives restarts in a small list: a night of charging spans
midnight, so a counter that reset at midnight would split it in two.
"""

from __future__ import annotations

from dataclasses import dataclass

WINDOW_S = 24 * 3600
_MINUTE_S = 60
# An evaluation runs at least every few seconds. A longer gap (a restart, the
# guard switched off) isn't counted as time over the limit.
_MAX_GAP_S = 30.0


@dataclass
class _Minute:
    start: float
    peak_a: float = 0.0
    peak_ts: float = 0.0
    over_s: float = 0.0
    cuts: int = 0
    starts: int = 0


@dataclass(frozen=True)
class Summary:
    cuts: int  # cuts, including starts into an overload
    limited_starts: int  # starts limited by the headroom
    over_s: float  # seconds the highest phase was over the guard's limit
    peak_a: float | None  # highest phase current seen
    peak_ts: float | None


class RollingStats:
    """Per-minute buckets over the last 24 hours."""

    def __init__(self, stored: list[list[float]] | None = None) -> None:
        self._minutes: list[_Minute] = []
        for row in stored or []:
            try:
                self._minutes.append(_Minute(*row[:4], int(row[4]), int(row[5])))
            except (TypeError, ValueError, IndexError):
                continue
        self._last: tuple[float, float, float] | None = None  # time, current, limit

    def record(
        self,
        now: float,
        current_a: float | None,
        limit_a: float,
        cut: bool = False,
        limited_start: bool = False,
    ) -> None:
        """Count one evaluation. The time since the last one counts as over the
        limit if the highest phase was over it then."""
        minute = self._minute(now)
        if self._last is not None:
            last_ts, last_a, last_limit = self._last
            gap = now - last_ts
            if 0 < gap <= _MAX_GAP_S and last_a > last_limit:
                minute.over_s += gap
        if current_a is not None:
            if current_a > minute.peak_a:
                minute.peak_a, minute.peak_ts = current_a, now
            self._last = (now, current_a, limit_a)
        else:
            self._last = None
        minute.cuts += int(cut)
        minute.starts += int(limited_start)
        self._prune(now)

    def summary(self, now: float) -> Summary:
        recent = [m for m in self._minutes if m.start > now - WINDOW_S]
        peak = max(recent, key=lambda m: m.peak_a, default=None)
        has_peak = peak is not None and peak.peak_ts > 0
        return Summary(
            cuts=sum(m.cuts for m in recent),
            limited_starts=sum(m.starts for m in recent),
            over_s=round(sum(m.over_s for m in recent), 1),
            peak_a=peak.peak_a if has_peak else None,
            peak_ts=peak.peak_ts if has_peak else None,
        )

    def as_list(self) -> list[list[float]]:
        """For the guard's store."""
        return [
            [m.start, m.peak_a, m.peak_ts, round(m.over_s, 1), m.cuts, m.starts]
            for m in self._minutes
        ]

    def _minute(self, now: float) -> _Minute:
        start = now - now % _MINUTE_S
        if self._minutes and self._minutes[-1].start == start:
            return self._minutes[-1]
        minute = _Minute(start)
        self._minutes.append(minute)
        return minute

    def _prune(self, now: float) -> None:
        cutoff = now - WINDOW_S
        if self._minutes and self._minutes[0].start <= cutoff:
            self._minutes = [m for m in self._minutes if m.start > cutoff]
