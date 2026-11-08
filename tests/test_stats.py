"""The last 24 hours: cuts, limited starts, time over the limit, peak current."""

from __future__ import annotations

from custom_components.fuse_guard.stats import WINDOW_S, RollingStats

T0 = 1_800_000_000.0  # on a minute boundary
LIMIT = 16.0


def test_empty() -> None:
    s = RollingStats().summary(T0)
    assert (s.cuts, s.limited_starts, s.over_s, s.peak_a, s.peak_ts) == (0, 0, 0, None, None)


def test_time_over_the_limit_counts_the_gap_after_an_over_reading() -> None:
    stats = RollingStats()
    stats.record(T0, 15.0, LIMIT)
    stats.record(T0 + 1, 19.1, LIMIT, cut=True)  # over from here...
    stats.record(T0 + 5, 19.2, LIMIT)
    stats.record(T0 + 12, 13.9, LIMIT)  # ...until this reading: 11 s
    stats.record(T0 + 20, 13.8, LIMIT)
    s = stats.summary(T0 + 20)
    assert s.over_s == 11
    assert s.cuts == 1
    assert s.peak_a == 19.2
    assert s.peak_ts == T0 + 5


def test_a_long_gap_is_not_counted() -> None:
    # A restart while over the limit: the time HA was down isn't known.
    stats = RollingStats()
    stats.record(T0, 19.0, LIMIT)
    stats.record(T0 + 300, 12.0, LIMIT)
    assert stats.summary(T0 + 300).over_s == 0


def test_missing_reading_breaks_the_interval() -> None:
    stats = RollingStats()
    stats.record(T0, 19.0, LIMIT)
    stats.record(T0 + 2, None, LIMIT)
    stats.record(T0 + 4, 12.0, LIMIT)
    assert stats.summary(T0 + 4).over_s == 2


def test_window_rolls_after_24_hours() -> None:
    stats = RollingStats()
    stats.record(T0, 19.0, LIMIT, cut=True, limited_start=True)
    stats.record(T0 + 3, 12.0, LIMIT)
    assert stats.summary(T0 + WINDOW_S - 60).cuts == 1
    stats.record(T0 + WINDOW_S + 60, 5.0, LIMIT)
    s = stats.summary(T0 + WINDOW_S + 60)
    assert (s.cuts, s.limited_starts, s.over_s, s.peak_a) == (0, 0, 0, 5.0)


def test_a_night_across_midnight_is_one_window() -> None:
    # 23:30 to 03:30: one start before midnight, one cut after.
    stats = RollingStats()
    stats.record(T0, 14.1, LIMIT, limited_start=True)
    stats.record(T0 + 4 * 3600, 19.1, LIMIT, cut=True)
    s = stats.summary(T0 + 10 * 3600)
    assert (s.cuts, s.limited_starts) == (1, 1)


def test_survives_a_restart() -> None:
    stats = RollingStats()
    stats.record(T0, 19.1, LIMIT, cut=True)
    stats.record(T0 + 11, 13.9, LIMIT)
    restored = RollingStats(stats.as_list())
    assert restored.summary(T0 + 60) == stats.summary(T0 + 60)


def test_bad_stored_rows_are_skipped() -> None:
    restored = RollingStats([[T0, 19.1, T0, 11, 1, 0], ["x"], None, [T0 + 60]])
    assert restored.summary(T0 + 120).cuts == 1


def test_peak_can_come_from_another_reading_than_the_time_over() -> None:
    stats = RollingStats()
    stats.record(T0, 13.9, LIMIT, peak_a=19.1)  # a slow meter's spike is still fresh
    stats.record(T0 + 5, 13.8, LIMIT, peak_a=19.1)
    s = stats.summary(T0 + 5)
    assert s.over_s == 0
    assert s.peak_a == 19.1
