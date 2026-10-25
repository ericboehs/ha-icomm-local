"""#18 phase 1: a heater's files move to heaters/<DSN>/, safely and once."""

from __future__ import annotations

import json
import os

from icomm_heater import Heater, known_dsn, migrate
from icomm_relay import Relay
from test_dns_pinning import HEATER, _fetch_dsn

DSN = "AC000W019949045"
KEY = "k" * 28


def _live_layout(data):
    """The single-heater /data of a 0.5.x add-on that pinned its module over DNS."""
    data.mkdir(parents=True, exist_ok=True)
    (data / "lanip.json").write_text(json.dumps({
        "lanip_key": KEY, "lanip_key_id": 1, "module_ip": HEATER,
        "module_ip_observed": True, "module_dsn": DSN}))
    os.chmod(data / "lanip.json", 0o600)
    (data / "pending.json").write_text(json.dumps({"pending": {}, "last_write": {"kind": "mode"}}))
    (data / "device.json").write_text(json.dumps({"facts": {"controller_firmware": "2.14"}}))
    (data / "dsn-original.xml").write_text("<device><lan-enabled>false</lan-enabled></device>")
    (data / "firmware.json").write_text(json.dumps({"allowed": []}))
    (data / "certs").mkdir()


def _start(data, monkeypatch, **env):
    monkeypatch.setenv("QUEUE_FILE", str(data / "pending.json"))
    monkeypatch.delenv("LANIP_KEY", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Relay()


def test_an_upgrade_moves_the_live_layout_into_the_heaters_dir(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _live_layout(data)
    relay = _start(data, monkeypatch)
    home = data / "heaters" / DSN
    assert relay.heater.dir == home and relay.heater.dsn == DSN
    # Same key, same pinned module: Local Connect carries on without a power cycle.
    assert (relay.lan.key, relay.lan.module_ip, relay.lan.module_ip_observed) == (KEY, HEATER, True)
    assert relay.lan.key_file == home / "lanip.json"
    assert (home / "lanip.json").stat().st_mode & 0o777 == 0o600
    relay._load_queue()
    assert relay.last_write == {"kind": "mode"}                     # queue restored from home
    assert relay.device.status()["controller_firmware"] == "2.14"
    assert relay.offline.available()
    # Downgrade-safe: the key and facts stay where 0.5.x looks; the queue doesn't
    # (an older relay must not replay it); relay-wide files don't move at all.
    assert (data / "lanip.json").exists() and (data / "device.json").exists()
    assert not (data / "pending.json").exists()
    assert (data / "firmware.json").exists() and not (home / "firmware.json").exists()
    assert (data / "certs").is_dir()


def test_a_second_start_changes_nothing(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _live_layout(data)
    first = _start(data, monkeypatch)
    first.lan.module_ip = "10.0.20.200"
    first.lan._persist()                                           # newer than the legacy copy
    again = _start(data, monkeypatch)
    assert again.lan.module_ip == "10.0.20.200"                    # legacy copy not re-applied


def test_an_unknown_dsn_waits_in_default_then_moves(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "lanip.json").write_text(json.dumps({"lanip_key": KEY, "lanip_key_id": 1}))
    relay = _start(data, monkeypatch, UPSTREAM="ayla.test")
    assert relay.heater.dir == data / "heaters" / "default" and relay.heater.dsn is None
    relay.queue_write("setpoint", 140)
    assert (data / "heaters" / "default" / "pending.json").exists()


async def test_the_first_pin_names_the_directory(tmp_path, monkeypatch):
    data = tmp_path / "data"
    relay = _start(data, monkeypatch, UPSTREAM="ayla.test")        # a fresh DNS install
    relay.queue_write("setpoint", 140)
    await _fetch_dsn(relay)                                        # Ayla's 200 pins it
    home = data / "heaters" / DSN
    assert relay.heater.dir == home and not (data / "heaters" / "default").exists()
    assert json.loads((home / "lanip.json").read_text())["module_dsn"] == DSN
    assert (home / "pending.json").exists() and (home / "dsn-original.xml").exists()
    assert relay.device.path == home / "device.json"
    again = _start(data, monkeypatch, UPSTREAM="ayla.test")
    assert again.heater.dir == home and again.lan.module_ip == HEATER


def test_a_trusted_config_fetch_names_it_too(tmp_path, monkeypatch):
    heater = Heater(tmp_path)
    heater.adopt("bad dsn!")                                       # not a DSN: ignored
    assert heater.dsn is None
    heater.adopt("ac000w019949045")
    assert heater.dsn == DSN and heater.dir.name == DSN
    heater.adopt("AC000W000000001")                                # first one wins (phase 1)
    assert heater.dsn == DSN


def test_adopting_when_the_dsn_dir_already_exists(tmp_path):
    (tmp_path / "heaters" / DSN).mkdir(parents=True)
    heater = Heater(tmp_path)                                      # known_dsn not consulted
    heater.adopt(DSN)
    assert heater.dir == tmp_path / "heaters" / DSN
    assert (tmp_path / "heaters" / "default").exists() is False


def test_where_the_dsn_comes_from(tmp_path):
    assert known_dsn(tmp_path) is None
    (tmp_path / "device.json").write_text(json.dumps({"facts": {"dsn": "ac000w000000002"}}))
    assert known_dsn(tmp_path) == "AC000W000000002"
    (tmp_path / "lanip.json").write_text(json.dumps({"module_dsn": "AC000W000000003"}))
    assert known_dsn(tmp_path) == "AC000W000000003"
    (tmp_path / "heaters" / "default").mkdir(parents=True)
    (tmp_path / "heaters" / "AC000W000000004").mkdir()
    assert known_dsn(tmp_path) == "AC000W000000004"
    assert known_dsn(tmp_path, "AC000W000000005") == "AC000W000000005"
    (tmp_path / "lanip.json").write_text("[1, 2]")                 # not an object
    assert known_dsn(tmp_path / "nowhere") is None


def test_a_custom_queue_name_and_an_existing_default(tmp_path):
    (tmp_path / "queue.json").write_text("{}")
    (tmp_path / "heaters" / "default").mkdir(parents=True)
    home = migrate(tmp_path, DSN, "queue.json")
    assert home == tmp_path / "heaters" / DSN                      # default renamed
    assert (home / "pending.json").exists() and not (tmp_path / "queue.json").exists()


def test_a_dsn_is_only_ever_a_plain_name(tmp_path):
    from icomm_heater import clean_dsn
    assert clean_dsn("ac000w019949045") == DSN
    for bad in ("AC000W019949045\n", "../../etc", "AC000W/0199", "", None, 42, "x" * 33):
        assert clean_dsn(bad) is None
    heater = Heater(tmp_path, "AC000W019949045\n")                # rejected: default slot
    assert heater.dsn is None and heater.dir.name == "default"
    heater.adopt("AC000W019949045\nINJECTED")
    assert heater.dsn is None


def test_adopt_stays_inside_heaters(tmp_path, monkeypatch):
    heater = Heater(tmp_path)
    monkeypatch.setattr("icomm_heater.clean_dsn", lambda value: "..")    # if validation slipped
    heater.adopt("anything")
    assert heater.dsn is None and heater.dir.name == "default"
