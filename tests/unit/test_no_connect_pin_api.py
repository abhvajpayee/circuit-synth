"""Unit tests for Pin.no_connect() -- the native no-connect API (promoted from
a board-local `_nc()`/NC_REGISTRY workaround, windTunnelProject, 2026-07-23).

Covers the core/pin.py + core/component.py contract only:
  - marking a pin no_connect() sets state and is readable back
  - a reason string is stored and round-trips through to_dict()
  - the two conflicting orders (connect-then-no_connect,
    no_connect-then-connect) are BOTH rejected, not silently accepted
  - Component.to_dict() surfaces the no_connect/no_connect_reason fields
    per pin, since that JSON is circuit_loader's only channel into
    schematic_writer's marker-emission pass

Schematic-generation-level behavior (the actual `(no_connect ...)` element
and its rotation/mirror-aware position) is covered separately in
tests/unit/kicad/test_no_connect_marker_position.py and the real-project
kicad-cli ERC check in tests/unit/kicad/test_no_connect_erc.py.
"""
import pytest

from circuit_synth import Component, Net, circuit
from circuit_synth.core.exception import ComponentError


def _conn():
    """A stock 4-pin connector -- all passive pins, cheap to instantiate,
    same fixture symbol used by test_name_indexed_pin_dedup.py."""
    return Component(symbol="Connector_Generic:Conn_01x04", ref="J1")


class TestNoConnectMarking:
    def test_no_connect_sets_state(self):
        @circuit(name="t_nc_basic")
        def _c():
            j = _conn()
            j[1].no_connect()

        c = _c()
        pin = c._components["J1"]["1"]
        assert pin.is_no_connect is True
        assert pin.no_connect_reason is None
        assert pin.net is None
        assert pin.connected is False

    def test_no_connect_stores_reason(self):
        @circuit(name="t_nc_reason")
        def _c():
            j = _conn()
            j[2].no_connect(reason="RFU per datasheet section 4")

        c = _c()
        pin = c._components["J1"]["2"]
        assert pin.is_no_connect is True
        assert pin.no_connect_reason == "RFU per datasheet section 4"

    def test_no_connect_returns_pin_for_chaining(self):
        @circuit(name="t_nc_chain")
        def _c():
            j = _conn()
            result = j[3].no_connect(reason="spare")
            assert result is j[3]

        _c()

    def test_unrelated_pins_are_not_marked(self):
        @circuit(name="t_nc_scoped")
        def _c():
            j = _conn()
            n = Net("N1")
            j[1] += n
            j[2].no_connect()

        c = _c()
        assert c._components["J1"]["1"].is_no_connect is False
        assert c._components["J1"]["2"].is_no_connect is True


class TestNoConnectConflictRejected:
    """A pin cannot be both wired and no-connect -- either order raises
    immediately at the point of the conflicting call, per the API design
    decision (raise at generation time was rejected in favor of catching
    the contradiction at the exact call site that creates it)."""

    def test_connect_then_no_connect_raises(self):
        @circuit(name="t_nc_conflict_a")
        def _c():
            j = _conn()
            n = Net("N1")
            j[1] += n
            with pytest.raises(ComponentError, match="already connected"):
                j[1].no_connect()

        _c()

    def test_no_connect_then_connect_raises(self):
        @circuit(name="t_nc_conflict_b")
        def _c():
            j = _conn()
            n = Net("N1")
            j[1].no_connect(reason="unused")
            with pytest.raises(ComponentError, match="already marked no_connect"):
                j[1] += n

        _c()

    def test_no_connect_then_connect_leaves_pin_unconnected(self):
        """The rejected `+=` must not partially apply."""
        @circuit(name="t_nc_conflict_c")
        def _c():
            j = _conn()
            n = Net("N1")
            j[1].no_connect()
            with pytest.raises(ComponentError):
                j[1] += n

        c = _c()
        pin = c._components["J1"]["1"]
        assert pin.net is None
        assert pin.is_no_connect is True

    def test_pin_to_pin_connect_also_rejected(self):
        """`pin += other_pin` routes through the same connect_to_net() guard."""
        @circuit(name="t_nc_conflict_pin_to_pin")
        def _c():
            j1 = _conn()
            j2 = Component(symbol="Connector_Generic:Conn_01x04", ref="J2")
            j1[1].no_connect()
            with pytest.raises(ComponentError):
                j1[1] += j2[1]

        _c()


class TestNoConnectToDict:
    def test_to_dict_marks_the_right_pin_only(self):
        @circuit(name="t_nc_dict")
        def _c():
            j = _conn()
            n = Net("N1")
            j[1] += n
            j[2].no_connect(reason="unused per BOM note")

        c = _c()
        comp_dict = c._components["J1"].to_dict()
        pins_by_id = {p["pin_id"]: p for p in comp_dict["pins"]}

        assert pins_by_id["1"]["no_connect"] is False
        assert pins_by_id["1"]["no_connect_reason"] is None

        assert pins_by_id["2"]["no_connect"] is True
        assert pins_by_id["2"]["no_connect_reason"] == "unused per BOM note"

        # Untouched pins default to not-no-connect.
        assert pins_by_id["3"]["no_connect"] is False
        assert pins_by_id["4"]["no_connect"] is False

    def test_pin_to_dict_includes_no_connect_fields(self):
        @circuit(name="t_nc_pin_dict")
        def _c():
            j = _conn()
            j[1].no_connect(reason="r")

        c = _c()
        d = c._components["J1"]["1"].to_dict()
        assert d["no_connect"] is True
        assert d["no_connect_reason"] == "r"
