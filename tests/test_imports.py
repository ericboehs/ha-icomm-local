"""Relay modules are imported one way only: flat, from relay/ on sys.path.

`relay.icomm_relay` beside `icomm_relay` would load two copies of the module,
with two sets of globals, so a monkeypatch on one would miss the other.
"""

import re
from pathlib import Path

PACKAGE_IMPORT = re.compile(r"^\s*(from|import)\s+relay\.", re.M)


def test_no_test_imports_the_relay_as_a_package():
    offenders = [p.name for p in Path(__file__).parent.glob("*.py")
                 if PACKAGE_IMPORT.search(p.read_text())]
    assert offenders == []
