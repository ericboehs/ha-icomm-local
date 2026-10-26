"""Firmware updates wait for the owner (relay/icomm_firmware.py)."""

from __future__ import annotations

import json
import time

import aiohttp
import pytest
from icomm_firmware import MAX_OFFERS, FirmwareGate
from test_proxy import FakeClient, _relay, _request

URL = "https://ota.example-s3.test/updates/AY001MUS/bc-2.12.0.patch"
OTA = {"ota": {"url": URL, "ver": "bc 2.12.0", "size": 412000,
               "type": "module", "source": "remote"}}
LANIP_CMD = ("<cmd><id>7</id><method>PUT</method><resource>lanip.json</resource>"
             "<uri>/lanip.json</uri><data>{}</data></cmd>")


def _ota_cmd(ota: dict = OTA, data: str | None = None, cmd_id: int = 41) -> str:
    body = json.dumps(ota) if data is None else data
    return (f"<cmd><id>{cmd_id}</id><method>PUT</method><resource>ota.json</resource>"
            f"<uri>/ota.json</uri><data>{body}</data></cmd>")


def _reply(*cmds: str) -> bytes:
    inner = f"<cmds>{''.join(cmds)}</cmds>" if cmds else "<cmds/>"
    return (f"<commands>\n  <properties/>\n  <schedules/>\n  {inner}\n</commands>\n").encode()


def _gate(tmp_path, policy="hold") -> FirmwareGate:
    return FirmwareGate(policy, tmp_path / "firmware.json")


# -- holding -----------------------------------------------------------------

def test_an_update_is_held_and_recorded(tmp_path):
    gate = _gate(tmp_path)
    out = gate.filter(_reply(_ota_cmd())).decode()
    assert "ota.json" not in out and "<cmds/>" in out          # the module sees none
    held = gate.status()["held"]
    assert [h["key"] for h in held] == ["module:bc 2.12.0"]
    assert held[0]["url_host"] == "ota.example-s3.test" and held[0]["size"] == 412000
    assert held[0]["source"] == "remote" and held[0]["times"] == 1


def test_other_commands_in_the_same_reply_survive(tmp_path):
    gate = _gate(tmp_path)
    out = gate.filter(_reply(LANIP_CMD, _ota_cmd())).decode()
    assert LANIP_CMD in out and "ota.json" not in out


def test_a_reply_without_firmware_is_untouched(tmp_path):
    gate = _gate(tmp_path)
    assert gate.filter(_reply()) is None
    assert gate.filter(_reply(LANIP_CMD)) is None
    assert gate.status()["held"] == []


@pytest.mark.parametrize("data", [
    json.dumps(OTA).replace('"', "&quot;"),                   # entity-escaped
    f"<![CDATA[{json.dumps(OTA)}]]>",                          # CDATA
])
def test_escaped_command_bodies_are_read(tmp_path, data):
    gate = _gate(tmp_path)
    gate.filter(_reply(_ota_cmd(data=data)))
    assert gate.status()["held"][0]["version"] == "bc 2.12.0"


@pytest.mark.parametrize("data", ["not json", "[1, 2]", '{"ota": "x"}'])
def test_an_unreadable_body_is_still_held(tmp_path, data):
    """Unknown contents are no reason to let it through."""
    gate = _gate(tmp_path)
    out = gate.filter(_reply(_ota_cmd(data=data))).decode()
    assert "ota.json" not in out
    assert gate.status()["held"][0]["key"] == "module:?"


def test_a_command_without_data_is_held(tmp_path):
    gate = _gate(tmp_path)
    cmd = "<cmd><id>3</id><method>PUT</method><resource>/lanota.json</resource></cmd>"
    assert b"lanota" not in gate.filter(_reply(cmd))


def test_a_controller_update_is_named_as_such(tmp_path):
    gate = _gate(tmp_path)
    ota = {"ota": dict(OTA["ota"], type="host_mcu", ver="2.15")}
    gate.filter(_reply(_ota_cmd(ota)))
    assert gate.status()["held"][0]["key"] == "host_mcu:2.15"


def test_repeat_offers_count_up(tmp_path):
    gate = _gate(tmp_path)
    for _ in range(3):
        gate.filter(_reply(_ota_cmd()))
    assert gate.status()["held"][0]["times"] == 3


# -- allowing ----------------------------------------------------------------

def test_an_allowed_update_passes_and_its_download_is_forwarded(tmp_path):
    gate = _gate(tmp_path)
    gate.filter(_reply(_ota_cmd()))
    assert gate.allow("module:bc 2.12.0")
    assert gate.filter(_reply(_ota_cmd())) is None             # passes untouched
    assert gate.status()["held"] == [] and gate.status()["allowed"] == ["module:bc 2.12.0"]
    assert gate.download_target("ota.example-s3.test:443", "/updates/AY001MUS/bc-2.12.0.patch") \
        == URL
    assert gate.download_target("OTA.EXAMPLE-S3.TEST", "/updates/AY001MUS/bc-2.12.0.patch") \
        == URL


def test_only_the_allowed_url_is_forwarded(tmp_path):
    gate = _gate(tmp_path)
    gate.filter(_reply(_ota_cmd()))
    path = "/updates/AY001MUS/bc-2.12.0.patch"
    assert gate.download_target("ota.example-s3.test", path) is None   # not allowed yet
    gate.allow("module:bc 2.12.0")
    assert gate.download_target("ota.example-s3.test", "/other") is None
    assert gate.download_target("evil.test", path) is None
    assert gate.download_target(None, path) is None


def test_allowing_an_unknown_offer_fails(tmp_path):
    assert not _gate(tmp_path).allow("module:9.9")


def test_policy_allow_passes_everything_but_still_records(tmp_path):
    gate = _gate(tmp_path, "allow")
    assert gate.filter(_reply(_ota_cmd())) is None
    status = gate.status()
    assert status["policy"] == "allow" and status["held"] == []
    assert "module:bc 2.12.0" in gate.offers


# -- persistence and bookkeeping ---------------------------------------------

def test_offers_and_decisions_survive_a_restart(tmp_path):
    gate = _gate(tmp_path)
    gate.filter(_reply(_ota_cmd()))
    gate.filter(_reply(_ota_cmd({"ota": dict(OTA["ota"], ver="bc 2.13.0")})))
    gate.allow("module:bc 2.13.0")
    again = _gate(tmp_path)
    assert [h["key"] for h in again.status()["held"]] == ["module:bc 2.12.0"]
    assert again.allowed == {"module:bc 2.13.0"}


@pytest.mark.parametrize("content", ["not json", "[1]", '{"offers": [], "allowed": "x"}'])
def test_an_unreadable_file_starts_empty(tmp_path, content):
    (tmp_path / "firmware.json").write_text(content)
    gate = _gate(tmp_path)
    assert gate.offers == {} and gate.allowed == set()


def test_a_failed_save_does_not_break_the_poll(tmp_path):
    (tmp_path / "firmware.json").mkdir()                      # unwritable as a file
    gate = _gate(tmp_path)
    assert b"ota.json" not in gate.filter(_reply(_ota_cmd()))


def test_stale_offers_are_forgotten_and_the_list_is_bounded(tmp_path, monkeypatch):
    gate = _gate(tmp_path)
    gate.filter(_reply(_ota_cmd()))
    gate.allow("module:bc 2.12.0")
    gate.offers["module:bc 2.12.0"]["last_seen"] = time.time() - 31 * 86400
    gate.filter(_reply(_ota_cmd({"ota": dict(OTA["ota"], ver="new")})))
    assert "module:bc 2.12.0" not in gate.offers and gate.allowed == set()
    for n in range(MAX_OFFERS + 4):
        gate.filter(_reply(_ota_cmd({"ota": dict(OTA["ota"], ver=f"v{n}")})))
    assert len(gate.offers) == MAX_OFFERS
    assert "module:v0" not in gate.offers                     # the oldest went first


def test_agent_version_and_status_reports(tmp_path):
    gate = _gate(tmp_path)
    gate.note_dsn("<dsn><sw-version>bc 2.11.4 05/13/25 03:34:57 ID 0520f59</sw-version></dsn>")
    gate.note_dsn("<dsn/>")                                    # no version: keeps the last
    for n in range(7):
        gate.note_status(f"status={n}")
    status = gate.status()
    assert status["agent_version"].startswith("bc 2.11.4")
    assert [r["query"] for r in status["status_reports"]] == [f"status={n}" for n in range(2, 7)]


# -- wired into the relay ----------------------------------------------------

async def test_the_relay_holds_an_update_in_a_command_poll(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(200, _reply(_ota_cmd()))
    reply = await relay._route(_request("GET", "/devices/29689890/commands.xml"))
    assert b"ota.json" not in reply.body and b"<cmds/>" in reply.body
    body = json.loads((await relay.state_handler(None)).body)
    assert body["firmware"]["held"][0]["key"] == "module:bc 2.12.0"
    assert (tmp_path / "firmware.json").exists()              # beside the write queue


async def test_the_relay_still_delivers_a_write_alongside_a_held_update(tmp_path, monkeypatch):
    """Dropping firmware must not cost a queued write, nor the other way round."""
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(200, _reply(_ota_cmd()))
    out = relay._transform_commands(_reply(_ota_cmd()))
    assert out is not None and b"ota.json" not in out


async def test_the_allow_endpoint(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.firmware.filter(_reply(_ota_cmd()))

    def post(body: bytes):
        return relay.firmware_allow_handler(_request("POST", "/firmware/allow", body))

    assert (await post(b"nope")).status == 400
    assert (await post(b'{"other": 1}')).status == 400
    assert (await post(b'{"key": "module:9"}')).status == 404
    reply = await post(json.dumps({"key": "module:bc 2.12.0"}).encode())
    assert reply.status == 200 and relay.firmware.allowed == {"module:bc 2.12.0"}


class DownloadClient(FakeClient):
    def get(self, url, **kw):
        return self.request("GET", url, **kw)


async def test_an_allowed_download_goes_to_its_real_host_verified(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.firmware.filter(_reply(_ota_cmd()))
    relay.firmware.allow("module:bc 2.12.0")
    relay.client = DownloadClient(206, b"\x01\x02", headers={
        "Content-Range": "bytes 0-1/412000", "Content-Type": "application/octet-stream"})
    reply = await relay._route(_request(
        "GET", "/updates/AY001MUS/bc-2.12.0.patch",
        headers={"Host": "ota.example-s3.test", "Range": "bytes=0-1", "Cookie": "x"}))
    call = relay.client.calls[0]
    assert call["url"] == URL and call["ssl"] is True
    assert call["headers"] == {"Range": "bytes=0-1"}           # nothing else forwarded
    assert reply.status == 206 and reply.body == b"\x01\x02"
    assert reply.headers["Content-Range"] == "bytes 0-1/412000"


async def test_a_download_that_fails_is_a_502(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.firmware.filter(_reply(_ota_cmd()))
    relay.firmware.allow("module:bc 2.12.0")
    relay.client = DownloadClient(error=aiohttp.ClientConnectionError("down"))
    reply = await relay._route(_request(
        "GET", "/updates/AY001MUS/bc-2.12.0.patch", headers={"Host": "ota.example-s3.test"}))
    assert reply.status == 502


async def test_a_download_not_allowed_goes_to_ayla_as_before(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.firmware.filter(_reply(_ota_cmd()))
    relay.client = FakeClient(404, b"")
    await relay._route(_request(
        "GET", "/updates/AY001MUS/bc-2.12.0.patch", headers={"Host": "ota.example-s3.test"}))
    assert str(relay.client.calls[0]["url"]).startswith("https://ayla.test/")


async def test_the_module_reporting_on_an_update_is_kept(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.client = FakeClient(200, b"")
    await relay._route(_request("PUT", "/devices/29689890/ota_status.json?status=1"))
    assert relay.firmware.status_reports[0]["query"] == "status=1"
    assert relay.client.calls                                  # and Ayla still hears it


async def test_the_dsn_reply_tells_the_agent_version(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay._rewrite_dsn(b"<dsn><sw-version>bc 2.11.4</sw-version></dsn>")
    assert relay.firmware.agent_version == "bc 2.11.4"



def test_the_allow_log_counts_rather_than_echoes_the_key(tmp_path, caplog):
    """CodeQL py/log-injection: the key arrives in the owner's request."""
    gate = _gate(tmp_path)
    gate.filter(_reply(_ota_cmd()))
    caplog.clear()                     # the offer itself is logged; not this test
    with caplog.at_level("WARNING"):
        assert gate.allow("module:bc 2.12.0")
    assert "(1 allowed)" in caplog.text and "bc 2.12.0" not in caplog.text
