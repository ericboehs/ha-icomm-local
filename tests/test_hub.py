"""#18 phase 2: one relay, two water heaters, kept apart.

Two simulated modules: A (the primary, 10.0.20.187) and B (10.0.20.188), each
with its own DSN and Ayla device ID. They fetch their config, post telemetry
(through DNS steering, from their own address, and through NAT, from the
gateway), poll for commands, and talk Local Connect with the libada-derived
module side from test_lan -- and every request has to land on its own heater.
"""

from __future__ import annotations

import json
import re

from icomm_hub import Hub
from icomm_peers import TRUSTED
from test_lan import EXCHANGE, module_directions
from test_proxy import _request, _telemetry_body
from test_security import _body, _from, _module_message

GATEWAY = "10.0.20.1"
A, B = "10.0.20.187", "10.0.20.188"
DSN_A, DSN_B = "AC000W019949045", "AC000W077777777"
ID_A, ID_B = "111", "222"
CONFIG = {DSN_A: (ID_A, A), DSN_B: (ID_B, B)}
EMPTY = b"<commands>\n  <properties/>\n  <schedules/>\n  <cmds/>\n</commands>\n"


def _config(dsn: str) -> bytes:
    device_id, ip = CONFIG[dsn]
    return (f"<device><key>{device_id}</key><unix-time>1700000000</unix-time>"
            f"<lan-ip>{ip}</lan-ip><oem-model>iCOMMNextGenHP</oem-model>"
            "<lan-enabled>false</lan-enabled>"
            "<local-access-enabled>false</local-access-enabled></device>").encode()


class _Up:
    def __init__(self, status: int, body: bytes) -> None:
        self.status, self._body, self.headers = status, body, {}

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Ayla:
    """Answers config fetches per DSN (a status per DSN, default 200), polls
    with an empty command list, and everything else with 200."""

    def __init__(self) -> None:
        self.status: dict[str, int] = {}
        self.dead = False
        self.calls: list[str] = []

    def request(self, method, url, **kw):
        path = str(url.path) if hasattr(url, "path") else str(url)
        self.calls.append(f"{method} {path}")
        if self.dead:
            raise OSError("Name or service not known")
        found = re.search(r"/dsns/(\w+)\.xml$", path)
        if found:
            return _Up(self.status.get(found.group(1), 200), _config(found.group(1)))
        return _Up(200, EMPTY if "commands.xml" in path else b"")


def _hub(tmp_path, monkeypatch, **env) -> Hub:
    # No LANIP_KEY: each heater mints and persists its own key and pin, as in
    # the add-on (the dev override persists nothing).
    monkeypatch.setenv("QUEUE_FILE", str(tmp_path / "pending.json"))
    monkeypatch.delenv("LANIP_KEY", raising=False)
    monkeypatch.setenv("UPSTREAM", "ayla.test")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    hub = Hub()
    hub.peers.gateways = {GATEWAY}
    hub.client = hub.primary.client = Ayla()
    for engine in hub.secondaries.values():
        engine.client = hub.client
    return hub


async def _module(hub: Hub, peer: str, method: str, path: str, body: bytes = b"",
                  headers=None):
    """A request as the module-facing listener's middleware would hand it on."""
    request = _request(method, path, body, headers=headers).clone(remote=peer)
    request[TRUSTED] = hub.peers.cloud_allowed(peer)
    return await hub.route(request)


async def _two_heaters(tmp_path, monkeypatch, **env) -> Hub:
    hub = _hub(tmp_path, monkeypatch, **env)
    await _module(hub, A, "GET", f"/dsns/{DSN_A}.xml")              # the first heater
    hub.client.status[DSN_B] = 401
    first = await _module(hub, B, "GET", f"/dsns/{DSN_B}.xml")      # Ayla's usual 401
    assert first.status == 401 and not hub.secondaries
    hub.client.status[DSN_B] = 200
    second = await _module(hub, B, "GET", f"/dsns/{DSN_B}.xml")
    assert b"<lan-enabled>true</lan-enabled>" in second.body        # B's own, rewritten
    return hub


async def test_a_second_heater_is_added_on_aylas_200_and_kept_apart(tmp_path, monkeypatch):
    hub = await _two_heaters(tmp_path, monkeypatch)
    a, b = hub.primary, hub.secondaries[DSN_B]
    assert (a.heater.dsn, a.lan.module_ip, a.device_id) == (DSN_A, A, ID_A)
    assert (b.heater.dsn, b.lan.module_ip, b.device_id) == (DSN_B, B, ID_B)
    assert b.lan.status()["module_ip_source"] == "observed"
    home = tmp_path / "heaters" / DSN_B
    assert json.loads((home / "lanip.json").read_text())["module_dsn"] == DSN_B
    assert (home / "dsn-original.xml").read_bytes() == _config(DSN_B)
    assert b"<key>111</key>" in (tmp_path / "heaters" / DSN_A / "dsn-original.xml").read_bytes()
    assert (tmp_path / "heaters" / "PRIMARY").read_text().strip() == DSN_A
    assert hub.peers.cloud_allowed(B) and hub.peers.lan_allowed(B)

    # Telemetry over DNS steering (from each heater's own address)...
    await _module(hub, A, "POST", f"/devices/{ID_A}/properties/superBlockDyn1/datapoints.xml",
                  _telemetry_body("telemetry_electric.json"))
    await _module(hub, B, "POST", f"/devices/{ID_B}/properties/superBlockDyn1/datapoints.xml",
                  _telemetry_body("telemetry_heat_pump.json"))
    assert (a.state["mode"], b.state["mode"]) == ("electric", "heat_pump")
    # ...and over NAT, where both arrive from the gateway: by Ayla's device ID.
    await _module(hub, GATEWAY, "POST",
                  f"/devices/{ID_B}/properties/superBlockDyn1/datapoints.xml",
                  _telemetry_body("telemetry_hybrid.json"))
    assert (a.state["mode"], b.state["mode"]) == ("electric", "hybrid")
    # Nothing to tell them apart: the primary, as a single relay would.
    await _module(hub, GATEWAY, "GET", "/time.json")
    assert a._dial is not None


async def test_writes_go_to_the_heater_they_name(tmp_path, monkeypatch, blocks_electric):
    hub = await _two_heaters(tmp_path, monkeypatch)
    a, b = hub.primary, hub.secondaries[DSN_B]
    for engine in (a, b):
        engine._ingest_blocks(blocks_electric)
        engine.health = lambda: {"state": "healthy"}
    app = hub.state_app()

    def handler(method: str, path: str):
        for route in app.router.routes():
            info = route.resource.get_info()
            pattern = info.get("formatter") or info.get("path")
            if route.method == method and pattern == path:
                return route.handler
        raise AssertionError(path)

    from test_proxy import _json_post
    ambiguous = await handler("POST", "/write")(_json_post("/write", b'{"setpoint": 140}'))
    assert ambiguous.status == 409 and json.loads(ambiguous.body)["heaters"] == [DSN_A, DSN_B]
    request = _json_post(f"/heaters/{DSN_B}/write", b'{"setpoint": 141}')
    request.match_info.update({"dsn": DSN_B.lower()})
    queued = await handler("POST", "/heaters/{dsn}/write")(request)
    assert queued.status == 200 and "setpoint" in b.pending and not a.pending

    # A's cloud poll carries nothing; B's carries B's write.
    poll_a = await _module(hub, A, "GET", f"/devices/{ID_A}/commands.xml")
    poll_b = await _module(hub, B, "GET", f"/devices/{ID_B}/commands.xml")
    assert b"setBlockParameter" not in poll_a.body and b"setBlockParameter" in poll_b.body

    state = _request("GET", f"/heaters/{DSN_B}/state")
    state.match_info.update({"dsn": DSN_B})
    body = json.loads((await handler("GET", "/heaters/{dsn}/state")(state)).body)
    assert body["heater"]["dsn"] == DSN_B and body["heaters"] == [DSN_A, DSN_B]
    unknown = _request("GET", "/heaters/AC000W000000001/state")
    unknown.match_info.update({"dsn": "AC000W000000001"})
    assert (await handler("GET", "/heaters/{dsn}/state")(unknown)).status == 404
    rows = json.loads((await hub.heaters_handler(None)).body)["heaters"]
    assert [(r["dsn"], r["primary"], r["pending"]) for r in rows] == [
        (DSN_A, True, 0), (DSN_B, False, 1)]
    # With one heater the old endpoints still write (an older integration).
    del hub.secondaries[DSN_B]
    single = await handler("POST", "/write")(_json_post("/write", b'{"setpoint": 139}'))
    assert single.status == 200 and "setpoint" in a.pending


async def test_local_connect_callbacks_find_their_own_heater(tmp_path, monkeypatch):
    hub = await _two_heaters(tmp_path, monkeypatch)
    a, b = hub.primary, hub.secondaries[DSN_B]
    b.queue_write("mode", "heat_pump")
    for peer in (A, B):
        reply = await hub._lan("_key_exchange")(_from(
            peer, "POST", "/local_lan/key_exchange.json",
            payload=_body({"key_exchange": EXCHANGE})))
        assert reply.status == 200
    assert a.lan.session is not None and b.lan.session is not None
    assert a.lan.session is not b.lan.session
    # B's encrypted update reaches B only.
    assert a.lan.key != b.lan.key                                   # a key each
    tx, rx = module_directions(b.lan.key.encode(), EXCHANGE, b.lan.session.random_2,
                               b.lan.session.time_2)
    update = _module_message(tx, {"seq_no": 1, "data": {"name": "faultCode", "value": "0"}})
    await hub._lan("_datapoint")(_from(B, "POST", "/local_lan/property/datapoint.json",
                                       payload=_body(update)))
    assert (a.lan.updates, b.lan.updates) == (0, 1) and b.lan.session_verified
    # B's poll delivers B's write; A's poll doesn't see it.
    import base64
    poll = await hub._lan("_commands")(_from(B, "GET", "/local_lan/commands.json"))
    command = json.loads(rx["cipher"].decrypt(base64.b64decode(json.loads(poll.body)["enc"]))
                         .rstrip(bytes([0])).decode())
    assert command["data"]["properties"][0]["property"]["name"] == "setBlockParameter"
    assert b.lan.writes_delivered == 1 and a.lan.writes_delivered == 0
    # Loopback (or a TRUSTED_PEERS address) falls to the primary, as before.
    assert hub._lan_engine(_from("127.0.0.1", "GET", "/local_lan/commands.json")) is a


async def test_a_restart_finds_both_and_the_primary_stays(tmp_path, monkeypatch):
    await _two_heaters(tmp_path, monkeypatch)
    again = _hub(tmp_path, monkeypatch)
    assert again.primary.heater.dsn == DSN_A and list(again.secondaries) == [DSN_B]
    b = again.secondaries[DSN_B]
    assert (b.lan.module_ip, b.device_id) == (B, ID_B)
    assert again.dsns() == [DSN_A, DSN_B]


async def test_ayla_out_while_the_second_heater_boots(tmp_path, monkeypatch):
    await _two_heaters(tmp_path, monkeypatch)
    hub = _hub(tmp_path, monkeypatch)                                # after a restart
    hub.client.dead = True
    for engine in hub.engines():
        engine.client = hub.client
    signed = {"x-Ayla-client-auth": "SIG"}
    first = await _module(hub, B, "GET", f"/dsns/{DSN_B}.xml", headers=signed)
    second = await _module(hub, B, "GET", f"/dsns/{DSN_B}.xml", headers=signed)
    assert (first.status, second.status) == (401, 200)
    assert b"<key>222</key>" in second.body                        # B's saved reply, not A's
    assert hub.secondaries[DSN_B].offline.served == 1 and hub.primary.offline.served == 0


async def test_max_heaters_one_leaves_the_second_on_ayla(tmp_path, monkeypatch, caplog):
    hub = _hub(tmp_path, monkeypatch, MAX_HEATERS="1")
    await _module(hub, A, "GET", f"/dsns/{DSN_A}.xml")
    for _ in range(2):
        reply = await _module(hub, B, "GET", f"/dsns/{DSN_B}.xml")
        assert reply.body == _config(DSN_B)                          # untouched
    assert not hub.secondaries and not (tmp_path / "heaters" / DSN_B).exists()
    assert caplog.text.count("max_heaters (1) reached") == 1
    assert json.loads((await hub.heaters_handler(None)).body)["refused"] == [DSN_B]


async def test_a_stranger_cannot_create_a_heater(tmp_path, monkeypatch):
    hub = _hub(tmp_path, monkeypatch)
    await _module(hub, A, "GET", f"/dsns/{DSN_A}.xml")
    for peer in ("8.8.8.8", "127.0.0.1"):                           # public; loopback
        await _module(hub, peer, "GET", f"/dsns/{DSN_B}.xml")
    await _module(hub, B, "POST", f"/dsns/{DSN_B}.xml")             # not a fetch
    hub.client.status[DSN_B] = 401
    await _module(hub, B, "GET", f"/dsns/{DSN_B}.xml")              # Ayla said no
    assert not hub.secondaries
    assert hub._pin_candidate(_from(B, "GET", f"/dsns/{DSN_B}.xml"))
    assert not hub._pin_candidate(_from("8.8.8.8", "GET", f"/dsns/{DSN_B}.xml"))
    assert not hub._pinnable("not-an-ip")


async def test_the_second_heater_gets_its_own_key_not_a_copy(tmp_path, monkeypatch):
    (tmp_path / "lanip.json").write_text(json.dumps(
        {"lanip_key": "k" * 28, "lanip_key_id": 1, "module_dsn": DSN_A}))
    monkeypatch.setenv("LANIP_FILE", str(tmp_path / "dev-lanip.json"))    # primary only
    hub = await _two_heaters(tmp_path, monkeypatch)
    b = hub._add("AC000W088888888")
    assert b.lan.key != "k" * 28 and b.lan.key_file.parent.name == "AC000W088888888"
    assert not (tmp_path / "heaters" / "AC000W088888888" / "pending.json").exists()


async def test_a_nat_install_adds_the_second_heater_without_pinning(tmp_path, monkeypatch):
    hub = _hub(tmp_path, monkeypatch, MODULE_IP=A)                   # NAT: configured
    await _module(hub, GATEWAY, "GET", f"/dsns/{DSN_A}.xml")
    await _module(hub, GATEWAY, "GET", f"/dsns/{DSN_B}.xml")
    b = hub.secondaries[DSN_B]
    assert b.lan.module_ip == B and b.lan.status()["module_ip_source"] == "ayla"
    assert hub.primary.lan.module_ip == A                           # configured, untouched


async def test_a_heater_added_while_running_loads_its_queue_and_registers(
        tmp_path, monkeypatch):
    import asyncio

    import icomm_lan
    started = []

    async def fake_start(self, listen=True):
        started.append((self.relay.heater.dsn, listen))

    monkeypatch.setattr(icomm_lan.LanServer, "start", fake_start)
    hub = _hub(tmp_path, monkeypatch)
    hub.running = True
    engine = hub._add(DSN_B)
    await asyncio.sleep(0)
    assert started == [(DSN_B, False)] and engine.queue_restored == 0
    assert hub.by_dsn(None) is None and hub.by_dsn("AC000W000000009") is None


def test_the_primary_marker_is_best_effort(tmp_path, caplog):
    from icomm_heater import Heater
    Heater(tmp_path).mark_primary()                          # no DSN yet: nothing
    assert not (tmp_path / "heaters" / "PRIMARY").exists()
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the data dir should be")
    heater = Heater(tmp_path, DSN_A)
    heater.data_dir = blocker
    heater.mark_primary()
    assert "could not record the primary heater" in caplog.text
