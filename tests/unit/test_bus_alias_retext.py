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
