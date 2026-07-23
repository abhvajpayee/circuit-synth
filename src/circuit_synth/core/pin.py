# src/circuit_synth/core/pin.py

from enum import Enum
from typing import TYPE_CHECKING, Optional, Union

from ._logger import context_logger
from .exception import ComponentError
from .net import Net

if TYPE_CHECKING:
    from .component import Component


class PinType(Enum):
    """Enumeration of valid pin types."""

    INPUT = "input"
    OUTPUT = "output"
    BIDIRECTIONAL = "bidirectional"
    POWER_IN = "power_in"
    POWER_OUT = "power_out"
    PASSIVE = "passive"
    TRI_STATE = "tri_state"  # Added for components like 74HC595
    NO_CONNECT = "no_connect"  # Added for unconnected pins
    UNSPECIFIED = "unspecified"  # Added for KiCad unspecified pin types
    OPEN_COLLECTOR = "open_collector"  # Added for open collector outputs
    OPEN_EMITTER = "open_emitter"  # Added for open emitter outputs

    def __eq__(self, other) -> bool:
        """
        Override equality comparison to ensure type safety.

        Args:
            other: The value to compare with

        Returns:
            bool: True if equal, False if not equal and same type

        Raises:
            TypeError: If comparing with non-PinType value
        """
        if not isinstance(other, PinType):
            raise TypeError(f"Cannot compare PinType with {type(other)}")
        return super().__eq__(other)

    def __str__(self) -> str:
        """Return the string value of the pin type."""
        return self.value

    @classmethod
    def from_string(cls, value: str) -> "PinType":
        """
        Create a PinType from a string value.

        Args:
            value: String representation of the pin type

        Returns:
            PinType: The corresponding PinType enum value

        Raises:
            ValueError: If the string doesn't match a valid pin type
        """
        try:
            return next(
                pin_type for pin_type in cls if pin_type.value.lower() == value.lower()
            )
        except StopIteration:
            raise ValueError(f"Invalid pin type: {value}")

    def can_connect_to(self, other: "PinType") -> bool:
        """
        Check if this pin type can connect to another pin type.

        Args:
            other: The PinType to check compatibility with

        Returns:
            bool: True if the pins can be connected, False otherwise

        Note:
            All pin types can connect to each other except NO_CONNECT pins.
            Design Rule Checks (DRC) should be used to flag potentially incorrect connections.
        """
        # No-connect pins cannot connect to anything
        if self == PinType.NO_CONNECT or other == PinType.NO_CONNECT:
            return False

        # All other pin types can connect to each other
        return True


class Pin:
    """
    Minimal Pin class for net connectivity.

    - No dedicated geometry fields in the constructor
      (x, y, length, orientation).
    - We do accept **kwargs so geometry can be passed (and ignored by core),
      but stored for schematic export.
    """

    def __init__(
        self, name: str, num: str, func: Union[str, PinType], unit: int = 1, **kwargs
    ):
        self.name = name
        self.num = num  # e.g. "1", "2", ...
        # Convert string pin type to enum if needed
        self.func = PinType.from_string(func) if isinstance(func, str) else func
        self.unit = unit

        # The Net currently connected (None if unconnected)
        self.net: Optional[Net] = None

        # Set by no_connect() when this pin is explicitly documented as
        # intentionally unconnected (renders a KiCad no-connect marker at
        # schematic-generation time). Mutually exclusive with self.net --
        # see no_connect() and connect_to_net().
        self._no_connect: bool = False
        self._no_connect_reason: Optional[str] = None

        # Assigned by the parent Component
        self._component: "Component" = None
        self._component_pin_id: int = None

        # Store geometry or other unexpected fields in a private dict:
        self._geometry = {}

        # If geometry keys were provided, keep them for schematic generation
        for key in ("x", "y", "length", "orientation"):
            # default to 0 if not present
            self._geometry[key] = kwargs.get(key, 0)

    @property
    def x(self) -> float:
        """Geometry helper, returns 0 if not set."""
        return self._geometry.get("x", 0)

    @property
    def y(self) -> float:
        return self._geometry.get("y", 0)

    @property
    def length(self) -> float:
        return self._geometry.get("length", 0)

    @property
    def orientation(self) -> float:
        return self._geometry.get("orientation", 0)

    @property
    def connected(self) -> bool:
        """Return True if this pin is connected to a net."""
        return self.net is not None

    @property
    def is_no_connect(self) -> bool:
        """Return True if this pin has been marked no_connect()."""
        return self._no_connect

    @property
    def no_connect_reason(self) -> Optional[str]:
        """The reason string passed to no_connect(), if any."""
        return self._no_connect_reason

    def no_connect(self, reason: Optional[str] = None) -> "Pin":
        """
        Mark this pin as intentionally left unconnected.

        Generates a real KiCad no-connect marker ((no_connect (at x y)))
        at this pin's exact, rotation/mirror-aware position when the
        schematic is generated -- see
        `SchematicWriter._add_no_connect_markers` (schematic_writer.py).
        This suppresses kicad-cli ERC's `pin_not_connected` finding for
        this pin without needing any downstream ERC-based post-processing.

        Args:
            reason: Optional human-readable note (e.g. a datasheet
                citation) documenting WHY the pin is left unconnected.
                Not written into the KiCad file -- it stays in the Python
                source as the readable record, same as any other comment.

        Returns:
            Pin: Self, for chaining.

        Raises:
            ComponentError: If this pin is already connected to a net --
                a pin cannot be both wired and marked no-connect. Mark the
                pin no_connect() BEFORE ever doing `pin += net`, or not at
                all.
        """
        if self.net is not None:
            raise ComponentError(
                f"Cannot mark pin '{self.name}' (#{self.num}) of "
                f"{self._component.ref if self._component else '?'} as "
                f"no_connect(): it is already connected to net "
                f"'{self.net.name}'. A pin cannot be both wired and "
                f"no-connect."
            )
        self._no_connect = True
        self._no_connect_reason = reason
        context_logger.debug(
            "Pin marked no_connect",
            component="PIN",
            pin_name=self.name,
            pin_number=self.num,
            reason=reason,
        )
        return self

    def connect_to_net(self, net: Net):
        """
        If already on another net, remove from old net. Then join the new net.

        Args:
            net: The Net to connect this pin to

        Raises:
            ValueError: If connecting would create an invalid pin type combination
            ComponentError: If this pin was already marked no_connect()
        """
        if self._no_connect:
            raise ComponentError(
                f"Cannot connect pin '{self.name}' (#{self.num}) of "
                f"{self._component.ref if self._component else '?'} to net "
                f"'{net.name}': it is already marked no_connect("
                f"reason={self._no_connect_reason!r}). A pin cannot be both "
                f"no-connect and wired."
            )
        # Check pin type compatibility if there are other pins on the net
        if net._pins:
            other_pin = next(iter(net._pins))
            # Check compatibility in both directions
            if not (
                self.func.can_connect_to(other_pin.func)
                or other_pin.func.can_connect_to(self.func)
            ):
                raise ValueError(
                    f"Cannot connect {self.func} pin to {other_pin.func} pin"
                )
        if self.net is not net:
            context_logger.debug(
                "Pin connecting to net",
                component="PIN",
                pin_name=self.name,
                pin_number=self.num,
                old_net=self.net.name if self.net else None,
                new_net=net.name,
            )
            # Remove from old net
            if self.net is not None:
                self.net._pins.discard(self)

            # Attach the new net
            self.net = net
            net._pins.add(self)

    def __iadd__(self, other):
        """
        Support pin += net, or pin += pin.

        Args:
            other: Either a Net or another Pin to connect to

        Returns:
            Pin: Self, for chaining

        Raises:
            TypeError: If other is not a Net or Pin
            ValueError: If pin types are incompatible
        """
        from .pin import Pin as PinClass

        if isinstance(other, Net):
            self.connect_to_net(other)
        elif isinstance(other, PinClass):
            # Validate pin type compatibility in both directions
            if not (
                self.func.can_connect_to(other.func)
                or other.func.can_connect_to(self.func)
            ):
                raise ValueError(f"Cannot connect {self.func} pin to {other.func} pin")

            # Pin-to-pin logic
            if self.net is None and other.net is None:
                new_net = Net()
                self.connect_to_net(new_net)
                other.connect_to_net(new_net)
            elif self.net is None and other.net is not None:
                self.connect_to_net(other.net)
            elif self.net is not None and other.net is None:
                other.connect_to_net(self.net)
            else:
                # Both pins have nets; unify them
                if self.net != other.net:
                    # Validate all pins in both nets are compatible
                    all_pins = list(self.net._pins) + list(other.net._pins)
                    for p1 in all_pins:
                        for p2 in all_pins:
                            if p1 != p2 and not (
                                p1.func.can_connect_to(p2.func)
                                or p2.func.can_connect_to(p1.func)
                            ):
                                raise ValueError(
                                    f"Cannot merge nets: incompatible pin types "
                                    f"{p1.func} and {p2.func}"
                                )

                    # All pins are compatible, proceed with merge
                    for p in list(other.net._pins):
                        p.connect_to_net(self.net)
        else:
            raise TypeError(f"Cannot add {type(other)} to Pin.")
        return self

    def __repr__(self):
        comp_ref = self._component.ref if self._component else "?"
        net_name = self.net.name if self.net else "None"
        return f"Pin({self.name} of {comp_ref}, net={net_name})"

    def to_dict(self) -> dict:
        """Convert pin to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "num": self.num,
            "func": self.func.value,  # Convert PinType to string
            "unit": self.unit,
            "net": self.net.name if self.net else None,
            "component": self._component.ref if self._component else None,
            "geometry": self._geometry,
            "no_connect": self._no_connect,
            "no_connect_reason": self._no_connect_reason,
        }

    def __json__(self) -> dict:
        """Alternative JSON serialization method."""
        return self.to_dict()
