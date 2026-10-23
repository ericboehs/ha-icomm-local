"""The degraded-state detector: the signature of a module that stopped listening.

The healthy and degraded shapes here are transcribed from the research repo's
conversation captures: healthy dials carry telemetry plus a poll and keep coming
back; degraded dials carry exactly one telemetry POST on a ~4 minute cadence and
never poll.
"""

from __future__ import annotations

import pytest
from icomm_relay import Relay, decode

TELEMETRY = "/devices/1/properties/superBlockDyn1/datapoints.xml"
POLL = "/devices/1/commands.xml?signal=-50"
REGTOKEN = "/dsns/AC000W019949045/regtoken.xml"


@pytest.fixture
def relay(blocks_heat_pump: list[bytes]) -> Relay:
    relay = Relay()
    relay.state = decode(blocks_heat_pump)
    relay.last_telemetry = 1_000_000.0
    return relay


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch):
    """A movable clock, so dial spacing is exact."""
    import icomm_relay
    state = {"now": 1_000_000.0}
    monkeypatch.setattr(icomm_relay.time, "time", lambda: state["now"])
    return state


def _dial(relay: Relay, clock, paths: list[str], gap: float) -> None:
    """One dial: `paths` requested in quick succession, then silence for `gap`."""
    for path in paths:
        relay._note_request(path)
    clock["now"] += gap


def test_unknown_before_enough_dials(relay: Relay, _clock) -> None:
    relay.last_telemetry = _clock["now"]
    assert relay.health()["state"] == "unknown"


def test_never_heard_from_is_degraded_after_a_while(relay: Relay, _clock) -> None:
    """A young relay that has heard nothing is the same silence, not "unknown".

    Restarting the relay drops the module's connection, and measured recovery after
    that is 20+ minutes, so this state has to be named rather than hidden.
    """
    relay.started = _clock["now"]
    relay.last_telemetry = 0.0
    assert relay.health()["state"] == "unknown"          # too early to say
    _clock["now"] += 901
    health = relay.health()
    assert health["state"] == "degraded"
    assert any("since the relay started" in reason for reason in health["reasons"])


def test_wedged_module_is_degraded_not_unknown(relay: Relay, _clock) -> None:
    """The bug this test exists for: a totally silent module reported "unknown".

    Observed on hardware -- the module answered ARP and ICMP from the gateway and
    sent nothing at all, not even ops keepalives. With no dials to study, the
    too-few-dials shortcut swallowed it, and "unknown" is precisely the answer that
    does not tell a human to flip the breaker.
    """
    relay.last_telemetry = _clock["now"]
    _clock["now"] += 901
    health = relay.health()
    assert health["state"] == "degraded"
    assert health["evidence"]["dials"] == 0
    assert "breaker" in health["remediation"].lower()


def test_healthy_dials(relay: Relay, _clock) -> None:
    """Telemetry plus a poll, about a minute apart, repeatedly."""
    for _ in range(4):
        _clock["now"] += 5
        _dial(relay, _clock, [TELEMETRY, POLL], gap=60)
        relay.last_telemetry = _clock["now"]
    health = relay.health()
    assert health["state"] == "healthy"
    assert health["evidence"]["polls"] >= 3


def test_degraded_signature(relay: Relay, _clock) -> None:
    """Exactly one telemetry POST per dial, ~4 minutes apart, no polls."""
    relay.state["water_top"] = relay.state["setpoint"] - 20
    for _ in range(4):
        _dial(relay, _clock, [TELEMETRY], gap=240)
        relay.last_telemetry = _clock["now"]
    health = relay.health()
    assert health["state"] == "degraded"
    assert any("single" in reason for reason in health["reasons"])
    assert "breaker" in health["remediation"].lower()


def test_degraded_names_what_it_cannot_see(relay: Relay, _clock) -> None:
    """Two of the documented symptoms are outside the relay's view; say so.

    Ops keepalives are UDP through the gateway and the GraphQL message belongs to
    the app's API. Reporting them as observed would be a lie the user acts on.
    """
    for _ in range(4):
        _dial(relay, _clock, [TELEMETRY], gap=240)
        relay.last_telemetry = _clock["now"]
    relay.state["water_top"] = relay.state["setpoint"] - 20
    unobservable = " ".join(relay.health()["unobservable"])
    assert "keepalive" in unobservable and "GraphQL" in unobservable


def test_satisfied_tank_going_quiet_is_idle_not_degraded(relay: Relay, _clock) -> None:
    """The bug this replaces: a satisfied tank was reported as needing a breaker flip.

    The appliance reports on its own schedule (measured ~6 h) plus on events, so a
    tank holding temperature is legitimately silent for hours. Alarming on that
    trains the user to ignore the alarm that matters.
    """
    relay.state["water_top"] = relay.state["setpoint"]     # up to temperature
    for _ in range(3):
        _dial(relay, _clock, [TELEMETRY, POLL], gap=3600)
        relay.last_telemetry = _clock["now"]
    _clock["now"] += 1800                                  # 30 quiet minutes
    health = relay.health()
    assert health["state"] == "idle"
    assert "remediation" not in health


def test_silence_while_calling_for_heat_is_degraded(relay: Relay, _clock) -> None:
    """Positive evidence: the appliance should be busy and is not."""
    relay.state["water_top"] = relay.state["setpoint"] - 20   # 20 F below target
    for _ in range(3):
        _dial(relay, _clock, [TELEMETRY, POLL], gap=60)
        relay.last_telemetry = _clock["now"]
    _clock["now"] += 601
    health = relay.health()
    assert health["state"] == "degraded"
    assert any("calling for heat" in reason for reason in health["reasons"])
    assert health["evidence"]["calling_for_heat"] is True


def test_silence_beyond_its_own_interval_is_degraded(relay: Relay, _clock) -> None:
    """Quiet longer than the cadence it advertised for itself is a fault."""
    relay.state["water_top"] = relay.state["setpoint"]
    relay.reporting_interval_s = 21601                     # the measured value
    for _ in range(3):
        _dial(relay, _clock, [TELEMETRY, POLL], gap=60)
        relay.last_telemetry = _clock["now"]
    _clock["now"] += 21601 + 901
    health = relay.health()
    assert health["state"] == "degraded"
    assert any("reporting interval" in reason for reason in health["reasons"])


def test_pending_write_on_a_satisfied_quiet_tank_is_degraded(relay: Relay, _clock) -> None:
    """A queue that cannot drain is evidence, even when the tank has nothing to say."""
    relay.state["water_top"] = relay.state["setpoint"]
    for _ in range(3):
        _dial(relay, _clock, [TELEMETRY, POLL], gap=60)
        relay.last_telemetry = _clock["now"]
    relay.queue_write("setpoint", 140)
    _clock["now"] += 901
    health = relay.health()
    assert health["state"] == "degraded"
    assert any("queued" in reason for reason in health["reasons"])


def test_reporting_interval_is_learned_from_the_config_reply(relay: Relay) -> None:
    """The appliance states its own cadence; guessing it was the earlier mistake."""
    assert relay.reporting_interval_s is None
    relay._learn_reporting_interval(
        "<commands><properties><property><name>superBlockDynDataUpdateRate</name>"
        "<value>21601</value></property></properties></commands>")
    assert relay.reporting_interval_s == 21601


def test_degraded_holds_injection(relay: Relay, _clock) -> None:
    """The whole point: never inject into a module that has stopped listening."""
    for _ in range(4):
        _dial(relay, _clock, [TELEMETRY], gap=240)
        relay.last_telemetry = _clock["now"]
    relay.state["water_top"] = relay.state["setpoint"] - 20
    relay.queue_write("setpoint", 140)
    assert relay._inject(b"<commands><properties/></commands>") is None
    assert relay.injections_held == 1
    assert relay.health()["state"] == "degraded"


def test_recovery_clears_degraded(relay: Relay, _clock) -> None:
    """After a breaker flip the module polls again; the detector must notice."""
    for _ in range(4):
        _dial(relay, _clock, [TELEMETRY], gap=240)
        relay.last_telemetry = _clock["now"]
    assert relay.health()["state"] == "degraded"

    # Re-registration, then a normal conversation.
    for _ in range(3):
        _dial(relay, _clock, [REGTOKEN, TELEMETRY, POLL], gap=60)
        relay.last_telemetry = _clock["now"]
    assert relay.health()["state"] == "healthy"
