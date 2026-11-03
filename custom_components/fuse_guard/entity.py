"""Base entity for Fuse Guard."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .guard import FuseGuard


class FuseGuardEntity(Entity):
    """Updates whenever the guard evaluates."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, guard: FuseGuard, key: str) -> None:
        self.guard = guard
        self._attr_translation_key = key
        self._attr_unique_id = f"{guard.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, guard.entry.entry_id)},
            name=guard.entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.guard.async_add_listener(self.async_write_ha_state))
