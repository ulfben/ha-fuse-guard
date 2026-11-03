"""Diagnostics download: config, current phase sources and recent decisions."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import FuseGuardConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: FuseGuardConfigEntry
) -> dict[str, Any]:
    return {"options": dict(entry.options), **entry.runtime_data.diagnostics()}
