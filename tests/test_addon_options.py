"""The add-on's options reach the relay: config.yaml <-> run.sh's mapper."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

RELAY = Path(__file__).resolve().parent.parent / "relay"


def _mapper() -> str:
    """The Python run.sh pipes options.json through."""
    text = (RELAY / "run.sh").read_text()
    return re.search(r"<<'PY'\n(.*?)\nPY\n", text, re.S).group(1)


def _exports(tmp_path, options: dict) -> str:
    path = tmp_path / "options.json"
    path.write_text(json.dumps(options))
    return subprocess.run([sys.executable, "-c", _mapper(), str(path)], check=True,
                          capture_output=True, text=True).stdout


def _section(name: str) -> dict[str, str]:
    """A flat top-level mapping of config.yaml (the add-on image has no
    PyYAML, and these tests run there too)."""
    out, inside = {}, False
    for line in (RELAY / "config.yaml").read_text().splitlines():
        if not line.startswith(" "):
            inside = line.rstrip() == f"{name}:"
            continue
        match = re.match(r"  (\w+): ?(.*)", line)
        if inside and match:
            out[match.group(1)] = match.group(2).strip()
    return out


def test_every_option_has_a_schema_entry():
    assert _section("options") and set(_section("options")) <= set(_section("schema"))


@pytest.mark.parametrize(("value", "exported"), [(True, True), (False, False), (None, False)])
def test_experimental_freeze_reconnect(tmp_path, value, exported):
    options = {} if value is None else {"experimental_freeze_reconnect": value}
    assert ("export FREEZE_RECONNECT=1" in _exports(tmp_path, options)) is exported


def test_it_ships_off():
    assert _section("options")["experimental_freeze_reconnect"] == "false"
    assert _section("schema")["experimental_freeze_reconnect"] == "bool?"


@pytest.mark.parametrize(("value", "exported"), [(True, True), (False, False), (None, False)])
def test_debug_tls_keylog(tmp_path, value, exported):
    options = {} if value is None else {"debug_tls_keylog": value}
    assert ("export TLS_KEYLOG=1" in _exports(tmp_path, options)) is exported


def test_debug_tls_keylog_is_hidden_and_optional():
    assert "debug_tls_keylog" not in _section("options")
    assert _section("schema")["debug_tls_keylog"] == "bool?"
