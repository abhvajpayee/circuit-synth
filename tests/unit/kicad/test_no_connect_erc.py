"""End-to-end test for Pin.no_connect(): real circuit -> real generated
.kicad_sch -> real `kicad-cli sch erc`.

Complements test_no_connect_marker_position.py (which isolates the
position math with fakes) by exercising the full pipeline this project's
schematic-design agent actually runs: core Pin/Component -> JSON ->
circuit_loader -> SchematicWriter -> a real KiCad file that real kicad-cli
can parse and ERC.

Mirrors the generation + kicad-cli pattern already used in
test_bus_alias_retext.py.
"""
import json
import os
import shutil
import subprocess

import pytest

from circuit_synth import Component, Net, circuit


def _circuit_with_no_connect():
    @circuit(name="nc_erc")
    def nc_erc():
        # A single-unit 8-pin connector standing in for an IC with some
        # RFU/spare pins -- e.g. the acquisition board's real _nc() sites
        # (STM32H755, TPM, eMMC, ...) are all single-unit KiCad symbols too.
        # Deliberately NOT a multi-unit part (e.g. LM358): SchematicWriter's
        # `component_manager.find_component(ref)` always resolves a
        # multi-unit reference to its unit1 SchematicSymbol regardless of
        # which unit a given pin actually lives on (component_manager.py
        # find_component()) -- a pre-existing limitation shared by
        # `_add_pin_level_net_labels`'s identical lookup (net-connected
        # pins on a non-unit1 unit get the same mis-position), not
        # something newly introduced here. Out of scope for this feature;
        # noted so a future fix benefits both label and no-connect
        # placement identically.
        u1 = Component(symbol="Connector_Generic:Conn_01x08", ref="U1")
        j1 = Component(symbol="Connector_Generic:Conn_01x08", ref="J1")

        for pin in (1, 2, 3, 4, 8):
            n = Net(f"SIG{pin}")
            n += u1[pin]
            n += j1[pin]

        # Pins 5, 6, 7 intentionally unused.
        u1[5].no_connect(reason="spare, unused in this design")
        u1[6].no_connect(reason="spare, unused in this design")
        u1[7].no_connect(reason="spare, unused in this design")

    return nc_erc()


def _generate(circ, outdir):
    os.makedirs(outdir, exist_ok=True)
    cwd = os.getcwd()
    os.chdir(outdir)
    try:
        circ.generate_kicad_project(project_name="nc_erc", generate_pcb=False)
    finally:
        os.chdir(cwd)
    return os.path.join(outdir, "nc_erc")


def _root_sch(proj_dir):
    cands = [f for f in os.listdir(proj_dir) if f.endswith(".kicad_sch")]
    # Root sheet shares the project name.
    root = [f for f in cands if f == "nc_erc.kicad_sch"]
    assert root, f"no root .kicad_sch found among {cands}"
    return os.path.join(proj_dir, root[0])


def test_no_connect_element_present_in_generated_schematic(tmp_path):
    proj = _generate(_circuit_with_no_connect(), str(tmp_path))
    sch_path = _root_sch(proj)
    text = open(sch_path).read()

    assert text.count("(no_connect") == 3, (
        "expected exactly 3 no_connect elements (U1 pins 5, 6, 7)"
    )


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not available")
def test_no_connect_pins_do_not_report_pin_not_connected(tmp_path):
    proj = _generate(_circuit_with_no_connect(), str(tmp_path))
    sch_path = _root_sch(proj)

    out_json = tmp_path / "erc.json"
    subprocess.run(
        ["kicad-cli", "sch", "erc", "--output", str(out_json),
         "--format", "json", "--severity-all", sch_path],
        check=True, cwd=proj, capture_output=True, text=True,
    )
    erc = json.loads(out_json.read_text())

    pin_not_connected_descs = []
    for sheet in erc.get("sheets", []):
        for v in sheet.get("violations", []):
            if v["type"] != "pin_not_connected":
                continue
            for item in v.get("items", []):
                pin_not_connected_descs.append(item.get("description", ""))

    for pin_no in ("5", "6", "7"):
        offending = [d for d in pin_not_connected_descs
                     if f"Symbol U1 Pin {pin_no} " in d]
        assert not offending, (
            f"U1 pin {pin_no} still reported pin_not_connected: {offending}"
        )


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not available")
def test_erc_still_parses_the_file_cleanly(tmp_path):
    """A sanity check independent of the specific violation list: kicad-cli
    must be able to run ERC on the file at all (paren-balanced, valid
    s-expression) -- see the kicad-sch-api-manual-edit-gotchas false-negative
    trap this project already hit with a different (unrelated) writer bug."""
    proj = _generate(_circuit_with_no_connect(), str(tmp_path))
    sch_path = _root_sch(proj)

    text = open(sch_path).read()
    assert text.count("(") == text.count(")")

    out_json = tmp_path / "erc2.json"
    result = subprocess.run(
        ["kicad-cli", "sch", "erc", "--output", str(out_json),
         "--format", "json", "--severity-all", sch_path],
        cwd=proj, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
