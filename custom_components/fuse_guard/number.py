"""Target power number, created only when the target is a fixed value."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FuseGuardConfigEntry
from .const import CONF_STEP_W
from .entity import FuseGuardEntity
from .guard import FuseGuard

DEFAULT_MAX_W = 100000


async def async_setup_entry(
    hass: HomeAssistant, entry: FuseGuardConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    guard = entry.runtime_data
    if guard.fixed_target:
        async_add_entities([TargetNumber(guard)])


class TargetNumber(FuseGuardEntity, NumberEntity):
    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_min_value = 0
    _attr_mode = NumberMode.BOX

    def __init__(self, guard: FuseGuard) -> None:
        super().__init__(guard, "target")
        self._attr_native_step = guard.options[CONF_STEP_W]

    @property
    def native_max_value(self) -> float:
        setpoint = self.hass.states.get(self.guard.setpoint_entity) if self.hass else None
        return float(setpoint.attributes.get("max", DEFAULT_MAX_W)) if setpoint else DEFAULT_MAX_W

    @property
    def native_value(self) -> float:
        return self.guard.target_w

    async def async_set_native_value(self, value: float) -> None:
        await self.guard.async_set_target(value)
