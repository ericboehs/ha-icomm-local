"""The module's block-write frames: encode them, and weave them into a poll reply.

A cloud command to this appliance is a single `setBlockParameter` property whose
value is a short binary frame:

    BD  40  <total_len>  <block>  <offset>  <payload...>  <crc8>

`0x40` is the block-write opcode; `total_len` counts the whole frame including its
CRC; `crc8` is the table below, recovered from the appliance's own app bundle.
Decoding this frame format from captured traffic is what makes local writes
possible at all.

Two registers matter, both in block 11:

| register | offset | encoding |
|---|---|---|
| setpoint | 6 | big-endian 1/256 degC (so 138 F -> `3A E4`) |
| mode | 15 | `64 01` electric, `00 04` hybrid, `00 05` heat_pump, `NN 02` vacation |

Vacation was captured from the app twice (2026-10-24): "1 day" sent `01 02`
and "3 days" sent `03 02`, and the tank echoed each as its mode pair. The second
byte is the mode; for vacation the first byte is the number of days.

The `crc8` table has 256 entries but only 128 distinct values; that is how it was
recovered, and it reproduces every frame Ayla ever sent (see the golden vectors in
tests/test_frames.py), so it is left exactly as found.
"""

from __future__ import annotations

import re

CRC8_TABLE = (
    0, 94, 188, 226, 38, 120, 154, 196, 76, 18, 240, 174, 106, 52, 214, 136,
    152, 198, 36, 122, 190, 224, 2, 92, 212, 138, 104, 54, 242, 172, 78, 16,
    110, 48, 210, 140, 72, 22, 244, 170, 34, 124, 158, 192, 4, 90, 184, 230,
    246, 168, 74, 20, 208, 142, 108, 50, 186, 228, 6, 88, 156, 194, 32, 126,
    220, 130, 96, 62, 250, 164, 70, 24, 144, 206, 44, 114, 182, 232, 10, 84,
    68, 26, 248, 166, 98, 60, 222, 128, 8, 86, 180, 234, 46, 112, 146, 204,
    178, 236, 14, 80, 148, 202, 40, 118, 254, 160, 66, 28, 216, 134, 100, 58,
    42, 116, 150, 200, 12, 82, 176, 238, 102, 56, 218, 132, 64, 30, 252, 162,
    230, 184, 90, 4, 192, 158, 124, 34, 170, 244, 22, 72, 140, 210, 48, 110,
    126, 32, 194, 156, 88, 6, 228, 186, 50, 108, 142, 208, 20, 74, 168, 246,
    136, 214, 52, 106, 174, 240, 18, 76, 196, 154, 120, 38, 226, 188, 94, 0,
    16, 78, 172, 242, 54, 104, 138, 212, 92, 2, 224, 190, 122, 36, 198, 152,
    58, 100, 134, 216, 28, 66, 160, 254, 118, 40, 202, 148, 80, 14, 236, 178,
    162, 252, 30, 64, 132, 218, 56, 102, 238, 176, 82, 12, 200, 150, 116, 42,
    84, 10, 232, 182, 114, 44, 206, 144, 24, 70, 164, 250, 62, 96, 130, 220,
    204, 146, 112, 46, 234, 180, 86, 8, 128, 222, 60, 98, 166, 248, 26, 68,
)


PREFIX = 0xBD
OP_WRITE = 0x40

BLOCK_SETPOINT = 0x0B
OFFSET_SETPOINT = 0x06
BLOCK_MODE = 0x0B
OFFSET_MODE = 0x0F
MODES = {"electric": b"\x64\x01", "hybrid": b"\x00\x04", "heat_pump": b"\x00\x05"}
VACATION = 0x02
VACATION_DEFAULT_DAYS = 1      # the app's own first choice, and HA away mode's
MAX_VACATION_DAYS = 99         # stays clear of 0x64, which electric's first byte uses

# The appliance's own limits, from its telemetry (max setpoint reads 150 F).
# A write outside these is a bug in the caller, not a preference to honour.
MIN_SETPOINT = 95.0
HARD_MAX_SETPOINT = 160.0


def crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = CRC8_TABLE[(crc ^ byte) & 0xFF]
    return crc


def encode(block: int, offset: int, payload: bytes) -> bytes:
    """Any block write, CRC included."""
    body = bytes([PREFIX, OP_WRITE, 0x00, block & 0xFF, offset & 0xFF]) + payload
    body = body[:2] + bytes([len(body) + 1]) + body[3:]  # total_len includes the CRC
    return body + bytes([crc8(body)])


def setpoint_frame(fahrenheit: float) -> bytes:
    """A setpoint write. The scale is 1/256 degC above freezing."""
    celsius = (fahrenheit - 32.0) / 1.8
    raw = round(celsius * 256)
    if not 0 <= raw <= 0xFFFF:
        raise ValueError(f"setpoint out of encodable range: {fahrenheit}")
    return encode(BLOCK_SETPOINT, OFFSET_SETPOINT, raw.to_bytes(2, "big"))


def vacation_days(days: object) -> int:
    """Validate a vacation length: a whole number of days, 1-99."""
    if days is None:
        return VACATION_DEFAULT_DAYS
    if isinstance(days, bool) or not isinstance(days, int):
        raise ValueError(f"vacation days must be a whole number, got {days!r}")
    if not 1 <= days <= MAX_VACATION_DAYS:
        raise ValueError(f"vacation days {days} outside 1-{MAX_VACATION_DAYS}")
    return days


def mode_frame(mode: str, days: object = None) -> bytes:
    if mode == "vacation":
        return encode(BLOCK_MODE, OFFSET_MODE, bytes([vacation_days(days), VACATION]))
    if days is not None:
        raise ValueError("days applies only to vacation")
    if mode not in MODES:
        raise ValueError(
            f"unknown mode {mode!r}; expected one of {sorted([*MODES, 'vacation'])}")
    return encode(BLOCK_MODE, OFFSET_MODE, MODES[mode])


def decode_frame(text: str) -> dict:
    """Read a frame back: for logs, and for understanding what the cloud sent."""
    raw = bytes.fromhex(text.replace(" ", ""))
    if len(raw) < 6 or raw[0] != PREFIX:
        raise ValueError(f"not a block frame: {text!r}")
    payload = raw[5:-1]
    out = {
        "op": raw[1],
        "block": raw[3],
        "offset": raw[4],
        "payload": payload.hex(),
        "crc_ok": crc8(raw[:-1]) == raw[-1],
    }
    if raw[3] == BLOCK_SETPOINT and raw[4] == OFFSET_SETPOINT and len(payload) == 2:
        out["setpoint"] = round(int.from_bytes(payload, "big") / 256.0 * 1.8 + 32.0, 1)
    if raw[3] == BLOCK_MODE and raw[4] == OFFSET_MODE and len(payload) == 2:
        out["mode"] = next((n for n, v in MODES.items() if v == payload), "unknown")
        if out["mode"] == "unknown" and payload[1] == VACATION:
            out["mode"] = "vacation"
            out["vacation_days"] = payload[0]
    return out


def as_text(frame: bytes) -> str:
    """The spaced hex form Ayla uses in the XML (`BD 40 08 0B 0F 64 01 34`)."""
    return " ".join(f"{byte:02X}" for byte in frame)


SET_BLOCK_PROPERTY = re.compile(
    r"(<name>\s*setBlockParameter\s*</name>\s*<value>)(.*?)(</value>)", re.S
)


def rewrite_commands(xml: str, frame: str) -> tuple[str, bool]:
    """Put `frame` into a commands reply, returning (xml, replaced_existing).

    Ayla's replies come in two shapes and both can carry this property:

      * a plain command delivery, whose only property is `setBlockParameter`;
      * the full configuration fetch (`commands.xml?input=true`) issued when the
        module re-registers, where `setBlockParameter` sits among firmware and
        periodic-parameter properties and *describes the appliance's current
        state* -- which is how a write can be delivered with no cloud at all.

    Everything else in the reply is preserved byte for byte: it also carries
    parameter blocks the appliance needs, and mangling them would make a write
    look like a reconfiguration.
    """
    if SET_BLOCK_PROPERTY.search(xml):
        return SET_BLOCK_PROPERTY.sub(lambda m: m.group(1) + frame + m.group(3), xml, count=1), True

    property_xml = (
        "    <property>\n"
        "      <base-type>string</base-type>\n"
        "      <mime-type></mime-type>\n"
        "      <name>setBlockParameter</name>\n"
        f"      <value>{frame}</value>\n"
        "    </property>\n"
    )
    if "<properties/>" in xml:
        opened = "<properties>\n" + property_xml + "  </properties>"
        return xml.replace("<properties/>", opened, 1), False
    if "<properties>" in xml:
        return xml.replace("<properties>\n", "<properties>\n" + property_xml, 1), False
    return xml, False
