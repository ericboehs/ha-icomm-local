"""docs/dashboard.yaml must only reference entities a fresh install creates."""

from __future__ import annotations

from pathlib import Path

import yaml
from homeassistant.core import HomeAssistant
from test_integration import _setup

DASHBOARD = Path(__file__).resolve().parents[1] / "docs" / "dashboard.yaml"
MULTI = DASHBOARD.with_name("dashboard-multi.yaml")


def _entities(node) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        if isinstance(node.get("entity"), str):
            found.add(node["entity"])
        for value in node.values():
            found |= _entities(value)
    elif isinstance(node, list):
        for item in node:
            found |= _entities(item)
    return found


async def test_every_dashboard_entity_exists_on_a_fresh_install(
        hass: HomeAssistant, aioclient_mock) -> None:
    view = yaml.safe_load(DASHBOARD.read_text())
    wanted = _entities(view)
    assert len(wanted) >= 10                       # the parser found the cards
    await _setup(hass, aioclient_mock)
    missing = sorted(e for e in wanted if hass.states.get(e) is None)
    assert missing == [], f"docs/dashboard.yaml references entities that do not exist: {missing}"


def _attribute_rows(node) -> list[tuple[str, str]]:
    found = []
    if isinstance(node, dict):
        if node.get("type") == "attribute":
            found.append((node["entity"], node["attribute"]))
        for value in node.values():
            found += _attribute_rows(value)
    elif isinstance(node, list):
        for item in node:
            found += _attribute_rows(item)
    return found


async def test_every_attribute_row_names_a_real_attribute(
        hass: HomeAssistant, aioclient_mock) -> None:
    """An attribute row for an attribute the entity doesn't have shows blank."""
    from test_firmware_info import DEVICE, NAMEPLATE
    from test_integration import STATE
    rows = _attribute_rows(yaml.safe_load(DASHBOARD.read_text()))
    assert len(rows) >= 8
    # A relay that knows the heater (0.5.7+): firmware, model and serial.
    await _setup(hass, aioclient_mock, state={**STATE, "device": {**DEVICE, **NAMEPLATE}})
    wrong = [(e, a) for e, a in rows if a not in hass.states.get(e).attributes]
    assert wrong == [], f"docs/dashboard.yaml shows attributes that don't exist: {wrong}"


def test_dashboard_is_a_sections_view_with_no_personal_entities() -> None:
    view = yaml.safe_load(DASHBOARD.read_text())
    assert view["type"] == "sections" and view["sections"]
    assert all(e.startswith(("sensor.icomm_water_heater_", "water_heater.icomm_water_heater"))
               for e in _entities(view))


async def test_every_multi_heater_dashboard_entity_exists_with_two_heaters(
        hass: HomeAssistant, aioclient_mock) -> None:
    from test_multi_heater import _setup as _setup_two
    view = yaml.safe_load(MULTI.read_text())
    wanted = _entities(view)
    assert {e for e in wanted if "_heater_2" in e} and len(wanted) >= 20
    await _setup_two(hass, aioclient_mock)
    missing = sorted(e for e in wanted if hass.states.get(e) is None)
    assert missing == [], f"docs/dashboard-multi.yaml references missing entities: {missing}"


def test_multi_heater_dashboard_columns_match() -> None:
    """Both heater columns show the same cards, so a copied third one will too."""
    view = yaml.safe_load(MULTI.read_text())
    one, two = (_entities(section) for section in view["sections"][:2])
    assert {e.replace("_heater_2", "_heater") for e in two} == one
