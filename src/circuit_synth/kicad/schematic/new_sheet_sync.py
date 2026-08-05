"""
Incremental-sync fix (wayfinder #67): create sheets that exist in the Python
circuit but not yet on disk.

Root cause: incremental sync's whole notion of "which sheets exist" is read
from DISK, never from the Python circuit. ``HierarchicalSynchronizer``
builds its sheet tree in ``_build_hierarchy()`` by parsing the root
``.kicad_sch`` and recursing through the ``(sheet ...)`` blocks it finds
there, so a ``@circuit``-decorated function that has never been generated is
simply absent from that tree -- ``_sync_sheet_recursive()`` never visits it,
and nothing anywhere else in the incremental pipeline creates a
``.kicad_sch`` file. Sheet creation lives exclusively in the clean-generation
path (``main_generator.generate_project()``'s dependency-ordered writer loop
plus ``schematic_writer.py``'s ``_add_subcircuit_sheets()``), which is only
reached with ``force_regenerate=True``.

Symptom: moving an existing ``Component()`` out of an existing sheet function
into a BRAND-NEW one does not fail loudly and does not no-op -- it silently
DELETES the moved components. The old sheet's own ``APISynchronizer`` run
correctly observes that those components are no longer in its circuit and
removes them (the deliberate, working deletion path from wayfinder #60),
while the new sheet that should now own them is never created and never
synced, so nothing ever adds them back. Net effect on a real board: the
components, their labels, and their nets vanish, and the ERC violation count
DROPS -- which reads as an improvement rather than as data loss.

Fix approach: close the gap at its source, before any of the disk-driven
machinery runs. This module walks the PYTHON hierarchy (each circuit's own
``child_instances``, exactly like ``sheet_pin_sync.py``/``boundary_nets.py``
already do -- the same "reason from the circuit, not from the files"
convention), finds every circuit with no ``.kicad_sch`` on disk, and
materializes it using the clean-generation reference implementation itself
(``SchematicWriter``), not a reimplementation. Because it runs FIRST, in
``_update_existing_project()`` before ``HierarchicalSynchronizer`` is even
constructed, every later stage -- hierarchy discovery, per-sheet component
sync, ``reconcile_sheet_pins()``, ``fix_sheet_symbol_sizes()`` -- sees an
ordinary, already-existing sheet and needs no knowledge of this fix at all.

Division of labour with wayfinder #65's ``sheet_pin_sync.py``: this module
adds the new sheet SYMBOL to the parent's canvas with an EMPTY pin list, and
deliberately leaves pin population to ``reconcile_sheet_pins()``, which
already computes the correct crossing-net set (``boundary_nets``), adds each
pin, adds the matching tie label only where one isn't already present at
that exact coordinate, and stays out of ``bus_emit.py``'s way for
bus-vector pins. Emitting pins here instead would suppress
``reconcile_sheet_pins()``'s own tie-label emission (it only ties pins it
adds), leaving the new sheet symbol's pins unconnected on the parent canvas.

Scope boundary -- REPARENTING IS REFUSED, NOT ATTEMPTED: a new sheet whose
own child already exists on disk under a DIFFERENT parent is a sheet MOVE,
not a sheet creation. Handling it would require removing the old parent's
sheet symbol and rewriting every affected component's hierarchical instance
path, which is exactly the class of change the rest of incremental sync has
never supported. Rather than silently producing a duplicate sheet symbol
(the same "quietly wrong" failure mode this ticket is about), that case
raises ``NewSheetSyncError``. Deleting a sheet that disappeared from Python
is likewise still out of scope and untouched -- an orphaned ``.kicad_sch``
is preserved, consistent with incremental sync's conservative stance on
deletion everywhere else.
"""

from __future__ import annotations

import logging
import uuid as uuid_module
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import kicad_sch_api as ksa

logger = logging.getLogger(__name__)

# Layout constants for a freshly-placed sheet symbol on its parent's canvas.
# Only a starting point: sch_postprocess.fix_sheet_symbol_sizes() runs later
# on the same generate and recomputes every sheet symbol's size and pin
# split from the pins actually present (see sheet_pin_sync.py's module
# docstring, which relies on the same guarantee).
GRID = 1.27  # mm, KiCad 50mil grid
SHEET_MARGIN = 12.7  # mm of clear space left below existing content
DEFAULT_SHEET_WIDTH = 25.4
MIN_SHEET_HEIGHT = 7.62
PIN_PITCH = 2.54
MARGIN_TOP = 2.54


class NewSheetSyncError(RuntimeError):
    """Raised when a Python-side sheet cannot be materialized safely.

    Deliberately loud: the alternative that this ticket exists to eliminate
    is incremental sync quietly producing a structurally wrong project.
    """


def _snap(value: float) -> float:
    return round(value / GRID) * GRID


def _python_children(subcircuits: Dict[str, Any], root_name: str) -> Dict[str, List[str]]:
    """``{parent_circuit_name: [child_circuit_name, ...]}`` from the PYTHON
    hierarchy (each circuit's own ``child_instances``), not from disk --
    same traversal ``sheet_pin_sync.reconcile_sheet_pins()`` uses, and for
    the same reason: it is correct before any corresponding file exists."""
    children: Dict[str, List[str]] = {}

    def _walk(name: str, seen: Set[str]) -> None:
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
            children.setdefault(name, []).append(sub_name)
            _walk(sub_name, seen)

    _walk(root_name, set())
    return children


def _disk_hierarchy(
    project_dir: Path, root_file: Path
) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """Read the ALREADY-GENERATED hierarchy from disk.

    Returns ``(uuid_paths, parent_of)``:

    * ``uuid_paths[circuit_name]`` is the full hierarchical UUID path KiCad
      uses for instance bookkeeping -- ``[root_document_uuid,
      sheet_symbol_uuid, ...]``, matching exactly what
      ``main_generator.generate_project()`` builds during clean generation
      (root's own document UUID first, then one sheet-SYMBOL UUID per level,
      never a child's own document UUID).
    * ``parent_of[circuit_name]`` is the name of the sheet whose canvas
      holds that sheet's symbol.

    A circuit name here is a file stem: this generator names every file
    after its ``@circuit(name=...)``, one file per circuit.
    """
    uuid_paths: Dict[str, List[str]] = {}
    parent_of: Dict[str, str] = {}

    def _walk(name: str, path: List[str], seen: Set[str]) -> None:
        if name in seen:
            return
        seen = seen | {name}
        sch_path = project_dir / f"{name}.kicad_sch"
        if not sch_path.exists():
            return
        try:
            sch = ksa.Schematic.load(str(sch_path))
        except Exception:
            logger.warning(
                "new_sheet_sync: could not read existing sheet %s -- its own "
                "children will not be considered already-generated",
                sch_path,
                exc_info=True,
            )
            return
        for sheet in sch.sheets.data.get("sheets", []) or []:
            filename = sheet.get("filename") or ""
            if not filename.endswith(".kicad_sch"):
                continue
            child_name = filename[: -len(".kicad_sch")]
            child_path = path + [sheet.get("uuid")]
            uuid_paths[child_name] = child_path
            parent_of[child_name] = name
            _walk(child_name, child_path, seen)

    try:
        root_sch = ksa.Schematic.load(str(root_file))
    except Exception as exc:
        raise NewSheetSyncError(
            f"cannot read root schematic {root_file}: {exc}"
        ) from exc

    root_name = root_file.stem
    root_uuid = root_sch._data.get("uuid") or getattr(root_sch, "uuid", None)
    if not root_uuid:
        raise NewSheetSyncError(f"root schematic {root_file} has no document UUID")
    uuid_paths[root_name] = [root_uuid]
    _walk(root_name, [root_uuid], set())
    return uuid_paths, parent_of


def _free_sheet_origin(sch) -> Tuple[float, float]:
    """A clear spot on ``sch``'s canvas for a new sheet symbol: below
    everything already drawn there.

    Position (unlike size and pin layout) is NOT recomputed by
    ``fix_sheet_symbol_sizes()``, so it has to be non-colliding on the first
    try. Stacking below the existing content is the same conservative choice
    ``placement.py`` makes for new components, and keeps the new sheet
    visually grouped with its siblings rather than dropped on top of them.
    """
    max_y = 0.0
    for sheet in sch.sheets.data.get("sheets", []) or []:
        pos = sheet.get("position") or {}
        size = sheet.get("size") or {}
        max_y = max(max_y, float(pos.get("y", 0.0)) + float(size.get("height", 0.0)))
    for comp in sch.components:
        try:
            max_y = max(max_y, float(comp.position.y))
        except (AttributeError, TypeError):
            continue
    if max_y <= 0.0:
        max_y = 12.7 - SHEET_MARGIN  # empty canvas: start near the top-left
    return _snap(33.02), _snap(max_y + SHEET_MARGIN)


def _boundary_pin_names(subcircuits: Dict[str, Any], child_name: str) -> List[str]:
    """Nets that will cross ``child_name``'s own sheet boundary, computed
    from the Python circuit exactly as ``sheet_pin_sync`` does. Used only to
    SIZE the new sheet symbol and to predict where ``reconcile_sheet_pins()``
    will place its pins -- the pins themselves are that module's job."""
    from .. import boundary_nets

    owners = boundary_nets.net_owner_circuits(subcircuits)
    return sorted(
        n
        for n in boundary_nets.subtree_net_names(subcircuits, child_name)
        if boundary_nets.net_crosses_boundary(subcircuits, child_name, n, owners)
    )


def _next_page_number(project_dir: Path) -> int:
    """One past the highest page number any existing sheet symbol claims."""
    highest = 1
    for sch_file in project_dir.glob("*.kicad_sch"):
        try:
            sch = ksa.Schematic.load(str(sch_file))
        except Exception:
            continue
        for sheet in sch.sheets.data.get("sheets", []) or []:
            try:
                highest = max(highest, int(sheet.get("page_number") or 1))
            except (TypeError, ValueError):
                continue
    return highest + 1


def create_missing_sheets(
    generator,
    top_circuit,
    subcircuits: Dict[str, Any],
    existing_references: Optional[Set[str]] = None,
) -> List[str]:
    """Materialize every circuit that exists in Python but has no
    ``.kicad_sch`` on disk yet, so the rest of incremental sync can treat it
    as an ordinary existing sheet.

    Args:
        generator: the ``SchematicGenerator`` driving this sync. Passed
            whole, deliberately: creating a sheet is a clean-GENERATION
            operation, and the point of this fix is to reuse that exact
            machinery (``_collision_place_all_circuits``, ``paper_size``,
            ``_update_kicad_pro``, ``SchematicWriter``) rather than grow a
            second, subtly-different sheet writer that would drift.
        top_circuit: the root ``Circuit`` (its ``.name`` names the root file).
        subcircuits: ``{circuit_name: Circuit}`` from
            ``circuit_loader.load_circuit_hierarchy()``.
        existing_references: references already in use project-wide, so the
            shared reference manager never re-mints one that belongs to a
            component on an untouched sheet.

    Returns:
        Human-readable descriptions of what was created (empty when the
        Python hierarchy and the on-disk one already agree, which is the
        overwhelmingly common case -- this then costs one pass of file
        existence checks and nothing else).

    Raises:
        NewSheetSyncError: on a reparenting attempt, or when a new sheet's
            required parent cannot be resolved. Failing loudly here is the
            whole point (see module docstring).
    """
    project_dir = Path(generator.project_dir)
    root_name = top_circuit.name

    root_file = project_dir / f"{root_name}.kicad_sch"
    if not root_file.exists():
        # Root file is named after the project rather than the circuit --
        # tolerated the same way elsewhere in this codebase.
        root_file = project_dir / f"{generator.project_name}.kicad_sch"
    if not root_file.exists():
        logger.debug("new_sheet_sync: no root schematic on disk, nothing to create")
        return []

    missing = [
        name
        for name in subcircuits
        if name != root_name and not (project_dir / f"{name}.kicad_sch").exists()
    ]
    if not missing:
        logger.debug("new_sheet_sync: every Python sheet already exists on disk")
        return []

    logger.info(
        "new_sheet_sync: %d sheet(s) present in Python but not on disk: %s",
        len(missing),
        ", ".join(sorted(missing)),
    )

    python_children = _python_children(subcircuits, root_name)
    parent_of_python: Dict[str, str] = {}
    for parent, kids in python_children.items():
        for kid in kids:
            parent_of_python[kid] = parent

    uuid_paths, disk_parent_of = _disk_hierarchy(project_dir, root_file)
    # The root file may be named after the project rather than the circuit;
    # key its path under the circuit name too so lookups below work either way.
    uuid_paths.setdefault(root_name, uuid_paths[root_file.stem])

    missing_set = set(missing)

    # Refuse a reparent before creating anything, so a rejected sync leaves
    # the project exactly as it found it rather than half-migrated.
    for name in missing:
        for child in python_children.get(name, []):
            if child not in missing_set and child in disk_parent_of:
                raise NewSheetSyncError(
                    f"sheet '{child}' already exists on disk under parent "
                    f"'{disk_parent_of[child]}' but Python now places it under the "
                    f"new sheet '{name}'. Re-parenting an existing sheet is not "
                    f"supported by incremental sync (it would require rewriting "
                    f"every affected component's hierarchical instance path); "
                    f"regenerate with force_regenerate=True instead."
                )

    # Only the TOP of each missing subtree needs a sheet symbol written into
    # an existing parent file -- SchematicWriter mints the sheet symbols for
    # a new sheet's own children itself, exactly as it does during clean
    # generation. Sorted for deterministic output across runs.
    subtree_roots = sorted(
        name for name in missing if parent_of_python.get(name) not in missing_set
    )
    for name in subtree_roots:
        if parent_of_python.get(name) is None:
            raise NewSheetSyncError(
                f"new sheet '{name}' has no parent in the Python hierarchy -- "
                f"cannot decide which existing sheet should hold its symbol"
            )

    # Give the new circuits component/sheet-symbol positions using the same
    # collision placement clean generation uses. Scoped to the missing
    # circuits: it mutates in-memory Circuit positions, and untouched sheets
    # must keep whatever the user has on disk.
    generator._collision_place_all_circuits({name: subcircuits[name] for name in missing})

    from ..sch_gen.integrated_reference_manager import IntegratedReferenceManager

    ref_manager = IntegratedReferenceManager()
    for ref in existing_references or ():
        ref_manager.used_references.add(ref)

    changes: List[str] = []
    page_number = _next_page_number(project_dir)

    for name in subtree_roots:
        parent_name = parent_of_python[name]
        parent_path = uuid_paths.get(parent_name)
        if parent_path is None:
            raise NewSheetSyncError(
                f"new sheet '{name}' needs a symbol on parent sheet '{parent_name}', "
                f"but that parent is not part of the on-disk hierarchy "
                f"(known sheets: {sorted(uuid_paths)})"
            )
        parent_file = project_dir / f"{parent_name}.kicad_sch"
        if parent_name == root_name:
            parent_file = root_file
        if not parent_file.exists():
            raise NewSheetSyncError(
                f"parent schematic {parent_file} for new sheet '{name}' not found"
            )

        pin_names = _boundary_pin_names(subcircuits, name)
        height = max(MIN_SHEET_HEIGHT, MARGIN_TOP * 2 + PIN_PITCH * max(len(pin_names) - 1, 0))

        parent_sch = ksa.Schematic.load(str(parent_file))
        origin_x, origin_y = _free_sheet_origin(parent_sch)
        sheet_symbol_uuid = str(uuid_module.uuid4())
        # No pins here on purpose -- reconcile_sheet_pins() (wayfinder #65),
        # which runs later on this same generate, owns pin population AND the
        # matching tie labels on this canvas. See the module docstring.
        parent_sch.add_sheet(
            name=name,
            filename=f"{name}.kicad_sch",
            position=(origin_x, origin_y),
            size=(DEFAULT_SHEET_WIDTH, _snap(height)),
            project_name=generator.project_name,
            page_number=str(page_number),
            uuid=sheet_symbol_uuid,
        )
        page_number += 1
        parent_sch.save(str(parent_file), preserve_format=False)
        changes.append(
            f"{parent_file.name}: added sheet symbol '{name}' at "
            f"({origin_x:.2f}, {origin_y:.2f})"
        )
        logger.info(
            "new_sheet_sync: added sheet symbol '%s' to %s", name, parent_file.name
        )

        # Positions reconcile_sheet_pins() will use for this symbol's pins
        # once it runs, so a new INTERMEDIATE sheet's synthesized
        # hierarchical labels line up with them. Purely cosmetic (KiCad
        # matches a sheet pin to a child's hierarchical label by NAME, and
        # fix_sheet_symbol_sizes() relays both afterwards) but free to get
        # right.
        from kicad_sch_api.core.types import Point

        parent_pins = [
            (
                pin_name,
                Point(origin_x + DEFAULT_SHEET_WIDTH, origin_y + MARGIN_TOP + i * PIN_PITCH),
                "bidirectional",
            )
            for i, pin_name in enumerate(pin_names)
        ]

        changes.extend(
            _write_subtree(
                generator=generator,
                subcircuits=subcircuits,
                python_children=python_children,
                missing_set=missing_set,
                name=name,
                hierarchical_path=parent_path + [sheet_symbol_uuid],
                uuid_paths=uuid_paths,
                ref_manager=ref_manager,
                parent_pins=parent_pins,
                project_dir=project_dir,
            )
        )

    # Rebuild the project's sheet cache from what is now on disk. Called with
    # no explicit UUIDs so it re-reads every document UUID from its file --
    # correct for incremental sync, where the pre-existing sheets' UUIDs must
    # be preserved exactly as they are rather than reinvented.
    generator._update_kicad_pro(subcircuits, root_name)

    return changes


def _write_subtree(
    generator,
    subcircuits: Dict[str, Any],
    python_children: Dict[str, List[str]],
    missing_set: Set[str],
    name: str,
    hierarchical_path: List[str],
    uuid_paths: Dict[str, List[str]],
    ref_manager,
    parent_pins,
    project_dir: Path,
) -> List[str]:
    """Write ``name``'s ``.kicad_sch`` and, recursively, those of its own
    still-missing children -- mirroring clean generation's dependency-ordered
    writer loop (``main_generator.generate_project()``), which likewise
    writes a parent before its children so the parent's freshly-minted
    sheet-symbol UUIDs are available to build the children's hierarchical
    paths."""
    from ..sch_gen.schematic_writer import SchematicWriter, write_schematic_file

    circ = subcircuits[name]
    writer = SchematicWriter(
        circ,
        subcircuits,
        instance_naming_map=None,
        paper_size=generator.paper_size,
        project_name=generator.project_name,
        hierarchical_path=hierarchical_path,
        reference_manager=ref_manager,
        parent_pins=parent_pins,
    )
    sch_expr = writer.generate_s_expr()
    out_path = project_dir / f"{name}.kicad_sch"
    write_schematic_file(sch_expr, str(out_path))
    writer._fix_power_symbol_text_positions(str(out_path))
    uuid_paths[name] = hierarchical_path

    changes = [
        f"{out_path.name}: created new sheet with "
        f"{len(getattr(circ, 'components', []) or [])} component(s)"
    ]
    logger.info("new_sheet_sync: created %s", out_path.name)

    for child in python_children.get(name, []):
        if child not in missing_set:
            continue  # refused earlier if it exists on disk; nothing to do here
        child_symbol_uuid = writer.sheet_symbol_map.get(child)
        if child_symbol_uuid is None:
            raise NewSheetSyncError(
                f"SchematicWriter did not emit a sheet symbol for '{child}' on "
                f"new sheet '{name}' -- cannot build its hierarchical path"
            )
        changes.extend(
            _write_subtree(
                generator=generator,
                subcircuits=subcircuits,
                python_children=python_children,
                missing_set=missing_set,
                name=child,
                hierarchical_path=hierarchical_path + [child_symbol_uuid],
                uuid_paths=uuid_paths,
                ref_manager=ref_manager,
                parent_pins=writer.child_sheet_pins.get(child, []),
                project_dir=project_dir,
            )
        )
    return changes
