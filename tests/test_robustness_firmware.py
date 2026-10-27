"""FIRMWARE_UPDATES=hold covers every commands.xml reply, trusted or not."""

import json

from icomm_relay import Relay
from test_hub import DSN_A, DSN_B, GATEWAY, ID_A, ID_B, A, Ayla, _hub, _module, _Up
from test_proxy import FakeClient, _request

OTA = ("<cmd><id>41</id><method>PUT</method><resource>ota.json</resource>"
       "<uri>/ota.json</uri><data>" + json.dumps(
           {"ota": {"url": "https://ota.example.test/x.patch", "ver": "bc 9.9.9",
                    "size": 1, "type": "module", "source": "remote"}})
       + "</data></cmd>")
OTA_REPLY = (f"<commands>\n  <properties/>\n  <schedules/>\n  <cmds>{OTA}</cmds>\n"
             "</commands>\n").encode()


def _relay(tmp_path, monkeypatch, **env) -> Relay:
    monkeypatch.setenv("QUEUE_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setenv("LANIP_KEY", "f" * 28)          # a dummy test key
    monkeypatch.setenv("UPSTREAM", "ayla.test")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Relay()


async def test_an_untrusted_poll_does_not_get_the_ota_command(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(200, OTA_REPLY)
    response = await relay._route(_request("GET", "/devices/9/commands.xml", trusted=False))
    assert response.status == 200
    assert b"ota.json" not in response.body and b"<cmds/>" in response.body
    assert [h["key"] for h in relay.firmware.status()["held"]] == ["module:bc 9.9.9"]


async def test_allow_policy_still_passes_it_untrusted(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch, FIRMWARE_UPDATES="allow")
    relay.client = FakeClient(200, OTA_REPLY)
    response = await relay._route(_request("GET", "/devices/9/commands.xml", trusted=False))
    assert b"ota.json" in response.body


async def test_other_untrusted_replies_are_still_verbatim(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(200, OTA_REPLY)
    response = await relay._route(_request("GET", "/devices/9/other.xml", trusted=False))
    assert response.body == OTA_REPLY


class _OtaAyla(Ayla):
    def request(self, method, url, **kw):
        path = str(url.path) if hasattr(url, "path") else str(url)
        if "commands.xml" in path:
            self.calls.append(f"{method} {path}")
            return _Up(200, OTA_REPLY)
        return super().request(method, url, **kw)


async def test_a_foreign_heater_behind_nat_does_not_get_the_ota_command(
        tmp_path, monkeypatch):
    hub = _hub(tmp_path, monkeypatch, MODULE_IP=A, MAX_HEATERS="1")
    hub.client = hub.primary.client = _OtaAyla()
    await _module(hub, GATEWAY, "GET", f"/dsns/{DSN_A}.xml")
    await _module(hub, GATEWAY, "GET", f"/dsns/{DSN_B}.xml")          # refused: full
    assert hub.primary.device_id == ID_A
    poll = await _module(hub, GATEWAY, "GET", f"/devices/{ID_B}/commands.xml")
    assert poll.status == 200 and b"ota.json" not in poll.body
