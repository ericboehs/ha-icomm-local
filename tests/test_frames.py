"""The write path: frames, and the reply rewriting that delivers them.

Golden vectors here are frames Ayla itself delivered to this appliance, recovered
from packet captures. If `encode` ever stops reproducing them, the write path is
broken even though the tests might otherwise look green.
"""

from __future__ import annotations

import icomm_frames as frames
import pytest

# Frames captured from Ayla's own `setBlockParameter` deliveries.
GOLDEN_SETPOINT = [
    (120.0, "BD 40 08 0B 06 30 E4 5E"),
    (121.0, "BD 40 08 0B 06 31 72 28"),
    (122.0, "BD 40 08 0B 06 32 00 56"),
    (138.0, "BD 40 08 0B 06 3A E4 92"),
]
GOLDEN_MODE = [
    ("electric", "BD 40 08 0B 0F 64 01 34"),
    ("hybrid", "BD 40 08 0B 0F 00 04 14"),
    ("heat_pump", "BD 40 08 0B 0F 00 05 4A"),
]

# The two reply shapes Ayla actually sends, trimmed to the properties that matter.
SIMPLE_REPLY = """<commands>
  <properties>
    <property>
      <base-type>string</base-type>
      <mime-type></mime-type>
      <name>setBlockParameter</name>
      <value>BD 40 08 0B 06 3A E4 92</value>
    </property>
  </properties>
  <schedules/>
  <cmds/>
</commands>
"""

FULL_REPLY = """<commands>
  <properties>
    <property>
      <base-type>integer</base-type>
      <mime-type></mime-type>
      <name>firmwareUpdateConsent</name>
      <value>1</value>
    </property>
    <property>
      <base-type>string</base-type>
      <mime-type></mime-type>
      <name>setBlockParameter</name>
      <value>BD 40 08 0B 06 3A E4 92</value>
    </property>
    <property>
      <base-type>integer</base-type>
      <mime-type></mime-type>
      <name>superBlockDynDataUpdateRate</name>
      <value>21601</value>
    </property>
  </properties>
  <schedules/>
  <cmds/>
</commands>
"""

EMPTY_REPLY = """<commands>
  <properties/>
  <schedules/>
  <cmds/>
</commands>
"""


@pytest.mark.parametrize("fahrenheit,expected", GOLDEN_SETPOINT)
def test_setpoint_frames_match_captured_traffic(fahrenheit: float, expected: str) -> None:
    assert frames.as_text(frames.setpoint_frame(fahrenheit)) == expected


@pytest.mark.parametrize("mode,expected", GOLDEN_MODE)
def test_mode_frames_match_captured_traffic(mode: str, expected: str) -> None:
    assert frames.as_text(frames.mode_frame(mode)) == expected


def test_frame_structure() -> None:
    """Every frame is 8 bytes for a 2-byte payload, with a valid CRC."""
    frame = frames.setpoint_frame(140.0)
    assert frame[0] == 0xBD and frame[1] == 0x40
    assert frame[2] == len(frame)          # total length includes the CRC
    assert frame[3] == 0x0B and frame[4] == 0x06
    assert frames.crc8(frame[:-1]) == frame[-1]


def test_crc_catches_corruption() -> None:
    frame = bytearray(frames.setpoint_frame(140.0))
    frame[5] ^= 0xFF
    assert frames.crc8(bytes(frame[:-1])) != frame[-1]


def test_setpoint_scale_round_trips() -> None:
    """1/256 degC is finer than the appliance's 1 F resolution, so it must not drift."""
    for fahrenheit in (95.0, 120.0, 138.0, 140.0, 149.0, 150.0):
        decoded = frames.decode_frame(frames.as_text(frames.setpoint_frame(fahrenheit)))
        assert decoded["setpoint"] == pytest.approx(fahrenheit, abs=0.01)
        assert decoded["crc_ok"] is True


@pytest.mark.parametrize("bad", ["turbo", "", "ELECTRIC", "hybrid "])
def test_unknown_mode_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        frames.mode_frame(bad)


def test_decode_rejects_non_frames() -> None:
    with pytest.raises(ValueError):
        frames.decode_frame("BD 40")                       # header only
    with pytest.raises(ValueError):
        frames.decode_frame("hello")                       # not hex at all


def test_decode_flags_a_missing_crc_byte() -> None:
    """A frame whose CRC byte never arrived must not read as valid."""
    assert frames.decode_frame("BD 40 08 0B 0F 64 01")["crc_ok"] is False


def test_rewrite_replaces_a_plain_command() -> None:
    """The common case: one queued command, replaced by ours."""
    new_frame = frames.as_text(frames.setpoint_frame(140.0))
    out, replaced = frames.rewrite_commands(SIMPLE_REPLY, new_frame)
    assert replaced is True
    assert new_frame in out
    assert "BD 40 08 0B 06 3A E4 92" not in out     # Ayla's 138 F is gone
    assert out.count("setBlockParameter") == 1


def test_rewrite_preserves_everything_else_in_a_full_reply() -> None:
    """The reconnect/config reply carries other properties that must survive.

    This is the reply shape issued when the module re-registers, and it is the one
    that allows a write with no cloud involvement at all.
    """
    new_frame = frames.as_text(frames.setpoint_frame(141.0))
    out, replaced = frames.rewrite_commands(FULL_REPLY, new_frame)
    assert replaced is True
    assert new_frame in out
    # Untouched neighbours, byte for byte.
    assert "firmwareUpdateConsent" in out and "<value>1</value>" in out
    assert "superBlockDynDataUpdateRate" in out and "<value>21601</value>" in out
    assert out.count("<property>") == FULL_REPLY.count("<property>")
    assert out.startswith("<commands>") and out.rstrip().endswith("</commands>")


def test_rewrite_inserts_into_an_empty_reply() -> None:
    """Ayla has nothing queued; the reply becomes the carrier."""
    new_frame = frames.as_text(frames.mode_frame("heat_pump"))
    out, replaced = frames.rewrite_commands(EMPTY_REPLY, new_frame)
    assert replaced is False
    assert new_frame in out
    assert "setBlockParameter" in out
    assert "<properties/>" not in out           # the self-closing tag got opened


def test_rewrite_is_surgical_on_the_setpoint_only() -> None:
    """A setpoint write must not be mistaken for a mode write or vice versa."""
    setpoint_reply, _ = frames.rewrite_commands(
        SIMPLE_REPLY, frames.as_text(frames.setpoint_frame(142.0)))
    assert "0B 06" in setpoint_reply and "0B 0F" not in setpoint_reply

    # A reply that carries a mode frame instead.
    mode_reply = SIMPLE_REPLY.replace("BD 40 08 0B 06 3A E4 92", "BD 40 08 0B 0F 64 01 34")
    out, _ = frames.rewrite_commands(mode_reply, frames.as_text(frames.mode_frame("heat_pump")))
    assert "0B 0F 00 05 4A" in out
    assert "0B 0F 64 01 34" not in out


def test_rewrite_leaves_unrelated_xml_alone() -> None:
    """Something that is not a commands reply must come back unchanged."""
    payload = "<foo><bar>1</bar></foo>"
    out, replaced = frames.rewrite_commands(payload, "BD 40 08 0B 06 3A E4 92")
    assert out == payload
    assert replaced is False
