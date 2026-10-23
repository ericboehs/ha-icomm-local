"""The cloud doorbell: make the appliance poll so a queued local write can land.

Why this exists: the module fetches commands only when Ayla notifies it that one
is waiting. Every local trigger was tested on hardware and failed -- a forced
re-dial, a 401 on a GET, a 401 on a telemetry POST (it retries 6 s later and
carries on). So a write queued in the relay can sit for hours.

The doorbell sends the *same* write through A.O. Smith's own API, the call the
mobile app makes. Ayla notifies the module, the module polls through the relay,
and the relay answers with its own queued frame (the value is identical, so
there is no conflict). Confirmation still comes from telemetry, never from the
cloud's "accepted".

Optional: with no credentials configured the relay stays purely local and
writes wait for a natural poll. Each ring logs in, writes and logs out, because
A.O. Smith sessions are single-seat -- a long-lived login would keep knocking
the mobile app (and HA's cloud integration) off.
"""

from __future__ import annotations

import asyncio
import logging
import time

_LOGGER = logging.getLogger("icomm_relay.doorbell")

# A slider produces a burst of writes; only the last one is worth a cloud call.
DEBOUNCE_S = 2.0


class Doorbell:
    def __init__(self, email: str, password: str, dsn: str = "") -> None:
        self.email = email
        self.password = password
        self.dsn = dsn
        self.junction_id: str | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self.rings = 0
        self.last: dict | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.email and self.password)

    def status(self) -> dict:
        return {"enabled": self.enabled, "rings": self.rings, "last": self.last}

    def ring(self, kind: str, value) -> None:
        """Schedule a ring; a newer write of the same kind replaces a pending one."""
        if not self.enabled:
            return
        old = self._tasks.get(kind)
        if old and not old.done():
            old.cancel()
        self._tasks[kind] = asyncio.get_running_loop().create_task(
            self._ring_later(kind, value))

    async def _ring_later(self, kind: str, value) -> None:
        await asyncio.sleep(DEBOUNCE_S)
        started = time.time()
        try:
            await self._send(kind, value)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # the cloud is flaky; report, never crash the relay
            self.last = {"kind": kind, "value": value, "at": started, "ok": False,
                         "error": f"{type(err).__name__}: {err}"[:300]}
            _LOGGER.warning("doorbell failed for %s=%s: %s", kind, value, err)
            return
        self.rings += 1
        self.last = {"kind": kind, "value": value, "at": started, "ok": True,
                     "seconds": round(time.time() - started, 1)}
        _LOGGER.info("doorbell rung: %s=%s (%.1fs)", kind, value, time.time() - started)

    async def _send(self, kind: str, value) -> None:
        # Imported here so the relay runs without the dependency when unconfigured.
        from py_aosmith import AOSmithAPIClient
        from py_aosmith.models import OperationMode

        client = AOSmithAPIClient(self.email, self.password)
        try:
            if self.junction_id is None:
                devices = await client.get_devices()
                ours = next((d for d in devices if not self.dsn or d.dsn == self.dsn), None)
                if ours is None:
                    raise RuntimeError(f"no device with DSN {self.dsn!r} on this account")
                self.junction_id = ours.junction_id
            if kind == "setpoint":
                await client.update_setpoint(self.junction_id, int(round(float(value))))
            elif kind == "mode":
                await client.update_mode(self.junction_id, OperationMode(value))
            else:
                raise ValueError(f"unknown write kind {kind!r}")
        finally:
            await client.close()
