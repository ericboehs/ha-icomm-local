"""The add-on's options reach the relay: config.yaml <-> run.sh's mapper."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

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


def test_every_option_has_a_schema_entry():
    config = yaml.safe_load((RELAY / "config.yaml").read_text())
    assert set(config["options"]) <= set(config["schema"])


@pytest.mark.parametrize(("value", "exported"), [(True, True), (False, False), (None, False)])
def test_experimental_freeze_reconnect(tmp_path, value, exported):
    options = {} if value is None else {"experimental_freeze_reconnect": value}
    assert ("export FREEZE_RECONNECT=1" in _exports(tmp_path, options)) is exported


def test_it_ships_off():
    config = yaml.safe_load((RELAY / "config.yaml").read_text())
    assert config["options"]["experimental_freeze_reconnect"] is False
    assert config["schema"]["experimental_freeze_reconnect"] == "bool?"
