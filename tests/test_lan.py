"""LAN mode: the local doorbell, tested against an independent module side.

The protocol comes from Ayla's libada (client.c/lan.c) and deiger/AirCon. These
tests build the *module's* side straight from the libada formulas rather than
from relay/icomm_lan.py, so a mistake in the server cannot validate itself.
"""

import base64
import hmac
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from Crypto.Cipher import AES
from icomm_lan import LanServer, Session
from icomm_relay import Relay

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


def _server(tmp_path, **env) -> LanServer:
    """A relay's LAN server with a verified session. The environment (`env`
    included) is set only while the relay is built: it reads its settings then."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("QUEUE_FILE", str(tmp_path / "pending.json"))
        mp.setenv("LANIP_KEY", SECRET)
        for key, value in env.items():
            mp.setenv(key, value)
        relay = Relay()
    server = relay.lan
    assert isinstance(server, LanServer)
    server.session = Session(SECRET.encode(), EXCHANGE)
    server.session_verified = True                   # the module has proven our key
    server.last_update = __import__("time").time()   # fresh unless a test says otherwise
    return server


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

    # No response means no automatic retry: the next polls carry only the
    # one-time device reads (firmware, nameplate), and then nothing.
    def next_command():
        return json.loads(rx["cipher"].decrypt(base64.b64decode(json.loads(
            response.body)["enc"])).rstrip(bytes([0])).decode())
    response = await server._commands(request)
    assert next_command()["data"]["cmds"][0]["cmd"]["resource"] == (
        "property.json?name=active_image_version")
    response = await server._commands(request)
    assert next_command()["data"]["cmds"][0]["cmd"]["resource"] == (
        "property.json?name=superBlockStatic1")
    response = await server._commands(request)
    assert next_command()["data"] == {}
    assert server.telemetry_requests == 1


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


def test_key_is_persisted_not_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_FILE", str(tmp_path / "pending.json"))
    monkeypatch.delenv("LANIP_KEY", raising=False)
    first = Relay().lan
    assert first.key and len(first.key) <= 32
    mode = first.key_file.stat().st_mode & 0o777
    assert mode == 0o600
    second = Relay().lan
    assert second.key == first.key           # a restart keeps the same key
    assert second.key_id == first.key_id


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


def test_a_configured_module_ip_beats_ayla_s_stale_copy(tmp_path, monkeypatch):
    """Live, 0.5.0: the heater moved to its own VLAN, and Ayla's DSN reply,
    still listing the old address, pulled the relay back to it."""
    monkeypatch.setenv("MODULE_IP", "10.0.20.187")
    server = _server(tmp_path)
    server.learn_module_ip("<device><lan-ip>10.0.1.187</lan-ip></device>")
    assert server.module_ip == "10.0.20.187"
    server.learn_module_ip("<device><lan-ip>10.0.20.187</lan-ip></device>")
    assert server.module_ip == "10.0.20.187"


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
    assert second.key_file.stat().st_mode & 0o777 == 0o600


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


async def test_write_status_lifecycle_never_confuses_ack_with_confirmation(tmp_path):
    """queued -> delivered -> acknowledged -> confirmed, or not_applied."""
    server = _server(tmp_path)
    relay = server.relay
    relay.state["setpoint"] = 135.0
    relay.queue_write("setpoint", 140)
    write = relay.pending["setpoint"]
    assert write.status == "queued"

    request = make_mocked_request("GET", "/local_lan/commands.json", app=web.Application())
    await server._commands(request)
    assert write.status == "delivered" and write.delivery_path == "lan"

    # An ack for some other id does not count.
    await relay.ingest_lan_update({"data": {"id": "nope", "ack_status": 200}})
    assert write.status == "delivered"
    await relay.ingest_lan_update({"data": {"id": write.command_id, "ack_status": 200}})
    assert write.status == "acknowledged"

    # Fresh telemetry with the old value: still only acknowledged, and visible.
    relay._confirm_pending()
    assert write.status == "acknowledged"
    assert write.reports_after_ack == 1 and write.observed_after_ack == 135.0
    state = json.loads((await relay.state_handler(None)).body)
    assert state["pending"]["setpoint"]["status"] == "acknowledged"

    # The tank reports the new value: confirmed, retired, recorded.
    relay.state["setpoint"] = 140.0
    relay._confirm_pending()
    assert "setpoint" not in relay.pending
    assert relay.last_write["status"] == "confirmed"


def test_three_acked_attempts_with_old_telemetry_read_as_not_applied(tmp_path):
    server = _server(tmp_path)
    relay = server.relay
    relay.state["setpoint"] = 135.0
    write = relay.queue_write("setpoint", 138)
    write.attempts, write.delivered, write.acked = 3, 1.0, 2.0
    relay._confirm_pending()
    assert write.status == "not_applied"


def test_lifecycle_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setenv("LANIP_KEY", SECRET)
    relay = Relay()
    write = relay.queue_write("setpoint", 140)
    relay._mark_delivered(write, "lan")
    write.command_id = "abcd1234"
    relay._record_ack("abcd1234", 200)
    again = Relay()
    again._load_queue()                     # what startup does
    restored = again.pending["setpoint"]
    assert restored.status == "acknowledged"
    assert restored.delivery_path == "lan" and restored.restored


async def test_idle_tank_gets_a_keepalive_read_before_going_stale(tmp_path):
    """Live finding: 13 min with no report made HA mark the entity unavailable
    and drop a write, while the LAN session was healthy all along."""
    import time as _time
    server = _server(tmp_path)
    request = make_mocked_request("GET", "/local_lan/commands.json", app=web.Application())
    _, rx = module_directions(SECRET.encode(), EXCHANGE,
                              server.session.random_2, server.session.time_2)

    def decode(resp):
        return json.loads(rx["cipher"].decrypt(base64.b64decode(
            json.loads(resp.body)["enc"])).rstrip(bytes([0])).decode())["data"]

    server.last_update = _time.time()                 # fresh: no read
    assert decode(await server._commands(request)) == {}
    server.last_update = _time.time() - 400           # stale: one read...
    server.relay.last_telemetry = server.last_update
    assert decode(await server._commands(request))["cmds"][0]["cmd"]["method"] == "GET"
    assert decode(await server._commands(request)) == {}   # ...not every poll
