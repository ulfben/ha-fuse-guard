"""Runtime tests: the guard inside Home Assistant, from states to writes."""

from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.fuse_guard.const import (
    CONF_ACTIVE_CONDITIONS,
    CONF_ACTUAL_SIGN,
    CONF_FRESHNESS_S,
    CONF_FUSE_A,
    CONF_KIND,
    CONF_LOAD_PHASES,
    CONF_LOADS,
    CONF_MARGIN_A,
    CONF_NAME,
    CONF_PHASE_SENSORS,
    CONF_PHASES,
    CONF_SETPOINT_ENTITY,
    CONF_STARTUP_HOLD_S,
    CONF_TARGET_W,
    CONF_VOLTAGE,
    DEFAULT_ADVANCED,
    DOMAIN,
    EVENT_ACTION,
    KIND_SETPOINT,
)
from custom_components.fuse_guard.logbook import async_describe_events

SETPOINT = "number.battery_power"
EMS_MODE = "sensor.ems_mode"
COMMAND = "sensor.charge_command"
# As at the reference installation: the battery is grid charging when EMS mode is 2 (forced) and
# the command is 170 (charge).
ACTIVE_CONDITIONS = [
    {"condition": "state", "entity_id": EMS_MODE, "state": "2"},
    {"condition": "state", "entity_id": COMMAND, "state": "170"},
]

OPTIONS = {
    CONF_NAME: "House",
    CONF_FUSE_A: 16,
    CONF_PHASES: 3,
    CONF_MARGIN_A: 0.6,
    CONF_VOLTAGE: 230,
    CONF_PHASE_SENSORS: {
        "l1": ["sensor.meter_l1"],
        "l2": ["sensor.meter_l2"],
        "l3": ["sensor.meter_l3", "sensor.other_l3"],
    },
    CONF_FRESHNESS_S: 60,
    CONF_LOADS: [
        {
            CONF_KIND: KIND_SETPOINT,
            CONF_SETPOINT_ENTITY: SETPOINT,
            CONF_TARGET_W: 6000,
            CONF_ACTIVE_CONDITIONS: ACTIVE_CONDITIONS,
            CONF_ACTUAL_SIGN: "positive",
            CONF_LOAD_PHASES: "all",
        }
    ],
    **DEFAULT_ADVANCED,
    CONF_STARTUP_HOLD_S: 0,
}


def set_phases(hass: HomeAssistant, l1: float = 8, l2: float = 8, l3: float = 8) -> None:
    for entity_id, value in (
        ("sensor.meter_l1", l1),
        ("sensor.meter_l2", l2),
        ("sensor.meter_l3", l3),
        ("sensor.other_l3", 5),
    ):
        hass.states.async_set(
            entity_id, str(value), {"unit_of_measurement": "A", "device_class": "current"}
        )


@pytest.fixture
async def setup(hass: HomeAssistant):
    """A guard over a 6000 W battery setpoint, plus a recorder of number writes."""
    set_phases(hass)
    hass.states.async_set(SETPOINT, "6000", {"unit_of_measurement": "W", "max": 10000})
    hass.states.async_set(EMS_MODE, "2")
    hass.states.async_set(COMMAND, "170")
    entry = MockConfigEntry(
        domain=DOMAIN, title="House", unique_id=SETPOINT, data={}, options=OPTIONS
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # After setup: loading our own number platform registers the real service.
    calls = async_mock_service(hass, "number", "set_value")
    return entry, calls


async def set_mode(hass: HomeAssistant, mode: str) -> None:
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.house_mode", "option": mode}, blocking=True
    )
    await hass.async_block_till_done()


async def test_entities_after_setup(hass: HomeAssistant, setup) -> None:
    assert hass.states.get("select.house_mode").state == "shadow"
    status = hass.states.get("sensor.house_status")
    assert status.attributes["summary"].startswith("Shadow · ")
    assert hass.states.get("sensor.house_highest_phase_current").state == "8.0"
    assert hass.states.get("sensor.house_headroom").state == "7.4"
    assert hass.states.get("number.house_target_power").state == "6000.0"
    l3 = hass.states.get("sensor.house_l3_current")
    assert l3.attributes["fresh_sources"] == 2
    assert l3.attributes["source_entity"] == "sensor.meter_l3"


async def test_shadow_mode_decides_but_never_writes(hass: HomeAssistant, setup) -> None:
    _, calls = setup
    events = []
    hass.bus.async_listen(EVENT_ACTION, events.append)
    set_phases(hass, l3=16.4)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.house_status").state == "limiting"
    assert float(hass.states.get("sensor.house_ceiling").state) == 5000
    assert hass.states.get("binary_sensor.house_limiting").state == "on"
    assert calls == []
    assert events and events[-1].data["mode"] == "shadow"


async def test_action_event_is_tied_to_the_status_sensor(hass: HomeAssistant, setup) -> None:
    """The logbook filters by entity and device, so the event must carry both."""
    events = []
    hass.bus.async_listen(EVENT_ACTION, events.append)
    set_phases(hass, l3=16.4)
    await hass.async_block_till_done()
    data = events[-1].data
    assert data["entity_id"] == "sensor.house_status"
    reg_entry = er.async_get(hass).async_get("sensor.house_status")
    assert data["device_id"] is not None
    assert data["device_id"] == reg_entry.device_id

    described = {}
    async_describe_events(hass, lambda domain, event, fn: described.update({event: fn}))
    entry = described[EVENT_ACTION](events[-1])
    assert entry["entity_id"] == "sensor.house_status"
    assert entry["message"].startswith("would set number.")


async def test_active_mode_cuts_the_setpoint(hass: HomeAssistant, setup) -> None:
    _, calls = setup
    await set_mode(hass, "active")
    set_phases(hass, l3=16.4)
    await hass.async_block_till_done()
    assert [c.data for c in calls] == [{"entity_id": SETPOINT, "value": 5000}]
    assert (
        "Limiting · 5.0 of 6.0 kW" in hass.states.get("sensor.house_status").attributes["summary"]
    )


async def test_inactive_load_is_never_written(hass: HomeAssistant, setup) -> None:
    _, calls = setup
    await set_mode(hass, "active")
    hass.states.async_set(COMMAND, "204")  # stop
    set_phases(hass, l3=20)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.house_status").state == "inactive"
    assert calls == []


async def test_stale_meters_pause_the_load(
    hass: HomeAssistant, setup, freezer: FrozenDateTimeFactory
) -> None:
    _, calls = setup
    await set_mode(hass, "active")
    freezer.tick(timedelta(seconds=70))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.house_status").state == "fail_safe"
    assert hass.states.get("sensor.house_l1_current").state == "unavailable"
    assert calls[-1].data == {"entity_id": SETPOINT, "value": 0}


async def test_mode_and_target_survive_reload(hass: HomeAssistant, setup) -> None:
    entry, _ = setup
    await set_mode(hass, "active")
    # number.set_value is mocked in these tests, so set the target directly.
    await entry.runtime_data.async_set_target(4000)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("select.house_mode").state == "active"
    assert hass.states.get("number.house_target_power").state == "4000.0"


async def test_unknown_condition_entity_never_writes(hass: HomeAssistant, setup) -> None:
    _, calls = setup
    await set_mode(hass, "active")
    hass.states.async_set(EMS_MODE, "unavailable")
    set_phases(hass, l3=20)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.house_status").state == "fail_safe"
    assert calls == []


async def test_condition_change_triggers_evaluation(hass: HomeAssistant, setup) -> None:
    _, calls = setup
    await set_mode(hass, "active")
    hass.states.async_set(COMMAND, "204")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.house_status").state == "inactive"
    hass.states.async_set(COMMAND, "170")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.house_status").state != "inactive"


async def test_new_guard_with_target_entity_starts_at_full_target(hass: HomeAssistant) -> None:
    """Regression: with no fixed target, a new guard started at 0 W while idle."""
    set_phases(hass)
    hass.states.async_set(SETPOINT, "6000", {"unit_of_measurement": "W", "max": 10000})
    hass.states.async_set("input_number.desired", "6000")
    hass.states.async_set(EMS_MODE, "0")  # self-consumption: not grid charging
    hass.states.async_set(COMMAND, "204")
    load = {k: v for k, v in OPTIONS[CONF_LOADS][0].items() if k != CONF_TARGET_W}
    options = {**OPTIONS, CONF_LOADS: [{**load, "target_entity": "input_number.desired"}]}
    entry = MockConfigEntry(domain=DOMAIN, title="House", unique_id=SETPOINT, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.house_status").state == "inactive"
    assert float(hass.states.get("sensor.house_ceiling").state) == 6000
    assert hass.states.get("binary_sensor.house_limiting").state == "off"
    assert hass.states.get("number.house_target_power") is None
