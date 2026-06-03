"""Tests for the resistor_bank() visual-grouping directive.

A resistor bank renders a single-common-rail array (pull-ups/pull-downs/config-to-
rail) as a tight row joined by one shared rail, while each fan-out pin keeps its own
net label. It is a representation-only change: connectivity (the netlist) must be
identical to the same resistors drawn individually.
"""
import os
import re
import shutil
import subprocess

import pytest

from circuit_synth import Component, Net, circuit, resistor_bank


def _R(ref, value, a, b):
    r = Component(symbol="Device:R", ref=ref, value=value)
    r[1] += a
    r[2] += b
    return r


def _bank_circuit(tag=True):
    @circuit(name="rb")
    def rb():
        v3v3 = Net("+3V3")
        gnd = Net("GND")
        s = [Net(f"SIG{i}") for i in range(3)]
        t = [Net(f"DRV{i}") for i in range(3)]

        # Pull-up bank: each signal to +3V3 (power common -> rail on top).
        pu = [_R(f"Rpu{i}", "10k", s[i], v3v3) for i in range(3)]
        # Pull-down bank: each line to GND (ground common -> rail on bottom),
        # with the common net on pin 1 to exercise the rotation path.
        pd = [_R(f"Rpd{i}", "10k", gnd, t[i]) for i in range(3)]
        if tag:
            resistor_bank(pu, "SIG_PU", common=v3v3)
            resistor_bank(pd, "DRV_PD", common=gnd)

        # Anchor every fan-out net to a second pin so nets are well-defined.
        for i in range(3):
            _R(f"Ra{i}", "1k", s[i], gnd)
            _R(f"Rb{i}", "1k", t[i], v3v3)

    return rb()


def _generate(circ, outdir):
    os.makedirs(outdir, exist_ok=True)
    cwd = os.getcwd()
    os.chdir(outdir)
    try:
        circ.generate_kicad_project(project_name="rb", generate_pcb=False)
    finally:
        os.chdir(cwd)
    (sch,) = [
        os.path.join(outdir, "rb", f)
        for f in os.listdir(os.path.join(outdir, "rb"))
        if f.endswith(".kicad_sch")
    ]
    return sch


def test_resistor_bank_requires_two():
    r1 = _R("R1", "10k", Net("A"), Net("V"))
    with pytest.raises(ValueError):
        resistor_bank([r1], "TOO_SMALL", common=Net("V"))


def test_resistor_bank_rejects_non_resistor():
    v = Net("+3V3")
    c1 = Component(symbol="Device:C", ref="C1", value="100nF")
    c2 = Component(symbol="Device:C", ref="C2", value="100nF")
    c1[1] += Net("A"); c1[2] += v
    c2[1] += Net("B"); c2[2] += v
    with pytest.raises(ValueError, match="Device:R only"):
        resistor_bank([c1, c2], "NOPE", common=v)


def test_resistor_bank_member_must_touch_common():
    v = Net("+3V3")
    r1 = _R("R1", "10k", Net("A"), v)
    r2 = _R("R2", "10k", Net("B"), Net("OTHER"))  # not on +3V3
    with pytest.raises(ValueError, match="does not connect to common"):
        resistor_bank([r1, r2], "BAD", common=v)


def test_resistor_bank_tags_properties():
    v = Net("+3V3")
    rs = [_R(f"R{i}", "10k", Net(f"S{i}"), v) for i in (1, 2)]
    resistor_bank(rs, "B1", common=v, pitch_mil=200, side="bottom")
    for r in rs:
        assert r.resistor_bank == "B1"
        assert r.resistor_bank_common == "+3V3"
        assert r.resistor_bank_side == "bottom"
        assert abs(float(r.resistor_bank_pitch) - 200 * 0.0254) < 1e-6


def test_resistor_bank_draws_rails_and_strips_property(tmp_path):
    sch = _generate(_bank_circuit(tag=True), str(tmp_path))
    text = open(sch).read()
    # Two banks -> two single rails -> 2 wires; 3 + 3 common pins -> 6 junctions.
    assert text.count("(wire") == 2
    assert text.count("(junction") == 6
    # Directive properties must not leak into the schematic symbol fields.
    assert "resistor_bank" not in text


@pytest.mark.skipif(
    shutil.which("kicad-cli") is None, reason="kicad-cli not available"
)
def test_resistor_bank_netlist_is_unchanged(tmp_path):
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
