"""Fuse Guard: keep every phase under the main fuse by throttling a load."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .guard import STORAGE_VERSION, FuseGuard

type FuseGuardConfigEntry = ConfigEntry[FuseGuard]

CARD_URL = f"/{DOMAIN}/fuse-guard-card.js"
CARD_FILE = Path(__file__).parent / "www" / "fuse-guard-card.js"
_CARD_REGISTERED = f"{DOMAIN}_card_registered"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: FuseGuardConfigEntry) -> bool:
    """Set up a guard from a config entry."""
    await _async_register_card(hass)
    guard = FuseGuard(hass, entry)
    entry.runtime_data = guard
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await guard.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FuseGuardConfigEntry) -> bool:
    """Unload a guard."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: FuseGuardConfigEntry) -> None:
    """Forget a removed guard's saved mode, target and ceiling."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the dashboard card and load it in every browser, once per run.

    The version in the URL makes browsers fetch the new card after an update.
    """
    if hass.data.get(_CARD_REGISTERED) or "frontend" not in hass.config.components:
        return
    from homeassistant.components.frontend import add_extra_js_url

    version = (await async_get_integration(hass, DOMAIN)).version
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(CARD_FILE), cache_headers=False)]
    )
    add_extra_js_url(hass, f"{CARD_URL}?v={version}")
    hass.data[_CARD_REGISTERED] = True


async def _async_reload_entry(hass: HomeAssistant, entry: FuseGuardConfigEntry) -> None:
    """Apply changed options by reloading."""
    await hass.config_entries.async_reload(entry.entry_id)
