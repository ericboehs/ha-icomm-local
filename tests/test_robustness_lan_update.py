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
