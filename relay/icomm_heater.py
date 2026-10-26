"""One water heater's identity, storage and per-device components (#18, phase 1).

Everything the relay keeps for a heater lives under `<data>/heaters/<DSN>/`:

  lanip.json         the LAN key we minted, the pinned module address and DSN
  pending.json       queued writes
  device.json        firmware and nameplate facts
  dsn-original.xml   the last real config reply (offline fallback, evidence)
  dsn-rewritten.xml  what the module got instead

Shared, relay-wide files (`firmware.json`, `certs/`, `options.json`) stay at the
top of the data directory.

Phase 1 still serves exactly one heater; this only gives it a home of its own,
so phase 2 can add more without another storage migration.

Migration from the single-heater layout (files at the top of the data dir) runs
once at start-up and is safe to downgrade from:
  * lanip.json, device.json and the DSN replies are **copied**: the originals
    stay, so an older relay still finds its LAN key (losing it strands the
    module's Local Connect until a power cycle);
  * pending.json is **moved**: an older relay must not replay writes that this
    one already delivered.
A heater whose DSN isn't known yet (a NAT install that never saw a config
fetch) lives in `heaters/default/` and is renamed when the DSN turns up.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
from pathlib import Path

from icomm_device import DeviceFacts
from icomm_offline import OfflineConfig

_LOGGER = logging.getLogger("icomm_heater")

DSN_RE = re.compile(r"[A-Za-z0-9]{6,32}")
DEFAULT_SLOT = "default"
PRIMARY_MARKER = "PRIMARY"
RESERVED = {DEFAULT_SLOT.upper(), PRIMARY_MARKER.upper()}
COPIED = ("lanip.json", "device.json", "dsn-original.xml", "dsn-rewritten.xml")
MOVED = ("pending.json",)


def clean_dsn(value) -> str | None:
    """A DSN fit for a directory name, or None.

    fullmatch, not match with ^$: "$" also matches before a trailing newline.
    The result is rebuilt from the match, so nothing else of the input survives.
    """
    found = DSN_RE.fullmatch(value) if isinstance(value, str) else None
    dsn = found.group(0).upper() if found else None
    # The storage's own names under heaters/ are never a heater's (case-blind:
    # macOS and some NAS filesystems would fold them together).
    return None if dsn in RESERVED else dsn


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def known_dsn(data_dir: Path, configured: str = "") -> str | None:
    """The primary heater's DSN.

    Configured, else the PRIMARY marker the hub keeps (#18 phase 2), else the
    single-heater files at the top of the data dir (the original heater), else
    the first heater directory.
    """
    heaters = data_dir / "heaters"
    marker = ""
    with contextlib.suppress(OSError, UnicodeDecodeError):
        marker = (heaters / PRIMARY_MARKER).read_text().strip()
    candidates = [configured, marker,
                  _json(data_dir / "lanip.json").get("module_dsn"),
                  (_json(data_dir / "device.json").get("facts") or {}).get("dsn")]
    if heaters.is_dir():
        candidates += sorted(p.name for p in heaters.iterdir()
                             if p.is_dir() and p.name != DEFAULT_SLOT)
    for candidate in candidates:
        dsn = clean_dsn(candidate)
        if dsn:
            return dsn
    return None


def migrate(data_dir: Path, dsn: str | None, queue_name: str = "pending.json",
            legacy: bool = True) -> Path:
    """Give the heater its directory, bringing the single-heater files along.

    Only the primary heater inherits the legacy files (`legacy`): a second
    heater copying them would take the first one's LAN key and pin.
    """
    heaters = data_dir / "heaters"
    dsn = clean_dsn(dsn)
    target = heaters / (dsn or DEFAULT_SLOT)
    if not legacy:
        return target
    default = heaters / DEFAULT_SLOT
    if dsn and default.is_dir() and not target.exists():
        default.rename(target)
        _LOGGER.warning("storage: heaters/%s is now heaters/%s", DEFAULT_SLOT, dsn)
    names = [(n, n, False) for n in COPIED] + [(n, "pending.json", True) for n in MOVED]
    if queue_name != "pending.json":
        names.append((queue_name, "pending.json", True))
    for old_name, new_name, move in names:
        old, new = data_dir / old_name, target / new_name
        if not old.is_file() or new.exists():
            continue
        target.mkdir(parents=True, exist_ok=True)
        if move:
            old.replace(new)
        else:
            shutil.copy2(old, new)                     # keeps 0600 on lanip.json
        _LOGGER.warning("storage: %s %s to %s", "moved" if move else "copied",
                        old_name, new.relative_to(data_dir))
    return target


RELAY_WIDE = {"firmware.json", "options.json", "certs"}


def _legacy_owner(data_dir: Path) -> str | None:
    """Whose the single-heater files at the top of the data dir are."""
    return (clean_dsn(_json(data_dir / "lanip.json").get("module_dsn"))
            or clean_dsn((_json(data_dir / "device.json").get("facts") or {}).get("dsn")))


def forget(data_dir: Path, dsn: str, primary: bool, queue_name: str = "pending.json") -> list[str]:
    """Delete a heater's files (DELETE /heaters/<dsn>), so no restart brings it
    back: its directory; the PRIMARY marker if it names it; and the
    single-heater files at the top of the data dir if they're its own (by the
    DSN they hold, or with no DSN, if it's the primary) -- known_dsn() and
    migrate() would rebuild it from them. Never another heater's, never the
    relay's own (firmware gate, options, certificates). Returns what went."""
    clean = clean_dsn(dsn)
    if not clean:
        return []
    heaters = os.path.realpath(data_dir / "heaters")
    full = os.path.normpath(os.path.join(heaters, clean))
    if not full.startswith(heaters + os.sep):         # CodeQL path-injection pattern
        return []
    removed = []
    if os.path.isdir(full):
        shutil.rmtree(full, ignore_errors=True)
        removed.append(f"heaters/{clean}/")
    marker = data_dir / "heaters" / PRIMARY_MARKER
    with contextlib.suppress(OSError, UnicodeDecodeError):
        if clean_dsn(marker.read_text().strip()) == clean:
            marker.unlink()
            removed.append(f"heaters/{PRIMARY_MARKER}")
    owner = _legacy_owner(data_dir)
    if owner == clean or (owner is None and primary):
        names = dict.fromkeys([*COPIED, *MOVED, os.path.basename(queue_name)])
        for name in names:
            if name in RELAY_WIDE:
                continue
            with contextlib.suppress(FileNotFoundError, IsADirectoryError):
                (data_dir / name).unlink()
                removed.append(name)
    return removed


class Heater:
    """A heater's DSN and directory, and the components that persist there."""

    def __init__(self, data_dir: Path, dsn: str | None = None,
                 offline_enabled: bool = True, queue_name: str = "pending.json",
                 legacy: bool = True) -> None:
        self.data_dir = data_dir
        dsn = clean_dsn(dsn)
        self.dsn = dsn
        self.dir = migrate(data_dir, dsn, queue_name, legacy)
        self.device = DeviceFacts(self.dir / "device.json")
        self.offline = OfflineConfig(self.dir / "dsn-original.xml", offline_enabled)
        if dsn:
            self.device.note_dsn_id(dsn)

    @property
    def queue_file(self) -> Path:
        return self.dir / "pending.json"

    @property
    def key_file(self) -> Path:
        return self.dir / "lanip.json"

    def adopt(self, dsn: str) -> None:
        """The DSN turned up: move out of heaters/default into heaters/<DSN>."""
        clean = clean_dsn(dsn)
        if self.dsn or not clean:
            return
        # Belt and braces (and CodeQL's path-injection pattern): the directory
        # must normalize to somewhere inside heaters/.
        heaters = os.path.realpath(self.data_dir / "heaters")
        full = os.path.normpath(os.path.join(heaters, clean))
        if not full.startswith(heaters + os.sep):
            return
        target = Path(full)
        if self.dir.is_dir() and not target.exists():
            self.dir.rename(target)
            # No names: the new one comes from the module's DSN (CodeQL
            # py/log-injection). /state's heater.dir shows where it went.
            _LOGGER.warning("storage: the heater's directory now carries its DSN")
        self.dsn, self.dir = clean, target
        self.device.path = target / "device.json"
        self.offline.cached = target / "dsn-original.xml"

    def mark_primary(self) -> None:
        """Remember which heater the old single-heater endpoints act on."""
        if not self.dsn:
            return
        marker = self.data_dir / "heaters" / PRIMARY_MARKER
        current = ""
        with contextlib.suppress(OSError, UnicodeDecodeError):
            current = marker.read_text().strip()
        if current == self.dsn:
            return
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(self.dsn + "\n")
        except OSError as err:
            _LOGGER.warning("storage: could not record the primary heater: %s", err)

    def status(self) -> dict:
        return {"dsn": self.dsn, "dir": str(self.dir)}
