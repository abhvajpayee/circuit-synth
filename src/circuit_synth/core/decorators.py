# FILE: src/circuit_synth/core/decorators.py

from functools import wraps

_CURRENT_CIRCUIT = None
# Track how many times each function has been called to auto-increment instance names
_CIRCUIT_CALL_COUNTERS = {}


def get_current_circuit():
    return _CURRENT_CIRCUIT


def set_current_circuit(circuit):
    global _CURRENT_CIRCUIT
    _CURRENT_CIRCUIT = circuit


def reset_circuit_call_counters():
    """Reset the call counters for auto-incrementing circuit instance names."""
    global _CIRCUIT_CALL_COUNTERS
    _CIRCUIT_CALL_COUNTERS = {}


def circuit(_func=None, *, name=None, comments=True):
    """
    Decorator that can be used in three ways:
      1) @circuit
         def my_circuit(...):
             ...
      2) @circuit(name="someCircuitName")
         def my_circuit(...):
             ...
      3) @circuit(name="someCircuitName", comments=False)
         def my_circuit(...):
             ...

    Creates a new Circuit object, optionally using `name` as the circuit name.
    If comments=True (default), the function's docstring will be added as a
    text annotation on the generated schematic.
    If there's an existing current circuit (the "parent"), the new circuit is
    attached to the parent as a subcircuit. Then references are finalized
    before returning the child circuit.

    NOTE on wayfinder #58 (incremental-sync reference preallocation): an
    earlier version of this fix tried to defer this eager, per-decorated-
    function finalize_references() call so that Circuit.generate_kicad_project()
    could preallocate connectivity-matched references BEFORE any
    counter-based assignment happened anywhere in the tree. That turned out
    to be incompatible with existing, load-bearing usage: many callers
    (including several tests) construct a circuit via a decorated function
    and inspect real, finalized `.ref` values immediately, without ever
    calling generate_kicad_project(). Since a nested @circuit call's own
    wrapper finalizes its circuit's references as soon as that call
    returns -- long before the outermost @circuit function itself returns
    -- there is no way to defer this eagerness generally without breaking
    that pattern. The eager call is therefore kept exactly as before;
    Circuit.remap_preallocated_references() (core/circuit.py) instead
    performs a conflict-safe *rename* pass after the fact, achieving the
    same observable outcome (matched components end up holding their old,
    stable reference; only genuinely new components keep a freshly
    counter-generated one) without requiring this call to move. See that
    method's docstring, and core/reference_preallocation.py, for the full
    design.
    """

    def _decorator(func):
        @wraps(func)
        def _wrapper(*args, **kwargs):
            from .circuit import Circuit  # local import to avoid circular import

            global _CURRENT_CIRCUIT
            global _CIRCUIT_CALL_COUNTERS

            parent_circuit = _CURRENT_CIRCUIT

            # Reset call counters when starting a new top-level circuit
            if parent_circuit is None:
                _CIRCUIT_CALL_COUNTERS = {}
            # Capture docstring to use as circuit description
            docstring = func.__doc__ or ""  # or you could .strip() if you prefer

            # Check if enable_comments decorator was used
            use_comments = comments or getattr(func, "_enable_comments", False)

            # Auto-increment instance name if no explicit name provided and inside parent circuit
            circuit_name = name
            if circuit_name is None:
                base_name = func.__name__
                if parent_circuit is not None:
                    # Inside a parent circuit - auto-increment the instance name
                    # Track calls per function name
                    if base_name not in _CIRCUIT_CALL_COUNTERS:
                        _CIRCUIT_CALL_COUNTERS[base_name] = 0
                    _CIRCUIT_CALL_COUNTERS[base_name] += 1
                    circuit_name = f"{base_name}_{_CIRCUIT_CALL_COUNTERS[base_name]}"
                else:
                    # Top-level circuit - use function name as-is
                    circuit_name = base_name

            c = Circuit(
                name=circuit_name,
                description=docstring,
                auto_comments=use_comments,
            )

            # Store reference to the circuit function for source rewriting
            c._circuit_func = func

            # Link it as a subcircuit if there's a parent
            if parent_circuit is not None:
                parent_circuit.add_subcircuit(c)

            old_circuit = _CURRENT_CIRCUIT
            _CURRENT_CIRCUIT = c

            try:
                func(*args, **kwargs)
                c.finalize_references()
            finally:
                _CURRENT_CIRCUIT = old_circuit

            return c

        return _wrapper

    if _func is None:
        # @circuit(name=...)
        return _decorator
    else:
        # @circuit
        return _decorator(_func)


def enable_comments(func):
    """
    Decorator that enables automatic docstring extraction for circuit functions.
    This is equivalent to using @circuit(comments=True) but provides a more explicit API.

    Usage:
        @enable_comments
        @circuit(name="my_circuit")
        def my_circuit():
            '''This docstring becomes a schematic annotation.'''
            pass
    """
    # This decorator just marks the function for comment extraction
    # The actual logic is handled by the @circuit decorator
    func._enable_comments = True
    return func
