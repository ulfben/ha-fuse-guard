"""Home Assistant runtime for one Fuse Guard: reads states, decides, writes."""

from __future__ import annotations

import asyncio
import copy
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import condition
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

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
    CONF_LOAD_PHASES,
    CONF_LOADS,
    CONF_MARGIN_A,
    CONF_MIN_OVER_A,
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
    DOMAIN,
    EVENT_ACTION,
    LOAD_PHASES_ALL,
    MISSING_PAUSE,
    MODE_ACTIVE,
    MODE_OFF,
    MODE_SHADOW,
    PHASE_KEYS,
    SIGN_NEGATIVE,
)
from .controller import Decision, GuardState, Inputs, Settings, Status, decide
from .sources import PhaseSample, Reading, select_phase

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
HISTORY_LENGTH = 50
# Statuses in which the guard is actually holding the load back.
_CONTROLLING = frozenset(
    {Status.LIMITING, Status.PAUSED, Status.HOLDING, Status.RECOVERING, Status.FAIL_SAFE}
)


def _number(state: State | None) -> float | None:
    if state is None:
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


class FuseGuard:
    """Owns one guard: its subscriptions, its persisted state and its decisions."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.options: dict[str, Any] = {**DEFAULT_ADVANCED, **entry.options}
        self.load: dict[str, Any] = self.options[CONF_LOADS][0]
        self.phase_keys: tuple[str, ...] = PHASE_KEYS[: self.options[CONF_PHASES]]
        load_phases = self.load.get(CONF_LOAD_PHASES, LOAD_PHASES_ALL)
        self.load_phases = self.phase_keys if load_phases == LOAD_PHASES_ALL else (load_phases,)
        self.setpoint_entity: str = self.load[CONF_SETPOINT_ENTITY]
        self.readback_entity: str | None = self.load.get(CONF_READBACK_ENTITY)
        self.fixed_target = CONF_TARGET_ENTITY not in self.load

        self.mode = MODE_SHADOW
        self.target_w: float = float(self.load.get(CONF_TARGET_W, 0))
        self.state = GuardState(ceiling_w=0.0, started_ts=0.0)
        self.decision: Decision | None = None
        self.phases: dict[str, PhaseSample] = {}
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY_LENGTH)

        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubscribe: list[CALLBACK_TYPE] = []
        self._lock = asyncio.Lock()
        self._pending = False
        self._active_check: Callable[[Any], bool] | None = None
        self._condition_entities: list[str] = []

    # Lifecycle

    async def async_start(self) -> None:
        """Restore persisted state and start listening."""
        await self._compile_conditions()
        stored = await self._store.async_load() or {}
        self.mode = stored.get("mode", MODE_SHADOW)
        if self.fixed_target:
            self.target_w = float(stored.get("target_w", self.target_w))
        now = dt_util.utcnow().timestamp()
        # A new guard starts at the full target, or the setpoint's max if the
        # target isn't known yet; never at 0, which would start a ramp.
        setpoint = self.hass.states.get(self.setpoint_entity)
        initial = self.effective_target_w or (
            float(setpoint.attributes.get("max", 0)) if setpoint else 0.0
        )
        self.state = GuardState(
            ceiling_w=float(stored.get("ceiling_w", initial)),
            started_ts=now,  # the startup hold counts from setup
            last_cut_ts=stored.get("last_cut_ts"),
            last_recovery_ts=stored.get("last_recovery_ts"),
        )
        self._unsubscribe.append(
            async_track_state_change_event(self.hass, self._watched_entities(), self._on_change)
        )
        self._unsubscribe.append(
            async_track_time_interval(
                self.hass,
                self._on_tick,
                timedelta(seconds=self.options[CONF_INTERVAL_S]),
                name=f"{DOMAIN} watchdog",
            )
        )
        await self.async_evaluate()

    async def async_stop(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        await self._store.async_save(self._stored())

    def _watched_entities(self) -> list[str]:
        entities = [e for key in self.phase_keys for e in self.options[CONF_PHASE_SENSORS][key]]
        for key in (
            CONF_SETPOINT_ENTITY,
            CONF_READBACK_ENTITY,
            CONF_ACTUAL_ENTITY,
            CONF_TARGET_ENTITY,
        ):
            if entity_id := self.load.get(key):
                entities.append(entity_id)
        return sorted({*entities, *self._condition_entities})

    async def _compile_conditions(self) -> None:
        """Turn the stored 'only write while' conditions into one check."""
        raw = self.load.get(CONF_ACTIVE_CONDITIONS) or []
        if not raw:
            return
        validated = await condition.async_validate_conditions_config(
            self.hass, cv.CONDITIONS_SCHEMA(copy.deepcopy(raw))
        )
        self._active_check = await condition.async_conditions_from_config(
            self.hass, validated, _LOGGER, self.entry.title
        )
        self._condition_entities = sorted(
            {e for config in validated for e in condition.async_extract_entities(config)}
        )

    def _stored(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "target_w": self.target_w,
            "ceiling_w": self.state.ceiling_w,
            "last_cut_ts": self.state.last_cut_ts,
            "last_recovery_ts": self.state.last_recovery_ts,
        }

    # Entities

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> Callable[[], None]:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    async def async_set_mode(self, mode: str) -> None:
        self.mode = mode
        self._store.async_delay_save(self._stored, 1)
        await self.async_evaluate()

    async def async_set_target(self, target_w: float) -> None:
        self.target_w = target_w
        self._store.async_delay_save(self._stored, 1)
        await self.async_evaluate()

    @property
    def effective_target_w(self) -> float | None:
        if self.fixed_target:
            return self.target_w
        return _number(self.hass.states.get(self.load[CONF_TARGET_ENTITY]))

    # Evaluation

    @callback
    def _on_change(self, event: Event) -> None:
        self.hass.async_create_task(self.async_evaluate(), eager_start=True)

    @callback
    def _on_tick(self, now: Any) -> None:
        self.hass.async_create_task(self.async_evaluate(), eager_start=True)

    async def async_evaluate(self) -> None:
        """Run one decision. Calls arriving meanwhile collapse into one rerun."""
        if self._lock.locked():
            self._pending = True
            return
        async with self._lock:
            while True:
                self._pending = False
                await self._evaluate_once()
                if not self._pending:
                    break
        for update in list(self._listeners):
            update()

    def _settings(self) -> Settings:
        o = self.options
        setpoint = self.hass.states.get(self.setpoint_entity)
        max_w = float(setpoint.attributes.get("max", 0)) if setpoint else 0.0
        return Settings(
            threshold_a=o[CONF_FUSE_A] - o[CONF_MARGIN_A],
            watts_per_amp=o[CONF_VOLTAGE] * len(self.load_phases),
            load_phases=self.load_phases,
            max_w=max_w,
            has_actual=CONF_ACTUAL_ENTITY in self.load,
            min_over_a=o[CONF_MIN_OVER_A],
            base_drop_w=o[CONF_BASE_DROP_W],
            cut_interval_s=o[CONF_CUT_INTERVAL_S],
            response_s=o[CONF_RESPONSE_S],
            cooldown_s=o[CONF_COOLDOWN_S],
            settling_s=o[CONF_SETTLING_S],
            recovery_interval_s=o[CONF_RECOVERY_INTERVAL_S],
            recovery_gain=o[CONF_RECOVERY_GAIN],
            step_w=o[CONF_STEP_W],
            startup_hold_s=o[CONF_STARTUP_HOLD_S],
            pause_on_missing_data=o[CONF_ON_MISSING_DATA] == MISSING_PAUSE,
        )

    def _fresh_number(self, entity_id: str | None, now: float) -> float | None:
        """Value of a sensor that must keep reporting, or None if it is stale."""
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or state.attributes.get("restored", False):
            return None
        if not 0 <= now - state.last_reported_timestamp <= self.options[CONF_FRESHNESS_S]:
            return None
        return _number(state)

    def _inputs(self, now: float) -> Inputs:
        sensors = self.options[CONF_PHASE_SENSORS]
        self.phases = {}
        for key in self.phase_keys:
            readings = [
                Reading(
                    s.entity_id,
                    s.state,
                    s.last_reported_timestamp,
                    bool(s.attributes.get("restored", False)),
                )
                for entity_id in sensors[key]
                if (s := self.hass.states.get(entity_id)) is not None
            ]
            self.phases[key] = select_phase(readings, now, self.options[CONF_FRESHNESS_S])

        # A separate readback sensor must keep reporting; the setpoint's own
        # state only changes when its value does.
        if self.readback_entity:
            readback = self._fresh_number(self.readback_entity, now)
        else:
            readback = _number(self.hass.states.get(self.setpoint_entity))

        actual = self._fresh_number(self.load.get(CONF_ACTUAL_ENTITY), now)
        if actual is not None and self.load.get(CONF_ACTUAL_SIGN) == SIGN_NEGATIVE:
            actual = -actual

        changed = [self.hass.states.get(self.readback_entity or self.setpoint_entity)]
        condition_states = [self.hass.states.get(e) for e in self._condition_entities]
        changed += condition_states
        active: bool | None = True
        if self._active_check is not None:
            if any(
                s is None or s.state in (STATE_UNAVAILABLE, STATE_UNKNOWN) for s in condition_states
            ):
                active = None  # can't tell: never write, keep the ceiling
            else:
                try:
                    active = bool(self._active_check(None))
                except HomeAssistantError as err:
                    _LOGGER.warning("Could not check active conditions: %s", err)
                    active = None
        command_changed_ts = max(
            (s.last_changed_timestamp for s in changed if s is not None), default=0.0
        )

        return Inputs(
            now=now,
            phases=self.phases,
            target_w=self.effective_target_w,
            readback_w=readback,
            actual_w=actual,
            active=active,
            command_changed_ts=command_changed_ts,
        )

    async def _evaluate_once(self) -> None:
        now = dt_util.utcnow().timestamp()
        inputs = self._inputs(now)
        if self.mode == MODE_OFF:
            self.decision = None
            return

        decision = decide(self._settings(), inputs, self.state)
        previous = self.decision
        self.decision = decision
        if self.mode == MODE_SHADOW and decision.write_w is not None:
            # Nothing was written, so don't pretend the setpoint is on its way.
            decision = replace(
                decision,
                state=replace(
                    decision.state,
                    last_write_w=self.state.last_write_w,
                    last_write_ts=self.state.last_write_ts,
                ),
            )
            self.decision = decision
        self.state = decision.state

        if self.mode == MODE_ACTIVE and decision.write_w is not None:
            try:
                await self.hass.services.async_call(
                    "number",
                    "set_value",
                    {ATTR_ENTITY_ID: self.setpoint_entity, "value": decision.write_w},
                    blocking=True,
                )
            except (HomeAssistantError, ValueError) as err:
                _LOGGER.warning("Could not set %s: %s", self.setpoint_entity, err)

        changed = previous is None or (
            previous.reason != decision.reason or previous.ceiling_w != decision.ceiling_w
        )
        # In shadow mode the setpoint never follows, so every evaluation "wants" to
        # write; only log when the decision itself changes.
        if changed or (self.mode == MODE_ACTIVE and decision.write_w is not None):
            self._record(decision)
        if previous is None or previous.ceiling_w != decision.ceiling_w:
            self._store.async_delay_save(self._stored, 5)

    def _record(self, decision: Decision) -> None:
        entry = {
            "time": dt_util.utcnow().isoformat(),
            "mode": self.mode,
            "status": decision.status.value,
            "message": decision.message,
            "write_w": decision.write_w,
        }
        self.history.append(entry)
        if decision.write_w is not None:
            self.hass.bus.async_fire(
                EVENT_ACTION,
                {
                    "entry_id": self.entry.entry_id,
                    "name": self.entry.title,
                    "mode": self.mode,
                    "status": decision.status.value,
                    "message": decision.message,
                    "write_w": decision.write_w,
                    "setpoint_entity": self.setpoint_entity,
                },
            )

    # Presentation

    @property
    def status(self) -> Status:
        if self.mode == MODE_OFF or self.decision is None:
            return Status.OFF
        return self.decision.status

    @property
    def limiting(self) -> bool:
        target = self.effective_target_w
        return (
            self.mode != MODE_OFF
            and self.decision is not None
            and self.decision.status in _CONTROLLING
            and target is not None
            and self.decision.ceiling_w < target
        )

    @property
    def summary(self) -> str:
        """One plain-language line for dashboards."""
        if self.mode == MODE_OFF or self.decision is None:
            return "Fuse guard off"
        d = self.decision
        prefix = "Shadow · " if self.mode == MODE_SHADOW else ""
        target = (self.effective_target_w or 0) / 1000
        if d.status in (Status.FAIL_SAFE, Status.STARTUP):
            text = "Starting up" if d.status is Status.STARTUP else "Meter data missing"
        elif d.status is Status.INACTIVE:
            text = "Load not active"
        elif d.status is Status.ZERO_REQUEST:
            text = "No power requested"
        elif d.ceiling_w <= 0:
            text = "Load paused by fuse guard"
        elif self.limiting:
            text = f"Limiting · {d.ceiling_w / 1000:.1f} of {target:.1f} kW"
        else:
            text = f"Full rate · {target:.1f} kW"
        return prefix + text

    @property
    def highest_current_a(self) -> float | None:
        values = [p.current for p in self.phases.values() if p.current is not None]
        return max(values) if len(values) == len(self.phases) and values else None

    @property
    def headroom_a(self) -> float | None:
        highest = self.highest_current_a
        if highest is None:
            return None
        return round(self.options[CONF_FUSE_A] - self.options[CONF_MARGIN_A] - highest, 2)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "target_w": self.effective_target_w,
            "state": asdict(self.state),
            "phases": {key: asdict(sample) for key, sample in self.phases.items()},
            "history": list(self.history),
        }
