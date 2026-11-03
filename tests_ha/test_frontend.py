"""The integration serves its Lovelace cards and loads them on every page."""

from __future__ import annotations

import pytest
from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from test_integration import _setup

from custom_components.icomm_local import frontend

CARD_URL = f"/icomm_local/{frontend.CARD_FILE}"

# Home Assistant's own http component warns about aiohttp app and request keys
# (its code, not ours); CI turns warnings into errors.
pytestmark = pytest.mark.filterwarnings(r"ignore:It is recommended to use web\.(App|Request)Key")


def _fake_frontend(hass: HomeAssistant) -> set[str]:
    """The test harness has no hass_frontend package, so the frontend component
    can't load; add_extra_js_url only needs its URL store, which has .add()."""
    urls: set[str] = set()
    hass.config.components.add("frontend")
    hass.data[DATA_EXTRA_MODULE_URL] = urls
    return urls


async def test_the_card_is_served_and_loaded(hass: HomeAssistant, aioclient_mock,
                                             hass_client) -> None:
    assert await async_setup_component(hass, "http", {})
    urls = _fake_frontend(hass)
    await _setup(hass, aioclient_mock)

    version = frontend.CARD_DIR.parent.joinpath("manifest.json").read_text()
    loaded = [u for u in urls if u.startswith(f"{CARD_URL}?v=")]
    assert len(loaded) == 1
    assert f'"version": "{loaded[0].split("?v=")[1]}"' in version

    client = await hass_client()
    resp = await client.get(CARD_URL)
    assert resp.status == 200
    body = await resp.text()
    assert 'customElements.define("icomm-water-heater-card"' in body
    assert 'customElements.define("icomm-water-heater-tile"' in body


async def test_registered_once_per_run(hass: HomeAssistant, aioclient_mock) -> None:
    assert await async_setup_component(hass, "http", {})
    urls = _fake_frontend(hass)
    await _setup(hass, aioclient_mock)
    # A second entry (another relay) or a reload must not register the path
    # again: aiohttp refuses a duplicate route.
    await frontend.async_register_cards(hass)
    assert len(urls) == 1


async def test_skipped_without_http_or_frontend(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    assert not hass.data.get(frontend._REGISTERED)


async def test_older_home_assistant_static_path(hass: HomeAssistant, aioclient_mock,
                                                monkeypatch) -> None:
    """Before 2024.7 there is no StaticPathConfig, only register_static_path."""
    assert await async_setup_component(hass, "http", {})
    urls = _fake_frontend(hass)
    monkeypatch.delattr("homeassistant.components.http.StaticPathConfig", raising=False)
    calls = []
    monkeypatch.setattr(hass.http, "register_static_path",
                        lambda *args: calls.append(args), raising=False)
    await _setup(hass, aioclient_mock)
    assert calls == [(frontend.URL_BASE, str(frontend.CARD_DIR), True)]
    assert len(urls) == 1
