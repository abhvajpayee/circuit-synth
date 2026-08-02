"""Tests for netlist-based existing-project connectivity extraction
(wayfinder #63).

Background
----------
The reference preallocator (wayfinder #58/#59) needs the *existing* KiCad
project's per-component pin -> net connectivity in order to compute a
connectivity signature and match it against the new (Python) side.

Historically that connectivity was reconstructed geometrically, by
``APISynchronizer._get_pin_labels()``: compute an "ideal" label position for
each pin from symbol geometry, then search for a label/power-symbol within a
0.5mm tolerance. That reconstruction is the shared root cause of wayfinder
#55/#57/#61 -- each a different way for a pin's real connectivity to be
invisible to a proximity search.

These tests cover the replacement source: KiCad's own exported netlist, which
states ``net -> [(ref, pin), ...]`` directly, with no geometry involved.
"""

import textwrap

import pytest

from circuit_synth.kicad.schematic.netlist_connectivity import (
    normalize_net_name,
    parse_netlist_connectivity,
)


# ---------------------------------------------------------------------------
# Net-name normalization
# ---------------------------------------------------------------------------


class TestNormalizeNetName:
    """KiCad's exported net names must be translated into the same
    vocabulary circuit-synth's Python side uses (``Net.name``, bare)."""

    def test_global_net_keeps_bare_name(self):
        # A net present at the root sheet is exported as "/DGND".
        assert normalize_net_name("/DGND") == "DGND"

    def test_sheet_local_net_strips_hierarchical_path(self):
        # A net local to a sub-sheet carries its full sheet path.
        assert normalize_net_name("/ComputeSetup/MCU/VCAP0") == "VCAP0"

    def test_deeply_nested_local_net(self):
        assert normalize_net_name("/A/B/C/D/SOME_NET") == "SOME_NET"

    def test_unconnected_pin_becomes_none(self):
        # KiCad synthesizes a placeholder name for a genuinely unwired pin.
        # That is *not* a net -- it must map to None so the signature encoder
        # emits its ("NC",) token, exactly as a blank label used to.
        assert normalize_net_name("unconnected-(U1-NC-PadK16)") is None
        assert normalize_net_name("unconnected-(U1-PA0_C-PadT1)") is None

    def test_already_bare_name_passes_through(self):
        assert normalize_net_name("+3V3") == "+3V3"

    def test_none_and_empty(self):
        assert normalize_net_name(None) is None
        assert normalize_net_name("") is None

    def test_circuit_synth_auto_net_survives_normalization(self):
        # circuit-synth's own auto-named nets (N$1, N$2) must still be
        # recognizable as auto-named AFTER normalization, or
        # reference_preallocation.AUTO_NET_RE stops matching and the whole
        # peer-resolution ("ANON") path silently degrades.
        from circuit_synth.core.reference_preallocation import AUTO_NET_RE

        normalized = normalize_net_name("/ComputeSetup/Ethernet/N$1")
        assert normalized == "N$1"
        assert AUTO_NET_RE.match(normalized)


# ---------------------------------------------------------------------------
# Netlist parsing
# ---------------------------------------------------------------------------


MINIMAL_NETLIST = textwrap.dedent(
    """\
    (export
      (version "E")
      (design
        (source "/tmp/Demo.kicad_sch")
        (tool "Eeschema 10.0.4-1.fc44"))
      (components
        (comp (ref "R1") (value "10k")
          (sheetpath (names "/ComputeSetup/MCU/") (tstamps "/aaaa/bbbb/")))
        (comp (ref "C1") (value "100n")
          (sheetpath (names "/") (tstamps "/"))))
      (nets
        (net (code "1") (name "/DGND")
          (node (ref "R1") (pin "2") (pintype "passive"))
          (node (ref "C1") (pin "2") (pintype "passive")))
        (net (code "2") (name "/ComputeSetup/MCU/VCAP0")
          (node (ref "R1") (pin "1") (pintype "passive"))
          (node (ref "C1") (pin "1") (pintype "passive")))
        (net (code "3") (name "unconnected-(R1-NC-Pad3)")
          (node (ref "R1") (pin "3") (pintype "no_connect")))))
    """
)


class TestParseNetlistConnectivity:
    def test_builds_ref_pin_net_mapping(self, tmp_path):
        path = tmp_path / "demo.net"
        path.write_text(MINIMAL_NETLIST)

        conn = parse_netlist_connectivity(path)

        assert conn["R1"]["1"] == "VCAP0"
        assert conn["R1"]["2"] == "DGND"
        assert conn["C1"]["1"] == "VCAP0"
        assert conn["C1"]["2"] == "DGND"

    def test_unconnected_pin_recorded_as_none_not_omitted(self, tmp_path):
        """The pin must be PRESENT with a None net, not missing.

        A missing key and a None value mean different things downstream:
        ComponentRecord.pins carries one entry per pin, and compute_signature
        emits a ("NC",) token for a None net. Dropping the key entirely would
        shorten the signature tuple and could make an unconnected pin
        indistinguishable from a pin that simply wasn't reported.
        """
        path = tmp_path / "demo.net"
        path.write_text(MINIMAL_NETLIST)

        conn = parse_netlist_connectivity(path)

        assert "3" in conn["R1"]
        assert conn["R1"]["3"] is None

    def test_pin_numbers_are_strings(self, tmp_path):
        """ComponentRecord.pins is keyed by string pin id everywhere else."""
        path = tmp_path / "demo.net"
        path.write_text(MINIMAL_NETLIST)

        conn = parse_netlist_connectivity(path)
        assert all(isinstance(k, str) for k in conn["R1"])

    def test_alphanumeric_bga_pin_names(self, tmp_path):
        """BGA pads (K16, T1) must round-trip unchanged -- the real project's
        STM32H755 is a TFBGA240."""
        path = tmp_path / "bga.net"
        path.write_text(
            textwrap.dedent(
                """\
                (export
                  (components
                    (comp (ref "U1") (value "STM32H755")
                      (sheetpath (names "/ComputeSetup/MCU/") (tstamps "/a/b/"))))
                  (nets
                    (net (code "1") (name "/DGND")
                      (node (ref "U1") (pin "K16") (pintype "power_in")))))
                """
            )
        )
        conn = parse_netlist_connectivity(path)
        assert conn["U1"]["K16"] == "DGND"

    def test_missing_file_returns_none(self, tmp_path):
        assert parse_netlist_connectivity(tmp_path / "nope.net") is None

    def test_malformed_file_returns_none(self, tmp_path):
        path = tmp_path / "bad.net"
        path.write_text("(export (nets (net (name ")  # truncated / unbalanced
        assert parse_netlist_connectivity(path) is None


class TestRailWiredPinResolution:
    """Regression guard for the wayfinder #61 class of bug.

    #61's symptom: a pin carrying no label/power-symbol of its own, wired
    instead into the MIDDLE of a shared rail wire, was invisible to
    _get_pin_labels()'s proximity search -- it needed a bespoke wire-graph
    walk (_find_marker_via_wires) to be resolved at all.

    A netlist has no such blind spot: KiCad's own connectivity engine has
    already resolved the rail, so the pin is simply a node on the net. This
    test encodes that structural property -- the parser needs no rail-specific
    code path for a rail-wired pin to come out correct.
    """

    def test_rail_wired_pin_needs_no_special_case(self, tmp_path):
        path = tmp_path / "rail.net"
        path.write_text(
            textwrap.dedent(
                """\
                (export
                  (components
                    (comp (ref "C27") (value "100n")
                      (sheetpath (names "/ComputeSetup/Storage/") (tstamps "/a/b/"))))
                  (nets
                    (net (code "1") (name "/ComputeSetup/Storage/+3V3_GATED_U3")
                      (node (ref "C27") (pin "1") (pintype "passive"))
                      (node (ref "U3") (pin "10") (pintype "power_in")))
                    (net (code "2") (name "/DGND")
                      (node (ref "C27") (pin "2") (pintype "passive")))))
                """
            )
        )
        conn = parse_netlist_connectivity(path)
        assert conn["C27"]["1"] == "+3V3_GATED_U3"
        assert conn["C27"]["2"] == "DGND"


class TestUnconnectedPinSymmetry:
    """The real matching bug this change fixes (wayfinder #63).

    ``Circuit.collect_preallocation_records()`` (the *new*/Python side) builds
    its pin dict from ``comp._pins`` -- i.e. EVERY pin of the symbol, with
    ``None`` for any pin that has no net. ``_get_pin_labels()`` (the old
    geometric *existing* side) returns an entry only for a pin where it
    actually found a label, so an unconnected pin is simply absent from the
    dict.

    That asymmetry means any component carrying unconnected pins gets a
    shorter signature tuple on the existing side than on the Python side, so
    the two can never be equal and the component never matches -- it loses
    its stable reference designator on every incremental sync, breaking the
    UUID anchor to its manually-placed PCB footprint.

    Measured on the real acquisition_mcu project, this silently affected six
    of the most placement-critical parts on the board -- the SDRAM (U2), TPM
    (U3), eMMC (U4), WiFi module (U5), Ethernet switch (U6) and RJ45 (J3) --
    every one of which has unused/NC pins. Sourcing connectivity from the
    netlist fixes it, because KiCad reports an unconnected pin explicitly
    (as ``unconnected-(...)``, normalized here to ``None``) rather than
    omitting it.
    """

    def test_signature_matches_python_side_when_pins_are_unconnected(self, tmp_path):
        from circuit_synth.core.reference_preallocation import (
            ComponentRecord,
            build_global_net_map,
            compute_signature,
        )

        path = tmp_path / "nc.net"
        path.write_text(
            textwrap.dedent(
                """\
                (export
                  (components
                    (comp (ref "U2") (value "IS42S16160J SDRAM")
                      (sheetpath (names "/ComputeSetup/DRAM/") (tstamps "/a/b/"))))
                  (nets
                    (net (code "1") (name "/DGND")
                      (node (ref "U2") (pin "1") (pintype "power_in")))
                    (net (code "2") (name "unconnected-(U2-NC-Pad2)")
                      (node (ref "U2") (pin "2") (pintype "no_connect")))))
                """
            )
        )
        existing_pins = parse_netlist_connectivity(path)["U2"]

        # What the Python side produces for the same part: every pin present,
        # unconnected one carried as None.
        python_pins = {"1": "DGND", "2": None}

        assert existing_pins == python_pins

        existing_rec = ComponentRecord(
            identity="U2", prefix="U", value="IS42S16160J SDRAM", pins=existing_pins
        )
        python_rec = ComponentRecord(
            identity="new", prefix="U", value="IS42S16160J SDRAM", pins=python_pins
        )
        assert compute_signature(
            existing_rec, build_global_net_map([existing_rec])
        ) == compute_signature(python_rec, build_global_net_map([python_rec]))

    def test_omitting_unconnected_pin_breaks_the_signature(self):
        """Guards the inverse: this is what the geometric path did wrong.

        Encoded explicitly so a future change that starts dropping
        None-valued pins again fails loudly here rather than silently
        regressing reference stability.
        """
        from circuit_synth.core.reference_preallocation import (
            ComponentRecord,
            build_global_net_map,
            compute_signature,
        )

        full = ComponentRecord(
            identity="U2", prefix="U", value="X", pins={"1": "DGND", "2": None}
        )
        omitted = ComponentRecord(
            identity="U2", prefix="U", value="X", pins={"1": "DGND"}
        )
        assert compute_signature(
            full, build_global_net_map([full])
        ) != compute_signature(omitted, build_global_net_map([omitted]))


class TestAliasedBusNamesAreCanonical:
    """A netlist reports an aliased bus member by its CANONICAL name.

    wayfinder #59 had to build an alias -> canonical map because
    _get_pin_labels() reads the on-disk label text, which bus_emit.py
    rewrites in place from the canonical positional name (ETH_9) to the
    elaborated alias (ETH_NRST). KiCad's netlist is not affected by that
    cosmetic retexting -- it resolves the bus member to its canonical vector
    net -- so the netlist side already speaks the same vocabulary as the
    Python side (Net.name), with no alias translation needed.
    """

    def test_bus_member_reported_canonically(self, tmp_path):
        path = tmp_path / "bus.net"
        path.write_text(
            textwrap.dedent(
                """\
                (export
                  (components
                    (comp (ref "C55") (value "100n")
                      (sheetpath (names "/ComputeSetup/Ethernet/") (tstamps "/a/b/"))))
                  (nets
                    (net (code "1") (name "/ComputeSetup/Ethernet/ETH_9")
                      (node (ref "C55") (pin "1") (pintype "passive")))))
                """
            )
        )
        conn = parse_netlist_connectivity(path)
        # canonical positional name, NOT the elaborated alias "ETH_NRST"
        assert conn["C55"]["1"] == "ETH_9"


class TestGeometricFallbackRetained:
    """The geometric path must remain reachable (wayfinder #63 design
    decision), not be replaced outright.

    circuit-synth's sync had no hard runtime dependency on the ``kicad-cli``
    binary before this change. If a missing kicad-cli made preallocation fail
    instead of degrade, every reference would renumber and every PCB footprint
    anchor would break -- the exact failure wayfinder #58 exists to prevent.
    """

    def test_export_returns_none_when_kicad_cli_missing(self, tmp_path, monkeypatch):
        from circuit_synth.kicad.schematic import netlist_connectivity as nc

        sch = tmp_path / "Demo.kicad_sch"
        sch.write_text("(kicad_sch)")
        monkeypatch.setattr(nc.shutil, "which", lambda _name: None)

        assert nc.export_netlist(sch) is None
        assert nc.load_project_connectivity(sch) is None

    def test_existing_records_fall_back_to_pin_labels(self):
        """With no netlist connectivity supplied, records still come out --
        via _get_pin_labels() -- rather than being empty."""
        from circuit_synth.kicad.schematic.reference_preallocator import (
            _existing_records_for_sheet,
        )

        class FakeLabel:
            def __init__(self, text):
                self.text = text

        class FakeComp:
            reference = "R1"
            value = "10k"

        class FakeSchematic:
            components = [FakeComp()]

        class FakeSync:
            schematic = FakeSchematic()

            def _get_pin_labels(self, _comp):
                return {"1": (FakeLabel("VCAP0"), "regular"),
                        "2": (FakeLabel("DGND"), "regular")}

        records = _existing_records_for_sheet(FakeSync(), alias_map={}, netlist_connectivity=None)

        assert len(records) == 1
        assert records[0].identity == "R1"
        assert records[0].pins == {"1": "VCAP0", "2": "DGND"}

    def test_netlist_takes_precedence_over_pin_labels(self):
        """When both sources are available the netlist wins."""
        from circuit_synth.kicad.schematic.reference_preallocator import (
            _existing_records_for_sheet,
        )

        class FakeLabel:
            def __init__(self, text):
                self.text = text

        class FakeComp:
            reference = "R1"
            value = "10k"

        class FakeSchematic:
            components = [FakeComp()]

        class FakeSync:
            schematic = FakeSchematic()

            def _get_pin_labels(self, _comp):
                return {"1": (FakeLabel("STALE_GEOMETRIC"), "regular")}

        records = _existing_records_for_sheet(
            FakeSync(),
            alias_map={},
            netlist_connectivity={"R1": {"1": "VCAP0", "2": None}},
        )

        assert records[0].pins == {"1": "VCAP0", "2": None}

    def test_component_absent_from_netlist_falls_back_per_component(self):
        """A component missing from the netlist uses the geometric path for
        that component only -- the rest still use the netlist."""
        from circuit_synth.kicad.schematic.reference_preallocator import (
            _existing_records_for_sheet,
        )

        class FakeLabel:
            def __init__(self, text):
                self.text = text

        class C1:
            reference = "C1"
            value = "100n"

        class R1:
            reference = "R1"
            value = "10k"

        class FakeSchematic:
            components = [C1(), R1()]

        class FakeSync:
            schematic = FakeSchematic()

            def _get_pin_labels(self, comp):
                return {"1": (FakeLabel(f"GEO_{comp.reference}"), "regular")}

        records = {
            r.identity: r
            for r in _existing_records_for_sheet(
                FakeSync(), alias_map={}, netlist_connectivity={"R1": {"1": "FROM_NETLIST"}}
            )
        }

        assert records["R1"].pins == {"1": "FROM_NETLIST"}
        assert records["C1"].pins == {"1": "GEO_C1"}
