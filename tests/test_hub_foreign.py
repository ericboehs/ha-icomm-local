"""A /devices/<id>/ request while the primary's Ayla ID is still unknown."""

from test_hub import (
    DSN_A,
    DSN_B,
    GATEWAY,
    ID_B,
    A,
    _hub,
    _module,
)
from test_proxy import _telemetry_body

TELEMETRY = "/devices/{}/properties/superBlockDyn1/datapoints.xml"


async def test_single_heater_first_boot_still_trusts_its_requests(tmp_path, monkeypatch):
    hub = _hub(tmp_path, monkeypatch, MODULE_IP=A)
    assert hub.primary.device_id is None
    await _module(hub, GATEWAY, "POST", TELEMETRY.format("999"), _telemetry_body())
    assert hub.primary.state["mode"] == "electric"


async def test_with_two_heaters_an_unknown_primary_id_is_not_assumed(tmp_path, monkeypatch):
    hub = _hub(tmp_path, monkeypatch, MODULE_IP=A)
    await _module(hub, GATEWAY, "GET", f"/dsns/{DSN_A}.xml")
    await _module(hub, GATEWAY, "GET", f"/dsns/{DSN_B}.xml")
    assert DSN_B in hub.secondaries
    hub.primary.device_id = None              # e.g. its config reply carried no <key>
    lan_json = await _module(hub, GATEWAY, "GET", "/devices/555/lan.json")
    assert hub.primary.lan.key.encode() not in lan_json.body
    await _module(hub, GATEWAY, "POST", TELEMETRY.format("555"),
                  _telemetry_body("telemetry_heat_pump.json"))
    assert not hub.primary.state.get("mode")
    # The secondary's own ID still routes to it.
    await _module(hub, GATEWAY, "POST", TELEMETRY.format(ID_B),
                  _telemetry_body("telemetry_hybrid.json"))
    assert hub.secondaries[DSN_B].state["mode"] == "hybrid"


async def test_a_refused_heater_also_makes_an_unknown_id_foreign(tmp_path, monkeypatch):
    hub = _hub(tmp_path, monkeypatch, MODULE_IP=A, MAX_HEATERS="1")
    hub.refused.add(DSN_B)
    await _module(hub, GATEWAY, "POST", TELEMETRY.format("555"), _telemetry_body())
    assert not hub.primary.state.get("mode")
    # Paths without a device ID still go to the primary.
    await _module(hub, GATEWAY, "GET", "/time.json")
    assert hub.primary._dial is not None
