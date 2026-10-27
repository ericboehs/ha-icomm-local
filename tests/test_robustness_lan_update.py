"""LAN updates of the wrong shape: a 400, never a 500, never proof of the key."""

import copy

import pytest
from icomm_lan import Session
from icomm_relay import Relay

FAKE_KEY = "f" * 28                      # a dummy test key, not a real one
EXCHANGE = {"ver": 1, "proto": 1, "random_1": "RpAKxT1NIl6t8XgK",
            "time_1": 185705179279283, "key_id": 1}


class _Json:
    def __init__(self, body) -> None:
        self.body = body

    async def json(self):
        return self.body


@pytest.fixture
def lan(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setenv("LANIP_KEY", FAKE_KEY)
    server = Relay().lan
    server.session = Session(FAKE_KEY.encode(), EXCHANGE)
    return server


def _authentic(lan, payload) -> dict:
    """Encrypted and signed as the module would, from the same session keys."""
    return copy.deepcopy(lan.session.dev).encrypt_sign(payload)


@pytest.mark.parametrize("payload", [
    [1, 2],
    "text",
    {"seq_no": 1, "data": [1]},
    {"seq_no": 1, "data": "x"},
    {"seq_no": 1, "data": {"name": ["superBlockDyn1"], "value": "00"}},
    {"seq_no": 1, "data": {"name": "superBlockDyn1", "value": ["00"]}},
    {"seq_no": 1, "data": {"name": "faultCode", "value": {"a": 1}}},
])
async def test_a_malformed_authentic_update_is_refused(lan, payload, caplog):
    response = await lan._datapoint(_Json(_authentic(lan, payload)))
    assert response.status == 400
    assert lan.session_verified is False and lan.updates == 0
    assert "malformed update" in caplog.text


async def test_a_well_formed_update_still_verifies_the_session(lan):
    payload = {"seq_no": 1, "data": {"name": "faultCode", "value": 0}}
    response = await lan._datapoint(_Json(_authentic(lan, payload)))
    assert response.status == 200
    assert lan.session_verified is True
    assert lan.relay.device.facts["fault_code"] == "0"


@pytest.mark.parametrize("update", [
    [1], None, {"data": [1]}, {"data": {"name": 5, "value": "x"}},
    {"data": {"name": "superBlockDyn1", "value": 7}},
    {"data": {"name": "active_image_version", "value": None}},
])
async def test_ingest_lan_update_tolerates_any_shape(lan, update):
    await lan.relay.ingest_lan_update(update)      # no exception
    assert "controller_firmware" not in lan.relay.device.facts


# -- #9: a suspected CBC desync is detected, not acted on ------------------------
async def test_deliveries_with_nothing_back_are_flagged_once(lan, caplog):
    session, lan.registered = lan.session, True
    for _ in range(5):
        lan.request_telemetry()
        await lan._commands(None)
    assert lan.unanswered_deliveries == 5
    assert lan.status()["suspected_desyncs"] == 1
    assert caplog.text.count("fallen out of step") == 1
    assert lan.session is session and lan.registered is True       # no reconnect


async def test_an_authentic_update_or_new_session_clears_the_count(lan):
    lan.request_telemetry()
    await lan._commands(None)
    await lan._commands(None)                       # nothing to send: not counted
    assert lan.unanswered_deliveries == 1
    payload = {"seq_no": 1, "data": {"name": "faultCode", "value": "0"}}
    assert (await lan._datapoint(_Json(_authentic(lan, payload)))).status == 200
    assert lan.unanswered_deliveries == 0
    lan.request_telemetry()
    await lan._commands(None)
    await lan._key_exchange(_Json({"key_exchange": EXCHANGE}))
    assert lan.unanswered_deliveries == 0


# -- #15: the advertised callback address follows the module --------------------
class _RouteSocket:
    """socket.socket stand-in: our address toward a module, per its subnet."""

    routes = {"10.0.1.187": "10.0.1.113", "10.0.20.187": "10.0.20.113"}

    def __init__(self, *args) -> None:
        self.peer = None

    def connect(self, addr) -> None:
        self.peer = addr[0]

    def getsockname(self):
        return (self.routes[self.peer], 5555)

    def close(self) -> None:
        pass


def test_advertise_ip_is_recomputed_when_the_module_moves(lan, monkeypatch):
    monkeypatch.setattr("icomm_lan.socket.socket", _RouteSocket)
    lan.module_ip = "10.0.1.187"
    assert lan._registration_body()["local_reg"]["ip"] == "10.0.1.113"
    lan.module_ip = "10.0.20.187"                      # moved to its own VLAN
    assert lan._registration_body()["local_reg"]["ip"] == "10.0.20.113"
    assert lan._registration_body()["local_reg"]["ip"] == "10.0.20.113"


def test_a_configured_callback_address_is_never_recomputed(lan, monkeypatch):
    monkeypatch.setattr("icomm_lan.socket.socket", _RouteSocket)
    lan.advertise_ip = "10.9.9.9"                       # as the add-on option sets it
    for module in ("10.0.1.187", "10.0.20.187"):
        lan.module_ip = module
        assert lan._registration_body()["local_reg"]["ip"] == "10.9.9.9"
