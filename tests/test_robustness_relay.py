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


# -- #4: block positions and short blocks -------------------------------------
def _post(blocks):
    return (b"<datapoint><value>" + ",".join(b.hex() for b in blocks).encode()
            + b"</value></datapoint>")


def test_an_empty_block_keeps_its_place(blocks_electric):
    from icomm_relay import telemetry_blocks
    blocks = list(blocks_electric)
    blocks[2] = b""
    parsed = telemetry_blocks(_post(blocks))
    assert len(parsed) == len(blocks) and parsed[2] == b"" and parsed[4] == blocks[4]


@pytest.mark.parametrize("index", [1, 3, 4])
def test_a_short_block_is_a_decode_error_not_zeros(relay, blocks_electric, index):
    from icomm_relay import decode
    blocks = list(blocks_electric)
    blocks[index] = b""
    with pytest.raises(IndexError):
        decode(blocks)
    relay._ingest_blocks(blocks)
    assert relay.decode_errors == 1
    assert "hot_water_availability" not in relay.state     # no invented 100 %


def test_an_empty_block_4_over_the_cloud_is_counted(relay, blocks_electric):
    from icomm_relay import telemetry_blocks
    blocks = list(blocks_electric)
    blocks[4] = b""
    relay._ingest_blocks(telemetry_blocks(_post(blocks)))
    assert relay.decode_errors == 1 and relay.state.get("watts") is None


def test_full_blocks_still_decode(relay, blocks_electric):
    relay._ingest_blocks(list(blocks_electric))
    assert relay.decode_errors == 0 and relay.state["watts"] == 4500
