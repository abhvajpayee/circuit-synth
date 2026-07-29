"""
Core circuit primitives and utilities
"""

from .circuit import Circuit
from .component import Component
from .component_dictionary import (
    export_component_dictionary,
    write_component_dictionary_json,
)
from .component_replacement import (
    ReplacementResult,
    find_replaceable_components,
    replace_components,
    replace_multiple,
)
from .decorators import circuit
from .dependency_injection import (
    DependencyContainer,
    IDependencyContainer,
    ServiceLocator,
)
from .exception import CircuitSynthError, ComponentError, ValidationError
from .bus import Bus
from .cap_bank import cap_bank
from .resistor_bank import resistor_bank
from .net import Net
from .pin import Pin

__all__ = [
    "Bus",
    "Circuit",
    "Component",
    "Net",
    "Pin",
    "circuit",
    "cap_bank",
    "resistor_bank",
    "ComponentError",
    "ValidationError",
    "CircuitSynthError",
    "DependencyContainer",
    "ServiceLocator",
    "IDependencyContainer",
    # Component replacement
    "replace_components",
    "replace_multiple",
    "find_replaceable_components",
    "ReplacementResult",
    # Component documentation-metadata export
    "export_component_dictionary",
    "write_component_dictionary_json",
]
