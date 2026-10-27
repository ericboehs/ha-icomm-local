"""Shared fixtures: real telemetry payloads, captured from the appliance.

The JSON files in `fixtures/` are unmodified `<value>` blobs from actual
`superBlockDyn1` posts, with device ids and headers stripped. Using real payloads
rather than hand-built ones is deliberate: the register atlas is the part most
likely to be got wrong, and a synthetic fixture would only encode the same
misunderstanding as the decoder.
"""

from __future__ import annotations

import json
import os
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


# Every environment variable the relay reads. Cleared before each test so a
# developer's shell (or a test that leaked) can't change what a test sees.
RELAY_ENV = (
    "CERT_DIR", "CLOUD_IDLE_AFTER", "CONTENTS_STALE_AFTER", "CONTENTS_STALE_HEATING_AFTER",
    "DATA_DIR", "DEBUG_REQUESTS", "DEVICE_DSN", "DNS_ANSWER", "DNS_PORT", "DNS_REWRITE",
    "DNS_SERVER", "DNS_UPSTREAM", "DSN_LAN_REWRITE", "FIRMWARE_UPDATES", "FREEZE_RECONNECT",
    "LAN_MODE", "LAN_PORT", "LAN_REFRESH_INTERVAL", "LAN_STALL_AFTER", "LANIP_FILE",
    "LANIP_KEY", "LANIP_KEY_ID", "LANIP_PUSH", "LISTEN_PORT", "MAX_HEATERS", "MODULE_IP",
    "OFFLINE_FALLBACK", "QUEUE_FILE", "QUEUE_MAX_AGE_S", "RELAY_CERT", "RELAY_DHPARAMS",
    "RELAY_IP", "RELAY_KEY", "REPORTING_MASK_RENEW", "REPORTING_MASK_RENEW_AFTER",
    "STATE_HOST", "STATE_PORT", "TRUSTED_PEERS", "UPSTREAM",
)


@pytest.fixture(autouse=True)
def _clean_relay_env(monkeypatch) -> None:
    for key in RELAY_ENV:
        monkeypatch.delenv(key, raising=False)


def require_openssl() -> None:
    """Skip the TLS tests without openssl on a dev machine; in CI (CI set, as
    on GitHub Actions) a missing openssl is a broken runner, so fail."""
    if shutil.which("openssl") is None:
        if os.environ.get("CI"):
            pytest.fail("openssl not installed, and CI is set: the TLS tests must run")
        pytest.skip("openssl not installed")


@pytest.fixture
def openssl_available() -> None:
    require_openssl()
