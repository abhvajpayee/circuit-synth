"""Inject graphical KiCad vector buses for declared :class:`Bus` objects.

circuit-synth connects by dropping one net-label per pin, so the members of a
bus appear as scattered individual labels. After the sheets are written, this
pass draws a real vector bus on whichever sheet(s) carry the members:

* **Plain** bus (``FMC_D[0..15]``) — one tap per member, on every sheet that
  has real (non-tie) component connections to at least two members.
* **Aliased** bus (``SPI_[0..3]``) — each component pin is **retext**ed to the
  elaborated member name (``SPI_SCK``) so the schematic reads by signal, while the
  bus stub keeps the numeric positional name (``SPI_0``) as the vector-bus tie. The
  tap therefore carries a **dual label** (``SPI_0`` + ``SPI_SCK`` on the same stub),
  tying the elaborated pin net to the numeric bus member. Drawn on **every** sheet a
  member appears, so the translation is uniform across the design. The net stays a
  numeric vector member (canonical net name remains ``SPI_0``); only the labels at
  the pins change, so connectivity is identical.

**Leaf vs. parent is per-bus and per-file, not global.** A circuit can be the
genuine parent of one bus (its child's sheet symbol exposes that bus's
members as pins) while simultaneously being an ordinary leaf -- with real
component connections of its own -- for a *different* bus, or even the same
one at a different level. E.g. MCU is the parent of DRAM for the FMC_D bus
(DRAM's sheet symbol exposes FMC_D's members) but is *also* a real leaf for
FMC_D itself (the STM32's own DQ pins wire straight to the same bus members),
and is an ordinary leaf (not a parent at all) for the SPI/UART/SDMMC/SD buses
that live one level further up, between MCU and its siblings. These roles are
independent and can overlap on the same file; nothing here treats "is a
parent" as disqualifying a file from also being drawn on as a leaf.

Telling a real (leaf) connection apart from a parent-side tie is positional:
`_add_subcircuit_sheets` (schematic_writer.py) always places a child-tie label
at the child's own sheet-pin coordinate, offset by a fixed, known amount in X
(`_TIE_LABEL_X_OFFSET` -- an arrow-glyph rendering offset relied on elsewhere,
see that constant's comment for why it isn't simply eliminated). Any label
with a member's name sitting anywhere else is a genuine component connection,
never a tie -- see `_real_label_occurrences` / `_tie_label_positions`.

**Hierarchical vs. local is decided per real occurrence, exactly like an
ordinary net label** (see `_is_net_hierarchical` in schematic_writer.py): a
leaf's own bus head label is emitted as `hierarchical_label` if and only if
that leaf's own real (non-tie) occurrence of the bus's members is itself a
`hierarchical_label` there already -- i.e. this pass never invents a new
"is this bus hierarchical" decision, it just carries forward whatever type
generation already assigned at that specific point. Different leaves of the
same bus can disagree (e.g. MCU's own FMC_D occurrence is local -- FMC_D never
reaches past MCU -- while DRAM's is hierarchical, since DRAM must expose it to
its parent, MCU).

Wherever at least one leaf's occurrence is hierarchical, the bus also needs
its per-member sheet pins/tie labels collapsing on its genuine parent
sheet(s) (see `_bus_parents`/`_parent_surgery`) into a single bus-vector pin,
so KiCad's ERC bus-membership check passes.

Multiple buses on one sheet are stacked vertically in a single column to the
right of the existing content. Placement is intentionally simple; it can run off
the page edge on very full sheets — visual only, never a short.
"""

import re
import uuid
from pathlib import Path

_STROKE = "(stroke (width 0) (type default))"


def _u():
    return str(uuid.uuid4())


def _label(text, x, y):
    return (f'\t(label "{text}"\n\t\t(at {x} {y} 0)\n'
            f'\t\t(effects (font (size 1.27 1.27)) (justify left bottom))\n'
            f'\t\t(uuid "{_u()}")\n\t)')


def _hier_label(text, x, y, shape="input"):
    return (f'\t(hierarchical_label "{text}"\n\t\t(shape {shape})\n\t\t(at {x} {y} 0)\n'
            f'\t\t(effects (font (size 1.27 1.27)) (justify left))\n'
            f'\t\t(uuid "{_u()}")\n\t)')


def _bus_block(bus_label, taps, bus_x, top_y, pitch=2.54, stub=20.32, bus_hier=False):
    """Draw one bus: a vertical bus wire + bus label (hierarchical when the bus
    crosses sheets), and per tap a bus_entry + stub wire carrying its label(s)."""
    y1 = top_y + (len(taps) - 1) * pitch + 5.08
    # The naming label must sit exactly on the bus wire's own start point (bus_x,
    # top_y - 2.54) -- anywhere else it's a disconnected floating label, and the
    # bus it was meant to name resolves as anonymous ("<NO NET>") everywhere it
    # crosses sheets, which cascades into every member failing ERC's
    # net_not_bus_member/bus_to_net_conflict checks.
    head = _hier_label(bus_label, bus_x, top_y - 2.54) if bus_hier else _label(bus_label, bus_x, top_y - 2.54)
    s = [
        f'\t(bus\n\t\t(pts (xy {bus_x} {top_y - 2.54}) (xy {bus_x} {y1}))\n\t\t{_STROKE}\n\t\t(uuid "{_u()}")\n\t)',
        head,
    ]
    for k, texts in enumerate(taps):
        y = top_y + k * pitch
        ex, ey = bus_x + 2.54, y + 2.54
        s.append(f'\t(bus_entry\n\t\t(at {bus_x} {y})\n\t\t(size 2.54 2.54)\n\t\t{_STROKE}\n\t\t(uuid "{_u()}")\n\t)')
        s.append(f'\t(wire\n\t\t(pts (xy {ex} {ey}) (xy {ex + stub} {ey}))\n\t\t{_STROKE}\n\t\t(uuid "{_u()}")\n\t)')
        for j, text in enumerate(texts):  # first near the bus, last at stub end
            lx = ex + (2.54 if j == 0 and len(texts) > 1 else stub)
            s.append(_label(text, round(lx, 2), ey))
    return "\n".join(s) + "\n"


def _present(txt, member):
    return bool(re.search(r'\((?:label|hierarchical_label|global_label) "'
                          + re.escape(member) + '"', txt))


# SheetPin.position is 1.27mm inside the sheet symbol's own raw right edge
# (an arrow-glyph rendering offset -- see the matching comment in
# schematic_writer.py's _add_subcircuit_sheets). The tie label
# _add_subcircuit_sheets places for that same pin sits at the raw,
# unshifted edge instead (same Y, X += this) -- deliberately NOT moved to
# coincide with the pin, because sch_postprocess.py's label-repositioning
# lookup depends on finding it at that unshifted edge. This module accounts
# for the resulting, fixed offset itself instead of eliminating it upstream
# (tried 2026-07-28: doing so broke every tie label project-wide, not just
# bus-related ones, since it's relied on for every sheet-pin/tie-label
# resize, not just by this module).
_TIE_LABEL_X_OFFSET = 1.27
_POS_TOL = 0.01  # mm; float-equality tolerance for comparing parsed coordinates
_GRID = 1.27  # mm; KiCad's default connection grid


def _close(a, b, tol=_POS_TOL):
    return abs(a - b) < tol


def _snap(v, grid=_GRID):
    """Round `v` to the nearest connection-grid point. `_bus_block`'s own
    internal offsets (pitch=2.54, stub=20.32, +2.54, +5.08 -- all multiples
    of this grid) keep every position it derives on-grid *provided* the
    (bus_x, top_y) origin passed in already is; this is what establishes
    that starting point. Without it, `max(xs) + 15.0` (bus_x) and the
    initial/incremented `cur_y` values are arbitrary floats, which is what
    the `endpoint_off_grid` ERC warning was flagging on every bus wire, bus
    entry, and stub."""
    return round(round(v / grid) * grid, 2)


def _top_level_block_spans(txt):
    """Return [(start, end), ...] character spans for every top-level
    ``\\t(...)`` s-expression element in `txt` (one KiCad schematic
    element per span: a component, wire, label, bus, bus_entry, etc.).

    Paren-depth scan rather than a fixed-format regex, because elements
    that have round-tripped through kicad-sch-api's own formatter (see
    `inject_buses`' orphaned-bus-graphic handling below) are reformatted
    into a different, multi-line style (e.g. `(effects\\n (font ...)\\n
    (justify ...))` instead of this module's own single-line
    `(effects (font ...) (justify ...))`), so a regex tied to this
    module's own emission templates cannot reliably match them back."""
    spans = []
    i, n = 0, len(txt)
    while i < n:
        if txt[i] == "\t" and i + 1 < n and txt[i + 1] == "(":
            depth = 0
            j = i + 1
            while j < n:
                if txt[j] == "(":
                    depth += 1
                elif txt[j] == ")":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
            end = j + 1 if j < n and txt[j] == "\n" else j
            spans.append((i, end))
            i = end
        else:
            i += 1
    return spans


def _block_at_xy(block_text):
    """First `(at X Y ...)` point in a block, or None."""
    m = re.search(r"\(at ([0-9.\-]+) ([0-9.\-]+)", block_text)
    return (float(m.group(1)), float(m.group(2))) if m else None


def _block_wire_endpoints(block_text):
    """Both endpoints of a `(wire (pts (xy x1 y1) (xy x2 y2)))` block, or None."""
    m = re.search(
        r"\(pts\s*\(xy ([0-9.\-]+) ([0-9.\-]+)\)\s*\(xy ([0-9.\-]+) ([0-9.\-]+)\)",
        block_text,
    )
    if not m:
        return None
    x1, y1, x2, y2 = (float(g) for g in m.groups())
    return (x1, y1), (x2, y2)


def _points_close(p, q, tol=_POS_TOL):
    return _close(p[0], q[0], tol) and _close(p[1], q[1], tol)


def _point_on_segment(p, a, b, tol=_POS_TOL):
    """Whether point `p` lies on the closed line segment `a`-`b`, within
    tolerance `tol` -- a bounding-box check plus a collinearity (cross-
    product) check, not an endpoint-only comparison.

    Needed because an ALIASED bus's tap carries a **dual label** on its stub
    wire (see module docstring / `_bus_block`): the elaborated name sits at
    the wire's far endpoint, but the positional name sits at a point
    *partway along* the same wire (`ex + 2.54`, between the wire's two
    endpoints, not coincident with either) -- `_points_close` against just
    `pts[1]` (the old check this replaces) never matches that inner point,
    so a PLAIN bus's single, endpoint-sited label is still found (this
    reduces to a degenerate segment check there), but an ALIASED bus's
    inner positional label was silently left behind by
    `_strip_orphaned_bus_graphic`, un-stripped -- see that function's
    docstring for the resulting bug (wayfinder #57)."""
    px, py = p
    ax, ay = a
    bx, by = b
    if not (min(ax, bx) - tol <= px <= max(ax, bx) + tol):
        return False
    if not (min(ay, by) - tol <= py <= max(ay, by) + tol):
        return False
    seg_len = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
    if seg_len < tol:
        return _points_close(p, a, tol)
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    return abs(cross) / seg_len < tol


def _bus_graphic_intact(txt, bus_label):
    """Tri-state check for whether `bus_label`'s vector-bus graphic is fully
    present in `txt`:

    - None   -- the aggregate tie label itself isn't present at all (bus
      never injected on this sheet; caller should fall through to the
      normal "not yet injected" path, same as a bare `_present()` miss).
    - True   -- tie label present AND a `(bus (pts (xy X Y) ...))` vector
      line starts at that same point -- genuinely, fully injected.
    - False  -- tie label present but NO matching `(bus ...)` line exists
      at that point -- the graphic was only *partially* destroyed.

    The False case is the actual bug (wayfinder #55): kicad-sch-api's
    `Schematic.load()`/`.save()` (used by `APISynchronizer` on every
    incremental sync) has no parser/model support for the native KiCad
    `bus` or `bus_entry` element types -- confirmed by a direct load+save
    round-trip dropping both unconditionally, for either
    `preserve_format` setting, because `load()` never captures them into
    its own `_data` model in the first place. A round-trip through it
    silently deletes this bus's `(bus ...)` vector-wire and every
    `(bus_entry ...)` tap while leaving the (properly-modeled)
    `hierarchical_label`/`label`/`wire` elements untouched. Without this
    check, `_present()` alone reads as a false "already fully injected"
    (the tie label survives), permanently orphaning each tap's stub wire
    -- its bus-side endpoint no longer connects to anything, which is
    exactly the `unconnected_wire_endpoint` ERC violation this bug
    reports."""
    head_pos = None
    for start, end in _top_level_block_spans(txt):
        block = txt[start:end]
        m = re.match(r'\t\((?:hierarchical_label|label|global_label) "([^"]*)"', block)
        if m and m.group(1) == bus_label:
            head_pos = _block_at_xy(block)
            break
    if head_pos is None:
        return None
    for start, end in _top_level_block_spans(txt):
        block = txt[start:end]
        if re.match(r"\t\(bus\n", block):
            pts = _block_wire_endpoints(block)
            # `(bus ...)` reuses the same (pts (xy a b) (xy c d)) shape as a
            # wire; its first point is the tie label's own anchor (see
            # `_bus_block`: the label sits exactly on the bus wire's start).
            if pts and _points_close(pts[0], head_pos):
                return True
    return False


def _orphaned_head_kind(txt, bus_label):
    """Whether `bus_label`'s (orphaned) head tie label is a
    `hierarchical_label` (True) or plain `label`/`global_label` (False).

    Needed because repairing an orphaned graphic (see
    `_bus_graphic_intact`) cannot re-derive this from the surviving
    per-pin real occurrences the normal first-injection path uses
    (`leaf_hier = any(is_hier for ...)` in `inject_buses`): `_demote()`
    already converted those from `hierarchical_label` to plain `label`
    the first time this bus was drawn (a member's own hierarchical
    crossing is only needed until the bus's aggregate tie absorbs it).
    Re-deriving `leaf_hier` from those now-permanently-demoted labels on
    a repair pass would silently flip a hierarchical bus tie to a local
    one. The orphaned head label itself is the only remaining record of
    the original decision, so capture it before stripping and force the
    repair to reuse it."""
    for start, end in _top_level_block_spans(txt):
        block = txt[start:end]
        m = re.match(r'\t\((hierarchical_label|label|global_label) "([^"]*)"', block)
        if m and m.group(2) == bus_label:
            return m.group(1) == "hierarchical_label"
    return None


def _strip_orphaned_bus_graphic(txt, bus_label, candidate_names):
    """Remove `bus_label`'s orphaned graphic remnants -- the aggregate tie
    label plus each tap's now-dangling stub wire and every label riding on
    it -- identified structurally (a `wire` with a `label` of a name in
    `candidate_names` anywhere along its own span, not just at its far
    endpoint -- see `_point_on_segment`), not by this module's own emission
    format, so it also matches remnants that a kicad-sch-api round-trip has
    since reformatted (see `_bus_graphic_intact`).

    A PLAIN bus's tap carries exactly one label, sitting at the stub wire's
    far endpoint, so an endpoint-only check used to suffice. An ALIASED
    bus's tap carries a **dual label** (see `_bus_block`): the elaborated
    name at the far endpoint, but the positional name partway along the
    SAME wire, not at either endpoint -- an endpoint-only check silently
    left that inner label behind after the wire itself was dropped,
    orphaning it (no wire, no bus_entry) -- KiCad ERC's `label_dangling`.
    Worse, that survivor's text was still the bare positional name, so the
    normal re-injection pass right after this strip mistook it for a fresh,
    genuine occurrence and *retexted* it in place to the elaborated alias,
    rather than recognizing it as stale stub wreckage (wayfinder #57).
    Checking the label's position against the wire's whole span, not just
    one endpoint, catches both labels of a dual-label tap together.

    Only ever called once `_bus_graphic_intact` has already confirmed the
    `(bus ...)`/`(bus_entry ...)` elements are gone; leaves every genuine
    per-pin/component label alone (real occurrences sit at unrelated
    positions -- a component's own pin, not this stub's wire span). After
    stripping, the sheet reads as "not yet injected" for this bus, so the
    normal detection/redraw path rebuilds a complete, non-duplicated
    graphic from scratch."""
    spans = _top_level_block_spans(txt)
    blocks = [txt[s:e] for s, e in spans]

    drop = set()
    for idx, block in enumerate(blocks):
        m = re.match(r'\t\((?:hierarchical_label|label|global_label) "([^"]*)"', block)
        if m and m.group(1) == bus_label:
            drop.add(idx)

    if not drop:
        return txt  # nothing to strip (shouldn't happen; caller already checked)

    label_pos_to_idx = {}
    for idx, block in enumerate(blocks):
        m = re.match(r'\t\(label "([^"]*)"', block)
        if m and m.group(1) in candidate_names:
            pos = _block_at_xy(block)
            if pos:
                label_pos_to_idx[pos] = idx

    for idx, block in enumerate(blocks):
        if re.match(r"\t\(wire\n", block):
            pts = _block_wire_endpoints(block)
            if not pts:
                continue
            a, b = pts
            matched = [lidx for pos, lidx in label_pos_to_idx.items() if _point_on_segment(pos, a, b)]
            if matched:
                drop.add(idx)
                drop.update(matched)

    out = []
    last = 0
    for idx, (start, end) in enumerate(spans):
        if idx in drop:
            out.append(txt[last:start])
            last = end
    out.append(txt[last:])
    return "".join(out)


def _sheet_pin_positions(txt):
    """Map member name -> set of (x, y) positions of that name's OWN sheet-pin,
    for every ``(sheet ...)`` block in `txt`."""
    positions = {}
    for m in re.finditer(r'\t\(sheet\n.*?\n\t\)\n', txt, flags=re.S):
        block = m.group(0)
        for pin_m in re.finditer(
            r'\t\t\(pin "([^"]+)" \w+\n\t\t\t\(at ([0-9.\-]+) ([0-9.\-]+)', block
        ):
            name, x, y = pin_m.group(1), pin_m.group(2), pin_m.group(3)
            positions.setdefault(name, set()).add((x, y))
    return positions


def _boundary_tie_positions(sheets, this_file, members):
    """Map member name -> set of (float x, float y) where THIS FILE's own
    boundary-crossing hierarchical_label (tying it to ITS OWN parent, not to
    one of its children) is expected to sit.

    A file with no components of its own but with children (e.g.
    ComputeSetup: zero components, forwards straight into MCU/WiFi/
    Ethernet/etc) gets this kind of label from
    `_add_intermediate_sheet_labels` in schematic_writer.py, positioned at
    EXACTLY the same (x, y) as the pin on THIS FILE's own sheet-symbol, as
    placed by whichever OTHER file references it as a child -- unlike a
    child-tie (which sits at the child's own pin position *plus*
    `_TIE_LABEL_X_OFFSET`), a boundary tie uses that position directly, no
    offset, because `_add_intermediate_sheet_labels` reuses the parent's own
    already-offset-adjusted `SheetPin.position` as-is.

    Without recognizing this second kind of tie, a file like ComputeSetup
    gets misclassified as a real leaf for any bus that happens to cross ITS
    OWN boundary (not just its children's) -- found 2026-07-29 on ETH_DN/
    ETH_UP, the first buses in this design whose members are threaded
    through an intermediate (zero-component) sheet on their way to a
    grandparent, rather than staying within one parent-child pair."""
    member_set = set(members)
    positions = {m: set() for m in members}
    for other_name, other_txt in sheets.items():
        if other_name == this_file:
            continue
        for m in re.finditer(r'\t\(sheet\n.*?\n\t\)\n', other_txt, flags=re.S):
            block = m.group(0)
            sf = re.search(r'\(property "Sheetfile" "([^"]+)"', block)
            if not sf or sf.group(1) != this_file:
                continue
            for pin_m in re.finditer(
                r'\t\t\(pin "([^"]+)" \w+\n\t\t\t\(at ([0-9.\-]+) ([0-9.\-]+)', block
            ):
                name, x, y = pin_m.group(1), pin_m.group(2), pin_m.group(3)
                if name in member_set:
                    positions[name].add((float(x), float(y)))
    return positions


def _tie_label_positions(txt, members, sheets=None, this_file=None, bus_label=None):
    """Map member name -> set of (float x, float y) where that member's OWN
    tie label is expected to sit -- all three kinds: a child-tie (each
    sheet-pin position from `_sheet_pin_positions`, shifted by the known,
    fixed rendering offset), when `sheets`/`this_file` are given, a
    boundary-tie to this file's own parent (see `_boundary_tie_positions`),
    and, when `bus_label` is given, the repeat/repair-run fallback for a
    child sheet-pin that's already collapsed to `bus_label` (see
    `_collapsed_pin_positions`).

    The `bus_label` fallback matters beyond `_parent_surgery` (which already
    merges it in separately): any caller checking whether a stale, scattered
    per-member tie label (leftover historical drift, or a manual edit gone
    half-way) is a genuine tie -- not just `_parent_surgery`'s own repair --
    needs it too, or it misreads that stale tie as a real, non-tie
    occurrence. Found 2026-08-02 (wayfinder #55): `inject_buses`' own
    leaf-vs-parent classification hit exactly this gap on a true root tying
    two sibling children, where ONE child's tie label had reverted to
    per-member form (a corrupted-history scenario) while the OTHER child's
    tie was still correctly collapsed -- the surviving collapsed tie kept
    `_present()` true (so the root was never treated as a fresh, first-time
    injection), while the corrupted child's scattered labels, unrecognized
    as ties without this fallback, were miscounted as real per-pin
    occurrences, misclassifying the root as a leaf and drawing a bogus bus
    graphic directly on it."""
    pin_positions = _sheet_pin_positions(txt)
    out = {}
    for name in members:
        out[name] = {
            (float(x) + _TIE_LABEL_X_OFFSET, float(y))
            for x, y in pin_positions.get(name, set())
        }
    if sheets is not None and this_file is not None:
        boundary = _boundary_tie_positions(sheets, this_file, members)
        for name in members:
            out[name] |= boundary.get(name, set())
    if bus_label is not None:
        collapsed = _collapsed_pin_positions(txt, members, bus_label)
        for name in members:
            out[name] |= collapsed.get(name, set())
    return out


def _label_occurrences(txt, member):
    """All (x, y, is_hierarchical) for every label/hierarchical_label instance
    of `member` in `txt`. x, y are the exact strings found in the file (never
    reconstructed via arithmetic), so callers can use them directly to build
    a regex that is guaranteed to match this same occurrence."""
    out = []
    for m in re.finditer(
        r'\((label|hierarchical_label) "' + re.escape(member) + r'"\n'
        r'(?:\t\t\(shape [^\)]*\)\n)?'
        r'\t\t\(at ([0-9.\-]+) ([0-9.\-]+)',
        txt,
    ):
        kind, x, y = m.group(1), m.group(2), m.group(3)
        out.append((x, y, kind == "hierarchical_label"))
    return out


def _real_label_occurrences(txt, member, tie_label_positions):
    """Occurrences of `member` that are genuine component connections, not a
    parent-side tie to a child's own sheet pin. Comparison is by tolerant
    float distance (not exact string/tuple equality), since a tie label's
    position is only ever known up to the fixed offset arithmetic in
    `_tie_label_positions`, not as a literal string to match against."""
    ties = tie_label_positions.get(member, set())
    out = []
    for x, y, is_hier in _label_occurrences(txt, member):
        xf, yf = float(x), float(y)
        if any(_close(xf, tx) and _close(yf, ty) for tx, ty in ties):
            continue
        out.append((x, y, is_hier))
    return out


def _at_literal_position(kind, member, x, y):
    """Regex matching one label/hierarchical_label occurrence at the exact,
    literal (x, y) string given -- `x`/`y` must come directly from a real
    match (see _label_occurrences), never be reconstructed via arithmetic,
    so this is guaranteed to match only that specific occurrence."""
    return (
        r'\(' + kind + r' "' + re.escape(member) + r'"\n'
        r'(?:\t\t\(shape [^\)]*\)\n)?'
        r'\t\t\(at ' + re.escape(x) + r' ' + re.escape(y) + r'[^\n]*\)'
    )


def _tie_element_pattern(label_kind, member, x, y):
    """Like `_at_literal_position`, but anchored on the leading tab and
    including the trailing newline -- matches a whole top-level tie-label
    element cleanly enough to delete it outright, not just rename text
    inside it. `x`/`y` are literal strings from a real match, never
    reconstructed. Shared by `_parent_surgery` (child-sheet-pin ties) and
    `_boundary_surgery` (a file's own boundary-crossing tie to its parent)
    since both collapse a tie label the same way."""
    return (
        r'\t\(' + label_kind + r' "' + re.escape(member) + r'"\n'
        r'(?:\t\t\(shape [^\)]*\)\n)?'
        r'\t\t\(at ' + re.escape(x) + r' ' + re.escape(y) + r'[^\n]*\)\n'
    )


# Matches the rest of a label/hierarchical_label element after its
# `(at ...)` line -- `(effects ...)`, `(uuid ...)`, and the closing paren --
# so a delete using `_tie_element_pattern` + this removes the WHOLE element,
# not just its opening tag through `(at ...)`. Found 2026-07-29: a delete
# using `_tie_element_pattern` alone leaves this tail behind as orphaned,
# unbalanced text (`(effects ...)`/`(uuid ...)` fragments with no opening
# tag), which fails to load in KiCad at all.
_TIE_ELEMENT_TAIL = r'(?:\t\t[^\n]*\n)*\t\)\n'


def _demote(txt, member, positions):
    """Convert a member ``hierarchical_label`` into a plain ``label`` (it now
    travels inside the bus, not as its own hierarchical net) -- ONLY at the
    given `positions` (this leaf's own real, non-tie occurrences; see
    `_real_label_occurrences`), never by name alone. A file can carry a
    *different*, unrelated occurrence of this same positional member name
    elsewhere -- a parent-side tie to one of its own children -- and a
    name-only substitution would demote that too, even though its
    hierarchical/local status is a separate, independent decision (whether
    the net crosses *this* file's own boundary, not this leaf's).

    Only the opening tag and `(shape ...)` line change; the `(at ...)` line
    (captured verbatim, never reconstructed) and everything after are left
    exactly as they were -- this never touches position, only type."""
    for x, y in positions:
        pattern = (
            r'\t\(hierarchical_label "' + re.escape(member) + r'"\n'
            r'\t\t\(shape [^\)]*\)\n'
            r'(\t\t\(at ' + re.escape(x) + r' ' + re.escape(y) + r'[^\n]*\)\n)'
        )
        txt = re.sub(
            pattern,
            lambda m, mem=member: f'\t(label "{mem}"\n' + m.group(1),
            txt, count=1,
        )
    return txt


def _retext_pin_labels(txt, positional, elaborated, positions):
    """Rename a member's per-pin labels from the positional bus name (``SPI_0``) to
    the elaborated alias (``SPI_SCK``), so components read by name -- ONLY at the
    given `positions` (this leaf's own real, non-tie occurrences; see
    `_real_label_occurrences`), never a blind, name-wide substitution.

    A file can be this bus's genuine parent (or another bus's) at the same
    time as being a real leaf for THIS bus -- e.g. MCU is both TPM_I2C's
    parent (of TPM) and a real TPM_I2C leaf itself (the STM32's own SCL/SDA
    pins). A name-only substitution would also rename the parent-side tie
    label sitting at TPM's own sheet-pin position (same positional name,
    different point in the same file) to the elaborated name -- which then
    can no longer be found by `_parent_surgery`'s own position-matched
    lookup for the tie it's expecting to collapse, silently leaving that
    collapsed sheet pin with nothing feeding it. Found 2026-07-29 doing the
    same fix for TPM_SPI/TPM_I2C sharing MCU as their real leaf AND parent
    simultaneously (`bus_to_net_conflict`: a leftover "TPM_I2C_SCL" label
    landing on top of the unrelated, already-collapsed "TPM_SPI_[0..3]"
    sheet pin once `fix_sheet_symbol_sizes` repositioned things afterward).

    Run *before* the bus block is spliced, so the only labels currently at
    `positions` are still the original positional name."""
    for x, y in positions:
        for kind in ("label", "hierarchical_label"):
            txt = re.sub(
                r'(' + _at_literal_position(kind, positional, x, y) + r')',
                lambda m: re.sub(
                    r'"' + re.escape(positional) + r'"', f'"{elaborated}"', m.group(1), count=1
                ),
                txt, count=1,
            )
    return txt


def _sheet_pin_names(txt):
    """All ``(pin "NAME" ...)`` names declared inside any ``(sheet ...)`` block
    in `txt` -- i.e. every sheet-symbol pin this file's own child sheets expose,
    across all of this file's children combined."""
    names = set()
    for m in re.finditer(r'\t\(sheet\n.*?\n\t\)\n', txt, flags=re.S):
        names.update(re.findall(r'\t\t\(pin "([^"]+)"', m.group(0)))
    return names


def _bus_parents(sheets, members, bus_label=None):
    """Files that are a genuine parent OF THIS BUS: ones with a child sheet
    symbol exposing at least two of this bus's own members as pins.

    "Is this file a parent" is bus-relative, not a fixed property of the
    file -- see the module docstring. A single global "any file with any
    (sheet ...) block" set would conflate a file's unrelated parent role
    (e.g. MCU, parent of DRAM/TPM) with this specific bus's own parent
    relationship.

    Also matches when `bus_label` is given and a child sheet's pin is
    ALREADY the collapsed vector name rather than individual members -- a
    repeat run over a project a previous run (or manual edit) already
    collapsed. Without this, a file that's a genuine parent but has nothing
    left to do on its child-pin side (already collapsed) would be excluded
    entirely, silently skipping any stale, leftover per-member tie labels
    still needing repair -- see `_collapsed_pin_positions`, found 2026-07-29
    via `test_bus_reinjection_after_historical_root_corruption_is_repaired`."""
    member_set = set(members)
    result = set()
    for n, t in sheets.items():
        pin_names = _sheet_pin_names(t)
        if len(pin_names & member_set) >= 2:
            result.add(n)
        elif bus_label is not None and bus_label in pin_names:
            result.add(n)
    return result


def _collapsed_pin_positions(txt, members, bus_label):
    """Fallback for `_parent_surgery`'s tie-label lookup when a child's
    sheet pin is ALREADY collapsed to `bus_label` (repeat/repair run over a
    project a previous run already processed): `_sheet_pin_positions` can no
    longer find each member's own SheetPin (only the survivor, now named
    `bus_label`, remains) -- so any leftover, stale per-member tie label
    (historical drift, or a manual edit gone half-way) has no position to
    match against and would never get cleaned up otherwise.

    Anchored on `bus_label`'s own SheetPin entry inside the `(sheet ...)`
    block (`_sheet_pin_positions`), not its tie label: a true root ties
    *each* sibling child separately, so when a file has more than one child
    exposing this bus, there are multiple, independently-positioned
    `bus_label`-named tie-label occurrences (one per child) -- if only ONE
    of them is the stale/corrupted one, its own sibling's still-correct tie
    label is a different, unrelated point and can't stand in for it.
    Each child's own SheetPin entry, however, is untouched by any of this
    (`_parent_surgery`'s `fix_block` only ever renames the survivor's pin,
    never moves it) and there is exactly one per child, so it's a stable,
    always-present, per-child anchor.

    From that anchor, each member's expected position is the fixed 2.54mm
    (100mil) per-member Y pitch `_add_subcircuit_sheets` lays sibling
    SheetPins out with (schematic_writer.py, pin_spacing=2.54; X constant
    across members, only Y advances) -- tried at BOTH candidate X offsets,
    since a mid-level parent's tie label sits at the pin's raw, unshifted
    edge (`_TIE_LABEL_X_OFFSET` away from the pin's own stored, shifted
    position) while a true root's tie is coincident with its own pin
    (zero offset) -- see `_parent_surgery`'s docstring for both
    conventions. Trying both is safe: `_tie_occurrences`' own tolerant
    position match only ever finds a real occurrence if one genuinely
    exists there, so the offset that doesn't apply here simply never
    matches anything."""
    out = {}
    for x, y in _sheet_pin_positions(txt).get(bus_label, set()):
        bx, by = float(x), float(y)
        for idx, mem in enumerate(members):
            for candidate_x in (bx, bx + _TIE_LABEL_X_OFFSET):
                out.setdefault(mem, set()).add((candidate_x, by + idx * 2.54))
    return out


def _parent_surgery(txt, members, bus_label):
    """On the parent sheet, collapse the per-member sheet pins on each child
    symbol into one bus pin, and the per-member tie labels into one bus label.

    Tried leaving the outside tie labels individually scalar-named (treating
    a child sheet purely as "just another component" whose pins connect by
    ordinary scalar name matching, relying on KiCad to resolve a vector
    sheet-pin against them natively) on 2026-07-29 -- measurably worse (168
    -> 681 ERC violations on the acquisition board): KiCad's ERC does
    require the outside tie to also be collapsed to the vector name for a
    vector sheet-pin to resolve its individual members at all. Reverted.

    A parent ties its children's sheet pins together one of two ways, depending
    on whether it is itself a child further up the hierarchy:
    * a mid-level parent uses ``hierarchical_label``s (it forwards the net to
      *its* parent);
    * a true root sheet (no parent of its own) uses plain coincident-point
      ``label``s instead -- circuit-synth's normal same-sheet tie mechanism.

    Both must be collapsed the same way (rename the first member's tie to the
    bus name, drop the rest -- their sheet pins are gone once collapsed, so an
    un-collapsed tie label would otherwise dangle, and the survivor would carry
    a scalar name that doesn't match the collapsed bus-vector pin, failing
    ERC's bus-membership check).

    BOTH the rename of the "first" member's tie (to the bus name) and the
    deletion of the "rest" members' ties are POSITION-matched (only the
    occurrence actually sitting at that member's own tie position, found via
    tolerant float comparison against `_tie_label_positions`, never a blind
    name-wide substitution). This same file may independently also be a real
    leaf for this bus (its own components, not just its child's sheet pin,
    connected to these same member names), and a name-only rename/delete
    would corrupt or destroy that unrelated, perfectly good connection along
    with the tie. Found 2026-07-28: MCU is FMC_D's genuine parent (of DRAM)
    AND a real FMC_D leaf itself (the STM32's own DQ pins) in the very same
    file -- a name-only rename of the first member ("FMC_D0") relabelled
    U1's own PD14 pin to the bus's vector name instead of leaving it alone."""
    tie_label_positions = _tie_label_positions(txt, members, bus_label=bus_label)
    first, rest = members[0], members[1:]

    def fix_block(m):
        b = m.group(0)
        b = re.sub(r'\(pin "' + re.escape(first) + r'" ', f'(pin "{bus_label}" ', b)
        for mem in rest:
            b = re.sub(r'\t\t\(pin "' + re.escape(mem) + r'"[^\n]*\n(?:\t\t\t[^\n]*\n)*\t\t\)\n', '', b)
        return b

    txt = re.sub(r'\t\(sheet\n.*?\n\t\)\n', fix_block, txt, flags=re.S)

    def _tie_occurrences(member):
        ties = tie_label_positions.get(member, set())
        return [
            (x, y, is_hier)
            for x, y, is_hier in _label_occurrences(txt, member)
            if any(_close(float(x), tx) and _close(float(y), ty) for tx, ty in ties)
        ]

    for label_kind, is_hier_kind in (("hierarchical_label", True), ("label", False)):
        for x, y, is_hier in _tie_occurrences(first):
            if is_hier != is_hier_kind:
                continue
            txt = re.sub(
                r'(' + _tie_element_pattern(label_kind, first, x, y) + r')',
                lambda m: re.sub(
                    r'"' + re.escape(first) + r'"', f'"{bus_label}"', m.group(1), count=1
                ),
                txt,
                count=1,
            )
        for mem in rest:
            for x, y, is_hier in _tie_occurrences(mem):
                if is_hier != is_hier_kind:
                    continue
                txt = re.sub(
                    _tie_element_pattern(label_kind, mem, x, y) + _TIE_ELEMENT_TAIL,
                    '', txt, count=1,
                )
    return txt


def _boundary_files(sheets, members):
    """Files that are THIS BUS's own boundary-tie carrier: a zero-component
    intermediate sheet (e.g. ComputeSetup) whose own sheet-symbol -- as
    exposed by whichever OTHER file references it as a child -- carries at
    least two of this bus's own members as pins.

    Mirrors `_bus_parents`, but returns the *referenced* (Sheetfile) name
    instead of the file doing the referencing: `_bus_parents` finds a bus's
    parent (root, here) by which file's own child-sheet block exposes the
    members; this instead finds the *child* being exposed (ComputeSetup),
    since that's the file whose own boundary-crossing hierarchical_labels
    (from `_add_intermediate_sheet_labels`) need collapsing to match the
    vector pin its parent already collapsed for it."""
    member_set = set(members)
    result = set()
    for txt in sheets.values():
        for m in re.finditer(r'\t\(sheet\n.*?\n\t\)\n', txt, flags=re.S):
            block = m.group(0)
            sf = re.search(r'\(property "Sheetfile" "([^"]+)"', block)
            if not sf:
                continue
            exposed = set(re.findall(r'\t\t\(pin "([^"]+)"', block)) & member_set
            if len(exposed) >= 2:
                result.add(sf.group(1))
    return result


def _boundary_surgery(txt, members, bus_label, sheets, this_file):
    """Collapse `this_file`'s OWN boundary-crossing hierarchical_labels --
    its tie UP to ITS OWN parent, from `_add_intermediate_sheet_labels` in
    schematic_writer.py -- into one bus-vector-named hierarchical_label,
    the same way `_parent_surgery` collapses a CHILD's sheet-pin tie.

    `_add_intermediate_sheet_labels` predates Bus objects and emits one
    individually-named hierarchical_label per member for a zero-component
    sheet's own forward-to-parent tie. Nothing collapsed these before: they
    aren't a `(sheet ...)` block's own pin (so `_parent_surgery` never sees
    them), and they aren't a real, non-tie occurrence either (excluded by
    `_boundary_tie_positions`/`_real_label_occurrences`). Meanwhile this
    file's own parent DOES collapse ITS sheet-symbol pin for this file into
    the vector name (`_parent_surgery`, driven by `_bus_parents`) -- leaving
    the parent's collapsed vector pin with nothing correctly named to
    resolve against inside this file, so ERC can't match any individual
    member across the boundary (`label_dangling`, one per member).

    Confirmed 2026-07-29: user hand-collapsed ComputeSetup's own six
    `hierarchical_label "ADDR_DRV0".."ADDR_DRV5"` entries down to one
    `"ADDR_DRV[0..5]"`, which eliminated all 12 label_dangling violations for
    that bus -- this function reproduces that edit generically, for any bus
    threaded through an intermediate (zero-component) sheet on its way to a
    grandparent (this design's other instance: ETH_DN/ETH_UP crossing
    ComputeSetup on their way to Stack)."""
    tie_positions = _boundary_tie_positions(sheets, this_file, members)
    first, rest = members[0], members[1:]

    def _tie_occurrences(member):
        ties = tie_positions.get(member, set())
        return [
            (x, y, is_hier)
            for x, y, is_hier in _label_occurrences(txt, member)
            if any(_close(float(x), tx) and _close(float(y), ty) for tx, ty in ties)
        ]

    for label_kind, is_hier_kind in (("hierarchical_label", True), ("label", False)):
        for x, y, is_hier in _tie_occurrences(first):
            if is_hier != is_hier_kind:
                continue
            txt = re.sub(
                r'(' + _tie_element_pattern(label_kind, first, x, y) + r')',
                lambda m: re.sub(
                    r'"' + re.escape(first) + r'"', f'"{bus_label}"', m.group(1), count=1
                ),
                txt, count=1,
            )
        for mem in rest:
            for x, y, is_hier in _tie_occurrences(mem):
                if is_hier != is_hier_kind:
                    continue
                txt = re.sub(
                    _tie_element_pattern(label_kind, mem, x, y) + _TIE_ELEMENT_TAIL,
                    '', txt, count=1,
                )
    return txt


def _splice(txt, block):
    idx = txt.find("\t(sheet_instances")
    if idx < 0:
        idx = txt.rstrip().rfind(")")
    return txt[:idx] + block + txt[idx:]


def inject_buses(project_dir, buses):
    """Draw each declared bus, converting cross-sheet buses to real hierarchical
    buses. Returns [(bus_label, sheet_filename), ...]."""
    project_dir = Path(project_dir)
    sheets = {p.name: p.read_text() for p in sorted(project_dir.glob("*.kicad_sch"))}
    orig = dict(sheets)  # detection on the unmodified text (order-independent)

    cur_y = {n: _snap(50.0) for n in sheets}
    bus_x = {}
    for n, t in orig.items():
        xs = [float(x) for x in re.findall(r"\(at ([0-9.\-]+) [0-9.\-]+", t)]
        bus_x[n] = _snap((max(xs) + 15.0) if xs else 50.0)

    injected = []
    for bus in buses:
        members = bus.member_names
        if len(members) < 2:
            continue
        aliased = getattr(bus, "kind", "vector") == "aliased"
        alias = bus.alias_names if aliased else None

        # A file is a leaf for THIS bus wherever it has real (non-tie)
        # component connections to at least two members -- independent of
        # whether that same file is also this bus's parent (see module
        # docstring). Each leaf's own head-label hierarchical/local choice
        # reuses whatever type its own real occurrence there already has,
        # exactly like an ordinary net label -- never a single global flag
        # for the whole bus.
        candidate_names = set(members) | (set(alias) if aliased else set())

        # Per-sheet override for `leaf_hier`, populated only when repairing
        # an orphaned graphic (see `_bus_graphic_intact`/`_orphaned_head_kind`).
        # The normal derivation below (`any(is_hier for ...)` over each
        # member's *current* real occurrence) is only valid the first time a
        # bus is drawn: `_demote()` permanently converts a hierarchical
        # member occurrence to a plain one as part of that first drawing, so
        # re-deriving `leaf_hier` from a member's real occurrence on a
        # repair pass would silently read the post-demotion (plain) state
        # and flip a hierarchical bus tie to a local one.
        forced_hier = {}

        def _leaf_status(own_text):
            """Compute (present, real_by_index, leaf_hier) for one sheet's
            `own_text` against the (never-mutated) `orig` dict for
            cross-sheet tie lookups.

            For an ALIASED bus, a leaf's genuine real occurrence may already
            be retexted to the elaborated alias name rather than the
            positional member name -- true of every pass after the very
            first injection, since `_retext_pin_labels` permanently renames
            it in place. Searching only the positional name (as a plain/
            vector bus always must, since it has no alias) misses that
            occurrence entirely on such a pass. This matters specifically
            on an orphan-repair pass (`_bus_graphic_intact` found a
            partially-destroyed graphic, see `_strip_orphaned_bus_graphic`):
            once the orphaned stub's own positional-named remnant is
            correctly stripped (wayfinder #57 fix), the ONLY remaining
            occurrence of the positional name in the file may be gone
            entirely, and skipping the alias-name search would then
            misclassify this leaf as having zero real connections --
            silently dropping the whole tap (and its already-correct,
            already-retexted component-pin connection) instead of
            redrawing it. Safe to search unconditionally (not just during
            repair): on a fresh/first pass the alias text doesn't exist yet
            anywhere, so this is a harmless no-op extra search."""
            tie_label_positions = _tie_label_positions(own_text, members, sheets=orig, this_file=n, bus_label=bus.label)
            present = []
            real_by_index = {}
            leaf_hier = False
            for i, m in enumerate(members):
                real = _real_label_occurrences(own_text, m, tie_label_positions)
                if aliased:
                    real = real + _real_label_occurrences(own_text, alias[i], tie_label_positions)
                if real:
                    present.append(i)
                    real_by_index[i] = [(x, y) for x, y, _ in real]
                    leaf_hier = leaf_hier or any(is_hier for _, _, is_hier in real)
            return present, real_by_index, leaf_hier

        draw_on = []
        for n in sheets:
            # Determine leaf status from the CURRENT (possibly still-orphaned)
            # text first. A parent-only / tie-only sheet (e.g. a true root
            # that ties sibling children via coincident-point labels, never
            # drawing its own `(bus ...)` graphic at all -- see
            # `_parent_surgery`'s docstring) has zero real occurrences here
            # and must never be run through the orphan check/repair below:
            # `_present()` is True for it too (its tie labels share the bus's
            # own name), but it never had a `(bus ...)` graphic to begin
            # with, so `_bus_graphic_intact` would always read it as
            # "orphaned" and incorrectly strip its legitimate tie labels.
            #
            # Deliberately never writes back into the shared `orig[n]` entry
            # here (only a local `own_text` view is used for this sheet's
            # own leaf-status recomputation after a strip): `orig` is passed
            # as the `sheets=` cross-reference argument to
            # `_tie_label_positions` for every OTHER sheet in this same
            # per-bus loop too, and every sheet is processed against the
            # same `orig` snapshot in this loop regardless of iteration
            # order. Mutating `orig[n]` here previously leaked this sheet's
            # strip into a *different* sheet's (e.g. a true root's)
            # cross-reference lookup later in the very same loop,
            # corrupting its own, unrelated tie-vs-real classification.
            present, real_by_index, leaf_hier = _leaf_status(orig[n])

            if len(present) < 2:
                continue  # not a real leaf for this bus on this sheet

            if _present(orig[n], bus.label):
                intact = _bus_graphic_intact(orig[n], bus.label)
                if intact:
                    continue  # already injected on a previous run -- idempotent no-op
                # `intact is False`: the tie label survived (e.g. a
                # kicad-sch-api load/save round-trip during incremental
                # sync -- see `_bus_graphic_intact`) but the `bus`/
                # `bus_entry` graphic it anchors did not. Capture the
                # orphaned head's own hierarchical/local type before
                # stripping (its member occurrences are already demoted and
                # can no longer be trusted to reconstruct it -- see above),
                # then strip the orphaned remnants from `sheets[n]` (the
                # actual output buffer) and recompute this sheet's own
                # `present`/`real_by_index`/`leaf_hier` from a *local*
                # stripped view so it's treated as not-yet-injected below
                # and gets a complete, non-duplicated graphic redrawn --
                # without ever touching the shared `orig` dict other
                # sheets' own cross-references read from.
                orphaned_hier = _orphaned_head_kind(orig[n], bus.label)
                if orphaned_hier is not None:
                    forced_hier[n] = orphaned_hier
                sheets[n] = _strip_orphaned_bus_graphic(sheets[n], bus.label, candidate_names)
                stripped_own_text = _strip_orphaned_bus_graphic(orig[n], bus.label, candidate_names)
                present, real_by_index, leaf_hier = _leaf_status(stripped_own_text)

            if n in forced_hier:
                leaf_hier = forced_hier[n]
            if len(present) >= 2:
                draw_on.append((n, present, leaf_hier, real_by_index))

        for n, present, leaf_hier, real_by_index in draw_on:
            t = sheets[n]
            taps = [([members[i], alias[i]] if aliased else [members[i]]) for i in present]
            block = _bus_block(bus.label, taps, bus_x[n], cur_y[n], bus_hier=leaf_hier)
            cur_y[n] = _snap(cur_y[n] + len(present) * 2.54 + 12.0)
            if leaf_hier:
                for i in present:
                    t = _demote(t, members[i], real_by_index[i])
            if aliased:
                # Elaborated name at each component pin; the numeric positional name
                # stays only on the bus stub (added by the tap) as the vector tie.
                # Only at THIS leaf's own real occurrence positions -- never a
                # parent-side tie to a child that happens to share the same
                # positional name elsewhere in this same file (see
                # _retext_pin_labels's own docstring for why that matters).
                for i in present:
                    t = _retext_pin_labels(t, members[i], alias[i], real_by_index[i])
            sheets[n] = _splice(t, block)
            injected.append((bus.label, n))

        # Whichever files are this bus's genuine parent(s) need their child
        # sheet symbols' per-member pins collapsed into one bus pin, whenever
        # at least one of them exposes >=2 members -- independent of the
        # per-leaf hierarchical/local choices above (a parent-side sheet-pin
        # collapse is needed purely because the child exposes multiple
        # members as pins, regardless of how any single leaf's own head label
        # was typed).
        for pn in _bus_parents(orig, members, bus.label):
            sheets[pn] = _parent_surgery(sheets[pn], members, bus.label)

        # Whichever file(s) are themselves this bus's own boundary-tie
        # carrier (an intermediate, zero-component sheet forwarding the bus
        # to ITS parent) need THEIR OWN outgoing tie collapsed the same way
        # -- otherwise the parent's collapsed vector pin has no matching
        # vector-named tie to resolve against inside this file at all.
        for bn in _boundary_files(orig, members):
            if bn in sheets:
                sheets[bn] = _boundary_surgery(sheets[bn], members, bus.label, orig, bn)

    for n, t in sheets.items():
        (project_dir / n).write_text(t)
    return injected
