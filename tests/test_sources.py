"""Tests for picking one current per phase."""

from custom_components.fuse_guard.sources import Reading, select_phase

NOW = 1_000_000.0


def test_highest_fresh_reading_wins() -> None:
    sample = select_phase(
        [
            Reading("sensor.tibber_l3", "14.2", NOW - 5),
            Reading("sensor.dsmr_l3", "15.1", NOW - 40),
            Reading("sensor.sungrow_a", "14.8", NOW - 2),
        ],
        NOW,
        60,
    )
    assert sample.current == 15.1
    assert sample.source == "sensor.dsmr_l3"
    assert sample.fresh_sources == 3
    assert sample.spread is not None and abs(sample.spread - 0.9) < 1e-9


def test_stale_restored_and_invalid_readings_are_ignored() -> None:
    sample = select_phase(
        [
            Reading("sensor.stale", "20", NOW - 61),
            Reading("sensor.restored", "19", NOW - 1, restored=True),
            Reading("sensor.unavailable", "unavailable", NOW - 1),
            Reading("sensor.negative", "-1", NOW - 1),
            Reading("sensor.ok", "9.5", NOW - 1),
        ],
        NOW,
        60,
    )
    assert sample.current == 9.5
    assert sample.fresh_sources == 1
    assert sample.spread is None


def test_no_fresh_reading_gives_empty_sample() -> None:
    sample = select_phase([Reading("sensor.stale", "10", NOW - 120)], NOW, 60)
    assert sample.current is None
    assert sample.fresh_sources == 0
