"""Shared fixtures: real telemetry payloads, captured from the appliance.

The JSON files in `fixtures/` are unmodified `<value>` blobs from actual
`superBlockDyn1` posts, with device ids and headers stripped. Using real payloads
rather than hand-built ones is deliberate: the register atlas is the part most
likely to be got wrong, and a synthetic fixture would only encode the same
misunderstanding as the decoder.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "relay"))
FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> list[bytes]:
    raw = json.loads((FIXTURES / name).read_text())["blocks"]
    return [bytes.fromhex(block) for block in raw]


@pytest.fixture
def blocks_electric() -> list[bytes]:
    """Element energised: 4500 W, mode 100/1."""
    return load("telemetry_electric.json")


@pytest.fixture
def blocks_heat_pump() -> list[bytes]:
    """Compressor running (356 W), mode 0/5, cold evaporator air."""
    return load("telemetry_heat_pump.json")


@pytest.fixture
def blocks_heat_pump_cooldown() -> list[bytes]:
    """Heat-pump mode immediately after the element stopped: air still warm."""
    return load("telemetry_heat_pump_cooldown.json")


@pytest.fixture
def blocks_vacation() -> list[bytes]:
    """Vacation, 2 days, register 2/2: active target 50 F, setpoint kept at 150."""
    return load("telemetry_vacation.json")


@pytest.fixture
def blocks_hybrid() -> list[bytes]:
    """Hybrid mode, idle: 0 W, mode 0/4."""
    return load("telemetry_hybrid.json")


@pytest.fixture
def openssl_available() -> None:
    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
