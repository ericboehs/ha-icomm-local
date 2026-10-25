"""Answer the module's boot-time config fetch when Ayla can't.

At every boot the Wi-Fi agent fetches GET /dsns/<DSN>.xml before anything
else, and LAN mode (local control, telemetry) is only armed by its reply. Ayla
being down across a power cut would otherwise leave the heater unreachable
until Ayla came back.

The real exchange (phase 18 capture, phase 45):

  GET /dsns/<DSN>.xml   x-Ayla-client-auth: <signed pseudo-header>
      <- 401  x-ayla-server-time: <unix seconds>   (the agent sets its clock)
  GET /dsns/<DSN>.xml   x-Ayla-client-auth: <signed again, right time>
      <- 200  x-ayla-auth-key: <36 chars>  + the device config XML
  ...every later request carries x-Ayla-auth-key.

Offline, the relay plays Ayla's part from the last real reply it saved
(dsn-original.xml), with a fresh <unix-time> and a made-up key. The agent can't
tell: it accepts any server certificate and has no way to check the key. When
Ayla returns, it answers the made-up key with 401 and no server time, which
libada treats as "re-authenticate" -- the agent signs in to Ayla again on its own.

Only the module's own requests (a trusted peer) get this, and only for the DSN
the relay already knows. Pinning still needs Ayla's real 200.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from pathlib import Path

from aiohttp import web

_LOGGER = logging.getLogger("icomm_offline")

CLIENT_AUTH = "x-Ayla-client-auth"
UNIX_TIME = re.compile(r"<unix-time>\d{1,12}</unix-time>")
UNAUTHORIZED = '{ "error":{ "code":"401", "message":"401 - Authorization Required." }}'
# A second signed fetch within this long of our 401 is the agent's retry.
CHALLENGE_WINDOW_S = 120


class OfflineConfig:
    def __init__(self, cached: Path, enabled: bool = True) -> None:
        self.cached = cached
        self.enabled = enabled
        self.challenged_at = 0.0
        self.challenges = 0
        self.served = 0
        self.last_served: float | None = None

    def available(self) -> bool:
        return self.enabled and self.cached.is_file()

    def answer(self, request: web.Request, dsn: str, known_dsn: str | None,
               transform=None) -> web.Response | None:
        """Ayla's reply to a config fetch, or None if this isn't one we may answer."""
        if (not self.available() or request.method != "GET"
                or (known_dsn and dsn != known_dsn)):
            return None
        try:
            text = self.cached.read_text()
        except (OSError, UnicodeDecodeError):
            return None
        now = time.time()
        if request.headers.get(CLIENT_AUTH) and now - self.challenged_at > CHALLENGE_WINDOW_S:
            # First signed fetch: what Ayla does after a boot, a 401 with the time.
            self.challenged_at, self.challenges = now, self.challenges + 1
            _LOGGER.warning("Ayla unreachable: answering the module's config fetch "
                            "locally (401 with the time, as Ayla does)")
            response = web.Response(status=401, text=UNAUTHORIZED,
                                    content_type="application/json")
            response.headers["x-ayla-server-time"] = str(int(now))
            return response
        body = UNIX_TIME.sub(f"<unix-time>{int(now)}</unix-time>", text, count=1).encode()
        if transform is not None:
            body = transform(body) or body
        self.served += 1
        self.last_served = now
        _LOGGER.warning("Ayla unreachable: served the module its saved config (%d bytes); "
                        "LAN mode stays available until Ayla is back", len(body))
        response = web.Response(status=200, body=body,
                                content_type="application/xml", charset="utf-8")
        if request.headers.get(CLIENT_AUTH):
            response.headers["x-ayla-auth-key"] = str(uuid.uuid4())
        return response

    def status(self) -> dict:
        return {"enabled": self.enabled, "cached": self.cached.is_file(),
                "challenges": self.challenges, "served": self.served,
                "last_served": self.last_served}
