"""Capacitor bank: a purely visual grouping directive.

A "cap bank" is a set of decoupling capacitors that are wired in parallel across
the *same two nets* (e.g. several 100nF bypass caps all on +3V3 / GND). Electrically
each cap is an independent two-terminal part; this helper only changes how the group
is *drawn* in the generated schematic.

When components are tagged with ``cap_bank(...)`` the schematic writer renders them as
a tight horizontal row joined by two shared rails (one wire per net) instead of
scattering them and connecting each pin with its own net label. The netlist is
unchanged: the rails carry the same two nets, and each rail keeps exactly one net
marker so the bank still ties into the global net by name.

This is a representation-only directive. It tags components via the ``cap_bank`` /
``cap_bank_pitch`` properties, which the writer consumes (and strips) at generation
time. It does not touch connectivity.
"""

MIL_TO_MM = 0.0254


def cap_bank(caps, name, pitch_mil=300):
    """Tag ``caps`` as one paralleled bypass bank, drawn as a tight two-rail array.

    Args:
        caps: Iterable of two-terminal capacitor :class:`Component` objects. Every cap
            must straddle the same two nets (pin 1 on the positive rail, pin 2 on the
            return rail); this is verified at generation time.
        name: Bank identifier (kept distinct from other banks on the same sheet).
        pitch_mil: Center-to-center spacing of the caps in mils (default 300 mil).

    Returns:
        The caps as a list (so the call can wrap an inline list of ``Component``s).
    """
    caps = list(caps)
    if len(caps) < 2:
        raise ValueError("cap_bank() needs at least two capacitors to form a bank")
    if not str(name).strip():
        raise ValueError("cap_bank() requires a non-empty bank name")

    pitch_mm = float(pitch_mil) * MIL_TO_MM
    for c in caps:
        # Tags travel as component properties: Component -> JSON -> loaded symbol.
        # The schematic writer reads and removes them so they do not pollute the
        # generated symbol fields.
        c.cap_bank = str(name)
        c.cap_bank_pitch = f"{pitch_mm:.4f}"
    return caps
