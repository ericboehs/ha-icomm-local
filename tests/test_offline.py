"""Ayla unreachable at boot: the relay answers the config fetch itself."""

from __future__ import annotations

import time

from icomm_offline import OfflineConfig
from test_device_facts import REAL_DSN
from test_proxy import FakeClient, _relay, _request

PATH = "/dsns/AC000W019949045.xml"
SIGNED = {"x-Ayla-client-auth": "SIG"}


class DeadClient:
    """Every upstream request fails, the way an outage or a bad hostname does."""

    def __init__(self) -> None:
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        raise OSError("Name or service not known")


def _offline_relay(tmp_path, monkeypatch, **env):
    (tmp_path / "dsn-original.xml").write_text(REAL_DSN.replace(
        "<registered>0</registered>", "<registered>0</registered>\n"
        "  <unix-time>1700000000</unix-time>\n  <lan-enabled>false</lan-enabled>"))
    relay = _relay(tmp_path, monkeypatch, DEVICE_DSN="AC000W019949045", **env)
    relay.client = DeadClient()
    return relay


async def test_the_boot_handshake_is_played_like_ayla(tmp_path, monkeypatch):
    relay = _offline_relay(tmp_path, monkeypatch)
    first = await relay._route(_request("GET", PATH, headers=SIGNED))
    assert first.status == 401
    assert abs(int(first.headers["x-ayla-server-time"]) - time.time()) < 5
    assert "Authorization Required" in first.text
    second = await relay._route(_request("GET", PATH, headers=SIGNED))
    assert second.status == 200
    assert len(second.headers["x-ayla-auth-key"]) == 36
    body = second.body.decode()
    assert "<unix-time>1700000000</unix-time>" not in body          # fresh time
    assert "<lan-enabled>true</lan-enabled>" in body                # LAN mode armed
    state = relay.offline.status()
    assert (state["challenges"], state["served"], state["cached"]) == (1, 1, True)
    # With a key (a later re-fetch), no new challenge and no new key.
    keyed = await relay._route(_request("GET", PATH, headers={"x-Ayla-auth-key": "k"}))
    assert keyed.status == 200 and "x-ayla-auth-key" not in keyed.headers


async def test_ayla_answering_5xx_counts_as_out(tmp_path, monkeypatch):
    relay = _offline_relay(tmp_path, monkeypatch)
    relay.client = FakeClient(503, b"<html>bad gateway</html>")
    relay.offline.challenged_at = time.time()                       # past the 401
    reply = await relay._route(_request("GET", PATH, headers=SIGNED))
    assert reply.status == 200 and b"<oem-model>" in reply.body
    # Not the config fetch: Ayla's 503 goes through as before.
    other = await relay._route(_request("GET", "/devices/1/whatever.json"))
    assert other.status == 503


async def test_what_it_will_not_answer(tmp_path, monkeypatch):
    relay = _offline_relay(tmp_path, monkeypatch)
    other_dsn = await relay._route(_request("GET", "/dsns/AC000W000000001.xml", headers=SIGNED))
    assert other_dsn.status == 503                                  # not our heater
    stranger = _request("GET", PATH, headers=SIGNED, trusted=False)
    assert relay._offline_config(stranger, None) is None            # pinning needs Ayla
    (tmp_path / "dsn-original.xml").unlink()
    assert (await relay._route(_request("GET", PATH, headers=SIGNED))).status == 503
    off = _offline_relay(tmp_path, monkeypatch, OFFLINE_FALLBACK="0")
    assert (await off._route(_request("GET", PATH, headers=SIGNED))).status == 503
    assert off.offline.status()["enabled"] is False


def test_an_unreadable_cache_is_not_served(tmp_path):
    cached = tmp_path / "dsn-original.xml"
    cached.write_bytes(b"\xff\xfe not utf-8")
    from aiohttp.test_utils import make_mocked_request
    request = make_mocked_request("GET", "/dsns/X.xml")
    assert OfflineConfig(cached).answer(request, "X", None) is None
