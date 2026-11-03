"""Constants for Fuse Guard."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "fuse_guard"

# Fuse
CONF_NAME: Final = "name"
CONF_FUSE_A: Final = "fuse_a"
CONF_PHASES: Final = "phases"
CONF_MARGIN_A: Final = "margin_a"
CONF_VOLTAGE: Final = "voltage"

# Phase sources
CONF_PHASE_SENSORS: Final = "phase_sensors"  # {"l1": [entity_id, ...], ...}
CONF_FRESHNESS_S: Final = "freshness_s"
PHASE_KEYS: Final = ("l1", "l2", "l3")

# Loads. Stored as a list so v2 can add more loads (and switched loads)
# without migrating existing entries.
CONF_LOADS: Final = "loads"
CONF_KIND: Final = "kind"
KIND_SETPOINT: Final = "setpoint"
CONF_SETPOINT_ENTITY: Final = "setpoint_entity"
CONF_TARGET_ENTITY: Final = "target_entity"
CONF_TARGET_W: Final = "target_w"
CONF_ACTIVE_CONDITIONS: Final = "active_conditions"  # HA condition configs, ANDed
CONF_READBACK_ENTITY: Final = "readback_entity"
CONF_ACTUAL_ENTITY: Final = "actual_entity"
CONF_ACTUAL_SIGN: Final = "actual_sign"
SIGN_POSITIVE: Final = "positive"  # positive while consuming
SIGN_NEGATIVE: Final = "negative"  # negative while consuming (e.g. Sungrow battery)
CONF_LOAD_PHASES: Final = "load_phases"
LOAD_PHASES_ALL: Final = "all"

# Advanced (options only). Defaults match the automation this replaces.
CONF_INTERVAL_S: Final = "interval_s"
CONF_MIN_OVER_A: Final = "min_over_a"
CONF_BASE_DROP_W: Final = "base_drop_w"
CONF_CUT_INTERVAL_S: Final = "cut_interval_s"
CONF_RESPONSE_S: Final = "response_s"
CONF_COOLDOWN_S: Final = "cooldown_s"
CONF_SETTLING_S: Final = "settling_s"
CONF_RECOVERY_INTERVAL_S: Final = "recovery_interval_s"
CONF_RECOVERY_GAIN: Final = "recovery_gain"
CONF_STEP_W: Final = "step_w"
CONF_STARTUP_HOLD_S: Final = "startup_hold_s"
CONF_ON_MISSING_DATA: Final = "on_missing_data"
MISSING_PAUSE: Final = "pause"
MISSING_HOLD: Final = "hold"

DEFAULT_NAME: Final = "Fuse Guard"
DEFAULT_FUSE_A: Final = 16
DEFAULT_PHASES: Final = 3
DEFAULT_MARGIN_A: Final = 0.6
DEFAULT_VOLTAGE: Final = 230
DEFAULT_FRESHNESS_S: Final = 60

DEFAULT_ADVANCED: Final = {
    CONF_INTERVAL_S: 10,
    CONF_MIN_OVER_A: 0.2,
    CONF_BASE_DROP_W: 310,
    CONF_CUT_INTERVAL_S: 10,
    CONF_RESPONSE_S: 8,
    CONF_COOLDOWN_S: 60,
    CONF_SETTLING_S: 30,
    CONF_RECOVERY_INTERVAL_S: 60,
    CONF_RECOVERY_GAIN: 0.6,
    CONF_STEP_W: 100,
    CONF_STARTUP_HOLD_S: 60,
    CONF_ON_MISSING_DATA: MISSING_PAUSE,
}

# Fired on every write (and, in shadow mode, every would-be write).
EVENT_ACTION: Final = "fuse_guard_action"

# Guard modes (select entity)
MODE_OFF: Final = "off"
MODE_SHADOW: Final = "shadow"
MODE_ACTIVE: Final = "active"
