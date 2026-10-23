"""The decoder: the register atlas, checked against real appliance payloads.

Expected values are asserted against facts that come from outside the decoder --
the element's rated draw, the documented 150 F upper setpoint limit, the physical
relationship between the top and bottom of a hot tank -- so a wrong offset cannot
pass by agreeing with itself.
"""

from __future__ import annotations

import pytest
from icomm_relay import decode, fahrenheit, telemetry_blocks

# The module's five blocks. If a firmware update changes these, the decoder is
# wrong everywhere at once; assert it rather than let an offset slide.
BLOCK_LENGTHS = [8, 146, 230, 52, 52]


def test_block_layout(blocks_electric: list[bytes]) -> None:
    assert [len(block) for block in blocks_electric] == BLOCK_LENGTHS


@pytest.mark.parametrize("fixture", ["blocks_electric", "blocks_heat_pump", "blocks_hybrid"])
def test_invariants_hold_for_every_capture(fixture: str, request: pytest.FixtureRequest) -> None:
    """Things true of any healthy tank, in any mode."""
    decoded = decode(request.getfixturevalue(fixture))

    # 150 F is the appliance's documented upper limit, constant in every capture.
    assert decoded["max_setpoint"] == 150.0
    # Line voltage on a 240 V circuit: this is the line-to-line reading (~235 V),
    # not a 120 V leg. An appliance reading ~120 here would mean the wrong field.
    assert 200 <= decoded["volts"] <= 260
    # A water heater's setpoint must sit inside its own limits.
    assert 32.0 < decoded["setpoint"] <= decoded["max_setpoint"]
    # Wi-Fi RSSI is negative dBm; a positive value means a signedness bug.
    assert -100 < decoded["rssi"] < 0
    # Air at the unit is a real temperature, not a nil-read zero.
    assert 32.0 < decoded["air"] < 140.0
    # Both tank probes report plausible water.
    assert 32.0 < decoded["water_bottom"] < 200.0
    assert 32.0 < decoded["water_top"] < 200.0


def test_electric_element_on(blocks_electric: list[bytes]) -> None:
    """Heating with the element: 4500 W is a hardware rating, not a magic number."""
    decoded = decode(blocks_electric)
    assert decoded["mode"] == "electric"
    assert decoded["mode_raw"] == "100/1"
    assert decoded["watts"] == 4500
    # Recovering tank: the bottom lags the top while the element works.
    assert decoded["water_top"] > decoded["water_bottom"]


def test_heat_pump_running(blocks_heat_pump: list[bytes]) -> None:
    """Compressor mode with the compressor working: its own draw, not the element's."""
    decoded = decode(blocks_heat_pump)
    assert decoded["mode"] == "heat_pump"
    assert decoded["watts"] == 356           # compressor, an order of magnitude below 4500
    assert decoded["water_top"] == 114.0
    # A satisfied tank reads the same at both probes.
    assert decoded["water_top"] == decoded["water_bottom"]
    # Cold air is the evaporator, not a nil read.
    assert decoded["air"] < 60.0


def test_hybrid(blocks_hybrid: list[bytes]) -> None:
    decoded = decode(blocks_hybrid)
    assert decoded["mode"] == "hybrid"
    assert decoded["mode_raw"] == "0/4"
    assert decoded["watts"] == 0


def test_compressor_draw_is_not_element_draw(blocks_heat_pump: list[bytes],
                                            blocks_electric: list[bytes]) -> None:
    """The heat pump must never report the element's 4500 W.

    Regression guard for the power model: conflating the two was a real earlier
    error, and 4500 W is a hardware rating that cannot come from a compressor.
    """
    heat_pump = decode(blocks_heat_pump)
    electric = decode(blocks_electric)
    assert heat_pump["watts"] == 356
    assert electric["watts"] == 4500
    assert heat_pump["watts"] != electric["watts"]


def test_air_is_a_lagging_thermal_reading_not_a_mode_signal(
        blocks_heat_pump_cooldown: list[bytes]) -> None:
    """Air temperature does not identify the mode, and believing it did was a bug.

    This payload is the first post after the element stopped: the mode register
    already reads heat pump and draw is already 0 W, but air still reads 83.2 F
    from the electric run that just ended (it fell to 49.0 F within 47 s). The
    authoritative mode is the register -- `mode`/`mode_raw` -- and air is only a
    thermal indicator of what the unit has been doing.
    """
    decoded = decode(blocks_heat_pump_cooldown)
    assert decoded["mode"] == "heat_pump"      # register says heat pump
    assert decoded["watts"] == 0               # nothing is running
    assert decoded["air"] > 80.0               # yet the air is still 83.2 F


def test_fahrenheit_scale() -> None:
    """1/256 of a degree Celsius, offset 32 -- and nothing below freezing."""
    assert fahrenheit(b"\x00\x00") == 32.0
    # The documented 150 F max setpoint; the raw value is 149.998, i.e. the
    # appliance's own 1 F rounding, so compare with tolerance rather than equality.
    assert fahrenheit(b"\x41\x8e") == pytest.approx(150.0, abs=0.01)
    # One integer step is 1/256 C, about 0.007 F.
    assert fahrenheit(b"\x00\x01") == pytest.approx(32.007, abs=1e-3)


def test_unknown_mode_pair_is_not_guessed() -> None:
    """An unrecognised mode must say so, not silently become a known one."""
    block = bytearray(52)
    block[30], block[31] = 7, 7
    blocks = [bytes(8), bytes(146), bytes(230), bytes(block), bytes(52)]
    decoded = decode(blocks)
    assert decoded["mode"] == "unknown"
    assert decoded["mode_raw"] == "7/7"


def test_telemetry_blocks_parses_real_body(blocks_electric: list[bytes]) -> None:
    body = ("<datapoints><datapoint><value>"
            + ",".join(block.hex() for block in blocks_electric)
            + "</value></datapoint></datapoints>").encode()
    assert telemetry_blocks(body) == blocks_electric


@pytest.mark.parametrize("body", [
    b"",
    b"<datapoints></datapoints>",             # no <value> at all
    b"<datapoints><value></value></datapoints>",  # empty value
    b"<datapoints><value>not-hex</value></datapoints>",
    b"\xff\xfe binary garbage",
    b"<datapoints><value>4283</value></datapoints>",  # a non-block datapoint
])
def test_telemetry_blocks_rejects_junk(body: bytes) -> None:
    """Anything that is not our block list returns None, never raises.

    The relay forwards arbitrary traffic on this path; a parse failure must not
    become a proxy failure.
    """
    assert telemetry_blocks(body) is None
