"""Unit tests for circuit_loader.py's no-connect pin extraction.

`Component.to_dict()` (core/component.py) puts a `no_connect`/
`no_connect_reason` pair on every pin dict it emits. `_parse_circuit`
(circuit_loader.py) is the only place that JSON is read back on the way
into `SchematicWriter` -- it must collect the no-connect pins into
`Circuit.no_connect_pins` as `(ref, pin_identifier, reason)` tuples, using
the SAME identifier-resolution precedence (pin number over name, skipping
empty/"~") that net-connection parsing already uses just below it in the
same function -- so a no-connect entry and a net-connection entry for the
same physical pin always agree on how to name it.
"""
from circuit_synth.kicad.sch_gen.circuit_loader import _parse_circuit


def _comp(ref, symbol="Device:R", pins=None):
    return {"ref": ref, "symbol": symbol, "value": "10k", "pins": pins or []}


def _circuit_data(comps=None, nets=None):
    return {"name": "C", "components": comps or [], "nets": nets or {}}


def _pin(pin_id="1", name="~", no_connect=False, reason=None, x=0.0, y=0.0,
         orientation=0.0, func="passive"):
    return {
        "pin_id": pin_id,
        "name": name,
        "func": func,
        "x": x,
        "y": y,
        "length": 2.54,
        "orientation": orientation,
        "no_connect": no_connect,
        "no_connect_reason": reason,
    }


class TestNoConnectExtraction:
    def test_no_connect_pin_collected(self):
        data = _circuit_data(comps=[
            _comp("U1", pins=[
                _pin("1", no_connect=True, reason="RFU"),
                _pin("2", no_connect=False),
            ])
        ])
        c = _parse_circuit(data, {})
        assert c.no_connect_pins == [("U1", "1", "RFU")]

    def test_reason_optional(self):
        data = _circuit_data(comps=[
            _comp("U1", pins=[_pin("3", no_connect=True, reason=None)])
        ])
        c = _parse_circuit(data, {})
        assert c.no_connect_pins == [("U1", "3", None)]

    def test_multiple_pins_and_components(self):
        data = _circuit_data(comps=[
            _comp("U1", pins=[
                _pin("1", no_connect=True, reason="a"),
                _pin("2", no_connect=True, reason="b"),
            ]),
            _comp("U2", pins=[_pin("5", no_connect=True, reason="c")]),
        ])
        c = _parse_circuit(data, {})
        assert set(c.no_connect_pins) == {
            ("U1", "1", "a"), ("U1", "2", "b"), ("U2", "5", "c"),
        }

    def test_connected_pins_not_collected(self):
        data = _circuit_data(comps=[
            _comp("U1", pins=[_pin("1", no_connect=False)])
        ])
        c = _parse_circuit(data, {})
        assert c.no_connect_pins == []

    def test_empty_circuit_has_empty_list(self):
        c = _parse_circuit(_circuit_data(), {})
        assert c.no_connect_pins == []

    def test_identifier_prefers_pin_number_over_name(self):
        """Matches the precedence net-connection parsing uses just below --
        a numbered pin must key off its number, not its (possibly
        function-shared) name."""
        data = _circuit_data(comps=[
            _comp("U1", pins=[
                _pin(pin_id="7", name="VDD", no_connect=True, reason="x"),
            ])
        ])
        c = _parse_circuit(data, {})
        assert c.no_connect_pins == [("U1", "7", "x")]

    def test_identifier_falls_back_to_name_when_number_empty_or_tilde(self):
        data = _circuit_data(comps=[
            _comp("U1", pins=[
                _pin(pin_id="~", name="EN", no_connect=True, reason="x"),
            ])
        ])
        c = _parse_circuit(data, {})
        assert c.no_connect_pins == [("U1", "EN", "x")]
