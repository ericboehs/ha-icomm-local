"""The write queue across restarts: it used to vanish, now it is a file.

The queue was in memory, so restarting the relay silently dropped a queued write --
observed while waiting on an idle appliance whose queue was the only evidence.
"""

from __future__ import annotations

import json

import pytest
from icomm_relay import Relay, decode


@pytest.fixture
def queue_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "state" / "pending.json"
    monkeypatch.setenv("QUEUE_FILE", str(path))
    return path


def _relay(tmp_path) -> Relay:
    return Relay()


def test_queue_is_written_to_disk(queue_file, blocks_heat_pump: list[bytes]) -> None:
    relay = _relay(queue_file)
    relay.state = decode(blocks_heat_pump)
    relay.queue_write("setpoint", 140)
    saved = json.loads(queue_file.read_text())
    assert saved["pending"]["setpoint"]["frame"].startswith("BD 40 08 0B 06")
    assert saved["pending"]["setpoint"]["value"] == 140.0


def test_queued_write_survives_a_restart(queue_file, blocks_heat_pump: list[bytes]) -> None:
    first = _relay(queue_file)
    first.state = decode(blocks_heat_pump)
    first.queue_write("setpoint", 140)

    second = _relay(queue_file)          # a new process reading the same file
    second._load_queue()
    assert second.pending["setpoint"].value == 140.0
    assert second.pending["setpoint"].restored is True
    assert second.queue_restored == 1


def test_cancelled_queue_does_not_come_back(queue_file, blocks_heat_pump: list[bytes]) -> None:
    first = _relay(queue_file)
    first.state = decode(blocks_heat_pump)
    first.queue_write("setpoint", 140)
    first.pending.clear()
    first._save_queue()

    second = _relay(queue_file)
    second._load_queue()
    assert second.pending == {}


def test_stale_write_is_dropped_not_delivered(queue_file, monkeypatch,
                                              blocks_heat_pump: list[bytes]) -> None:
    """An appliance idle overnight must not surprise anyone at breakfast."""
    first = _relay(queue_file)
    first.state = decode(blocks_heat_pump)
    first.queue_write("setpoint", 140)

    # Pretend the write is two days old.
    saved = json.loads(queue_file.read_text())
    saved["pending"]["setpoint"]["queued"] -= 2 * 24 * 3600
    queue_file.write_text(json.dumps(saved))

    second = _relay(queue_file)
    second.queue_max_age_s = 24 * 3600
    second._load_queue()
    assert second.pending == {}


def test_corrupt_queue_file_does_not_stop_the_relay(queue_file) -> None:
    """A truncated file must not take the appliance's cloud bridge down."""
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    queue_file.write_text("{not json")
    relay = _relay(queue_file)
    relay._load_queue()
    assert relay.pending == {}


def test_save_is_atomic(queue_file, blocks_heat_pump: list[bytes]) -> None:
    """No half-written file: it is written to a temporary path and renamed."""
    relay = _relay(queue_file)
    relay.state = decode(blocks_heat_pump)
    relay.queue_write("setpoint", 140)
    assert not queue_file.with_suffix(".tmp").exists()
    assert json.loads(queue_file.read_text())["pending"]


def test_confirmation_is_persisted_too(queue_file, blocks_hybrid: list[bytes]) -> None:
    """`last_write` is what a human reads after a restart; it must survive as well."""
    relay = _relay(queue_file)
    relay.state = decode(blocks_hybrid)
    relay.queue_write("setpoint", 140)
    relay.state = decode(blocks_hybrid)
    relay._confirm_pending()

    saved = json.loads(queue_file.read_text())
    assert saved["pending"] == {}
    assert saved["last_write"]["outcome"] == "confirmed"

    restored = _relay(queue_file)
    restored._load_queue()
    assert restored.last_write["value"] == 140.0


def test_delivery_attempt_is_persisted(queue_file, blocks_heat_pump: list[bytes]) -> None:
    """A restart must not replay a write that was already handed to the appliance."""
    relay = _relay(queue_file)
    relay.state = decode(blocks_heat_pump)
    relay.queue_write("setpoint", 140)
    relay.state["water_top"] = relay.state["setpoint"] - 20   # calling for heat
    relay.last_telemetry = __import__("time").time()
    relay._inject(b"<commands><properties/></commands>")

    saved = json.loads(queue_file.read_text())
    assert saved["pending"]["setpoint"]["attempts"] == 1
    assert saved["pending"]["setpoint"]["delivered"] is not None


def test_state_file_lives_where_the_addon_keeps_data(queue_file) -> None:
    """Default location is overridable, because /data is what survives in a container."""
    relay = _relay(queue_file)
    assert str(relay.queue_file) == str(queue_file)
