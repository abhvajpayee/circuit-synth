"""Resistor bank: a purely visual grouping directive for single-common-rail arrays.

A "resistor bank" is a set of resistors that all share *one* common net -- the
classic pull-up array (each line to +3V3/+12V), pull-down array (each line to GND),
or config-to-rail group (set resistors to a reference). Unlike a :func:`cap_bank`
(where both pins are shared, giving two rails), here only one pin per resistor ties
to the shared rail; the opposite pin fans out to its own distinct signal.

When resistors are tagged with ``resistor_bank(...)`` the schematic writer renders
them as a tight horizontal row whose common pins are joined by a single shared rail
(one wire, one net marker), while each fan-out pin keeps its own per-pin net label.
The netlist is unchanged: the rail carries the common net and ties into the global
net by name, and each fan-out pin still labels its own net.

This is a representation-only directive. It tags components via ``resistor_bank`` /
``resistor_bank_pitch`` / ``resistor_bank_common`` / ``resistor_bank_side``
properties, which the writer consumes (and strips) at generation time. It does not
touch connectivity.

Contrast with :func:`circuit_synth.cap_bank`: cap_bank *infers* its two rails and
warns+degrades on ambiguity; resistor_bank takes an *explicit* ``common`` net, so a
member that does not straddle it exactly once is a caller error and raises here, at
the call site, where the pin nets are already bound.
"""

MIL_TO_MM = 0.0254


def _pin_net_names(comp):
    """Return the list of net names bound to ``comp``'s pins (None for unconnected)."""
    pins = getattr(comp, "_pins", None) or {}
    names = []
    for pin in pins.values():
        net = getattr(pin, "net", None)
        names.append(getattr(net, "name", None) if net is not None else None)
    return names


def resistor_bank(resistors, name, common, pitch_mil=300, side="auto"):
    """Tag ``resistors`` as one single-common-rail bank, drawn as a tight row.

    Args:
        resistors: Iterable of two-terminal ``Device:R`` :class:`Component` objects.
            Every resistor must connect to ``common`` on exactly one pin; the other
            pin fans out to its own net.
        name: Bank identifier (kept distinct from other banks on the same sheet).
        common: The shared :class:`Net` (or a net name) that becomes the rail.
        pitch_mil: Center-to-center spacing of the resistors in mils (default 300).
        side: ``"auto"`` (default) draws the rail on the bottom for ground-like
            commons (name contains ``GND`` or ``VSS``, case-insensitive) and on top
            otherwise; ``"top"`` / ``"bottom"`` force it.

    Returns:
        The resistors as a list (so the call can wrap an inline list of components).

    Raises:
        ValueError: if fewer than two resistors, an empty name, a missing/invalid
            ``common``, a non-``Device:R`` member, or a member that does not straddle
            ``common`` on exactly one pin.
    """
    resistors = list(resistors)
    if len(resistors) < 2:
        raise ValueError("resistor_bank() needs at least two resistors to form a bank")
    if not str(name).strip():
        raise ValueError("resistor_bank() requires a non-empty bank name")

    common_name = getattr(common, "name", common)
    if not common_name or not str(common_name).strip():
        raise ValueError("resistor_bank() requires a non-empty `common` net")
    common_name = str(common_name)

    side = str(side).lower()
    if side not in ("auto", "top", "bottom"):
        raise ValueError(
            f"resistor_bank() side must be 'auto', 'top', or 'bottom', got {side!r}"
        )

    for r in resistors:
        ref = getattr(r, "reference", "<?>")
        symbol = getattr(r, "symbol", "")
        if not str(symbol).startswith("Device:R"):
            raise ValueError(
                f"resistor_bank() is Device:R only; {ref} is {symbol!r}"
            )
        nets = _pin_net_names(r)
        if len(nets) != 2:
            raise ValueError(
                f"resistor_bank(): {ref} is not a two-terminal resistor "
                f"(pins: {len(nets)})"
            )
        on_common = [n for n in nets if n == common_name]
        if len(on_common) == 0:
            raise ValueError(
                f"resistor_bank(): {ref} does not connect to common '{common_name}' "
                f"(pins on {nets}); members must each touch the rail once"
            )
        if len(on_common) == 2:
            raise ValueError(
                f"resistor_bank(): {ref} connects to common '{common_name}' on both "
                f"pins (a short); each member must touch the rail exactly once"
            )

    pitch_mm = float(pitch_mil) * MIL_TO_MM
    for r in resistors:
        # Tags travel as component properties: Component -> JSON -> loaded symbol.
        # The schematic writer reads and removes them so they do not pollute the
        # generated symbol fields.
        r.resistor_bank = str(name)
        r.resistor_bank_pitch = f"{pitch_mm:.4f}"
        r.resistor_bank_common = common_name
        r.resistor_bank_side = side
    return resistors
