"""docs/dashboard.yaml must only reference entities a fresh install creates."""

from __future__ import annotations

from pathlib import Path

import yaml
from homeassistant.core import HomeAssistant
from test_integration import _setup

DASHBOARD = Path(__file__).resolve().parents[1] / "docs" / "dashboard.yaml"


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


def test_dashboard_is_a_sections_view_with_no_personal_entities() -> None:
    view = yaml.safe_load(DASHBOARD.read_text())
    assert view["type"] == "sections" and view["sections"]
    assert all(e.startswith(("sensor.icomm_water_heater_", "water_heater.icomm_water_heater"))
               for e in _entities(view))
