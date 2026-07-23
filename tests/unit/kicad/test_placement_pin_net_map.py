"""Regression test for the missing pin_net_map at the text-flow placement
call site (`schematic_writer.py`'s `place_components`).

Final component positions come from `TextFlowPlacer` (`text_flow_placement.py`),
fed by bounding boxes computed in `schematic_writer.py`. That call site invoked
`SymbolBoundingBoxCalculator.calculate_bounding_box(lib_data, include_properties=True)`
with NO `pin_net_map` -- so every pin, regardless of its real net name, fell
into `_get_pin_bounds`'s "no net match" fallback and was sized as a generic
3-character "XXX" placeholder. A component with genuinely long net names
(e.g. "R_TPMMISO_D", 11 chars) therefore got placed as if its labels were
tiny, leaving no real clearance -- so a neighboring component's labels could
end up overlapping it even though the (undersized) bounding boxes didn't.

This is a *different, deeper* bug than the direction bug already fixed in
`_get_pin_bounds` (angle-to-extend-direction was backwards) -- that fix is
necessary but not sufficient, since it only matters once real net-name
lengths are actually supplied.

`build_ref_to_pin_net_map()` fixes this by constructing a
{component_ref: {pin_number: net_name}} map from the circuit's own net
connections (the same `net.connections` list `_add_pin_level_net_labels`
already uses successfully), for `schematic_writer.py` to pass through to
`calculate_bounding_box()`.

Found investigating a real, user-reported overlap between the Acquisition
Module MCU sheet's SDRAM (U2) and TPM (U6) pin labels (windTunnelProject,
2026-07-22).
"""
from circuit_synth.kicad.sch_gen.symbol_geometry import build_ref_to_pin_net_map


class _FakeNet:
    def __init__(self, name, connections):
        self.name = name
        self.connections = connections


class _FakeCircuit:
    def __init__(self, nets):
        self.nets = nets


def test_builds_per_component_pin_number_to_net_name_map():
    circuit = _FakeCircuit(
        nets=[
            _FakeNet("TPM_MISO", [("U1", "190"), ("U6", "21")]),
            _FakeNet("FMC_D15", [("U1", "80"), ("U2", "A1")]),
            _FakeNet("GND", [("U1", "6"), ("U2", "A3"), ("U6", "2")]),
        ]
    )

    result = build_ref_to_pin_net_map(circuit)

    assert result["U1"] == {"190": "TPM_MISO", "80": "FMC_D15", "6": "GND"}
    assert result["U6"] == {"21": "TPM_MISO", "2": "GND"}
    assert result["U2"] == {"A1": "FMC_D15", "A3": "GND"}


def test_handles_dict_shaped_nets():
    """`circuit.nets` can be a dict (name -> Net) instead of a list, per the
    same dual-shape handling `_add_pin_level_net_labels` already does."""
    circuit = _FakeCircuit(
        nets={"SIG": _FakeNet("SIG", [("J1", "1"), ("J2", "2")])}
    )

    result = build_ref_to_pin_net_map(circuit)

    assert result["J1"] == {"1": "SIG"}
    assert result["J2"] == {"2": "SIG"}


def test_missing_component_returns_empty_map_not_keyerror():
    circuit = _FakeCircuit(nets=[_FakeNet("SIG", [("J1", "1")])])
    result = build_ref_to_pin_net_map(circuit)

    assert result.get("NONEXISTENT", {}) == {}


def test_empty_circuit_returns_empty_map():
    circuit = _FakeCircuit(nets=[])
    assert build_ref_to_pin_net_map(circuit) == {}
