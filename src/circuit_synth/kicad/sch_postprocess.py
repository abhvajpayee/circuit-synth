"""
Post-processing passes applied to every generated KiCad schematic.

These correct known circuit-synth generation artefacts:

  fix_sheet_symbol_sizes()
    - Sheet symbol boxes are sized from len(sub_circ.nets) (all internal nets),
      making them 4–5× taller than the boundary pin count requires.
    - Pins are emitted on the right side only.
    - Root-level hierarchical_label elements fail KiCad 10 ERC
      ("cannot be connected to non-existent parent sheet").

  fix_subsheet_labels()
    - Internal/power nets in sub-sheets are sometimes marked as
      hierarchical_label even when no parent sheet pin exposes them,
      causing KiCad 10 ERC 'hier_label_mismatch' violations.
"""

from __future__ import annotations

import math
import re
from pathlib import Path


def fix_sheet_symbol_sizes(top_sch_path: str) -> None:
    """
    Resize every hierarchical sheet symbol in the top-level schematic to match
    its actual pin count, split pins across LEFT and RIGHT sides (first
    ceil(n/2) on left, rest on right) to halve box height, and convert all
    root-level hierarchical_labels to net labels for KiCad 10 compatibility.
    """
    path = Path(top_sch_path)
    lines = path.read_text().splitlines(keepends=True)

    PIN_PITCH    = 2.54    # mm grid
    MARGIN_TOP   = 2.54    # mm from sheet top to first pin
    MARGIN_BOT   = 2.54    # mm from last pin to sheet bottom
    LABEL_OFFSET = 0.7116  # KiCad 1/36-inch offset for Sheetfile label

    # ---- Pass 1: scan every sheet block ----
    # label_updates[(old_right_x, pin_name)] = (new_x, new_y, new_angle)
    # sheet_rewrites[block_start_line]       = (block_end_line, new_block_text)
    label_updates:  dict[tuple[float, str], tuple[float, float, int]] = {}
    sheet_rewrites: dict[int, tuple[int, str]] = {}

    i = 0
    while i < len(lines):
        if lines[i] != '\t(sheet\n':
            i += 1; continue

        block_start = i
        block = [lines[i]]
        i += 1; depth = 1
        while i < len(lines) and depth > 0:
            depth += lines[i].count('(') - lines[i].count(')')
            block.append(lines[i]); i += 1
        block_end = i

        raw = ''.join(block)

        at_m = re.search(r'\t\t\(at\s+([\d.+-]+)\s+([\d.+-]+)\)', raw)
        if not at_m: continue
        sx, sy = float(at_m.group(1)), float(at_m.group(2))

        size_m = re.search(r'\t\t\(size\s+([\d.]+)\s+([\d.]+)\)', raw)
        if not size_m: continue
        cur_w = float(size_m.group(1))
        old_right_x = sx + cur_w

        pin_names = re.findall(r'\t\t\(pin\s+"([^"]+)"', raw)
        n = len(pin_names)
        if n == 0: continue

        n_left = math.ceil(n / 2)   # left side always >= right side
        raw_h  = MARGIN_TOP + (n_left - 1) * PIN_PITCH + MARGIN_BOT
        new_h  = math.ceil(raw_h / PIN_PITCH) * PIN_PITCH  # snap to grid

        for k, name in enumerate(pin_names[:n_left]):
            label_updates[(old_right_x, name)] = (sx, sy + MARGIN_TOP + k * PIN_PITCH, 180)
        for k, name in enumerate(pin_names[n_left:]):
            label_updates[(old_right_x, name)] = (sx + cur_w, sy + MARGIN_TOP + k * PIN_PITCH, 0)

        # Rewrite sheet block
        new_sheetfile_y = sy + new_h + LABEL_OFFSET
        left_idx = right_idx = pin_counter = 0
        in_sf = False; sf_depth = 0
        in_pin = False; pin_depth = 0
        is_left = False
        new_block: list[str] = []

        for ln in block:
            if re.match(r'\t\t\(property\s+"Sheetfile"', ln):
                in_sf = True; sf_depth = 1
                new_block.append(ln); continue
            if in_sf:
                sf_depth += ln.count('(') - ln.count(')')
                m = re.match(r'(\t\t\t\(at\s+)([\d.+-]+)\s+([\d.+-]+)(\s+\d+\))', ln)
                if m:
                    ln = f'{m.group(1)}{m.group(2)} {new_sheetfile_y:.4f}{m.group(4)}\n'
                if sf_depth == 0: in_sf = False
                new_block.append(ln); continue

            if re.match(r'\t\t\(pin\s+"', ln):
                in_pin = True; pin_depth = 1
                is_left = (pin_counter < n_left); pin_counter += 1
                new_block.append(ln); continue
            if in_pin:
                pin_depth += ln.count('(') - ln.count(')')
                m = re.match(r'(\t\t\t\(at\s+)([\d.+-]+)\s+([\d.+-]+)(\s+)(\d+)(\))', ln)
                if m:
                    if is_left:
                        nx, ny, na = sx, sy + MARGIN_TOP + left_idx * PIN_PITCH, 180
                        left_idx += 1
                    else:
                        nx, ny, na = sx + cur_w, sy + MARGIN_TOP + right_idx * PIN_PITCH, 0
                        right_idx += 1
                    ln = f'{m.group(1)}{nx:.4f} {ny:.4f}{m.group(4)}{na}{m.group(6)}\n'
                m2 = re.match(r'(\t{4}\(justify\s+)\w+(\))', ln)
                if m2:
                    just = 'left' if is_left else 'right'
                    ln = f'{m2.group(1)}{just}{m2.group(2)}\n'
                if pin_depth == 0: in_pin = False
                new_block.append(ln); continue

            m = re.match(r'(\t\t\(size\s+)([\d.]+)(\s+)([\d.]+)(\))', ln)
            if m:
                ln = f'{m.group(1)}{cur_w:.4f}{m.group(3)}{new_h:.4f}{m.group(5)}\n'
            new_block.append(ln)

        sheet_rewrites[block_start] = (block_end, ''.join(new_block))

    # ---- Pass 2: rebuild the file ----
    out: list[str] = []
    i = 0
    while i < len(lines):
        if i in sheet_rewrites:
            block_end, new_text = sheet_rewrites[i]
            out.append(new_text)
            i = block_end
            continue

        # Convert every hierarchical_label → net label (KiCad 10: root-sheet
        # hierarchical_labels have no parent to connect to and fail ERC).
        if re.match(r'\t\(hierarchical_label\s+"', lines[i]):
            hl_block = [lines[i]]
            i += 1; depth = 1
            while i < len(lines) and depth > 0:
                depth += lines[i].count('(') - lines[i].count(')')
                hl_block.append(lines[i]); i += 1

            name_m = re.match(r'\t\(hierarchical_label\s+"([^"]+)"', hl_block[0])
            hl_name = name_m.group(1) if name_m else None
            at_line_idx = None; at_x = at_y = at_angle = None
            for j, ln in enumerate(hl_block):
                m = re.match(r'\t\t\(at\s+([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\)', ln)
                if m:
                    at_line_idx = j
                    at_x, at_y, at_angle = float(m.group(1)), float(m.group(2)), float(m.group(3))
                    break

            update = None
            if hl_name is not None and at_x is not None:
                for (kx, kn), v in label_updates.items():
                    if kn == hl_name and abs(kx - at_x) < 0.01:
                        update = v; break

            final_x     = update[0] if update else (at_x or 0.0)
            final_y     = update[1] if update else (at_y or 0.0)
            final_angle = float(update[2]) if update else (at_angle or 0.0)
            new_justify = 'right bottom' if abs(final_angle - 180.0) < 1 else 'left bottom'

            new_hl: list[str] = []
            for j, ln in enumerate(hl_block):
                if j == 0:
                    ln = re.sub(r'\(hierarchical_label\s+', '(label ', ln)
                elif re.match(r'\t\t\(shape\s+', ln):
                    continue
                elif at_line_idx is not None and j == at_line_idx:
                    ln = f'\t\t(at {final_x:.4f} {final_y:.4f} {final_angle:.4f})\n'
                elif re.match(r'\t\t\t\(justify\s+', ln):
                    ln = f'\t\t\t(justify {new_justify})\n'
                new_hl.append(ln)
            out.append(''.join(new_hl))
            continue

        out.append(lines[i]); i += 1

    path.write_text(''.join(out))


def fix_subsheet_labels(top_sch_path: str) -> None:
    """
    Convert hierarchical_labels in sub-sheets that have no matching sheet pin
    in the parent to regular net labels.  circuit-synth marks some internal
    and power nets as hierarchical_labels even though they are never exposed
    via a parent sheet pin, causing KiCad 10 'hier_label_mismatch' ERC errors.
    """
    top = Path(top_sch_path)
    parent_text = top.read_text()

    # Build map: sub-sheet filename → set of pin names declared in parent
    sheet_pins: dict[str, set[str]] = {}
    lines = parent_text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        if lines[i] != '\t(sheet\n':
            i += 1; continue
        block: list[str] = [lines[i]]; i += 1; depth = 1
        while i < len(lines) and depth > 0:
            depth += lines[i].count('(') - lines[i].count(')')
            block.append(lines[i]); i += 1
        raw = ''.join(block)
        fname_m = re.search(r'\(property\s+"Sheetfile"\s+"([^"]+)"', raw)
        if fname_m:
            sheet_pins[fname_m.group(1)] = set(re.findall(r'\t\t\(pin\s+"([^"]+)"', raw))

    sub_dir = top.parent
    for fname, declared_pins in sheet_pins.items():
        sub_path = sub_dir / fname
        if not sub_path.exists():
            continue
        sub_lines = sub_path.read_text().splitlines(keepends=True)
        out: list[str] = []; i = 0; changed = 0
        while i < len(sub_lines):
            if not re.match(r'\t\(hierarchical_label\s+"', sub_lines[i]):
                out.append(sub_lines[i]); i += 1; continue

            hl_block: list[str] = [sub_lines[i]]; i += 1; depth = 1
            while i < len(sub_lines) and depth > 0:
                depth += sub_lines[i].count('(') - sub_lines[i].count(')')
                hl_block.append(sub_lines[i]); i += 1

            name_m = re.match(r'\t\(hierarchical_label\s+"([^"]+)"', hl_block[0])
            hl_name = name_m.group(1) if name_m else None

            if hl_name in declared_pins:
                out.append(''.join(hl_block)); continue

            at_angle = 0.0
            for ln in hl_block:
                m = re.match(r'\t\t\(at\s+[\d.+-]+\s+[\d.+-]+\s+([\d.+-]+)\)', ln)
                if m:
                    at_angle = float(m.group(1)); break
            new_justify = 'right bottom' if abs(at_angle - 180.0) < 1 else 'left bottom'

            new_hl: list[str] = []
            for j, ln in enumerate(hl_block):
                if j == 0:
                    ln = re.sub(r'\(hierarchical_label\s+', '(label ', ln)
                elif re.match(r'\t\t\(shape\s+', ln):
                    continue
                elif re.match(r'\t\t\t\(justify\s+', ln):
                    ln = f'\t\t\t(justify {new_justify})\n'
                new_hl.append(ln)
            out.append(''.join(new_hl))
            changed += 1

        if changed:
            sub_path.write_text(''.join(out))
