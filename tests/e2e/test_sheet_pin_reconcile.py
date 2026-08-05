"""Regression tests for wayfinder #65: incremental sync must add a missing
sheet-symbol pin when a new scalar net starts crossing an EXISTING
intermediate sheet's boundary.

Reproduces the exact reported shape: a root-level component starts using a
brand-new net, and that same net is also threaded as a new parameter through
an already-existing WRAPPER sheet (zero local components of its own -- it
only forwards the net to a child) to reach a component on a LEAF sheet one
level further down. Before the fix:

  - `synchronizer.py`'s `_add_pin_label()` always emitted a
    `hierarchical_label` for a new pin connection, including at the ROOT
    sheet -- which can never validly hold one (no parent sheet symbol for
    KiCad to match it against; `kicad-cli sch erc` reports "Hierarchical
    label '...' in root sheet cannot be connected to non-existent parent
    sheet").
  - Nothing in the incremental-sync pipeline ever added a NEW `SheetPin` to
    an EXISTING sheet symbol -- `SheetPin` objects were only ever minted by
    `schematic_writer.py`'s clean-generation path (a fresh `Sheet` every
    run). The wrapper sheet's own sheet-symbol (on the root's canvas) and
    the leaf sheet's own sheet-symbol (on the wrapper's canvas) both never
    gained a pin for the new net, leaving it structurally unconnected across
    those two sheet boundaries even though a label existed at each end's
    use site.

Both are fixed together: `boundary_nets.py` (generic net_crosses_boundary
computation, reused for both LOCAL-vs-HIERARCHICAL label-type decisions and
for sheet-pin reconciliation) and `sheet_pin_sync.reconcile_sheet_pins()`
(adds whatever `SheetPin`s -- and, where nothing on the parent canvas
already ties the net at that exact point, a matching tie label -- are
missing), wired into `main_generator.py`'s `_update_existing_project()`.

Confirmed failing against the pre-fix parent commit (80259e2, wayfinder
#64) and passing at the fix.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from circuit_synth import Bus, Component, Net, circuit
from circuit_synth.core.circuit import Circuit
from circuit_synth.core.decorators import get_current_circuit, set_current_circuit


def _build(builder):
    """Build one circuit under a private root -- tests/conftest.py's autouse
    mock_active_circuit fixture installs a single shared root per test, and
    every @circuit builder called during that test attaches to it, so
    building the "before" and "after" versions of the same reference
    designators back to back would collide before sync is ever reached."""
    previous = get_current_circuit()
    set_current_circuit(Circuit(name="SheetPinReconcileIsolatedRoot"))
    try:
        return builder()
    finally:
        set_current_circuit(previous)


def _v1():
    """Root component (J1) + an existing WRAPPER sheet (zero local
    components -- only forwards to its own child) + a LEAF sheet under the
    wrapper with one component (U1). No cross-cutting net yet -- this is the
    "already-generated, already-existing sheet hierarchy" precondition."""

    @circuit(name="leaf")
    def leaf():
        u1 = Component(symbol="Device:R", ref="R", value="10k")
        gnd = Net("GND")
        gnd += u1[2]

    @circuit(name="wrapper")
    def wrapper():
        leaf()

    @circuit(name="root")
    def root():
        j1 = Component(symbol="Connector_Generic:Conn_01x02", ref="J1")
        gnd = Net("GND")
        gnd += j1[2]
        wrapper()

    return root()


def _v2():
    """Same sheet hierarchy as _v1 (wrapper() and leaf() already existed),
    but a brand-new scalar net (NEWNET) is added: used directly by a new
    root-level component (J1 pin 1) AND threaded as a new parameter through
    wrapper() into leaf(), where a new component (R2) also uses it. This is
    the exact ADR-0027 shape: a new net through an EXISTING intermediate
    sheet's call chain to reach a component several levels away."""

    @circuit(name="leaf")
    def leaf(newnet):
        u1 = Component(symbol="Device:R", ref="R", value="10k")
        gnd = Net("GND")
        gnd += u1[2]
        r2 = Component(symbol="Device:R", ref="R", value="1k")
        r2[1] += newnet

    @circuit(name="wrapper")
    def wrapper(newnet):
        leaf(newnet)

    @circuit(name="root")
    def root():
        j1 = Component(symbol="Connector_Generic:Conn_01x02", ref="J1")
        gnd = Net("GND")
        gnd += j1[2]
        newnet = Net("NEWNET")
        newnet += j1[1]
        wrapper(newnet)

    return root()


def _generate(circ, out_dir: Path, force: bool):
    # update_source_refs=False: its default ("auto-update unless
    # force_regenerate=True") would otherwise rewrite THIS TEST FILE's own
    # bare `ref="J"`-style source in place on every force=False call --
    # observed corrupting a later fixture in this same file (a second
    # incremental-sync call rewrote one bare ref to an already-used
    # concrete reference, colliding with a different fixture's own
    # component). Irrelevant to what's under test here either way -- these
    # fixtures always pass fully-numbered refs already.
    circ.generate_kicad_project(
        str(out_dir),
        force_regenerate=force,
        generate_pcb=False,
        update_source_refs=False,
    )


def _sheet_pin_names(sch_text: str, sheet_symbol_name: str) -> set:
    """Pin names on the `(sheet ...)` block whose Sheetname property matches
    `sheet_symbol_name`. Walks top-level `\\t(sheet ...)` blocks by paren
    depth -- the same technique `sch_postprocess.py`'s own block-scanners
    use, more robust than a multi-line regex against this exact
    s-expression layout."""
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


def _label_kind(sch_text: str, net_name: str) -> str:
    """'hierarchical', 'local', or 'missing' for the FIRST occurrence of
    net_name as a label in this sheet's own text."""
    if re.search(r'\(hierarchical_label "' + re.escape(net_name) + r'"', sch_text):
        return "hierarchical"
    if re.search(r'(?<!hierarchical_)\(label "' + re.escape(net_name) + r'"', sch_text):
        return "local"
    return "missing"


def test_new_net_through_existing_wrapper_sheet_gets_sheet_pins(tmp_path):
    """The reported symptom: after incremental sync, both the wrapper's own
    sheet-symbol (on root) and the leaf's own sheet-symbol (on wrapper) must
    expose a pin for the new net -- not just a label at each use site."""
    proj = tmp_path / "root"
    _generate(_build(_v1), proj, force=True)
    _generate(_build(_v2), proj, force=False)

    root_text = (proj / "root.kicad_sch").read_text()
    wrapper_text = (proj / "wrapper.kicad_sch").read_text()

    assert "NEWNET" in _sheet_pin_names(root_text, "wrapper"), (
        "root's own sheet-symbol for 'wrapper' is missing the NEWNET pin "
        "(wayfinder #65 regression)"
    )
    assert "NEWNET" in _sheet_pin_names(wrapper_text, "leaf"), (
        "wrapper's own sheet-symbol for 'leaf' is missing the NEWNET pin "
        "(wayfinder #65 regression)"
    )


def test_root_label_for_new_net_is_local_not_hierarchical(tmp_path):
    """A root sheet can never validly hold a hierarchical_label (no parent
    sheet symbol exists to match it against) -- J1's own new NEWNET pin
    label must be a plain local label."""
    proj = tmp_path / "root"
    _generate(_build(_v1), proj, force=True)
    _generate(_build(_v2), proj, force=False)

    root_text = (proj / "root.kicad_sch").read_text()
    assert _label_kind(root_text, "NEWNET") == "local", (
        "root sheet holds a hierarchical_label for NEWNET -- invalid KiCad "
        "(no parent sheet symbol can ever match it)"
    )


def test_wrapper_gets_synthesized_passthrough_tie_label(tmp_path):
    """wrapper() has zero local components of its own -- nothing else would
    ever put a NEWNET label on its canvas. reconcile_sheet_pins() must
    synthesize the passthrough tie (hierarchical, since NEWNET also crosses
    wrapper's own boundary up to root) itself."""
    proj = tmp_path / "root"
    _generate(_build(_v1), proj, force=True)
    _generate(_build(_v2), proj, force=False)

    wrapper_text = (proj / "wrapper.kicad_sch").read_text()
    assert _label_kind(wrapper_text, "NEWNET") == "hierarchical", (
        "wrapper sheet (zero local components) is missing its synthesized "
        "hierarchical passthrough tie label for NEWNET"
    )


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not available")
def test_sync_result_matches_clean_regen_erc(tmp_path):
    """End-to-end bar, same discipline as the #64 no-connect-marker
    regression test: an incrementally synced project must report the same
    ERC violation profile as a clean --force regen of the identical (_v2)
    source. Before the fix the synced project additionally reported
    isolated_pin_label / pin_not_connected for every NEWNET-family use."""
    after = _build(_v2)  # built once, used for both the sync and the clean regen

    synced = tmp_path / "root"
    _generate(_build(_v1), synced, force=True)
    _generate(after, synced, force=False)

    regen = tmp_path / "regen" / "root"
    regen.parent.mkdir(parents=True, exist_ok=True)
    _generate(after, regen, force=True)

    def erc(project_dir: Path):
        sch = project_dir / "root.kicad_sch"
        out_json = project_dir / "erc.json"
        subprocess.run(
            ["kicad-cli", "sch", "erc", "--output", str(out_json),
             "--format", "json", "--severity-all", str(sch)],
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


# --- Regression test for a real bug found *while building* the fix above ---
#
# reconcile_sheet_pins() reasons about individual net names (boundary_nets.py
# has no notion of a Bus -- at the JSON-netlist level a bus is just N
# separately-named nets, indistinguishable from N unrelated scalar nets). A
# bus member net that already crosses a sheet boundary via its OWN
# already-existing VECTOR sheet pin (e.g. "DATA_[0..2]", drawn and
# maintained entirely by the separate, already-existing bus_emit.py /
# inject_buses() machinery -- wayfinder #55/#57/#62) looked "missing" by a
# naive exact-name comparison against existing pin names, and got a second,
# redundant, individually-named pin added right alongside the vector one.
# On the real acquisition_mcu board this corrupted an unrelated pre-existing
# bus (ETH_DN_[0..3]) into duplicate labels once inject_buses' own repair
# pass ran against the unexpected extra pin. Fixed by
# `_covered_by_existing_vector_pin()`.


def _v1_bus():
    """Root component (J1) using 3 members of a cross-sheet Bus, tied to a
    LEAF sheet's own component using the same 3 members -- the minimal
    shape that gets a real vector sheet-pin ("DATA_[0..2]") drawn by the
    existing, separate bus_emit.py machinery."""

    @circuit(name="leaf")
    def leaf(data):
        u1 = Component(symbol="Connector_Generic:Conn_01x03", ref="U")
        for i in range(3):
            u1[i + 1] += data[i]

    @circuit(name="root")
    def root():
        j1 = Component(symbol="Connector_Generic:Conn_01x03", ref="J1")
        data = Bus("DATA", width=3)
        for i in range(3):
            j1[i + 1] += data[i]
        leaf(data)

    return root()


def _v2_bus():
    """Same bus/leaf shape as _v1_bus, plus one brand-new, UNRELATED scalar
    net threaded through the same leaf sheet -- this is what
    reconcile_sheet_pins() actually has something to add on this sync."""

    @circuit(name="leaf")
    def leaf(data, newnet):
        u1 = Component(symbol="Connector_Generic:Conn_01x03", ref="U")
        for i in range(3):
            u1[i + 1] += data[i]
        r1 = Component(symbol="Device:R", ref="R", value="1k")
        r1[1] += newnet

    @circuit(name="root")
    def root():
        j1 = Component(symbol="Connector_Generic:Conn_01x03", ref="J1")
        data = Bus("DATA", width=3)
        for i in range(3):
            j1[i + 1] += data[i]
        j2 = Component(symbol="Connector_Generic:Conn_01x02", ref="J2")
        newnet = Net("NEWNET")
        newnet += j2[1]
        leaf(data, newnet)

    return root()


def test_reconcile_does_not_add_duplicate_pin_for_existing_bus_member(tmp_path):
    """A net that's already exposed via an existing bus-vector sheet pin
    ("DATA[0..2]") must not also get a redundant, individually-named
    scalar pin ("DATA0") added alongside it. `Bus(name, width=N)` names its
    members "{name}{i}" with no separator (confirmed via the JSON netlist:
    "DATA0"/"DATA1"/"DATA2", vector pin "DATA[0..2]") -- the real board's
    own ETH_DN_[0..3] bus happens to use an underscore-joined name instead,
    but `_covered_by_existing_vector_pin()`'s prefix match handles either
    convention identically (it splits on the vector pin's own bracket, not
    on an assumed separator character)."""
    proj = tmp_path / "root"
    _generate(_build(_v1_bus), proj, force=True)
    _generate(_build(_v2_bus), proj, force=False)

    root_text = (proj / "root.kicad_sch").read_text()
    leaf_pins = _sheet_pin_names(root_text, "leaf")

    assert any(re.match(r"DATA\[\d+\.\.\d+\]$", p) for p in leaf_pins), (
        f"expected a vector bus pin for DATA on the 'leaf' sheet symbol, got {leaf_pins}"
    )
    for i in range(3):
        assert f"DATA{i}" not in leaf_pins, (
            f"reconcile_sheet_pins() added a redundant individual pin 'DATA{i}' "
            f"alongside the existing vector bus pin: {leaf_pins}"
        )
    assert "NEWNET" in leaf_pins, "the actually-new scalar net's own pin is missing"
