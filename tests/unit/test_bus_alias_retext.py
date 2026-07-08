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
