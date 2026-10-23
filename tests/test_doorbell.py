"""The cloud doorbell: off without credentials, debounced, never fatal."""

from __future__ import annotations

import asyncio

import icomm_doorbell
from icomm_doorbell import Doorbell


def test_disabled_without_credentials() -> None:
    bell = Doorbell("", "")
    assert not bell.enabled
    bell.ring("setpoint", 140)          # no running loop needed: returns before scheduling
    assert bell.status() == {"enabled": False, "rings": 0, "last": None}


def test_debounce_sends_only_the_last_value(monkeypatch) -> None:
    """A slider fires a burst; the cloud should see one call with the final value."""
    monkeypatch.setattr(icomm_doorbell, "DEBOUNCE_S", 0.01)
    sent = []

    async def fake_send(self, kind, value):
        sent.append((kind, value))

    monkeypatch.setattr(Doorbell, "_send", fake_send)

    async def go():
        bell = Doorbell("a@b", "pw")
        for value in (141, 143, 145):
            bell.ring("setpoint", value)
        await asyncio.sleep(0.05)
        return bell

    bell = asyncio.run(go())
    assert sent == [("setpoint", 145)]
    assert bell.rings == 1 and bell.last["ok"] is True


def test_independent_kinds_both_ring(monkeypatch) -> None:
    monkeypatch.setattr(icomm_doorbell, "DEBOUNCE_S", 0.01)
    sent = []

    async def fake_send(self, kind, value):
        sent.append(kind)

    monkeypatch.setattr(Doorbell, "_send", fake_send)

    async def go():
        bell = Doorbell("a@b", "pw")
        bell.ring("setpoint", 140)
        bell.ring("mode", "electric")
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert sorted(sent) == ["mode", "setpoint"]


def test_cloud_failure_is_reported_not_raised(monkeypatch) -> None:
    """A.O. Smith's API is flaky; a failed ring must not take the relay down."""
    monkeypatch.setattr(icomm_doorbell, "DEBOUNCE_S", 0.01)

    async def boom(self, kind, value):
        raise RuntimeError("502 from r2.wh8.co")

    monkeypatch.setattr(Doorbell, "_send", boom)

    async def go():
        bell = Doorbell("a@b", "pw")
        bell.ring("setpoint", 140)
        await asyncio.sleep(0.05)
        return bell

    bell = asyncio.run(go())
    assert bell.rings == 0
    assert bell.last["ok"] is False and "502" in bell.last["error"]
