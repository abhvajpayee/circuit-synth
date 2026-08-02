"""
Integration-level (but KiCad-I/O-free) tests for reference preallocation
wired into Circuit/Component (wayfinder #58).

These are the two "good first slice" scenarios the ticket calls out
explicitly, exercised end-to-end through Circuit.finalize_references() and
Circuit.collect_preallocation_records() -- but using a hand-built "existing
project" snapshot (plain ComponentRecord/PinRecord data) instead of a real
loaded .kicad_sch, matching the ticket's own suggestion that this slice can
be tested "purely against core/reference_manager.py + core/circuit.py,
without touching KiCad file I/O at all." The real KiCad-file adapter that
builds this same kind of snapshot from an actual existing project lives in
kicad.schematic.reference_preallocator and is exercised separately.
"""

from circuit_synth.core.circuit import Circuit
from circuit_synth.core.component import Component
from circuit_synth.core.net import Net
from circuit_synth.core.reference_preallocation import (
    ComponentRecord,
    build_global_net_map,
    match_sheet_prefix_group,
    ref_prefix,
)


def _existing_snapshot(finalized_root: Circuit):
    """Build an 'existing KiCad project' style snapshot -- keyed by ref
    string, exactly like a real loaded .kicad_sch would produce -- from an
    already-finalized circuit tree. Mirrors what
    kicad.schematic.reference_preallocator does for a real project."""
    per_sheet, _, _ = finalized_root.collect_preallocation_records()
    # Rekey each record's identity from id(comp) (only meaningful for the
    # *new* side) to comp.ref (what a real KiCad project's identity always
    # is), matching the real adapter's contract for the existing side.
    id_to_ref = {}
    for circ, comp in finalized_root._iter_all_components_with_circuit():
        id_to_ref[id(comp)] = comp.ref

    rekeyed_per_sheet = {}
    for sheet_name, records in per_sheet.items():
        rekeyed_per_sheet[sheet_name] = [
            ComponentRecord(
                identity=id_to_ref[rec.identity],
                prefix=rec.prefix,
                value=rec.value,
                pins=rec.pins,
            )
            for rec in records
        ]
    all_records = [r for records in rekeyed_per_sheet.values() for r in records]
    global_net_map = build_global_net_map(all_records)
    return rekeyed_per_sheet, global_net_map


def _preallocate(new_root: Circuit, existing_per_sheet, existing_global_map):
    """The glue a real caller (kicad.schematic.reference_preallocator, or
    Circuit.generate_kicad_project once wired up) performs: for every
    sheet present on both sides, group by prefix, match, and set
    _preallocated_ref on the matched new-side Component objects.

    Connectivity matching never depends on what reference a component
    currently holds (a signature is built purely from wired pins/nets), so
    this can run either before OR after new_root.finalize_references() --
    in practice it always runs AFTER, since finalize_references() already
    ran eagerly (per @circuit-decorated function) long before any
    KiCad-aware code could possibly run; see
    Circuit.remap_preallocated_references()'s docstring. Setting
    _preallocated_ref here is just bookkeeping either way -- applying it
    (renaming already-finalized components into place) is
    remap_preallocated_references()'s job.
    """
    new_per_sheet, new_global_map, by_id = new_root.collect_preallocation_records()

    for sheet_name, new_records in new_per_sheet.items():
        existing_records = existing_per_sheet.get(sheet_name, [])
        if not existing_records:
            continue

        prefixes = {r.prefix for r in new_records} | {r.prefix for r in existing_records}
        for prefix in prefixes:
            new_group = [r for r in new_records if r.prefix == prefix]
            existing_group = [r for r in existing_records if r.prefix == prefix]
            if not new_group or not existing_group:
                continue
            matches = match_sheet_prefix_group(
                new_group, existing_group, new_global_map, existing_global_map
            )
            for new_identity, existing_ref in matches.items():
                by_id[new_identity]._preallocated_ref = existing_ref


def _wire_resistor(circuit, value, pin1_net, pin2_net):
    r = Component(symbol="Device:R", ref="R", value=value)
    circuit.add_component(r)
    r[1] += pin1_net
    r[2] += pin2_net
    return r


def test_adding_component_to_sheet_a_leaves_sheet_b_references_unchanged():
    """Scenario 1 from the ticket: add a component to sheet A, assert
    sheet B's stored references are unchanged."""
    # --- "Before" run: SheetA has one resistor, SheetB has one resistor. ---
    before_root = Circuit(name="root")
    before_a = Circuit(name="SheetA")
    before_b = Circuit(name="SheetB")
    before_root.add_subcircuit(before_a)
    before_root.add_subcircuit(before_b)

    gnd = Net("GND")
    vcc = Net("VCC")
    sig_b = Net("SIG_B")

    _wire_resistor(before_a, "10k", vcc, gnd)
    _wire_resistor(before_b, "4k7", sig_b, gnd)

    before_root.finalize_references()
    b_ref_before = list(before_b._components.values())[0].ref
    a_ref_before = list(before_a._components.values())[0].ref
    assert b_ref_before == "R2" or b_ref_before == "R1"  # sanity: some final ref assigned
    existing_per_sheet, existing_global_map = _existing_snapshot(before_root)

    # --- "After" run: same circuits, but SheetA gained a brand-new resistor. ---
    after_root = Circuit(name="root")
    after_a = Circuit(name="SheetA")
    after_b = Circuit(name="SheetB")
    after_root.add_subcircuit(after_a)
    after_root.add_subcircuit(after_b)

    gnd2 = Net("GND")
    vcc2 = Net("VCC")
    sig_b2 = Net("SIG_B")
    sig_new = Net("SIG_NEW")

    _wire_resistor(after_a, "10k", vcc2, gnd2)
    new_r = _wire_resistor(after_a, "1k", sig_new, gnd2)  # newly added component
    _wire_resistor(after_b, "4k7", sig_b2, gnd2)

    after_root.finalize_references()
    _preallocate(after_root, existing_per_sheet, existing_global_map)
    after_root.remap_preallocated_references()

    a_components = list(after_a._components.values())
    b_components = list(after_b._components.values())

    a_ref_after = next(c.ref for c in a_components if c.value == "10k")
    b_ref_after = next(c.ref for c in b_components if c.value == "4k7")
    new_ref_after = next(c.ref for c in a_components if c.value == "1k")

    assert b_ref_after == b_ref_before, "SheetB's untouched component must keep its old reference"
    assert a_ref_after == a_ref_before, "SheetA's pre-existing component must keep its old reference"
    assert new_ref_after not in (a_ref_before, b_ref_before), "the new component must get a fresh reference"
    assert new_ref_after == new_r.ref


def test_mid_sheet_insertion_preserves_other_components_own_sheet_refs():
    """Scenario 2 from the ticket: insert a component in the middle of
    sheet A's own source, assert sheet A's OTHER pre-existing components
    keep their references -- the case the superseded ordinal-only design
    could not handle."""
    before_root = Circuit(name="root")
    before_a = Circuit(name="SheetA")
    before_root.add_subcircuit(before_a)

    gnd = Net("GND")
    vcc = Net("VCC")
    sig_a = Net("SIG_A")

    r1 = _wire_resistor(before_a, "10k", vcc, gnd)
    r2 = _wire_resistor(before_a, "4k7", sig_a, gnd)

    before_root.finalize_references()
    r1_ref_before = r1.ref
    r2_ref_before = r2.ref
    assert r1_ref_before != r2_ref_before
    existing_per_sheet, existing_global_map = _existing_snapshot(before_root)

    # "After" run: a brand new resistor is spliced in BETWEEN r1 and r2 in
    # SheetA's own declaration order.
    after_root = Circuit(name="root")
    after_a = Circuit(name="SheetA")
    after_root.add_subcircuit(after_a)

    gnd2 = Net("GND")
    vcc2 = Net("VCC")
    sig_a2 = Net("SIG_A")
    sig_mid = Net("SIG_MID")

    new_r1 = _wire_resistor(after_a, "10k", vcc2, gnd2)
    mid_r = _wire_resistor(after_a, "1k", sig_mid, gnd2)  # <-- spliced in the middle
    new_r2 = _wire_resistor(after_a, "4k7", sig_a2, gnd2)

    after_root.finalize_references()
    _preallocate(after_root, existing_per_sheet, existing_global_map)
    after_root.remap_preallocated_references()

    assert new_r1.ref == r1_ref_before
    assert new_r2.ref == r2_ref_before
    assert mid_r.ref not in (r1_ref_before, r2_ref_before)


def test_duplicate_zero_ohm_tie_resistors_preallocate_by_ordinal_tiebreak():
    """Real example from the consuming project: 4x parallel 0R tie
    resistors between the same two nets are genuinely indistinguishable by
    connectivity -- must still preallocate deterministically via the
    ordinal-among-duplicates tiebreak, not collapse/collide."""
    before_root = Circuit(name="root")
    before_a = Circuit(name="SheetA")
    before_root.add_subcircuit(before_a)

    agnd = Net("AGND")
    dgnd = Net("DGND")
    ties = [_wire_resistor(before_a, "0", agnd, dgnd) for _ in range(4)]
    before_root.finalize_references()
    old_refs_in_order = [t.ref for t in ties]
    assert len(set(old_refs_in_order)) == 4

    existing_per_sheet, existing_global_map = _existing_snapshot(before_root)

    after_root = Circuit(name="root")
    after_a = Circuit(name="SheetA")
    after_root.add_subcircuit(after_a)
    agnd2 = Net("AGND")
    dgnd2 = Net("DGND")
    new_ties = [_wire_resistor(after_a, "0", agnd2, dgnd2) for _ in range(4)]

    after_root.finalize_references()
    _preallocate(after_root, existing_per_sheet, existing_global_map)
    after_root.remap_preallocated_references()

    new_refs_in_order = [t.ref for t in new_ties]
    assert new_refs_in_order == sorted(old_refs_in_order, key=lambda r: int(r[1:])), (
        "declaration-order new ties must map onto ref-ascending old ties, in order"
    )


def test_remap_displaces_an_unrelated_component_squatting_on_the_target_ref():
    """Directly exercises Circuit.remap_preallocated_references()'s
    conflict-safe two-phase rename: a matched component's target reference
    ("R1") may already be held by a completely different, unrelated,
    genuinely-new component purely because of where the counter happened
    to land it -- that component must be displaced to a fresh reference,
    not silently collide with (or block) the matched component reclaiming
    its rightful old identity."""
    before_root = Circuit(name="root")
    before_a = Circuit(name="SheetA")
    before_root.add_subcircuit(before_a)

    gnd = Net("GND")
    vcc = Net("VCC")
    original = _wire_resistor(before_a, "10k", vcc, gnd)
    before_root.finalize_references()
    original_ref = original.ref  # "R1"
    assert original_ref == "R1"
    existing_per_sheet, existing_global_map = _existing_snapshot(before_root)

    after_root = Circuit(name="root")
    after_a = Circuit(name="SheetA")
    after_root.add_subcircuit(after_a)

    gnd2 = Net("GND")
    vcc2 = Net("VCC")
    sig_unrelated = Net("SIG_UNRELATED")
    # Declared FIRST, so the counter assigns it "R1" -- squatting on the
    # exact reference the second (matched) component will need to reclaim.
    unrelated = _wire_resistor(after_a, "1k", sig_unrelated, gnd2)
    # Declared SECOND: same connectivity as the original -- must match and
    # reclaim "R1", displacing `unrelated` in the process.
    matched = _wire_resistor(after_a, "10k", vcc2, gnd2)

    after_root.finalize_references()
    assert unrelated.ref == "R1"  # sanity: counter really did land here first
    assert matched.ref == "R2"

    _preallocate(after_root, existing_per_sheet, existing_global_map)
    after_root.remap_preallocated_references()

    assert matched.ref == "R1", "matched component must reclaim its old, stable reference"
    assert unrelated.ref != "R1", "the displaced component must no longer hold the reclaimed reference"
    assert unrelated.ref not in (None, ""), "the displaced component must still have SOME valid reference"
    assert not unrelated.ref.startswith("__PREALLOC_TMP__"), "no component may be left on a temporary reference"
    assert matched.ref != unrelated.ref
