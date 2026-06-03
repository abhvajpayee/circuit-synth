# FILE: src/circuit_synth/core/bus.py
"""Vector bus abstraction.

A :class:`Bus` is a named group of member :class:`Net` objects
(``PREFIX0 .. PREFIX{N-1}``). Declaring ``Bus("FMC_D", 16)`` creates 16 member
nets ``FMC_D0 .. FMC_D15`` indexable as ``bus[i]``. Members are ordinary nets
(connect with ``+=``); in addition, schematic generation draws a graphical KiCad
vector bus ``FMC_D[lo..hi]`` on the sheet where the members appear.

The bus is registered on the active circuit so generation can find it.
"""

from .decorators import get_current_circuit
from .net import Net


class Bus:
    """A vector bus of member nets ``PREFIX{start} .. PREFIX{start+width-1}``."""

    def __init__(self, name, width=None, *, start=0, members=None):
        if not name:
            raise ValueError("Bus requires a non-empty name")
        self.name = name
        self.start = int(start)

        if members is not None:
            self.members = list(members)
            self.width = len(self.members)
        else:
            if not width or width < 1:
                raise ValueError("Bus requires width >= 1 (or an explicit members list)")
            self.width = int(width)
            self.members = [Net(f"{name}{self.start + i}") for i in range(self.width)]

        circ = get_current_circuit()
        if circ is not None and hasattr(circ, "add_bus"):
            circ.add_bus(self)

    @property
    def lo(self):
        return self.start

    @property
    def hi(self):
        return self.start + self.width - 1

    @property
    def label(self):
        """KiCad vector-bus label, e.g. ``FMC_D[0..15]``."""
        return f"{self.name}[{self.lo}..{self.hi}]"

    @property
    def member_names(self):
        return [n.name for n in self.members]

    def __getitem__(self, i):
        return self.members[i]

    def __len__(self):
        return len(self.members)

    def __iter__(self):
        return iter(self.members)

    def __repr__(self):
        return f"Bus({self.label})"
