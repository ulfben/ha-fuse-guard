"""Live checks shown during setup: what the picked sensors read right now."""

from __future__ import annotations

import copy
import logging
import re
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import condition
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ACTIVE_CONDITIONS,
    CONF_ACTUAL_ENTITY,
    CONF_ACTUAL_SIGN,
    CONF_READBACK_ENTITY,
    CONF_SETPOINT_ENTITY,
    CONF_TARGET_ENTITY,
    CONF_TARGET_W,
    DOMAIN,
    SIGN_NEGATIVE,
)
from .meters import (
    CurrentEntity,
    Meter,
    Reading,
    check,
    find_meters,
    markdown_table,
    meter_key,
    phase_token,
)

_LOGGER = logging.getLogger(__name__)


def _name(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    return (state.attributes.get("friendly_name") if state else None) or entity_id


def discover_meters(hass: HomeAssistant, phases: int) -> list[Meter]:
    """Meters among all current sensors, excluding our own entities."""
    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)
    entities = []
    for state in hass.states.async_all("sensor"):
        if state.attributes.get("device_class") != "current":
            continue
        entry = entity_reg.async_get(state.entity_id)
        if entry and entry.platform == DOMAIN:
            continue
        device = device_reg.async_get(entry.device_id) if entry and entry.device_id else None
        device_name = (device.name_by_user or device.name) if device else None
        entities.append(CurrentEntity(state.entity_id, _name(hass, state.entity_id), device_name))
    return find_meters(entities, phases)


def meter_label(hass: HomeAssistant, meter: Meter) -> str:
    """'SlimmeLezer · 13.1 / 13.0 / 10.8 A': a name, then what it reads now.

    Live readings make the meters on the main fuse easy to spot: they agree
    with each other, unlike an inverter's own output or a broken meter.
    """
    name = meter.label
    first = next(iter(meter.sensors.values()))
    if name == meter.key.partition(".")[2].replace("_", " "):
        # No device: use the first sensor's name, with its phase made generic.
        index, _, lettered = phase_token(first.partition(".")[2]) or (0, "", False)
        raw = first.partition(".")[2].split("_")[index]
        name = re.sub(
            rf"(?i)\b{re.escape(raw)}\b",
            "A/B/C" if lettered else "L1/L2/L3",
            _name(hass, first),
            count=1,
        )
    values = []
    for entity_id in meter.sensors.values():
        state = hass.states.get(entity_id)
        try:
            values.append(f"{float(state.state):.1f}" if state else "–")
        except ValueError:
            values.append("–")
    return f"{name} · {' / '.join(values)} A"


def phase_check(
    hass: HomeAssistant,
    phase_sensors: dict[str, list[str]],
    freshness_s: float,
    max_plausible_a: float | None = None,
):
    """Check the picked phase sensors against each other, right now."""
    now = dt_util.utcnow().timestamp()
    readings = []
    for phase, entity_ids in phase_sensors.items():
        for entity_id in entity_ids:
            state = hass.states.get(entity_id)
            value = None
            if state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                try:
                    value = float(state.state)
                except ValueError:
                    value = None
            key, lettered = meter_key(entity_id)
            readings.append(
                Reading(
                    entity_id=entity_id,
                    name=_name(hass, entity_id),
                    phase=phase,
                    meter=key,
                    value=value,
                    age_s=now - state.last_reported_timestamp if state else None,
                    unit=state.attributes.get("unit_of_measurement") if state else None,
                    lettered=lettered,
                )
            )
    return check(readings, freshness_s, max_plausible_a)


def phase_check_text(result) -> dict[str, str]:
    """Description placeholders for the check page."""
    if result.problems:
        findings = "\n".join(f"- ⚠ {p}" for p in result.problems)
    else:
        findings = "✅ Every sensor is fresh, and the meters agree on every phase."
    if result.suggestion:
        findings += (
            "\n\n**Suggested fix:** the readings fit better as "
            f"{result.suggestion_text}. Pick *Use the suggested phase mapping* to apply it."
        )
    return {"table": markdown_table(result), "findings": findings}


def _value(hass: HomeAssistant, entity_id: str | None) -> tuple[float | None, float | None]:
    """(value, seconds since last report) of an entity, or Nones."""
    state = hass.states.get(entity_id) if entity_id else None
    if state is None:
        return None, None
    age = dt_util.utcnow().timestamp() - state.last_reported_timestamp
    try:
        return float(state.state), age
    except ValueError:
        return None, age


async def load_check_text(hass: HomeAssistant, load: dict[str, Any]) -> dict[str, str]:
    """A plain report of the load's entities, and whether they look consistent."""
    lines: list[str] = []
    problems: list[str] = []

    setpoint_id = load[CONF_SETPOINT_ENTITY]
    setpoint, _ = _value(hass, setpoint_id)
    state = hass.states.get(setpoint_id)
    maximum = state.attributes.get("max") if state else None
    lines.append(
        f"- **Setpoint** {_name(hass, setpoint_id)}: {_fmt(setpoint)} W, max {_fmt(maximum)} W"
    )

    if target_id := load.get(CONF_TARGET_ENTITY):
        target, _ = _value(hass, target_id)
        lines.append(f"- **Target** {_name(hass, target_id)}: {_fmt(target)} W")
    else:
        target = load.get(CONF_TARGET_W)
        lines.append(f"- **Target** fixed: {_fmt(target)} W")
    if target is not None and maximum is not None and target > maximum:
        problems.append("The target is above the setpoint's maximum.")

    if readback_id := load.get(CONF_READBACK_ENTITY):
        readback, age = _value(hass, readback_id)
        lines.append(
            f"- **Readback** {_name(hass, readback_id)}: {_fmt(readback)} W, "
            f"reported {_fmt(age)} s ago"
        )
        if readback is None:
            problems.append("The readback has no numeric value right now.")

    active = await _conditions_now(hass, load.get(CONF_ACTIVE_CONDITIONS))
    lines.append(
        "- **Only write while:** "
        + {
            True: "the conditions hold right now",
            False: "the conditions don't hold right now",
            None: "always (no conditions)",
        }[active]
    )

    if actual_id := load.get(CONF_ACTUAL_ENTITY):
        actual, age = _value(hass, actual_id)
        if actual is not None and load.get(CONF_ACTUAL_SIGN) == SIGN_NEGATIVE:
            actual = -actual
        lines.append(
            f"- **Actual power** {_name(hass, actual_id)}: {_fmt(actual)} W consumed, "
            f"reported {_fmt(age)} s ago"
        )
        if actual is None:
            problems.append("The actual power sensor has no numeric value right now.")
        elif active is not False and actual < -100:
            problems.append(
                "While the load is active it should consume power, but the actual power "
                "reads negative. Check *Actual power sign*."
            )
        elif active is False:
            lines.append(
                "  (The sign can only be checked while the load is active; the guard's Shadow "
                "mode will show it.)"
            )

    findings = (
        "\n".join(f"- ⚠ {p}" for p in problems)
        if problems
        else "✅ Nothing looks wrong. The guard starts in Shadow mode, so you can watch "
        "its decisions before letting it write."
    )
    return {"report": "\n".join(lines), "findings": findings}


async def _conditions_now(hass: HomeAssistant, conditions: list[dict] | None) -> bool | None:
    if not conditions:
        return None
    try:
        validated = await condition.async_validate_conditions_config(
            hass, cv.CONDITIONS_SCHEMA(copy.deepcopy(conditions))
        )
        checker = await condition.async_conditions_from_config(hass, validated, _LOGGER, DOMAIN)
        return bool(checker(None))
    except HomeAssistantError:
        return False


def _fmt(value: float | None) -> str:
    if value is None:
        return "–"
    return f"{value:.0f}" if abs(value) >= 10 else f"{value:.1f}"
