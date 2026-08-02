"""
KiCad-I/O adapter for connectivity-based reference preallocation
(wayfinder #58).

This module bridges the pure, KiCad-I/O-free matching algorithm in
``circuit_synth.core.reference_preallocation`` with a real, already-
generated KiCad project on disk: it loads the existing project's sheet
hierarchy (reusing ``HierarchicalSynchronizer``'s own sheet-discovery
machinery), extracts each sheet's components' pin -> net connectivity by
reusing ``APISynchronizer._get_pin_labels()`` (the same per-pin,
geometry-aware label/power-symbol lookup the existing fuzzy sync
strategies are themselves built on), matches each sheet against the
corresponding Python subcircuit's own (already-finalized) components, and
finally drives ``Circuit.remap_preallocated_references()`` to rename
matched components into place.

Design note -- reusing sheet discovery AND per-pin label lookup, but not
the fuzzy connectivity tracer
--------------------------------------------------------------------------
``APISynchronizer``/``HierarchicalSynchronizer`` already do several
different jobs that could all loosely be called "connectivity extraction":

1. Sheet discovery + loading (``HierarchicalSynchronizer._build_hierarchy``/
   ``_load_sheet_hierarchy``) -- walks a project's ``.kicad_sch`` hierarchy
   and loads each sheet's ``kicad_sch_api.Schematic`` (and its own
   ``APISynchronizer``). Reused here directly (by constructing a real
   ``HierarchicalSynchronizer`` and reading its ``root_sheet`` tree) --
   there's nothing sheet-discovery-specific about *this* ticket's job, so
   there's no reason to reimplement it.
2. Per-pin label lookup (``APISynchronizer._get_pin_labels()``,
   ``synchronizer.py:559``) -- computes each pin's real absolute position
   from the symbol library's own geometry (rotation/mirror-aware) and
   matches it, within a distance tolerance, against the schematic's actual
   regular labels, hierarchical labels, and power symbols. This IS reused
   directly here (``_existing_records_for_sheet()`` below), because it's
   the one existing primitive that's actually pin-accurate for how
   circuit-synth writes connectivity to disk (labels/power-symbols at
   computed pin positions, not classic wire-traced net objects).
   Originally this adapter instead tried ``kicad_sch_api.Schematic``'s own
   ``get_net_for_pin()``/``list_component_pins()`` -- a superficially
   simpler, more direct-looking primitive -- but that turned out (verified
   empirically in a disposable sandbox project, against both a
   freshly-generated and a freshly-synced schematic) to always return an
   unresolved/empty net for every single pin: kicad_sch_api's
   ``NetCollection`` is a plain, manually-populated container, never
   auto-computed from a loaded file's actual wires/labels/power-symbols on
   ``Schematic.load()``. Left as a documented dead end here rather than a
   silent swap, since it's a natural thing for a future session to retry
   without this context.
3. Fuzzy, confidence-scored net-topology matching (``ConnectionTracer``,
   ``NetMatcher``, ``ConnectionMatchStrategy``) -- a *component-position*-
   granularity (not per-pin!) trace used only by the existing sync
   strategies, which only need "probably the same component" at a 0.7
   confidence threshold. This ticket's matching is deliberately exact (a
   full pin-set signature, with an explicit ordinal tiebreak for genuine
   duplicates -- see core/reference_preallocation.py), so this is
   deliberately NOT reused -- ``ConnectionTracer._build_connection_graph()``
   itself notes it adds "the component position as a pin (simplified --
   needs symbol library)" rather than real per-pin geometry, which would
   silently collapse a multi-pin component's distinct pins into one node.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...core.reference_preallocation import (
    ComponentRecord,
    build_global_net_map,
    match_sheet_prefix_group,
    ref_prefix,
)
from .hierarchical_synchronizer import HierarchicalSheet, HierarchicalSynchronizer

logger = logging.getLogger(__name__)


def _build_alias_map(top_circuit) -> Dict[str, str]:
    """Build an ``{elaborated_alias_name: canonical_positional_net_name}``
    lookup across every aliased :class:`~circuit_synth.core.bus.Bus` in
    ``top_circuit`` (and all its subcircuits), for normalizing the existing
    KiCad project's on-disk pin labels back to the same canonical net-name
    vocabulary the new (Python) side always uses -- see
    :func:`_existing_records_for_sheet`'s docstring for why this is needed.

    Only aliased buses contribute entries (``Bus.translations`` is empty for
    a plain vector bus, which was never retexted in the first place). A
    plain-vector-bus member's own name IS its canonical name already, so it
    needs no entry here.
    """
    alias_map: Dict[str, str] = {}
    for bus in top_circuit._all_buses():
        for positional_name, alias_name in bus.translations:
            alias_map[alias_name] = positional_name
    return alias_map


def _existing_records_for_sheet(
    synchronizer, alias_map: Optional[Dict[str, str]] = None
) -> List[ComponentRecord]:
    """Build ComponentRecords for every real (non-power-symbol) component
    directly on one loaded sheet.

    Uses ``APISynchronizer._get_pin_labels()`` -- the same, already-existing
    per-pin connectivity extraction the fuzzy sync strategies are built on
    (``synchronizer.py:559``) -- rather than ``kicad_sch_api``'s own
    ``Schematic.get_net_for_pin()``/``list_component_pins()``. The latter
    looked like a simpler, more direct primitive, but turned out (confirmed
    empirically, in a disposable sandbox project, against both a
    freshly-generated and a freshly-synced schematic) to always return an
    empty/unresolved net for every pin on a circuit-synth-generated
    schematic: kicad_sch_api's ``NetCollection`` is a plain, manually-
    populated container (``add``/``remove``/``find``), never auto-computed
    from a loaded file's actual wires/labels/power-symbols on
    ``Schematic.load()``. ``_get_pin_labels()`` instead computes each pin's
    real absolute position from the symbol library's own pin geometry
    (rotation/mirror-aware) and matches it against the schematic's actual
    regular labels, hierarchical labels, and power symbols within a
    distance tolerance -- exactly how circuit-synth itself writes pin
    connectivity to disk (bare/hierarchical labels and power symbols at
    computed pin positions, not wire-traced net objects) -- so it's the
    correct existing machinery to reuse here, not the superficially
    simpler kicad_sch_api net API.

    Alias normalization (wayfinder #59)
    ------------------------------------
    ``_get_pin_labels()`` returns whatever label text is actually sitting at
    a pin's position, verbatim. For a pin wired to an *aliased* Bus member
    (``Bus(name, members=[...])``), that text is **not** the bus member's
    canonical, positional net name (``ETH_8``) -- ``bus_emit.py``'s
    ``_retext_pin_labels()`` rewrites every real per-pin occurrence of the
    positional name to the member's elaborated alias (``ETH_TX_EN``) *in
    place*, for on-schematic readability. This is a label-only rewrite (the
    underlying KiCad net identity, and this module's own new/Python-side
    signatures via ``pin.net.name``, both stay canonical/positional) -- but
    it means a naive ``label.text`` comparison between the two sides can
    never agree for any aliased-bus-connected pin: the existing/old side
    always yields the alias form, the new/Python side always yields the
    canonical form. Confirmed empirically (wayfinder #59 investigation):
    with no normalization, this made the connectivity-signature matcher
    fail for essentially every ``_ser()``-created series-termination
    resistor and other aliased-bus-connected component in this project --
    a handful appeared to "match" anyway only because they got rescued by
    the separate, coarser legacy fuzzy matcher (``_match_components``'s own
    ``ConnectionTracer``/position-based strategies) elsewhere in the sync
    pipeline, which succeeds unpredictably -- the exact unreliable fallback
    behavior issue #58 was chartered to eliminate. ``alias_map`` (built by
    :func:`_build_alias_map` from the *new* side's own live ``Bus`` objects,
    the single source of truth for which alias maps to which canonical net)
    translates any alias-form label text back to its canonical form before
    it ever becomes part of a signature, restoring an apples-to-apples
    comparison. Text not present in ``alias_map`` (already-canonical names,
    net names outside any aliased bus, power-net names, etc.) passes through
    unchanged.
    """
    records = []
    schematic = synchronizer.schematic
    alias_map = alias_map or {}
    for comp in schematic.components:
        ref = getattr(comp, "reference", None)
        if not ref or ref.startswith("#"):
            continue  # power symbols (#PWR*) and sheet-internal flags aren't matching candidates
        prefix = ref_prefix(ref)
        pin_labels = synchronizer._get_pin_labels(comp)  # {pin_num: (Label, label_type)}
        pins: Dict[str, Optional[str]] = {
            pin_num: (alias_map.get(label.text, label.text) if label.text else None)
            for pin_num, (label, _label_type) in pin_labels.items()
        }
        records.append(
            ComponentRecord(
                identity=ref,
                prefix=prefix,
                value=getattr(comp, "value", None),
                pins=pins,
            )
        )
    return records


def build_existing_project_snapshot(project_path: str, alias_map: Optional[Dict[str, str]] = None):
    """Load an existing KiCad project and build (per_sheet, global_net_map)
    -- the same shape core/reference_preallocation.py's matcher expects for
    the "existing" side, keyed by KiCad sheet name.

    Args:
        project_path: path to the project's ``.kicad_pro`` file (or its
            directory -- the root ``.kicad_sch``/``root.kicad_sch`` is
            resolved the same way ``HierarchicalSynchronizer`` does).
        alias_map: optional ``{elaborated_alias_name: canonical_net_name}``
            lookup (see :func:`_build_alias_map`) used to normalize any
            aliased-bus pin label text back to its canonical, positional
            form -- see :func:`_existing_records_for_sheet`'s "Alias
            normalization" docstring section for why this is needed.
            ``None``/empty is safe (no normalization applied), which is
            correct for a project with no aliased buses at all.

    Returns:
        (per_sheet, global_net_map): ``per_sheet`` maps each sheet's own
        name (matching ``HierarchicalSheet.name``, which is what
        ``Circuit.name`` is matched against -- see
        ``preallocate_from_existing_project`` below) to a list of
        ``ComponentRecord``. ``global_net_map`` spans the WHOLE project
        (every sheet), since a net (e.g. GND) can cross sheet boundaries.
    """
    synchronizer = HierarchicalSynchronizer(str(project_path))

    per_sheet: Dict[str, List[ComponentRecord]] = {}
    all_records: List[ComponentRecord] = []

    def _walk(sheet: HierarchicalSheet):
        if sheet.synchronizer is not None:
            records = _existing_records_for_sheet(sheet.synchronizer, alias_map)
            per_sheet[sheet.name] = records
            all_records.extend(records)
        for child in sheet.children:
            _walk(child)

    _walk(synchronizer.root_sheet)

    global_net_map = build_global_net_map(all_records)
    return per_sheet, global_net_map


def _sheet_name_candidates(kicad_sheet_name: str) -> List[str]:
    """A KiCad sheet's own name may or may not carry a .kicad_sch suffix
    depending on how it was discovered (see
    HierarchicalSynchronizer._find_circuit_for_sheet, which this mirrors)."""
    candidates = [kicad_sheet_name]
    if kicad_sheet_name.endswith(".kicad_sch"):
        candidates.append(kicad_sheet_name[: -len(".kicad_sch")])
    return candidates


def preallocate_from_existing_project(top_circuit, project_path: str) -> Dict[str, str]:
    """Match ``top_circuit`` (a fully-built, already-finalized Circuit tree
    -- see Circuit.remap_preallocated_references()'s docstring for why
    "already-finalized" is the correct state to match from) against the
    KiCad project at ``project_path``, and rename matched components into
    their old, stable references in place.

    Returns the ``{old_ref: new_ref}`` rename dict from
    ``Circuit.remap_preallocated_references()`` (empty if nothing needed
    renaming -- including, harmlessly, when ``project_path`` doesn't
    resolve to a loadable project at all, which is logged and treated the
    same as "nothing to preallocate against" rather than raised, since a
    first/clean generation must be unaffected by this step).
    """
    try:
        alias_map = _build_alias_map(top_circuit)
        existing_per_sheet, existing_global_map = build_existing_project_snapshot(
            project_path, alias_map
        )
    except Exception:
        logger.warning(
            "Could not load existing project for reference preallocation -- "
            "skipping (falling back to ordinary counter-based references)",
            exc_info=True,
        )
        return {}

    new_per_sheet, new_global_map, components_by_id = top_circuit.collect_preallocation_records()

    # Build a lookup from Python circuit name -> matching KiCad sheet name(s)
    # present in the existing project, mirroring
    # HierarchicalSynchronizer._find_circuit_for_sheet's own matching
    # direction (KiCad sheet -> Python subcircuit) but inverted, since here
    # we're iterating the *new* side's sheets and looking for their
    # existing-side counterpart.
    existing_sheet_names = set(existing_per_sheet.keys())

    total_matches = 0
    for circuit_name, new_records in new_per_sheet.items():
        matched_sheet_name = None
        if circuit_name in existing_sheet_names:
            matched_sheet_name = circuit_name
        else:
            for candidate in existing_sheet_names:
                if circuit_name in _sheet_name_candidates(candidate):
                    matched_sheet_name = candidate
                    break
        if matched_sheet_name is None:
            continue

        existing_records = existing_per_sheet[matched_sheet_name]
        prefixes = {r.prefix for r in new_records} | {r.prefix for r in existing_records}
        for prefix in prefixes:
            new_group = [r for r in new_records if r.prefix == prefix]
            existing_group = [r for r in existing_records if r.prefix == prefix]
            if not new_group or not existing_group:
                continue
            matches = match_sheet_prefix_group(
                new_group, existing_group, new_global_map, existing_global_map
            )
            for new_identity, existing_ref in matches.items():
                components_by_id[new_identity]._preallocated_ref = existing_ref
                total_matches += 1

    logger.info(
        "Reference preallocation: matched %d component(s) against existing project %s",
        total_matches,
        project_path,
    )

    return top_circuit.remap_preallocated_references()
