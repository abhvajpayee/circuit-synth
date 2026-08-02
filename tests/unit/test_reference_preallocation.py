"""
Unit tests for core.reference_preallocation -- the connectivity-based
reference-preallocation matcher for incremental KiCad sync (wayfinder #58).

These tests operate entirely on plain ComponentRecord/PinRecord data, with
no KiCad file I/O and no Circuit/Component objects -- exercising the
matching algorithm (signature computation, duplicate tiebreak, auto-named
net resolution) in isolation, per the ticket's suggested "good first slice."
"""

from circuit_synth.core.reference_preallocation import (
    ComponentRecord,
    build_global_net_map,
    compute_signature,
    match_sheet_prefix_group,
    ref_numeric_suffix,
    ref_prefix,
)


def test_ref_prefix_and_numeric_suffix():
    assert ref_prefix("R38") == "R"
    assert ref_prefix("R_DBG_SCL_PU1") == "R_DBG_SCL_PU"
    assert ref_numeric_suffix("R38") == 38
    assert ref_numeric_suffix("C1") == 1
    assert ref_numeric_suffix("NOREF") == -1


def test_simple_named_net_match():
    """A single resistor on stable, explicitly-named nets matches directly,
    regardless of the specific ref/prefix-ordinal it's given."""
    existing = [
        ComponentRecord(
            identity="R38", prefix="R", value="10k",
            pins={"1": "+3V3", "2": "GND"},
        )
    ]
    new = [
        ComponentRecord(
            identity="new_comp_obj_1", prefix="R", value="10k",
            pins={"1": "+3V3", "2": "GND"},
        )
    ]
    existing_map = build_global_net_map(existing)
    new_map = build_global_net_map(new)

    matches = match_sheet_prefix_group(new, existing, new_map, existing_map)
    assert matches == {"new_comp_obj_1": "R38"}


def test_no_match_for_different_connectivity():
    existing = [
        ComponentRecord(identity="R38", prefix="R", value="10k", pins={"1": "+3V3", "2": "GND"})
    ]
    new = [
        ComponentRecord(identity="new1", prefix="R", value="10k", pins={"1": "+5V", "2": "GND"})
    ]
    existing_map = build_global_net_map(existing)
    new_map = build_global_net_map(new)
    matches = match_sheet_prefix_group(new, existing, new_map, existing_map)
    assert matches == {}


def test_mid_sheet_insertion_does_not_disturb_other_components():
    """Scenario 2 from the ticket: insert a new component in the middle of
    a sheet's own source; the sheet's *other*, pre-existing components must
    still match their old references, keyed purely by connectivity -- not
    by their (now-shifted) declaration-order position."""
    # "Before": sheet had R1 (VCC/GND) and R2 (SIG_A/GND), in that decl order.
    existing = [
        ComponentRecord(identity="R1", prefix="R", value="10k", pins={"1": "VCC", "2": "GND"}),
        ComponentRecord(identity="R2", prefix="R", value="4k7", pins={"1": "SIG_A", "2": "GND"}),
    ]
    # "After": a new resistor (R_NEW placeholder, unfinalized) is spliced in
    # BEFORE the Python-source equivalent of R2 -- i.e. new_records reflects
    # declaration order [new_middle, r1_equivalent, r2_equivalent] would be
    # wrong framing; more precisely, the *sheet's own source* now declares
    # components in the order [r1_equivalent, new_middle, r2_equivalent].
    new = [
        ComponentRecord(identity="r1_new", prefix="R", value="10k", pins={"1": "VCC", "2": "GND"}),
        ComponentRecord(identity="new_middle", prefix="R", value="1k", pins={"1": "SIG_B", "2": "GND"}),
        ComponentRecord(identity="r2_new", prefix="R", value="4k7", pins={"1": "SIG_A", "2": "GND"}),
    ]
    existing_map = build_global_net_map(existing)
    new_map = build_global_net_map(new)
    matches = match_sheet_prefix_group(new, existing, new_map, existing_map)

    assert matches.get("r1_new") == "R1"
    assert matches.get("r2_new") == "R2"
    # The newly-inserted component has no existing counterpart to match.
    assert "new_middle" not in matches


def test_duplicate_connectivity_tiebreak_ordinal_among_group():
    """Real example from the consuming project: 4x parallel 0R AGND/DGND
    tie resistors, all wired between the exact same two nets -- genuinely
    indistinguishable by connectivity alone. Tiebreak: existing side
    ascending by numeric ref, new side by declaration order."""
    existing = [
        ComponentRecord(identity="R76", prefix="R", value="0", pins={"1": "AGND", "2": "DGND"}),
        ComponentRecord(identity="R73", prefix="R", value="0", pins={"1": "AGND", "2": "DGND"}),
        ComponentRecord(identity="R75", prefix="R", value="0", pins={"1": "AGND", "2": "DGND"}),
        ComponentRecord(identity="R74", prefix="R", value="0", pins={"1": "AGND", "2": "DGND"}),
    ]
    # New-side declaration order must be preserved by the caller.
    new = [
        ComponentRecord(identity="new_a", prefix="R", value="0", pins={"1": "AGND", "2": "DGND"}),
        ComponentRecord(identity="new_b", prefix="R", value="0", pins={"1": "AGND", "2": "DGND"}),
        ComponentRecord(identity="new_c", prefix="R", value="0", pins={"1": "AGND", "2": "DGND"}),
        ComponentRecord(identity="new_d", prefix="R", value="0", pins={"1": "AGND", "2": "DGND"}),
    ]
    existing_map = build_global_net_map(existing)
    new_map = build_global_net_map(new)
    matches = match_sheet_prefix_group(new, existing, new_map, existing_map)

    assert matches == {
        "new_a": "R73",
        "new_b": "R74",
        "new_c": "R75",
        "new_d": "R76",
    }


def test_duplicate_tiebreak_partial_when_counts_differ():
    """If a 5th duplicate-connectivity resistor is added, only 4 of the 5
    new components can match (there are only 4 existing); the 5th is
    genuinely new and must fall through to counter-based assignment."""
    existing = [
        ComponentRecord(identity="R73", prefix="R", value="0", pins={"1": "AGND", "2": "DGND"}),
        ComponentRecord(identity="R74", prefix="R", value="0", pins={"1": "AGND", "2": "DGND"}),
    ]
    new = [
        ComponentRecord(identity="new_a", prefix="R", value="0", pins={"1": "AGND", "2": "DGND"}),
        ComponentRecord(identity="new_b", prefix="R", value="0", pins={"1": "AGND", "2": "DGND"}),
        ComponentRecord(identity="new_c", prefix="R", value="0", pins={"1": "AGND", "2": "DGND"}),
    ]
    existing_map = build_global_net_map(existing)
    new_map = build_global_net_map(new)
    matches = match_sheet_prefix_group(new, existing, new_map, existing_map)

    assert matches == {"new_a": "R73", "new_b": "R74"}
    assert "new_c" not in matches


def test_auto_named_net_resolution_anchors_on_peer_structure():
    """The 'hard sub-problem' the ticket flags: an unnamed net's own
    auto-generated name (N$k on the new/Python side) is unstable across
    runs and must never be compared directly. Anchor on the *other*
    directly-connected component's structural description (prefix + value
    + pin) instead.

    Existing (old) side: a real KiCad project always has final, stable
    refs, and its own unnamed-net auto-name convention is irrelevant here --
    what matters is that the *token* built from "other peer's structural
    description" comes out identical to the new side's token, even though
    the raw net name differs completely between the two representations
    (a realistic KiCad auto-name vs. circuit-synth's own N$k).
    """
    # Old side: R38 sits on an unnamed net together with C12's pin 1.
    existing = [
        ComponentRecord(identity="R38", prefix="R", value="10k", pins={"1": "Net-(C12-Pad1)", "2": "GND"}),
        ComponentRecord(identity="C12", prefix="C", value="100nF", pins={"1": "Net-(C12-Pad1)", "2": "GND"}),
    ]
    # New side: same shape, but circuit-synth's own N$-style auto name, and
    # totally different literal net name than the old side's KiCad-style
    # auto name -- yet the two must resolve to the same signature for R's
    # own pin-1 token, because R's own peer (a "C"-prefix, 100nF part, on
    # its own pin "1") is structurally identical.
    new = [
        ComponentRecord(identity="r_new", prefix="R", value="10k", pins={"1": "N$7", "2": "GND"}),
        ComponentRecord(identity="c_new", prefix="C", value="100nF", pins={"1": "N$7", "2": "GND"}),
    ]

    # Only the AUTO_NET_RE-matching (N$k) side is treated as anonymous by
    # this module; a KiCad-style "Net-(C12-Pad1)" name is treated as a
    # stable, explicitly-named net (comparing unequal to "N$7" literally).
    # To prove the anchoring logic actually works (not just named-net
    # equality), rerun with existing renamed to also look auto-generated,
    # using a *different* counter value than the new side to prove the
    # comparison isn't literal-string-based.
    existing_anon = [
        ComponentRecord(identity="R38", prefix="R", value="10k", pins={"1": "N$3", "2": "GND"}),
        ComponentRecord(identity="C12", prefix="C", value="100nF", pins={"1": "N$3", "2": "GND"}),
    ]

    existing_map = build_global_net_map(existing_anon)
    new_map = build_global_net_map(new)

    r_sig_old = compute_signature(existing_anon[0], existing_map)
    r_sig_new = compute_signature(new[0], new_map)
    assert r_sig_old == r_sig_new, "auto-named net token must not depend on the literal N$k value"

    matches = match_sheet_prefix_group(
        [new[0]], [existing_anon[0]], new_map, existing_map
    )
    assert matches == {"r_new": "R38"}


def test_auto_named_net_with_shifted_counter_value_still_matches():
    """Directly reproduces the 'auto-named nets are themselves unstable'
    concern: the SAME circuit, resynthesized after an unrelated net was
    declared earlier in the source (shifting every later N$ counter value
    by one), must still preallocate the same reference."""
    # Run 1: unrelated net consumed N$1, so this pair's net is N$2.
    existing = [
        ComponentRecord(identity="R5", prefix="R", value="1k", pins={"1": "N$2", "2": "GND"}),
        ComponentRecord(identity="D1", prefix="D", value="LED", pins={"1": "N$2", "2": "GND"}),
    ]
    # Run 2 (new Python execution): one more unrelated unnamed net got
    # declared earlier in the source, so the *same* logical net is now
    # N$3 instead of N$2 -- a pure renumbering artifact, no topology change.
    new = [
        ComponentRecord(identity="r_new", prefix="R", value="1k", pins={"1": "N$3", "2": "GND"}),
        ComponentRecord(identity="d_new", prefix="D", value="LED", pins={"1": "N$3", "2": "GND"}),
    ]
    existing_map = build_global_net_map(existing)
    new_map = build_global_net_map(new)
    matches = match_sheet_prefix_group([new[0]], [existing[0]], new_map, existing_map)
    assert matches == {"r_new": "R5"}


def test_no_connect_pin_matches_no_connect_pin():
    existing = [
        ComponentRecord(identity="U1", prefix="U", value="MCU", pins={"1": "GND", "2": None}),
    ]
    new = [
        ComponentRecord(identity="u_new", prefix="U", value="MCU", pins={"1": "GND", "2": None}),
    ]
    existing_map = build_global_net_map(existing)
    new_map = build_global_net_map(new)
    matches = match_sheet_prefix_group(new, existing, new_map, existing_map)
    assert matches == {"u_new": "U1"}
