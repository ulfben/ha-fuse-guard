"""Describe Fuse Guard writes in the logbook."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_ACTION, MODE_SHADOW


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, Any]]], None],
) -> None:
    @callback
    def describe(event: Event) -> dict[str, Any]:
        data = event.data
        prefix = "would set" if data.get("mode") == MODE_SHADOW else "set"
        return {
            LOGBOOK_ENTRY_NAME: data.get("name", "Fuse Guard"),
            LOGBOOK_ENTRY_MESSAGE: (
                f"{prefix} {data.get('setpoint_entity')} to {int(data.get('write_w', 0))} W: "
                f"{data.get('message')}"
            ),
        }

    async_describe_event(DOMAIN, EVENT_ACTION, describe)
