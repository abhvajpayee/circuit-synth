"""
Connectivity-based reference preallocation for incremental KiCad sync.

Background / design rationale
------------------------------
See ``docs/research/kicad-incremental-sync-identity-stability.md`` (in the
consuming project) and wayfinder issue #58 for the full history. Summary:

Reference-designator auto-numbering (:meth:`Circuit.finalize_references`) is
a single global counter per prefix, recomputed from scratch every time the
Python source is executed, in sheet-call order. Inserting a new component
into a sheet that is traversed *before* another, unrelated sheet shifts every
later sheet's own auto-numbered components -- even though that sheet's
source is byte-for-byte unchanged. Left unfixed, the KiCad-side incremental
sync then can't tell "this component's reference just shifted" apart from
"this component was deleted and an unrelated one was created", and silently
deletes+recreates components with fresh UUIDs, breaking their anchor to any
manually-placed PCB footprint.

This module implements the "current recommended approach" from issue #58:
before the counter-based assignment in :meth:`Circuit.finalize_references`
ever touches a given placeholder component, match it against the
*already-annotated* components of the existing KiCad project (loaded
separately, see ``circuit_synth.kicad.schematic.reference_preallocator``)
using net *connectivity* -- not declaration order -- and preallocate the
matched component's old, stable reference directly. Only genuinely-new
(unmatched) components ever consume a fresh counter value.

This module itself has **no KiCad-file-I/O dependency** -- it operates on
plain, already-extracted connectivity records, so the whole matching
algorithm (including the two trickiest pieces: duplicate-connectivity
tiebreaking and auto-named-net resolution) can be unit tested without ever
touching ``kicad_sch_api`` or a real ``.kicad_sch`` file. The adapter that
extracts these records from a real, already-generated KiCad project lives in
``circuit_synth.kicad.schematic.reference_preallocator``; the adapter that
extracts them from an in-memory, not-yet-finalized Python ``Circuit`` tree
lives in ``Circuit._collect_preallocation_records`` (``core/circuit.py``).

Connectivity signature encoding (design decision, not fully specified by the
ticket)
------------------------------------------------------------------------
A component's connectivity signature is a sorted tuple of
``(pin_id, net_token)`` pairs, one per wired pin. ``net_token`` is:

- ``("NC",)`` if the pin has no net at all.
- ``("NET", <name>)`` for an explicitly-named net (including KiCad power/
  global nets) -- the name itself is stable across runs, so it's used
  directly.
- ``("ANON", <peer tuple>)`` for one of circuit-synth's own auto-named nets
  (``N$1``, ``N$2``, ...). These names are themselves unstable across runs
  (a new unnamed net declared earlier in the source shifts every later
  unnamed net's counter value, the exact same failure mode as reference
  designators) so the raw name is never compared directly. Instead the net
  is described by its *other* connections: for every other pin on that net,
  a ``(peer_prefix, peer_value, peer_pin_id)`` tuple -- deliberately a
  *structural* descriptor (prefix + value + pin number), not an *identity*
  descriptor (a ref string or an ordinal position). This is what makes the
  same token computable, and comparable, on both the old (KiCad-side, real
  final refs) and new (Python-side, still-unfinalized placeholders) side:
  neither a raw net name nor a raw ref/ordinal would be legible on both
  sides at once, but "what kind of thing is connected, and to which of its
  pins" is. Only one level of recursion is performed (the peer is described
  by its own type, not by recursively resolving *its* other anonymous
  nets) -- deliberately bounded, to keep the signature finite and avoid
  cycles; residual ambiguity beyond that is left to the ordinal tiebreak
  below, which already exists for a different reason (see below) and
  handles this case too.

Duplicate-connectivity tiebreak
--------------------------------
Some components are genuinely, legitimately indistinguishable by
connectivity alone -- the concrete example already in this codebase's
consuming project is four parallel 0R AGND/DGND tie resistors
(``R73``-``R76`` on the ``acquisition_analog_power`` board), each wired
between the exact same two nets. :func:`match_sheet_prefix_group` resolves
this by pairing components sharing an identical signature in a fixed,
deterministic order on each side: existing (old) components ascending by
their numeric reference suffix, new (Python) components in declaration
order (the order they were constructed in the sheet's own source) -- the
same "ordinal-among-the-duplicate-group" tiebreak the ticket specifies,
scoped narrowly to just that one duplicate-signature bucket rather than the
whole sheet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Hashable, List, Optional, Tuple

# circuit-synth's own convention for auto-generated (unnamed) net names --
# see core/reference_manager.py:generate_next_unnamed_net_name().
AUTO_NET_RE = re.compile(r"^N\$\d+$")

# Matches a trailing digit run for computing a reference's numeric suffix
# (used to order existing/old-side components deterministically within a
# duplicate-signature bucket).
_TRAILING_DIGITS_RE = re.compile(r"(\d+)$")

# Matches a leading alpha (+ underscore) run for computing a reference's
# prefix from a final ("R38") KiCad reference string.
_LEADING_PREFIX_RE = re.compile(r"^([A-Za-z_]+)")


def ref_prefix(ref: str) -> str:
    """Return the alpha prefix of a final reference string, e.g. "R38" -> "R"."""
    m = _LEADING_PREFIX_RE.match(ref)
    return m.group(1) if m else ref


def ref_numeric_suffix(ref: str) -> int:
    """Return the trailing numeric suffix of a reference, or -1 if none."""
    m = _TRAILING_DIGITS_RE.search(ref)
    return int(m.group(1)) if m else -1


@dataclass(frozen=True)
class PinRecord:
    """One wired (or unwired) pin, for global net-token resolution.

    ``owner_key`` only needs to be hashable and unique per owning component
    within one call to :func:`build_global_net_map` -- it is used solely to
    exclude "self" when describing a net's *other* connections, never
    compared across the old/new side boundary.
    """

    owner_key: Hashable
    prefix: str
    value: Optional[str]
    pin_id: str
    net_name: Optional[str]


@dataclass(frozen=True)
class ComponentRecord:
    """One component's connectivity record, from either side (old KiCad
    project or new, not-yet-finalized Python circuit).

    ``identity`` is opaque to this module: for the old side it's typically
    the KiCad reference string (e.g. "R38"); for the new side it's whatever
    the caller (``Circuit``) wants to use to look the placeholder component
    back up after matching (in practice, the ``Component`` object itself).
    """

    identity: Any
    prefix: str
    value: Optional[str]
    pins: Dict[str, Optional[str]]
    # Present (and used for ordering) only on the old/existing side.
    sort_key: Any = None


def build_global_net_map(
    records: List[ComponentRecord],
) -> Dict[str, List[PinRecord]]:
    """Build a net-name -> [PinRecord, ...] map across an entire set of
    components (a whole sheet, or a whole project/tree -- callers decide
    the scope; auto-named-net resolution needs the *whole project* scope
    since a net can cross sheet boundaries, see reference_preallocator.py).
    """
    net_map: Dict[str, List[PinRecord]] = {}
    for rec in records:
        for pin_id, net_name in rec.pins.items():
            if not net_name:
                continue
            net_map.setdefault(net_name, []).append(
                PinRecord(
                    owner_key=rec.identity,
                    prefix=rec.prefix,
                    value=rec.value,
                    pin_id=str(pin_id),
                    net_name=net_name,
                )
            )
    return net_map


def _resolve_net_token(
    net_name: Optional[str],
    self_owner_key: Hashable,
    self_pin_id: str,
    global_net_map: Dict[str, List[PinRecord]],
) -> Tuple:
    if not net_name:
        return ("NC",)
    if not AUTO_NET_RE.match(net_name):
        return ("NET", net_name)

    peers = tuple(
        sorted(
            (pr.prefix, pr.value or "", pr.pin_id)
            for pr in global_net_map.get(net_name, [])
            if not (pr.owner_key == self_owner_key and pr.pin_id == self_pin_id)
        )
    )
    return ("ANON", peers)


def compute_signature(
    rec: ComponentRecord,
    global_net_map: Dict[str, List[PinRecord]],
) -> Tuple:
    """Deterministic, order-independent connectivity signature for one
    component: the component's value plus a sorted tuple of
    (pin_id, resolved_net_token) pairs.

    Value is part of the signature (wayfinder #60). Two components can
    share byte-identical pin/net topology while being genuinely different
    parts -- the concrete case found in this codebase's consuming project:
    an 8-member 100nF decoupling bank (`_decouple()`) sharing its rail/GND
    nets with one unrelated standalone 4.7uF bulk cap (`_C("C", "4.7uF",
    rail, gnd)`), all 9 wired to the exact same two NAMED (not auto-named)
    nets. Before this fix, `_resolve_net_token` returns a bare `("NET",
    name)` token for a named net regardless of value, so all 9 components
    landed in ONE duplicate-connectivity bucket together. Deleting one bank
    member (dropping the Python side to 8 total: 7 bank + 1 standalone) then
    let `match_sheet_prefix_group`'s ordinal tiebreak pair the *standalone*
    4.7uF cap against an *old bank member's* reference -- same bucket,
    consumed in position order, no value check to stop it. The matched
    (same-ref) pair then went through `_process_matches`/`_needs_update()`,
    which silently overwrote the old 100nF bank member's on-disk value to
    4.7uF (position/footprint/UUID unchanged, so nothing else caught it),
    while the real old 4.7uF component's reference was left unmatched --
    headed for spurious removal by `_process_unmatched()`. Reproduced and
    confirmed against a sandboxed copy of the real `acquisition_mcu` board's
    MCU sheet (`_decouple("C_MCU", v3v3_u1, gnd, 8)` -> `7`): incremental
    sync silently relabeled two live components' values (C11 100nF->4.7uF,
    C12 4.7uF->100nF) at their original positions.

    Including value in the signature keeps genuinely-identical duplicates
    (e.g. four parallel 0R AGND/DGND ties, all value "0") in one bucket
    exactly as before -- see the module docstring's "Duplicate-connectivity
    tiebreak" section -- while correctly splitting a same-nets-different-
    value component into its own, unambiguous bucket.
    """
    tokens = [
        (
            str(pin_id),
            _resolve_net_token(net_name, rec.identity, str(pin_id), global_net_map),
        )
        for pin_id, net_name in rec.pins.items()
    ]
    return (("__value__", rec.value or ""),) + tuple(sorted(tokens))


def match_sheet_prefix_group(
    new_records: List[ComponentRecord],
    existing_records: List[ComponentRecord],
    new_global_net_map: Dict[str, List[PinRecord]],
    existing_global_net_map: Dict[str, List[PinRecord]],
) -> Dict[Any, Any]:
    """Match one sheet's new (Python-side) components against the existing
    (KiCad-side) components sharing the same reference prefix.

    Both ``new_records`` and ``existing_records`` must already be filtered
    to a single (sheet, prefix) group -- prefix identity is a required
    equality dimension, never derived from the signature itself.

    ``new_records`` order is significant: it must be the sheet's own
    Python declaration order (used as the new-side tiebreak ordering).
    ``existing_records`` order is not significant -- this function sorts
    them by ascending numeric reference suffix itself for the old-side
    tiebreak ordering.

    Returns ``{new_record.identity: existing_record.identity}`` for every
    match found. Unmatched new records (genuinely new components) and
    unmatched existing records (genuinely removed components) are simply
    absent from the result -- callers fall back to counter-based assignment
    for the former and the existing removal path for the latter.
    """
    existing_sorted = sorted(existing_records, key=lambda r: ref_numeric_suffix(r.sort_key or r.identity))

    # Bucket existing components by signature, preserving the ref-ascending
    # order within each bucket (the old-side half of the tiebreak).
    existing_buckets: Dict[Tuple, List[ComponentRecord]] = {}
    for rec in existing_sorted:
        sig = compute_signature(rec, existing_global_net_map)
        existing_buckets.setdefault(sig, []).append(rec)

    matches: Dict[Any, Any] = {}
    # new_records is already in declaration order (the new-side half of the
    # tiebreak) -- consume matching existing components in that order.
    for rec in new_records:
        sig = compute_signature(rec, new_global_net_map)
        bucket = existing_buckets.get(sig)
        if bucket:
            matched = bucket.pop(0)
            matches[rec.identity] = matched.identity

    return matches
