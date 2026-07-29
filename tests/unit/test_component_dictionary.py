"""
Unit tests for Component documentation-metadata (`doc=`) and the generic
component-dictionary export feature.

Covers:
- Component(..., doc={...}) constructor field: storage, default, backward
  compatibility, to_dict()/from_dict() round trip.
- export_component_dictionary(circuit): recursive hierarchy walk, keyed by
  reference, only components with doc metadata are included.
"""

import pytest

from circuit_synth.core import Circuit, Component
from circuit_synth.core.component_dictionary import export_component_dictionary
from circuit_synth.core.decorators import get_current_circuit, set_current_circuit


class TestComponentDocField:
    """Test the Component.doc metadata field."""

    def test_doc_defaults_to_none(self):
        """Components created without doc= should be unaffected (backward compatible)."""
        r1 = Component("Device:R", ref="R", value="10k")
        assert r1.doc is None

    def test_doc_stores_structured_metadata(self):
        """doc= accepts a dict of freeform string keys (what/why/reference convention)."""
        u1 = Component(
            "Device:R",
            ref="R",
            value="2.2k",
            doc={
                "what": "TPM I2C-select pull-down",
                "why": "DNP -- populate for I2C rework, datasheet Fig. 4",
                "reference": "ADR-0024",
            },
        )
        assert u1.doc == {
            "what": "TPM I2C-select pull-down",
            "why": "DNP -- populate for I2C rework, datasheet Fig. 4",
            "reference": "ADR-0024",
        }

    def test_doc_round_trips_through_to_dict_from_dict(self):
        """to_dict()/from_dict() must preserve doc metadata. from_dict()
        re-registers a component under the same final ref, so it must not
        run against the same active circuit as the original (a real
        reference collision, not a bug in doc round-tripping) -- match the
        pattern any from_dict reload would need."""
        r1 = Component(
            "Device:R", ref="R1", value="10k",
            doc={"what": "pull-up", "why": "idle-high strap"},
        )
        data = r1.to_dict()
        assert data["doc"] == {"what": "pull-up", "why": "idle-high strap"}

        old_circuit = get_current_circuit()
        set_current_circuit(None)
        try:
            r2 = Component.from_dict(data)
        finally:
            set_current_circuit(old_circuit)
        assert r2.doc == {"what": "pull-up", "why": "idle-high strap"}

    def test_doc_none_round_trips_as_none(self):
        r1 = Component("Device:R", ref="R1", value="10k")
        data = r1.to_dict()

        old_circuit = get_current_circuit()
        set_current_circuit(None)
        try:
            r2 = Component.from_dict(data)
        finally:
            set_current_circuit(old_circuit)
        assert r2.doc is None


class TestExportComponentDictionary:
    """Test the generic export_component_dictionary() library function."""

    def test_empty_circuit_returns_empty_dict(self):
        circuit = Circuit("Empty")
        assert export_component_dictionary(circuit) == {}

    def test_only_components_with_doc_are_included(self):
        circuit = Circuit("Test")
        r1 = Component("Device:R", ref="R", value="10k")  # no doc
        r2 = Component(
            "Device:R", ref="R", value="4.7k",
            doc={"what": "SPI clock pull-up", "why": "idle-high per datasheet"},
        )
        circuit.add_component(r1)
        circuit.add_component(r2)
        circuit.finalize_references()

        out = export_component_dictionary(circuit)
        assert set(out.keys()) == {"R2"}
        assert out["R2"]["doc"]["what"] == "SPI clock pull-up"
        assert out["R2"]["symbol"] == "Device:R"
        assert out["R2"]["value"] == "4.7k"

    def test_recurses_into_subcircuits(self):
        parent = Circuit("Parent")
        child = Circuit("Child")
        parent.add_subcircuit(child)

        u1 = Component(
            "Device:R", ref="U", value="TPM2.0",
            doc={"what": "TPM security element", "reference": "ADR-0024"},
        )
        child.add_component(u1)
        child.finalize_references()

        out = export_component_dictionary(parent)
        assert "U1" in out
        assert out["U1"]["sheet"] == "Parent/Child"
        assert out["U1"]["doc"]["reference"] == "ADR-0024"

    def test_keys_are_sortable_by_reference_designator(self):
        """Export keys should sort naturally (letter prefix, then number) -- callers
        (e.g. the markdown doc generator) rely on this for ascending-by-ref output."""
        circuit = Circuit("Test")
        for ref, doc in [
            ("R10", {"what": "b"}), ("R2", {"what": "a"}), ("C1", {"what": "c"}),
        ]:
            comp = Component("Device:R", ref=ref, value="1k", doc=doc)
            circuit.add_component(comp)

        out = export_component_dictionary(circuit)
        import re

        def sort_key(ref):
            m = re.match(r"([A-Za-z_]+)(\d+)", ref)
            return (m.group(1), int(m.group(2))) if m else (ref, 0)

        ordered = sorted(out.keys(), key=sort_key)
        assert ordered == ["C1", "R2", "R10"]
