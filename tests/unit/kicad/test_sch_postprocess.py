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


def _sheet(pins: list[str]) -> str:
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
    for k, name in enumerate(pins):
        py = _SY + 2.54 + k * 2.54
        body += _pin_block(name, _OLD_RIGHT_X, py, angle=0)
    body += '\t)\n'
    return body


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


def _top(pins: list[str]) -> str:
    """Top-level schematic: one sheet + one HL per pin (all initially on right)."""
    hls = ''.join(
        _hl(name, _OLD_RIGHT_X, _SY + 2.54 + k * 2.54, angle=0.0)
        for k, name in enumerate(pins)
    )
    return (
        '(kicad_sch (version 20211123) (generator circuit_synth)\n'
        + _sheet(pins)
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
# fix_sheet_symbol_sizes — hierarchical_label → label conversion
# ---------------------------------------------------------------------------

def test_hl_converted_to_label(tmp_path: Path) -> None:
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(_top(["CLK", "DATA"]))
    fix_sheet_symbol_sizes(str(sch))
    text = sch.read_text()
    assert "(hierarchical_label" not in text
    assert '(label "CLK"' in text
    assert '(label "DATA"' in text


def test_hl_shape_line_removed(tmp_path: Path) -> None:
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(_top(["CLK"]))
    fix_sheet_symbol_sizes(str(sch))
    assert "(shape" not in sch.read_text()


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


def test_unmatched_hl_still_converted(tmp_path: Path) -> None:
    """HL with no matching sheet pin is still converted to a net label (no crash)."""
    content = (
        '(kicad_sch (version 20211123) (generator circuit_synth)\n'
        + _hl("ORPHAN", 50.0, 50.0, 0.0)
        + ')\n'
    )
    sch = tmp_path / "top.kicad_sch"
    sch.write_text(content)
    fix_sheet_symbol_sizes(str(sch))
    text = sch.read_text()
    assert "(hierarchical_label" not in text
    assert '(label "ORPHAN"' in text


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
