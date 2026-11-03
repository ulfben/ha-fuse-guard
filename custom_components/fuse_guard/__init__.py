"""Fuse Guard: keep every phase under the main fuse by throttling a load."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .guard import STORAGE_VERSION, FuseGuard

_LOGGER = logging.getLogger(__name__)

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
    """Serve the dashboard card and make every client load it, once per run.

    The card is added as a dashboard resource, like a HACS card. Clients fetch
    the resources whenever they load a dashboard, so the HA app picks it up too;
    a script added to the page HTML (add_extra_js_url) never reached the app.
    With YAML resources we can't add one, so the page script is the fallback.
    The content hash in the URL makes clients fetch a changed card.
    """
    if hass.data.get(_CARD_REGISTERED) or "frontend" not in hass.config.components:
        return
    hass.data[_CARD_REGISTERED] = True
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(CARD_FILE), cache_headers=False)]
    )
    digest = await hass.async_add_executor_job(_card_digest)
    url = f"{CARD_URL}?v={digest}"
    if not await _async_add_card_resource(hass, url):
        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, url)


def _card_digest() -> str:
    return hashlib.sha256(CARD_FILE.read_bytes()).hexdigest()[:10]


async def _async_add_card_resource(hass: HomeAssistant, url: str) -> bool:
    """Add or update our dashboard resource. False if resources are in YAML."""
    try:
        from homeassistant.components.lovelace.const import LOVELACE_DATA
    except ImportError:
        return False
    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None or lovelace.resource_mode != "storage":
        return False
    resources = lovelace.resources
    try:
        await resources.async_get_info()  # loads the stored items
        ours = [r for r in resources.async_items() if r["url"].split("?")[0] == CARD_URL]
        if not ours:
            await resources.async_create_item({"res_type": "module", "url": url})
            return True
        first, *extra = ours
        if first["url"] != url or first.get("type") != "module":
            await resources.async_update_item(first["id"], {"res_type": "module", "url": url})
        for item in extra:
            await resources.async_delete_item(item["id"])
    except Exception:  # noqa: BLE001 - never block setup over the card
        _LOGGER.warning("Could not add the Fuse Guard card as a dashboard resource", exc_info=True)
        return False
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: FuseGuardConfigEntry) -> None:
    """Apply changed options by reloading."""
    await hass.config_entries.async_reload(entry.entry_id)
