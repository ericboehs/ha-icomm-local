"""The LAN key file: never overwritten while it might hold the module's key."""

import json
import os
import stat

import pytest
from icomm_heater import write_private
from icomm_relay import Relay


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_FILE", str(tmp_path / "pending.json"))
    monkeypatch.delenv("LANIP_KEY", raising=False)
    monkeypatch.delenv("LANIP_FILE", raising=False)
    return tmp_path


def _key_file(data):
    return data / "heaters" / "default" / "lanip.json"


@pytest.mark.parametrize("content", ["{\"lanip_key\": \"abc", "null", "[1, 2]", "42",
                                     "{\"lanip_key\": 7}", "{\"lanip_key\": \"\"}",
                                     "{\"lanip_key\": \"k\", \"lanip_key_id\": null}",
                                     "{\"lanip_key\": \"k\", \"lanip_key_id\": \"x\"}"])
def test_an_unusable_key_file_is_moved_aside_not_overwritten(data, content, caplog):
    path = _key_file(data)
    path.parent.mkdir(parents=True)
    path.write_text(content)
    lan = Relay().lan                      # no AttributeError on null/list JSON
    assert lan.key
    aside = list(path.parent.glob("lanip.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text() == content
    assert json.loads(path.read_text())["lanip_key"] == lan.key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert lan.status()["key_quarantined"] == 1
    assert any(r.levelname == "ERROR" and "kept it as" in r.getMessage()
               for r in caplog.records)
    assert lan.key not in caplog.text


def test_a_second_corruption_says_so(data, caplog, monkeypatch):
    path = _key_file(data)
    path.parent.mkdir(parents=True)
    (path.parent / "lanip.json.corrupt-1").write_text("old")
    path.write_text("{")
    Relay()
    assert "1 earlier corrupt" in caplog.text


def test_a_key_file_that_cannot_be_moved_aside_is_left_alone(data, monkeypatch, caplog):
    path = _key_file(data)
    path.parent.mkdir(parents=True)
    path.write_text("{")

    def refuse(self, target):
        raise PermissionError("read-only")

    monkeypatch.setattr("pathlib.Path.rename", refuse)
    lan = Relay().lan
    assert lan.key                         # a temporary key, in memory only
    assert path.read_text() == "{"         # the original is untouched
    assert "could not be moved aside" in caplog.text


def test_saved_fields_of_the_wrong_type_are_ignored(data):
    path = _key_file(data)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"lanip_key": "k" * 28, "lanip_key_id": 2,
                                "module_ip": ["x"], "module_dsn": 5}))
    lan = Relay().lan
    assert lan.key == "k" * 28 and lan.key_id == 2
    assert lan.module_ip is None and lan.module_dsn is None


def test_a_missing_key_file_mints_one_without_quarantine(data):
    lan = Relay().lan
    assert lan.key_quarantined == 0
    assert not list(_key_file(data).parent.glob("*.corrupt-*"))


def test_key_file_is_created_0600_not_chmodded_afterwards(data, monkeypatch):
    """Never readable under the umask, even between create and chmod (#12)."""
    calls = []
    monkeypatch.setattr("os.chmod", lambda *a, **k: calls.append(a))
    old = os.umask(0o022)
    try:
        lan = Relay().lan
    finally:
        os.umask(old)
    assert not calls
    assert stat.S_IMODE(lan.key_file.stat().st_mode) == 0o600
    lan.learn_module_ip("<device><lan-ip>10.0.1.187</lan-ip></device>")   # _persist
    assert stat.S_IMODE(lan.key_file.stat().st_mode) == 0o600
    assert not [p for p in lan.key_file.parent.iterdir() if p.name.endswith(".tmp")]


def test_write_private_replaces_atomically_and_cleans_up(tmp_path, monkeypatch):
    target = tmp_path / "f.json"
    target.write_text("old")
    stale = tmp_path / f".f.json.{os.getpid()}.tmp"
    stale.write_text("x")
    stale.chmod(0o644)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError):
        write_private(target, b"new")
    assert target.read_text() == "old"     # never torn
    assert not stale.exists()
    monkeypatch.undo()
    write_private(target, b"new")
    assert target.read_text() == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_persist_failure_is_logged_by_type(data, monkeypatch, caplog):
    lan = Relay().lan

    def boom(path, payload):
        raise PermissionError("secret-ish detail")

    monkeypatch.setattr("icomm_lan.write_private", boom)
    lan.learn_module_ip("<device><lan-ip>10.0.1.187</lan-ip></device>")
    assert "PermissionError" in caplog.text and "secret-ish" not in caplog.text
