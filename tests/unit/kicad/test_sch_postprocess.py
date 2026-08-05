"""Tests for circuit_synth.kicad.sch_postprocess.

Both functions operate on .kicad_sch text files; no circuit generator is
needed — fixtures are built as minimal inline S-expression strings and
written to tmp_path.

Constants used by the module (and expected in assertions):
  PIN_PITCH    = 2.54 mm
  MARGIN_TOP   = 2.54 mm   (from sheet top to first pin)
  MARGIN_BOT   = 2.54 mm   (from last pin to sheet bottom)
  LABEL_OFFSET = 0.7116 mm (Sheetfile label below sheet bottom)

Sheet origin for all fixtures: sx=10.0, sy=10.0, width=20.0.
Initial height is 100.0 (unrealistically tall — the fix must shrink it).
Old right edge: 30.0.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from circuit_synth.kicad.sch_postprocess import fix_sheet_symbol_sizes, fix_subsheet_labels

# ---------------------------------------------------------------------------
# Shared geometry
# ---------------------------------------------------------------------------

_SX, _SY, _W = 10.0, 10.0, 20.0
_OLD_H = 100.0
_OLD_RIGHT_X = _SX + _W          # 30.0


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _pin_block(name: str, x: float, y: float, angle: int = 0) -> str:
    justify = "right" if angle == 0 else "left"
    return (
        f'\t\t(pin "{name}" input\n'
        f'\t\t\t(at {x:.4f} {y:.4f} {angle})\n'
        '\t\t\t(effects\n'
        '\t\t\t\t(font (size 1.27 1.27))\n'
        f'\t\t\t\t(justify {justify})\n'
        '\t\t\t)\n'
        '\t\t)\n'
    )


def _pin_block_decimal_angle(name: str, x: float, y: float, angle: float = 0.0) -> str:
    """Same as _pin_block, but the angle is serialized with 4 decimal places
    (e.g. "0.0000"/"180.0000") -- the shape kicad_sch_api's own formatter
    produces whenever it resaves a file (e.g. during an unrelated incremental
    sync elsewhere in the project), as opposed to the plain-integer angle
    ("0"/"180") this module's own rewrite path emits. Reproduces the exact
    real-world precondition found on the acquisition board 2026-07-08."""
    justify = "right" if angle == 0 else "left"
    return (
        f'\t\t(pin "{name}" input\n'
        f'\t\t\t(at {x:.4f} {y:.4f} {angle:.4f})\n'
        '\t\t\t(effects\n'
        '\t\t\t\t(font (size 1.27 1.27))\n'
        f'\t\t\t\t(justify {justify})\n'
        '\t\t\t)\n'
        '\t\t)\n'
    )


def _sheet(pins: list[str], decimal_angle: bool = False) -> str:
    """Minimal sheet block with all pins placed on the right side."""
    sheetfile_y = _SY + _OLD_H + 0.7116
    body = (
        '\t(sheet\n'
        f'\t\t(at {_SX:.4f} {_SY:.4f})\n'
        f'\t\t(size {_W:.4f} {_OLD_H:.4f})\n'
        '\t\t(property "Sheetfile" "sub.kicad_sch"\n'
        f'\t\t\t(at {_SX:.4f} {sheetfile_y:.4f} 0)\n'
        '\t\t)\n'
    )
    builder = _pin_block_decimal_angle if decimal_angle else _pin_block
    for k, name in enumerate(pins):
        py = _SY + 2.54 + k * 2.54
        body += builder(name, _OLD_RIGHT_X, py, angle=0.0 if decimal_angle else 0)
    body += '\t)\n'
    return body


def _plain_label(name: str, x: float, y: float, angle: float = 0.0) -> str:
    """A root-sheet plain tie label -- the mechanism a TRUE root (no parent of
    its own) uses to tie sibling sheet-symbol pins together, per
    bus_emit.py's _parent_surgery. Structurally like _hl but `label` instead
    of `hierarchical_label`, and no `shape` line."""
    justify = "right bottom" if abs(angle - 180.0) < 1 else "left bottom"
    return (
        f'\t(label "{name}"\n'
        f'\t\t(at {x:.4f} {y:.4f} {angle:.4f})\n'
        '\t\t(effects\n'
        '\t\t\t(font (size 1.27 1.27))\n'
        f'\t\t\t(justify {justify})\n'
        '\t\t)\n'
        '\t\t(uuid "11111111-1111-1111-1111-111111111111")\n'
        '\t)\n'
    )


def _hl(name: str, x: float, y: float, angle: float = 0.0) -> str:
    justify = "right bottom" if abs(angle - 180.0) < 1 else "left bottom"
    return (
        f'\t(hierarchical_label "{name}"\n'
        f'\t\t(shape input)\n'
        f'\t\t(at {x:.4f} {y:.4f} {angle:.4f})\n'
        '\t\t(effects\n'
        '\t\t\t(font (size 1.27 1.27))\n'
        f'\t\t\t(justify {justify})\n'
        '\t\t)\n'
        '\t)\n'
    )


def _top(pins: list[str], decimal_angle: bool = False) -> str:
    """Top-level schematic: one sheet + one HL per pin (all initially on right)."""
    hls = ''.join(
        _hl(name, _OLD_RIGHT_X, _SY + 2.54 + k * 2.54, angle=0.0)
        for k, name in enumerate(pins)
    )
    return (
        '(kicad_sch (version 20211123) (generator circuit_synth)\n'
        + _sheet(pins, decimal_angle=decimal_angle)
        + hls
        + ')\n'
    )


# ---------------------------------------------------------------------------
# fix_sheet_symbol_sizes — sheet geometry
# ---------------------------------------------------------------------------

def test_height_reduced_two_pins(tmp_path: Path) -> None:
    # 2 pins: n_left=1, new_h = MARGIN_TOP + 0*PITCH + MARGIN_BOT = 5.08
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(_top(["CLK", "DATA"]))
    fix_sheet_symbol_sizes(str(sch))
    text = sch.read_text()
    assert "(size 20.0000 5.0800)" in text
    assert "(size 20.0000 100.0000)" not in text


def test_height_reduced_four_pins(tmp_path: Path) -> None:
    # 4 pins: n_left=2, new_h = 2.54 + 1*2.54 + 2.54 = 7.62
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(_top(["A", "B", "C", "D"]))
    fix_sheet_symbol_sizes(str(sch))
    assert "(size 20.0000 7.6200)" in sch.read_text()


def test_sheetfile_y_updated(tmp_path: Path) -> None:
    # Sheetfile label y = sy + new_h + LABEL_OFFSET = 10 + 5.08 + 0.7116 = 15.7916
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(_top(["CLK", "DATA"]))
    fix_sheet_symbol_sizes(str(sch))
    text = sch.read_text()
    assert "(at 10.0000 15.7916 0)" in text
    assert "110.7116" not in text           # old Sheetfile Y must be gone


def test_no_pins_sheet_unchanged(tmp_path: Path) -> None:
    """Sheet block with no pins passes through without modification."""
    content = (
        '(kicad_sch (version 20211123) (generator circuit_synth)\n'
        '\t(sheet\n'
        '\t\t(at 10.0000 10.0000)\n'
        '\t\t(size 20.0000 100.0000)\n'
        '\t\t(property "Sheetfile" "empty.kicad_sch"\n'
        '\t\t\t(at 10.0000 110.7116 0)\n'
        '\t\t)\n'
        '\t)\n'
        ')\n'
    )
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(content)
    fix_sheet_symbol_sizes(str(sch))
    assert "(size 20.0000 100.0000)" in sch.read_text()


# ---------------------------------------------------------------------------
# fix_sheet_symbol_sizes — pin placement
# ---------------------------------------------------------------------------

def test_single_pin_goes_left(tmp_path: Path) -> None:
    """1 pin: ceil(1/2)=1 → all on left at angle=180."""
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(_top(["RESET"]))
    fix_sheet_symbol_sizes(str(sch))
    # Pin block angle is an integer (no decimal point) — distinguish from HL angle
    assert "(at 10.0000 12.5400 180)" in sch.read_text()


def test_even_split_left_pins(tmp_path: Path) -> None:
    """4 pins → 2 on left (angle=180)."""
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(_top(["A", "B", "C", "D"]))
    fix_sheet_symbol_sizes(str(sch))
    text = sch.read_text()
    assert "(at 10.0000 12.5400 180)" in text
    assert "(at 10.0000 15.0800 180)" in text


def test_even_split_right_pins(tmp_path: Path) -> None:
    """4 pins → 2 on right (angle=0)."""
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(_top(["A", "B", "C", "D"]))
    fix_sheet_symbol_sizes(str(sch))
    text = sch.read_text()
    assert "(at 30.0000 12.5400 0)" in text
    assert "(at 30.0000 15.0800 0)" in text


def test_odd_split_more_left(tmp_path: Path) -> None:
    """3 pins → ceil(3/2)=2 left, 1 right."""
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(_top(["X", "Y", "Z"]))
    fix_sheet_symbol_sizes(str(sch))
    text = sch.read_text()
    # Pin-block angles are integers; HL label angles are floats (e.g. 180.0000).
    # Regex matches only integer-angle occurrences (pin blocks).
    left_pins  = re.findall(r'\(at 10\.0000 [\d.]+ 180\)', text)
    right_pins = re.findall(r'\(at 30\.0000 [\d.]+ 0\)',   text)
    assert len(left_pins) == 2
    assert len(right_pins) == 1


def test_left_pin_justify_left(tmp_path: Path) -> None:
    """Left-side pins carry (justify left) inside their effects block."""
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(_top(["CLK", "DATA"]))
    fix_sheet_symbol_sizes(str(sch))
    assert "\t\t\t\t(justify left)" in sch.read_text()


def test_right_pin_justify_right(tmp_path: Path) -> None:
    """Right-side pins carry (justify right) inside their effects block."""
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(_top(["CLK", "DATA"]))
    fix_sheet_symbol_sizes(str(sch))
    assert "\t\t\t\t(justify right)" in sch.read_text()


# ---------------------------------------------------------------------------
# fix_sheet_symbol_sizes — decimal-formatted incoming pin angles
#
# Found on the acquisition board 2026-07-08: kicad_sch_api's own formatter
# always serializes angle with 4 decimal places ("0.0000"/"180.0000") when it
# resaves a file (e.g. an unrelated incremental-sync edit elsewhere in the
# project causes a full round-trip through kicad_sch_api's object model).
# The position-rewrite regex here required a bare integer angle (`\d+`,
# matching the plain "0"/"180" this module itself writes) and silently failed
# to match decimal angles -- so once a pin's incoming angle was already
# decimal, its left_idx/right_idx slot counter never advanced and its
# coordinates were never rewritten, permanently freezing it at a stale
# position. Repeated regenerations (pin count shifting as, e.g., bus
# collapsing removed members) then caused two *different* pins to land on the
# exact same coordinate -- a real short/connectivity-collision on the sheet
# symbol, not just a cosmetic issue.
# ---------------------------------------------------------------------------

def test_decimal_angle_pins_are_still_repositioned(tmp_path: Path) -> None:
    """A pin arriving with an already-decimal-formatted angle ("0.0000") must
    still be repositioned onto the new grid -- not silently skipped."""
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(_top(["CLK", "DATA"], decimal_angle=True))   # CLK -> left
    fix_sheet_symbol_sizes(str(sch))
    text = sch.read_text()
    assert "(at 10.0000 12.5400 180)" in text, (
        f"decimal-angle pin CLK was not repositioned to the left slot:\n{text}"
    )
    assert "(at 30.0000 12.5400 0)" in text, (
        f"decimal-angle pin DATA was not repositioned to the right slot:\n{text}"
    )


def test_decimal_angle_pins_no_position_collision(tmp_path: Path) -> None:
    """With N pins all arriving in decimal-angle format (the real-world state
    after any kicad_sch_api resave), every pin must land on a distinct
    coordinate -- no two sheet pins may collide."""
    names = [f"SIG{i}" for i in range(10)]
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(_top(names, decimal_angle=True))
    fix_sheet_symbol_sizes(str(sch))
    text = sch.read_text()
    coords = re.findall(r'\(pin "[^"]+" input\n\t\t\t\(at ([\d.\-]+) ([\d.\-]+) [\d.\-]+\)', text)
    assert len(coords) == len(names), f"expected {len(names)} pin coordinates, found {coords}"
    assert len(set(coords)) == len(coords), (
        f"pin position collision: {[c for c in coords if coords.count(c) > 1]}"
    )


def test_preexisting_collision_from_stale_decimal_positions_is_repaired(tmp_path: Path) -> None:
    """Directly reproduces the acquisition-board MCU-sheet precondition: two
    pins (e.g. GND and a bus-vector pin like SDMMC_[0..10]) already sitting at
    the identical (x, y) -- stale positions frozen by an earlier run of this
    same pass under a *different* pin count/n_left boundary (e.g. before bus
    member collapsing shrank the list), each still carrying the decimal angle
    kicad_sch_api's resave leaves behind. A correct run must resolve the
    collision by repositioning every pin fresh, regardless of its incoming
    angle format -- not leave two pins short-circuited onto the same point."""
    sheetfile_y = _SY + _OLD_H + 0.7116
    body = (
        '\t(sheet\n'
        f'\t\t(at {_SX:.4f} {_SY:.4f})\n'
        f'\t\t(size {_W:.4f} {_OLD_H:.4f})\n'
        '\t\t(property "Sheetfile" "sub.kicad_sch"\n'
        f'\t\t\t(at {_SX:.4f} {sheetfile_y:.4f} 0)\n'
        '\t\t)\n'
    )
    names = ["ALPHA", "BETA", "GND", "SDMMC_BUS", "ZETA", "OMEGA"]
    collided_y = _SY + 2.54 + 2 * 2.54  # both GND and SDMMC_BUS start here
    for k, name in enumerate(names):
        if name in ("GND", "SDMMC_BUS"):
            body += _pin_block_decimal_angle(name, _OLD_RIGHT_X, collided_y, angle=0.0)
        else:
            py = _SY + 2.54 + k * 2.54
            body += _pin_block_decimal_angle(name, _OLD_RIGHT_X, py, angle=0.0)
    body += '\t)\n'
    content = '(kicad_sch (version 20211123) (generator circuit_synth)\n' + body + ')\n'
    sch = tmp_path / "top.kicad_sch"
    # Sanity: the fixture really does start with a collision.
    precheck = re.findall(r'\(at [\d.]+ ([\d.]+) [\d.]+\)', content)
    assert precheck.count(f"{collided_y:.4f}") == 2, "fixture setup failed to seed a collision"

    sch.write_text(content)
    fix_sheet_symbol_sizes(str(sch))
    text = sch.read_text()
    coords = re.findall(r'\(pin "[^"]+" input\n\t\t\t\(at ([\d.\-]+) ([\d.\-]+) [\d.\-]+\)', text)
    assert len(coords) == len(names)
    assert len(set(coords)) == len(coords), (
        f"GND/SDMMC_BUS collision was not repaired -- pins still overlap: {coords}"
    )


def test_mixed_integer_and_decimal_angle_pins_no_collision(tmp_path: Path) -> None:
    """Some pins already correctly repositioned (plain-integer angle, from an
    earlier successful run within the same pass) alongside others still
    carrying a decimal angle -- both groups must end up on distinct
    coordinates after this pass, whichever side (left/right) they land on."""
    sheetfile_y = _SY + _OLD_H + 0.7116
    body = (
        '\t(sheet\n'
        f'\t\t(at {_SX:.4f} {_SY:.4f})\n'
        f'\t\t(size {_W:.4f} {_OLD_H:.4f})\n'
        '\t\t(property "Sheetfile" "sub.kicad_sch"\n'
        f'\t\t\t(at {_SX:.4f} {sheetfile_y:.4f} 0)\n'
        '\t\t)\n'
    )
    names = ["A", "B", "C", "D", "E", "F"]
    for k, name in enumerate(names):
        py = _SY + 2.54 + k * 2.54
        if k % 2 == 0:
            body += _pin_block(name, _OLD_RIGHT_X, py, angle=0)
        else:
            body += _pin_block_decimal_angle(name, _OLD_RIGHT_X, py, angle=0.0)
    body += '\t)\n'
    content = '(kicad_sch (version 20211123) (generator circuit_synth)\n' + body + ')\n'
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(content)
    fix_sheet_symbol_sizes(str(sch))
    text = sch.read_text()
    coords = re.findall(r'\(pin "[^"]+" input\n\t\t\t\(at ([\d.\-]+) ([\d.\-]+) [\d.\-]+\)', text)
    assert len(coords) == len(names)
    assert len(set(coords)) == len(coords), (
        f"pin position collision between integer- and decimal-angle pins: {coords}"
    )


# ---------------------------------------------------------------------------
# fix_sheet_symbol_sizes — hierarchical_label repositioning (type preserved)
# ---------------------------------------------------------------------------
#
# A hierarchical_label's TYPE is never converted here (removed 2026-07-29):
# a non-root sheet's hierarchical_label legitimately needs to reach that
# sheet's own real parent one level up, so converting it to a plain label
# would silently break that. Only the true root can never have a valid
# hierarchical_label at all, and that is the generator's own responsibility
# to never emit in the first place (schematic_writer.py's
# _net_crosses_boundary) -- not something this pass patches after the fact.
# These tests only check repositioning/justify tracking, which applies
# identically regardless of root-ness.

def test_hl_stays_hierarchical(tmp_path: Path) -> None:
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(_top(["CLK", "DATA"]))
    fix_sheet_symbol_sizes(str(sch))
    text = sch.read_text()
    assert '(hierarchical_label "CLK"' in text
    assert '(hierarchical_label "DATA"' in text


def test_hl_shape_line_preserved(tmp_path: Path) -> None:
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(_top(["CLK"]))
    fix_sheet_symbol_sizes(str(sch))
    assert "(shape" in sch.read_text()


def test_hl_position_moved_to_left_pin(tmp_path: Path) -> None:
    """HL for the first pin (goes left) is repositioned to left edge."""
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(_top(["CLK", "DATA"]))   # CLK → left
    fix_sheet_symbol_sizes(str(sch))
    # HL label angle is float: angle=180.0000
    assert "(at 10.0000 12.5400 180.0000)" in sch.read_text()


def test_hl_justify_right_bottom_for_left_pin(tmp_path: Path) -> None:
    """HL at angle≈180 (left edge) gets justify 'right bottom'."""
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(_top(["CLK", "DATA"]))   # CLK → left
    fix_sheet_symbol_sizes(str(sch))
    assert "(justify right bottom)" in sch.read_text()


def test_hl_justify_left_bottom_for_right_pin(tmp_path: Path) -> None:
    """HL at angle≈0 (right edge) gets justify 'left bottom'."""
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(_top(["CLK", "DATA"]))   # DATA → right
    fix_sheet_symbol_sizes(str(sch))
    assert "(justify left bottom)" in sch.read_text()


def test_unmatched_hl_untouched(tmp_path: Path) -> None:
    """HL with no matching sheet pin is left as-is: still hierarchical_label,
    at its original position (no crash)."""
    content = (
        '(kicad_sch (version 20211123) (generator circuit_synth)\n'
        + _hl("ORPHAN", 50.0, 50.0, 0.0)
        + ')\n'
    )
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(content)
    fix_sheet_symbol_sizes(str(sch))
    text = sch.read_text()
    assert '(hierarchical_label "ORPHAN"' in text
    assert "(at 50.0000 50.0000 0.0000)" in text


# ---------------------------------------------------------------------------
# fix_sheet_symbol_sizes — root-sheet plain tie labels must track pin moves
#
# Found on the acquisition board 2026-07-08, as a second-order effect of
# fixing the bus-connectivity bug: a true root sheet ties sibling sheet-symbol
# pins together with plain `label`s (not hierarchical_label -- root has no
# parent to forward to). When this pass moves a sheet pin to its freshly
# recomputed grid slot, an un-tracked plain tie label is stranded at the pin's
# OLD coordinate, where it can collide with whatever *different* pin the new
# grid assigns to that spot.
# ---------------------------------------------------------------------------

def test_plain_tie_label_follows_repositioned_pin(tmp_path: Path) -> None:
    """A root-level plain `label` coincident with a sheet pin's OLD position
    must move to the pin's NEW position, the same way a hierarchical_label
    does (test_hl_position_moved_to_left_pin)."""
    content = (
        '(kicad_sch (version 20211123) (generator circuit_synth)\n'
        + _sheet(["CLK", "DATA"])                                    # CLK -> left
        + _plain_label("CLK", _OLD_RIGHT_X, _SY + 2.54, angle=0.0)    # old CLK position
        + ')\n'
    )
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(content)
    fix_sheet_symbol_sizes(str(sch))
    text = sch.read_text()
    assert '(label "CLK"\n\t\t(at 10.0000 12.5400 180.0000)' in text, (
        f"plain tie label CLK was not repositioned to follow its pin:\n{text}"
    )


def test_plain_tie_label_no_collision_with_repositioned_pin(tmp_path: Path) -> None:
    """Directly reproduces the acquisition-board MCU-sheet precondition: a
    root-level plain tie label (e.g. a bus-vector name like SDMMC_[0..10])
    sitting at a bus pin's OLD position, and a DIFFERENT scalar pin (e.g. GND)
    that the freshly recomputed grid assigns to that exact spot. The label
    must move with its own pin, not collide with the unrelated one."""
    names = ["ALPHA", "BUS_PIN", "GND", "ZETA"]
    content = (
        '(kicad_sch (version 20211123) (generator circuit_synth)\n'
        + _sheet(names)
        # BUS_PIN's tie label at its own (pre-reposition) coordinate --
        # index 1 -> old y = _SY + 2.54 + 1*2.54
        + _plain_label("BUS_PIN", _OLD_RIGHT_X, _SY + 2.54 + 2.54, angle=0.0)
        + ')\n'
    )
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(content)
    fix_sheet_symbol_sizes(str(sch))
    text = sch.read_text()
    pin_coords = re.findall(r'\(pin "([^"]+)" input\n\t\t\t\(at ([\d.\-]+) ([\d.\-]+) [\d.\-]+\)', text)
    label_m = re.search(r'\(label "BUS_PIN"\n\t\t\(at ([\d.\-]+) ([\d.\-]+)', text)
    assert label_m, f"BUS_PIN tie label missing after reposition:\n{text}"
    label_xy = (label_m.group(1), label_m.group(2))
    bus_pin_xy = next((x, y) for name, x, y in pin_coords if name == "BUS_PIN")
    assert label_xy == bus_pin_xy, (
        f"BUS_PIN's tie label {label_xy} did not follow its pin {bus_pin_xy}"
    )
    other_pin_coords = {(x, y) for name, x, y in pin_coords if name != "BUS_PIN"}
    assert label_xy not in other_pin_coords, (
        f"BUS_PIN's tie label collided with another pin at {label_xy}"
    )


def test_plain_tie_label_previously_on_left_edge_still_tracked(tmp_path: Path) -> None:
    """Directly reproduces the acquisition-board ETH_TX_EN precondition (found
    2026-07-08): a tie label frozen at the sheet's LEFT edge (sx) from some
    earlier regeneration -- when the pin/member split shifted (e.g. the
    alphabetical left/right boundary moved as other pins were added/removed),
    the SAME pin is now classified on the RIGHT side this run. The old
    label_updates lookup only ever registered a stale label's position as the
    sheet's OLD RIGHT edge (old_right_x) -- a label already sitting at the
    LEFT edge could never be found, leaving it permanently stranded and the
    net effectively disconnected on the root sheet (no coincident tie at the
    pin's real, current position)."""
    names = ["ALPHA", "BETA", "GAMMA", "DELTA"]   # n=4, n_left=2: ALPHA/BETA left, GAMMA/DELTA right
    content = (
        '(kicad_sch (version 20211123) (generator circuit_synth)\n'
        + _sheet(names)
        # DELTA's tie label frozen at the sheet's LEFT edge (angle=180) --
        # as if DELTA used to be a left-side pin in an earlier regeneration.
        + _plain_label("DELTA", _SX, _SY + 2.54, angle=180.0)
        + ')\n'
    )
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(content)
    fix_sheet_symbol_sizes(str(sch))
    text = sch.read_text()
    pin_coords = re.findall(r'\(pin "([^"]+)" input\n\t\t\t\(at ([\d.\-]+) ([\d.\-]+) [\d.\-]+\)', text)
    delta_pin_xy = next((x, y) for name, x, y in pin_coords if name == "DELTA")
    assert delta_pin_xy == ("30.0000", "15.0800"), f"unexpected DELTA pin position: {delta_pin_xy}"

    label_m = re.search(r'\(label "DELTA"\n\t\t\(at ([\d.\-]+) ([\d.\-]+)', text)
    assert label_m, f"DELTA tie label missing after reposition:\n{text}"
    label_xy = (label_m.group(1), label_m.group(2))
    assert label_xy == delta_pin_xy, (
        f"DELTA's tie label {label_xy} is still stranded at the old LEFT edge, "
        f"not tracking its pin's current position {delta_pin_xy} -- the net is "
        f"effectively disconnected on the root sheet"
    )


# ---------------------------------------------------------------------------
# fix_subsheet_labels — helpers
# ---------------------------------------------------------------------------

def _sub_hl(name: str, angle: float = 0.0) -> str:
    justify = "right bottom" if abs(angle - 180.0) < 1 else "left bottom"
    return (
        f'\t(hierarchical_label "{name}"\n'
        f'\t\t(shape input)\n'
        f'\t\t(at 10.0000 10.0000 {angle:.4f})\n'
        '\t\t(effects\n'
        '\t\t\t(font (size 1.27 1.27))\n'
        f'\t\t\t(justify {justify})\n'
        '\t\t)\n'
        '\t)\n'
    )


def _sub_sch(*names: str, angle: float = 0.0) -> str:
    body = '(kicad_sch (version 20211123) (generator circuit_synth)\n'
    for name in names:
        body += _sub_hl(name, angle)
    body += ')\n'
    return body


def _parent_top(fname: str, pins: list[str]) -> str:
    pin_lines = ''.join(
        f'\t\t(pin "{p}" input\n'
        f'\t\t\t(at 30.0000 {10.0 + 2.54 + k * 2.54:.4f} 0)\n'
        '\t\t)\n'
        for k, p in enumerate(pins)
    )
    return (
        '(kicad_sch (version 20211123) (generator circuit_synth)\n'
        '\t(sheet\n'
        '\t\t(at 10.0000 10.0000)\n'
        '\t\t(size 20.0000 5.0800)\n'
        f'\t\t(property "Sheetfile" "{fname}"\n'
        '\t\t\t(at 10.0000 16.5 0)\n'
        '\t\t)\n'
        + pin_lines
        + '\t)\n'
        ')\n'
    )


# ---------------------------------------------------------------------------
# fix_subsheet_labels — tests
# ---------------------------------------------------------------------------

def test_declared_hl_preserved(tmp_path: Path) -> None:
    """HL whose name matches a declared parent pin is left as hierarchical_label."""
    (tmp_path / "sub.kicad_sch").write_text(_sub_sch("EXPOSED", "INTERNAL"))
    (tmp_path / "top.kicad_sch").write_text(
        _parent_top("sub.kicad_sch", pins=["EXPOSED"])
    )
    fix_subsheet_labels(str(tmp_path / "top.kicad_sch"))
    assert '(hierarchical_label "EXPOSED"' in (tmp_path / "sub.kicad_sch").read_text()


def test_undeclared_hl_converted(tmp_path: Path) -> None:
    """HL not in declared parent pins becomes a net label."""
    (tmp_path / "sub.kicad_sch").write_text(_sub_sch("EXPOSED", "INTERNAL"))
    (tmp_path / "top.kicad_sch").write_text(
        _parent_top("sub.kicad_sch", pins=["EXPOSED"])
    )
    fix_subsheet_labels(str(tmp_path / "top.kicad_sch"))
    text = (tmp_path / "sub.kicad_sch").read_text()
    assert '(hierarchical_label "INTERNAL"' not in text
    assert '(label "INTERNAL"' in text


def test_undeclared_hl_shape_removed(tmp_path: Path) -> None:
    (tmp_path / "sub.kicad_sch").write_text(_sub_sch("INTERNAL"))
    (tmp_path / "top.kicad_sch").write_text(
        _parent_top("sub.kicad_sch", pins=[])
    )
    fix_subsheet_labels(str(tmp_path / "top.kicad_sch"))
    assert "(shape" not in (tmp_path / "sub.kicad_sch").read_text()


def test_undeclared_hl_justify_left_bottom(tmp_path: Path) -> None:
    """Converted HL at angle=0 gets justify 'left bottom'."""
    (tmp_path / "sub.kicad_sch").write_text(_sub_sch("INT", angle=0.0))
    (tmp_path / "top.kicad_sch").write_text(
        _parent_top("sub.kicad_sch", pins=[])
    )
    fix_subsheet_labels(str(tmp_path / "top.kicad_sch"))
    assert "(justify left bottom)" in (tmp_path / "sub.kicad_sch").read_text()


def test_undeclared_hl_justify_right_bottom(tmp_path: Path) -> None:
    """Converted HL at angle=180 gets justify 'right bottom'."""
    (tmp_path / "sub.kicad_sch").write_text(_sub_sch("INT", angle=180.0))
    (tmp_path / "top.kicad_sch").write_text(
        _parent_top("sub.kicad_sch", pins=[])
    )
    fix_subsheet_labels(str(tmp_path / "top.kicad_sch"))
    assert "(justify right bottom)" in (tmp_path / "sub.kicad_sch").read_text()


def test_missing_subsheet_no_crash(tmp_path: Path) -> None:
    """Sub-sheet file referenced in parent but absent on disk is silently skipped."""
    (tmp_path / "top.kicad_sch").write_text(
        _parent_top("nonexistent.kicad_sch", pins=["NET"])
    )
    fix_subsheet_labels(str(tmp_path / "top.kicad_sch"))  # must not raise


def test_no_change_file_not_rewritten(tmp_path: Path) -> None:
    """Sub-sheet with only declared HLs is not written to disk."""
    sub = tmp_path / "sub.kicad_sch"
    sub.write_text(_sub_sch("EXPOSED"))
    (tmp_path / "top.kicad_sch").write_text(
        _parent_top("sub.kicad_sch", pins=["EXPOSED"])
    )
    content_before = sub.read_text()
    fix_subsheet_labels(str(tmp_path / "top.kicad_sch"))
    assert sub.read_text() == content_before


# ---------------------------------------------------------------------------
# fix_sheet_symbol_sizes — per-pin tie-label identity (wayfinder #67)
# ---------------------------------------------------------------------------

def _sheet_at(
    sx: float, sy: float, filename: str, pins: list[str], width: float = _W
) -> str:
    """A sheet block at an arbitrary origin, all pins on its right edge --
    the shape sheet_pin_sync.reconcile_sheet_pins() produces for a
    freshly-created sheet symbol."""
    body = (
        '\t(sheet\n'
        f'\t\t(at {sx:.4f} {sy:.4f})\n'
        f'\t\t(size {width:.4f} {_OLD_H:.4f})\n'
        f'\t\t(property "Sheetfile" "{filename}"\n'
        f'\t\t\t(at {sx:.4f} {sy + _OLD_H + 0.7116:.4f} 0)\n'
        '\t\t)\n'
    )
    for k, name in enumerate(pins):
        body += _pin_block(name, sx + width, sy + 2.54 + k * 2.54, angle=0)
    body += '\t)\n'
    return body


def test_sheet_symbols_sharing_an_edge_keep_their_own_tie_labels(tmp_path: Path) -> None:
    """Wayfinder #67 regression: two sheet symbols stacked in a column (same
    x, therefore the same left AND right edge) that each expose a pin with
    the SAME name must each keep their own coincident tie label.

    The label-repositioning lookup used to be keyed on (x, pin_name) alone,
    which is not unique across sheets sharing an edge -- the last sheet
    scanned overwrote the others, and every same-named tie label on the
    canvas was relocated onto that one sheet's pin. Real symptom on the
    acquisition Analog+Power board: four freshly-created LDO_* sheet symbols
    each with an "AGND" pin left three of the four sheet pins with no
    coincident tie (`pin_not_connected`) plus a stranded label
    (`label_dangling`).

    Both sheets here have 2 pins, so n_left=1: the FIRST pin (alphabetically
    "AGND") moves to the left edge, the second stays on the right.
    """
    sx, sy_a, sy_b = 10.0, 10.0, 60.0
    text = (
        '(kicad_sch (version 20211123) (generator circuit_synth)\n'
        + _sheet_at(sx, sy_a, "ldo_a.kicad_sch", ["AGND", "VOUT_A"])
        + _sheet_at(sx, sy_b, "ldo_b.kicad_sch", ["AGND", "VOUT_B"])
        # Each sheet's own AGND tie label, coincident with its own pin.
        + _hl("AGND", sx + _W, sy_a + 2.54, angle=0.0)
        + _hl("AGND", sx + _W, sy_b + 2.54, angle=0.0)
        + ')\n'
    )
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(text)
    fix_sheet_symbol_sizes(str(sch))
    out = sch.read_text()

    # Both AGND pins move to their own sheet's LEFT edge, first slot.
    expected = {
        (sx, sy_a + 2.54),
        (sx, sy_b + 2.54),
    }
    agnd_labels = {
        (round(float(m.group(1)), 4), round(float(m.group(2)), 4))
        for m in re.finditer(
            r'\(hierarchical_label "AGND"\n\t\t\(shape input\)\n\t\t'
            r'\(at ([\d.+-]+) ([\d.+-]+)',
            out,
        )
    }
    assert agnd_labels == expected, (
        f"AGND tie labels collapsed onto one sheet instead of tracking their "
        f"own pins: got {sorted(agnd_labels)}, expected {sorted(expected)}"
    )
