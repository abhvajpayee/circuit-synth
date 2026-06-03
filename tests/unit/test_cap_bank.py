"""Tests for the cap_bank() visual-grouping directive.

A cap bank renders paralleled bypass caps as a tight two-rail array. It is a
representation-only change: connectivity (the netlist) must be identical to the
same caps drawn individually.
"""
import os
import re
import shutil
import subprocess
import tempfile

import pytest

from circuit_synth import Component, Net, cap_bank, circuit


def _bank_circuit(tag=True):
    @circuit(name="cb")
    def cb():
        vdd = Net("+3V3")
        gnd = Net("GND")
        sig = Net("VREF_X")
        # Power-net bank on +3V3 / GND.
        pb = [Component(symbol="Device:C", ref=f"C{i}", value="100nF") for i in (1, 2, 3, 4)]
        for c in pb:
            c[1] += vdd
            c[2] += gnd
        # Signal-net bank on VREF_X / GND (one regular net, one power net).
        sb = [Component(symbol="Device:C", ref=f"C{i}", value="1uF") for i in (5, 6, 7)]
        for c in sb:
            c[1] += sig
            c[2] += gnd
        if tag:
            cap_bank(pb, "VDD_DECOUPLE")
            cap_bank(sb, "VREF_BANK")
        # Anchor so +3V3 and VREF_X also exist outside the banks.
        r = Component(symbol="Device:R", ref="R1", value="10k")
        r[1] += vdd
        r[2] += sig

    return cb()


def _generate(circ, outdir):
    os.makedirs(outdir, exist_ok=True)
    cwd = os.getcwd()
    os.chdir(outdir)
    try:
        circ.generate_kicad_project(project_name="cb", generate_pcb=False)
    finally:
        os.chdir(cwd)
    (sch,) = [
        os.path.join(outdir, "cb", f)
        for f in os.listdir(os.path.join(outdir, "cb"))
        if f.endswith(".kicad_sch")
    ]
    return sch


def test_cap_bank_requires_two_caps():
    c1 = Component(symbol="Device:C", ref="C1", value="100nF")
    with pytest.raises(ValueError):
        cap_bank([c1], "TOO_SMALL")


def test_cap_bank_tags_properties():
    caps = [Component(symbol="Device:C", ref=f"C{i}", value="1u") for i in (1, 2)]
    cap_bank(caps, "B1", pitch_mil=200)
    for c in caps:
        assert c.cap_bank == "B1"
        assert abs(float(c.cap_bank_pitch) - 200 * 0.0254) < 1e-6


def test_cap_bank_draws_rails_and_strips_property(tmp_path):
    sch = _generate(_bank_circuit(tag=True), str(tmp_path))
    text = open(sch).read()
    # Two banks -> two rail pairs -> 4 wires; 4 + 3 caps -> 2*(4+3) = 14 junctions.
    assert text.count("(wire") == 4
    assert text.count("(junction") == 14
    # Directive properties must not leak into the schematic symbol fields.
    assert "cap_bank" not in text


@pytest.mark.skipif(
    shutil.which("kicad-cli") is None, reason="kicad-cli not available"
)
def test_cap_bank_netlist_is_unchanged(tmp_path):
    """The agreed acceptance gate: net membership identical with vs without banks."""

    def membership(sch):
        net = str(tmp_path / (os.path.basename(sch) + ".net"))
        subprocess.run(
            ["kicad-cli", "sch", "export", "netlist", "--format", "kicadsexpr",
             "-o", net, sch],
            check=True, capture_output=True,
        )
        t = open(net).read()
        pin2net = {}
        for m in re.finditer(r"\(net\b(.*?)(?=\(net\b|\Z)", t, re.S):
            nm = re.search(r'\(name "([^"]*)"', m.group(1))
            if not nm:
                continue
            for ref, pin in re.findall(r'\(ref "([^"]+)"\)\s*\(pin "([^"]+)"', m.group(1)):
                pin2net[(ref, pin)] = nm.group(1)
        return pin2net

    untagged = _generate(_bank_circuit(tag=False), str(tmp_path / "u"))
    tagged = _generate(_bank_circuit(tag=True), str(tmp_path / "t"))
    assert membership(untagged) == membership(tagged)
