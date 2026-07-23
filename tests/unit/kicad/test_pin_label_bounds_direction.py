"""Regression tests for pin-label direction in
`SymbolBoundingBoxCalculator._get_pin_bounds`.

Schematic component placement (`CollisionManager.place_symbol` and, more
importantly, the `TextFlowPlacer` that actually determines final positions)
is supposed to reserve extra bounding-box margin for each pin's net-name
label, in whatever direction that label actually renders, so two components
placed "non-colliding" don't end up with their real, rendered label text
overlapping.

IMPORTANT: `_get_pin_bounds` receives the pin's RAW angle as declared in the
symbol (the direction the pin's own line points, away from the body) -- this
is NOT the angle the rendered label ends up at. `_add_pin_level_net_labels`
(schematic_writer.py) computes `label_angle = (pin_angle + 180) % 360`
before writing the label. Verified directly against a real generated
schematic (windTunnelProject, 2026-07-22): the Acquisition Module's SDRAM
symbol declares its DQ15 pin at raw angle 0, but the rendered net label for
that pin has angle 180 and visibly extends toward -X in the exported PDF.

An earlier version of this fix reasoned from the raw angle directly (i.e.
treated `render_angle == angle`), which got every cardinal direction
backwards -- confirmed by regenerating the real board: the computed bounding
boxes changed as expected, but the visible label collision persisted,
because the "fix" was reserving margin on the correct-looking but actually
wrong side. This file's expectations are keyed off the CORRECT relationship:
`render_angle = (angle + 180) % 360`.

Found investigating a real, user-reported overlap between the Acquisition
Module MCU sheet's SDRAM (U2) and TPM (U6) pin labels (windTunnelProject,
2026-07-22).
"""
from circuit_synth.kicad.sch_gen.symbol_geometry import SymbolBoundingBoxCalculator as C

# _get_pin_bounds always pads ALL FOUR sides by a small, direction-independent
# margin for the pin NUMBER text (DEFAULT_PIN_NUMBER_SIZE * 1.5 = 1.905mm),
# separate from the (direction-dependent) net-name label this test targets.
# Assertions use a tolerance comfortably above that fixed margin but well
# below the ~20mm+ a real label name adds, so they only fail on the actual
# direction bug, not on the always-present pin-number padding.
_FIXED_MARGIN_TOLERANCE = 3.0


def _pin(angle, number="1"):
    """`angle` is the pin's RAW declared angle (as in the symbol library),
    not the rendered label's angle."""
    return {"at": [0.0, 0.0, angle], "name": f"PIN{number}", "number": number}


def test_raw_angle_0_renders_at_180_and_extends_toward_negative_x():
    """Raw angle 0 -> render_angle 180 -> label extends -X (matches the
    real SDRAM DQ15 pin verified against a rendered schematic)."""
    pin = _pin(0)
    net_map = {"1": "SOME_NET_NAME"}
    min_x, min_y, max_x, max_y = C._get_pin_bounds(pin, pin_net_map=net_map)

    pin_tip_x = C.DEFAULT_PIN_LENGTH  # length * cos(0) = length
    assert min_x < 0, "label must extend past the origin toward -X"
    assert max_x <= pin_tip_x + _FIXED_MARGIN_TOLERANCE, "max_x must NOT be pulled toward +X"


def test_raw_angle_180_renders_at_0_and_extends_toward_positive_x():
    """Raw angle 180 -> render_angle 0 -> label extends +X (matches the
    real TPM SPI_CS/PIRQ/etc. pins, all declared at raw angle 180)."""
    pin = _pin(180)
    net_map = {"1": "SOME_NET_NAME"}
    min_x, min_y, max_x, max_y = C._get_pin_bounds(pin, pin_net_map=net_map)

    pin_tip_x = -C.DEFAULT_PIN_LENGTH  # length * cos(180) = -length
    assert max_x > 0, "label must extend past the origin toward +X"
    assert min_x >= pin_tip_x - _FIXED_MARGIN_TOLERANCE, "min_x must NOT be pulled toward -X"


def test_raw_angle_90_renders_at_270_and_extends_toward_negative_y():
    pin = _pin(90)
    net_map = {"1": "SOME_NET_NAME"}
    min_x, min_y, max_x, max_y = C._get_pin_bounds(pin, pin_net_map=net_map)

    pin_tip_y = C.DEFAULT_PIN_LENGTH  # length * sin(90) = length
    assert min_y < 0, "label must extend past the origin toward -Y"
    assert max_y <= pin_tip_y + _FIXED_MARGIN_TOLERANCE, "max_y must NOT be pulled toward +Y"


def test_raw_angle_270_renders_at_90_and_extends_toward_positive_y():
    pin = _pin(270)
    net_map = {"1": "SOME_NET_NAME"}
    min_x, min_y, max_x, max_y = C._get_pin_bounds(pin, pin_net_map=net_map)

    pin_tip_y = -C.DEFAULT_PIN_LENGTH  # length * sin(270) = -length
    assert max_y > 0, "label must extend past the origin toward +Y"
    assert min_y >= pin_tip_y - _FIXED_MARGIN_TOLERANCE, "min_y must NOT be pulled toward -Y"


def test_two_components_facing_each_other_reserve_a_real_gap():
    """End-to-end sanity check matching the real-world failure: TPM's real
    pins (SPI_CS, PIRQ, etc.) are declared at raw angle 180 (render 0,
    extends +X) and SDRAM sits to TPM's right -- so TPM's box must claim
    real space on its +X side, matching where its labels actually go."""
    long_net = "R_TPMMISO_D"  # 11 chars, same order of magnitude as the real bug

    tpm_pin = _pin(180)  # TPM's real pins: raw angle 180 -> renders +X, toward SDRAM
    tpm_bounds = C._get_pin_bounds(tpm_pin, pin_net_map={"1": long_net})

    sdram_pin = _pin(0)  # SDRAM's real pins (e.g. DQ15): raw angle 0 -> renders -X, toward TPM
    sdram_bounds = C._get_pin_bounds(sdram_pin, pin_net_map={"1": long_net})

    tpm_max_x = tpm_bounds[2]
    sdram_min_x = sdram_bounds[0]
    assert tpm_max_x > 0
    assert sdram_min_x < 0
