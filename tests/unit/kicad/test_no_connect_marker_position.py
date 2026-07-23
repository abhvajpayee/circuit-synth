"""Regression/unit tests for `SchematicWriter._add_no_connect_markers`.

Exercises the new no-connect marker emission pass in isolation, using the
same lightweight fake-object style as test_placement_pin_net_map.py and the
same rotation-math verification approach as test_pin_label_bounds_direction.py
(multiple pin angles, checked against hand-computed expected coordinates).

Position formula reused, NOT re-derived: `SchematicWriter._pin_xy` already
implements the rotation/mirror-aware pin-tip position used by
`_add_pin_level_net_labels` for hierarchical/local labels (schematic Y is
inverted from symbol-library Y -- see the kicad-sch-api-manual-edit-gotchas
memory and `_pin_xy`'s own docstring). A no-connect marker sits exactly AT
the pin tip (no label offset), so `_add_no_connect_markers` must produce the
same (x, y) `_pin_xy` would for that pin -- this suite checks that directly
against independently hand-computed values at 0/90/180/270 degree component
rotations, not merely that it calls `_pin_xy`.
"""
import math

import pytest

from circuit_synth.kicad.sch_gen.schematic_writer import SchematicWriter
from kicad_sch_api.core.types import Point


class _FakePosition:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class _FakeComponent:
    def __init__(self, ref, lib_id, x, y, rotation):
        self.reference = ref
        self.lib_id = lib_id
        self.position = _FakePosition(x, y)
        self.rotation = rotation


class _FakeComponentManager:
    def __init__(self, components_by_ref):
        self._components = components_by_ref

    def find_component(self, ref):
        return self._components.get(ref)


class _FakeNoConnects:
    def __init__(self):
        self.added = []  # list[Point]

    def add(self, point):
        self.added.append(point)
        return point


class _FakeSchematic:
    def __init__(self):
        self.no_connects = _FakeNoConnects()


class _FakeCircuit:
    def __init__(self, name, no_connect_pins):
        self.name = name
        self.no_connect_pins = no_connect_pins


def _make_writer(circuit, components_by_ref):
    writer = SchematicWriter.__new__(SchematicWriter)
    writer.circuit = circuit
    writer.component_manager = _FakeComponentManager(components_by_ref)
    writer.schematic = _FakeSchematic()
    return writer


def _lib_data_with_pin_at(x, y, angle, number="1"):
    return {"pins": [{"pin_id": number, "number": number, "name": "NC_PIN",
                       "x": x, "y": y, "orientation": angle, "length": 2.54}]}


@pytest.fixture
def patch_symbol_lib(monkeypatch):
    """Patch SymbolLibCache.get_symbol_data to return a caller-controlled
    single-pin symbol, mirroring how test_pin_label_bounds_direction.py
    isolates pin geometry from needing a real KiCad symbol library."""
    from circuit_synth.kicad.sch_gen import schematic_writer as sw_mod

    store = {}

    def _fake_get_symbol_data(lib_id):
        return store.get(lib_id)

    monkeypatch.setattr(sw_mod.SymbolLibCache, "get_symbol_data", _fake_get_symbol_data)
    return store


class TestNoConnectMarkerPosition:
    def test_no_no_connect_pins_adds_nothing(self, patch_symbol_lib):
        circuit = _FakeCircuit("c", [])
        writer = _make_writer(circuit, {})
        writer._add_no_connect_markers()
        assert writer.schematic.no_connects.added == []

    def test_unrotated_component_pin_at_origin_offset(self, patch_symbol_lib):
        # Pin declared at local (2.54, 0) angle 0, component at (10, 20),
        # rotation 0 -> global (12.54, 20).
        patch_symbol_lib["Lib:Part"] = _lib_data_with_pin_at(2.54, 0.0, 0)
        comp = _FakeComponent("U1", "Lib:Part", x=10.0, y=20.0, rotation=0.0)
        circuit = _FakeCircuit("c", [("U1", "1", "reason")])
        writer = _make_writer(circuit, {"U1": comp})

        writer._add_no_connect_markers()

        assert len(writer.schematic.no_connects.added) == 1
        pt = writer.schematic.no_connects.added[0]
        assert pt.x == pytest.approx(12.54)
        assert pt.y == pytest.approx(20.0)

    @pytest.mark.parametrize("rotation", [0.0, 90.0, 180.0, 270.0])
    def test_rotation_aware_position_matches_pin_xy(self, patch_symbol_lib, rotation):
        """Independent hand computation of the rotation formula, checked
        against the marker actually emitted, at all 4 cardinal component
        rotations -- the schematic-Y-inversion gotcha this project already
        hit means getting the sign wrong here silently mis-places every
        no-connect marker on a rotated component."""
        patch_symbol_lib["Lib:Part"] = _lib_data_with_pin_at(3.0, 1.5, 0)
        comp = _FakeComponent("U1", "Lib:Part", x=5.0, y=-7.0, rotation=rotation)
        circuit = _FakeCircuit("c", [("U1", "1", None)])
        writer = _make_writer(circuit, {"U1": comp})

        writer._add_no_connect_markers()

        # Hand-computed expected position: schematic Y is inverted from the
        # symbol-library Y for pin geometry (local_y = -anchor_y), then
        # rotated by the component's rotation, then translated by the
        # component's placed position -- same convention `_pin_xy` and
        # `_add_pin_level_net_labels` both use.
        anchor_x, anchor_y = 3.0, 1.5
        r = math.radians(rotation)
        local_x, local_y = anchor_x, -anchor_y
        rx = (local_x * math.cos(r)) - (local_y * math.sin(r))
        ry = (local_x * math.sin(r)) + (local_y * math.cos(r))
        expected_x = comp.position.x + rx
        expected_y = comp.position.y + ry

        assert len(writer.schematic.no_connects.added) == 1
        pt = writer.schematic.no_connects.added[0]
        assert pt.x == pytest.approx(expected_x)
        assert pt.y == pytest.approx(expected_y)

    def test_multiple_entries_all_added(self, patch_symbol_lib):
        patch_symbol_lib["Lib:Part"] = {
            "pins": [
                {"pin_id": "1", "number": "1", "name": "A", "x": 0.0, "y": 0.0,
                 "orientation": 0, "length": 2.54},
                {"pin_id": "2", "number": "2", "name": "B", "x": 0.0, "y": -2.54,
                 "orientation": 180, "length": 2.54},
            ]
        }
        comp = _FakeComponent("U1", "Lib:Part", x=0.0, y=0.0, rotation=0.0)
        circuit = _FakeCircuit("c", [("U1", "1", None), ("U1", "2", "x")])
        writer = _make_writer(circuit, {"U1": comp})

        writer._add_no_connect_markers()

        assert len(writer.schematic.no_connects.added) == 2

    def test_missing_component_is_skipped_not_raised(self, patch_symbol_lib):
        circuit = _FakeCircuit("c", [("GHOST", "1", None)])
        writer = _make_writer(circuit, {})
        writer._add_no_connect_markers()  # must not raise
        assert writer.schematic.no_connects.added == []

    def test_missing_pin_on_real_component_is_skipped_not_raised(self, patch_symbol_lib):
        patch_symbol_lib["Lib:Part"] = _lib_data_with_pin_at(0.0, 0.0, 0, number="1")
        comp = _FakeComponent("U1", "Lib:Part", x=0.0, y=0.0, rotation=0.0)
        circuit = _FakeCircuit("c", [("U1", "99", None)])
        writer = _make_writer(circuit, {"U1": comp})
        writer._add_no_connect_markers()  # pin "99" doesn't exist -- skip, don't raise
        assert writer.schematic.no_connects.added == []
