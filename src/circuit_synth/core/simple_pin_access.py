# FILE: src/circuit_synth/core/simple_pin_access.py

import warnings
from typing import Union

from .exception import ComponentError
from .pin import Pin


class PinGroup:
    """A simple group of pins that supports the += operator for net connections."""

    def __init__(self, pins):
        self._pins = pins

    def __iadd__(self, net):
        """Connect all pins in this group to the given net."""
        for pin in self._pins:
            pin += net
        return self

    def __repr__(self):
        names = [p.name for p in self._pins]
        return f"<PinGroup names={names}>"


class SimplifiedPinAccess:
    """
    Simplified pin access mixin that provides cleaner pin lookup.

    This replaces the over-engineered __getitem__ method with a simpler approach:
    - Direct pin number lookup (primary)
    - Pin name lookup (secondary)
    - Clear error messages without excessive complexity
    """

    def __getitem__(self, pin_id: Union[int, str]) -> Union[Pin, PinGroup]:
        """
        Retrieve pin(s) by pin number or name.
        Returns PinGroup if multiple pins share the same name.

        Supports both integer and string pin numbers.
        Note: Pin 0 access requires the component to actually have a pin 0.
        """
        # Handle integer access - convert to string for lookup
        if isinstance(pin_id, int):
            # Validate pin 0 access - only allow if component actually has pin 0
            if pin_id == 0 and "0" not in self._pins:
                # Check if this might be an accidental pin 0 access
                available_nums = [p.num for p in self._pins.values() if p.num.isdigit()]
                if available_nums and all(int(num) > 0 for num in available_nums):
                    raise ComponentError(
                        f"Pin 0 not found in {self.ref} ({self.symbol}). "
                        f"Available numeric pins: {', '.join(sorted(available_nums, key=int))}. "
                        f"Did you mean pin 1?"
                    )
            pin_id = str(pin_id)

        # Validate input
        if not isinstance(pin_id, str):
            raise ComponentError(
                f"Pin identifier must be string or int, got {type(pin_id).__name__}"
            )

        # 1. Try direct pin number lookup (most common case)
        if pin_id in self._pins:
            return self._pins[pin_id]

        # 2. Try pin name lookup
        if hasattr(self, "_pin_names") and pin_id in self._pin_names:
            pins = self._pin_names[pin_id]
            return pins[0] if len(pins) == 1 else PinGroup(pins)

        # 3. Not found - provide helpful error
        available_pins = [f"{p.num}" for p in self._pins.values()]
        if hasattr(self, "_pin_names"):
            available_names = list(self._pin_names.keys())
            if available_names:
                available_pins.extend(
                    [f"'{name}'" for name in available_names if name and name != "~"]
                )

        raise ComponentError(
            f"Pin '{pin_id}' not found in {self.ref} ({self.symbol}). "
            f"Available: {', '.join(sorted(available_pins))}"
        )

    def __setitem__(self, pin_id: Union[int, str], new_pin: Pin):
        """
        Set a pin. Ignores PinGroup assignments (from += operations).

        Note: `comp["NAME"] += net` triggers Python's augmented-assignment
        protocol -- __getitem__ (fetch), Pin.__iadd__ (connect), then this
        __setitem__ as a writeback of the (same) result -- even when `pin_id`
        was a NAME string rather than the pin's own number. Always re-key by
        the pin's own number so a name-based writeback can't create a second,
        bogus dict entry under the name string; and de-duplicate the
        `_pin_names` list so repeated writebacks for the same physical pin
        don't grow it past one entry.
        """
        # Ignore PinGroup assignments (these happen after += operations)
        if isinstance(new_pin, PinGroup):
            return

        if not isinstance(new_pin, Pin):
            raise ComponentError(
                f"Can only assign Pin objects, got {type(new_pin).__name__}"
            )

        # Always store by the pin's OWN number -- `pin_id` may be the NAME
        # the caller indexed with (e.g. comp["PG7"] += net), not the pin's
        # number, and keying by it would create a second entry for one pin.
        pin_key = str(new_pin.num)
        self._pins[pin_key] = new_pin

        # Update name lookup if applicable, de-duplicated by identity so a
        # writeback for a pin already registered under this name doesn't
        # grow the list.
        if hasattr(self, "_pin_names") and new_pin.name and new_pin.name != "~":
            existing = self._pin_names.setdefault(new_pin.name, [])
            if new_pin not in existing:
                existing.append(new_pin)
