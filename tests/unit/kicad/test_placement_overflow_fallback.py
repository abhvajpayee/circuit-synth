"""Regression tests for `PlacementEngine`'s out-of-room fallback.

`_find_next_available_position()` runs a bounded row-major search against
`self.sheet_size`. When that search runs out of room its fallback used to be
a literal constant::

    logger.warning("Could not find available position with dynamic spacing, using origin")
    return (self.margin, self.margin)

Two independent things are wrong with that:

1. It is never collision-checked. `(margin, margin)` is simply asserted to be
   free, and on an already-populated sheet it usually is not.
2. It is **constant**. N consecutive overflows return the *same* point, so N
   components land exactly on top of each other.

(2) is the damaging one, and the damage is electrical rather than cosmetic:
coincident components have coincident pins, and circuit-synth's pin-label
writer emits one label per position -- so a stack of N components yields
one label instead of N, silently dropping N-1 pin connections. That is what
produced the `isolated_pin_label` ERC findings in windTunnelProject wayfinder
#64 (8 of 15 newly-synced components stacked at `(25.4, 25.4)` on the
Acquisition MCU board's MCU sheet, 11 pin labels lost).

The fix replaces the constant with `_find_overflow_position()`: a
deterministic, collision-checked grid in an *unbounded* region below all
existing content. Crucially it does not touch the bounded search's success
path -- a placement that already succeeds is unaffected -- because that path
is shared with `project_generator.py` and `component_manager.py`.
"""

import pytest
from kicad_sch_api.core.types import Point, SchematicSymbol

from circuit_synth.kicad.schematic.placement import (
    ComponentBounds,
    PlacementEngine,
    PlacementStrategy,
)


class _FakeSheetManager:
    """Mimics `kicad_sch_api`'s `SheetManager`: no `__iter__`/`__len__`, sheet
    dicts reachable only through the inherited `BaseManager.data` property."""

    def __init__(self, sheets=None):
        self.data = {"sheets": sheets or []}


class _FakeSchematic:
    def __init__(self, components=None, sheets=None):
        self.components = components if components is not None else []
        self.sheets = _FakeSheetManager(sheets)


def _symbol(reference, x, y, lib_id="FakeLib:FakePart"):
    """A component the symbol library cannot resolve, so
    `_estimate_component_size()` takes its documented fallback branch and the
    test stays independent of whichever KiCad symbol libraries are installed."""
    return SchematicSymbol(
        uuid=f"uuid-{reference}",
        lib_id=lib_id,
        position=Point(x, y),
        reference=reference,
        value="1k",
    )


def _bounds_of(engine, component):
    size = engine._estimate_component_size(component)
    return ComponentBounds(
        component.position.x - size[0] / 2,
        component.position.y - size[1] / 2,
        size[0],
        size[1],
    )


def _full_sheet_engine():
    """A schematic whose nominal search window has no room left -- the real
    precondition on the Acquisition MCU sheet, where 71 existing components
    already covered the placer's window.

    A deliberately small `sheet_size` keeps the reproduction fast: what matters
    is that the *bounded* search exhausts, not how many square millimetres it
    had to give up on. The far-away component mirrors how circuit-synth's own
    fresh-generate path routinely writes content well past the declared paper
    size (measured x up to 823mm on a nominally-A3 sheet), which is exactly why
    the nominal bound is a fiction in the first place.
    """
    schematic = _FakeSchematic()
    engine = PlacementEngine(schematic, sheet_size=(80.0, 80.0))
    n = 0
    y = engine.margin
    while y <= engine.sheet_size[1]:
        x = engine.margin
        while x <= engine.sheet_size[0]:
            schematic.components.append(_symbol(f"RX{n}", x, y))
            n += 1
            x += 6.0
        y += 4.0
    schematic.components.append(_symbol("RFAR", 300.0, 200.0))
    return engine, schematic


# ---------------------------------------------------------------------------
# The reported symptom
# ---------------------------------------------------------------------------


def test_overflowing_components_do_not_stack_on_one_point():
    """Eight components placed onto a full sheet must get eight distinct,
    mutually non-overlapping positions -- not eight copies of `(margin, margin)`."""
    engine, schematic = _full_sheet_engine()

    placed = []
    for i in range(8):
        comp = _symbol(f"RNEW{i}", 0.0, 0.0)
        x, y = engine.find_position(PlacementStrategy.AUTO, component=comp)
        comp.position = Point(x, y)
        schematic.components.append(comp)  # the real caller adds it too
        placed.append(comp)

    positions = [(c.position.x, c.position.y) for c in placed]
    assert len(set(positions)) == len(positions), (
        f"overflowing components collapsed onto shared position(s): {positions}"
    )
    assert (engine.margin, engine.margin) not in positions, (
        "overflow fallback still returns the un-collision-checked origin"
    )


def test_overflowing_components_do_not_overlap_each_other():
    """Distinct positions are not enough -- the bounding boxes must be
    disjoint, since it is overlapping *pins* (not overlapping centres) that
    collapse two pin labels into one."""
    engine, schematic = _full_sheet_engine()

    placed = []
    for i in range(8):
        comp = _symbol(f"RNEW{i}", 0.0, 0.0)
        x, y = engine.find_position(PlacementStrategy.AUTO, component=comp)
        comp.position = Point(x, y)
        schematic.components.append(comp)
        placed.append(comp)

    boxes = [_bounds_of(engine, c) for c in placed]
    for i, a in enumerate(boxes):
        for j, b in enumerate(boxes[i + 1 :], start=i + 1):
            assert not a.overlaps(b), (
                f"{placed[i].reference} and {placed[j].reference} overlap: "
                f"({placed[i].position.x}, {placed[i].position.y}) vs "
                f"({placed[j].position.x}, {placed[j].position.y})"
            )


def test_overflowing_component_does_not_land_on_existing_content():
    """The overflow position must also be clear of everything already placed --
    the old constant fallback was never collision-checked at all."""
    engine, schematic = _full_sheet_engine()
    existing = [_bounds_of(engine, c) for c in schematic.components]

    comp = _symbol("RNEW", 0.0, 0.0)
    x, y = engine.find_position(PlacementStrategy.AUTO, component=comp)
    comp.position = Point(x, y)
    new_box = _bounds_of(engine, comp)

    for box in existing:
        assert not new_box.overlaps(box), (
            f"overflow position ({x}, {y}) collides with existing content at "
            f"x={box.x}..{box.right}, y={box.y}..{box.bottom}"
        )


def test_size_only_overflow_path_also_avoids_the_constant_origin():
    """`_find_next_available_position_with_size()` (used when no component
    object is available) carried an identical constant fallback."""
    engine, schematic = _full_sheet_engine()

    seen = set()
    for _ in range(4):
        x, y = engine._find_next_available_position_with_size((10.0, 10.0))
        assert (x, y) not in seen, f"repeated overflow position {(x, y)}"
        seen.add((x, y))
        schematic.components.append(_symbol(f"RS{len(seen)}", x, y))


# ---------------------------------------------------------------------------
# Blast-radius guards: the success path must be untouched
# ---------------------------------------------------------------------------


def test_successful_bounded_search_is_unchanged_on_empty_sheet():
    """A placement that fits must still return exactly what it returned before
    the fix. `_find_next_available_position()`'s bounded search is shared with
    `project_generator.py` and `component_manager.py`; only the exhausted-search
    fallback was allowed to change."""
    schematic = _FakeSchematic()
    engine = PlacementEngine(schematic)

    comp = _symbol("R1", 0.0, 0.0)
    x, y = engine.find_position(PlacementStrategy.AUTO, component=comp)

    # Value produced by the pre-fix implementation for this exact input.
    assert (x, y) == (45.72, 50.8)


def test_successful_bounded_search_still_respects_sheet_bounds():
    """Non-overflow placements stay inside the nominal page, as before."""
    schematic = _FakeSchematic()
    engine = PlacementEngine(schematic)

    for i in range(5):
        comp = _symbol(f"R{i}", 0.0, 0.0)
        x, y = engine.find_position(PlacementStrategy.AUTO, component=comp)
        comp.position = Point(x, y)
        schematic.components.append(comp)
        size = engine._estimate_component_size(comp)
        assert engine._check_within_bounds(x, y, size[0], size[1]), (
            f"{comp.reference} at ({x}, {y}) escaped the nominal sheet even "
            "though the sheet still had room"
        )


def test_overflow_region_starts_below_all_existing_content():
    """Documents the chosen strategy: overflow goes *outside* the existing
    content's bounding box rather than into its interior gaps.

    The collision model knows component bounding boxes only -- it has no model
    of wires, labels, junctions or no-connect markers (the real MCU sheet has
    108 labels and 57 no-connect markers inside the placer's own search window).
    Placing new parts beyond the existing extent is therefore strictly safer
    than searching interior gaps for them."""
    engine, schematic = _full_sheet_engine()
    content_bottom = max(_bounds_of(engine, c).bottom for c in schematic.components)

    comp = _symbol("RNEW", 0.0, 0.0)
    x, y = engine.find_position(PlacementStrategy.AUTO, component=comp)
    size = engine._estimate_component_size(comp)

    assert y - size[1] / 2 >= content_bottom, (
        f"overflow position y={y} is not below existing content bottom "
        f"{content_bottom}"
    )
