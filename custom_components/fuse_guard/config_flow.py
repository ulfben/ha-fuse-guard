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
from homeassistant.data_entry_flow import section
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
from .meters import Meter
from .setup_checks import (
    discover_meters,
    load_check_text,
    meter_label,
    phase_check,
    phase_check_text,
)

CONF_METERS = "meters"  # flow-only field: the meters picked in the first sensor step
# Flow-only: the collapsible section that holds the optional device feedback.
SECTION_FEEDBACK = "feedback"
_FEEDBACK_KEYS = (CONF_READBACK_ENTITY, CONF_ACTUAL_ENTITY, CONF_ACTUAL_SIGN)

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


def _load_schema(phases: int, collapsed: bool = True) -> vol.Schema:
    feedback = vol.Schema(
        {
            vol.Optional(CONF_READBACK_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    filter=[
                        selector.EntityFilterSelectorConfig(domain="sensor", device_class="power"),
                        selector.EntityFilterSelectorConfig(domain="number"),
                    ]
                )
            ),
            vol.Optional(CONF_ACTUAL_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="power")
            ),
            vol.Required(CONF_ACTUAL_SIGN, default=SIGN_POSITIVE): _select(
                [SIGN_POSITIVE, SIGN_NEGATIVE], CONF_ACTUAL_SIGN
            ),
        }
    )
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
            vol.Required(CONF_LOAD_PHASES, default=LOAD_PHASES_ALL): _select(
                [LOAD_PHASES_ALL, *PHASE_KEYS[:phases]], CONF_LOAD_PHASES
            ),
            vol.Optional(SECTION_FEEDBACK, default={}): section(feedback, {"collapsed": collapsed}),
        }
    )


def _flatten_load(user_input: dict[str, Any]) -> dict[str, Any]:
    """Lift the feedback section's fields to the top level, where they are stored."""
    values = {k: v for k, v in user_input.items() if k != SECTION_FEEDBACK}
    values.update(user_input.get(SECTION_FEEDBACK) or {})
    return values


def _nest_load(values: dict[str, Any]) -> dict[str, Any]:
    """The reverse of _flatten_load, for the form's suggested values."""
    nested = {k: v for k, v in values.items() if k not in _FEEDBACK_KEYS}
    nested[SECTION_FEEDBACK] = {k: values[k] for k in _FEEDBACK_KEYS if k in values}
    return nested


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


def _meters_schema(hass: HomeAssistant, meters: list[Meter], picked: list[str]) -> vol.Schema:
    options = [selector.SelectOptionDict(value=m.key, label=meter_label(hass, m)) for m in meters]
    return vol.Schema(
        {
            vol.Optional(CONF_METERS, default=picked): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options, multiple=True, mode=selector.SelectSelectorMode.LIST
                )
            )
        }
    )


class _SetupSteps:
    """Steps shared by the config and options flows: meters, sensors, checks, load.

    Subclasses provide `_data` (the options so far) and `_update`, and name the
    steps that follow the phase check and the load check.
    """

    hass: HomeAssistant
    _prefill: dict[str, list[str]] | None = None
    _pending_load: dict[str, Any] | None = None
    _after_sources_step = "load"
    _after_load_step = "finish"

    @property
    def _data(self) -> dict[str, Any]:
        raise NotImplementedError

    def _update(self, values: dict[str, Any]) -> None:
        raise NotImplementedError

    async def async_step_meters(self, user_input: dict[str, Any] | None = None):
        """Offer the meters found among the current sensors; their phases prefill the next form."""
        phases = self._data[CONF_PHASES]
        meters = discover_meters(self.hass, phases)
        if user_input is not None or not meters:
            chosen = [m for m in meters if m.key in (user_input or {}).get(CONF_METERS, [])]
            if chosen:
                # A sensor that is already set up keeps its phase; the A/B/C guess
                # only applies to sensors that are new to this guard.
                current = {
                    e: p for p, ids in self._data.get(CONF_PHASE_SENSORS, {}).items() for e in ids
                }
                self._prefill = {p: [] for p in PHASE_KEYS[:phases]}
                for meter in chosen:
                    for guessed, entity_id in meter.sensors.items():
                        self._prefill[current.get(entity_id, guessed)].append(entity_id)
            return await self.async_step_sources()
        # Tick the meters whose sensors are all in use already (Configure).
        in_use = {e for ids in self._data.get(CONF_PHASE_SENSORS, {}).values() for e in ids}
        picked = [m.key for m in meters if set(m.sensors.values()) <= in_use]
        return self.async_show_form(
            step_id="meters",
            data_schema=_meters_schema(self.hass, meters, picked),
            description_placeholders={"count": str(len(meters))},
        )

    async def async_step_sources(self, user_input: dict[str, Any] | None = None):
        phases = self._data[CONF_PHASES]
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_sources(self.hass, user_input, phases)
            if not errors:
                self._update(_sources_values(user_input, phases))
                return await self.async_step_check()
        suggested = user_input or {**_form_values(self._data), **(self._prefill or {})}
        schema = self.add_suggested_values_to_schema(_sources_schema(phases), suggested)
        return self.async_show_form(step_id="sources", data_schema=schema, errors=errors)

    def _phase_check(self):
        return phase_check(
            self.hass,
            self._data[CONF_PHASE_SENSORS],
            self._data[CONF_FRESHNESS_S],
            max_plausible_a=3 * self._data[CONF_FUSE_A],
        )

    async def async_step_check(self, user_input: dict[str, Any] | None = None):
        """Show what every picked sensor reads right now, and what looks wrong."""
        result = self._phase_check()
        options = [self._after_sources_step]
        if result.suggestion:
            options.append("apply_suggestion")
        options += ["sources", "check"]
        return self.async_show_menu(
            step_id="check",
            menu_options=options,
            description_placeholders=phase_check_text(result),
        )

    async def async_step_apply_suggestion(self, user_input: dict[str, Any] | None = None):
        result = self._phase_check()
        if result.suggestion:
            sensors = {p: list(ids) for p, ids in self._data[CONF_PHASE_SENSORS].items()}
            for entity_id, phase in result.suggestion.items():
                for ids in sensors.values():
                    if entity_id in ids:
                        ids.remove(entity_id)
                sensors.setdefault(phase, []).append(entity_id)
            self._update({CONF_PHASE_SENSORS: sensors})
            self._prefill = None
        return await self.async_step_check()

    async def _validate_load_input(self, user_input: dict[str, Any]) -> dict[str, str]:
        errors = _validate_load(self.hass, user_input)
        if not errors and (
            error := await _conditions_error(self.hass, user_input.get(CONF_ACTIVE_CONDITIONS))
        ):
            errors[CONF_ACTIVE_CONDITIONS] = error
        return errors

    def _load_form(self, values: dict[str, Any] | None, errors: dict[str, str]):
        values = values or _form_values(self._data)
        # Open the feedback section only when something in it is already set.
        collapsed = not any(values.get(k) for k in (CONF_READBACK_ENTITY, CONF_ACTUAL_ENTITY))
        schema = self.add_suggested_values_to_schema(
            _load_schema(self._data[CONF_PHASES], collapsed), _nest_load(values)
        )
        return self.async_show_form(step_id="load", data_schema=schema, errors=errors)

    async def async_step_load_check(self, user_input: dict[str, Any] | None = None):
        """Show the load's entities as they read now, and anything inconsistent."""
        return self.async_show_menu(
            step_id="load_check",
            menu_options=[self._after_load_step, "load"],
            description_placeholders=await load_check_text(self.hass, self._pending_load or {}),
        )


class FuseGuardConfigFlow(_SetupSteps, ConfigFlow, domain=DOMAIN):
    """Set up a Fuse Guard: fuse, meters, phase sensors, controllable load."""

    VERSION = 1

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    @property
    def _data(self) -> dict[str, Any]:
        return self._options

    def _update(self, values: dict[str, Any]) -> None:
        self._options.update(values)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_fuse(user_input)
            if not errors:
                self._options.update(_fuse_values(user_input))
                self._options.setdefault(CONF_FRESHNESS_S, DEFAULT_FRESHNESS_S)
                return await self.async_step_meters()
        schema = self.add_suggested_values_to_schema(_fuse_schema(True), user_input or {})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_load(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = _flatten_load(user_input)
            errors = await self._validate_load_input(user_input)
            if not errors:
                # One guard per setpoint, so two guards can never fight over a load.
                await self.async_set_unique_id(user_input[CONF_SETPOINT_ENTITY])
                self._abort_if_unique_id_configured()
                self._pending_load = _load_values(user_input)
                return await self.async_step_load_check()
        return self._load_form(user_input or self._pending_load, errors)

    async def async_step_finish(self, user_input: dict[str, Any] | None = None):
        self._options[CONF_LOADS] = [self._pending_load]
        self._options.update(DEFAULT_ADVANCED)
        return self.async_create_entry(
            title=self._options[CONF_NAME], data={}, options=self._options
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> FuseGuardOptionsFlow:
        return FuseGuardOptionsFlow()


class FuseGuardOptionsFlow(_SetupSteps, OptionsFlow):
    """Change any part of the setup; each menu entry saves on its own."""

    _after_sources_step = "save"
    _after_load_step = "save"

    def __init__(self) -> None:
        self._pending: dict[str, Any] = {}

    @property
    def _data(self) -> dict[str, Any]:
        return {**self.config_entry.options, **self._pending}

    def _update(self, values: dict[str, Any]) -> None:
        self._pending.update(values)

    async def async_step_save(self, user_input: dict[str, Any] | None = None):
        if self._pending_load is not None:
            self._pending[CONF_LOADS] = [self._pending_load]
            setpoint = self._pending_load[CONF_SETPOINT_ENTITY]
            if self.config_entry.unique_id != setpoint:
                self.hass.config_entries.async_update_entry(self.config_entry, unique_id=setpoint)
        else:
            # A phase count change can leave the load on a phase that no longer exists.
            load = self._data[CONF_LOADS][0]
            if load.get(CONF_LOAD_PHASES, LOAD_PHASES_ALL) not in (
                LOAD_PHASES_ALL,
                *PHASE_KEYS[: self._data[CONF_PHASES]],
            ):
                return await self.async_step_load()
        return self.async_create_entry(data=self._data)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["fuse", "meters", "load", "advanced"]
        )

    async def async_step_fuse(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_fuse(user_input)
            if not errors:
                old_phases = self._data[CONF_PHASES]
                self._pending.update(_fuse_values(user_input))
                if self._pending[CONF_PHASES] != old_phases:
                    # The phase sensors (and the load's phase) must match the new count.
                    return await self.async_step_meters()
                return await self.async_step_save()
        schema = self.add_suggested_values_to_schema(
            _fuse_schema(False), user_input or _form_values(self._data)
        )
        return self.async_show_form(step_id="fuse", data_schema=schema, errors=errors)

    async def async_step_load(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = _flatten_load(user_input)
            errors = await self._validate_load_input(user_input)
            setpoint = user_input[CONF_SETPOINT_ENTITY]
            if not errors and any(
                entry.unique_id == setpoint
                for entry in self.hass.config_entries.async_entries(DOMAIN)
                if entry.entry_id != self.config_entry.entry_id
            ):
                errors[CONF_SETPOINT_ENTITY] = "already_guarded"
            if not errors:
                self._pending_load = _load_values(user_input)
                return await self.async_step_load_check()
        return self._load_form(user_input or self._pending_load, errors)

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._pending.update(user_input)
            return await self.async_step_save()
        schema = self.add_suggested_values_to_schema(
            _advanced_schema(), {**DEFAULT_ADVANCED, **self._data}
        )
        return self.async_show_form(step_id="advanced", data_schema=schema)
