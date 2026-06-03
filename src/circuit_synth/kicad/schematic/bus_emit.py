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

Multiple buses on one sheet are stacked vertically in a single column to the
right of the existing content (no overlap). Connectivity is unchanged: the
positional label reuses the member net name and merges with the existing pin
label; the bus is a drafting overlay.

Placement is intentionally simple (one right-side column); it can run off the
page edge on very full sheets — visual only, never a short.
"""

import re
import uuid
from collections import defaultdict
from pathlib import Path

_STROKE = "(stroke (width 0) (type default))"


def _u():
    return str(uuid.uuid4())


def _label(text, x, y):
    return (f'\t(label "{text}"\n\t\t(at {x} {y} 0)\n'
            f'\t\t(effects (font (size 1.27 1.27)) (justify left bottom))\n'
            f'\t\t(uuid "{_u()}")\n\t)')


def _bus_block(bus_label, taps, bus_x, top_y, pitch=2.54, stub=20.32):
    """Draw one bus: a vertical bus wire + bus_label, and per tap a
    bus_entry + stub wire carrying its label(s) (all on the same wire)."""
    y1 = top_y + (len(taps) - 1) * pitch + 5.08
    s = [
        f'\t(bus\n\t\t(pts (xy {bus_x} {top_y - 2.54}) (xy {bus_x} {y1}))\n\t\t{_STROKE}\n\t\t(uuid "{_u()}")\n\t)',
        _label(bus_label, bus_x, top_y - 5.08),
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


def inject_buses(project_dir, buses):
    """Draw each declared bus. Returns [(bus_label, sheet_filename), ...]."""
    project_dir = Path(project_dir)

    # Plan which buses go on which sheet (a plain bus draws on its first sheet;
    # an aliased bus draws on every sheet that carries its members).
    plan = defaultdict(list)  # sheet filename -> [(bus, present_member_indices)]
    for bus in buses:
        members = bus.member_names
        if len(members) < 2:
            continue
        aliased = getattr(bus, "kind", "vector") == "aliased"
        for sch in sorted(project_dir.glob("*.kicad_sch")):
            txt = sch.read_text()
            present = [i for i, m in enumerate(members) if _present(txt, m)]
            if len(present) >= 2:
                plan[sch.name].append((bus, present))
                if not aliased:
                    break

    injected = []
    for name, items in plan.items():
        sch = project_dir / name
        txt = sch.read_text()
        xs = [float(x) for x in re.findall(r"\(at ([0-9.\-]+) [0-9.\-]+", txt)]
        bus_x = round((max(xs) + 15.0) if xs else 50.0, 2)  # right of content
        cur_y, blob = 50.0, ""
        for bus, present in items:
            aliased = getattr(bus, "kind", "vector") == "aliased"
            alias = bus.alias_names if aliased else None
            mem = bus.member_names
            taps = [([mem[i], alias[i]] if aliased else [mem[i]]) for i in present]
            blob += _bus_block(bus.label, taps, bus_x, cur_y)
            cur_y += len(present) * 2.54 + 12.0  # stack next bus below, with a gap
            injected.append((bus.label, name))
        idx = txt.find("\t(sheet_instances")
        if idx < 0:
            idx = txt.rstrip().rfind(")")
        sch.write_text(txt[:idx] + blob + txt[idx:])
    return injected
