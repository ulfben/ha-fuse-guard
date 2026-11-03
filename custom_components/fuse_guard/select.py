"""Mode select: off, shadow (decide but never write) or active."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FuseGuardConfigEntry
from .const import MODE_ACTIVE, MODE_OFF, MODE_SHADOW
from .entity import FuseGuardEntity
from .guard import FuseGuard


async def async_setup_entry(
    hass: HomeAssistant, entry: FuseGuardConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([ModeSelect(entry.runtime_data)])


class ModeSelect(FuseGuardEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = [MODE_OFF, MODE_SHADOW, MODE_ACTIVE]

    def __init__(self, guard: FuseGuard) -> None:
        super().__init__(guard, "mode")

    @property
    def current_option(self) -> str:
        return self.guard.mode

    async def async_select_option(self, option: str) -> None:
        await self.guard.async_set_mode(option)
