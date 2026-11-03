"""Serve the Lovelace cards that ship with the integration, and load them.

The cards live in frontend/ beside this file, so HACS (which installs
custom_components/icomm_local only) brings them along. They are served from
/icomm_local/ and added to every frontend page with add_extra_js_url, so
nobody has to add a dashboard resource by hand. The integration's version is
appended as ?v= so a new release is fetched instead of a cached copy.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_FILE = "icomm-water-heater-card.js"
URL_BASE = f"/{DOMAIN}"
CARD_DIR = Path(__file__).parent / "frontend"
_REGISTERED = f"{DOMAIN}_frontend_registered"


async def async_register_cards(hass: HomeAssistant) -> None:
    """Once per Home Assistant run: serve frontend/ and load the card module.

    Both need components a real install always has (http, frontend: both are
    after_dependencies); without them, as in a bare test instance, this does
    nothing rather than failing the entry.
    """
    if hass.data.get(_REGISTERED):
        return
    if getattr(hass, "http", None) is None or "frontend" not in hass.config.components:
        _LOGGER.debug("http or frontend not loaded; the cards are not registered")
        return
    hass.data[_REGISTERED] = True
    version = (await async_get_integration(hass, DOMAIN)).version
    try:
        from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415
    except ImportError:  # Home Assistant before 2024.7
        hass.http.register_static_path(URL_BASE, str(CARD_DIR), True)
    else:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(URL_BASE, str(CARD_DIR), True)])
    from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415

    add_extra_js_url(hass, f"{URL_BASE}/{CARD_FILE}?v={version}")
