"""The cloud-echo probe (phase 53).

A healthy module passes a LAN reporting-mask write on to the cloud
(POST periodicParameterSetting?echo=true) about 100 ms before it acks it over
LAN: 8 of 8 overnight, and every probe while healthy. After a long relay
outage (a 7-minute host reboot, reproduced with a silent drop) it acks with
no echo, and stays that way until its Wi-Fi drops or it's power-cycled. Health
said "unknown: too few dials observed yet" for 8.5 hours; the probe turns that
into "degraded: cloud_down" within minutes.
"""

from __future__ import annotations

import json

import pytest
from icomm_relay import CLOUD_PROBE_FIRST_S, CLOUD_PROBE_GRACE_S, Relay

ECHO = "/devices/1/properties/periodicParameterSetting/datapoints.xml?echo=true"
TELEMETRY = "/devices/1/properties/superBlockDyn1/datapoints.xml"


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch):
    state = {"now": 1_000_000.0}
    monkeypatch.setattr("icomm_relay.time.time", lambda: state["now"])
    monkeypatch.setattr("icomm_lan.time.time", lambda: state["now"])
    return state


@pytest.fixture
def relay(_clock) -> Relay:
    relay = Relay()
    relay.started = _clock["now"]
    relay.lan.enabled = relay.lan.registered = relay.lan.session_verified = True
    relay.lan.session = object()                        # a session to carry the mask
    return relay


def _send(relay: Relay, clock) -> str:
    assert relay.lan.mask_pending
    relay.lan.mask_pending = False
    relay.lan.masks_sent += 1
    relay.lan.last_mask_sent = clock["now"]
    relay.lan.mask_id = f"m{relay.lan.masks_sent}"
    return relay.lan.mask_id


def _probe(relay: Relay, clock, echo: bool) -> None:
    """Probe, deliver, (echo,) ack, then wait out the grace period."""
    relay.maybe_probe_cloud()
    mask_id = _send(relay, clock)
    clock["now"] += 6
    if echo:
        relay._note_request(ECHO)
    clock["now"] += 0.1
    relay._record_ack(mask_id, 200)
    clock["now"] += CLOUD_PROBE_GRACE_S


def test_no_probe_before_five_quiet_minutes(relay, _clock):
    _clock["now"] += CLOUD_PROBE_FIRST_S - 1
    relay.maybe_probe_cloud()
    assert not relay.lan.mask_pending
    _clock["now"] += 1
    relay.maybe_probe_cloud()
    assert relay.lan.mask_pending and relay.cloud_probes == 1


def test_an_ack_without_an_echo_is_cloud_down(relay, _clock):
    _clock["now"] += CLOUD_PROBE_FIRST_S
    _probe(relay, _clock, echo=False)
    health = relay.health()
    assert health["state"] == "degraded" and health["code"] == "cloud_down"
    assert "didn't pass it on to Ayla" in health["reasons"][0]
    assert "90 seconds" in health["remediation"] and "breaker" in health["remediation"]
    assert relay.cloud_probe_result == "missing" and relay.cloud_down_count == 1


def test_an_echo_before_the_ack_is_healthy(relay, _clock):
    _clock["now"] += CLOUD_PROBE_FIRST_S
    _probe(relay, _clock, echo=True)
    assert relay.health().get("code") != "cloud_down"
    assert relay.cloud_probe_result == "echoed" and relay.cloud_down_since is None


def test_no_verdict_inside_the_grace_period(relay, _clock):
    """The echo comes first when healthy, but give a late one a chance."""
    _clock["now"] += CLOUD_PROBE_FIRST_S
    relay.maybe_probe_cloud()
    mask_id = _send(relay, _clock)
    relay._record_ack(mask_id, 200)
    _clock["now"] += CLOUD_PROBE_GRACE_S - 1
    assert relay.health().get("code") != "cloud_down"
    relay._note_request(ECHO)                           # late, but in time
    _clock["now"] += 5
    assert relay.health().get("code") != "cloud_down"


def test_any_cloud_request_clears_it(relay, _clock):
    """The fix (a Wi-Fi drop or a power cycle) ends in a config fetch and posts."""
    _clock["now"] += CLOUD_PROBE_FIRST_S
    _probe(relay, _clock, echo=False)
    assert relay.health()["code"] == "cloud_down"
    _clock["now"] += 600
    relay._note_request("/dsns/AC000W000000000.xml")
    assert relay.health().get("code") != "cloud_down"
    assert relay.cloud_down_since is None and relay.cloud_probe_result == "echoed"


def test_an_unrelated_ack_is_not_the_probe(relay, _clock):
    _clock["now"] += CLOUD_PROBE_FIRST_S
    relay.maybe_probe_cloud()
    _send(relay, _clock)
    relay._record_ack("someWrite", 200)
    _clock["now"] += 60
    assert relay.lan.mask_acked == 0.0
    assert relay.health().get("code") != "cloud_down"


def test_spacing_and_one_in_flight(relay, _clock):
    _clock["now"] += CLOUD_PROBE_FIRST_S
    relay.maybe_probe_cloud()
    _send(relay, _clock)                                # sent, never acked
    _clock["now"] += 60
    relay.maybe_probe_cloud()
    assert not relay.lan.mask_pending                   # one in flight
    _clock["now"] += relay.cloud_probe_after
    relay.maybe_probe_cloud()
    assert relay.lan.mask_pending and relay.cloud_probes == 2


def test_after_a_cloud_request_it_waits_the_full_interval(relay, _clock):
    relay._note_request(TELEMETRY)                      # heard since start
    _clock["now"] += CLOUD_PROBE_FIRST_S
    relay.maybe_probe_cloud()
    assert not relay.lan.mask_pending
    _clock["now"] += relay.cloud_probe_after
    relay.maybe_probe_cloud()
    assert relay.lan.mask_pending


@pytest.mark.parametrize("change", ["off", "no_session", "pending"])
def test_no_probe_when_it_cannot_or_should_not(relay, _clock, change):
    if change == "off":
        relay.cloud_probe = False
    elif change == "no_session":
        relay.lan.session_verified = False
    else:
        relay.lan.mask_pending = True
    _clock["now"] += CLOUD_PROBE_FIRST_S
    relay.maybe_probe_cloud()
    assert relay.cloud_probes == 0


def test_cloud_probe_env(monkeypatch):
    monkeypatch.setenv("CLOUD_PROBE", "0")
    monkeypatch.setenv("CLOUD_PROBE_AFTER", "900")
    relay = Relay()
    assert relay.cloud_probe is False and relay.cloud_probe_after == 900


async def test_state_reports_the_probe(relay, _clock):
    _clock["now"] += CLOUD_PROBE_FIRST_S
    _probe(relay, _clock, echo=False)
    _clock["now"] += 120
    response = await relay.state_handler(None)
    probe = json.loads(response.body)["cloud_probe"]
    assert probe == {"enabled": True, "after_s": relay.cloud_probe_after, "probes": 1,
                     "result": "missing", "down": True, "down_s": 135, "times_down": 1}


def test_an_answered_probe_still_waits_the_interval(relay, _clock):
    _clock["now"] += CLOUD_PROBE_FIRST_S
    _probe(relay, _clock, echo=False)                   # acked: not in flight
    _clock["now"] += 60
    relay.maybe_probe_cloud()
    assert not relay.lan.mask_pending and relay.cloud_probes == 1
