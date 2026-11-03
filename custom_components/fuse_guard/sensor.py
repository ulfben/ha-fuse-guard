"""Sensors: status, ceiling, phase currents, headroom."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfElectricCurrent, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FuseGuardConfigEntry
from .const import CONF_FUSE_A, CONF_MARGIN_A
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
    async_add_entities(entities)


class StatusSensor(FuseGuardEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [status.value for status in Status]
    # For the card only; the logbook already keeps the history.
    _unrecorded_attributes = frozenset({"recent", "fuse_a", "threshold_a"})

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
