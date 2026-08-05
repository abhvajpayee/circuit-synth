"""
Incremental-sync fix (wayfinder #65): reconcile missing sheet-symbol pins.

Root cause: nothing in the incremental-sync pipeline ever adds a NEW
`SheetPin` to an EXISTING sheet symbol. `SheetPin` objects are only ever
created in one place, `schematic_writer.py`'s `_add_subcircuit_sheets()`
(clean generation, which mints a brand-new `Sheet` -- with a fresh UUID --
every run, so its pin list is always freshly computed from the current
circuit). `kicad_sch_api`'s own `SheetManager.add_sheet_pin()` exists and
works (verified directly), but circuit-synth's incremental-sync codepath
(`synchronizer.py` / `hierarchical_synchronizer.py`) never calls it --
`kicad.schematic.sheet_manager.SheetManager.add_sheet_pin()` (a *different*,
now-superseded, circuit-synth-local wrapper) is unreferenced dead code.

Symptom: a brand-new net threaded as a new parameter through an EXISTING
intermediate sheet's call chain (e.g. `computeSetup()` in this project) to
reach a new component several sheet-levels away gets a hierarchical_label
(or, for a zero-local-component wrapper sheet, nothing at all) at its
various component-pin use sites, but the intermediate sheet's own
sheet-symbol (on its parent's canvas) never gains the matching pin --
`kicad-cli sch erc` then reports the label as unconnected
("Hierarchical label '...' in root sheet cannot be connected to
non-existent parent sheet" at the true root, `hier_label_mismatch`
elsewhere).

This module reconciles every existing (parent, child) sheet-symbol edge in
the already-generated KiCad project against the Python circuit hierarchy's
own boundary-crossing net set (see `boundary_nets.py`) and adds whatever
pins -- and, where nothing on the parent's own canvas already represents
the net, a matching tie label -- are missing. It only ever ADDS: never
removes a pin/label whose net no longer crosses a boundary, matching this
project's established "additive only" incremental-sync philosophy (see
`docs/adr` / wayfinder map #54's notes on unverified deletion scenarios).

Exact pin positions/justify are not load-bearing here: `sch_postprocess.py`'s
`fix_sheet_symbol_sizes()` always runs immediately afterward (both after a
clean regen and after incremental sync -- see `core/circuit.py`'s
`_postprocess_schematic()`) and fully recomputes every sheet symbol's size,
pin split, and tied-label position from the pins actually present. This
function's only job is to make sure the RIGHT SET of pins/labels exists
before that pass runs; final cosmetic layout is that pass's job, not this
one's. New pins are still placed on the sheet's "right" edge, and new tie
labels colocated with the *current* unshifted right edge (`sheet_x +
sheet_width`), specifically so `fix_sheet_symbol_sizes()`'s own
`label_updates` lookup (keyed on that same coordinate) finds and correctly
repositions them on this same run, exactly like a label placed by
`schematic_writer.py`'s clean-generation path.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Set

import kicad_sch_api as ksa

from .. import boundary_nets

# Matches a KiCad bus-vector sheet-pin name, e.g. "ETH_DN_[0..3]" or
# "FMC_D[0..15]" -- see bus_emit.py, which draws these. A net's own name at
# the JSON-netlist level is always its plain positional member name (e.g.
# "ETH_DN_2"), even for an ALIASED bus (only the component-pin-level LABEL
# TEXT gets alias-elaborated, never the sheet-symbol pin itself -- see this
# project's "aliased bus pin retext" memory) -- so this same numeric-range
# match covers both plain and aliased buses.
_VECTOR_PIN_RE = re.compile(r"^(.*)\[(\d+)\.\.(\d+)\]$")

logger = logging.getLogger(__name__)

PIN_PITCH = 2.54  # mm, KiCad 50mil grid -- matches sch_postprocess.py
MARGIN_TOP = 2.54


def reconcile_sheet_pins(
    project_dir: Path,
    root_circuit_name: str,
    subcircuits: Dict[str, Any],
) -> List[str]:
    """
    Walk the circuit hierarchy (via each circuit's own `child_instances`)
    and, for every (parent, child) sheet-symbol edge, ensure the parent's
    on-disk sheet symbol for that child exposes a `SheetPin` for every net
    `boundary_nets.net_crosses_boundary()` says must cross there. Adds a
    matching tie label on the parent's own canvas when the net isn't
    already represented there by anything else (an existing component
    pin's own label, or another tie).

    Args:
        project_dir: Directory containing the project's `.kicad_sch` files
            (one per circuit name, e.g. `ComputeSetup.kicad_sch`).
        root_circuit_name: Name of the top-level circuit (its own file is
            `{root_circuit_name}.kicad_sch`, same convention as every other
            circuit -- this project's generator names the root file after
            `@circuit(name=...)`, not a separately-tracked "project name").
        subcircuits: `{circuit_name: Circuit}` as returned by
            `circuit_loader.load_circuit_hierarchy()` -- the same dict
            `schematic_writer.py` calls `all_subcircuits`.

    Returns:
        Human-readable change descriptions, one per pin (and, where
        applicable, tie label) added. Empty if nothing needed to change.

    Caller is responsible for `KICAD_SYMBOL_DIR` being set before calling
    this (same requirement as any other `kicad_sch_api.Schematic.load()` +
    `.save()` round-trip in this codebase -- see the schematic-design
    agent's documented gotcha: without it, a resave can silently drop the
    file's own `lib_symbols` cache for symbols it didn't even touch).
    """
    changes: List[str] = []

    # (parent_name, child_name) edges, derived from the Python circuit tree
    # itself -- not from the KiCad files -- so this is correct even before
    # any pin exists on disk yet.
    children_by_parent: Dict[str, List[str]] = {}

    def _walk(name: str, seen: Set[str]):
        if name in seen:
            return  # guard against a pathological cycle
        seen = seen | {name}
        circ = subcircuits.get(name)
        if circ is None:
            return
        for child_info in getattr(circ, "child_instances", []) or []:
            sub_name = child_info.get("sub_name")
            if not sub_name:
                continue
            children_by_parent.setdefault(name, []).append(sub_name)
            _walk(sub_name, seen)

    _walk(root_circuit_name, set())

    owners = boundary_nets.net_owner_circuits(subcircuits)

    for parent_name, child_names in children_by_parent.items():
        parent_path = project_dir / f"{parent_name}.kicad_sch"
        if not parent_path.exists():
            logger.warning(
                f"reconcile_sheet_pins: parent schematic not found, skipping: {parent_path}"
            )
            continue

        sch = None  # lazily loaded -- avoid touching a file with nothing to add
        modified = False

        for child_name in child_names:
            pin_list = sorted(
                n
                for n in boundary_nets.subtree_net_names(subcircuits, child_name)
                if boundary_nets.net_crosses_boundary(subcircuits, child_name, n, owners)
            )
            if not pin_list:
                continue

            if sch is None:
                sch = ksa.Schematic.load(str(parent_path))

            sheet_dict = sch.sheets.get_sheet_by_filename(f"{child_name}.kicad_sch")
            if sheet_dict is None:
                logger.warning(
                    f"reconcile_sheet_pins: no sheet symbol for '{child_name}.kicad_sch' "
                    f"found in {parent_path} (parent circuit '{parent_name}') -- skipping"
                )
                continue

            existing_pin_names = {p["name"] for p in sheet_dict["pins"]}
            missing = [
                n
                for n in pin_list
                if n not in existing_pin_names
                and not _covered_by_existing_vector_pin(n, existing_pin_names)
            ]
            if not missing:
                continue

            sheet_uuid = sheet_dict["uuid"]
            sx = sheet_dict["position"]["x"]
            sy = sheet_dict["position"]["y"]
            width = sheet_dict["size"]["width"]
            tie_x = sx + width  # unshifted right edge -- see module docstring

            for net_name in missing:
                pos_along_edge = MARGIN_TOP + len(sheet_dict["pins"]) * PIN_PITCH
                sch.sheets.add_sheet_pin(
                    sheet_uuid, net_name, "bidirectional", "right", pos_along_edge
                )
                tie_y = sy + pos_along_edge
                modified = True
                changes.append(
                    f"{parent_path.name}: added sheet pin '{net_name}' on "
                    f"'{child_name}' sheet symbol"
                )

                # A SheetPin's connectivity to the rest of its own sheet is
                # position-based, not just same-text-anywhere: verified
                # directly against a real, already-working reference in this
                # project (PG_14V0) -- its tie label sits exactly coincident
                # with its sheet pin's own (x, y), *in addition to* a
                # second, unrelated PG_14V0 label elsewhere on the same
                # sheet at the actual driving component's pin position, not
                # touching the sheet pin at all. A same-text label that
                # merely exists SOMEWHERE else on the canvas (e.g. this
                # ticket's own new component's pin label, which can be
                # anywhere) does not satisfy that -- only skip adding a new
                # tie label if one already sits at this EXACT coordinate
                # (true duplicate), matching schematic_writer.py's own
                # unconditional per-pin tie-label emission.
                already_tied_here = any(
                    lbl.text == net_name and _same_point(lbl.position, tie_x, tie_y)
                    for lbl in sch.labels
                ) or any(
                    lbl.text == net_name and _same_point(lbl.position, tie_x, tie_y)
                    for lbl in sch.hierarchical_labels
                )
                if already_tied_here:
                    continue

                # Same generic rule as everywhere else in this fix: no
                # root-position special case -- net_crosses_boundary()
                # already evaluates False for the root circuit by
                # construction (see its docstring).
                hierarchical = boundary_nets.net_crosses_boundary(
                    subcircuits, parent_name, net_name, owners
                )
                if hierarchical:
                    label_uuid = sch.add_hierarchical_label(
                        text=net_name,
                        position=(tie_x, tie_y),
                        shape="bidirectional",
                        rotation=0.0,
                    )
                    _set_hierarchical_label_justify(sch, label_uuid, "left")
                    changes.append(
                        f"{parent_path.name}: added hierarchical tie label "
                        f"'{net_name}' at ({tie_x:.2f}, {tie_y:.2f})"
                    )
                else:
                    sch.add_label(text=net_name, position=(tie_x, tie_y), rotation=0.0)
                    changes.append(
                        f"{parent_path.name}: added local tie label "
                        f"'{net_name}' at ({tie_x:.2f}, {tie_y:.2f})"
                    )

        if modified and sch is not None:
            sch.save(str(parent_path), preserve_format=False)
            logger.info(f"reconcile_sheet_pins: saved {parent_path}")

    return changes


def _same_point(pos, x: float, y: float, tol: float = 0.01) -> bool:
    """True if `pos` (a kicad_sch_api Point-like with .x/.y) is within `tol` mm of (x, y)."""
    try:
        return abs(pos.x - x) < tol and abs(pos.y - y) < tol
    except AttributeError:
        return False


def _covered_by_existing_vector_pin(net_name: str, existing_pin_names: Set[str]) -> bool:
    """
    True if `net_name` is a positional member of a bus that already has a
    vector-style sheet pin among `existing_pin_names` (e.g. net "ETH_DN_2"
    is covered by an existing pin literally named "ETH_DN_[0..3]").

    This guard exists because `boundary_nets` reasons about individual net
    names only -- at the JSON-netlist level a bus is just N separately-named
    nets, no different from N unrelated scalar nets, so a bus member net
    that already crosses this exact boundary via its OWN vector pin (added
    and maintained entirely by the existing, separate `bus_emit.py` /
    `inject_buses()` machinery -- wayfinder #55/#57/#62) would otherwise look
    "missing" by plain name comparison and get a second, redundant,
    individually-named pin added right alongside the vector one. Found via a
    real regression while testing this fix on the acquisition_mcu board:
    adding such a duplicate pin for `ETH_DN_0..3` (unrelated to this
    ticket's own EN_12V/FLT_* nets -- just another bus already present on
    the same sheet) confused `inject_buses()`'s own bus-repair pass on the
    very same run, which produced duplicate labels. Bus-crossing pins are
    entirely `bus_emit.py`'s territory; this function's only job is to
    politely stay out of it.
    """
    for pin_name in existing_pin_names:
        m = _VECTOR_PIN_RE.match(pin_name)
        if not m:
            continue
        prefix, lo, hi = m.group(1), int(m.group(2)), int(m.group(3))
        if not net_name.startswith(prefix):
            continue
        suffix = net_name[len(prefix):]
        if suffix.isdigit() and lo <= int(suffix) <= hi:
            return True
    return False


def _set_hierarchical_label_justify(sch, label_uuid: str, justify: str) -> None:
    """
    Best-effort match of `synchronizer.py`'s `_add_pin_label()` justify
    patch -- cosmetic only (`fix_sheet_symbol_sizes()` overwrites it again
    on the very same run once the sheet symbol it ties to gets its final
    pin split), so silently no-ops if the internal `_data` shape isn't what's
    expected rather than raising.
    """
    try:
        if hasattr(sch, "_data") and "hierarchical_labels" in sch._data:
            for label_dict in sch._data["hierarchical_labels"]:
                if label_dict.get("uuid") == label_uuid:
                    label_dict.setdefault("effects", {})["justify"] = justify
                    break
    except Exception:
        logger.debug("Could not set justify on new hierarchical tie label", exc_info=True)
