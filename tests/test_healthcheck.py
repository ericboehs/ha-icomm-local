"""relay/healthcheck.py: the Docker HEALTHCHECK the Supervisor watchdog acts on."""

from __future__ import annotations

import asyncio
import json

import healthcheck
import pytest
from aiohttp import web
from test_lan import SECRET

from relay.icomm_relay import Relay


async def _serve(app: web.Application) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


async def test_a_running_relay_is_healthy(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setenv("LANIP_KEY", SECRET)
    app = web.Application()
    app.router.add_get("/health", Relay().health_handler)
    runner, port = await _serve(app)
    try:
        assert await asyncio.to_thread(healthcheck.healthy, port)
    finally:
        await runner.cleanup()


async def test_a_hung_relay_is_not(tmp_path):
    async def stuck(request):
        await asyncio.sleep(1)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/health", stuck)
    runner, port = await _serve(app)
    try:
        assert not await asyncio.to_thread(healthcheck.healthy, port, 0.2)
    finally:
        await runner.cleanup()


async def test_a_wrong_answer_is_not(tmp_path):
    async def not_ok(request):
        return web.json_response({"ok": False})

    app = web.Application()
    app.router.add_get("/health", not_ok)
    runner, port = await _serve(app)
    try:
        assert not await asyncio.to_thread(healthcheck.healthy, port)
    finally:
        await runner.cleanup()


def test_nothing_listening_is_not_healthy():
    assert not healthcheck.healthy(1, 0.5)


@pytest.mark.parametrize(("env", "options", "port"), [
    (None, None, 8124),                              # no options file: the default
    (None, {"state_port": 9124}, 9124),              # the add-on option
    (None, {"state_port": None}, 8124),
    (None, "not json", 8124),
    (None, ["a", "list"], 8124),
    ("7124", {"state_port": 9124}, 7124),            # env wins, as in run.sh
])
def test_state_port_follows_run_sh(tmp_path, monkeypatch, env, options, port):
    if env:
        monkeypatch.setenv("STATE_PORT", env)
    else:
        monkeypatch.delenv("STATE_PORT", raising=False)
    path = tmp_path / "options.json"
    if options is not None:
        path.write_text(options if isinstance(options, str) else json.dumps(options))
    assert healthcheck.state_port(str(path)) == port


def test_main_exit_codes(monkeypatch):
    monkeypatch.setattr(healthcheck, "state_port", lambda: 1)
    monkeypatch.setattr(healthcheck, "healthy", lambda port: True)
    assert healthcheck.main() == 0
    monkeypatch.setattr(healthcheck, "healthy", lambda port: False)
    assert healthcheck.main() == 1
