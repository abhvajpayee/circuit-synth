"""Regression tests for name-indexed pin connection duplication.

`comp["PIN_NAME"] += net` triggers Python's augmented-assignment protocol:
`__getitem__` (fetch the Pin), `Pin.__iadd__` (connect), then
`__setitem__(pin_id, pin)` (writeback) -- even though the result is only ever
assigned back to the same slot. `SimplifiedPinAccess.__setitem__` re-keyed
`_pins` by the caller's lookup string (`pin_id`, e.g. "PG7") instead of the
pin's own number, and unconditionally appended to `_pin_names[name]` without
checking whether that exact Pin object was already registered there. A single
`comp["NAME"] += net` therefore left the SAME Pin object reachable under two
keys in `_pins` (its real number, and the name string) and duplicated in
`_pin_names[name]`.

`NetlistExporter.to_dict()` builds each net's connection list by iterating
`comp._pins.items()` with no de-duplication, so the doubled dict entry above
produced two identical connection nodes for one physical pin -- which then
rendered as two overlapping labels at the same schematic coordinate.

Found while investigating duplicate overlapping labels on the Acquisition
Module's MCU sheet (windTunnelProject, 2026-07-22): every net wired via
`comp["NAME"] += net` was affected; nets wired via `comp[number] += net`
(all passives -- pin name is empty/"~") were not.
"""
from circuit_synth import Component, Net, circuit
from circuit_synth.core.netlist_exporter import NetlistExporter


@circuit(name="t_single_name_connect")
def _single_name_connect():
    j = Component(symbol="Connector_Generic:Conn_01x04", ref="J1")
    n = Net("N1")
    j["Pin_1"] += n


def test_single_name_indexed_connection_stays_singleton():
    circ = _single_name_connect()
    j = circ._components["J1"]
    assert len(j._pin_names["Pin_1"]) == 1
    # The pin must be reachable ONLY by its real number, not also by the
    # name string used to index it.
    assert list(j._pins.keys()) == ["1", "2", "3", "4"]
    assert "Pin_1" not in j._pins


@circuit(name="t_repeated_name_connect")
def _repeated_name_connect():
    j = Component(symbol="Connector_Generic:Conn_01x04", ref="J1")
    n = Net("N1")
    j["Pin_1"] += n
    j["Pin_1"] += n  # re-touch the same pin/net on purpose


def test_reassigning_same_pin_does_not_grow_list():
    """Some call sites re-touch the same named pin (e.g. a helper wires it,
    then something else inspects/re-wires it). Repeated name-indexed access
    to the SAME pin must stay a singleton."""
    circ = _repeated_name_connect()
    j = circ._components["J1"]
    assert len(j._pin_names["Pin_1"]) == 1


@circuit(name="t_export_single_node_per_pin")
def _export_single_node_per_pin():
    j1 = Component(symbol="Connector_Generic:Conn_01x04", ref="J1")
    j2 = Component(symbol="Connector_Generic:Conn_01x04", ref="J2")
    n = Net("SIGNAL")
    j1["Pin_1"] += n
    j2["Pin_2"] += n


def test_name_indexed_pin_produces_one_connection_node():
    circ = _export_single_node_per_pin()
    data = NetlistExporter(circ).to_dict()
    nodes = data["nets"]["SIGNAL"]["nodes"]

    assert len(nodes) == 2
    pairs = sorted((node["component"], node["pin"]["number"]) for node in nodes)
    assert pairs == [("J1", "1"), ("J2", "2")]


@circuit(name="t_export_number_indexed_control")
def _export_number_indexed_control():
    r = Component(symbol="Device:R", ref="R1", value="10k")
    n = Net("N2")
    r[1] += n


def test_number_indexed_pin_was_never_affected():
    """Control case: number-indexed passive pins (name '~') were never
    duplicated -- confirm the fix didn't change that."""
    circ = _export_number_indexed_control()
    data = NetlistExporter(circ).to_dict()
    nodes = data["nets"]["N2"]["nodes"]
    assert len(nodes) == 1
