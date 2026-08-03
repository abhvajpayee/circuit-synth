"""Regression tests for no-connect marker reconciliation during incremental sync.

`(no_connect ...)` markers were emitted only by the fresh-generate path
(`SchematicWriter._add_no_connect_markers`). `synchronizer.py` had no notion of
them at all, so an incremental sync (`force_regenerate=False`):

* never created a marker for a pin newly given `Pin.no_connect()`, and
* never removed one for a pin that stopped being no-connect and got wired.

The second case is the damaging one. Sync happily rewrites the pin's label to
the new net, but the stale marker stays behind on that same point -- which is
precisely KiCad ERC's `no_connect_connected`, "a pin with a 'no connection'
flag is connected".

Found in windTunnelProject wayfinder #64. Seven STM32H755 GPIOs were repurposed
out of the MCU sheet's spare-pin no-connect registry into front-panel user I/O;
all seven kept their stale markers through the sync, and the one whose net ended
up with a second labelled pin reported `no_connect_connected`. A clean `--force`
regen of the identical source reported none, because it rebuilds the marker list
from the Python source instead of inheriting it from the file.

Note this is *not* the placement bug tracked in the same ticket (see
tests/unit/kicad/test_placement_overflow_fallback.py). The two were originally
believed to be one -- "the placer dropped a switch onto an existing no-connect
marker" -- but measurement showed the switch sat 90mm away from the marker, on a
different component entirely. Fixing placement alone leaves this defect firing.
"""

import contextlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import kicad_sch_api as ksa
import pytest

from circuit_synth import Component, Net, circuit
from circuit_synth.core.circuit import Circuit
from circuit_synth.core.decorators import get_current_circuit, set_current_circuit


@contextlib.contextmanager
def _isolated_build():
    """Build one circuit under a private root.

    `tests/conftest.py`'s autouse `mock_active_circuit` fixture installs a single
    shared root per test, and every `@circuit` builder called during that test
    attaches to it -- so building two circuits that both declare `U1` collides on
    the reference before any sync runs. Each build here gets its own throwaway
    root instead. Test-harness concern only; nothing to do with the behaviour
    under test.
    """
    previous = get_current_circuit()
    set_current_circuit(Circuit(name="NcSyncIsolatedRoot"))
    try:
        yield
    finally:
        set_current_circuit(previous)


def _build(builder):
    with _isolated_build():
        return builder()


def _find_schematic(project_dir: Path):
    candidates = sorted(project_dir.rglob("*.kicad_sch"))
    assert candidates, f"no .kicad_sch generated under {project_dir}"
    named = [p for p in candidates if p.stem == project_dir.name]
    return named[0] if named else candidates[0]


def _no_connect_positions(sch_path: Path):
    sch = ksa.Schematic.load(str(sch_path))
    markers = sch._data.get("no_connects", [])
    return {
        (round(float(m["position"]["x"]), 3), round(float(m["position"]["y"]), 3))
        for m in markers
    }


def _pin_tip(sch_path: Path, reference: str, pin_number: str):
    from circuit_synth.kicad.sch_gen.schematic_writer import pin_tip_xy

    sch = ksa.Schematic.load(str(sch_path))
    comp = sch.components.get(reference)
    assert comp is not None, f"{reference} missing from {sch_path}"
    xy = pin_tip_xy(comp, pin_number)
    assert xy is not None, f"could not resolve {reference} pin {pin_number}"
    return (round(xy[0], 3), round(xy[1], 3))


def _v1():
    """U1 pins 5/6/7 are spare and declared no-connect."""

    @circuit(name="nc_sync")
    def nc_sync():
        u1 = Component(symbol="Connector_Generic:Conn_01x08", ref="U1")
        j1 = Component(symbol="Connector_Generic:Conn_01x08", ref="J1")
        for pin in (1, 2, 3, 4, 8):
            net = Net(f"SIG{pin}")
            net += u1[pin]
            net += j1[pin]
        u1[5].no_connect(reason="spare, unused in this design")
        u1[6].no_connect(reason="spare, unused in this design")
        u1[7].no_connect(reason="spare, unused in this design")

    return nc_sync()


def _v2():
    """Pin 5 is repurposed: wired to J1 pin 5 instead of being no-connect.

    The same shape as the real incident -- a pin moved out of a spare/no-connect
    registry and given a real signal, with everything else untouched.
    """

    @circuit(name="nc_sync")
    def nc_sync():
        u1 = Component(symbol="Connector_Generic:Conn_01x08", ref="U1")
        j1 = Component(symbol="Connector_Generic:Conn_01x08", ref="J1")
        for pin in (1, 2, 3, 4, 5, 8):
            net = Net(f"SIG{pin}")
            net += u1[pin]
            net += j1[pin]
        u1[6].no_connect(reason="spare, unused in this design")
        u1[7].no_connect(reason="spare, unused in this design")

    return nc_sync()


def _v3():
    """Pin 4 additionally becomes no-connect, on top of v2's pin-5 rewiring."""

    @circuit(name="nc_sync")
    def nc_sync():
        u1 = Component(symbol="Connector_Generic:Conn_01x08", ref="U1")
        j1 = Component(symbol="Connector_Generic:Conn_01x08", ref="J1")
        for pin in (1, 2, 3, 5, 8):
            net = Net(f"SIG{pin}")
            net += u1[pin]
            net += j1[pin]
        u1[4].no_connect(reason="newly retired")
        u1[6].no_connect(reason="spare, unused in this design")
        u1[7].no_connect(reason="spare, unused in this design")

    return nc_sync()


def _generate(circ, out_dir: Path, force: bool):
    circ.generate_kicad_project(
        str(out_dir), force_regenerate=force, generate_pcb=False
    )


def test_stale_no_connect_marker_removed_when_pin_becomes_wired():
    """The reported symptom: a pin repurposed from no-connect to wired must not
    keep its marker through an incremental sync."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "nc_sync"
        _generate(_build(_v1), out, force=True)
        sch = _find_schematic(out)

        pin5 = _pin_tip(sch, "U1", "5")
        assert pin5 in _no_connect_positions(sch), (
            "precondition failed: U1 pin 5 should start with a no-connect marker"
        )
        before = _no_connect_positions(sch)
        assert len(before) == 3

        _generate(_build(_v2), out, force=False)

        after = _no_connect_positions(sch)
        assert pin5 not in after, (
            "stale no-connect marker survived on U1 pin 5 after it was wired"
        )
        assert len(after) == 2, f"expected 2 remaining markers, got {sorted(after)}"
        # The two pins that are still no-connect must keep their markers.
        assert _pin_tip(sch, "U1", "6") in after
        assert _pin_tip(sch, "U1", "7") in after


def test_new_no_connect_marker_added_for_newly_marked_pin():
    """The mirror case: a pin newly given `Pin.no_connect()` must gain a marker."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "nc_sync"
        _generate(_build(_v2), out, force=True)
        sch = _find_schematic(out)

        pin4 = _pin_tip(sch, "U1", "4")
        assert pin4 not in _no_connect_positions(sch)

        _generate(_build(_v3), out, force=False)

        after = _no_connect_positions(sch)
        assert pin4 in after, "no-connect marker not created for newly marked U1 pin 4"


def test_repeated_sync_is_idempotent_for_no_connect_markers():
    """Syncing the same source twice must not duplicate or drop markers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "nc_sync"
        # Build once and reuse: each call of a `@circuit` builder registers a
        # new subcircuit under the session's active circuit, so calling the same
        # builder three times collides on U1 before the sync is ever reached.
        c = _build(_v1)
        _generate(c, out, force=True)
        sch = _find_schematic(out)

        _generate(c, out, force=False)
        once = _no_connect_positions(sch)
        _generate(c, out, force=False)
        twice = _no_connect_positions(sch)

        assert once == twice, f"markers drifted across syncs: {once} -> {twice}"
        assert len(twice) == 3


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not available")
def test_sync_result_matches_clean_regen_erc():
    """End-to-end bar: an incrementally synced project must report the same ERC
    violations as a clean `--force` regen of the identical source.

    This is the property the real board is measured against (137 violations
    either way). Before the fix the synced project carried an extra
    `no_connect_connected` the regenerated one did not."""
    with tempfile.TemporaryDirectory() as tmpdir:
        after = _build(_v2)  # built once, used for both the sync and the clean regen

        synced = Path(tmpdir) / "nc_sync"
        _generate(_build(_v1), synced, force=True)
        _generate(after, synced, force=False)

        regen = Path(tmpdir) / "regen" / "nc_sync"
        regen.parent.mkdir(parents=True, exist_ok=True)
        _generate(after, regen, force=True)

        def erc(project_dir: Path):
            sch = _find_schematic(project_dir)
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

        assert synced_types.get("no_connect_connected", 0) == 0, (
            f"synced project reports no_connect_connected: {synced_types}"
        )
        assert regen_types.get("no_connect_connected", 0) == 0

        # The marker set itself must agree with what a clean regen would write.
        assert _no_connect_positions(_find_schematic(synced)) == _no_connect_positions(
            _find_schematic(regen)
        ), "synced no-connect markers differ from a clean regen's"

        # Deliberately NOT asserting full ERC equality here. On this
        # single-flat-sheet fixture the synced project additionally reports two
        # `pin_not_connected` findings on "Hierarchical Label 'SIG5'": sync's
        # `_add_pin_label` always emits a *hierarchical* label, which dangles on a
        # root sheet with nothing above it, whereas generation emits a plain label
        # there. That is a separate, pre-existing sync/generate divergence with
        # nothing to do with no-connect markers, and it does not arise on the real
        # hierarchical board this fix was measured against (which lands on exactly
        # the clean-regen violation count). Asserting it here would tie this
        # regression test to an unrelated open defect.
        assert set(synced_types) - set(regen_types) <= {"pin_not_connected"}, (
            f"unexpected new violation categories after sync: "
            f"{set(synced_types) - set(regen_types)}"
        )
