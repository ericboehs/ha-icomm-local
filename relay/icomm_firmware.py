"""Hold firmware updates until the owner allows them.

How an update reaches the module (Ayla's agent source, libada client.c and
client_ota.c): the cloud queues a reverse-REST command in a commands.xml
reply,

    <cmd><id>N</id><method>PUT</method><resource>ota.json</resource>
         <uri>/ota.json</uri><data>{"ota":{"url":..,"ver":..,"size":..,
         "type":"module"|"host_mcu","source":..}}</data></cmd>

and the agent then downloads the image from that URL. A cloud update carries
no signature the agent checks (only LAN updates have a signed header), and
"host_mcu" updates reach the heater's own controller through the module.

The relay sits on that path, so it can:
- drop the command from the reply, which leaves it queued at Ayla, so it comes
  back on every poll until the owner decides;
- once the owner allows that exact (type, version), let the command through,
  and forward the image download to its real host. The steering rule sends
  all of the module's HTTPS here, so without that the download could never
  work. Only the URL from the allowed command is forwarded.

A firmware update could add the certificate check this project relies on, so
holding is the default (FIRMWARE_UPDATES=hold). FIRMWARE_UPDATES=allow passes
everything, as before 0.5.0.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

_LOGGER = logging.getLogger("icomm_relay")

CMD = re.compile(r"<cmd>(.*?)</cmd>", re.S)
RESOURCE = re.compile(r"<resource>\s*/?([^<\s]*)\s*</resource>")
DATA = re.compile(r"<data>(.*?)</data>", re.S)
SW_VERSION = re.compile(r"<sw-version>([^<]{1,80})</sw-version>")
FIRMWARE_RESOURCES = {"ota.json", "lanota.json"}
FORGET_AFTER = 30 * 86400        # an offer Ayla stopped sending
MAX_OFFERS = 16


def _cmd_payload(cmd: str) -> dict:
    found = DATA.search(cmd)
    if not found:
        return {}
    raw = found.group(1).strip()
    if raw.startswith("<![CDATA[") and raw.endswith("]]>"):
        raw = raw[9:-3]
    try:
        body = json.loads(html.unescape(raw))
    except ValueError:
        return {}
    ota = body.get("ota") if isinstance(body, dict) else None
    return ota if isinstance(ota, dict) else {}


def offer_key(kind: str, version: str) -> str:
    return f"{kind}:{version}"


class FirmwareGate:
    def __init__(self, policy: str, path: Path) -> None:
        self.hold = policy.strip().lower() != "allow"
        self.path = path
        self.offers: dict[str, dict] = {}      # key -> what Ayla offered
        self.allowed: set[str] = set()
        self.agent_version: str | None = None
        self.status_reports: list[dict] = []   # the module's ota_status acks
        self.load()

    # -- persistence -----------------------------------------------------
    def load(self) -> None:
        try:
            saved = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict):
            return
        offers = saved.get("offers")
        if isinstance(offers, dict):
            self.offers = {k: v for k, v in offers.items() if isinstance(v, dict)}
        allowed = saved.get("allowed")
        if isinstance(allowed, list):
            self.allowed = {str(k) for k in allowed}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(
                {"offers": self.offers, "allowed": sorted(self.allowed)}))
        except OSError as err:                 # never let bookkeeping break a poll
            _LOGGER.warning("could not save %s: %s", self.path, err)

    # -- the commands.xml reply -------------------------------------------
    def filter(self, payload: bytes) -> bytes | None:
        """Record firmware commands; drop the ones not allowed. None = unchanged."""
        text = payload.decode(errors="replace")
        if "<cmd>" not in text:
            return None
        dropped = recorded = 0

        def visit(found: re.Match) -> str:
            nonlocal dropped, recorded
            cmd = found.group(1)
            resource = RESOURCE.search(cmd)
            if not resource or resource.group(1) not in FIRMWARE_RESOURCES:
                return found.group(0)
            key = self._record(resource.group(1), _cmd_payload(cmd))
            recorded += 1
            if self.hold and key not in self.allowed:
                dropped += 1
                return ""
            return found.group(0)

        rewritten = CMD.sub(visit, text)
        if recorded:
            self.save()
        if not dropped:
            return None
        rewritten = re.sub(r"<cmds>\s*</cmds>", "<cmds/>", rewritten)
        _LOGGER.warning("firmware update held: %d command(s) kept from the module",
                        dropped)
        return rewritten.encode()

    def _record(self, resource: str, ota: dict) -> str:
        kind = str(ota.get("type") or "module")
        version = str(ota.get("ver") or "?")
        key = offer_key(kind, version)
        now = time.time()
        url = str(ota.get("url") or "")
        parts = urlsplit(url)
        offer = self.offers.get(key) or {"first_seen": now, "times": 0}
        offer.update({
            "type": kind, "version": version, "resource": resource,
            "size": ota.get("size"), "source": ota.get("source"),
            "url_host": parts.hostname, "url_path": parts.path,
            "url_scheme": parts.scheme, "last_seen": now,
            "times": offer["times"] + 1,
        })
        if offer["times"] == 1:
            _LOGGER.warning("firmware update offered: %s %s (%s bytes from %s)",
                            kind, version, offer["size"], parts.hostname)
        self.offers[key] = offer
        self._prune(now)
        return key

    def _prune(self, now: float) -> None:
        for key in [k for k, o in self.offers.items()
                    if now - o.get("last_seen", 0) > FORGET_AFTER]:
            self.offers.pop(key)
            self.allowed.discard(key)
        while len(self.offers) > MAX_OFFERS:
            oldest = min(self.offers, key=lambda k: self.offers[k].get("last_seen", 0))
            self.offers.pop(oldest)

    # -- the owner's decision --------------------------------------------
    def allow(self, key: str) -> bool:
        if key not in self.offers:
            return False
        self.allowed.add(key)
        self.save()
        # A count, not the key: it arrives in the owner's request.
        _LOGGER.warning("firmware update allowed by the owner (%d allowed)",
                        len(self.allowed))
        return True

    def download_target(self, host: str | None, path: str) -> str | None:
        """The https URL to fetch for an allowed image download, else None."""
        if not host:
            return None
        host = host.split(":", 1)[0].lower()
        for key in self.allowed:
            offer = self.offers.get(key) or {}
            if (offer.get("url_host") or "").lower() == host and offer.get("url_path") == path:
                return f"https://{offer['url_host']}{path}"
        return None

    # -- what the module and the DSN tell us -------------------------------
    def note_dsn(self, text: str) -> None:
        found = SW_VERSION.search(text)
        if found:
            self.agent_version = found.group(1).strip()

    def note_status(self, query: str) -> None:
        self.status_reports = (self.status_reports + [
            {"at": time.time(), "query": query[:120]}])[-5:]

    def status(self) -> dict:
        held = [dict(o, key=k) for k, o in self.offers.items()
                if self.hold and k not in self.allowed]
        return {"policy": "hold" if self.hold else "allow",
                "agent_version": self.agent_version,
                "held": held, "allowed": sorted(self.allowed),
                "status_reports": self.status_reports}
