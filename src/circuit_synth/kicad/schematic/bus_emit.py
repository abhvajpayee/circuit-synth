"""Inject graphical KiCad vector buses for declared :class:`Bus` objects.

circuit-synth connects by dropping one net-label per pin, so the members of a
bus appear as scattered individual labels. After the sheets are written, this
pass draws a real vector bus on whichever sheet(s) carry the members:

* **Plain** bus (``FMC_D[0..15]``) — one tap per member, on the first sheet that
  has the members.
* **Aliased** bus (``SPI_[0..3]``) — a **dual label** per tap: the positional
  member ``SPI_0`` *and* the explanatory ``SPI_MISO`` on the same stub, which
  translates the flat net onto a readable name. Drawn on **every** sheet a
  member appears, so the translation is uniform across the design.

**Hierarchical-bus conversion.** When a bus is cross-sheet — i.e. any member is
a *hierarchical* label (circuit-synth promotes sibling-shared nets to
hierarchical labels + sheet pins) — the bus is turned into a real KiCad
hierarchical bus instead of N independent hierarchical member nets:

* on each leaf sheet the bus label is emitted as a ``hierarchical_label`` and
  every member ``hierarchical_label`` is demoted to a plain ``label`` (the
  member now travels *inside* the bus);
* on the parent sheet the per-member sheet pins on each child symbol are
  collapsed into a single bus sheet pin (``SPI_[0..3]``), and the per-member
  tie ``hierarchical_label``s are collapsed into one bus tie label.

This keeps connectivity identical (verified via the exported netlist) while the
schematic reads as one bus crossing the hierarchy.

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
    head = _hier_label(bus_label, bus_x, top_y - 5.08) if bus_hier else _label(bus_label, bus_x, top_y - 5.08)
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


def _is_hier(txt, member):
    return bool(re.search(r'\(hierarchical_label "' + re.escape(member) + '"', txt))


def _demote(txt, member):
    """Convert a member ``hierarchical_label`` into a plain ``label`` (it now
    travels inside the bus, not as its own hierarchical net)."""
    return re.sub(r'\t\(hierarchical_label "' + re.escape(member) + r'"\n\t\t\(shape [^\)]*\)\n',
                  f'\t(label "{member}"\n', txt)


def _parent_surgery(txt, members, bus_label):
    """On the parent sheet, collapse the per-member sheet pins on each child
    symbol into one bus pin, and the per-member tie labels into one bus label."""
    first, rest = members[0], members[1:]

    def fix_block(m):
        b = m.group(0)
        b = re.sub(r'\(pin "' + re.escape(first) + r'" ', f'(pin "{bus_label}" ', b)
        for mem in rest:
            b = re.sub(r'\t\t\(pin "' + re.escape(mem) + r'"[^\n]*\n(?:\t\t\t[^\n]*\n)*\t\t\)\n', '', b)
        return b

    txt = re.sub(r'\t\(sheet\n.*?\n\t\)\n', fix_block, txt, flags=re.S)
    txt = re.sub(r'\(hierarchical_label "' + re.escape(first) + r'"',
                 f'(hierarchical_label "{bus_label}"', txt)
    for mem in rest:
        txt = re.sub(r'\t\(hierarchical_label "' + re.escape(mem) + r'"\n(?:\t\t[^\n]*\n)*\t\)\n', '', txt)
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
    parents = {n for n, t in orig.items() if re.search(r"\n\t\(sheet\n", t)}

    cur_y = {n: 50.0 for n in sheets}
    bus_x = {}
    for n, t in orig.items():
        xs = [float(x) for x in re.findall(r"\(at ([0-9.\-]+) [0-9.\-]+", t)]
        bus_x[n] = round((max(xs) + 15.0) if xs else 50.0, 2)

    injected = []
    for bus in buses:
        members = bus.member_names
        if len(members) < 2:
            continue
        aliased = getattr(bus, "kind", "vector") == "aliased"
        alias = bus.alias_names if aliased else None
        hierarchical = any(_is_hier(orig[n], m) for n in orig for m in members)

        # Leaf sheets carry the member component pins; the parent only ties them.
        candidates = [n for n in sheets if n not in parents] if hierarchical else sorted(sheets)
        draw_on = []
        for n in candidates:
            present = [i for i, m in enumerate(members) if _present(orig[n], m)]
            if len(present) >= 2:
                draw_on.append((n, present))
                if not hierarchical and not aliased:
                    break

        for n, present in draw_on:
            t = sheets[n]
            taps = [([members[i], alias[i]] if aliased else [members[i]]) for i in present]
            block = _bus_block(bus.label, taps, bus_x[n], cur_y[n], bus_hier=hierarchical)
            cur_y[n] += len(present) * 2.54 + 12.0
            if hierarchical:
                for i in present:
                    t = _demote(t, members[i])
            sheets[n] = _splice(t, block)
            injected.append((bus.label, n))

        if hierarchical:
            for pn in parents:
                sheets[pn] = _parent_surgery(sheets[pn], members, bus.label)

    for n, t in sheets.items():
        (project_dir / n).write_text(t)
    return injected
