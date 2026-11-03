"""Serve the Lovelace cards that ship with the integration, and load them.

The cards live in frontend/ beside this file, so HACS (which installs
custom_components/icomm_local only) brings them along. How a dashboard gets
them, most reliable first:

1. **A copy under www/ plus a dashboard resource.** Home Assistant serves
   /local/ (config/www) and the resource list from the moment the frontend
   starts, so a page loaded while Home Assistant is still starting gets the
   card too. Each start copies the card to www/icomm_local/ when it changed
   and points the resource at /local/icomm_local/...?v=<version>, the same
   way HACS installs cards. Storage-mode dashboards only; a YAML resource
   list belongs to the user.
2. **add_extra_js_url** (/icomm_local/...?v=<version>). Pages list extra
   modules when they load and never pick up later ones, so a page loaded
   before this integration set up shows "Configuration error" until it is
   refreshed. Used only when (1) can't work this run: YAML resources, no
   lovelace, or a www folder created just now (/local/ is registered only if
   www existed when the frontend started).

The card checks whether it is already defined, so loading both is harmless.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_FILE = "icomm-water-heater-card.js"
URL_BASE = f"/{DOMAIN}"
CARD_DIR = Path(__file__).parent / "frontend"
LOCAL_URL = f"/local/{DOMAIN}/{CARD_FILE}"
_REGISTERED = f"{DOMAIN}_frontend_registered"


def _copy_card(www: Path) -> bool:
    """Copy the card into www/icomm_local/ if it differs.

    Returns False when www/ didn't exist before, so /local/ isn't served this
    run (the frontend registers it only for a folder that exists at startup).
    """
    existed = www.is_dir()
    target = www / DOMAIN / CARD_FILE
    source = (CARD_DIR / CARD_FILE).read_bytes()
    if not target.is_file() or target.read_bytes() != source:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source)
    return existed


def _remove_card(www: Path) -> None:
    target = www / DOMAIN / CARD_FILE
    target.unlink(missing_ok=True)
    # Not empty (something else lives there) or already gone: leave it.
    with contextlib.suppress(OSError):
        target.parent.rmdir()


async def _storage_resources(hass: HomeAssistant):
    """The dashboard resource collection if it's UI-managed, else None."""
    if "lovelace" not in hass.config.components:
        return None
    from homeassistant.components.lovelace.resources import (  # noqa: PLC0415
        ResourceStorageCollection,
    )

    data = hass.data.get("lovelace")
    resources = getattr(data, "resources", None)
    if resources is None and isinstance(data, dict):  # before 2025.2
        resources = data.get("resources")
    if not isinstance(resources, ResourceStorageCollection):
        return None
    await resources.async_get_info()  # loads it from storage if needed
    return resources


def _ours(resources) -> list[dict]:
    return [item for item in resources.async_items()
            if str(item.get("url", "")).split("?")[0] == LOCAL_URL]


async def _ensure_resource(hass: HomeAssistant, url: str) -> bool:
    """Point exactly one dashboard resource at url. False if not possible."""
    resources = await _storage_resources(hass)
    if resources is None:
        return False
    items = _ours(resources)
    if not items:
        await resources.async_create_item({"res_type": "module", "url": url})
        return True
    first, *extra = items
    if first.get("url") != url or first.get("type") != "module":
        await resources.async_update_item(first["id"], {"res_type": "module", "url": url})
    for item in extra:
        await resources.async_delete_item(item["id"])
    return True


async def async_register_cards(hass: HomeAssistant) -> None:
    """Once per Home Assistant run: make the cards load on every dashboard.

    Needs http and frontend (after_dependencies, always present on a real
    install); without them, as in a bare test instance, this does nothing
    rather than failing the entry.
    """
    if hass.data.get(_REGISTERED):
        return
    if getattr(hass, "http", None) is None or "frontend" not in hass.config.components:
        _LOGGER.debug("http or frontend not loaded; the cards are not registered")
        return
    hass.data[_REGISTERED] = True
    version = (await async_get_integration(hass, DOMAIN)).version

    try:
        served = await hass.async_add_executor_job(_copy_card, Path(hass.config.path("www")))
    except OSError as err:
        _LOGGER.warning("Couldn't copy the card into www/: %s", type(err).__name__)
        served = False
    resource = served and await _ensure_resource(hass, f"{LOCAL_URL}?v={version}")
    if resource:
        return

    try:
        from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415
    except ImportError:  # Home Assistant before 2024.7
        hass.http.register_static_path(URL_BASE, str(CARD_DIR), True)
    else:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(URL_BASE, str(CARD_DIR), True)])
    from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415

    add_extra_js_url(hass, f"{URL_BASE}/{CARD_FILE}?v={version}")


async def async_unregister_cards(hass: HomeAssistant) -> None:
    """The last relay was removed: drop the dashboard resource and the copy."""
    resources = await _storage_resources(hass)
    if resources is not None:
        for item in _ours(resources):
            await resources.async_delete_item(item["id"])
    await hass.async_add_executor_job(_remove_card, Path(hass.config.path("www")))
