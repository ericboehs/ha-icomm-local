"""The fault table in block 2 and how it feeds the fault fact (phase 52)."""

from __future__ import annotations

import json
from pathlib import Path

from test_proxy import _relay

from relay.icomm_relay import decode

FIXTURES = Path(__file__).parent / "fixtures"


def _blocks(name: str) -> list[bytes]:
    return [bytes.fromhex(b) for b in json.loads((FIXTURES / name).read_text())["blocks"]]


def test_during_the_leak():
    decoded = decode(_blocks("telemetry_leak.json"))
    assert decoded["active_fault"] == 31                          # Water leak
    assert decoded["fault_history"] == [31, 31, 31, 31]           # H1-H4, newest first
    assert decoded["operating_state"] == "fault" and decoded["operating_state_raw"] == 5
    assert decoded["active_setpoint"] == 120.0 and decoded["setpoint"] == 150.0
    assert decoded["watts"] == 0 and decoded["mode"] == "electric"


def test_before_the_leak_the_panel_showed_this_history():
    decoded = decode(_blocks("telemetry_panel_check.json"))      # 14:39, H1-H4 read off it
    assert decoded["active_fault"] == 0
    assert decoded["fault_history"] == [31, 31, 31, 48]


def test_a_short_block_2_leaves_the_faults_unknown():
    blocks = _blocks("telemetry_leak.json")
    blocks[2] = blocks[2][:40]
    decoded = decode(blocks)
    assert decoded["active_fault"] is None and decoded["fault_history"] is None
    assert decoded["watts"] == 0                                  # the rest still decodes


def _report(relay, name: str) -> None:
    relay._ingest_blocks(_blocks(name))


def test_telemetry_settles_the_fact_after_a_restart(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    relay.device.facts["fault_code"] = "31"                       # stale, from before
    _report(relay, "telemetry_panel_check.json")                  # active fault 0
    assert relay.device.facts["fault_code"] == "0"
    assert relay.device.facts["fault_source"] == "telemetry"


def test_the_property_leads_and_a_stale_report_cannot_undo_a_clear(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    _report(relay, "telemetry_panel_check.json")                  # 0
    relay.device.note_property("faultCode", "31")                 # 14:56:35, over the LAN
    assert relay.device.facts["fault_code"] == "31"
    assert relay.device.facts["fault_source"] == "property"
    _report(relay, "telemetry_leak.json")                         # 14:56:58: 31 as well
    relay.device.note_property("faultCode", "0")                  # 14:57:48: cleared
    _report(relay, "telemetry_leak.json")                         # a report still saying 31
    assert relay.device.facts["fault_code"] == "0"                # unchanged telemetry: ignored
    _report(relay, "telemetry_panel_check.json")                  # 14:57:58: 0
    assert relay.device.facts["fault_code"] == "0"


def test_a_fault_seen_only_in_telemetry(tmp_path, monkeypatch):
    relay = _relay(tmp_path, monkeypatch)
    _report(relay, "telemetry_panel_check.json")
    _report(relay, "telemetry_leak.json")                         # the property was missed
    assert relay.device.facts["fault_code"] == "31"
    assert relay.device.facts["fault_source"] == "telemetry"
