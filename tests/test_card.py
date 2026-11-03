"""The dashboard card is added as a Lovelace resource, so every client loads it."""

from __future__ import annotations

from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.fuse_guard import CARD_URL, _async_add_card_resource


async def _resources(hass: HomeAssistant):
    assert await async_setup_component(hass, "lovelace", {})
    resources = hass.data[LOVELACE_DATA].resources
    await resources.async_get_info()
    return resources


def _ours(resources) -> list[dict]:
    return [r for r in resources.async_items() if r["url"].startswith(CARD_URL)]


async def test_adds_the_card_once(hass: HomeAssistant) -> None:
    resources = await _resources(hass)

    assert await _async_add_card_resource(hass, f"{CARD_URL}?v=aaa")
    assert await _async_add_card_resource(hass, f"{CARD_URL}?v=aaa")

    ours = _ours(resources)
    assert len(ours) == 1
    assert ours[0]["url"] == f"{CARD_URL}?v=aaa"
    assert ours[0]["type"] == "module"


async def test_a_changed_card_updates_the_url_and_drops_duplicates(hass: HomeAssistant) -> None:
    resources = await _resources(hass)
    await resources.async_create_item({"res_type": "module", "url": f"{CARD_URL}?v=old"})
    await resources.async_create_item({"res_type": "js", "url": CARD_URL})
    await resources.async_create_item({"res_type": "module", "url": "/local/other-card.js"})

    assert await _async_add_card_resource(hass, f"{CARD_URL}?v=new")

    assert [r["url"] for r in _ours(resources)] == [f"{CARD_URL}?v=new"]
    assert any(r["url"] == "/local/other-card.js" for r in resources.async_items())


async def test_yaml_resources_fall_back_to_the_page_script(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "lovelace", {"lovelace": {"resource_mode": "yaml"}})

    assert not await _async_add_card_resource(hass, f"{CARD_URL}?v=aaa")


async def test_without_lovelace_it_falls_back(hass: HomeAssistant) -> None:
    assert not await _async_add_card_resource(hass, f"{CARD_URL}?v=aaa")
