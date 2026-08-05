"""
Net sheet-boundary-crossing analysis, shared by clean generation and
incremental sync.

This mirrors `schematic_writer.py`'s `SchematicWriter._subtree_net_names` /
`_net_crosses_boundary` (the logic that decides, for a given circuit's
subtree, which nets need a sheet-symbol pin exposing them to the rest of the
design) as a standalone set of functions operating on a plain
`{circuit_name: Circuit}` dict, instead of methods bundled onto
`SchematicWriter` (which also carries a lot of generation-only state --
placement, `self.circuit`, `self.schematic` -- that incremental sync has no
equivalent of).

Deliberately duplicated rather than imported from schematic_writer.py: that
module's version is a proven, heavily-exercised part of the clean-generation
path, and importing from it would couple incremental sync's correctness to
a module that also does a lot of unrelated (placement/writing) work. If
schematic_writer.py's boundary-crossing rule ever changes, mirror the change
here too -- the docstrings below restate the rationale so the two can be
compared directly.

Used by:
  - `schematic/sheet_pin_sync.py` (wayfinder #65) -- reconciles missing
    sheet-symbol pins on an EXISTING KiCad project during incremental sync.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Set


def own_net_names(subcircuits: Dict[str, Any], circuit_name: str) -> Set[str]:
    """Net names directly used by this circuit's own components (no recursion)."""
    circ = subcircuits.get(circuit_name)
    if circ is None:
        return set()
    nets = circ.nets.values() if isinstance(circ.nets, dict) else circ.nets
    return {n.name for n in nets}


def subtree_circuit_names(
    subcircuits: Dict[str, Any],
    circuit_name: str,
    _cache: Optional[Dict[str, Set[str]]] = None,
) -> Set[str]:
    """
    Set of circuit names in `circuit_name`'s own subtree: itself plus every
    descendant, transitively (via each circuit's own `child_instances`).

    `_cache` is an optional caller-supplied memo dict, shared across calls
    within one reconciliation pass -- the subcircuit dict is static for the
    duration of one sync run, exactly like schematic_writer.py's own
    per-instance cache.
    """
    if _cache is None:
        _cache = {}
    if circuit_name in _cache:
        return _cache[circuit_name]
    circ = subcircuits.get(circuit_name)
    if circ is None:
        return set()
    names = {circuit_name}
    _cache[circuit_name] = names  # placeholder guards against pathological cycles
    for child_info in getattr(circ, "child_instances", []) or []:
        names |= subtree_circuit_names(subcircuits, child_info.get("sub_name"), _cache)
    _cache[circuit_name] = names
    return names


def subtree_net_names(subcircuits: Dict[str, Any], circuit_name: str) -> Set[str]:
    """
    Net names used anywhere in `circuit_name`'s own subtree: by the circuit
    itself, or transitively by any of its descendants.

    A wrapper circuit that only forwards its parameters to a child (no
    direct component connections of its own) never registers those nets
    into its own `.nets` -- this recursive closure is what makes such a net
    still visible at the wrapper's level. See schematic_writer.py's
    identically-named method for the fuller rationale (this project's
    "pass-through nets" fix, 2026-07-29).
    """
    names: Set[str] = set()
    cache: Dict[str, Set[str]] = {}
    for name in subtree_circuit_names(subcircuits, circuit_name, cache):
        names |= own_net_names(subcircuits, name)
    return names


def net_owner_circuits(subcircuits: Dict[str, Any]) -> Dict[str, Set[str]]:
    """Reverse index: net name -> set of circuit names whose OWN components directly use it."""
    owners: Dict[str, Set[str]] = {}
    for name in subcircuits:
        for net_name in own_net_names(subcircuits, name):
            owners.setdefault(net_name, set()).add(name)
    return owners


def net_crosses_boundary(
    subcircuits: Dict[str, Any],
    circuit_name: str,
    net_name: str,
    _owners: Optional[Dict[str, Set[str]]] = None,
) -> bool:
    """
    Does `net_name` cross `circuit_name`'s own sheet boundary -- i.e. is it
    used both inside this circuit's subtree (by itself or a descendant) AND
    by at least one circuit OUTSIDE that subtree (its parent, a sibling,
    anywhere else in the design)?

    For the root circuit this is always False by construction: the root's
    own subtree always covers the entire project, so there is never a
    circuit "outside" it -- a root-level hierarchical_label is therefore
    never valid (KiCad has no parent sheet symbol for it to match), which is
    why a caller synthesizing a tie label on the root's own canvas must
    always use a plain LOCAL label there, never HIERARCHICAL, regardless of
    what this function returns.

    `_owners` lets a caller reuse one precomputed `net_owner_circuits()`
    result across many calls in the same reconciliation pass instead of
    recomputing it per net.
    """
    subtree = subtree_circuit_names(subcircuits, circuit_name)
    owners = (_owners if _owners is not None else net_owner_circuits(subcircuits)).get(
        net_name, set()
    )
    if not (owners & subtree):
        return False  # not even used inside this subtree
    return bool(owners - subtree)
