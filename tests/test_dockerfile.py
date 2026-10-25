"""The add-on image ships every relay module (0.5.5 nearly shipped without one)."""

from pathlib import Path

RELAY = Path(__file__).parent.parent / "relay"


def test_every_relay_module_is_copied_into_the_image():
    copied = set()
    for line in (RELAY / "Dockerfile").read_text().splitlines():
        if line.startswith("COPY "):
            copied.update(line.split()[1:-1])
    modules = {p.name for p in RELAY.glob("*.py")}
    assert modules - copied == set()
