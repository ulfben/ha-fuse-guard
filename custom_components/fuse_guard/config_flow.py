"""Config and options flow for Fuse Guard."""

from __future__ import annotations

import copy
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import UnitOfElectricCurrent, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import condition, selector
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_ACTIVE_CONDITIONS,
    CONF_ACTUAL_ENTITY,
    CONF_ACTUAL_SIGN,
    CONF_BASE_DROP_W,
    CONF_COOLDOWN_S,
    CONF_CUT_INTERVAL_S,
    CONF_FRESHNESS_S,
    CONF_FUSE_A,
    CONF_INTERVAL_S,
    CONF_KIND,
    CONF_LOAD_PHASES,
    CONF_LOADS,
    CONF_MARGIN_A,
    CONF_MIN_OVER_A,
    CONF_NAME,
    CONF_ON_MISSING_DATA,
    CONF_PHASE_SENSORS,
    CONF_PHASES,
    CONF_READBACK_ENTITY,
    CONF_RECOVERY_GAIN,
    CONF_RECOVERY_INTERVAL_S,
    CONF_RESPONSE_S,
    CONF_SETPOINT_ENTITY,
    CONF_SETTLING_S,
    CONF_STARTUP_HOLD_S,
    CONF_STEP_W,
    CONF_TARGET_ENTITY,
    CONF_TARGET_W,
    CONF_VOLTAGE,
    DEFAULT_ADVANCED,
    DEFAULT_FRESHNESS_S,
    DEFAULT_FUSE_A,
    DEFAULT_MARGIN_A,
    DEFAULT_NAME,
    DEFAULT_PHASES,
    DEFAULT_VOLTAGE,
    DOMAIN,
    KIND_SETPOINT,
    LOAD_PHASES_ALL,
    MISSING_HOLD,
    MISSING_PAUSE,
    PHASE_KEYS,
    SIGN_NEGATIVE,
    SIGN_POSITIVE,
)

_LOGGER = logging.getLogger(__name__)

# Optional load fields; dropped when left empty.
_OPTIONAL_LOAD_KEYS = (
    CONF_TARGET_ENTITY,
    CONF_TARGET_W,
    CONF_ACTIVE_CONDITIONS,
    CONF_READBACK_ENTITY,
    CONF_ACTUAL_ENTITY,
)


class _ConditionSelector(selector.ConditionSelector):
    """Condition picker that stores what the user built, as plain JSON.

    The stock selector returns validated conditions, which can contain
    template objects that config entries can't store. This one only checks.
    """

    def __call__(self, data: Any) -> Any:
        super().__call__(copy.deepcopy(data))
        return data


async def _conditions_error(hass: HomeAssistant, conditions: list[dict] | None) -> str | None:
    """Return an error key if the conditions can't be used, else None."""
    if not conditions:
        return None
    try:
        validated = await condition.async_validate_conditions_config(
            hass, cv.CONDITIONS_SCHEMA(copy.deepcopy(conditions))
        )
        await condition.async_conditions_from_config(hass, validated, _LOGGER, DOMAIN)
    except (vol.Invalid, HomeAssistantError):
        return "invalid_conditions"
    return None


def _number(
    minimum: float, maximum: float, step: float, unit: str | None = None
) -> selector.NumberSelector:
    config = selector.NumberSelectorConfig(
        min=minimum, max=maximum, step=step, mode=selector.NumberSelectorMode.BOX
    )
    if unit is not None:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


def _select(options: list[str], key: str) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            translation_key=key,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _fuse_schema(include_name: bool) -> vol.Schema:
    fields: dict[Any, Any] = {}
    if include_name:
        fields[vol.Required(CONF_NAME, default=DEFAULT_NAME)] = selector.TextSelector()
    fields.update(
        {
            vol.Required(CONF_FUSE_A, default=DEFAULT_FUSE_A): _number(
                6, 80, 1, UnitOfElectricCurrent.AMPERE
            ),
            vol.Required(CONF_PHASES, default=str(DEFAULT_PHASES)): _select(
                ["1", "3"], CONF_PHASES
            ),
            vol.Required(CONF_MARGIN_A, default=DEFAULT_MARGIN_A): _number(
                0, 5, 0.1, UnitOfElectricCurrent.AMPERE
            ),
            vol.Required(CONF_VOLTAGE, default=DEFAULT_VOLTAGE): _number(100, 260, 1, "V"),
        }
    )
    return vol.Schema(fields)


def _sources_schema(phases: int) -> vol.Schema:
    current_sensors = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="current", multiple=True)
    )
    fields: dict[Any, Any] = {vol.Required(key): current_sensors for key in PHASE_KEYS[:phases]}
    fields[vol.Required(CONF_FRESHNESS_S, default=DEFAULT_FRESHNESS_S)] = _number(10, 600, 1, "s")
    return vol.Schema(fields)


def _load_schema(phases: int) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_SETPOINT_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="number")
            ),
            vol.Optional(CONF_TARGET_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["input_number", "number", "sensor"])
            ),
            vol.Optional(CONF_TARGET_W): _number(0, 100000, 100, UnitOfPower.WATT),
            vol.Optional(CONF_ACTIVE_CONDITIONS): _ConditionSelector(),
            vol.Optional(CONF_READBACK_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor", "number"])
            ),
            vol.Optional(CONF_ACTUAL_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="power")
            ),
            vol.Required(CONF_ACTUAL_SIGN, default=SIGN_POSITIVE): _select(
                [SIGN_POSITIVE, SIGN_NEGATIVE], CONF_ACTUAL_SIGN
            ),
            vol.Required(CONF_LOAD_PHASES, default=LOAD_PHASES_ALL): _select(
                [LOAD_PHASES_ALL, *PHASE_KEYS[:phases]], CONF_LOAD_PHASES
            ),
        }
    )


def _advanced_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_INTERVAL_S): _number(2, 60, 1, "s"),
            vol.Required(CONF_MIN_OVER_A): _number(0, 5, 0.05, UnitOfElectricCurrent.AMPERE),
            vol.Required(CONF_BASE_DROP_W): _number(0, 5000, 10, UnitOfPower.WATT),
            vol.Required(CONF_CUT_INTERVAL_S): _number(2, 600, 1, "s"),
            vol.Required(CONF_RESPONSE_S): _number(0, 120, 1, "s"),
            vol.Required(CONF_COOLDOWN_S): _number(0, 3600, 1, "s"),
            vol.Required(CONF_SETTLING_S): _number(0, 600, 1, "s"),
            vol.Required(CONF_RECOVERY_INTERVAL_S): _number(5, 3600, 1, "s"),
            vol.Required(CONF_RECOVERY_GAIN): _number(0.1, 1, 0.05),
            vol.Required(CONF_STEP_W): _number(1, 1000, 1, UnitOfPower.WATT),
            vol.Required(CONF_STARTUP_HOLD_S): _number(0, 600, 1, "s"),
            vol.Required(CONF_ON_MISSING_DATA): _select(
                [MISSING_PAUSE, MISSING_HOLD], CONF_ON_MISSING_DATA
            ),
        }
    )


def _validate_fuse(user_input: dict[str, Any]) -> dict[str, str]:
    if user_input[CONF_MARGIN_A] >= user_input[CONF_FUSE_A]:
        return {CONF_MARGIN_A: "margin_too_large"}
    return {}


def _validate_sources(
    hass: HomeAssistant, user_input: dict[str, Any], phases: int
) -> dict[str, str]:
    errors: dict[str, str] = {}
    for key in PHASE_KEYS[:phases]:
        entity_ids = user_input.get(key) or []
        if not entity_ids:
            errors[key] = "no_sensors"
            continue
        for entity_id in entity_ids:
            state = hass.states.get(entity_id)
            # A sensor that is unavailable during setup has no unit to check.
            if state is not None and state.attributes.get("unit_of_measurement") not in (
                None,
                UnitOfElectricCurrent.AMPERE,
            ):
                errors[key] = "not_amps"
    return errors


def _validate_load(hass: HomeAssistant, user_input: dict[str, Any]) -> dict[str, str]:
    has_entity = bool(user_input.get(CONF_TARGET_ENTITY))
    has_fixed = user_input.get(CONF_TARGET_W) is not None
    if has_entity == has_fixed:
        return {"base": "target_one_of"}
    setpoint = hass.states.get(user_input[CONF_SETPOINT_ENTITY])
    if setpoint is None:
        return {CONF_SETPOINT_ENTITY: "entity_missing"}
    if setpoint.attributes.get("unit_of_measurement") != UnitOfPower.WATT:
        return {CONF_SETPOINT_ENTITY: "not_watts"}
    maximum = setpoint.attributes.get("max")
    if has_fixed and maximum is not None and user_input[CONF_TARGET_W] > maximum:
        return {CONF_TARGET_W: "above_setpoint_max"}
    return {}


def _fuse_values(user_input: dict[str, Any]) -> dict[str, Any]:
    values = dict(user_input)
    values[CONF_PHASES] = int(values[CONF_PHASES])
    return values


def _sources_values(user_input: dict[str, Any], phases: int) -> dict[str, Any]:
    return {
        CONF_PHASE_SENSORS: {key: list(user_input[key]) for key in PHASE_KEYS[:phases]},
        CONF_FRESHNESS_S: user_input[CONF_FRESHNESS_S],
    }


def _load_values(user_input: dict[str, Any]) -> dict[str, Any]:
    load = {CONF_KIND: KIND_SETPOINT, **user_input}
    for key in _OPTIONAL_LOAD_KEYS:
        if load.get(key) in (None, "", []):
            load.pop(key, None)
    return load


def _form_values(options: dict[str, Any]) -> dict[str, Any]:
    """Flatten stored options into the shape the forms use (for suggested values)."""
    values = dict(options)
    values[CONF_PHASES] = str(options.get(CONF_PHASES, DEFAULT_PHASES))
    values.update(options.get(CONF_PHASE_SENSORS, {}))
    if options.get(CONF_LOADS):
        values.update(options[CONF_LOADS][0])
    return values


class FuseGuardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a Fuse Guard: fuse, phase sensors, controllable load."""

    VERSION = 1

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_fuse(user_input)
            if not errors:
                self._options.update(_fuse_values(user_input))
                return await self.async_step_sources()
        schema = self.add_suggested_values_to_schema(_fuse_schema(True), user_input or {})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        phases = self._options[CONF_PHASES]
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_sources(self.hass, user_input, phases)
            if not errors:
                self._options.update(_sources_values(user_input, phases))
                return await self.async_step_load()
        schema = self.add_suggested_values_to_schema(_sources_schema(phases), user_input or {})
        return self.async_show_form(step_id="sources", data_schema=schema, errors=errors)

    async def async_step_load(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        phases = self._options[CONF_PHASES]
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_load(self.hass, user_input)
            if not errors and (
                error := await _conditions_error(self.hass, user_input.get(CONF_ACTIVE_CONDITIONS))
            ):
                errors[CONF_ACTIVE_CONDITIONS] = error
            if not errors:
                # One guard per setpoint, so two guards can never fight over a load.
                await self.async_set_unique_id(user_input[CONF_SETPOINT_ENTITY])
                self._abort_if_unique_id_configured()
                self._options[CONF_LOADS] = [_load_values(user_input)]
                self._options.update(DEFAULT_ADVANCED)
                return self.async_create_entry(
                    title=self._options[CONF_NAME], data={}, options=self._options
                )
        schema = self.add_suggested_values_to_schema(_load_schema(phases), user_input or {})
        return self.async_show_form(step_id="load", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> FuseGuardOptionsFlow:
        return FuseGuardOptionsFlow()


class FuseGuardOptionsFlow(OptionsFlow):
    """Change any part of the setup; each menu entry saves on its own."""

    def __init__(self) -> None:
        self._pending: dict[str, Any] = {}

    @property
    def _current(self) -> dict[str, Any]:
        return {**self.config_entry.options, **self._pending}

    def _save(self) -> ConfigFlowResult:
        return self.async_create_entry(data=self._current)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["fuse", "sources", "load", "advanced"]
        )

    async def async_step_fuse(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_fuse(user_input)
            if not errors:
                old_phases = self._current[CONF_PHASES]
                self._pending.update(_fuse_values(user_input))
                if self._pending[CONF_PHASES] != old_phases:
                    # The phase sensors (and the load's phase) must match the new count.
                    return await self.async_step_sources()
                return self._save()
        schema = self.add_suggested_values_to_schema(
            _fuse_schema(False), user_input or _form_values(self._current)
        )
        return self.async_show_form(step_id="fuse", data_schema=schema, errors=errors)

    async def async_step_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        phases = self._current[CONF_PHASES]
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_sources(self.hass, user_input, phases)
            if not errors:
                self._pending.update(_sources_values(user_input, phases))
                load = self._current[CONF_LOADS][0]
                if load.get(CONF_LOAD_PHASES, LOAD_PHASES_ALL) not in (
                    LOAD_PHASES_ALL,
                    *PHASE_KEYS[:phases],
                ):
                    return await self.async_step_load()
                return self._save()
        schema = self.add_suggested_values_to_schema(
            _sources_schema(phases), user_input or _form_values(self._current)
        )
        return self.async_show_form(step_id="sources", data_schema=schema, errors=errors)

    async def async_step_load(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        phases = self._current[CONF_PHASES]
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_load(self.hass, user_input)
            if not errors and (
                error := await _conditions_error(self.hass, user_input.get(CONF_ACTIVE_CONDITIONS))
            ):
                errors[CONF_ACTIVE_CONDITIONS] = error
            setpoint = user_input[CONF_SETPOINT_ENTITY]
            if not errors and any(
                entry.unique_id == setpoint
                for entry in self.hass.config_entries.async_entries(DOMAIN)
                if entry.entry_id != self.config_entry.entry_id
            ):
                errors[CONF_SETPOINT_ENTITY] = "already_guarded"
            if not errors:
                self._pending[CONF_LOADS] = [_load_values(user_input)]
                if self.config_entry.unique_id != setpoint:
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, unique_id=setpoint
                    )
                return self._save()
        schema = self.add_suggested_values_to_schema(
            _load_schema(phases), user_input or _form_values(self._current)
        )
        return self.async_show_form(step_id="load", data_schema=schema, errors=errors)

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._pending.update(user_input)
            return self._save()
        schema = self.add_suggested_values_to_schema(
            _advanced_schema(), {**DEFAULT_ADVANCED, **self._current}
        )
        return self.async_show_form(step_id="advanced", data_schema=schema)
