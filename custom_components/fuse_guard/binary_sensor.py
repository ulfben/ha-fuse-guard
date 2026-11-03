"""Binary sensor: on while the guard holds the load below its target."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FuseGuardConfigEntry
from .entity import FuseGuardEntity
from .guard import FuseGuard


async def async_setup_entry(
    hass: HomeAssistant, entry: FuseGuardConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([LimitingSensor(entry.runtime_data)])


class LimitingSensor(FuseGuardEntity, BinarySensorEntity):
    def __init__(self, guard: FuseGuard) -> None:
        super().__init__(guard, "limiting")

    @property
    def is_on(self) -> bool:
        return self.guard.limiting
