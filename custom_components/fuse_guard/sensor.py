"""Sensors: status, ceiling, phase currents, headroom, the last 24 hours."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfElectricCurrent, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FuseGuardConfigEntry
from .const import CONF_FUSE_A, CONF_MARGIN_A, CONF_PHASE_SENSORS
from .controller import Status
from .entity import FuseGuardEntity
from .guard import FuseGuard


async def async_setup_entry(
    hass: HomeAssistant, entry: FuseGuardConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    guard = entry.runtime_data
    entities: list[SensorEntity] = [
        StatusSensor(guard),
        CeilingSensor(guard),
        HighestCurrentSensor(guard),
        HeadroomSensor(guard),
    ]
    entities += [PhaseCurrentSensor(guard, key) for key in guard.phase_keys]
    entities += [
        CutsSensor(guard),
        LimitedStartsSensor(guard),
        TimeOverLimitSensor(guard),
        PeakCurrentSensor(guard),
    ]
    async_add_entities(entities)


class StatusSensor(FuseGuardEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [status.value for status in Status]
    # For the card only; the logbook already keeps the history.
    _unrecorded_attributes = frozenset({"recent", "fuse_a", "threshold_a", "last_24h"})

    def __init__(self, guard: FuseGuard) -> None:
        super().__init__(guard, "status")

    @property
    def native_value(self) -> str:
        return self.guard.status.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.guard.decision
        return {
            "mode": self.guard.mode,
            "summary": self.guard.summary,
            "reason": d.reason if d else None,
            "message": d.message if d else None,
            "ceiling_w": d.ceiling_w if d else None,
            "requested_w": d.requested_w if d else None,
            "readback_w": d.readback_w if d else None,
            "target_w": self.guard.effective_target_w,
            "fuse_a": self.guard.options[CONF_FUSE_A],
            "threshold_a": self.guard.options[CONF_FUSE_A] - self.guard.options[CONF_MARGIN_A],
            "recent": list(self.guard.history)[-5:][::-1],
            "last_24h": asdict(self.guard.last_24h),
        }


class CeilingSensor(FuseGuardEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, guard: FuseGuard) -> None:
        super().__init__(guard, "ceiling")

    @property
    def native_value(self) -> float:
        return self.guard.state.ceiling_w


class _CurrentSensor(FuseGuardEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_suggested_display_precision = 2


class PhaseCurrentSensor(_CurrentSensor):
    """Highest fresh reading of one phase."""

    def __init__(self, guard: FuseGuard, phase: str) -> None:
        super().__init__(guard, f"current_{phase}")
        self.phase = phase

    @property
    def native_value(self) -> float | None:
        sample = self.guard.phases.get(self.phase)
        return sample.current if sample else None

    @property
    def available(self) -> bool:
        return self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sample = self.guard.phases.get(self.phase)
        if sample is None:
            return {}
        return {
            "source_entity": sample.source,
            "sample_timestamp": sample.timestamp,
            "fresh_sources": sample.fresh_sources,
            "total_sources": len(self.guard.options[CONF_PHASE_SENSORS][self.phase]),
            "spread": sample.spread,
        }


class HighestCurrentSensor(_CurrentSensor):
    def __init__(self, guard: FuseGuard) -> None:
        super().__init__(guard, "highest_current")

    @property
    def native_value(self) -> float | None:
        return self.guard.highest_current_a

    @property
    def available(self) -> bool:
        return self.native_value is not None


class HeadroomSensor(_CurrentSensor):
    def __init__(self, guard: FuseGuard) -> None:
        super().__init__(guard, "headroom")

    @property
    def native_value(self) -> float | None:
        return self.guard.headroom_a

    @property
    def available(self) -> bool:
        return self.native_value is not None


# The last 24 hours, rolling: a night of charging spans midnight, so a count
# that reset at midnight would split it. Measurements, not totals: they go down
# as old minutes leave the window.


class CutsSensor(FuseGuardEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:content-cut"

    def __init__(self, guard: FuseGuard) -> None:
        super().__init__(guard, "cuts_24h")

    @property
    def native_value(self) -> int:
        return self.guard.last_24h.cuts


class LimitedStartsSensor(FuseGuardEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:play-speed"

    def __init__(self, guard: FuseGuard) -> None:
        super().__init__(guard, "limited_starts_24h")

    @property
    def native_value(self) -> int:
        return self.guard.last_24h.limited_starts


class TimeOverLimitSensor(FuseGuardEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    def __init__(self, guard: FuseGuard) -> None:
        super().__init__(guard, "over_limit_24h")

    @property
    def native_value(self) -> float:
        return self.guard.last_24h.over_s


class PeakCurrentSensor(_CurrentSensor):
    def __init__(self, guard: FuseGuard) -> None:
        super().__init__(guard, "peak_current_24h")

    @property
    def native_value(self) -> float | None:
        return self.guard.last_24h.peak_a

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ts = self.guard.last_24h.peak_ts
        return {"time": datetime.fromtimestamp(ts, UTC).isoformat() if ts else None}
