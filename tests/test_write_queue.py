"""The write queue's safety rules: clamping, coalescing, gating, confirmation.

These are the behaviours that decide whether a bug in the write path heats a tank
or black-holes it, so they are tested rather than trusted.
"""

from __future__ import annotations

import pytest
from icomm_relay import PendingWrite, Relay, decode

FRAME_138 = "BD 40 08 0B 06 3A E4 92"


@pytest.fixture
def relay(blocks_electric: list[bytes]) -> Relay:
    """A relay that has already seen telemetry, so writes are not gated."""
    relay = Relay()
    relay.state = decode(blocks_electric)
    relay.state["online"] = True
    relay.last_telemetry = 1_000_000.0
    return relay


@pytest.fixture(autouse=True)
def _frozen_time(monkeypatch: pytest.MonkeyPatch):
    """Pin the clock so freshness windows are decidable."""
    monkeypatch.setattr("icomm_relay.time.time", lambda: 1_000_060.0)


def test_queue_setpoint(relay: Relay) -> None:
    write = relay.queue_write("setpoint", 140)
    assert isinstance(write, PendingWrite)
    assert write.kind == "setpoint"
    assert write.frame.startswith("BD 40 08 0B 06")
    assert relay.pending["setpoint"] is write


def test_setpoint_clamped_to_the_appliance_ceiling(relay: Relay) -> None:
    """The ceiling comes from the appliance's own telemetry (150 F here)."""
    assert relay.state["max_setpoint"] == 150.0
    with pytest.raises(ValueError, match="outside"):
        relay.queue_write("setpoint", 151)
    with pytest.raises(ValueError, match="outside"):
        relay.queue_write("setpoint", 300)
    with pytest.raises(ValueError, match="outside"):
        relay.queue_write("setpoint", 60)


def test_setpoint_ceiling_survives_a_bogus_max(relay: Relay) -> None:
    """A garbage max setpoint must not become an unbounded write."""
    relay.state["max_setpoint"] = 240.0
    with pytest.raises(ValueError):
        relay.queue_write("setpoint", 200)


def test_unknown_mode_rejected(relay: Relay) -> None:
    with pytest.raises(ValueError):
        relay.queue_write("mode", "turbo")
    with pytest.raises(ValueError):
        relay.queue_write("volume", 11)


def test_newer_write_supersedes_older(relay: Relay) -> None:
    """Two setpoints in flight: only the newest may reach the appliance."""
    relay.queue_write("setpoint", 139)
    relay.queue_write("setpoint", 142)
    assert len(relay.pending) == 1
    assert relay.pending["setpoint"].value == 142.0
    assert relay.pending["setpoint"].superseded == 1


def test_injection_delivers_and_records(relay: Relay) -> None:
    relay.queue_write("setpoint", 140)
    out = relay._inject(b"<commands>\n  <properties/>\n  <schedules/>\n  <cmds/>\n</commands>\n")
    assert out is not None
    assert relay.pending["setpoint"].frame in out.decode()
    assert relay.pending["setpoint"].attempts == 1
    assert relay.writes_delivered == 1


def test_injection_reported_as_not_a_confirmation(relay: Relay) -> None:
    """Delivery is not success: the write stays pending until telemetry agrees."""
    relay.queue_write("setpoint", 140)
    relay._inject(b"<commands><properties/></commands>")
    assert "setpoint" in relay.pending
    assert relay.pending["setpoint"].confirmed is None
    assert relay.last_write is None


def test_telemetry_confirms_the_write(relay: Relay, blocks_hybrid: list[bytes]) -> None:
    relay.queue_write("setpoint", 140)
    relay._inject(b"<commands><properties/></commands>")
    # The hybrid capture reports setpoint 140.0 -- the appliance took it.
    relay.state = decode(blocks_hybrid)
    relay._confirm_pending()
    assert relay.pending == {}
    assert relay.last_write["outcome"] == "confirmed"
    assert relay.writes_confirmed == 1


def test_telemetry_showing_the_old_value_does_not_confirm(relay: Relay,
                                                        blocks_electric: list[bytes]) -> None:
    relay.queue_write("setpoint", 120)
    relay.state = decode(blocks_electric)      # still 149 F
    relay._confirm_pending()
    assert "setpoint" in relay.pending


def test_mode_confirms_on_mode_not_on_setpoint(relay: Relay, blocks_hybrid: list[bytes]) -> None:
    relay.queue_write("mode", "hybrid")
    relay.state = decode(blocks_hybrid)
    relay._confirm_pending()
    assert relay.pending == {}
    assert relay.last_write["kind"] == "mode"


def test_stale_appliance_holds_writes(relay: Relay, monkeypatch) -> None:
    """A silent appliance is in its degraded state; injecting would be a no-op.

    The module ignores queued commands when degraded (the cure is a breaker flip),
    so holding and saying so is the honest behaviour.
    """
    relay.queue_write("setpoint", 140)
    monkeypatch.setattr("icomm_relay.time.time", lambda: relay.last_telemetry + 601)
    assert relay._inject(b"<commands><properties/></commands>") is None
    assert relay.injections_held == 1
    assert "setpoint" in relay.pending


def test_no_pending_means_no_rewrite(relay: Relay) -> None:
    assert relay._inject(b"<commands><properties/></commands>") is None
    assert relay.writes_delivered == 0


def test_unrewritable_reply_is_left_alone(relay: Relay) -> None:
    relay.queue_write("setpoint", 140)
    assert relay._inject(b"<html>404</html>") is None
    assert relay.writes_delivered == 0
