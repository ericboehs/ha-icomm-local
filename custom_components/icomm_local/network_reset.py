"""Bring the heater's cloud side back by cutting its network access briefly.

After a long relay outage (a Home Assistant host reboot) the heater's Wi-Fi
module stops connecting to Ayla: it still looks up its cloud name, but opens
no connection, for hours, until its Wi-Fi drops or it's power-cycled. The relay
spots that state with its cloud-echo probe (health code `cloud_down`).

Measured (research phase 53): turning the heater's network access off for 90
seconds and on again (a UniFi client block, then unblock) restarts its cloud
client like a power cycle: it rejoins, connects at once, fetches its config and
re-registers Local Connect. A quick Wi-Fi kick (7 s) doesn't: the module never
notices it was gone.

So, with a switch configured (the UniFi integration's block-client switch, or
anything else that cuts the heater's network access while off), this turns it
off for NETWORK_OFF_SECONDS and on again, at most once per
NETWORK_RESET_COOLDOWN. The "off" is written to storage first, so a Home
Assistant restart mid-reset turns the switch back on at the next start.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    NETWORK_OFF_SECONDS,
    NETWORK_ON_RETRIES,
    NETWORK_ON_RETRY_SECONDS,
    NETWORK_RESET_COOLDOWN,
    as_dict,
    as_number,
)

_LOGGER = logging.getLogger(__name__)


def _now() -> float:
    return dt_util.utcnow().timestamp()


class NetworkReset:
    """One heater's network switch, used only when its cloud side is down."""

    def __init__(self, hass: HomeAssistant, entry_id: str, switch: str) -> None:
        self.hass = hass
        self.switch = switch
        self.store: Store = Store(hass, 1, f"{DOMAIN}.network_reset.{entry_id}")
        self.last = 0.0                       # when the last reset started
        self.off = False                      # the switch is off because of us
        self.resets = 0
        self._cancel = None
        self._task = None

    async def async_load(self) -> None:
        data = as_dict(await self.store.async_load())
        self.last = as_number(data.get("last")) or 0.0
        self.resets = int(as_number(data.get("resets")) or 0)
        if data.get("off"):
            # Home Assistant stopped during a reset: never leave the heater offline.
            _LOGGER.warning("%s was left off by an interrupted network reset; turning it on",
                            self.switch)
            self.off = True
            await self._turn_on()

    @callback
    def check(self, health: dict) -> None:
        """From the coordinator's listener: reset when the relay says cloud_down."""
        busy = self._task is not None and not self._task.done()
        if (health.get("code") != "cloud_down" or self.off or busy
                or _now() - self.last < NETWORK_RESET_COOLDOWN):
            return
        self._task = self.hass.async_create_task(self._reset())

    async def _reset(self) -> None:
        # (HA may run this eagerly, before check() has stored the task: so the
        # task's done() is the guard, not clearing it here.)
        self.last, self.off = _now(), True
        self.resets += 1
        await self._save()
        _LOGGER.warning("the water heater isn't reaching Ayla: turning %s off for %d s, "
                        "then on, so it reconnects", self.switch, NETWORK_OFF_SECONDS)
        try:
            await self.hass.services.async_call(
                "switch", "turn_off", {"entity_id": self.switch}, blocking=True)
        except HomeAssistantError as err:
            _LOGGER.error("couldn't turn %s off: %s", self.switch, err)
            self.off = False
            await self._save()
            return
        self._cancel = async_call_later(self.hass, NETWORK_OFF_SECONDS, self._back_on)

    async def _back_on(self, _now_dt=None, attempt: int = 1) -> None:
        self._cancel = None
        if not await self._turn_on() and attempt < NETWORK_ON_RETRIES:
            async def _retry(_dt) -> None:
                await self._back_on(attempt=attempt + 1)
            self._cancel = async_call_later(self.hass, NETWORK_ON_RETRY_SECONDS, _retry)

    async def _turn_on(self) -> bool:
        try:
            await self.hass.services.async_call(
                "switch", "turn_on", {"entity_id": self.switch}, blocking=True)
        except HomeAssistantError as err:
            _LOGGER.error("couldn't turn %s back on: %s; the water heater has no network "
                          "until it is", self.switch, err)
            return False
        self.off = False
        await self._save()
        return True

    async def _save(self) -> None:
        await self.store.async_save({"last": self.last, "off": self.off,
                                     "resets": self.resets})

    async def async_unload(self) -> None:
        """Unloading mid-reset: turn the switch back on now."""
        if self._cancel is not None:
            self._cancel()
            self._cancel = None
        if self.off:
            await self._turn_on()

    def status(self) -> dict:
        return {"switch": self.switch, "resets": self.resets,
                "last": dt_util.utc_from_timestamp(self.last).isoformat() if self.last else None,
                "in_progress": self.off}
