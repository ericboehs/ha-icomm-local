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


# -- #5: ingest never stops the forward ---------------------------------------
async def test_an_ingest_bug_still_forwards_the_report(relay, monkeypatch, caplog,
                                                        blocks_electric):
    from aiohttp.test_utils import make_mocked_request
    from aiohttp.web import Response

    forwarded = []

    async def proxy(request, transform=None):
        forwarded.append(request.path)
        return Response(status=201)

    def explode(blocks, lan=False):
        raise RuntimeError("bug")

    monkeypatch.setattr(relay, "proxy", proxy)
    monkeypatch.setattr(relay, "_ingest_blocks", explode)
    request = make_mocked_request(
        "POST", "/apiv1/dsns/X/properties/superBlockDyn1/datapoints.xml")

    async def read():
        return _post(blocks_electric)

    monkeypatch.setattr(request, "read", read)
    response = await relay.telemetry(request)
    assert response.status == 201 and forwarded
    assert relay.ingest_errors == 1
    assert "telemetry ingest failed (RuntimeError)" in caplog.text


async def test_a_lan_ingest_bug_is_counted_not_raised(relay, monkeypatch, blocks_electric):
    def explode(blocks, lan=False):
        raise RuntimeError("bug")

    monkeypatch.setattr(relay, "_ingest_blocks", explode)
    value = ",".join(b.hex() for b in blocks_electric)
    await relay.ingest_lan_update({"data": {"name": "superBlockDyn1", "value": value}})
    assert relay.ingest_errors == 1


# -- #5/#11: the restored queue ------------------------------------------------
def _restore(relay, payload):
    import json
    relay.queue_file.parent.mkdir(parents=True, exist_ok=True)
    relay.queue_file.write_text(json.dumps(payload))
    relay._load_queue()


@pytest.mark.parametrize("payload", [None, [], 3, "x", {"pending": [1]},
                                     {"pending": None, "last_write": [1]}])
def test_a_queue_file_of_the_wrong_shape_starts_empty(relay, payload):
    _restore(relay, payload)
    assert relay.pending == {} and relay.last_write is None


@pytest.mark.parametrize("entry", [
    "not a dict",
    {"frame": "01 02", "value": None},
    {"frame": "01 02", "value": "hot"},
    {"frame": "01 02", "value": True},
    {"frame": 5, "value": 140},
    {"frame": "01 02", "value": 140, "queued": "yesterday"},
    {"frame": "01 02", "value": 140, "attempts": "two"},
    {"frame": "01 02", "value": 140, "kind": "mode"},
])
def test_a_malformed_setpoint_write_is_dropped(relay, entry, blocks_electric):
    _restore(relay, {"pending": {"setpoint": entry}})
    assert relay.pending == {}
    relay._ingest_blocks(list(blocks_electric))         # and telemetry keeps working
    assert relay.state["online"] is True


def test_a_malformed_mode_write_is_dropped(relay):
    _restore(relay, {"pending": {"mode": {"frame": "01", "value": 4}}})
    assert relay.pending == {}


def test_a_good_write_is_still_restored(relay):
    import time
    _restore(relay, {"pending": {
        "setpoint": {"frame": "01 02", "value": 140.0, "queued": time.time(), "attempts": 1},
        "mode": {"frame": "03", "value": "heat_pump"}}})
    assert set(relay.pending) == {"setpoint", "mode"}
    assert relay.pending["setpoint"].value == 140.0


@pytest.mark.parametrize("last", [
    {"outcome": "not_applied", "kind": "setpoint", "delivered": "soon", "value": 140},
    {"outcome": "not_applied", "kind": "setpoint", "delivered": 0, "value": None},
    {"outcome": "not_applied", "kind": "setpoint", "delivered": None, "value": "x"},
])
def test_a_malformed_last_write_never_breaks_confirmation(relay, last, blocks_electric):
    import time
    if last["delivered"] == 0:
        last["delivered"] = time.time()
    _restore(relay, {"last_write": last})
    relay._ingest_blocks(list(blocks_electric))
    assert relay.ingest_errors == 0 and relay.last_write["outcome"] == "not_applied"


# -- #11: device facts of the wrong shape --------------------------------------
@pytest.mark.parametrize("content", ["null", "[1]", "{\"facts\": [1], \"static\": 3}",
                                     "{\"facts\": {\"dsn\": \"AC000W1\"}, \"static\": null}"])
def test_device_facts_of_the_wrong_shape_do_not_crash(tmp_path, content):
    from icomm_device import DeviceFacts
    path = tmp_path / "device.json"
    path.write_text(content)
    facts = DeviceFacts(path)
    assert facts.static == {}
    assert facts.facts in ({}, {"dsn": "AC000W1"})
