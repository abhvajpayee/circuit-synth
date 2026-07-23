"""
symbol_geometry.py

Calculate accurate bounding boxes for KiCad symbols based on their graphical elements.
This ensures proper spacing and collision detection in schematic layouts.
"""

import logging
import math
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def build_ref_to_pin_net_map(circuit: Any) -> Dict[str, Dict[str, str]]:
    """
    Build a {component_ref: {pin_number: net_name}} map from a circuit's own
    net connections, for callers that need to pass a real `pin_net_map` into
    `SymbolBoundingBoxCalculator.calculate_bounding_box()` / `get_symbol_dimensions()`
    so pin labels are sized by their actual net name length rather than
    falling into the generic "XXX" 3-character placeholder.

    Mirrors the same `net.connections` iteration `_add_pin_level_net_labels`
    (schematic_writer.py) already uses successfully -- `circuit.nets` may be
    a dict (name -> Net) or a list of Net objects, each with `.name` and
    `.connections` (a list of (component_ref, pin_number) tuples).

    Args:
        circuit: A circuit-like object exposing `.nets`.

    Returns:
        Dict keyed by component reference, each value a dict of
        {pin_number: net_name}. Missing components simply aren't present as
        keys -- callers should use `.get(ref, {})`.
    """
    circuit_nets = circuit.nets.values() if isinstance(circuit.nets, dict) else circuit.nets

    ref_to_pin_net_map: Dict[str, Dict[str, str]] = {}
    for net in circuit_nets:
        for comp_ref, pin_identifier in net.connections:
            ref_to_pin_net_map.setdefault(comp_ref, {})[pin_identifier] = net.name

    return ref_to_pin_net_map


class SymbolBoundingBoxCalculator:
    """Calculate the actual bounding box of a symbol from its graphical elements."""

    # KiCad default text size in mm
    # Increased to better match actual KiCad rendering
    DEFAULT_TEXT_HEIGHT = 2.54  # 100 mils (doubled from 50 mils)
    DEFAULT_PIN_LENGTH = 2.54  # 100 mils
    DEFAULT_PIN_NAME_OFFSET = 0.508  # 20 mils
    DEFAULT_PIN_NUMBER_SIZE = 1.27  # 50 mils
    # Improved text width ratio to match KiCad's proportional font rendering
    # KiCad uses proportional fonts where average character width is ~0.65x height
    # This prevents label text from extending beyond calculated bounding boxes
    DEFAULT_PIN_TEXT_WIDTH_RATIO = (
        0.65  # Width to height ratio for pin text (proportional font average)
    )

    @classmethod
    def calculate_bounding_box(
        cls,
        symbol_data: Dict[str, Any],
        include_properties: bool = True,
        hierarchical_labels: Optional[List[Dict[str, Any]]] = None,
        pin_net_map: Optional[Dict[str, str]] = None,
    ) -> Tuple[float, float, float, float]:
        """
        Calculate the actual bounding box of a symbol from its graphical elements.

        Args:
            symbol_data: Dictionary containing symbol definition from KiCad library
            include_properties: Whether to include space for Reference/Value labels
            hierarchical_labels: List of hierarchical labels attached to this symbol
            pin_net_map: Optional mapping of pin numbers to net names (for accurate label sizing)

        Returns:
            Tuple of (min_x, min_y, max_x, max_y) in mm

        Raises:
            ValueError: If symbol data is invalid or bounding box cannot be calculated
        """
        if not symbol_data:
            raise ValueError("Symbol data is None or empty")

        import sys

        # Reduced logging frequency - only log if DEBUG environment variable is set
        debug_enabled = os.getenv("CIRCUIT_SYNTH_DEBUG", "").lower() == "true"
        if debug_enabled:
            print(f"\n=== CALCULATING BOUNDING BOX ===", file=sys.stderr, flush=True)
            print(
                f"include_properties={include_properties}", file=sys.stderr, flush=True
            )

        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")

        # Process main symbol shapes (handle both 'shapes' and 'graphics' keys)
        shapes = symbol_data.get("shapes", []) or symbol_data.get("graphics", [])
        print(f"Processing {len(shapes)} main shapes", file=sys.stderr, flush=True)
        for shape in shapes:
            shape_bounds = cls._get_shape_bounds(shape)
            if shape_bounds:
                s_min_x, s_min_y, s_max_x, s_max_y = shape_bounds
                min_x = min(min_x, s_min_x)
                min_y = min(min_y, s_min_y)
                max_x = max(max_x, s_max_x)
                max_y = max(max_y, s_max_y)

        # Process pins (including their labels)
        pins = symbol_data.get("pins", [])
        print(f"Processing {len(pins)} main pins", file=sys.stderr, flush=True)
        for pin in pins:
            pin_bounds = cls._get_pin_bounds(pin, pin_net_map)
            if pin_bounds:
                p_min_x, p_min_y, p_max_x, p_max_y = pin_bounds
                min_x = min(min_x, p_min_x)
                min_y = min(min_y, p_min_y)
                max_x = max(max_x, p_max_x)
                max_y = max(max_y, p_max_y)

        # Process sub-symbols
        sub_symbols = symbol_data.get("sub_symbols", [])
        for sub in sub_symbols:
            # Sub-symbols can have their own shapes and pins (handle both 'shapes' and 'graphics' keys)
            sub_shapes = sub.get("shapes", []) or sub.get("graphics", [])
            for shape in sub_shapes:
                shape_bounds = cls._get_shape_bounds(shape)
                if shape_bounds:
                    s_min_x, s_min_y, s_max_x, s_max_y = shape_bounds
                    min_x = min(min_x, s_min_x)
                    min_y = min(min_y, s_min_y)
                    max_x = max(max_x, s_max_x)
                    max_y = max(max_y, s_max_y)

            sub_pins = sub.get("pins", [])
            for pin in sub_pins:
                pin_bounds = cls._get_pin_bounds(pin, pin_net_map)
                if pin_bounds:
                    p_min_x, p_min_y, p_max_x, p_max_y = pin_bounds
                    min_x = min(min_x, p_min_x)
                    min_y = min(min_y, p_min_y)
                    max_x = max(max_x, p_max_x)
                    max_y = max(max_y, p_max_y)

        # Check if we found any geometry
        if min_x == float("inf") or max_x == float("-inf"):
            raise ValueError(f"No valid geometry found in symbol data")

        print(
            f"After geometry processing: ({min_x:.2f}, {min_y:.2f}) to ({max_x:.2f}, {max_y:.2f})",
            file=sys.stderr,
            flush=True,
        )
        print(
            f"  Width: {max_x - min_x:.2f}, Height: {max_y - min_y:.2f}",
            file=sys.stderr,
            flush=True,
        )

        # Add small margin for text that might extend beyond shapes
        margin = 0.254  # 10 mils
        min_x -= margin
        min_y -= margin
        max_x += margin
        max_y += margin

        # Include space for component properties (Reference, Value, Footprint)
        if include_properties:
            # Use adaptive spacing based on component dimensions
            component_width = max_x - min_x
            component_height = max_y - min_y

            # Adaptive property width: minimum 10mm or 80% of component width
            property_width = max(10.0, component_width * 0.8)
            property_height = cls.DEFAULT_TEXT_HEIGHT

            # Adaptive vertical spacing: minimum 5mm or 10% of component height
            vertical_spacing_above = max(5.0, component_height * 0.1)
            vertical_spacing_below = max(10.0, component_height * 0.15)

            # Reference label above
            min_y -= vertical_spacing_above + property_height

            # Value and Footprint labels below
            max_y += vertical_spacing_below + property_height

            # Extend horizontally for property text
            center_x = (min_x + max_x) / 2
            min_x = min(min_x, center_x - property_width / 2)
            max_x = max(max_x, center_x + property_width / 2)

        logger.debug(
            f"Calculated bounding box: ({min_x:.2f}, {min_y:.2f}) to ({max_x:.2f}, {max_y:.2f})"
        )

        print(
            f"FINAL BBOX: ({min_x:.2f}, {min_y:.2f}) to ({max_x:.2f}, {max_y:.2f})",
            file=sys.stderr,
            flush=True,
        )
        print(
            f"  Width: {max_x - min_x:.2f}, Height: {max_y - min_y:.2f}",
            file=sys.stderr,
            flush=True,
        )
        print("=" * 50 + "\n", file=sys.stderr, flush=True)

        return (min_x, min_y, max_x, max_y)

    @classmethod
    def get_symbol_dimensions(
        cls,
        symbol_data: Dict[str, Any],
        include_properties: bool = True,
        pin_net_map: Optional[Dict[str, str]] = None,
    ) -> Tuple[float, float]:
        """
        Get the width and height of a symbol.

        Args:
            symbol_data: Dictionary containing symbol definition
            include_properties: Whether to include space for Reference/Value labels
            pin_net_map: Optional mapping of pin numbers to net names

        Returns:
            Tuple of (width, height) in mm
        """
        min_x, min_y, max_x, max_y = cls.calculate_bounding_box(
            symbol_data, include_properties, pin_net_map=pin_net_map
        )
        width = max_x - min_x
        height = max_y - min_y
        return (width, height)

    @classmethod
    def _get_shape_bounds(
        cls, shape: Dict[str, Any]
    ) -> Optional[Tuple[float, float, float, float]]:
        """Get bounding box for a graphical shape."""
        shape_type = shape.get("shape_type", "")

        if shape_type == "rectangle":
            start = shape.get("start", [0, 0])
            end = shape.get("end", [0, 0])
            return (
                min(start[0], end[0]),
                min(start[1], end[1]),
                max(start[0], end[0]),
                max(start[1], end[1]),
            )

        elif shape_type == "circle":
            center = shape.get("center", [0, 0])
            radius = shape.get("radius", 0)
            return (
                center[0] - radius,
                center[1] - radius,
                center[0] + radius,
                center[1] + radius,
            )

        elif shape_type == "arc":
            # For arcs, we need to consider start, mid, and end points
            start = shape.get("start", [0, 0])
            mid = shape.get("mid", [0, 0])
            end = shape.get("end", [0, 0])

            # Simple approach: use bounding box of all three points
            # More accurate would be to calculate the actual arc bounds
            min_x = min(start[0], mid[0], end[0])
            min_y = min(start[1], mid[1], end[1])
            max_x = max(start[0], mid[0], end[0])
            max_y = max(start[1], mid[1], end[1])

            return (min_x, min_y, max_x, max_y)

        elif shape_type == "polyline":
            points = shape.get("points", [])
            if not points:
                return None

            min_x = min(p[0] for p in points)
            min_y = min(p[1] for p in points)
            max_x = max(p[0] for p in points)
            max_y = max(p[1] for p in points)

            return (min_x, min_y, max_x, max_y)

        elif shape_type == "text":
            # Text bounding box estimation
            at = shape.get("at", [0, 0])
            text = shape.get("text", "")
            # Rough estimation: each character is about 1.27mm wide
            text_width = len(text) * cls.DEFAULT_TEXT_HEIGHT * 0.6
            text_height = cls.DEFAULT_TEXT_HEIGHT

            return (
                at[0] - text_width / 2,
                at[1] - text_height / 2,
                at[0] + text_width / 2,
                at[1] + text_height / 2,
            )

        return None

    @classmethod
    def _get_pin_bounds(
        cls, pin: Dict[str, Any], pin_net_map: Optional[Dict[str, str]] = None
    ) -> Optional[Tuple[float, float, float, float]]:
        """Get bounding box for a pin including its labels."""
        import sys

        # Handle both formats: 'at' array or separate x/y/orientation
        if "at" in pin:
            at = pin.get("at", [0, 0])
            x, y = at[0], at[1]
            angle = at[2] if len(at) > 2 else 0
        else:
            # Handle the format from symbol cache
            x = pin.get("x", 0)
            y = pin.get("y", 0)
            angle = pin.get("orientation", 0)

        length = pin.get("length", cls.DEFAULT_PIN_LENGTH)

        # Calculate pin endpoint based on angle
        angle_rad = math.radians(angle)
        end_x = x + length * math.cos(angle_rad)
        end_y = y + length * math.sin(angle_rad)

        # Start with pin line bounds
        min_x = min(x, end_x)
        min_y = min(y, end_y)
        max_x = max(x, end_x)
        max_y = max(y, end_y)

        # Add space for pin name and number
        pin_name = pin.get("name", "")
        pin_number = pin.get("number", "")

        # Use net name for label sizing if available (hierarchical labels show net names, not pin names)
        # If no net name match, use minimal fallback to avoid oversized bounding boxes
        if pin_net_map and pin_number in pin_net_map:
            label_text = pin_net_map[pin_number]
            logger.debug(
                f"PIN {pin_number}: Using net '{label_text}' (len={len(label_text)}), at=({x:.2f}, {y:.2f}), angle={angle}"
            )
        else:
            # No net match - use minimal size (3 chars) instead of potentially long pin name
            label_text = "XXX"  # 3-character placeholder for unmatched pins
            logger.debug(
                f"PIN {pin_number}: Using fallback sizing (pin name='{pin_name}'), at=({x:.2f}, {y:.2f})"
            )

        if label_text and label_text != "~":  # ~ means no name
            # Calculate text dimensions
            # For horizontal text: width = char_count * char_width
            name_width = (
                len(label_text)
                * cls.DEFAULT_TEXT_HEIGHT
                * cls.DEFAULT_PIN_TEXT_WIDTH_RATIO
            )
            # For vertical text: height = char_count * char_height (characters stack vertically)
            name_height = len(label_text) * cls.DEFAULT_TEXT_HEIGHT

            print(
                f"    label_width={name_width:.2f}, label_height={name_height:.2f} (len={len(label_text)})",
                file=sys.stderr,
                flush=True,
            )

            # Adjust bounds based on pin orientation
            # Labels are placed at PIN ENDPOINT with offset, extending AWAY from the component
            # Pin angle indicates where the pin points (into component)
            # Apply KiCad's standard pin name offset (0.508mm / 20 mils)
            offset = cls.DEFAULT_PIN_NAME_OFFSET

            # Direction convention -- CORRECTED 2026-07-22 after an earlier
            # same-day fix here turned out to be wrong (see
            # tests/unit/kicad/test_pin_label_bounds_direction.py history).
            #
            # `angle` here is the pin's RAW angle as declared in the symbol
            # (the direction the pin's own line points, into empty space,
            # away from the body). That is NOT the angle the rendered label
            # ends up at: `_add_pin_level_net_labels` (schematic_writer.py)
            # computes `label_angle = (pin_angle + 180) % 360` before writing
            # the label, and KiCad's justify convention then determines which
            # way the TEXT extends from that. Verified directly against a
            # real generated schematic: the SDRAM's DQ15 pin is declared at
            # raw angle 0, but its rendered net label has angle 180 and
            # visibly extends toward -X. Skipping the +180 step and reasoning
            # from the raw angle directly (as an earlier version of this
            # function did) gets every cardinal direction backwards.
            render_angle = (angle + 180) % 360

            if render_angle == 0:  # label extends toward +X
                label_x = end_x + offset + name_width
                print(
                    f"    angle={angle} (render 0): max_x {max_x:.2f} -> {label_x:.2f} (offset={offset:.3f})",
                    file=sys.stderr,
                    flush=True,
                )
                max_x = max(max_x, label_x)
            elif render_angle == 180:  # label extends toward -X
                label_x = end_x - offset - name_width
                print(
                    f"    angle={angle} (render 180): min_x {min_x:.2f} -> {label_x:.2f} (offset={offset:.3f})",
                    file=sys.stderr,
                    flush=True,
                )
                min_x = min(min_x, label_x)
            elif render_angle == 90:  # label extends toward +Y
                label_y = end_y + offset + name_height
                print(
                    f"    angle={angle} (render 90): max_y {max_y:.2f} -> {label_y:.2f} (offset={offset:.3f})",
                    file=sys.stderr,
                    flush=True,
                )
                max_y = max(max_y, label_y)
            elif render_angle == 270:  # label extends toward -Y
                label_y = end_y - offset - name_height
                print(
                    f"    angle={angle} (render 270): min_y {min_y:.2f} -> {label_y:.2f} (offset={offset:.3f})",
                    file=sys.stderr,
                    flush=True,
                )
                min_y = min(min_y, label_y)

        # Pin numbers are typically placed near the component body
        if pin_number:
            num_width = (
                len(pin_number)
                * cls.DEFAULT_PIN_NUMBER_SIZE
                * cls.DEFAULT_PIN_TEXT_WIDTH_RATIO
            )
            # Add some space for the pin number
            margin = (
                cls.DEFAULT_PIN_NUMBER_SIZE * 1.5
            )  # Increase margin for better spacing
            min_x -= margin
            min_y -= margin
            max_x += margin
            max_y += margin

        print(
            f"    Pin bounds: ({min_x:.2f}, {min_y:.2f}) to ({max_x:.2f}, {max_y:.2f})",
            file=sys.stderr,
            flush=True,
        )
        return (min_x, min_y, max_x, max_y)
