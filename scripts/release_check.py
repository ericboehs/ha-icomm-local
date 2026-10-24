#!/usr/bin/env python3
"""Release consistency checks, used by CI and the release workflow.

    release_check.py mirror           # relay/CHANGELOG.md == CHANGELOG.md
    release_check.py same-version     # add-on and integration versions agree
    release_check.py versions v0.4.6  # tag == add-on == integration; changelog has it
    release_check.py notes 0.4.6      # print that version's changelog section
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
MIRROR = ROOT / "relay" / "CHANGELOG.md"


def addon_version() -> str:
    found = re.search(r'^version:\s*"?([^"\n]+)"?', (ROOT / "relay/config.yaml").read_text(), re.M)
    return found.group(1).strip() if found else ""


def integration_version() -> str:
    manifest = ROOT / "custom_components/icomm_local/manifest.json"
    return json.loads(manifest.read_text())["version"]


def section(version: str) -> str:
    """The body of '## [version] - date', up to the next '## [' heading."""
    text = CHANGELOG.read_text()
    found = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: |\Z)",
                      text, re.S | re.M)
    return found.group(1).strip() if found else ""


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "mirror":
        if not MIRROR.exists() or MIRROR.read_text() != CHANGELOG.read_text():
            print("relay/CHANGELOG.md differs from CHANGELOG.md: cp CHANGELOG.md relay/")
            return 1
        print("changelog mirror ok")
        return 0
    if cmd == "same-version":
        if addon_version() != integration_version():
            print(f"::error::add-on {addon_version()} != integration {integration_version()}")
            return 1
        print(f"add-on and integration both {addon_version()}")
        return 0
    if cmd == "versions" and len(argv) == 3:
        tag = argv[2].removeprefix("v")
        errors = []
        if addon_version() != tag:
            errors.append(f"relay/config.yaml version {addon_version()} != tag {tag}")
        if integration_version() != tag:
            errors.append(f"manifest.json version {integration_version()} != tag {tag}")
        if not section(tag):
            errors.append(f"CHANGELOG.md has no '## [{tag}]' section with content")
        for error in errors:
            print(f"::error::{error}")
        if not errors:
            print(f"versions ok: {tag}")
        return 1 if errors else 0
    if cmd == "notes" and len(argv) == 3:
        body = section(argv[2].removeprefix("v"))
        if not body:
            return 1
        print(body)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
