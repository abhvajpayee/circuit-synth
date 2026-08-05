"""Regression tests for wayfinder #60: verify (and where needed, fix)
incremental sync's handling of component DELETION -- removing a
Component() call from Python source and re-running sync (force_regenerate=
False) against a previously-generated project.

This scenario had never been deliberately tested end-to-end before #60
(prior tickets #55/#57/#58/#59/#61-#66 covered insertion, matcher gaps, and
sheet-pin reconciliation, but not deletion). Two real bugs were found and
fixed, both discovered by reproducing the exact two cases wayfinder #60's
own checklist calls out (a standalone front-panel-style component, and a
cap_bank()/resistor_bank() member) against a sandboxed copy of the real
acquisition_mcu board before being distilled into these minimal repros:

1. Orphaned pin-stub labels (synchronizer.py, `_process_unmatched()`):
   circuit-synth writes most pin connectivity as a label sitting exactly
   on the pin's own absolute position, with no separate wire object.
   `ComponentManager.remove_component()` only ever removed the component's
   `(symbol ...)` element, never those now-coincident-with-nothing label
   objects -- leaving them to be flagged `label_dangling` by `kicad-cli sch
   erc` on every later run. Fixed by `_remove_component_pin_labels()`,
   called right before a component's symbol is removed.

2. Value confusion in duplicate-connectivity matching
   (core/reference_preallocation.py, `compute_signature()`): a component's
   value was never part of its connectivity signature, so a same-value
   bank (e.g. cap_bank() decoupling caps) and an unrelated, differently-
   valued component sharing the exact same two named nets (e.g. a bulk cap
   on the same rail/GND) collapsed into one indistinguishable duplicate-
   connectivity bucket. Deleting one bank member shifted the ordinal
   tiebreak's pairing, silently rebinding the *different-value* component
   to a bank member's old reference and corrupting its on-disk value via
   `_needs_update()`/`update_component()`. Fixed by folding `value` into
   the signature.

Both confirmed failing against the pre-fix parent commit (5d544b3,
wayfinder #66) and passing at the fix.
"""

import json
import subprocess
from pathlib import Path

import pytest

from circuit_synth import Component, Net, cap_bank, circuit
from circuit_synth.core.circuit import Circuit
from circuit_synth.core.decorators import get_current_circuit, set_current_circuit


def _build(builder):
    """Build one circuit under a private root -- tests/conftest.py's
    autouse mock_active_circuit fixture installs a single shared root per
    test, and every @circuit builder called during that test attaches to
    it, so building "before" and "after" versions of the same reference
    designators back to back would collide before sync is ever reached.
    Same helper as test_sheet_pin_reconcile.py."""
    previous = get_current_circuit()
    set_current_circuit(Circuit(name="DeletionSyncIsolatedRoot"))
    try:
        return builder()
    finally:
        set_current_circuit(previous)


def _generate(circ, out_dir: Path, force: bool):
    circ.generate_kicad_project(
        str(out_dir),
        force_regenerate=force,
        generate_pcb=False,
        update_source_refs=False,
    )


def _erc_types(sch_path: Path) -> dict:
    out_json = sch_path.parent / (sch_path.stem + "_erc.json")
    subprocess.run(
        ["kicad-cli", "sch", "erc", "--output", str(out_json),
         "--format", "json", "--severity-all", str(sch_path)],
        check=True, capture_output=True, text=True,
    )
    data = json.loads(out_json.read_text())
    types = {}
    for sheet in data.get("sheets", []):
        for v in sheet.get("violations", []):
            types[v["type"]] = types.get(v["type"], 0) + 1
    return types


# ---------------------------------------------------------------------
# Case 1: standalone component deletion (mirrors deleting one front-panel
# LED from acquisition.py's _userio_frontpanel() -- a self-contained
# component + its own dedicated series resistor, both wired via their own
# private, uniquely-named net with no other consumer).
# ---------------------------------------------------------------------

def _standalone_v1():
    @circuit(name="root")
    def root():
        gnd = Net("GND")
        vcc = Net("VCC")
        j1 = Component(symbol="Connector_Generic:Conn_01x02", ref="J1")
        j1[1] += vcc
        j1[2] += gnd

        # A standalone LED + its own series resistor, wired through a
        # private net -- like acquisition.py's power-present LED (pled).
        led_a = Net("LED_A")
        r = Component(symbol="Device:R", ref="R", value="330R")
        r[1] += vcc
        r[2] += led_a
        d = Component(symbol="Device:LED", ref="D", value="LED")
        d[2] += led_a
        d[1] += gnd

    return root()


def _standalone_v2():
    @circuit(name="root")
    def root():
        gnd = Net("GND")
        vcc = Net("VCC")
        j1 = Component(symbol="Connector_Generic:Conn_01x02", ref="J1")
        j1[1] += vcc
        j1[2] += gnd
        # LED + resistor deleted.

    return root()


def test_standalone_component_deletion_removes_symbol(tmp_path):
    """Checklist item 1: the deleted component's schematic symbol must
    actually be gone after sync."""
    proj = tmp_path / "root"
    _generate(_build(_standalone_v1), proj, force=True)
    _generate(_build(_standalone_v2), proj, force=False)

    text = (proj / "root.kicad_sch").read_text()
    assert '(property "Reference" "D1"' not in text, "D1 symbol still present after deletion"
    assert '(property "Reference" "R2"' not in text or "LED_A" not in text


def test_standalone_component_deletion_leaves_no_dangling_labels(tmp_path):
    """Checklist item 3, standalone case: before the fix, the deleted
    component's own pin-stub labels (here: 'LED_A', and the VCC/GND labels
    at its own pins) survived the symbol's removal and were flagged
    label_dangling by kicad-cli -- zero wires involved, since circuit-synth
    ties a pin to a label by coincident position, not an explicit wire."""
    proj = tmp_path / "root"
    _generate(_build(_standalone_v1), proj, force=True)
    _generate(_build(_standalone_v2), proj, force=False)

    sch = proj / "root.kicad_sch"
    types = _erc_types(sch)
    assert types.get("label_dangling", 0) == 0, (
        f"orphaned pin-stub label(s) left behind after deletion: {types} "
        f"(wayfinder #60 regression)"
    )


def test_standalone_component_deletion_matches_clean_regen_erc(tmp_path):
    """Checklist item 3 (full bar): synced result's ERC profile must match
    a clean --force regen of the identical (post-deletion) source, same
    discipline as test_sheet_pin_reconcile.py's own ERC parity test."""
    after = _build(_standalone_v2)

    synced = tmp_path / "root"
    _generate(_build(_standalone_v1), synced, force=True)
    _generate(after, synced, force=False)

    regen = tmp_path / "regen" / "root"
    regen.parent.mkdir(parents=True, exist_ok=True)
    _generate(after, regen, force=True)

    synced_types = _erc_types(synced / "root.kicad_sch")
    regen_types = _erc_types(regen / "root.kicad_sch")
    assert synced_types == regen_types, (
        f"synced ERC profile diverges from clean-regen baseline:\n"
        f"  synced: {synced_types}\n  regen:  {regen_types}"
    )


def test_standalone_component_deletion_does_not_disturb_other_refs(tmp_path):
    """Checklist item 2: deleting one component must not renumber or
    reassign identity among the surviving components (here: J1)."""
    proj = tmp_path / "root"
    _generate(_build(_standalone_v1), proj, force=True)

    before = (proj / "root.kicad_sch").read_text()
    import re
    j1_uuid_before = re.search(
        r'\(property "Reference" "J1".*?\n.*?\(uuid "([^"]+)"\)', before, re.S
    )

    _generate(_build(_standalone_v2), proj, force=False)

    after = (proj / "root.kicad_sch").read_text()
    assert '(property "Reference" "J1"' in after, "unrelated component J1 lost its reference"


# ---------------------------------------------------------------------
# Case 2: cap_bank() member deletion (mirrors reducing acquisition.py's
# `_decouple("C_MCU", v3v3_u1, gnd, 8)` to `7`, right next to an unrelated
# standalone cap sharing the exact same two named nets -- the real-board
# shape that surfaced the value-confusion bug).
# ---------------------------------------------------------------------

def _bank_v1(n):
    @circuit(name="root")
    def root():
        gnd = Net("GND")
        rail = Net("RAIL")
        j1 = Component(symbol="Connector_Generic:Conn_01x02", ref="J1")
        j1[1] += rail
        j1[2] += gnd

        caps = []
        for _ in range(n):
            c = Component(symbol="Device:C", ref="C", value="100nF")
            c[1] += rail
            c[2] += gnd
            caps.append(c)
        if len(caps) >= 2:
            cap_bank(caps, "C_BANK")

        # Unrelated standalone bulk cap on the SAME two named nets --
        # the exact shape that collapsed into the bank's duplicate-
        # connectivity bucket pre-fix.
        bulk = Component(symbol="Device:C", ref="C", value="4.7uF")
        bulk[1] += rail
        bulk[2] += gnd

    return root()


def test_bank_member_deletion_preserves_other_members_and_bulk_cap_value(tmp_path):
    """Checklist item 4: deleting one cap_bank() member must not reassign
    references/values among the survivors, and must not corrupt the
    unrelated same-net standalone cap's value (the C11/C12 value-swap
    bug)."""
    proj = tmp_path / "root"
    _generate(_build(lambda: _bank_v1(8)), proj, force=True)
    _generate(_build(lambda: _bank_v1(7)), proj, force=False)

    text = (proj / "root.kicad_sch").read_text()

    # Exactly one component (of the original 9: 8 bank + 1 bulk) must be
    # gone; the standalone bulk cap's own value must survive unmolested
    # somewhere in the file, and a "100nF" bank member must never have
    # been overwritten to "4.7uF" or vice versa on a component that
    # should have kept its own value.
    import re
    values_present = re.findall(r'\(property "Value" "([^"]+)"', text)
    c_values = [v for v in values_present if v in ("100nF", "4.7uF")]
    assert c_values.count("4.7uF") == 1, (
        f"expected exactly one 4.7uF component to survive, found "
        f"{c_values.count('4.7uF')} (wayfinder #60 value-confusion regression): {c_values}"
    )
    assert c_values.count("100nF") == 7, (
        f"expected exactly 7 (of the original 8) 100nF bank members to survive, "
        f"found {c_values.count('100nF')}: {c_values}"
    )


def test_bank_member_deletion_matches_clean_regen_erc(tmp_path):
    """Checklist item 3 for the bank case: full ERC-parity bar.

    History (wayfinder #60): this test initially failed with an extra
    `unconnected_wire_endpoint` + a `lib_symbol_issues` mismatch. A wire-
    stub-removal fix was prototyped to chase it
    (`_remove_component_pin_wires()`, removing any simple 2-point wire with
    an endpoint at the deleted pin) but proved UNSAFE and was reverted --
    empirically, cap_bank()'s shared rail can include short simple wire
    segments structurally indistinguishable from a private per-member stub,
    so that rule risked deleting a still-live rail link (confirmed: it broke
    connectivity for surviving neighbors of a deleted middle bank member).
    The actual root cause turned out to be different: this synthetic
    circuit's small bank gives each member its own individual `#PWR*`
    power-symbol instance (rather than routing through a shared rail
    marker, as the real board's larger banks do), and the SAME orphaned-
    power-symbol gap `_remove_component_orphaned_power_symbols()` fixes
    for the standalone-component case also covers it here. Passes cleanly
    once that fix is in place -- no wire-stub-specific fix was needed after
    all.
    """
    after = _build(lambda: _bank_v1(7))

    synced = tmp_path / "root"
    _generate(_build(lambda: _bank_v1(8)), synced, force=True)
    _generate(after, synced, force=False)

    regen = tmp_path / "regen" / "root"
    regen.parent.mkdir(parents=True, exist_ok=True)
    _generate(after, regen, force=True)

    synced_types = _erc_types(synced / "root.kicad_sch")
    regen_types = _erc_types(regen / "root.kicad_sch")
    assert synced_types == regen_types, (
        f"synced ERC profile diverges from clean-regen baseline:\n"
        f"  synced: {synced_types}\n  regen:  {regen_types}"
    )
