"""Tests for meter discovery and the live phase check."""

from custom_components.fuse_guard.meters import (
    CurrentEntity,
    Reading,
    check,
    find_meters,
    markdown_table,
)

# The reference installation's current sensors (names shortened).
ENTITIES = [
    CurrentEntity("sensor.current_l1_house", "Current L1", "Tibber Pulse"),
    CurrentEntity("sensor.current_l2_house", "Current L2", "Tibber Pulse"),
    CurrentEntity("sensor.current_l3_house", "Current L3", "Tibber Pulse"),
    CurrentEntity("sensor.electricity_meter_current_phase_l1", "Current phase L1", "SlimmeLezer"),
    CurrentEntity("sensor.electricity_meter_current_phase_l2", "Current phase L2", "SlimmeLezer"),
    CurrentEntity("sensor.electricity_meter_current_phase_l3", "Current phase L3", "SlimmeLezer"),
    CurrentEntity("sensor.meter_phase_a_current_inv_1", "Meter phase A current inv 1"),
    CurrentEntity("sensor.meter_phase_b_current_inv_1", "Meter phase B current inv 1"),
    CurrentEntity("sensor.meter_phase_c_current_inv_1", "Meter phase C current inv 1"),
    CurrentEntity("sensor.mppt1_current_inv_1", "MPPT1 current inv 1"),
    CurrentEntity("sensor.battery_current_inv_1", "Battery current inv 1"),
    CurrentEntity("sensor.fridge_current", "Current", "Fridge plug"),
]


def test_finds_three_meters_including_one_without_device() -> None:
    meters = {m.key: m for m in find_meters(ENTITIES)}
    assert set(meters) == {
        "sensor.current_*_house",
        "sensor.electricity_meter_current_phase_*",
        "sensor.meter_phase_*_current_inv_1",
    }
    sungrow = meters["sensor.meter_phase_*_current_inv_1"]
    assert sungrow.lettered
    assert sungrow.label == "meter phase * current inv 1"
    assert sungrow.sensors == {
        "l1": "sensor.meter_phase_a_current_inv_1",
        "l2": "sensor.meter_phase_b_current_inv_1",
        "l3": "sensor.meter_phase_c_current_inv_1",
    }
    assert meters["sensor.current_*_house"].label == "Tibber Pulse"
    assert not meters["sensor.current_*_house"].lettered


def test_incomplete_meter_is_not_offered() -> None:
    meters = find_meters([CurrentEntity("sensor.x_l1"), CurrentEntity("sensor.x_l2")])
    assert meters == []


def _readings(sungrow_by_label: dict[str, float], ages: dict[str, float] | None = None):
    ages = ages or {}
    house = {"l1": 9.4, "l2": 12.8, "l3": 4.1}
    out = []
    for meter, prefix in (("tibber", "Tibber"), ("dsmr", "DSMR")):
        for phase, value in house.items():
            out.append(
                Reading(
                    f"sensor.{meter}_{phase}",
                    f"{prefix} {phase.upper()}",
                    phase,
                    meter,
                    value + (0.1 if meter == "dsmr" else 0),
                    ages.get(meter, 3),
                    "A",
                )
            )
    # Sungrow labels A/B/C, mapped A->L1, B->L2, C->L3 by default.
    for label, phase in (("a", "l1"), ("b", "l2"), ("c", "l3")):
        out.append(
            Reading(
                f"sensor.sungrow_{label}",
                f"Sungrow {label.upper()}",
                phase,
                "sungrow",
                sungrow_by_label[label],
                ages.get("sungrow", 2),
                "A",
                lettered=True,
            )
        )
    return out


def test_correct_mapping_passes() -> None:
    result = check(_readings({"a": 9.5, "b": 12.7, "c": 4.2}), freshness_s=60)
    assert result.problems == []
    assert result.suggestion is None
    assert all(row[4] == "✓" for row in result.rows)


def test_sungrow_style_swap_is_detected_and_fixed() -> None:
    # Physically A = L3, B = L1, C = L2.
    result = check(_readings({"a": 4.2, "b": 9.5, "c": 12.7}), freshness_s=60)
    assert any("L1 sensors disagree" in p for p in result.problems)
    assert result.suggestion == {
        "sensor.sungrow_a": "l3",
        "sensor.sungrow_b": "l1",
        "sensor.sungrow_c": "l2",
    }
    assert result.suggestion_text == "Sungrow A → L3, Sungrow B → L1, Sungrow C → L2"
    assert "⚠ disagrees" in markdown_table(result)


def test_stale_meter_is_flagged_and_left_out_of_comparisons() -> None:
    result = check(_readings({"a": 1, "b": 1, "c": 1}, ages={"sungrow": 300}), freshness_s=60)
    assert any("Sungrow A last reported 5 min ago" in p for p in result.problems)
    assert not any("disagree" in p for p in result.problems)
    assert result.suggestion is None


def test_two_meters_that_disagree_get_no_suggestion() -> None:
    readings = [r for r in _readings({"a": 4.2, "b": 9.5, "c": 12.7}) if r.meter != "dsmr"]
    readings = [
        r if r.meter != "tibber" else Reading(**{**r.__dict__, "lettered": True}) for r in readings
    ]
    result = check(readings, freshness_s=60)
    assert result.suggestion is None  # either meter could be the wrong one


def test_single_meter_warns_that_mapping_is_unchecked() -> None:
    readings = [r for r in _readings({"a": 9.5, "b": 12.7, "c": 4.2}) if r.meter == "tibber"]
    result = check(readings, freshness_s=60)
    assert any("can't be cross-checked" in p for p in result.problems)


def test_sensor_on_two_phases_and_wrong_unit() -> None:
    readings = [
        Reading("sensor.a", "A", "l1", "x", 5, 1, "A"),
        Reading("sensor.a", "A", "l2", "x", 5, 1, "A"),
        Reading("sensor.b", "B", "l3", "y", 5000, 1, "mA"),
    ]
    result = check(readings, freshness_s=60)
    assert any("more than one phase" in p for p in result.problems)
    assert any("mA" in p for p in result.problems)


def test_real_snapshot_with_similar_phases_gives_no_guess() -> None:
    """Reference installation, 2026-11-03 evening: L1 and L2 within 0.1 A."""
    readings = []
    for name, meter, values in (
        ("Tibber", "tibber", {"l1": 13.1, "l2": 13.0, "l3": 10.8}),
        ("DSMR", "dsmr", {"l1": 13.1, "l2": 13.0, "l3": 10.8}),
        # Sungrow mapped A->L1, B->L2, C->L3; physically B = L1, C = L2, A = L3.
        ("Sungrow", "sungrow", {"l1": 10.83, "l2": 13.10, "l3": 13.08}),
    ):
        for phase, value in values.items():
            readings.append(
                Reading(
                    f"sensor.{meter}_{phase}",
                    f"{name} {phase.upper()}",
                    phase,
                    meter,
                    value,
                    3,
                    "A",
                    lettered=meter == "sungrow",
                )
            )
    result = check(readings, freshness_s=60)
    assert result.suggestion is None
    assert any("Sungrow L1 → L3 fits the other meters" in p for p in result.problems)
    assert any("L1 and L2 carry similar currents" in p for p in result.problems)


def test_implausible_reading_is_flagged_and_ignored() -> None:
    readings = [
        Reading("sensor.a_l1", "A L1", "l1", "a", 9.0, 3, "A"),
        Reading("sensor.b_l1", "B L1", "l1", "b", 327.67, 3, "A"),
    ]
    result = check(readings, freshness_s=60, max_plausible_a=48)
    assert any("327.7 A" in p for p in result.problems)
    assert not any("disagree" in p for p in result.problems)


def test_derived_sensors_are_not_offered_as_meters() -> None:
    entities = [CurrentEntity(f"sensor.main_fuse_headroom_{p}_a") for p in ("l1", "l2", "l3")] + [
        CurrentEntity(f"sensor.{p}_current_spread_sources") for p in ("l1", "l2", "l3")
    ]
    assert find_meters(entities) == []
