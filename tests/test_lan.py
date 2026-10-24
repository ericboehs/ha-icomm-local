"""LAN mode: the local doorbell, tested against an independent module side.

The protocol comes from Ayla's libada (client.c/lan.c) and deiger/AirCon. These
tests build the *module's* side straight from the libada formulas rather than
from relay/icomm_lan.py, so a mistake in the server cannot validate itself.
"""

import base64
import hmac
import json
import os
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from Crypto.Cipher import AES
from icomm_lan import LanServer, Session

from relay.icomm_relay import Relay

SECRET = "test-lanip-key-28-chars-pad!!"
EXCHANGE = {"ver": 1, "proto": 1, "random_1": "RpAKxT1NIl6t8XgK",
            "time_1": 185705179279283, "key_id": 1}


def module_directions(secret: bytes, exchange: dict, random_2: str, time_2: int):
    """The module's own KDF, independently written from libada's gen_keys."""
    def kdf(msg: bytes) -> bytes:
        return hmac.digest(secret, hmac.digest(secret, msg, "sha256") + msg, "sha256")

    def direction(msg: bytes):
        return {"sign": kdf(msg + b"0"),
                "cipher": AES.new(kdf(msg + b"1"), AES.MODE_CBC, kdf(msg + b"2")[:16])}

    r1, r2 = exchange["random_1"].encode(), random_2.encode()
    t1, t2 = str(exchange["time_1"]).encode(), str(time_2).encode()
    return direction(r2 + r1 + t2 + t1), direction(r1 + r2 + t1 + t2)  # tx, rx


def test_key_exchange_response_and_session_directions():
    session = Session(SECRET.encode(), EXCHANGE)
    response = session.response()
    assert set(response) == {"random_2", "time_2"}
    assert len(response["random_2"]) == 16
    assert 0 <= response["time_2"] < 2**40

    # The module encrypts to us with mod_root, we encrypt back with app_root.
    tx, rx = module_directions(SECRET.encode(), EXCHANGE,
                               response["random_2"], response["time_2"])
    payload = {"seq_no": 1, "data": {"name": "x", "value": "y"}}
    text = json.dumps(payload).encode()
    padded = text.ljust((len(text) + 15) // 16 * 16, bytes([0]))
    body = {"enc": base64.b64encode(tx["cipher"].encrypt(padded)).decode(),
            "sign": base64.b64encode(hmac.digest(tx["sign"], text, "sha256")).decode()}
    assert session.dev.decrypt_validate(body) == payload

    # ...and our response decrypts on the module's side with the other root.
    ours = session.app.encrypt_sign({"seq_no": 2, "data": {}})
    plain = rx["cipher"].decrypt(base64.b64decode(ours["enc"])).rstrip(bytes([0]))
    assert json.loads(plain.decode()) == {"seq_no": 2, "data": {}}
    assert ours["sign"] == base64.b64encode(
        hmac.digest(rx["sign"], plain, "sha256")).decode()


def test_bad_signature_is_rejected():
    session = Session(SECRET.encode(), EXCHANGE)
    tx, _ = module_directions(SECRET.encode(), EXCHANGE,
                              session.random_2, session.time_2)
    text = json.dumps({"seq_no": 0, "data": {}}).encode()
    body = {"enc": base64.b64encode(tx["cipher"].encrypt(text.ljust(32, bytes([0])))).decode(),
            "sign": base64.b64encode(b"not-the-signature-at-all!!!!!").decode()}
    with pytest.raises(ValueError, match="signature"):
        session.dev.decrypt_validate(body)


def _server(tmp_path) -> LanServer:
    os.environ["QUEUE_FILE"] = str(tmp_path / "pending.json")
    os.environ["LANIP_KEY"] = SECRET
    relay = Relay()
    try:
        server = relay.lan
        assert isinstance(server, LanServer)
        server.session = Session(SECRET.encode(), EXCHANGE)
        return server
    finally:
        os.environ.pop("LANIP_KEY", None)
        os.environ.pop("QUEUE_FILE", None)


async def test_commands_delivers_the_queued_write_as_an_encrypted_property(tmp_path):
    server = _server(tmp_path)
    server.relay.queue_write("setpoint", 140)
    request = make_mocked_request("GET", "/local_lan/commands.json", app=web.Application())
    response = await server._commands(request)
    body = json.loads(response.body)
    _, rx = module_directions(SECRET.encode(), EXCHANGE,
                              server.session.random_2, server.session.time_2)
    text = rx["cipher"].decrypt(base64.b64decode(body["enc"])).rstrip(bytes([0]))
    command = json.loads(text.decode())
    prop = command["data"]["properties"][0]["property"]
    assert prop["name"] == "setBlockParameter"
    assert prop["base_type"] == "string"
    assert prop["value"] == "BD 40 08 0B 06 3C 00 6E"     # 140 F, golden frame
    assert server.writes_delivered == 1
    assert server.relay.writes_delivered == 1

    # Next poll: nothing queued, and the empty command still verifies.
    response = await server._commands(request)
    command = json.loads(rx["cipher"].decrypt(
        base64.b64decode(json.loads(response.body)["enc"])).rstrip(bytes([0])).decode())
    assert command["data"] == {}


async def test_ack_queues_a_read_only_mcu_telemetry_request(tmp_path):
    """A write ack is followed by GET property.json, not a repeated write.

    libada routes that GET to ada_prop_mgr_get(), so the value comes from the
    tank controller and returns through the ordinary datapoint endpoint.
    """
    server = _server(tmp_path)
    await server.relay.ingest_lan_update(
        {"data": {"id": "abc", "ack_status": 200}})
    assert server.refresh_pending is True
    request = make_mocked_request("GET", "/local_lan/commands.json", app=web.Application())
    response = await server._commands(request)
    _, rx = module_directions(SECRET.encode(), EXCHANGE,
                              server.session.random_2, server.session.time_2)
    command = json.loads(rx["cipher"].decrypt(
        base64.b64decode(json.loads(response.body)["enc"])).rstrip(bytes([0])).decode())
    cmd = command["data"]["cmds"][0]["cmd"]
    assert cmd["method"] == "GET"
    assert cmd["resource"] == "property.json?name=superBlockDyn1"
    assert cmd["uri"] == "/local_lan/property/datapoint.json"
    assert "properties" not in command["data"]
    assert server.refresh_pending is False
    assert server.telemetry_requests == 1

    # No response means no automatic retry.
    response = await server._commands(request)
    command = json.loads(rx["cipher"].decrypt(
        base64.b64decode(json.loads(response.body)["enc"])).rstrip(bytes([0])).decode())
    assert command["data"] == {}


async def test_lan_telemetry_updates_state_and_confirms_the_write(tmp_path):
    server = _server(tmp_path)
    # A real captured superBlockDyn1 report (setpoint 120 F, heat pump).
    fixture = json.loads((Path(__file__).parent / "fixtures"
                          / "telemetry_heat_pump.json").read_text())
    value = ",".join(fixture["blocks"])
    server.relay.queue_write("setpoint", 120)
    update = {"seq_no": 2, "data": {"name": "superBlockDyn1", "value": value}}
    tx, _ = module_directions(SECRET.encode(), EXCHANGE,
                              server.session.random_2, server.session.time_2)
    text = json.dumps(update).encode()
    body = {"enc": base64.b64encode(tx["cipher"].encrypt(
        text.ljust((len(text) + 15) // 16 * 16, bytes([0])))).decode(),
        "sign": base64.b64encode(hmac.digest(tx["sign"], text, "sha256")).decode()}

    class _Request:                       # the handler only needs .json()
        async def json(self):
            return body

    response = await server._datapoint(_Request())
    assert response.status == 200
    assert server.relay.state["setpoint"] == 120.0
    assert server.relay.state["mode"] == "heat_pump"
    assert server.relay.last_telemetry > 0
    assert "setpoint" not in server.relay.pending        # confirmed by telemetry
    assert server.relay.last_write["outcome"] == "confirmed"
    assert server.updates == 1


def test_key_is_persisted_not_logged(tmp_path):
    os.environ["QUEUE_FILE"] = str(tmp_path / "pending.json")
    os.environ.pop("LANIP_KEY", None)
    try:
        first = Relay().lan
        assert first.key and len(first.key) <= 32
        mode = (tmp_path / "lanip.json").stat().st_mode & 0o777
        assert mode == 0o600
        second = Relay().lan
        assert second.key == first.key           # a restart keeps the same key
        assert second.key_id == first.key_id
    finally:
        os.environ.pop("QUEUE_FILE", None)


def test_lan_json_matches_libadas_expected_shape(tmp_path):
    server = _server(tmp_path)
    body = server.lan_json()["lanip"]
    assert body["status"] == "enable"             # exact string, per the C parser
    assert body["lanip_key"] == SECRET
    assert body["lanip_key_id"] == 1
    assert body["keep_alive"] == 30 and body["auto_sync"] == 1
    assert server.key_served_at > 0


def test_module_ip_learned_from_the_dsn_reply(tmp_path):
    server = _server(tmp_path)
    assert server.module_ip is None
    server.learn_module_ip("<device><lan-ip>10.0.1.187</lan-ip></device>")
    assert server.module_ip == "10.0.1.187"
    body = server._registration_body()["local_reg"]
    assert body["port"] == 8888 and body["uri"] == "/local_lan" and body["notify"] == 1
    assert body["ip"]                          # the address the module calls back on


async def test_lan_delivery_stops_after_three_unconfirmed_attempts(tmp_path):
    """Live finding: an acked write with no confirming telemetry was re-sent
    every 60 s indefinitely. Three attempts, then the LAN path stops."""
    server = _server(tmp_path)
    server.relay.queue_write("setpoint", 135)
    write = server.relay.pending["setpoint"]
    for _ in range(3):
        write.delivered = 0.0                 # pretend the retry window passed
        assert server.relay.take_pending_for_lan() is write
    write.delivered = 0.0
    assert server.relay.take_pending_for_lan() is None
    assert write.attempts == 3


def test_module_address_survives_a_relay_restart(tmp_path, monkeypatch):
    """Live finding: after an add-on update the relay sat dark until the module
    re-registered, because <lan-ip> only arrives in the DSN reply."""
    monkeypatch.setenv("QUEUE_FILE", str(tmp_path / "pending.json"))
    monkeypatch.delenv("LANIP_KEY", raising=False)
    first = Relay().lan
    first.learn_module_ip("<device><lan-ip>10.0.1.187</lan-ip></device>")
    second = Relay().lan
    assert second.module_ip == "10.0.1.187"
    assert second.key == first.key
    assert (tmp_path / "lanip.json").stat().st_mode & 0o777 == 0o600


async def test_repeated_exchanges_without_updates_flag_a_foreign_key(tmp_path):
    server = _server(tmp_path)
    for _ in range(2):
        await server._key_exchange(_exchange_request())
    assert server.needs_key_push is False
    await server._key_exchange(_exchange_request())
    assert server.needs_key_push is True


def _exchange_request():
    class _Request:
        async def json(self):
            return {"key_exchange": EXCHANGE}
    return _Request()
