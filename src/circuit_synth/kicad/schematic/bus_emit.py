"""Inject graphical KiCad vector buses for declared :class:`Bus` objects.

circuit-synth connects by dropping one net-label per pin, so the members of a
bus appear as scattered individual labels. After the sheets are written, this
pass draws a real vector bus (``PREFIX[lo..hi]``) — a bus wire with one
``bus_entry`` + stub + member label per signal — on whichever sheet already
carries the member labels. Connectivity is unchanged: each tap reuses the member
net name, so KiCad merges it with the existing label at the IC pin. The bus is
purely a drafting aid.
"""

import re
import uuid
from pathlib import Path

_STROKE = "(stroke (width 0) (type default))"
# usable max X per KiCad paper size (mm), leaving a margin for the border
_PAPER_W = {"A4": 284.0, "A3": 407.0, "A2": 581.0, "A1": 828.0, "A0": 1176.0}


def _u():
    return str(uuid.uuid4())


def _bus_sexpr(label, lo, hi, members, bus_x, top_y, pitch=2.54, stub=10.16):
    y0, y1 = top_y, top_y + (len(members) - 1) * pitch + 5.08
    s = [
        f'\t(bus\n\t\t(pts (xy {bus_x} {y0 - 2.54}) (xy {bus_x} {y1}))\n\t\t{_STROKE}\n\t\t(uuid "{_u()}")\n\t)',
        f'\t(label "{label}"\n\t\t(at {bus_x} {y0 - 5.08} 0)\n\t\t(effects (font (size 1.27 1.27)) (justify left bottom))\n\t\t(uuid "{_u()}")\n\t)',
    ]
    for k, name in enumerate(members):
        y = y0 + k * pitch
        ex, ey = bus_x + 2.54, y + 2.54
        s.append(f'\t(bus_entry\n\t\t(at {bus_x} {y})\n\t\t(size 2.54 2.54)\n\t\t{_STROKE}\n\t\t(uuid "{_u()}")\n\t)')
        s.append(f'\t(wire\n\t\t(pts (xy {ex} {ey}) (xy {ex + stub} {ey}))\n\t\t{_STROKE}\n\t\t(uuid "{_u()}")\n\t)')
        s.append(f'\t(label "{name}"\n\t\t(at {ex + stub} {ey} 0)\n\t\t(effects (font (size 1.27 1.27)) (justify left bottom))\n\t\t(uuid "{_u()}")\n\t)')
    return "\n".join(s) + "\n"


def inject_buses(project_dir, buses):
    """For each bus, draw it on the first sheet that carries >=2 of its members.

    Returns a list of (bus_label, sheet_filename) for what was injected.
    """
    project_dir = Path(project_dir)
    injected = []
    for bus in buses:
        members = list(bus.member_names)
        if len(members) < 2:
            continue
        for sch in sorted(project_dir.glob("*.kicad_sch")):
            txt = sch.read_text()
            present = sum(1 for m in members if f'(label "{m}"' in txt)
            if present < 2:
                continue
            paper = re.search(r'\(paper "([^"]+)"', txt)
            max_w = _PAPER_W.get(paper.group(1) if paper else "A3", 407.0)
            xs = [float(x) for x in re.findall(r"\(at ([0-9.\-]+) [0-9.\-]+", txt)]
            bus_x = min((max(xs) + 15.0) if xs else 50.0, max_w - 15.0)
            blob = _bus_sexpr(bus.label, bus.lo, bus.hi, members, round(bus_x, 2), 50.0)
            idx = txt.find("\t(sheet_instances")
            if idx < 0:
                idx = txt.rstrip().rfind(")")
            sch.write_text(txt[:idx] + blob + txt[idx:])
            injected.append((bus.label, sch.name))
            break
    return injected
