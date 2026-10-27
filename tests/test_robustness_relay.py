"""Relay robustness: malformed input and disk errors never break the proxy."""

import stat

import pytest
from icomm_relay import Relay

DSN_XML = (b"<device><lan-ip>10.0.1.187</lan-ip><lan-enabled>false</lan-enabled>"
           b"<local-access-enabled>false</local-access-enabled></device>")


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setenv("LANIP_KEY", "a" * 28)
    return Relay()


# -- #10: the DSN evidence files ---------------------------------------------
def test_dsn_evidence_is_written_0600(relay):
    assert relay._rewrite_dsn(DSN_XML) is not None
    for name in ("dsn-original.xml", "dsn-rewritten.xml"):
        path = relay.heater.dir / name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (relay.heater.dir / "dsn-original.xml").read_bytes() == DSN_XML


def test_a_disk_error_saving_evidence_still_returns_the_rewrite(relay, monkeypatch, caplog):
    def boom(path, data):
        raise OSError("no space left")

    monkeypatch.setattr("icomm_relay.write_private", boom)
    out = relay._rewrite_dsn(DSN_XML)
    assert out is not None and b"<lan-enabled>true</lan-enabled>" in out
    assert "evidence not saved (OSError)" in caplog.text
