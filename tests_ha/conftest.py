"""Run the integration inside a real Home Assistant (pytest-homeassistant-custom-component).

Kept out of tests/ so the relay suite stays dependency-light; this one needs HA.
"""

from pathlib import Path

import pytest

import custom_components

# The plugin ships its own `custom_components` package (testing_config), which
# shadows the repo's. Extend its search path rather than fight the import order.
_OURS = str(Path(__file__).resolve().parents[1] / "custom_components")
if _OURS not in custom_components.__path__:
    custom_components.__path__.append(_OURS)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
