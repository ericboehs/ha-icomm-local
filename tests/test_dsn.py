"""The DSN LAN-mode experiment: flip the OEM's factory kill switches.

Ayla's DSN config for the module ships `<lan-enabled>false` and
`<local-access-enabled>false` -- the LAN protocol exists in the firmware and the
OEM turned it off. The relay's job in the experiment is to change exactly those
two values in the reply the module receives on re-registration, and nothing
else. These tests pin that byte-exactness.
"""

import pathlib
import tempfile

from relay import icomm_relay

DSN = """<?xml version="1.0" encoding="UTF-8"?>
<device>
  <product-name>NextGenHP</product-name>
  <lan-ip>10.0.1.187</lan-ip>
  <regtoken>abc123</regtoken>
  <lan-enabled>false</lan-enabled>
  <local-access-enabled>false</local-access-enabled>
  <registered>0</registered>
</device>
"""


def _relay(tmp_path: pathlib.Path) -> icomm_relay.Relay:
    relay = icomm_relay.Relay.__new__(icomm_relay.Relay)
    relay.queue_file = tmp_path / "pending.json"
    relay.dsn_lan_rewrite = True
    return relay


def test_both_switches_flipped_and_nothing_else():
    with tempfile.TemporaryDirectory() as d:
        relay = _relay(pathlib.Path(d))
        out = relay._rewrite_dsn(DSN.encode())
        assert out is not None
        text = out.decode()
        assert "<lan-enabled>true</lan-enabled>" in text
        assert "<local-access-enabled>true</local-access-enabled>" in text
        # every other byte identical, in order
        assert text.replace("lan-enabled>true", "lan-enabled>false") \
                   .replace("local-access-enabled>true", "local-access-enabled>false") == DSN
        assert "registered>0" in text          # untouched
        assert (pathlib.Path(d) / "dsn-original.xml").read_bytes() == DSN.encode()
        assert (pathlib.Path(d) / "dsn-rewritten.xml").exists()


def test_reply_without_switches_passes_through():
    with tempfile.TemporaryDirectory() as d:
        relay = _relay(pathlib.Path(d))
        assert relay._rewrite_dsn(b"<device><registered>0</registered></device>") is None
        assert not (pathlib.Path(d) / "dsn-original.xml").exists()


def test_one_switch_still_rewrites():
    with tempfile.TemporaryDirectory() as d:
        relay = _relay(pathlib.Path(d))
        out = relay._rewrite_dsn(
            b"<device><lan-enabled>false</lan-enabled></device>")
        assert out == b"<device><lan-enabled>true</lan-enabled></device>"
