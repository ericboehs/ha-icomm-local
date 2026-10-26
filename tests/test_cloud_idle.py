"""Health: the heater's cloud side stopped while Local Connect carries on.

Seen live (2026-10-25): after the relay was unreachable for a host reboot and
an add-on restart, the module sent changing telemetry over LAN for 80+ minutes
and not one packet to Ayla -- the router saw no SYN to :443 -- until a power
cycle. HA was fine; the A.O. Smith app was not. "unknown: too few dials" hid
it. libada drops the cloud as a destination after a failed connect and, with
its UDP notification channel up, doesn't poll.
"""

from __future__ import annotations

import pytest
from icomm_relay import CLOUD_IDLE_AFTER, Relay
from test_degraded import POLL, TELEMETRY, _dial


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch):
    import icomm_relay
    state = {"now": 1_000_000.0}
    monkeypatch.setattr(icomm_relay.time, "time", lambda: state["now"])
    return state


@pytest.fixture
def relay(_clock) -> Relay:
    relay = Relay()
    relay.started = _clock["now"]
    relay.lan.enabled = relay.lan.registered = True
    return relay


def _lan_reports(relay, clock, blocks: list[list[bytes]], every: float = 300) -> None:
    """The relay's LAN refresh every 5 min, answered over LAN only."""
    for report in blocks:
        relay._ingest_blocks(report, lan=True)
        clock["now"] += every


def test_changing_lan_reports_and_a_silent_cloud(relay, _clock, blocks_heat_pump,
                                                 blocks_electric):
    reports = [blocks_heat_pump, blocks_electric] * 4                  # 8 × 5 min = 40 min
    _lan_reports(relay, _clock, reports[:5])
    assert relay.health()["state"] != "degraded"                       # 25 min: not yet
    _lan_reports(relay, _clock, reports[5:])
    health = relay.health()
    assert health["state"] == "degraded" and health["code"] == "cloud_idle"
    assert "cloud side idle" in health["reasons"][0]
    assert "breaker" in health["remediation"] and "Home Assistant is unaffected" in (
        health["remediation"])
    assert health["evidence"]["cloud_quiet_s"] >= CLOUD_IDLE_AFTER
    assert health["evidence"]["lan_changes"] >= 2


def test_a_cloud_request_clears_it(relay, _clock, blocks_heat_pump, blocks_electric):
    _lan_reports(relay, _clock, [blocks_heat_pump, blocks_electric] * 4)
    assert relay.health().get("code") == "cloud_idle"
    for _ in range(3):                                                 # the power cycle
        _dial(relay, _clock, [TELEMETRY, POLL], gap=60)
    relay._ingest_blocks(blocks_heat_pump)                             # via the cloud
    assert relay.health()["state"] == "healthy"


def test_frozen_contents_are_not_evidence(relay, _clock, blocks_heat_pump):
    """Unchanged reports (a satisfied tank, or lapsed live reporting) say
    nothing about the cloud side: a healthy module wouldn't post them either."""
    _lan_reports(relay, _clock, [blocks_heat_pump] * 10)
    assert relay.health().get("code") != "cloud_idle"


def test_not_without_a_lan_session_or_once_lan_goes_quiet(relay, _clock, blocks_heat_pump,
                                                          blocks_electric):
    _lan_reports(relay, _clock, [blocks_heat_pump, blocks_electric] * 4)
    relay.lan.registered = False
    assert relay.health().get("code") != "cloud_idle"
    relay.lan.registered = True
    _clock["now"] += 900                                               # LAN quiet too
    assert relay.health().get("code") != "cloud_idle"


def test_cloud_reports_dont_count_as_lan(relay, _clock, blocks_heat_pump, blocks_electric):
    for report in [blocks_heat_pump, blocks_electric] * 4:
        relay._ingest_blocks(report)                                   # lan=False
        _clock["now"] += 300
    assert relay.health().get("code") != "cloud_idle"


def test_quiet_counts_from_the_last_dial(relay, _clock, blocks_heat_pump, blocks_electric):
    _clock["now"] += 3600
    for _ in range(3):
        _dial(relay, _clock, [TELEMETRY, POLL], gap=60)                # healthy earlier
    _lan_reports(relay, _clock, [blocks_heat_pump, blocks_electric] * 3)   # 30 min since
    assert relay.health().get("code") == "cloud_idle"
