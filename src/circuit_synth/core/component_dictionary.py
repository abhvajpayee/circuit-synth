"""
Generic component-documentation-metadata export.

Walks a circuit's full hierarchy (itself + all subcircuits, recursively) and
collects the `doc=` metadata dict authored on any `Component` (see
`Component.doc` in `component.py`) into a single flat dictionary keyed by
final reference designator. Components with no `doc` metadata are omitted --
this is an export of *authored documentation*, not a full BOM.

Intended for any circuit-synth project (not board-specific): a project's own
tooling can call `export_component_dictionary(circuit)` to get a JSON-ready
dict, then render it however it likes (e.g. a Markdown component dictionary,
sorted ascending by reference designator).
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Union

from ._logger import context_logger


def _ref_sort_key(ref: str):
    """Sort key for reference designators: alphabetic prefix, then numeric
    suffix ascending (R2 before R10, C1 before R1)."""
    m = re.match(r"^([A-Za-z_]+)(\d+)$", ref)
    if m:
        return (m.group(1), int(m.group(2)))
    return (ref, 0)


def export_component_dictionary(circuit: "Any", *, _sheet_path: str = "") -> Dict[str, Dict[str, Any]]:
    """Recursively collect documentation metadata for every component in
    `circuit` and its subcircuits that has `doc` metadata set.

    Args:
        circuit: A `circuit_synth.core.circuit.Circuit` instance (the root of
            the hierarchy to export -- typically the top-level circuit
            returned by an `@circuit`-decorated function).

    Returns:
        Dict keyed by final reference designator (e.g. "R12", "U3"), each
        value a dict with keys: `ref`, `symbol`, `value`, `sheet` (the
        hierarchical sheet path this component was found on, "/"-joined),
        and `doc` (the exact dict passed to `Component(doc=...)`).

        The returned dict iterates in insertion (hierarchy-traversal) order;
        callers that want ascending-by-reference-designator output should
        sort the keys themselves, e.g.:

            sorted(export_component_dictionary(c).keys(),
                   key=lambda r: __import__("re").match(r"([A-Za-z_]+)(\\d+)", r).groups())

        (`_ref_sort_key` in this module implements exactly that and is used
        by `write_component_dictionary_json` below.)
    """
    result: Dict[str, Dict[str, Any]] = {}
    sheet_name = getattr(circuit, "name", None) or ""
    this_path = f"{_sheet_path}/{sheet_name}" if _sheet_path else sheet_name

    for ref, comp in getattr(circuit, "_components", {}).items():
        doc = getattr(comp, "doc", None)
        if not doc:
            continue
        result[ref] = {
            "ref": ref,
            "symbol": comp.symbol or "",
            "value": comp.value or "",
            "sheet": this_path,
            "doc": dict(doc),
        }

    for sub in getattr(circuit, "_subcircuits", []):
        sub_result = export_component_dictionary(sub, _sheet_path=this_path)
        result.update(sub_result)

    return result


def write_component_dictionary_json(
    circuit: "Any", filename: Union[str, Path]
) -> Path:
    """Export `circuit`'s component dictionary (see `export_component_dictionary`)
    and write it as JSON to `filename`, sorted ascending by reference
    designator (prefix letters, then numeric suffix). Returns the path
    written."""
    data = export_component_dictionary(circuit)
    ordered = {ref: data[ref] for ref in sorted(data.keys(), key=_ref_sort_key)}

    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ordered, f, indent=2, default=str)

    context_logger.info(
        f"Component dictionary written to {path} ({len(ordered)} documented components)",
        component="COMPONENT_DICTIONARY",
    )
    return path
