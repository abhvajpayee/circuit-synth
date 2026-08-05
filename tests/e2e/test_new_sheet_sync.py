"""Regression tests for wayfinder #67: incremental sync must CREATE a sheet
that exists in the Python circuit but not yet on disk -- not silently delete
the components that moved into it.

Reported shape, reproduced exactly by `_v1`/`_v2` below: an existing sheet
function holds some components inline; the next revision of the same Python
source moves those components, unchanged, into a BRAND-NEW
`@circuit`-decorated function called from that same sheet. Before the fix,
incremental sync (`force_regenerate=False`):

  - never created `ldo.kicad_sch` and never added an `ldo` sheet symbol to
    `power.kicad_sch`, because incremental sync's entire notion of "which
    sheets exist" is read from DISK
    (`HierarchicalSynchronizer._build_hierarchy()` parses the root
    `.kicad_sch` and recurses through its `(sheet ...)` blocks), so a
    never-generated `@circuit` function is absent from that tree and
    `_sync_sheet_recursive()` never visits it;
  - and therefore let `power`'s own `APISynchronizer` run observe the moved
    components as "not in Python code" and REMOVE them -- the deliberate,
    working deletion path from wayfinder #60 -- with nothing anywhere in the
    pipeline to add them back.

Net effect: silent data loss that reads as an improvement, since removing
real symbols also removes their ERC violations.

Fixed by `new_sheet_sync.create_missing_sheets()`, called from
`main_generator._update_existing_project()` BEFORE `HierarchicalSynchronizer`
is constructed, so every later stage (hierarchy discovery, per-sheet sync,
`reconcile_sheet_pins()`, `fix_sheet_symbol_sizes()`) sees an ordinary
existing sheet.

Confirmed failing against the pre-fix parent commit (639fda5, wayfinder #60)
and passing at the fix.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from circuit_synth import Component, Net, circuit
from circuit_synth.core.circuit import Circuit
from circuit_synth.core.decorators import get_current_circuit, set_current_circuit


def _build(builder):
    """Build one circuit under a private root -- tests/conftest.py's autouse
    mock_active_circuit fixture installs a single shared root per test, and
    every @circuit builder called during that test attaches to it, so
    building the "before" and "after" versions of the same reference
    designators back to back would collide before sync is ever reached.
    (Same helper, same reason, as test_sheet_pin_reconcile.py's.)"""
    previous = get_current_circuit()
    set_current_circuit(Circuit(name="NewSheetSyncIsolatedRoot"))
    try:
        return builder()
    finally:
        set_current_circuit(previous)


def _v1():
    """"Before": `power` holds R1 (the regulator stand-in) and R2 inline,
    the already-generated precondition."""

    @circuit(name="power")
    def power(vin, gnd, vout):
        u1 = Component(symbol="Device:R", ref="R1", value="LDO")
        u1[1] += vin
        u1[2] += vout
        r1 = Component(symbol="Device:R", ref="R2", value="1k")
        r1[1] += vout
        r1[2] += gnd

    @circuit(name="root")
    def root():
        vin, gnd, vout = Net("VIN"), Net("GND"), Net("VOUT")
        j1 = Component(symbol="Connector_Generic:Conn_01x03", ref="J1")
        j1[1] += vin
        j1[2] += gnd
        j1[3] += vout
        power(vin, gnd, vout)

    return root()


def _v2():
    """"After": byte-for-byte the same components and nets, but R1/R2 now
    live in a BRAND-NEW `ldo` sheet called from `power`. `power` itself
    becomes a zero-local-component wrapper. Nothing else changes -- so any
    difference from `_v1` other than the sheet split is a bug."""

    @circuit(name="ldo")
    def ldo(vin, gnd, vout):
        u1 = Component(symbol="Device:R", ref="R1", value="LDO")
        u1[1] += vin
        u1[2] += vout
        r1 = Component(symbol="Device:R", ref="R2", value="1k")
        r1[1] += vout
        r1[2] += gnd

    @circuit(name="power")
    def power(vin, gnd, vout):
        ldo(vin, gnd, vout)

    @circuit(name="root")
    def root():
        vin, gnd, vout = Net("VIN"), Net("GND"), Net("VOUT")
        j1 = Component(symbol="Connector_Generic:Conn_01x03", ref="J1")
        j1[1] += vin
        j1[2] += gnd
        j1[3] += vout
        power(vin, gnd, vout)

    return root()


def _generate(circ, out_dir: Path, force: bool):
    # update_source_refs=False for the same reason test_sheet_pin_reconcile.py
    # gives: its default would rewrite THIS TEST FILE's own source in place on
    # an incremental call. Irrelevant here (these fixtures use fully-numbered
    # refs already).
    return circ.generate_kicad_project(
        str(out_dir),
        force_regenerate=force,
        generate_pcb=False,
        update_source_refs=False,
    )


def _refs(sch_text: str) -> set:
    """Reference designators of the symbols PLACED on this sheet.

    Matched via the per-symbol `(instances ... (reference "X"))` block rather
    than the `Reference` property, because the latter also appears inside the
    file's `lib_symbols` cache as the library part's own prefix (a bare "R"),
    which is not a placed component. Ignores #PWR*/#FLG* power symbols."""
    return {
        r
        for r in re.findall(r'\(reference "([^"]+)"', sch_text)
        if not r.startswith("#")
    }


def _sheet_filenames(sch_text: str) -> set:
    return set(re.findall(r'\(property "Sheetfile" "([^"]+)"', sch_text))


def _sheet_pin_names(sch_text: str, sheet_symbol_name: str) -> set:
    """Pin names on the `(sheet ...)` block whose Sheetname matches. Walks
    top-level `\\t(sheet ...)` blocks by paren depth -- same technique
    `sch_postprocess.py`'s own block scanners use."""
    lines = sch_text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        if lines[i] != "\t(sheet\n":
            i += 1
            continue
        block = [lines[i]]
        i += 1
        depth = 1
        while i < len(lines) and depth > 0:
            depth += lines[i].count("(") - lines[i].count(")")
            block.append(lines[i])
            i += 1
        raw = "".join(block)
        if f'(property "Sheetname" "{sheet_symbol_name}"' in raw:
            return set(re.findall(r'\(pin "([^"]+)"', raw))
    raise AssertionError(f"sheet symbol '{sheet_symbol_name}' not found")


def _synced(tmp_path: Path) -> Path:
    proj = tmp_path / "root"
    _generate(_build(_v1), proj, force=True)
    _generate(_build(_v2), proj, force=False)
    return proj


def test_moved_components_are_not_deleted(tmp_path):
    """The reported symptom, stated directly: after the move, R1 and R2 must
    still exist somewhere in the project. Before the fix they existed
    nowhere -- `power` deleted them and no sheet was created to receive
    them."""
    proj = _synced(tmp_path)

    surviving = set()
    for sch in proj.glob("*.kicad_sch"):
        surviving |= _refs(sch.read_text())

    assert {"R1", "R2"} <= surviving, (
        f"components moved into a brand-new sheet were silently deleted by "
        f"incremental sync (wayfinder #67 regression); project contains "
        f"{sorted(surviving)}"
    )


def test_new_sheet_file_is_created_and_owns_the_moved_components(tmp_path):
    """They must land in the NEW sheet specifically, and leave the old one --
    a move, not a duplication and not a stay-put."""
    proj = _synced(tmp_path)

    ldo_path = proj / "ldo.kicad_sch"
    assert ldo_path.exists(), "new sheet 'ldo.kicad_sch' was never created"

    assert _refs(ldo_path.read_text()) == {"R1", "R2"}
    assert _refs((proj / "power.kicad_sch").read_text()) == set(), (
        "moved components are still present on their OLD sheet as well"
    )


def test_parent_gains_a_sheet_symbol_for_the_new_sheet(tmp_path):
    """A new `.kicad_sch` file with no sheet symbol pointing at it is
    invisible to KiCad -- the parent's canvas must gain the symbol too."""
    proj = _synced(tmp_path)

    power_text = (proj / "power.kicad_sch").read_text()
    assert "ldo.kicad_sch" in _sheet_filenames(power_text), (
        "'power' has no sheet symbol referencing the new 'ldo' sheet"
    )


def test_new_sheet_symbol_gets_its_boundary_pins(tmp_path):
    """new_sheet_sync deliberately adds the sheet symbol with an EMPTY pin
    list and leaves pin population to reconcile_sheet_pins() (wayfinder
    #65), which also emits the matching tie labels. Verify that handoff
    actually happens end to end rather than leaving a pinless symbol."""
    proj = _synced(tmp_path)

    pins = _sheet_pin_names((proj / "power.kicad_sch").read_text(), "ldo")
    assert {"VIN", "GND", "VOUT"} <= pins, (
        f"new 'ldo' sheet symbol is missing boundary pins; has {sorted(pins)}"
    )


def test_new_sheet_registered_in_kicad_pro(tmp_path):
    """The project's own sheet cache must list the new sheet's document
    UUID, and must still list the pre-existing sheets under their ORIGINAL
    UUIDs (rewriting those would orphan every existing page setting)."""
    proj = _synced(tmp_path)

    pre_uuid = re.search(
        r'\(uuid "([^"]+)"\)', (proj / "power.kicad_sch").read_text()
    ).group(1)
    new_uuid = re.search(
        r'\(uuid "([^"]+)"\)', (proj / "ldo.kicad_sch").read_text()
    ).group(1)

    sheets = json.loads((proj / "root.kicad_pro").read_text())["sheets"]
    listed = {uuid for uuid, _name in sheets}

    assert new_uuid in listed, "new sheet is not registered in .kicad_pro"
    assert pre_uuid in listed, "existing sheet's .kicad_pro entry was lost"


def test_new_sheet_components_carry_the_correct_hierarchical_path(tmp_path):
    """A component's `(instances ...)` path must be
    root_document_uuid / power_sheet_symbol_uuid / ldo_sheet_symbol_uuid --
    the same convention clean generation builds. A wrong path here still
    loads in KiCad but silently detaches the component from its sheet for
    annotation and PCB association."""
    proj = _synced(tmp_path)

    root_text = (proj / "root.kicad_sch").read_text()
    power_text = (proj / "power.kicad_sch").read_text()
    ldo_text = (proj / "ldo.kicad_sch").read_text()

    root_uuid = re.search(r'\(uuid "([^"]+)"\)', root_text).group(1)

    def _sheet_symbol_uuid(text: str, filename: str) -> str:
        lines = text.splitlines(keepends=True)
        i = 0
        while i < len(lines):
            if lines[i] != "\t(sheet\n":
                i += 1
                continue
            block = [lines[i]]
            i += 1
            depth = 1
            while i < len(lines) and depth > 0:
                depth += lines[i].count("(") - lines[i].count(")")
                block.append(lines[i])
                i += 1
            raw = "".join(block)
            if f'(property "Sheetfile" "{filename}"' in raw:
                return re.search(r'\t\t\(uuid "([^"]+)"\)', raw).group(1)
        raise AssertionError(f"no sheet symbol for {filename}")

    power_symbol = _sheet_symbol_uuid(root_text, "power.kicad_sch")
    ldo_symbol = _sheet_symbol_uuid(power_text, "ldo.kicad_sch")
    expected = f"/{root_uuid}/{power_symbol}/{ldo_symbol}"

    assert f'(path "{expected}"' in ldo_text, (
        f"new sheet's components do not carry the expected hierarchical "
        f"instance path {expected}"
    )


def test_reparenting_an_existing_sheet_is_refused_loudly(tmp_path):
    """Scope boundary: moving an ALREADY-GENERATED sheet under a new parent
    is a sheet move, not a sheet creation, and would need every affected
    component's instance path rewritten. It must raise rather than produce a
    duplicate sheet symbol -- the same "quietly wrong" failure mode this
    ticket is about."""

    def _v3():
        @circuit(name="ldo")
        def ldo(vin, gnd, vout):
            u1 = Component(symbol="Device:R", ref="R1", value="LDO")
            u1[1] += vin
            u1[2] += vout
            r1 = Component(symbol="Device:R", ref="R2", value="1k")
            r1[1] += vout
            r1[2] += gnd

        @circuit(name="rail")
        def rail(vin, gnd, vout):
            ldo(vin, gnd, vout)

        @circuit(name="power")
        def power(vin, gnd, vout):
            rail(vin, gnd, vout)

        @circuit(name="root")
        def root():
            vin, gnd, vout = Net("VIN"), Net("GND"), Net("VOUT")
            j1 = Component(symbol="Connector_Generic:Conn_01x03", ref="J1")
            j1[1] += vin
            j1[2] += gnd
            j1[3] += vout
            power(vin, gnd, vout)

        return root()

    proj = _synced(tmp_path)  # 'ldo' now exists on disk, parented by 'power'
    ldo_before = (proj / "ldo.kicad_sch").read_text()

    # NewSheetSyncError propagates out of _update_existing_project (which
    # re-raises rather than falling back to a destructive regen), and
    # Circuit.generate_kicad_project() converts any exception into a
    # success=False result -- so assert on that, not on pytest.raises.
    result = _generate(_build(_v3), proj, force=False)
    assert result["success"] is False
    assert "Re-parenting" in result["error"], result["error"]

    assert not (proj / "rail.kicad_sch").exists(), (
        "the refused sync left a half-created sheet behind -- the check must "
        "run before anything is written"
    )
    assert (proj / "ldo.kicad_sch").read_text() == ldo_before, (
        "the refused sync modified the existing sheet it was asked to reparent"
    )


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not available")
def test_sync_result_matches_clean_regen_erc(tmp_path):
    """End-to-end bar, the same one wayfinder #64/#65's regression tests use:
    an incrementally-synced project must report the same ERC violation
    profile as a clean --force regen of the identical (_v2) source.

    This is the assertion that would have caught the original bug's most
    misleading property -- deleting real components makes the raw violation
    COUNT go DOWN, so only a comparison against a correct reference reveals
    the loss."""
    after = _build(_v2)  # built once, used for both the sync and the clean regen

    synced = tmp_path / "root"
    _generate(_build(_v1), synced, force=True)
    _generate(after, synced, force=False)

    regen = tmp_path / "regen" / "root"
    regen.parent.mkdir(parents=True, exist_ok=True)
    _generate(after, regen, force=True)

    def erc(project_dir: Path):
        out_json = project_dir / "erc.json"
        subprocess.run(
            ["kicad-cli", "sch", "erc", "--output", str(out_json),
             "--format", "json", "--severity-all",
             str(project_dir / "root.kicad_sch")],
            check=True, cwd=str(project_dir), capture_output=True, text=True,
        )
        data = json.loads(out_json.read_text())
        types = {}
        for sheet in data.get("sheets", []):
            for v in sheet.get("violations", []):
                types[v["type"]] = types.get(v["type"], 0) + 1
        return types

    synced_types = erc(synced)
    regen_types = erc(regen)

    assert synced_types == regen_types, (
        f"incrementally-synced project's ERC profile diverges from a clean "
        f"regen of the same source: synced={synced_types}, regen={regen_types}"
    )


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not available")
def test_sync_preserves_net_membership(tmp_path):
    """Connectivity, not just symbol survival: every net's component/pin
    membership after the sheet split must match a clean regen of the same
    source exactly. Moving components between sheets must not change a
    single connection."""
    after = _build(_v2)

    synced = tmp_path / "root"
    _generate(_build(_v1), synced, force=True)
    _generate(after, synced, force=False)

    regen = tmp_path / "regen" / "root"
    regen.parent.mkdir(parents=True, exist_ok=True)
    _generate(after, regen, force=True)

    def netlist(project_dir: Path):
        out = project_dir / "exported.net"
        subprocess.run(
            ["kicad-cli", "sch", "export", "netlist", "--output", str(out),
             str(project_dir / "root.kicad_sch")],
            check=True, cwd=str(project_dir), capture_output=True, text=True,
        )
        text = out.read_text()
        nets = {}
        # kicad-cli writes each net as a multi-line `(net (code ..) (name ..)
        # (node (ref ..) (pin ..)) ...)` block; split on the block opener
        # rather than trying to match the whole thing in one regex.
        for block in text.split("\t\t(net\n")[1:]:
            name_m = re.search(r'\(name "([^"]*)"\)', block)
            if not name_m:
                continue
            nodes = set(
                re.findall(r'\(ref "([^"]+)"\)\s*\n\s*\(pin "([^"]+)"\)', block)
            )
            # Leading "/" is the sheet path prefix, which changes by design
            # when a component moves to a different sheet -- the CONNECTIVITY
            # is what must be identical, so compare on the bare net name.
            nets[name_m.group(1).rsplit("/", 1)[-1]] = nodes
        return nets

    synced_nets = netlist(synced)
    regen_nets = netlist(regen)
    assert synced_nets, "netlist export produced no nets -- parser is broken"
    assert synced_nets == regen_nets, (
        f"net membership after moving components into a new sheet differs "
        f"from a clean regen of the same source: "
        f"synced={synced_nets}, regen={regen_nets}"
    )


# --- Regression tests for two deletion-path gaps found by running the fix
# --- above against the real acquisition Analog+Power board (wayfinder #67).
#
# Moving a component to a new sheet DELETES it from its old sheet, so it
# exercises the deletion path -- and turned up two orphans that path still
# left behind, each reported by kicad-cli sch erc on every subsequent run:
#
#   1. a `(no_connect ...)` marker for a `Pin.no_connect()` pin, which
#      nothing removed when its component went away (`no_connect_dangling`);
#   2. a second, STACKED copy of a pin label at the identical position,
#      which `_get_pin_labels()` structurally cannot return because it is
#      keyed by pin and breaks on the first match (`label_dangling`).


def _nc_v1():
    """"Before": `power` holds J2, whose pin 3 is explicitly no-connected."""

    @circuit(name="power")
    def power(vin, gnd):
        j2 = Component(symbol="Connector_Generic:Conn_01x03", ref="J2")
        j2[1] += vin
        j2[2] += gnd
        j2[3].no_connect(reason="reserved, intentionally unconnected")

    @circuit(name="root")
    def root():
        vin, gnd = Net("VIN"), Net("GND")
        j1 = Component(symbol="Connector_Generic:Conn_01x02", ref="J1")
        j1[1] += vin
        j1[2] += gnd
        power(vin, gnd)

    return root()


def _nc_v2():
    """"After": the same J2, no-connect and all, moved into a new sheet."""

    @circuit(name="ldo")
    def ldo(vin, gnd):
        j2 = Component(symbol="Connector_Generic:Conn_01x03", ref="J2")
        j2[1] += vin
        j2[2] += gnd
        j2[3].no_connect(reason="reserved, intentionally unconnected")

    @circuit(name="power")
    def power(vin, gnd):
        ldo(vin, gnd)

    @circuit(name="root")
    def root():
        vin, gnd = Net("VIN"), Net("GND")
        j1 = Component(symbol="Connector_Generic:Conn_01x02", ref="J1")
        j1[1] += vin
        j1[2] += gnd
        power(vin, gnd)

    return root()


def test_no_connect_marker_moves_with_its_component(tmp_path):
    """A `Pin.no_connect()` marker must follow its component to the new
    sheet, leaving none stranded on the old one.

    `ComponentManager.remove_component()` only removes the `(symbol ...)`
    element, and the no-connect reconciliation added by wayfinder #64 only
    visits MATCHED components' pins -- so a marker whose component is gone
    from Python was never considered at all."""
    proj = tmp_path / "root"
    _generate(_build(_nc_v1), proj, force=True)
    _generate(_build(_nc_v2), proj, force=False)

    power_text = (proj / "power.kicad_sch").read_text()
    ldo_text = (proj / "ldo.kicad_sch").read_text()

    assert power_text.count("(no_connect") == 0, (
        "old sheet kept an orphaned no-connect marker after its component "
        "moved away (kicad-cli sch erc: no_connect_dangling)"
    )
    assert ldo_text.count("(no_connect") == 1, (
        "the moved component's no-connect marker did not follow it to the "
        "new sheet"
    )


def test_stacked_duplicate_pin_label_is_removed_with_its_component(tmp_path):
    """Two labels stacked at one pin position must BOTH go when their
    component does.

    `_get_pin_labels()` is keyed by pin and `break`s on the first label
    within tolerance, so when a component has several pins resolving to the
    same coordinate, every one of them maps to the SAME label object and the
    copy underneath is never a removal candidate. Reproduced here by
    duplicating a real generated label in place -- the same on-disk shape a
    multi-pin-one-net part (e.g. LT3045's IN/IN/EN-UV) produces naturally."""
    proj = tmp_path / "root"
    _generate(_build(_v1), proj, force=True)

    power_path = proj / "power.kicad_sch"
    text = power_path.read_text()
    m = re.search(
        r'\t\((hierarchical_label|label) "([^"]+)"\n(?:\t\t.*\n)*?\t\)\n', text
    )
    assert m, "fixture precondition: generated sheet has no pin label at all"
    original, kind, label_name = m.group(0), m.group(1), m.group(2)
    needle = f'({kind} "{label_name}"'
    # Same text, same position, different uuid -- a true stacked duplicate.
    if '(uuid "' in original:
        duplicate = re.sub(
            r'\(uuid "[^"]+"\)',
            '(uuid "deadbeef-0000-0000-0000-000000000001")',
            original,
        )
    else:
        duplicate = original.replace(
            "\t)\n", '\t\t(uuid "deadbeef-0000-0000-0000-000000000001")\n\t)\n'
        )
    power_path.write_text(text.replace(original, original + duplicate, 1))
    assert power_path.read_text().count(needle) >= 2

    # The pin position both copies sit at. Asserting on POSITION rather than
    # on a raw count matters: `power` legitimately keeps a same-named tie
    # label of its own after the move (it forwards VIN/GND/VOUT on to the
    # new `ldo` sheet symbol), so "zero labels named VIN" would be wrong.
    # What must be gone is anything still sitting on the DELETED
    # component's old pin.
    at_m = re.search(r'\t\t\(at ([\d.+-]+) ([\d.+-]+)', original)
    assert at_m, "fixture precondition: label has no position"
    old_x, old_y = float(at_m.group(1)), float(at_m.group(2))

    _generate(_build(_v2), proj, force=False)

    stranded = [
        (float(x), float(y))
        for x, y in re.findall(
            r'\t\(' + kind + r' "' + re.escape(label_name)
            + r'"\n(?:\t\t.*\n)*?\t\t\(at ([\d.+-]+) ([\d.+-]+)',
            power_path.read_text(),
        )
        if abs(float(x) - old_x) < 0.01 and abs(float(y) - old_y) < 0.01
    ]
    assert not stranded, (
        f"a stacked duplicate of '{label_name}' survived its component's "
        f"removal, still sitting on the deleted pin at ({old_x}, {old_y}) "
        f"(kicad-cli sch erc: label_dangling)"
    )
