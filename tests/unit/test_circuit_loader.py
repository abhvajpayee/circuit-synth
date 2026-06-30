"""Unit tests for circuit_synth.kicad.sch_gen.circuit_loader.

Covers the two gaps flagged in the test-coverage audit:

1. Pin-by-number resolution: when a net connection has both a pin number and a
   pin name, the number must be used as the identifier (not the name).  Multiple
   pins with the same function name (e.g. VDD, PGND) would otherwise collapse
   onto a single coordinate; using the number keeps each physical pin distinct.

2. Empty / tilde pin-name handling: empty string and "~" in the "number" or
   "name" field must fall through to the next fallback level rather than being
   used literally as identifiers.

Additional coverage for _circuits_match(), assign_subcircuit_instance_labels(),
build_ prefix stripping, and both net-format variants (old list / new dict).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from circuit_synth.kicad.sch_gen.circuit_loader import (
    _circuits_match,
    _parse_circuit,
    assign_subcircuit_instance_labels,
    load_circuit_hierarchy,
)


# ---------------------------------------------------------------------------
# Minimal fixtures
# ---------------------------------------------------------------------------

def _comp(ref="R1", symbol="Device:R", value="10k", pins=None):
    return {"ref": ref, "symbol": symbol, "value": value, "pins": pins or []}


def _conn(comp_ref, pin_dict):
    return {"component": comp_ref, "pin": pin_dict}


def _circuit_data(name="C", comps=None, nets=None, subcircuits=None):
    return {
        "name": name,
        "components": comps or [],
        "nets": nets or {},
        "subcircuits": subcircuits or [],
    }


# ---------------------------------------------------------------------------
# Pin-identifier resolution: number takes priority over name
# ---------------------------------------------------------------------------

class TestPinIdentifierResolution:
    """Tests for the logic at circuit_loader.py lines 289-294."""

    def _net_with_pin(self, pin_dict):
        """Return a parsed Circuit whose first net has one connection."""
        data = _circuit_data(
            comps=[_comp()],
            nets={"VCC": {"nodes": [_conn("R1", pin_dict)]}},
        )
        return _parse_circuit(data, {})

    def test_number_used_when_present(self):
        c = self._net_with_pin({"number": "1", "name": "VDD"})
        assert c.nets[0].connections == [("R1", "1")]

    def test_number_used_over_name_for_shared_function_names(self):
        """Two pins with name VDD but different numbers must map to "1" and "2"."""
        data = _circuit_data(
            comps=[_comp()],
            nets={
                "VCC": {"nodes": [_conn("R1", {"number": "1", "name": "VDD"})]},
                "VCC2": {"nodes": [_conn("R1", {"number": "2", "name": "VDD"})]},
            },
        )
        c = _parse_circuit(data, {})
        ids = {n.name: n.connections[0][1] for n in c.nets}
        assert ids["VCC"] == "1"
        assert ids["VCC2"] == "2"

    def test_empty_number_falls_back_to_name(self):
        c = self._net_with_pin({"number": "", "name": "VDD"})
        assert c.nets[0].connections == [("R1", "VDD")]

    def test_tilde_number_falls_back_to_name(self):
        c = self._net_with_pin({"number": "~", "name": "VDD"})
        assert c.nets[0].connections == [("R1", "VDD")]

    def test_missing_number_uses_name(self):
        c = self._net_with_pin({"name": "CLK"})
        assert c.nets[0].connections == [("R1", "CLK")]

    def test_tilde_name_falls_back_to_pin_id(self):
        c = self._net_with_pin({"number": "~", "name": "~", "pin_id": "42"})
        assert c.nets[0].connections == [("R1", "42")]

    def test_empty_name_falls_back_to_pin_id(self):
        c = self._net_with_pin({"number": "", "name": "", "pin_id": "7"})
        assert c.nets[0].connections == [("R1", "7")]

    def test_no_fields_yields_empty_string(self):
        c = self._net_with_pin({})
        assert c.nets[0].connections == [("R1", "")]


# ---------------------------------------------------------------------------
# Net format variants
# ---------------------------------------------------------------------------

class TestNetFormatVariants:

    def test_old_list_format(self):
        """nets dict value is a bare list of connection objects."""
        data = _circuit_data(
            comps=[_comp()],
            nets={"GND": [_conn("R1", {"number": "2", "name": "GND"})]},
        )
        c = _parse_circuit(data, {})
        assert c.nets[0].name == "GND"
        assert c.nets[0].connections == [("R1", "2")]

    def test_new_dict_format_with_nodes_key(self):
        data = _circuit_data(
            comps=[_comp()],
            nets={"GND": {"nodes": [_conn("R1", {"number": "2"})]}},
        )
        c = _parse_circuit(data, {})
        assert c.nets[0].connections == [("R1", "2")]

    def test_new_dict_format_fallback_to_connections_key(self):
        data = _circuit_data(
            comps=[_comp()],
            nets={"GND": {"connections": [_conn("R1", {"number": "2"})]}},
        )
        c = _parse_circuit(data, {})
        assert c.nets[0].connections == [("R1", "2")]

    def test_net_metadata_parsed(self):
        data = _circuit_data(
            nets={
                "+3V3": {
                    "nodes": [],
                    "is_power": True,
                    "power_symbol": "power:+3.3V",
                    "impedance": "50",
                }
            },
        )
        c = _parse_circuit(data, {})
        net = c.nets[0]
        assert net.is_power is True
        assert net.power_symbol == "power:+3.3V"
        assert net.impedance == "50"


# ---------------------------------------------------------------------------
# Circuit name normalisation
# ---------------------------------------------------------------------------

class TestCircuitNameNormalisation:

    def test_build_prefix_stripped(self):
        data = _circuit_data(name="build_acquisition")
        c = _parse_circuit(data, {})
        assert c.name == "Acquisition"

    def test_build_prefix_with_underscores(self):
        data = _circuit_data(name="build_power_board")
        c = _parse_circuit(data, {})
        assert c.name == "Power Board"

    def test_no_build_prefix_unchanged(self):
        data = _circuit_data(name="acquisition")
        c = _parse_circuit(data, {})
        assert c.name == "acquisition"


# ---------------------------------------------------------------------------
# Component formats
# ---------------------------------------------------------------------------

class TestComponentFormats:

    def test_list_format_creates_components(self):
        data = _circuit_data(comps=[_comp("R1"), _comp("R2")])
        c = _parse_circuit(data, {})
        assert len(c.components) == 2

    def test_dict_format_creates_components(self):
        data = {
            "name": "C",
            "components": {
                "R1": {"symbol": "Device:R", "value": "10k"},
                "C1": {"symbol": "Device:C", "value": "100nF"},
            },
            "nets": {},
        }
        c = _parse_circuit(data, {})
        refs = {comp.reference for comp in c.components}
        assert refs == {"R1", "C1"}

    def test_component_value_defaults_to_symbol_suffix(self):
        data = _circuit_data(comps=[{"ref": "U1", "symbol": "Device:LED", "pins": []}])
        c = _parse_circuit(data, {})
        assert c.components[0].value == "LED"


# ---------------------------------------------------------------------------
# _circuits_match
# ---------------------------------------------------------------------------

class TestCircuitsMatch:

    def _make_circuit(self, data):
        return _parse_circuit(data, {})

    def test_identical_structure_matches(self):
        data = _circuit_data(comps=[_comp("R1")], nets={"VCC": {"nodes": []}})
        c = self._make_circuit(data)
        assert _circuits_match(c, data) is True

    def test_different_component_types_no_match(self):
        data_a = _circuit_data(comps=[_comp("R1", symbol="Device:R")], nets={"N": {"nodes": []}})
        data_b = _circuit_data(comps=[_comp("C1", symbol="Device:C")], nets={"N": {"nodes": []}})
        c = self._make_circuit(data_a)
        assert _circuits_match(c, data_b) is False

    def test_different_net_count_no_match(self):
        data_a = _circuit_data(comps=[_comp()], nets={"VCC": {"nodes": []}})
        data_b = _circuit_data(comps=[_comp()], nets={"VCC": {"nodes": []}, "GND": {"nodes": []}})
        c = self._make_circuit(data_a)
        assert _circuits_match(c, data_b) is False


# ---------------------------------------------------------------------------
# assign_subcircuit_instance_labels
# ---------------------------------------------------------------------------

class TestAssignInstanceLabels:

    def _top_with_children(self, child_names):
        from circuit_synth.kicad.sch_gen.circuit_loader import Circuit
        top = Circuit("top")
        sub_dict = {}
        for name in child_names:
            if name not in sub_dict:
                sub_dict[name] = Circuit(name)
            top.child_instances.append({"sub_name": name, "instance_label": ""})
        return top, sub_dict

    def test_single_use_keeps_name(self):
        top, sub_dict = self._top_with_children(["MCU"])
        assign_subcircuit_instance_labels(top, sub_dict)
        assert top.child_instances[0]["instance_label"] == "MCU"

    def test_multiple_use_appends_index(self):
        top, sub_dict = self._top_with_children(["ADC", "ADC", "ADC"])
        assign_subcircuit_instance_labels(top, sub_dict)
        labels = [c["instance_label"] for c in top.child_instances]
        assert labels == ["ADC1", "ADC2", "ADC3"]

    def test_mixed_single_and_repeated(self):
        top, sub_dict = self._top_with_children(["MCU", "ADC", "ADC"])
        assign_subcircuit_instance_labels(top, sub_dict)
        labels = [c["instance_label"] for c in top.child_instances]
        assert labels == ["MCU", "ADC1", "ADC2"]


# ---------------------------------------------------------------------------
# load_circuit_hierarchy (integration)
# ---------------------------------------------------------------------------

def test_load_circuit_hierarchy_returns_circuit_and_dict(tmp_path: Path):
    payload = {
        "name": "top",
        "components": [_comp("R1")],
        "nets": {"VCC": {"nodes": [_conn("R1", {"number": "1"})]}},
    }
    json_file = tmp_path / "circuit.json"
    json_file.write_text(json.dumps(payload))
    top, sub_dict = load_circuit_hierarchy(str(json_file))
    assert top.name == "top"
    assert "top" in sub_dict
    assert len(top.nets) == 1


def test_load_circuit_hierarchy_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_circuit_hierarchy(str(tmp_path / "nonexistent.json"))
