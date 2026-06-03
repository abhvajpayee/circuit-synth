# FILE: src/circuit_synth/core/bus.py
"""Vector bus abstraction (plain and aliased).

Two flavours, both drawn as a KiCad **vector** bus:

* **Plain** — ``Bus("FMC_D", 16)`` → members ``FMC_D0 .. FMC_D15``, bus
  ``FMC_D[0..15]``. Good for already-meaningful indexed signals.

* **Aliased** — ``Bus("SPI", members=["SCK", "MISO", "MOSI", "CS"])`` → positional
  member nets ``SPI_0 .. SPI_3`` and the bus ``SPI_[0..3]``, but each member also
  carries an explanatory name ``SPI_SCK``/``SPI_MISO``/… Generation emits a
  **dual label** at every tap — the positional ``SPI_0`` *and* the explanatory
  ``SPI_MISO`` on the same net — translating one to the other (``SPI_0 <-> SPI_MISO``).
  The translation is emitted on every sheet a member appears, so heterogeneous
  groups (SPI/UART/SDMMC) read clearly while still grouping into one bus.

Members are ordinary :class:`Net` objects (connect with ``+=``); indexable by
position (``bus[0]``) and, for aliased buses, by member name (``bus["MISO"]``).
The bus registers on the active circuit so generation can draw it.
"""

from .decorators import get_current_circuit
from .net import Net


class Bus:
    """A KiCad vector bus, optionally with explanatory per-member aliases."""

    def __init__(self, name, width=None, *, start=0, members=None):
        if not name:
            raise ValueError("Bus requires a non-empty name")
        self.name = name
        self.start = int(start)
        self.aliases = None  # explanatory member names, parallel to self.members

        if members is not None and all(isinstance(m, str) for m in members):
            # Aliased vector bus: `members` are explanatory member names.
            if not members:
                raise ValueError("aliased Bus requires at least one member name")
            self.kind = "aliased"
            self.aliases = list(members)
            self.width = len(members)
            self.prefix = f"{name}_"  # member nets SPI_0..SPI_n, bus SPI_[0..n]
            self.members = [Net(f"{self.prefix}{self.start + i}") for i in range(self.width)]
        elif members is not None:
            # Explicit list of Net objects.
            self.kind = "vector"
            self.prefix = name
            self.members = list(members)
            self.width = len(self.members)
        else:
            if not width or width < 1:
                raise ValueError("Bus requires width >= 1 or a members list")
            self.kind = "vector"
            self.prefix = name
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
        """KiCad vector-bus label, e.g. ``FMC_D[0..15]`` or ``SPI_[0..3]``."""
        return f"{self.prefix}[{self.lo}..{self.hi}]"

    @property
    def member_names(self):
        """Flat (positional) net names of the members."""
        return [n.name for n in self.members]

    @property
    def alias_names(self):
        """Explanatory member labels (``SPI_SCK`` …), or ``None`` for a plain bus."""
        if self.kind != "aliased":
            return None
        return [f"{self.name}_{m}" for m in self.aliases]

    @property
    def translations(self):
        """Aliased bus: list of ``(positional_net_name, explanatory_name)`` pairs.

        These are the dual labels emitted at each tap (e.g. ``SPI_0`` & ``SPI_MISO``).
        Empty for a plain vector bus.
        """
        if self.kind != "aliased":
            return []
        return list(zip(self.member_names, self.alias_names))

    def __getitem__(self, key):
        if isinstance(key, int):
            return self.members[key]
        if self.kind == "aliased" and key in self.aliases:
            return self.members[self.aliases.index(key)]
        raise KeyError(key)

    def __len__(self):
        return len(self.members)

    def __iter__(self):
        return iter(self.members)

    def __repr__(self):
        return f"Bus({self.label})"
