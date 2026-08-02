"""
Real-KiCad-file end-to-end tests for reference preallocation (wayfinder
#58), exercising the full pipeline through Circuit.generate_kicad_project()
against a real, disposable (tmp_path) multi-sheet project -- not the live
acquisition_mcu board.

These complement tests/unit/test_reference_preallocation.py (pure
algorithm) and tests/unit/test_circuit_reference_preallocation.py
(Circuit/Component wiring, hand-built "existing" snapshot) by proving the
real KiCad-I/O adapter (kicad.schematic.reference_preallocator) -- sheet
discovery + per-pin label/power-symbol lookup via
APISynchronizer._get_pin_labels() -- actually reproduces the two ticket
scenarios end to end, using kicad_sch_api to read back the regenerated
project's real .kicad_sch files.
"""

import logging

import kicad_sch_api as ksa
import pytest

from circuit_synth import Component, Net, circuit
from circuit_synth.core.decorators import set_current_circuit

logging.getLogger("circuit_synth").setLevel(logging.WARNING)


def _refs_and_values(sch) -> dict:
    return {c.reference: c.value for c in sch.components if not c.reference.startswith("#")}


def _start_fresh_top_level_build():
    """Each of the two "generations" built within a single test function
    must simulate what two genuinely separate process invocations of
    ``python3 board.py`` would each see: NO active/parent circuit at all,
    so the resulting top-level @circuit call becomes a true root with its
    own fresh ReferenceManager. Without this, tests/conftest.py's
    session-wide ``mock_active_circuit`` autouse fixture (which sets one
    shared dummy "active circuit" for the whole test function, so that ad
    hoc Component(...) calls elsewhere don't need their own circuit
    context) would silently parent BOTH generations' entire trees onto
    that one shared dummy circuit -- causing them to share a single
    reference-manager and collide over reference strings that, in real
    usage, belong to two completely independent process runs with no
    shared state whatsoever."""
    set_current_circuit(None)


def test_mid_sheet_insertion_does_not_cascade_across_sheets(tmp_path):
    """The original incident (wayfinder #55->#58's motivating bug): adding
    a component to one sheet must not renumber, or worse delete+recreate,
    an unrelated sheet's own components -- and must survive the new
    component being spliced in the *middle* of its own sheet's source."""
    project_dir = tmp_path / "proj"
    _start_fresh_top_level_build()

    @circuit(name="SheetA")
    def sheet_a_v1():
        gnd = Net("GND")
        vcc = Net("VCC")
        r = Component(symbol="Device:R", ref="R", value="10k")
        r[1] += vcc
        r[2] += gnd

    @circuit(name="SheetB")
    def sheet_b():
        gnd = Net("GND")
        sig = Net("SIG_B")
        r = Component(symbol="Device:R", ref="R", value="4k7")
        r[1] += sig
        r[2] += gnd

    @circuit(name="root")
    def root_v1():
        sheet_a_v1()
        sheet_b()

    c1 = root_v1()
    result1 = c1.generate_kicad_project(
        str(project_dir), generate_pcb=False, force_regenerate=False, update_source_refs=False
    )
    assert result1["success"]

    sch_a_before = ksa.Schematic.load(str(project_dir / "SheetA.kicad_sch"))
    sch_b_before = ksa.Schematic.load(str(project_dir / "SheetB.kicad_sch"))
    a_ref_before = _refs_and_values(sch_a_before)
    b_ref_before = _refs_and_values(sch_b_before)
    assert len(a_ref_before) == 1
    assert len(b_ref_before) == 1
    r10k_ref = next(iter(a_ref_before))
    r4k7_ref = next(iter(b_ref_before))

    # --- second generation: a new resistor spliced BEFORE the existing one
    # in SheetA's own source; SheetB's own source is untouched. ---
    _start_fresh_top_level_build()

    @circuit(name="SheetA")
    def sheet_a_v2():
        gnd = Net("GND")
        vcc = Net("VCC")
        sig_new = Net("SIG_NEW")
        r_new = Component(symbol="Device:R", ref="R", value="1k")  # <-- inserted first
        r_new[1] += sig_new
        r_new[2] += gnd
        r = Component(symbol="Device:R", ref="R", value="10k")
        r[1] += vcc
        r[2] += gnd

    @circuit(name="root")
    def root_v2():
        sheet_a_v2()
        sheet_b()

    c2 = root_v2()
    result2 = c2.generate_kicad_project(
        str(project_dir), generate_pcb=False, force_regenerate=False, update_source_refs=False
    )
    assert result2["success"]

    sch_a_after = ksa.Schematic.load(str(project_dir / "SheetA.kicad_sch"))
    sch_b_after = ksa.Schematic.load(str(project_dir / "SheetB.kicad_sch"))
    a_after = _refs_and_values(sch_a_after)
    b_after = _refs_and_values(sch_b_after)

    assert a_after.get(r10k_ref) == "10k", "the pre-existing SheetA resistor must keep its old reference"
    assert b_after == b_ref_before, "SheetB must be completely untouched by SheetA's edit"

    new_refs = [ref for ref, val in a_after.items() if val == "1k"]
    assert len(new_refs) == 1
    assert new_refs[0] not in (r10k_ref, r4k7_ref), "the new component must get a fresh reference"


def test_duplicate_zero_ohm_ties_real_project(tmp_path):
    """Real-file version of the 4x0R AGND/DGND tiebreak scenario: adding a
    5th duplicate-connectivity tie resistor must preserve all 4 existing
    references and assign the 5th a genuinely new one."""
    project_dir = tmp_path / "proj"
    _start_fresh_top_level_build()

    @circuit(name="Ties")
    def ties_v1():
        agnd = Net("AGND")
        dgnd = Net("DGND")
        for _ in range(4):
            r = Component(symbol="Device:R", ref="R", value="0")
            r[1] += agnd
            r[2] += dgnd

    @circuit(name="root")
    def root_v1():
        ties_v1()

    c1 = root_v1()
    result1 = c1.generate_kicad_project(
        str(project_dir), generate_pcb=False, force_regenerate=False, update_source_refs=False
    )
    assert result1["success"]
    sch_before = ksa.Schematic.load(str(project_dir / "Ties.kicad_sch"))
    refs_before = set(_refs_and_values(sch_before).keys())
    assert len(refs_before) == 4

    _start_fresh_top_level_build()

    @circuit(name="Ties")
    def ties_v2():
        agnd = Net("AGND")
        dgnd = Net("DGND")
        for _ in range(5):
            r = Component(symbol="Device:R", ref="R", value="0")
            r[1] += agnd
            r[2] += dgnd

    @circuit(name="root")
    def root_v2():
        ties_v2()

    c2 = root_v2()
    result2 = c2.generate_kicad_project(
        str(project_dir), generate_pcb=False, force_regenerate=False, update_source_refs=False
    )
    assert result2["success"]
    sch_after = ksa.Schematic.load(str(project_dir / "Ties.kicad_sch"))
    refs_after = set(_refs_and_values(sch_after).keys())

    assert refs_before.issubset(refs_after), "all 4 pre-existing tie resistors must keep their references"
    assert len(refs_after) == 5
