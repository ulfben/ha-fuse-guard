"""Tests for the config and options flow."""

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.fuse_guard.const import (
    CONF_LOADS,
    CONF_PHASE_SENSORS,
    CONF_PHASES,
    CONF_STEP_W,
    DOMAIN,
)

SETPOINT = "number.battery_power"


def _add_entities(hass: HomeAssistant) -> None:
    for phase in ("l1", "l2", "l3"):
        hass.states.async_set(
            f"sensor.meter_{phase}", "10", {"unit_of_measurement": "A", "device_class": "current"}
        )
    hass.states.async_set(SETPOINT, "6000", {"unit_of_measurement": "W", "max": 10000})


async def _run_flow(hass: HomeAssistant, **load) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "House", "fuse_a": 16, "phases": "3", "margin_a": 0.6, "voltage": 230},
    )
    assert result["step_id"] == "sources"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "l1": ["sensor.meter_l1"],
            "l2": ["sensor.meter_l2"],
            "l3": ["sensor.meter_l3"],
            "freshness_s": 60,
        },
    )
    assert result["step_id"] == "load"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"setpoint_entity": SETPOINT, "actual_sign": "positive", "load_phases": "all", **load},
    )


async def test_full_flow_creates_entry(hass: HomeAssistant) -> None:
    _add_entities(hass)
    result = await _run_flow(hass, target_w=6000)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    options = result["options"]
    assert options[CONF_PHASES] == 3
    assert options[CONF_PHASE_SENSORS]["l3"] == ["sensor.meter_l3"]
    assert options[CONF_LOADS][0]["target_w"] == 6000
    assert options[CONF_STEP_W] == 100
    assert result["result"].unique_id == SETPOINT


async def test_target_must_be_entity_or_fixed(hass: HomeAssistant) -> None:
    _add_entities(hass)
    result = await _run_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "target_one_of"}


async def test_options_change_fuse_and_advanced(hass: HomeAssistant) -> None:
    _add_entities(hass)
    entry = (await _run_flow(hass, target_w=6000))["result"]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "fuse"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"fuse_a": 20, "phases": "3", "margin_a": 0.6, "voltage": 230}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["fuse_a"] == 20
    assert entry.options[CONF_LOADS][0]["target_w"] == 6000

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced"}
    )
    advanced = {
        key: value for key, value in entry.options.items() if key in result["data_schema"].schema
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**advanced, CONF_STEP_W: 50}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_STEP_W] == 50
    assert entry.options["fuse_a"] == 20


async def test_same_setpoint_cannot_be_guarded_twice(hass: HomeAssistant) -> None:
    _add_entities(hass)
    await _run_flow(hass, target_w=6000)
    result = await _run_flow(hass, target_w=6000)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_conditions_are_stored_as_entered(hass: HomeAssistant) -> None:
    _add_entities(hass)
    hass.states.async_set("sensor.ems_mode", "2")
    conditions = [
        {"condition": "state", "entity_id": "sensor.ems_mode", "state": "2"},
        {"condition": "template", "value_template": "{{ true }}"},
    ]
    result = await _run_flow(hass, target_w=6000, active_conditions=conditions)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_LOADS][0]["active_conditions"] == conditions


async def test_invalid_conditions_are_rejected(hass: HomeAssistant) -> None:
    _add_entities(hass)
    with pytest.raises(InvalidData):
        await _run_flow(
            hass, target_w=6000, active_conditions=[{"condition": "state", "state": "2"}]
        )
