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

from circuit_synth import Bus, Component, Net, circuit
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


def test_aliased_bus_unique_connectivity_matcher_gap(tmp_path):
    """wayfinder #59: R26-class miss -- a component connected to an ALIASED
    Bus member must still be identity-matched on resync even though its own
    connectivity is completely unique (no duplicate-signature tiebreak
    involved at all -- this is deliberately NOT #58's already-covered 4x0R
    tiebreak scenario above).

    Root cause: ``bus_emit.py``'s aliased-Bus dual-label convention retexts
    a tapped pin's label from the bus member's canonical/positional net name
    (``SIG_0``) to its elaborated alias (``SIG_A``) *in place*, for
    on-schematic readability -- so the *existing* (old) side's connectivity
    extraction (``reference_preallocator._existing_records_for_sheet()``,
    via ``APISynchronizer._get_pin_labels()``) sees the alias text, while
    the *new* (Python) side's own signature always uses the canonical
    ``pin.net.name`` (``core/circuit.py``'s ``collect_preallocation_records``).
    Every aliased-bus-connected pin's token therefore NEVER equalled across
    the two sides, so no aliased-bus-connected component could ever
    preallocation-match, deterministically -- regardless of how unique its
    own connectivity otherwise was.

    (In the real ``acquisition_mcu`` board this presented as a
    seemingly-arbitrary "R25 matches, R26 doesn't" asymmetry between two
    structurally-identical series-termination resistors, because a handful
    of components got incidentally rescued by a *separate*, coarser legacy
    fuzzy matcher elsewhere in the sync pipeline (``_match_components``'s
    own position/connectivity-tracer strategies) that succeeds
    unpredictably -- exactly the unreliable fallback behavior issue #58 was
    chartered to eliminate. This test isolates the deterministic root cause
    directly, without depending on that second matcher's own luck: BOTH
    resistors below are shown to lose identity against the parent commit,
    not just one.)

    Two series resistors tap two DIFFERENT members of one aliased Bus, each
    with its own uniquely-named driver-side net (mirroring this project's
    real ``_ser()`` house pattern of ``Net(f"{ref}_D")`` -> R -> bus
    member) -- genuinely unique, non-duplicate connectivity on both sides.
    An unrelated component inserted into an earlier-traversed sheet on the
    second generation forces the classic global-counter renumbering cascade
    (the #55/#58 trigger). Both series resistors must keep their original
    reference AND UUID.
    """
    project_dir = tmp_path / "proj"
    _start_fresh_top_level_build()

    @circuit(name="Early")
    def early_v1():
        gnd = Net("GND")
        vcc = Net("VCC")
        r = Component(symbol="Device:R", ref="R", value="10k")
        r[1] += vcc
        r[2] += gnd

    @circuit(name="Main")
    def main():
        gnd = Net("GND")
        bus = Bus("SIG", members=["A", "B"])
        drv_a = Net("R_A_D")
        ra = Component(symbol="Device:R", ref="R", value="33R")
        ra[1] += drv_a
        ra[2] += bus["A"]
        drv_b = Net("R_B_D")
        rb = Component(symbol="Device:R", ref="R", value="33R")
        rb[1] += drv_b
        rb[2] += bus["B"]

    @circuit(name="root")
    def root_v1():
        early_v1()
        main()

    c1 = root_v1()
    result1 = c1.generate_kicad_project(
        str(project_dir), generate_pcb=False, force_regenerate=False, update_source_refs=False
    )
    assert result1["success"]

    sch_main_before = ksa.Schematic.load(str(project_dir / "Main.kicad_sch"))
    before_by_uuid = {
        c.uuid: c.reference for c in sch_main_before.components if not c.reference.startswith("#")
    }
    assert len(before_by_uuid) == 2, "expected exactly the two series resistors on Main"

    # --- second generation: an unrelated component inserted into the
    # EARLIER-traversed "Early" sheet -- Main's own source is byte-for-byte
    # unchanged, but the global per-prefix "R" counter shifts underneath it.
    # ---
    _start_fresh_top_level_build()

    @circuit(name="Early")
    def early_v2():
        gnd = Net("GND")
        vcc = Net("VCC")
        sig_new = Net("SIG_NEW")
        r_new = Component(symbol="Device:R", ref="R", value="1k")  # <-- new, shifts the counter
        r_new[1] += sig_new
        r_new[2] += gnd
        r = Component(symbol="Device:R", ref="R", value="10k")
        r[1] += vcc
        r[2] += gnd

    @circuit(name="root")
    def root_v2():
        early_v2()
        main()

    c2 = root_v2()
    result2 = c2.generate_kicad_project(
        str(project_dir), generate_pcb=False, force_regenerate=False, update_source_refs=False
    )
    assert result2["success"]

    sch_main_after = ksa.Schematic.load(str(project_dir / "Main.kicad_sch"))
    after_by_uuid = {
        c.uuid: c.reference for c in sch_main_after.components if not c.reference.startswith("#")
    }
    assert len(after_by_uuid) == 2, "Main's own two resistors must not multiply or vanish"

    for old_uuid, old_ref in before_by_uuid.items():
        assert old_uuid in after_by_uuid, (
            f"component {old_ref} (uuid {old_uuid}) lost its identity across resync -- "
            "an aliased-bus-connected component with unique connectivity failed to "
            "preallocation-match (wayfinder #59 R26-class miss)"
        )
        assert after_by_uuid[old_uuid] == old_ref, (
            f"component {old_ref} kept its UUID but was renumbered to "
            f"{after_by_uuid[old_uuid]} -- preallocation matched it to the wrong reference"
        )


def _bank_sheet_snapshot(project_dir):
    """Extract the existing-side connectivity signatures for the Banks sheet,
    exactly as the preallocation matcher itself sees them."""
    from circuit_synth.kicad.schematic.reference_preallocator import (
        build_existing_project_snapshot,
    )

    pro_files = sorted(project_dir.glob("*.kicad_pro"))
    assert pro_files, f"no .kicad_pro generated under {project_dir}"
    per_sheet, _global_map = build_existing_project_snapshot(str(pro_files[0]))
    return {r.identity: r.pins for r in per_sheet["Banks"]}


def _build_bank_project(project_dir, extra_early_component: bool):
    """Generate a project whose 'Banks' sheet holds a resistor_bank pull-up
    array and a cap_bank decoupling array -- the two circuit-synth renderers
    that express a member pin's connectivity as a shared RAIL WIRE rather
    than a per-pin label/power symbol."""
    from circuit_synth import cap_bank, resistor_bank

    _start_fresh_top_level_build()

    @circuit(name="Early")
    def early():
        gnd = Net("GND")
        vcc = Net("VCC")
        if extra_early_component:
            sig_new = Net("SIG_NEW")
            r_new = Component(symbol="Device:R", ref="R", value="1k")
            r_new[1] += sig_new
            r_new[2] += gnd
        r = Component(symbol="Device:R", ref="R", value="10k")
        r[1] += vcc
        r[2] += gnd

    @circuit(name="Banks")
    def banks():
        v3v3 = Net("+3V3")
        gnd = Net("GND")
        pus = []
        for signal in ("SD_CMD", "SD_D0", "SD_D1"):
            r = Component(symbol="Device:R", ref="R", value="47k")
            r[1] += v3v3
            r[2] += Net(signal)
            pus.append(r)
        resistor_bank(pus, "SD_PU", common=v3v3)

        caps = []
        for _ in range(4):
            c = Component(symbol="Device:C", ref="C", value="100n")
            c[1] += v3v3
            c[2] += gnd
            caps.append(c)
        cap_bank(caps, "DEC3V3")

    @circuit(name="root")
    def root():
        early()
        banks()

    c = root()
    result = c.generate_kicad_project(
        str(project_dir), generate_pcb=False, force_regenerate=False, update_source_refs=False
    )
    assert result["success"]
    return result


def test_rail_wired_bank_pin_connectivity_is_extracted(tmp_path):
    """wayfinder #61: a pin whose connectivity is expressed as a shared RAIL
    WIRE (``cap_bank()`` / ``resistor_bank()``) must still contribute its net
    to the component's preallocation connectivity signature.

    Root cause this pins down: ``APISynchronizer._get_pin_labels()`` only
    ever looks for a regular label, hierarchical label, or power symbol
    sitting within ``PIN_LABEL_DISTANCE_TOLERANCE`` (0.5 mm) of the pin's own
    computed position. But circuit-synth's own ``schematic_writer``
    deliberately does NOT write a per-pin marker for bank member pins: it
    adds them to ``_cap_bank_suppress`` / ``_resistor_bank_suppress``, and
    instead wires them with a shared rail wire carrying exactly ONE net
    marker at the rail's *extended* far end (``_add_rail_marker()`` at
    ``x_lo - ext``, where ``ext >= 2.54 mm``) plus a junction at each member
    pin. Every bank member pin is therefore an arbitrary, bank-size-
    proportional distance from the only marker naming its net -- far outside
    any fixed tolerance -- so the extraction returns nothing for it.

    Consequences, both reproduced below:
      * a ``resistor_bank()`` pull-up loses its COMMON (power-rail) pin, so
        its signature is partial -- the ``SD_CMD``-pull-up-to-``+3V3``
        symptom originally reported on the ticket;
      * a ``cap_bank()`` decoupling cap has BOTH pins rail-wired, so its
        signature is entirely EMPTY -- strictly worse than the ticket
        assumed, and the real reason a handful of decoupling caps churn.

    This is deliberately NOT fixed by widening the distance tolerance: the
    marker's distance grows with the bank, and a tolerance large enough to
    reach it would falsely bind unrelated neighbouring markers. The pin is
    connected through the rail's wire graph, so the wire graph is what has
    to be traversed.
    """
    project_dir = tmp_path / "proj"
    _build_bank_project(project_dir, extra_early_component=False)

    pins_by_ref = _bank_sheet_snapshot(project_dir)
    resistors = {ref: pins for ref, pins in pins_by_ref.items() if ref.startswith("R")}
    caps = {ref: pins for ref, pins in pins_by_ref.items() if ref.startswith("C")}
    assert len(resistors) == 3, f"expected the 3 bank pull-ups, got {sorted(resistors)}"
    assert len(caps) == 4, f"expected the 4 bank caps, got {sorted(caps)}"

    # Each resistor_bank pull-up must carry BOTH its fan-out signal pin and
    # its rail-wired common pin on +3V3.
    for ref, pins in sorted(resistors.items()):
        assert "+3V3" in pins.values(), (
            f"pull-up {ref} lost its rail-wired +3V3 common pin: pins={pins} -- "
            "resistor_bank() suppresses the per-pin marker and names the net only "
            "at the rail's far end, which the pin-position search cannot see"
        )
        assert len(pins) == 2, f"pull-up {ref} should have 2 connected pins, got {pins}"

    # Each cap_bank cap has BOTH pins rail-wired, so both must resolve.
    for ref, pins in sorted(caps.items()):
        assert set(pins.values()) == {"+3V3", "GND"}, (
            f"decoupling cap {ref} lost its rail-wired pins: pins={pins} -- both of a "
            "cap_bank member's pins are drawn by shared rails, leaving its "
            "connectivity signature empty and its identity unmatchable on resync"
        )


def test_rail_wired_bank_members_keep_identity_on_resync(tmp_path):
    """wayfinder #61, end-to-end consequence: because rail-wired bank members
    have empty/partial connectivity signatures, they fail to preallocation-
    match on an incremental resync and get removed + recreated with fresh
    UUIDs -- the observed ``C4``/``C5``/``C6``/``C36``/``C40`` churn on the
    real acquisition_mcu board.

    A component inserted into the earlier-traversed "Early" sheet shifts the
    global per-prefix counter underneath the untouched "Banks" sheet (the
    classic #55/#58 cascade trigger). Every bank member must keep both its
    reference and its UUID.
    """
    project_dir = tmp_path / "proj"
    _build_bank_project(project_dir, extra_early_component=False)

    sch_before = ksa.Schematic.load(str(project_dir / "Banks.kicad_sch"))
    before_by_uuid = {
        c.uuid: c.reference for c in sch_before.components if not c.reference.startswith("#")
    }
    assert len(before_by_uuid) == 7, "expected 3 bank pull-ups + 4 bank caps on Banks"

    _build_bank_project(project_dir, extra_early_component=True)

    sch_after = ksa.Schematic.load(str(project_dir / "Banks.kicad_sch"))
    after_by_uuid = {
        c.uuid: c.reference for c in sch_after.components if not c.reference.startswith("#")
    }
    assert len(after_by_uuid) == 7, "Banks' own components must not multiply or vanish"

    for old_uuid, old_ref in sorted(before_by_uuid.items(), key=lambda kv: kv[1]):
        assert old_uuid in after_by_uuid, (
            f"bank member {old_ref} (uuid {old_uuid}) was removed and recreated on "
            "resync -- its rail-wired connectivity was invisible to the "
            "preallocation matcher (wayfinder #61)"
        )
        assert after_by_uuid[old_uuid] == old_ref, (
            f"bank member {old_ref} kept its UUID but was renumbered to "
            f"{after_by_uuid[old_uuid]}"
        )
