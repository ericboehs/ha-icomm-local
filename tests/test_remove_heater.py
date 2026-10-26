"""DELETE /heaters/<dsn>: forget a heater that's gone or replaced (#18).

Its files go -- for the primary, the PRIMARY marker and the single-heater
files too, or a restart would rebuild it -- and the relay starts over from
what's left on disk.
"""

from __future__ import annotations

import asyncio
import json
import time

from aiohttp.test_utils import make_mocked_request
from icomm_heater import forget, known_dsn
from test_hub import DSN_A, DSN_B, A, _hub, _module, _two_heaters

LEGACY = ("lanip.json", "device.json", "dsn-original.xml", "dsn-rewritten.xml", "pending.json")


def _delete(dsn: str, query: str = ""):
    request = make_mocked_request("DELETE", f"/heaters/{dsn}{query}")
    request.match_info.update({"dsn": dsn})
    return request


async def _remove(hub, dsn: str, query: str | None = None):
    response = await hub.remove_handler(_delete(dsn, f"?confirm={dsn}" if query is None else query))
    return response.status, json.loads(response.body)


def _legacy(tmp_path) -> None:
    """What a pre-0.6 install left at the top of /data (kept for downgrades)."""
    for name in LEGACY:
        (tmp_path / name).write_text(json.dumps({"module_dsn": DSN_A}))


async def test_removing_the_second_heater(tmp_path, monkeypatch):
    hub = await _two_heaters(tmp_path, monkeypatch)
    status, body = await _remove(hub, DSN_B)
    assert status == 200 and body == {"removed": DSN_B, "primary": False,
                                      "files": [f"heaters/{DSN_B}/"], "restarting": True}
    assert not (tmp_path / "heaters" / DSN_B).exists()
    assert (tmp_path / "heaters" / DSN_A / "lanip.json").is_file()        # A untouched
    again = _hub(tmp_path, monkeypatch)                                   # the restart
    assert again.primary.heater.dsn == DSN_A and not again.secondaries
    # B, still steered here, is new again: added on Ayla's next 200, its own key.
    await _module(again, "10.0.20.188", "GET", f"/dsns/{DSN_B}.xml")
    assert list(again.secondaries) == [DSN_B]


async def test_removing_the_primary_promotes_the_other(tmp_path, monkeypatch):
    hub = await _two_heaters(tmp_path, monkeypatch)
    _legacy(tmp_path)
    status, body = await _remove(hub, DSN_A)
    assert status == 200 and body["primary"] is True
    assert set(body["files"]) == {f"heaters/{DSN_A}/", "heaters/PRIMARY", *LEGACY}
    assert known_dsn(tmp_path) == DSN_B
    again = _hub(tmp_path, monkeypatch)
    assert again.primary.heater.dsn == DSN_B and not again.secondaries
    assert not (tmp_path / "heaters" / DSN_A).exists()                    # not rebuilt


async def test_removing_the_only_heater_leaves_a_fresh_relay(tmp_path, monkeypatch):
    """The single-heater replacement: the next heater to boot is the primary."""
    hub = _hub(tmp_path, monkeypatch)
    await _module(hub, A, "GET", f"/dsns/{DSN_A}.xml")
    _legacy(tmp_path)
    assert (await _remove(hub, DSN_A))[0] == 200
    assert known_dsn(tmp_path) is None
    again = _hub(tmp_path, monkeypatch)
    assert again.primary.heater.dsn is None
    await _module(again, "10.0.20.188", "GET", f"/dsns/{DSN_B}.xml")     # the new heater
    assert again.primary.heater.dsn == DSN_B


async def test_the_guards(tmp_path, monkeypatch):
    hub = await _two_heaters(tmp_path, monkeypatch)
    assert (await _remove(hub, "AC000W000000001"))[0] == 404
    assert (await _remove(hub, DSN_B, ""))[0] == 400                      # no confirm
    assert (await _remove(hub, DSN_B, f"?confirm={DSN_A}"))[0] == 400     # the wrong one
    hub.secondaries[DSN_B].last_telemetry = time.time()                   # still reporting
    status, body = await _remove(hub, DSN_B)
    assert status == 409 and "still reporting" in body["error"]
    assert (tmp_path / "heaters" / DSN_B).is_dir() and not hub.removing
    assert (await _remove(hub, DSN_B, f"?confirm={DSN_B.lower()}&force=1"))[0] == 200


async def test_the_primary_named_by_an_option_is_kept(tmp_path, monkeypatch):
    hub = await _two_heaters(tmp_path, monkeypatch, DEVICE_DSN=DSN_A)
    status, body = await _remove(hub, DSN_A)
    assert status == 409 and "device_dsn" in body["error"]
    hub = await _two_heaters(tmp_path / "x", monkeypatch, DEVICE_DSN="", MODULE_IP=A)
    assert (await _remove(hub, DSN_A))[0] == 409


def test_forget_stays_inside_heaters(tmp_path):
    (tmp_path / "heaters").mkdir()
    (tmp_path / "keep.json").write_text("{}")
    assert forget(tmp_path, "../keep", primary=False) == []
    assert forget(tmp_path, "PRIMARY", primary=True) == []                # reserved: not a DSN
    assert forget(tmp_path, DSN_B, primary=False) == []                   # nothing there
    assert (tmp_path / "keep.json").exists()


def test_forget_checks_the_path_even_for_a_clean_dsn(tmp_path, monkeypatch):
    import icomm_heater
    monkeypatch.setattr(icomm_heater, "clean_dsn", lambda value: "../escape")
    assert forget(tmp_path, DSN_B, primary=True) == []


async def test_the_relay_restarts_and_deletes_again_after_stopping(tmp_path, monkeypatch):
    """run() returns True after a removal; a file an engine wrote between the
    delete and the stop is deleted too. main() starts a new hub."""
    from aiohttp import web
    from test_proxy import FakeSite
    hub = await _two_heaters(tmp_path, monkeypatch, STATE_PORT="18127",
                             LISTEN_PORT="18447", LAN_PORT="18891")
    FakeSite.started = []
    monkeypatch.setattr(web, "TCPSite", FakeSite)
    hub.primary.tls_context = object

    async def no_lan(listen=True):
        return None

    for engine in hub.engines():
        engine.lan.start = no_lan
    task = asyncio.create_task(hub.run())
    for _ in range(400):
        await asyncio.sleep(0)
        if hub._stop is not None and len(FakeSite.started) == 3:
            break
    late = tmp_path / "heaters" / DSN_B / "device.json"

    real_stop = hub.secondaries[DSN_B].lan.stop

    async def stop_and_save():                               # a save racing the stop
        late.parent.mkdir(parents=True, exist_ok=True)
        late.write_text("{}")
        await real_stop()

    hub.secondaries[DSN_B].lan.stop = stop_and_save
    assert (await _remove(hub, DSN_B))[0] == 200
    assert await asyncio.wait_for(task, 5) is True
    assert not (tmp_path / "heaters" / DSN_B).exists()


def test_main_starts_over_after_a_removal(monkeypatch):
    import icomm_hub
    runs = iter([True, True, False])
    made = []

    class FakeHub:
        def __init__(self):
            made.append(self)

        async def run(self):
            return next(runs)

    monkeypatch.setattr(icomm_hub, "Hub", FakeHub)
    icomm_hub.main()
    assert len(made) == 3


async def test_nothing_is_served_between_the_removal_and_the_restart(tmp_path, monkeypatch):
    """Otherwise HA, reloading at once, would read the removed heater from
    /state and re-create it."""
    hub = await _two_heaters(tmp_path, monkeypatch)
    app = hub.state_app()
    calls = []

    async def handler(request):
        calls.append(request.path)
        from aiohttp import web
        return web.json_response({})

    request = make_mocked_request("GET", "/state", app=app)
    assert (await hub._restarting(request, handler)).status == 200
    assert (await _remove(hub, DSN_B))[0] == 200
    response = await hub._restarting(request, handler)
    assert response.status == 503 and response.headers["Retry-After"] == "5"
    assert calls == ["/state"]
