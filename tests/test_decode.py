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
    # Block 27 (research phase 49): a lifetime Wh counter in the millions, a
    # known operating state, a level the heater names, and no "air" (that
    # offset is the energy capacity).
    assert 5_000_000 < decoded["energy_wh"] < 6_000_000
    assert decoded["operating_state"] in ("idle", "heating")
    assert decoded["hot_water_level"] in ("low", "medium", "high")
    assert "air" not in decoded
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
    assert decoded["operating_state"] == "heating" and decoded["eev_pulses"] == 480


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


def test_what_was_read_as_air_is_the_energy_capacity(
        blocks_heat_pump_cooldown: list[bytes], blocks_electric: list[bytes],
        blocks_heat_pump: list[bytes]) -> None:
    """Block 4 offset 30 is Block 27 param 15, an energy capacity in Wh, not air.

    This payload once read as "air still 83.2 F" after the element stopped,
    "falling to 49.0 F within 47 s". Those are 7275 and 2412 raw: the
    electric-mode capacity, then the heat pump's at 150 F. Live, it stepped
    2412 -> 1812 in the same report as a 150 -> 130 F setpoint change.
    """
    cooldown = decode(blocks_heat_pump_cooldown)
    assert cooldown["mode"] == "heat_pump" and cooldown["watts"] == 0
    assert cooldown["operating_state"] == "idle"
    assert cooldown["energy_capacity_wh"] == decode(blocks_electric)["energy_capacity_wh"] == 7275
    assert decode(blocks_heat_pump)["energy_capacity_wh"] < 7275


def test_lifetime_energy_rises_and_has_a_no_value(blocks_heat_pump: list[bytes]) -> None:
    from icomm_relay import lifetime_energy
    block = bytearray(blocks_heat_pump[4])
    assert lifetime_energy(bytes(block)) == decode(blocks_heat_pump)["energy_wh"]
    block[14:20] = b"\xff" * 6
    assert lifetime_energy(bytes(block)) is None


def test_the_heaters_level_and_the_apps_percentage(blocks_hybrid: list[bytes]) -> None:
    """The heater's level is more pessimistic than the app: medium at a 270 Wh
    deficit, where the app (and hot_water_availability) says 100 %."""
    decoded = decode(blocks_hybrid)
    assert decoded["heat_deficit_wh"] == 270
    assert (decoded["hot_water_level"], decoded["hot_water_availability"]) == ("medium", 100)


def test_an_unknown_state_or_level_is_none(blocks_heat_pump: list[bytes]) -> None:
    block = bytearray(blocks_heat_pump[4])
    block[4:6] = (99).to_bytes(2, "big")
    block[46:48] = (7).to_bytes(2, "big")
    decoded = decode([*blocks_heat_pump[:4], bytes(block)])
    assert decoded["operating_state"] is None and decoded["operating_state_raw"] == 99
    assert decoded["hot_water_level"] is None


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


def test_hot_water_availability_matches_the_cloud() -> None:
    """A.O. Smith's 0/50/100 % against the controller's energy-deficit counter.

    The labels are the cloud sensor's value in effect when each capture was taken.
    The cloud recomputes it on each report, so a capture taken in the second
    before the cloud updated still carries the previous label. Exactly two such
    lag samples exist, and both match the *next* cloud value.
    """
    import json
    from pathlib import Path

    data = json.loads((Path(__file__).parent / "fixtures/availability_labelled.json").read_text())
    lagged = 0
    for sample in data["samples"]:
        got = decode([bytes.fromhex(b) for b in sample["blocks"]])["hot_water_availability"]
        if got != sample["cloud"]:
            lagged += 1
            assert got == sample["next_cloud"], sample["t"]
    assert len(data["samples"]) == 33
    assert lagged <= 2


def test_heat_deficit_falls_at_the_elements_rate() -> None:
    """Falls at roughly the element's 75 Wh/min (4500 W x 1/60 h): evidence for Wh.

    Not exact: the controller recomputes the counter in steps (150, 75, 120...),
    so it measured 76 Wh/min over the full 40-minute recovery and 86 Wh/min over
    the labelled 26 minutes used here.
    """
    import json
    from pathlib import Path

    from icomm_relay import availability

    data = json.loads((Path(__file__).parent / "fixtures/availability_labelled.json").read_text())
    heating = [(s["t"], decode([bytes.fromhex(b) for b in s["blocks"]]))
               for s in data["samples"]]
    heating = [(t, d["heat_deficit_wh"]) for t, d in heating if d["watts"] == 4500]
    (t0, d0), (t1, d1) = heating[0], heating[-1]
    rate = (d0 - d1) / ((t1 - t0) / 60)
    assert 65 <= rate <= 95, rate
    assert availability(5100) == 0 and availability(4800) == 50 and availability(450) == 100


# -- the heating element (block 1 byte 61, research phase 36) -----------------

@pytest.mark.parametrize(("flags", "element"), [
    (0x04, "upper"), (0x08, "lower"), (0x00, "off"),
    (0x03, "off"),              # the heat pump's bits, no element
    (0x01, "off"),              # vacation
    (0x0C, None),               # both: never seen, so unknown rather than a guess
])
def test_heating_element(flags: int, element: str | None) -> None:
    from icomm_relay import heating_element
    assert heating_element(flags) == element


@pytest.mark.parametrize(("fixture", "element", "flags"), [
    ("blocks_electric", "lower", 0x08),      # top at 146: the lower element's turn
    ("blocks_heat_pump", "off", 0x03),
    ("blocks_hybrid", "off", 0x00),
])
def test_heating_element_in_the_captures(fixture, element, flags, request) -> None:
    decoded = decode(request.getfixturevalue(fixture))
    assert decoded["heating_element"] == element and decoded["heat_flags"] == flags
