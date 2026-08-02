"""Tests for aliased-bus pin retexting (bus_emit.inject_buses).

An aliased Bus -- e.g. Bus("SPI", members=["SCK","MISO","MOSI","CS"]) -- is drawn
as a numeric vector bus SPI_[0..3], but each component pin must read the elaborated
member name (SPI_SCK ...). The numeric positional name (SPI_0) stays only on the bus
stub as the vector tie. This is a label/representation change: the netlist
connectivity (and the canonical numeric net names) must be unchanged.

Hierarchical (cross-sheet) aliased buses are exercised by the acquisition board.
"""
import os
import re
import shutil
import subprocess

import pytest

from circuit_synth import Bus, Component, circuit


def _bus_circuit():
    @circuit(name="sb")
    def sb():
        spi = Bus("SPI", members=["SCK", "MISO", "MOSI", "CS"])
        j1 = Component(symbol="Connector_Generic:Conn_01x04", ref="J1")
        j2 = Component(symbol="Connector_Generic:Conn_01x04", ref="J2")
        for k in range(4):
            j1[k + 1] += spi[k]
            j2[k + 1] += spi[k]

    return sb()


def _generate(circ, outdir):
    os.makedirs(outdir, exist_ok=True)
    cwd = os.getcwd()
    os.chdir(outdir)
    try:
        circ.generate_kicad_project(project_name="sb", generate_pcb=False)
    finally:
        os.chdir(cwd)
    return os.path.join(outdir, "sb")


def test_aliased_bus_retexts_pins_to_elaborated(tmp_path):
    proj = _generate(_bus_circuit(), str(tmp_path))
    text = "".join(
        open(os.path.join(proj, f)).read()
        for f in os.listdir(proj)
        if f.endswith(".kicad_sch")
    )
    # Elaborated member labels appear (at the component pins).
    for alias in ("SPI_SCK", "SPI_MISO", "SPI_MOSI", "SPI_CS"):
        assert f'(label "{alias}"' in text, f"missing elaborated label {alias}"
    # The numeric vector bus is still drawn.
    assert "SPI_[0..3]" in text
    # Positional names survive only as the bus-stub tie, not at every pin:
    # fewer positional labels than elaborated ones (which sit at pins AND stub).
    pos = len(re.findall(r'\(label "SPI_0"', text))
    elab = len(re.findall(r'\(label "SPI_SCK"', text))
    assert 0 < pos < elab, f"positional={pos} elaborated={elab}"


@pytest.mark.skipif(
    shutil.which("kicad-cli") is None, reason="kicad-cli not available"
)
def test_aliased_bus_netlist_unchanged(tmp_path):
    """Retexting is representation-only: connectivity and the canonical numeric net
    names must be exactly what a plain (un-retexted) numeric bus would produce."""
    proj = _generate(_bus_circuit(), str(tmp_path))
    (sch,) = [
        os.path.join(proj, f) for f in os.listdir(proj)
        if f.endswith(".kicad_sch") and not _is_child(proj, f)
    ]
    net = str(tmp_path / "sb.net")
    subprocess.run(
        ["kicad-cli", "sch", "export", "netlist", "--format", "kicadsexpr",
         "-o", net, sch],
        check=True, capture_output=True,
    )
    t = open(net).read()
    name2pins = {}
    for m in re.finditer(r"\(net\b(.*?)(?=\(net\b|\Z)", t, re.S):
        nm = re.search(r'\(name "([^"]*)"', m.group(1))
        if not nm:
            continue
        pins = frozenset(re.findall(r'\(ref "([^"]+)"\)\s*\(pin "([^"]+)"', m.group(1)))
        if pins:
            name2pins[nm.group(1)] = pins
    # Each SPI member ties J1.k <-> J2.k, named by the NUMERIC positional name.
    for k in range(4):
        assert any(n.endswith(f"SPI_{k}") for n in name2pins), f"no net named SPI_{k}"
    spi_nets = {n: p for n, p in name2pins.items() if "SPI_" in n}
    # No elaborated name became a canonical net name.
    assert not any(re.search(r"SPI_(SCK|MISO|MOSI|CS)$", n) for n in spi_nets)


def _is_child(proj, fname):
    """A child sheet is referenced by a (sheet ...) in another sheet; the root is
    the project-named top sheet."""
    return fname.lower() != "sb.kicad_sch"


def _hier_bus_circuit():
    """Root sheet calling two *sibling* child sheets that share an aliased,
    cross-sheet bus -- the acquisition board's MCU<->WiFi SPI[SCK,MISO,MOSI,CS]
    shape. Reproduces the root-sheet bus-tie bug: the root ties sibling sheet
    pins together with coincident-point scalar labels, not hierarchical_labels,
    so _parent_surgery's hierarchical_label-only collapsing misses them."""

    @circuit(name="child_a")
    def child_a(bus):
        j1 = Component(symbol="Connector_Generic:Conn_01x04", ref="J1")
        for k in range(4):
            j1[k + 1] += bus[k]

    @circuit(name="child_b")
    def child_b(bus):
        j2 = Component(symbol="Connector_Generic:Conn_01x04", ref="J2")
        for k in range(4):
            j2[k + 1] += bus[k]

    @circuit(name="root")
    def root():
        spi = Bus("SPI", members=["SCK", "MISO", "MOSI", "CS"])
        child_a(spi)
        child_b(spi)

    return root()


def test_root_sheet_bus_tie_has_no_dangling_or_scalar_labels(tmp_path):
    """On the ROOT sheet, the per-sheet-symbol bus pin (SPI_[0..3]) must be tied
    by a bus label (SPI_[0..3]), not a lone scalar member label (SPI_0) -- and the
    other members' old per-pin tie labels (SPI_1/2/3) must not survive as
    dangling labels once their sheet pins are collapsed away."""
    proj = _generate(_hier_bus_circuit(), str(tmp_path))
    root_file = os.path.join(proj, "root.kicad_sch")
    text = open(root_file).read()

    # Root sheet should have zero bare scalar member labels left (SPI_1/2/3
    # never renamed to the bus name, and never a valid tie -- they must be
    # gone, not dangling).
    for mem in ("SPI_1", "SPI_2", "SPI_3"):
        assert f'(label "{mem}"' not in text, (
            f"dangling scalar label {mem} survives on root sheet after bus-pin collapse"
        )

    # And the surviving tie at the collapsed bus pin's coincidence point must be
    # the bus label itself, not the bare positional member SPI_0.
    assert '(label "SPI_[0..3]"' in text, "root sheet is missing the collapsed bus tie label"


def test_bus_reinjection_after_historical_root_corruption_is_repaired(tmp_path):
    """Directly reproduces the exact acquisition-board precondition (found
    2026-07-08): a root sheet that already carries stale, un-collapsed
    per-member tie labels (SPI_1/2/3, left over from history -- e.g. an older
    circuit-synth version, or drift across many incremental syncs that never
    re-ran _parent_surgery), while its leaf sheets' positional members are
    already demoted to plain labels (i.e. the bus was successfully injected on
    the leaves at some point in the past).

    A hierarchical-bus detector that only checks members for a surviving
    hierarchical_label sees none (all demoted) and returns False, so it
    misclassifies the root as a flat leaf sheet: the stale labels satisfy
    "member present >= 2", so the root wrongly gets a brand-new duplicate bus
    block drawn directly on it, and its stale ties get retexted to the
    elaborated alias names instead of being collapsed/repaired.

    A correct re-run must instead repair the root: collapse the stale ties down
    to the single bus label, with no new bus block appearing on the root.
    """
    proj = _generate(_hier_bus_circuit(), str(tmp_path))
    root_file = os.path.join(proj, "root.kicad_sch")
    root_text = open(root_file).read()

    # Simulate historical drift: revert the (already-correctly-collapsed) root
    # tie for child_a back to the OLD, un-collapsed, four-separate-labels shape
    # that a pre-bugfix run (or a much older circuit-synth) would have left
    # behind, while leaving the leaf sheets exactly as they are (positional
    # hierarchical_labels already demoted -- untouched).
    pat = re.compile(
        r'\t\(label "SPI_\[0\.\.3\]"\n\t\t\(at ([\d.\-]+) ([\d.\-]+) ([\d.\-]+)\)\n'
        r'(?:\t\t[^\n]*\n)*?\t\)\n'
    )
    m = pat.search(root_text)
    assert m, f"expected to find the root's collapsed SPI_[0..3] tie label:\n{root_text}"
    x, y, angle = m.groups()
    stale_labels = "".join(
        f'\t(label "SPI_{i}"\n\t\t(at {x} {float(y) + i * 2.54} {angle})\n'
        f'\t\t(effects (font (size 1.27 1.27)) (justify left bottom))\n'
        f'\t\t(uuid "{__import__("uuid").uuid4()}")\n\t)\n'
        for i in range(4)
    )
    corrupted = root_text[: m.start()] + stale_labels + root_text[m.end():]
    open(root_file, "w").write(corrupted)

    # Re-run generation over this corrupted-but-plausible-history state, into
    # the SAME project directory (must match _generate's project_name="sb" and
    # cwd so this is a genuine re-run over the same files, not a fresh one
    # elsewhere).
    cwd = os.getcwd()
    os.chdir(str(tmp_path))
    try:
        _hier_bus_circuit().generate_kicad_project(project_name="sb", generate_pcb=False)
    finally:
        os.chdir(cwd)
    fixed_text = open(root_file).read()

    # No duplicate/bogus bus block introduced directly on the root sheet.
    assert fixed_text.count("\n\t(bus\n") == 0, (
        "root sheet gained a bus graphic of its own -- it was misclassified as a leaf:\n"
        + fixed_text
    )
    # The stale per-member labels must have been collapsed away, not retexted.
    for mem in ("SPI_1", "SPI_2", "SPI_3"):
        assert f'(label "{mem}"' not in fixed_text, f"stale label {mem} was not repaired"
    for alias in ("SPI_SCK", "SPI_MISO", "SPI_MOSI", "SPI_CS"):
        assert f'(label "{alias}"' not in fixed_text, (
            f"root's stale ties were wrongly retexted to alias {alias} instead of repaired"
        )


def test_bus_injection_is_idempotent_across_regenerations(tmp_path):
    """Regenerating the SAME project a second time (as `generate_kicad_project`
    is called on every incremental sync, not just the first fresh generate) must
    not re-corrupt the root sheet: no duplicate bus block, no re-retexting of the
    root's collapsed tie labels back into scalar/alias form.

    This is the acquisition-board failure mode found 2026-07-08: after the FIRST
    injection, every member's hierarchical_label is demoted to a plain label (by
    design -- only the bus's own name stays hierarchical), so a hierarchical-bus
    detector that only checks members comes back False on the second run. That
    misclassifies the parent as an ordinary flat leaf sheet (since its old
    collapsed tie label still satisfies the "member present" check), drawing a
    bogus duplicate bus block on it directly."""
    circ = _hier_bus_circuit()
    proj = _generate(circ, str(tmp_path))
    root_file = os.path.join(proj, "root.kicad_sch")
    text_after_first = open(root_file).read()

    # Second call, same project directory (mirrors what an incremental sync /
    # re-run of the generating script does -- generate_kicad_project always
    # re-runs bus injection over whatever is already on disk).
    cwd = os.getcwd()
    os.chdir(str(tmp_path))
    try:
        circ.generate_kicad_project(project_name="sb", generate_pcb=False)
    finally:
        os.chdir(cwd)
    text_after_second = open(root_file).read()

    # Exactly one collapsed bus tie label per sheet symbol (2 sheets here) --
    # not duplicated, not reverted to a scalar member name.
    assert text_after_second.count('(label "SPI_[0..3]"') == 2, (
        "root sheet's bus tie label count changed/duplicated on the second run:\n"
        + text_after_second
    )
    for mem in ("SPI_0", "SPI_1", "SPI_2", "SPI_3"):
        assert f'(label "{mem}"' not in text_after_second, (
            f"second run reintroduced a bare scalar tie label {mem} on the root sheet"
        )
    # No net-new bus graphics should have appeared on the root sheet itself --
    # it only ties sibling sheet pins, it never carries a bus wire of its own.
    assert text_after_second.count("\n\t(bus\n") == text_after_first.count("\n\t(bus\n") == 0


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not available")
def test_root_sheet_bus_erc_clean(tmp_path):
    """ERC on the root sheet must not report net_not_bus_member / bus_to_net_conflict
    for the cross-sheet SPI bus tie."""
    proj = _generate(_hier_bus_circuit(), str(tmp_path))
    root_file = os.path.join(proj, "root.kicad_sch")
    out = str(tmp_path / "erc.json")
    subprocess.run(
        ["kicad-cli", "sch", "erc", "-o", out, "--format", "json", "--severity-all", root_file],
        capture_output=True, text=True,
    )
    import json
    data = json.load(open(out))
    bad_types = {"net_not_bus_member", "bus_to_net_conflict"}
    violations = [
        v for sheet in data.get("sheets", []) for v in sheet.get("violations", [])
        if v["type"] in bad_types
    ]
    assert not violations, f"root-sheet bus ERC violations: {violations}"


def _plain_vector_bus_circuit():
    """Root sheet -> two sibling leaf sheets, each with real component
    connections to >=2 members of a plain (non-aliased) vector Bus. Deliberately
    NOT aliased -- this is the minimal shape that reproduces wayfinder #55:
    a genuine incremental-sync (force_regenerate=False) round-trip through
    APISynchronizer, which loads/saves via kicad-sch-api. kicad-sch-api's
    Schematic.load() has no parser support for the native KiCad `bus`/
    `bus_entry` element types -- confirmed directly: a bare load+save
    round-trip of a file containing them drops both unconditionally, for
    either preserve_format setting, because load() never captures them into
    its own `_data` model at all. That silently strips the vector-bus
    graphic each leaf sheet's `inject_buses()` pass drew (the bus wire + its
    per-tap `bus_entry`), while leaving the (properly-modeled) per-tap stub
    `wire` and its end `label` behind -- orphaning the stub wire's bus-side
    endpoint, which is exactly KiCad ERC's `unconnected_wire_endpoint`."""

    @circuit(name="leaf_a")
    def leaf_a(bus, gnd):
        for i in range(3):
            r = Component("Device:R", ref="R", value="1k")
            r[1] += bus.members[i]
            r[2] += gnd

    @circuit(name="leaf_b")
    def leaf_b(bus, gnd):
        for i in range(3):
            r = Component("Device:R", ref="R", value="2k")
            r[1] += bus.members[i]
            r[2] += gnd

    @circuit(name="root")
    def root():
        from circuit_synth import Net

        gnd = Net("GND")
        data = Bus("DATA", width=3)
        leaf_a(data, gnd)
        leaf_b(data, gnd)

    return root()


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not available")
def test_incremental_sync_does_not_orphan_bus_stub_wires(tmp_path):
    """Regression test for wayfinder #55: running circuit-synth's incremental
    sync (generate_kicad_project with force_regenerate=False, the path
    APISynchronizer.sync_with_circuit() is reached through) against an
    already-generated project with a cross-sheet vector Bus must not
    introduce new `unconnected_wire_endpoint` ERC violations, even with zero
    Python source changes between the two generate_kicad_project() calls.

    This is a pure idempotency check: fresh generate (force_regenerate=True,
    never touches APISynchronizer) establishes a clean baseline; the SECOND
    call (force_regenerate defaults to False, and the project now exists, so
    this is the real incremental-sync path) must reproduce the identical ERC
    violation set on the root sheet -- not just the same count, the same
    category breakdown."""
    import json

    circ = _plain_vector_bus_circuit()
    proj_dir = str(tmp_path / "sb")
    circ.generate_kicad_project(proj_dir, generate_pcb=False, force_regenerate=True)

    root_file = os.path.join(proj_dir, "root.kicad_sch")

    def _erc_violation_types(out_path):
        subprocess.run(
            ["kicad-cli", "sch", "erc", "-o", out_path, "--format", "json",
             "--severity-all", root_file],
            capture_output=True, text=True,
        )
        data = json.load(open(out_path))
        counts = {}
        for sheet in data.get("sheets", []):
            for v in sheet.get("violations", []):
                counts[v["type"]] = counts.get(v["type"], 0) + 1
        return counts

    baseline = _erc_violation_types(str(tmp_path / "erc_baseline.json"))
    assert "unconnected_wire_endpoint" not in baseline, (
        f"fresh generation should never have dangling wire endpoints: {baseline}"
    )

    # The incremental-sync call: same circuit, same project dir, no Python
    # source changes, force_regenerate left at its False default.
    circ2 = _plain_vector_bus_circuit()
    circ2.generate_kicad_project(proj_dir, generate_pcb=False)

    after_sync = _erc_violation_types(str(tmp_path / "erc_after_sync.json"))
    assert "unconnected_wire_endpoint" not in after_sync, (
        "incremental sync introduced dangling bus-stub wire endpoints "
        f"(wayfinder #55 regression): {after_sync}"
    )
    assert after_sync == baseline, (
        "incremental sync changed the ERC violation profile on an unchanged "
        f"circuit: baseline={baseline}, after_sync={after_sync}"
    )


_ALIASED_BUS_LEAF_SCRIPT = '''
import sys
from circuit_synth import Bus, Component, Net, circuit

@circuit(name="leaf_a")
def leaf_a(bus, gnd):
    j = Component(symbol="Connector_Generic:Conn_01x04", ref="J")
    for k in range(4):
        j[k + 1] += bus[k]

@circuit(name="leaf_b")
def leaf_b(bus, gnd):
    j = Component(symbol="Connector_Generic:Conn_01x04", ref="J")
    for k in range(4):
        j[k + 1] += bus[k]

@circuit(name="root")
def root():
    gnd = Net("GND")
    spi = Bus("SPI", members=["SCK", "MISO", "MOSI", "CS"])
    leaf_a(spi, gnd)
    leaf_b(spi, gnd)

proj_dir = sys.argv[1]
force = sys.argv[2] == "force"
root().generate_kicad_project(proj_dir, generate_pcb=False, force_regenerate=force)
'''


def _generate_aliased_bus_leaf_project(proj_dir, force, py_exe):
    """Run `_ALIASED_BUS_LEAF_SCRIPT` in its OWN, fresh subprocess -- not as
    an in-process second/third call to `generate_kicad_project()` -- so each
    generation gets a fresh circuit-synth reference-numbering registry,
    exactly like a real incremental sync (each is a separate `python3
    <script>.py` invocation, e.g. this project's `acquisition.py`). Calling
    `generate_kicad_project()` a 2nd/3rd time in-process against brand-new
    `@circuit`-decorated root objects (rather than the SAME object) hits an
    unrelated auto-ref-numbering artifact (found while writing this test:
    a fresh top-level circuit object's ref counter does not reliably
    restart at the existing schematic's own next-free number past the
    second such in-process construction, misidentifying every component as
    removed+re-added and corrupting the run) -- out of scope for wayfinder
    #57, and irrelevant to real usage, so this test avoids it entirely by
    using separate processes instead."""
    result = subprocess.run(
        [py_exe, "-c", _ALIASED_BUS_LEAF_SCRIPT, proj_dir, "force" if force else "sync"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"generation subprocess failed (force={force}):\\n"
        f"stdout:\\n{result.stdout}\\nstderr:\\n{result.stderr}"
    )


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not available")
def test_incremental_sync_does_not_orphan_aliased_bus_stub_labels(tmp_path):
    """Regression test for wayfinder #57: incremental sync (force_regenerate
    left at its False default, the real APISynchronizer/kicad-sch-api
    round-trip path) against an ALIASED cross-sheet Bus must not introduce
    a `label_dangling` ERC finding -- even though the #55 fix already made
    the analogous PLAIN-bus case (`test_incremental_sync_does_not_orphan_bus_stub_wires`
    above) fully clean.

    An aliased bus's tap draws a **dual label** on its stub wire (see
    `bus_emit._bus_block`): the elaborated alias name sits at the wire's
    far endpoint, but the positional name sits partway along the same
    wire -- neither coincident with the wire's other endpoint. The #55 fix
    (`_bus_graphic_intact` / `_strip_orphaned_bus_graphic`) only matched a
    dangling label at a wire's far endpoint, so it fully repaired a plain
    bus's single-label tap but left an aliased bus's inner (positional)
    label behind -- and worse, the very next re-injection pass mistook that
    survivor for a fresh, genuine connection and retexted it in place to
    the elaborated alias, landing it, still with no wire, as a stable
    (non-growing but still wrong) `label_dangling` finding on every
    subsequent sync.

    Fresh generate (force_regenerate=True) establishes a clean baseline on
    each leaf sheet (the dual-label tap is fully formed); the SECOND call
    (a fresh process, force_regenerate defaulting to False against the
    already-generated project directory) is the real incremental-sync path
    and must reproduce the identical, zero-`label_dangling` ERC profile --
    checked on BOTH leaf sheets (where the dual-label stub actually lives),
    not just the root sheet (which only ties sibling children and never
    carries this bus's own stub labels). A THIRD call repeats the sync
    again, since the #57 bug was reported stable/non-growing across
    repeated syncs, not a one-time event."""
    import json
    import sys

    proj_dir = str(tmp_path / "sb")
    _generate_aliased_bus_leaf_project(proj_dir, force=True, py_exe=sys.executable)

    def _erc_violation_types(sch_file, out_path):
        subprocess.run(
            ["kicad-cli", "sch", "erc", "-o", out_path, "--format", "json",
             "--severity-all", sch_file],
            capture_output=True, text=True,
        )
        data = json.load(open(out_path))
        counts = {}
        for sheet in data.get("sheets", []):
            for v in sheet.get("violations", []):
                counts[v["type"]] = counts.get(v["type"], 0) + 1
        return counts

    leaf_files = {
        leaf: os.path.join(proj_dir, f"{leaf}.kicad_sch")
        for leaf in ("leaf_a", "leaf_b")
    }

    baseline = {
        leaf: _erc_violation_types(f, str(tmp_path / f"erc_baseline_{leaf}.json"))
        for leaf, f in leaf_files.items()
    }
    for leaf, counts in baseline.items():
        assert "label_dangling" not in counts, (
            f"fresh generation should never have a dangling bus-tap label "
            f"on {leaf}: {counts}"
        )

    # The incremental-sync call: same circuit, same project dir, no Python
    # source changes, force_regenerate defaulting to False -- this is what
    # reaches APISynchronizer.sync_with_circuit() via kicad-sch-api, the
    # round-trip that drops native `bus`/`bus_entry` elements.
    _generate_aliased_bus_leaf_project(proj_dir, force=False, py_exe=sys.executable)

    after_sync = {
        leaf: _erc_violation_types(f, str(tmp_path / f"erc_after_sync_{leaf}.json"))
        for leaf, f in leaf_files.items()
    }
    for leaf, counts in after_sync.items():
        assert "label_dangling" not in counts, (
            "incremental sync introduced a dangling aliased-bus stub label on "
            f"{leaf} (wayfinder #57 regression): {counts}"
        )
        assert counts == baseline[leaf], (
            f"incremental sync changed the ERC violation profile on {leaf} "
            f"for an unchanged circuit: baseline={baseline[leaf]}, after_sync={counts}"
        )

    # And a second incremental sync must stay just as clean (the #57 bug
    # was reported stable/non-growing across repeated syncs, not something
    # that only shows up once).
    _generate_aliased_bus_leaf_project(proj_dir, force=False, py_exe=sys.executable)
    after_second_sync = {
        leaf: _erc_violation_types(f, str(tmp_path / f"erc_after_2nd_sync_{leaf}.json"))
        for leaf, f in leaf_files.items()
    }
    for leaf, counts in after_second_sync.items():
        assert "label_dangling" not in counts, (
            f"a second incremental sync reintroduced dangling aliased-bus "
            f"stub labels on {leaf}: {counts}"
        )


_ROOT_LEAF_AND_PARENT_SCRIPT = '''
import sys
from circuit_synth import Bus, Component, circuit

@circuit(name="child")
def child(eth, addr):
    j = Component(symbol="Connector_Generic:Conn_01x10", ref="J")
    for k in range(4):
        j[k + 1] += eth[k]
    for k in range(6):
        j[k + 5] += addr.members[k]

@circuit(name="root")
def root():
    # Aliased bus (the ETH_UP_* class) and plain vector bus (the ADDR_DRV*
    # class) -- the two shapes the real board reported this symptom on.
    eth = Bus("ETH_UP", members=["TXP", "TXN", "RXP", "RXN"])
    addr = Bus("ADDR_DRV", 6)
    # The root sheet carries its OWN component on both buses (like the real
    # board's inter-board connector J5 sitting directly on the root sheet),
    # which makes root a genuine LEAF for each bus...
    j = Component(symbol="Connector_Generic:Conn_01x10", ref="J")
    for k in range(4):
        j[k + 1] += eth[k]
    for k in range(6):
        j[k + 5] += addr.members[k]
    # ...while ALSO being their PARENT, via a child sheet on the same buses.
    child(eth, addr)

proj_dir = sys.argv[1]
force = sys.argv[2] == "force"
root().generate_kicad_project(proj_dir, generate_pcb=False, force_regenerate=force)
'''


def _generate_root_leaf_and_parent_project(proj_dir, force, py_exe):
    """Run `_ROOT_LEAF_AND_PARENT_SCRIPT` in its own fresh subprocess, for
    the same reason `_generate_aliased_bus_leaf_project` does -- see that
    helper's docstring."""
    result = subprocess.run(
        [py_exe, "-c", _ROOT_LEAF_AND_PARENT_SCRIPT, proj_dir, "force" if force else "sync"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"generation subprocess failed (force={force}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _bus_vector_label_positions(sch_path, bus_label):
    """Every top-level `(label "<bus_label>" ...)` occurrence's (x, y) in a
    sheet file -- i.e. both the bus graphic's own head label AND each child
    sheet symbol's collapsed-vector tie label, which share that same text."""
    text = open(sch_path).read()
    return sorted(
        (float(m.group(1)), float(m.group(2)))
        for m in re.finditer(
            r'\t\(label "' + re.escape(bus_label) + r'"\n\t\t\(at ([0-9.\-]+) ([0-9.\-]+) ',
            text,
        )
    )


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not available")
def test_incremental_sync_keeps_parent_tie_on_sheet_that_is_also_a_bus_leaf(tmp_path):
    """Regression test for wayfinder #62: incremental sync must not strip a
    child sheet symbol's collapsed bus-vector TIE label off a sheet that is
    simultaneously that same bus's own LEAF.

    `_strip_orphaned_bus_graphic()` (the wayfinder #55 self-healing repair
    for the `bus`/`bus_entry` elements kicad-sch-api's load/save round-trip
    silently drops) identified the orphaned graphic's head label BY NAME
    alone -- dropping *every* top-level label whose text equals the bus's
    vector name. On a sheet that is only a leaf, or only a parent, there is
    exactly one such label and that is correct. But a sheet can be both at
    once: the real `acquisition_mcu` root sheet carries the inter-board
    connector J5 directly (making root a genuine leaf for `ETH_UP_[0..3]`
    and `ADDR_DRV[0..5]`) *and* the `ComputeSetup` child sheet symbol whose
    per-member pins `_parent_surgery()` already collapsed into one
    bus-vector pin with a matching tie label of that same vector name. The
    name-only strip destroyed that tie along with the orphaned head,
    permanently disconnecting the child sheet from the bus -- every stub
    label on the redrawn graphic then touched only the root-side connector
    pin, which is KiCad ERC's `isolated_pin_label` ("Label connected to
    only one pin"). Measured on the real board: 0 -> 53 root-sheet
    `isolated_pin_label` findings on a single sync.

    `_parent_surgery()`'s own docstring already records the same lesson for
    the per-member ties it renames/deletes (position-match, never a
    name-wide substitution, precisely because a file can be a parent and a
    leaf for one bus at the same time); the head-label strip simply never
    applied it.

    Fresh generate (force_regenerate=True, never touches APISynchronizer)
    establishes the baseline; the second and third calls are real
    incremental syncs in fresh processes."""
    import json
    import sys

    proj_dir = str(tmp_path / "sb")
    _generate_root_leaf_and_parent_project(proj_dir, force=True, py_exe=sys.executable)
    root_file = os.path.join(proj_dir, "root.kicad_sch")

    def _erc_violation_types(out_path):
        subprocess.run(
            ["kicad-cli", "sch", "erc", "-o", out_path, "--format", "json",
             "--severity-all", root_file],
            capture_output=True, text=True,
        )
        data = json.load(open(out_path))
        counts = {}
        for sheet in data.get("sheets", []):
            for v in sheet.get("violations", []):
                counts[v["type"]] = counts.get(v["type"], 0) + 1
        return counts

    baseline = _erc_violation_types(str(tmp_path / "erc_baseline.json"))
    assert "isolated_pin_label" not in baseline, (
        f"fresh generation should never isolate a bus pin label: {baseline}"
    )

    # Both labels must exist to begin with: the bus graphic's head, and the
    # child sheet symbol's collapsed-vector tie.
    baseline_ties = {
        bus: _bus_vector_label_positions(root_file, bus)
        for bus in ("ETH_UP_[0..3]", "ADDR_DRV[0..5]")
    }
    for bus, positions in baseline_ties.items():
        assert len(positions) >= 2, (
            f"fresh generation should place both a bus head label and a child "
            f"sheet-symbol tie label named {bus} on the root sheet, got {positions}"
        )

    for pass_no in (1, 2):
        _generate_root_leaf_and_parent_project(proj_dir, force=False, py_exe=sys.executable)
        after = _erc_violation_types(str(tmp_path / f"erc_after_sync_{pass_no}.json"))
        assert "isolated_pin_label" not in after, (
            f"incremental sync pass {pass_no} isolated a bus pin label on a sheet "
            f"that is both a leaf and a parent of that bus (wayfinder #62 "
            f"regression): {after}"
        )
        assert after == baseline, (
            f"incremental sync pass {pass_no} changed the root-sheet ERC violation "
            f"profile for an unchanged circuit: baseline={baseline}, after={after}"
        )
        for bus in ("ETH_UP_[0..3]", "ADDR_DRV[0..5]"):
            positions = _bus_vector_label_positions(root_file, bus)
            assert len(positions) >= 2, (
                f"incremental sync pass {pass_no} dropped the child sheet symbol's "
                f"collapsed-vector tie label for {bus} from the root sheet "
                f"(only {positions} left)"
            )
