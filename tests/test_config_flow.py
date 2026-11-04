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


async def _to_sources(hass: HomeAssistant, meters: list[str] | None = None) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "House", "fuse_a": 16, "phases": "3", "margin_a": 0.6, "voltage": 230},
    )
    assert result["step_id"] == "meters"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"meters": meters or []}
    )
    assert result["step_id"] == "sources"
    return result


async def _run_flow(hass: HomeAssistant, **load) -> dict:
    result = await _to_sources(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "l1": ["sensor.meter_l1"],
            "l2": ["sensor.meter_l2"],
            "l3": ["sensor.meter_l3"],
            "freshness_s": 60,
        },
    )
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "check"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "load"}
    )
    assert result["step_id"] == "load"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"setpoint_entity": SETPOINT, "load_phases": "all", **load},
    )
    if result["type"] is not FlowResultType.MENU:
        return result  # an error form or an abort
    assert result["step_id"] == "load_check"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
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


def _suggested(result: dict, key: str):
    for field in result["data_schema"].schema:
        if field == key:
            return (field.description or {}).get("suggested_value")
    raise KeyError(key)


def _add_swapped_inverter_meter(hass: HomeAssistant) -> None:
    """A meter labelled A/B/C where physically A = L3, B = L1, C = L2."""
    for label, value in (("a", "4.2"), ("b", "9.5"), ("c", "12.7")):
        hass.states.async_set(
            f"sensor.meter_phase_{label}_current_inv_1",
            value,
            {"unit_of_measurement": "A", "device_class": "current"},
        )
    for phase, value in (("l1", "9.4"), ("l2", "12.8"), ("l3", "4.1")):
        hass.states.async_set(
            f"sensor.meter_{phase}", value, {"unit_of_measurement": "A", "device_class": "current"}
        )


async def test_picked_meters_prefill_the_sensors(hass: HomeAssistant) -> None:
    _add_entities(hass)
    _add_swapped_inverter_meter(hass)
    result = await _to_sources(hass, ["sensor.meter_*", "sensor.meter_phase_*_current_inv_1"])
    assert _suggested(result, "l1") == ["sensor.meter_l1", "sensor.meter_phase_a_current_inv_1"]
    assert _suggested(result, "l3") == ["sensor.meter_l3", "sensor.meter_phase_c_current_inv_1"]


async def test_check_page_finds_and_fixes_a_swapped_meter(hass: HomeAssistant) -> None:
    _add_entities(hass)
    _add_swapped_inverter_meter(hass)
    result = await _to_sources(hass, ["sensor.meter_*", "sensor.meter_phase_*_current_inv_1"])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "l1": ["sensor.meter_l1", "sensor.meter_phase_a_current_inv_1"],
            "l2": ["sensor.meter_l2", "sensor.meter_phase_b_current_inv_1"],
            "l3": ["sensor.meter_l3", "sensor.meter_phase_c_current_inv_1"],
            "freshness_s": 60,
        },
    )
    assert result["step_id"] == "check"
    assert "apply_suggestion" in result["menu_options"]
    assert "disagree" in result["description_placeholders"]["findings"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "apply_suggestion"}
    )
    assert result["step_id"] == "check"
    assert "apply_suggestion" not in result["menu_options"]
    assert "✅" in result["description_placeholders"]["findings"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "load"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "setpoint_entity": SETPOINT,
            "target_w": 6000,
            "load_phases": "all",
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    sensors = result["options"][CONF_PHASE_SENSORS]
    assert "sensor.meter_phase_b_current_inv_1" in sensors["l1"]
    assert "sensor.meter_phase_c_current_inv_1" in sensors["l2"]
    assert "sensor.meter_phase_a_current_inv_1" in sensors["l3"]


async def test_load_check_flags_a_reversed_actual_power_sign(hass: HomeAssistant) -> None:
    _add_entities(hass)
    hass.states.async_set("sensor.battery_power", "-5800", {"device_class": "power"})
    result = await _to_sources(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "l1": ["sensor.meter_l1"],
            "l2": ["sensor.meter_l2"],
            "l3": ["sensor.meter_l3"],
            "freshness_s": 60,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "load"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "setpoint_entity": SETPOINT,
            "target_w": 6000,
            "load_phases": "all",
            "feedback": {"actual_entity": "sensor.battery_power", "actual_sign": "positive"},
        },
    )
    assert result["step_id"] == "load_check"
    assert "Actual power sign" in result["description_placeholders"]["findings"]


async def test_options_sensor_change_goes_through_the_check(hass: HomeAssistant) -> None:
    _add_entities(hass)
    entry = (await _run_flow(hass, target_w=6000))["result"]
    hass.states.async_set(
        "sensor.other_l1", "10", {"unit_of_measurement": "A", "device_class": "current"}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "meters"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"meters": []})
    assert result["step_id"] == "sources"
    assert _suggested(result, "l1") == ["sensor.meter_l1"]  # keeps what is configured
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "l1": ["sensor.meter_l1", "sensor.other_l1"],
            "l2": ["sensor.meter_l2"],
            "l3": ["sensor.meter_l3"],
            "freshness_s": 60,
        },
    )
    assert result["step_id"] == "check"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "save"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PHASE_SENSORS]["l1"] == ["sensor.meter_l1", "sensor.other_l1"]
    assert entry.options[CONF_LOADS][0]["target_w"] == 6000


async def test_meter_labels_show_names_and_live_readings(hass: HomeAssistant) -> None:
    _add_entities(hass)
    _add_swapped_inverter_meter(hass)
    for label in ("a", "b", "c"):
        state = hass.states.get(f"sensor.meter_phase_{label}_current_inv_1")
        hass.states.async_set(
            state.entity_id,
            state.state,
            {**state.attributes, "friendly_name": f"Meter phase {label.upper()} current inv 1"},
        )
    entry = (await _run_flow(hass, target_w=6000))["result"]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "meters"}
    )
    field = next(iter(result["data_schema"].schema))
    labels = {o["value"]: o["label"] for o in result["data_schema"].schema[field].config["options"]}
    assert labels["sensor.meter_phase_*_current_inv_1"] == (
        "Meter phase A/B/C current inv 1 · 4.2 / 9.5 / 12.7 A"
    )
    assert field.default() == ["sensor.meter_*"]  # the meter already in use is ticked


async def test_ticking_a_meter_in_use_keeps_its_phases(hass: HomeAssistant) -> None:
    """Regression: re-picking a correctly mapped A/B/C meter reverted it to A = L1."""
    _add_entities(hass)
    _add_swapped_inverter_meter(hass)
    entry = (await _run_flow(hass, target_w=6000))["result"]
    mapped = {
        "l1": ["sensor.meter_l1", "sensor.meter_phase_b_current_inv_1"],
        "l2": ["sensor.meter_l2", "sensor.meter_phase_c_current_inv_1"],
        "l3": ["sensor.meter_l3", "sensor.meter_phase_a_current_inv_1"],
    }
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PHASE_SENSORS: mapped}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "meters"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"meters": ["sensor.meter_*", "sensor.meter_phase_*_current_inv_1"]}
    )
    for phase, ids in mapped.items():
        assert _suggested(result, phase) == ids


async def test_load_feedback_section_is_stored_flat(hass: HomeAssistant) -> None:
    """The form nests the optional feedback fields; the entry stores them flat."""
    _add_entities(hass)
    hass.states.async_set("sensor.battery_power", "5800", {"device_class": "power"})
    result = await _run_flow(
        hass,
        target_w=6000,
        feedback={"actual_entity": "sensor.battery_power", "actual_sign": "negative"},
    )
    load = result["options"][CONF_LOADS][0]
    assert load["actual_entity"] == "sensor.battery_power"
    assert load["actual_sign"] == "negative"
    assert "feedback" not in load


async def test_load_feedback_defaults_when_section_left_closed(hass: HomeAssistant) -> None:
    _add_entities(hass)
    load = (await _run_flow(hass, target_w=6000))["options"][CONF_LOADS][0]
    assert load["actual_sign"] == "positive"
    assert "readback_entity" not in load and "feedback" not in load


async def test_options_load_form_opens_feedback_when_set(hass: HomeAssistant) -> None:
    _add_entities(hass)
    hass.states.async_set("sensor.battery_power", "5800", {"device_class": "power"})
    entry = (
        await _run_flow(
            hass,
            target_w=6000,
            feedback={"actual_entity": "sensor.battery_power", "actual_sign": "positive"},
        )
    )["result"]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "load"}
    )
    feedback = result["data_schema"].schema["feedback"]
    assert feedback.options["collapsed"] is False
    actual = next(k for k in feedback.schema.schema if k == "actual_entity")
    assert actual.description["suggested_value"] == "sensor.battery_power"
