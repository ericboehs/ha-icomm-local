"""The integration ships its Lovelace cards and makes every dashboard load them."""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_integration import URL, _setup

from custom_components.icomm_local import frontend

CARD_URL = f"/icomm_local/{frontend.CARD_FILE}"
SOURCE = (frontend.CARD_DIR / frontend.CARD_FILE).read_bytes()

# Home Assistant's own http component warns about aiohttp app and request keys
# (its code, not ours); CI turns warnings into errors.
pytestmark = pytest.mark.filterwarnings(r"ignore:It is recommended to use web\.(App|Request)Key")


@pytest.fixture
def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Keep the www/ copy out of the shared test config folder."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path


def _fake_frontend(hass: HomeAssistant) -> set[str]:
    """The test harness has no hass_frontend package, so the frontend component
    can't load; add_extra_js_url only needs its URL store, which has .add()."""
    urls: set[str] = set()
    hass.config.components.add("frontend")
    hass.data[DATA_EXTRA_MODULE_URL] = urls
    return urls


async def _http_and_frontend(hass: HomeAssistant, lovelace: bool = True) -> set[str]:
    assert await async_setup_component(hass, "http", {})
    if lovelace:
        assert await async_setup_component(hass, "lovelace", {})
    return _fake_frontend(hass)


async def _resources(hass: HomeAssistant):
    return await frontend._storage_resources(hass)


async def _version(hass: HomeAssistant) -> str:
    return (await async_get_integration(hass, "icomm_local")).version


async def test_resource_and_www_copy(hass: HomeAssistant, aioclient_mock, config_dir) -> None:
    """The normal case: a /local/ copy and a dashboard resource, which Home
    Assistant serves from the start, so pages loaded mid-startup work."""
    (config_dir / "www").mkdir()
    urls = await _http_and_frontend(hass)
    await _setup(hass, aioclient_mock)

    assert (config_dir / "www/icomm_local" / frontend.CARD_FILE).read_bytes() == SOURCE
    items = (await _resources(hass)).async_items()
    assert [(i["type"], i["url"]) for i in items] == [
        ("module", f"{frontend.LOCAL_URL}?v={await _version(hass)}")]
    assert urls == set()  # no page-level module needed


async def test_an_old_resource_is_updated_and_duplicates_go(hass: HomeAssistant, aioclient_mock,
                                                            config_dir) -> None:
    (config_dir / "www/icomm_local").mkdir(parents=True)
    (config_dir / "www/icomm_local" / frontend.CARD_FILE).write_text("old card")
    await _http_and_frontend(hass)
    resources = await _resources(hass)
    old = f"{frontend.LOCAL_URL}?v=0.0.1"
    await resources.async_create_item({"res_type": "module", "url": old})
    await resources.async_create_item({"res_type": "js", "url": frontend.LOCAL_URL})
    other = await resources.async_create_item({"res_type": "module", "url": "/local/other.js"})
    await _setup(hass, aioclient_mock)

    urls = sorted(i["url"] for i in resources.async_items())
    assert urls == sorted([f"{frontend.LOCAL_URL}?v={await _version(hass)}", other["url"]])
    assert (config_dir / "www/icomm_local" / frontend.CARD_FILE).read_bytes() == SOURCE


async def test_an_up_to_date_resource_is_left_alone(hass: HomeAssistant, aioclient_mock,
                                                    config_dir, monkeypatch) -> None:
    (config_dir / "www").mkdir()
    await _http_and_frontend(hass)
    resources = await _resources(hass)
    url = f"{frontend.LOCAL_URL}?v={await _version(hass)}"
    await resources.async_create_item({"res_type": "module", "url": url})
    calls = []
    monkeypatch.setattr(
        "homeassistant.components.lovelace.resources.ResourceStorageCollection.async_update_item",
        lambda *args: calls.append(args))
    await _setup(hass, aioclient_mock)
    assert calls == []
    assert [i["url"] for i in resources.async_items()] == [url]


async def test_resources_kept_in_a_dict(hass: HomeAssistant, aioclient_mock, config_dir) -> None:
    """Before 2025.2 hass.data["lovelace"] was a dict."""
    (config_dir / "www").mkdir()
    await _http_and_frontend(hass)
    real = await _resources(hass)
    hass.data["lovelace"] = {"resources": real}
    await _setup(hass, aioclient_mock)
    assert len(frontend._ours(real)) == 1


async def test_without_www_it_falls_back_to_the_page_module(hass: HomeAssistant, aioclient_mock,
                                                            config_dir, hass_client) -> None:
    """/local/ is registered only if www/ existed when the frontend started,
    so on this run the card comes from the integration's own path."""
    urls = await _http_and_frontend(hass)
    await _setup(hass, aioclient_mock)

    assert (config_dir / "www/icomm_local" / frontend.CARD_FILE).is_file()  # for the next start
    assert urls == {f"{CARD_URL}?v={await _version(hass)}"}
    assert (await _resources(hass)).async_items() == []

    client = await hass_client()
    resp = await client.get(CARD_URL)
    assert resp.status == 200
    body = await resp.text()
    assert 'customElements.define("icomm-water-heater-card"' in body
    assert 'customElements.define("icomm-water-heater-tile"' in body


async def test_without_lovelace_it_falls_back(hass: HomeAssistant, aioclient_mock,
                                              config_dir) -> None:
    (config_dir / "www").mkdir()
    urls = await _http_and_frontend(hass, lovelace=False)
    await _setup(hass, aioclient_mock)
    assert len(urls) == 1


async def test_yaml_resources_are_not_touched(hass: HomeAssistant, aioclient_mock,
                                              config_dir) -> None:
    (config_dir / "www").mkdir()
    urls = await _http_and_frontend(hass, lovelace=False)
    hass.config.components.add("lovelace")
    hass.data["lovelace"] = {"resources": object()}  # e.g. ResourceYAMLCollection
    await _setup(hass, aioclient_mock)
    assert len(urls) == 1


async def test_a_failed_copy_falls_back(hass: HomeAssistant, aioclient_mock, config_dir,
                                        monkeypatch) -> None:
    def fail(_www):
        raise PermissionError
    monkeypatch.setattr("custom_components.icomm_local.frontend._copy_card", fail)
    (config_dir / "www").mkdir()
    urls = await _http_and_frontend(hass)
    await _setup(hass, aioclient_mock)
    assert len(urls) == 1
    assert (await _resources(hass)).async_items() == []


async def test_registered_once_per_run(hass: HomeAssistant, aioclient_mock, config_dir) -> None:
    urls = await _http_and_frontend(hass, lovelace=False)
    await _setup(hass, aioclient_mock)
    # A second entry (another relay) or a reload must not register the path
    # again: aiohttp refuses a duplicate route.
    await frontend.async_register_cards(hass)
    assert len(urls) == 1


async def test_skipped_without_http_or_frontend(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    assert not hass.data.get(frontend._REGISTERED)


async def test_older_home_assistant_static_path(hass: HomeAssistant, aioclient_mock,
                                                config_dir, monkeypatch) -> None:
    """Before 2024.7 there is no StaticPathConfig, only register_static_path."""
    urls = await _http_and_frontend(hass, lovelace=False)
    monkeypatch.delattr("homeassistant.components.http.StaticPathConfig", raising=False)
    calls = []
    monkeypatch.setattr(hass.http, "register_static_path",
                        lambda *args: calls.append(args), raising=False)
    await _setup(hass, aioclient_mock)
    assert calls == [(frontend.URL_BASE, str(frontend.CARD_DIR), True)]
    assert len(urls) == 1


async def test_removing_the_last_relay_cleans_up(hass: HomeAssistant, aioclient_mock,
                                                 config_dir) -> None:
    (config_dir / "www").mkdir()
    await _http_and_frontend(hass)
    entry = await _setup(hass, aioclient_mock)
    second = MockConfigEntry(domain="icomm_local", data={"url": URL + "/2"}, title="second")
    second.add_to_hass(hass)
    copy = config_dir / "www/icomm_local" / frontend.CARD_FILE
    resources = await _resources(hass)

    await hass.config_entries.async_remove(second.entry_id)  # another relay remains
    assert copy.is_file() and len(frontend._ours(resources)) == 1

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not copy.exists() and not copy.parent.exists()
    assert frontend._ours(resources) == []


async def test_cleanup_keeps_other_files(hass: HomeAssistant, config_dir) -> None:
    folder = config_dir / "www/icomm_local"
    folder.mkdir(parents=True)
    (folder / frontend.CARD_FILE).write_bytes(SOURCE)
    (folder / "mine.txt").write_text("user file")
    await frontend.async_unregister_cards(hass)  # no lovelace: only the file goes
    assert not (folder / frontend.CARD_FILE).exists()
    assert (folder / "mine.txt").exists()


def test_an_unchanged_copy_is_not_rewritten(tmp_path: Path) -> None:
    target = tmp_path / "www/icomm_local" / frontend.CARD_FILE
    target.parent.mkdir(parents=True)
    target.write_bytes(SOURCE)
    before = target.stat().st_mtime_ns
    assert frontend._copy_card(tmp_path / "www") is True
    assert target.stat().st_mtime_ns == before
