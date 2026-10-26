"""What the water heater says about itself: firmware, module, identity.

Two sources, both already passing through the relay:

  * the DSN config reply (every boot): the Wi-Fi module's agent version
    (`sw-version`), its hardware (`manuf-model`), the Ayla model, DSN and MAC;
  * the module's boot-time property reports, over LAN or as cloud uploads:
    `active_image_version` is the controller firmware the A.O. Smith app shows
    as "Firmware Version" (2.14), `inactive_image_version` the standby image.

The reports only arrive at boot, so the latest values are saved beside the
write queue (`device.json`, no secrets) and survive relay restarts.

superBlockStatic1, part 1, carries the nameplate the app shows (phase 45). Its
ASCII is stored as 16-bit words with the bytes swapped ("PH1S-0..." is
"HPS10-..."): the model at offset 32 and the serial number at offset 72, both
NUL-padded. The other blocks are kept raw for later decoding.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
from pathlib import Path

_LOGGER = logging.getLogger("icomm_device")

# DSN element -> our key. Only identity and versions: never the key, the
# regtoken, the ANS cipher key or the setup token.
DSN_FIELDS = {"sw-version": "module_firmware", "manuf-model": "module_hardware",
              "oem-model": "model", "mac": "mac"}
PROPERTIES = {"active_image_version": "controller_firmware",
              "inactive_image_version": "controller_firmware_standby",
              # The controller's current fault code, decimal; "0" is none. Posted
              # at boot and when it changes. Titles: the integration's FAULTS.
              "faultCode": "fault_code"}
STATIC = ("superBlockStatic1", "superBlockStatic2", "superBlockStatic3")
SAFE = re.compile(r"^[\x20-\x7e]{1,80}$")
HEX_LIST = re.compile(r"^[0-9a-fA-F,]{1,4096}$")
# (offset, length) in superBlockStatic1 part 1, after the byte swap.
NAMEPLATE = {"model_number": (32, 24), "serial_number": (72, 16)}
DATAPOINT = re.compile(r"/properties/([A-Za-z0-9_]{1,64})/datapoints\.xml$")
VALUE = re.compile(rb"<value>([^<]{0,4096})</value>")


def nameplate(value: str) -> dict[str, str]:
    """Model and serial number from superBlockStatic1 (comma-separated hex parts)."""
    parts = value.split(",")
    try:
        raw = bytes.fromhex(parts[1]) if len(parts) > 1 else b""
    except ValueError:
        return {}
    swapped = bytes(b for i in range(0, len(raw) - 1, 2) for b in (raw[i + 1], raw[i]))
    out = {}
    for key, (offset, length) in NAMEPLATE.items():
        text = swapped[offset:offset + length].split(b"\0", 1)[0].decode("ascii", "replace")
        if text and SAFE.fullmatch(text) and re.fullmatch(r"[A-Za-z0-9-]{4,24}", text):
            out[key] = text
    return out


class DeviceFacts:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.facts: dict = {}
        self.static: dict[str, str] = {}
        with contextlib.suppress(OSError, ValueError):
            saved = json.loads(path.read_text())
            self.facts = {k: v for k, v in (saved.get("facts") or {}).items()
                          if isinstance(v, str) and SAFE.fullmatch(v)}
            self.static = {k: v for k, v in (saved.get("static") or {}).items()
                           if k in STATIC and isinstance(v, str) and HEX_LIST.fullmatch(v)}

    def note_dsn(self, text: str) -> None:
        changed = False
        for element, key in DSN_FIELDS.items():
            found = re.search(rf"<{element}>([^<]{{1,80}})</{element}>", text)
            if found and SAFE.fullmatch(found.group(1).strip()):
                changed |= self._set(key, found.group(1).strip())
        if changed:
            self._save()

    def note_ayla_id(self, device_id: str) -> None:
        """Ayla's numeric device ID (the config reply's <key>). Not a credential:
        it is in the path of every cloud request; kept so a restart can route
        before the next config fetch (#18)."""
        if device_id.isdigit() and len(device_id) <= 20 and self._set("ayla_id", device_id):
            self._save()

    def note_dsn_id(self, dsn: str) -> None:
        """The DSN is in the config fetch's path, not its body."""
        if SAFE.fullmatch(dsn) and self._set("dsn", dsn):
            self._save()

    def note_property(self, name: str, value: str) -> None:
        if name in PROPERTIES and SAFE.fullmatch(value.strip()) and value.strip() != "None":
            if self._set(PROPERTIES[name], value.strip()):
                if name == "faultCode":
                    self.facts["fault_source"] = "property"
                if name == "active_image_version":
                    _LOGGER.warning("controller firmware: %s", value.strip())
                self._save()
        elif name in STATIC and HEX_LIST.fullmatch(value) and self.static.get(name) != value:
            self.static[name] = value
            if name == "superBlockStatic1":
                for key, found in nameplate(value).items():
                    self._set(key, found)
            self._save()

    def note_fault(self, code: int, source: str) -> None:
        """A fault code from somewhere other than the faultCode property."""
        if self._set("fault_code", str(code)):
            self.facts["fault_source"] = source
            self._save()

    def note_upload(self, path: str, body: bytes) -> None:
        """A cloud datapoint POST: /devices/<id>/properties/<name>/datapoints.xml."""
        found, value = DATAPOINT.search(path), VALUE.search(body)
        if found and value:
            self.note_property(found.group(1), value.group(1).decode("ascii", "replace"))

    def _set(self, key: str, value: str) -> bool:
        if self.facts.get(key) == value:
            return False
        self.facts[key] = value
        self.facts["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        return True

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"facts": self.facts, "static": self.static}))
        except OSError as err:
            _LOGGER.warning("could not save device facts: %s", err)

    def status(self) -> dict:
        return {k: v for k, v in self.facts.items() if k != "updated"} | {
            "updated": self.facts.get("updated")}
