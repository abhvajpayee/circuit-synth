"""
Post-processing passes applied to every generated KiCad schematic.

These correct known circuit-synth generation artefacts:

  fix_sheet_symbol_sizes()
    - Sheet symbol boxes are sized from len(sub_circ.nets) (all internal nets),
      making them 4-5x taller than the boundary pin count requires.
    - Pins are emitted on the right side only.

  fix_subsheet_labels()
    - Internal/power nets in sub-sheets are sometimes marked as
      hierarchical_label even when no parent sheet pin exposes them,
      causing KiCad 10 ERC 'hier_label_mismatch' violations.

Neither pass converts a `hierarchical_label`'s TYPE (e.g. root-level
hierarchical_label -> plain label). That conversion used to live here, but
was removed 2026-07-29: a root sheet (no parent of its own) can never have a
valid hierarchical_label -- there is no parent sheet symbol it could ever
match -- so the generator itself (schematic_writer.py's
`_net_crosses_boundary`, which returns False for the root's own boundary
check by construction: the root's own subtree always covers the entire
project, so there is never a circuit "outside" it) must simply never emit
one there in the first place. Patching a wrong TYPE back to correct here,
after the fact, would silently mask any future regression in that generator
logic -- a root-level hierarchical_label would stop being an ERC failure
that gets noticed and fixed at the source, and instead become invisible
output that happens to still be valid. Confirmed empirically (both with and
without bus injection) that a current, correct generator run already
produces zero hierarchical_label elements in the root file on its own, so
this file no longer needs to compensate for it. If a root-level
hierarchical_label ever reappears, that is a generator bug to fix in
schematic_writer.py/bus_emit.py, not something to re-patch here.
"""

from __future__ import annotations

import math
import re
from pathlib import Path


def fix_sheet_symbol_sizes(sch_path: str) -> None:
    """
    Resize every hierarchical sheet symbol in `sch_path` to match its actual
    pin count, split pins across LEFT and RIGHT sides (first ceil(n/2) on
    left, rest on right) to halve box height, and track that repositioning
    into every tie label (whether `label` or `hierarchical_label`) that
    connects to one of those pins. Never changes a label's TYPE (hierarchical
    vs. local) -- only its position and justify -- see the module docstring
    for why that conversion doesn't belong here.

    Must be called on EVERY schematic file in a hierarchical project, not
    just the true root -- each file's OWN `(sheet ...)` blocks describe ITS
    OWN children (e.g. MCU.kicad_sch describes DRAM/TPM), and those need
    the identical resize/split treatment the root's own children get.
    Calling this only on the root leaves every non-root sheet's own child
    sheet symbols at their original, oversized, right-side-only-pin layout
    (found 2026-07-28: e.g. MCU's own DRAM sheet symbol stayed at
    25.4x116.84mm with all 26 pins jammed on one side, while sibling sheet
    symbols on the actual root were correctly resized to 28x50.8mm with
    pins split left/right).
    """
    path = Path(sch_path)
    lines = path.read_text().splitlines(keepends=True)

    PIN_PITCH    = 2.54    # mm grid
    MARGIN_TOP   = 2.54    # mm from sheet top to first pin
    MARGIN_BOT   = 2.54    # mm from last pin to sheet bottom
    LABEL_OFFSET = 0.7116  # KiCad 1/36-inch offset for Sheetfile label

    # ---- Pass 1: scan every sheet block ----
    # label_updates[(old_x, pin_name)]                = (new_x, new_y, new_angle)
    # label_updates_exact[(old_x, old_y, pin_name)]   = (new_x, new_y, new_angle)
    # sheet_rewrites[block_start_line]                = (block_end_line, new_block_text)
    #
    # Two lookups, because the (x, name) key alone is NOT unique: it
    # identifies a pin only when no two sheet symbols on this canvas share
    # both a vertical edge and a pin name. Clean generation happens to avoid
    # that (text-flow placement gives each sheet its own x), but wayfinder
    # #67's incremental sheet creation stacks new sheet symbols in a column
    # at one x -- so four freshly-created LDO_* sheet symbols each carrying
    # an "AGND" pin all collided on the single key (58.42, "AGND"), the last
    # one scanned overwriting the rest. Every AGND tie label on the canvas
    # was then relocated onto that ONE sheet's pin, leaving the other three
    # sheet pins with no coincident tie (kicad-cli sch erc:
    # pin_not_connected) and one label stranded (label_dangling).
    #
    # The exact key adds the pin's own current y, which does identify a
    # single pin. The old (x, name) key is kept as a fallback for the case it
    # was introduced for (wayfinder #6: a tie label frozen at the sheet's
    # other edge from an earlier run, so its y may not match the pin's
    # current y either) -- but only when that key is UNAMBIGUOUS, i.e. it was
    # registered by exactly one pin. A guessing fallback is what produced the
    # wrong-sheet relocation above; leaving an ambiguous label untouched is
    # strictly safer than moving it somewhere provably arbitrary.
    label_updates:  dict[tuple[float, str], tuple[float, float, int]] = {}
    label_updates_exact: dict[tuple[float, float, str], tuple[float, float, int]] = {}
    ambiguous_keys: set[tuple[float, str]] = set()
    sheet_rewrites: dict[int, tuple[int, str]] = {}

    def _register(kx: float, ky: float | None, name: str, target) -> None:
        if ky is not None:
            label_updates_exact[(round(kx, 4), round(ky, 4), name)] = target
        key = (kx, name)
        if key in label_updates and label_updates[key] != target:
            ambiguous_keys.add(key)
        label_updates[key] = target

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

        # Each pin's name AND its own CURRENT position -- the position is
        # what makes a pin (and therefore its coincident tie label)
        # individually identifiable across sheet symbols that share an edge.
        pin_entries: list[tuple[str, float | None, float | None]] = []
        for pin_m in re.finditer(
            r'\t\t\(pin\s+"([^"]+)"[^\n]*\n(?:[^\n]*\n)*?\t\t\t\(at\s+([\d.+-]+)\s+([\d.+-]+)',
            raw,
        ):
            pin_entries.append(
                (pin_m.group(1), float(pin_m.group(2)), float(pin_m.group(3)))
            )
        pin_names = re.findall(r'\t\t\(pin\s+"([^"]+)"', raw)
        n = len(pin_names)
        if n == 0: continue
        # Fall back to name-only registration if the position scan didn't
        # line up one-to-one (malformed or unexpected block layout).
        if len(pin_entries) != n or [e[0] for e in pin_entries] != pin_names:
            pin_entries = [(name, None, None) for name in pin_names]

        n_left = math.ceil(n / 2)   # left side always >= right side
        raw_h  = MARGIN_TOP + (n_left - 1) * PIN_PITCH + MARGIN_BOT
        new_h  = math.ceil(raw_h / PIN_PITCH) * PIN_PITCH  # snap to grid

        # A tie label's CURRENT x may be either edge, not just old_right_x:
        # the module docstring's "pins are emitted on the right side only"
        # describes the base writer's *first-ever* output, but any pin that
        # was classified LEFT (x=sx) on some earlier regeneration has its tie
        # label sitting at sx, not old_right_x -- e.g. the pin/member set
        # shrank or grew and the alphabetical left/right split boundary
        # (n_left) shifted, moving a given name from the left half to the
        # right half or vice versa between runs. Registering both possible
        # incoming x's under the same target keeps the lookup below correct
        # regardless of which side the pin's tie was on last time. Found on
        # the acquisition board 2026-07-08: ETH_TX_EN's tie label was frozen
        # at the MCU sheet's LEFT edge from an earlier run while the pin
        # itself is now on the RIGHT -- with only old_right_x registered, the
        # label was never found and the net was effectively disconnected on
        # the root sheet (no coincident tie at the pin's real position).
        for k, (name, px, py) in enumerate(pin_entries[:n_left]):
            target = (sx, sy + MARGIN_TOP + k * PIN_PITCH, 180)
            if px is not None:
                _register(px, py, name, target)
            _register(old_right_x, py, name, target)
            _register(sx, py, name, target)
        for k, (name, px, py) in enumerate(pin_entries[n_left:]):
            target = (sx + cur_w, sy + MARGIN_TOP + k * PIN_PITCH, 0)
            if px is not None:
                _register(px, py, name, target)
            _register(old_right_x, py, name, target)
            _register(sx, py, name, target)

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
                # Angle group accepts decimals too ("0.0000"/"180.0000"), the
                # format kicad_sch_api's own formatter always emits on resave
                # -- an integer-only `\d+` here silently fails to match and
                # leaves this label's Y position stale (see the pin-position
                # comment below for the more serious sibling of this bug).
                m = re.match(r'(\t\t\t\(at\s+)([\d.+-]+)\s+([\d.+-]+)(\s+[\d.+-]+\))', ln)
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
                # Angle group must accept decimals ("0.0000"/"180.0000"), not
                # just a bare integer ("0"/"180"). kicad_sch_api's own
                # formatter always writes the decimal form when it resaves a
                # file (e.g. an unrelated incremental-sync edit elsewhere in
                # the project triggers a full round-trip through its object
                # model). An integer-only `\d+` here silently fails to match
                # any pin whose incoming angle is already decimal -- so its
                # left_idx/right_idx slot counter never advances and its
                # coordinates are never rewritten, freezing it at a stale
                # position. Across repeated regenerations (pin count shifting
                # as bus-member collapsing removes pins, changing n_left) this
                # produced two *different* pins landing on the exact same
                # coordinate -- a real position collision on the sheet
                # symbol, found on the acquisition board's MCU sheet
                # 2026-07-08 (GND and SDMMC_[0..10] both at (63.5, 22.86)).
                m = re.match(r'(\t\t\t\(at\s+)([\d.+-]+)\s+([\d.+-]+)(\s+)([\d.+-]+)(\))', ln)
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

    def _lookup(name, at_x, at_y):
        """Find the repositioning target for a tie label at (at_x, at_y).

        Exact (x, y, name) first -- that identifies one specific sheet pin.
        Only if nothing matches exactly does this fall back to the looser
        (x, name) key (wayfinder #6's stale-edge case), and then only when
        that key is unambiguous; an ambiguous one means two or more sheet
        symbols on this canvas share an edge and a pin name, and guessing
        between them is what wayfinder #67 had to undo.
        """
        if name is None or at_x is None:
            return None
        if at_y is not None:
            for (kx, ky, kn), v in label_updates_exact.items():
                if kn == name and abs(kx - at_x) < 0.01 and abs(ky - at_y) < 0.01:
                    return v
        for (kx, kn), v in label_updates.items():
            if kn == name and abs(kx - at_x) < 0.01:
                return None if (kx, kn) in ambiguous_keys else v
        return None

    # ---- Pass 2: rebuild the file ----
    out: list[str] = []
    i = 0
    while i < len(lines):
        if i in sheet_rewrites:
            block_end, new_text = sheet_rewrites[i]
            out.append(new_text)
            i = block_end
            continue

        # Reposition in place only -- never convert the label's type. A
        # hierarchical_label here always legitimately needs to reach this
        # sheet's own real parent one level up (see module docstring for why
        # the generator itself, not this pass, is responsible for never
        # emitting one on the true root, where no such parent exists).
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

            update = _lookup(hl_name, at_x, at_y)

            final_x     = update[0] if update else (at_x or 0.0)
            final_y     = update[1] if update else (at_y or 0.0)
            final_angle = float(update[2]) if update else (at_angle or 0.0)
            new_justify = 'right bottom' if abs(final_angle - 180.0) < 1 else 'left bottom'

            new_hl: list[str] = []
            for j, ln in enumerate(hl_block):
                if at_line_idx is not None and j == at_line_idx:
                    ln = f'\t\t(at {final_x:.4f} {final_y:.4f} {final_angle:.4f})\n'
                elif re.match(r'\t\t\t\(justify\s+', ln):
                    ln = f'\t\t\t(justify {new_justify})\n'
                new_hl.append(ln)
            out.append(''.join(new_hl))
            continue

        # A root sheet (no parent of its own) ties sibling sheet-symbol pins
        # together with plain, coincident-point `label`s rather than
        # hierarchical_labels (see bus_emit.py's _parent_surgery). Those tie
        # labels must track a sheet pin's position the same way a
        # hierarchical_label does above -- otherwise, once this pass moves a
        # pin to its freshly recomputed grid slot, the (unmoved) tie label is
        # stranded at the pin's OLD coordinate and collides with whatever
        # *different* pin the fresh grid now assigns to that same spot. Found
        # on the acquisition board 2026-07-08 as a second-order effect of
        # fixing the bus-connectivity bug: MCU/WiFi/Storage's bus-vector tie
        # labels (SDMMC_[0..10], SD_[0..5], SPI_[0..3], UART_[0..1]) went
        # stale and collided with newly-repositioned scalar pins (GND, SD_CD,
        # WIFI_CHEN, WIFI_BOOT).
        if re.match(r'\t\(label\s+"', lines[i]):
            lbl_block = [lines[i]]
            i += 1; depth = 1
            while i < len(lines) and depth > 0:
                depth += lines[i].count('(') - lines[i].count(')')
                lbl_block.append(lines[i]); i += 1

            name_m = re.match(r'\t\(label\s+"([^"]+)"', lbl_block[0])
            lbl_name = name_m.group(1) if name_m else None
            at_line_idx = None; at_x = at_y = at_angle = None
            for j, ln in enumerate(lbl_block):
                m = re.match(r'\t\t\(at\s+([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\)', ln)
                if m:
                    at_line_idx = j
                    at_x, at_y, at_angle = float(m.group(1)), float(m.group(2)), float(m.group(3))
                    break

            update = _lookup(lbl_name, at_x, at_y)

            if update is not None:
                final_x, final_y, final_angle = update[0], update[1], float(update[2])
                new_justify = 'right bottom' if abs(final_angle - 180.0) < 1 else 'left bottom'
                new_lbl: list[str] = []
                for j, ln in enumerate(lbl_block):
                    if at_line_idx is not None and j == at_line_idx:
                        ln = f'\t\t(at {final_x:.4f} {final_y:.4f} {final_angle:.4f})\n'
                    elif re.match(r'\t\t\t\(justify\s+', ln):
                        ln = f'\t\t\t(justify {new_justify})\n'
                    new_lbl.append(ln)
                out.append(''.join(new_lbl))
            else:
                out.append(''.join(lbl_block))
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
