"""Existing-project pin -> net connectivity, read from a KiCad-exported
netlist rather than reconstructed geometrically (wayfinder #63).

Why this module exists
----------------------
The reference preallocator (``reference_preallocator.py``, wayfinder #58/#59)
needs one thing from an already-generated KiCad project: for every component,
which net is each of its pins on. That connectivity is what its per-component
signature is computed from, and therefore what decides whether a component
keeps its stable reference designator across an incremental sync -- which in
turn decides whether manually-placed PCB footprints keep their anchor.

Until now that connectivity was *reconstructed* by
``APISynchronizer._get_pin_labels()``: compute each pin's absolute position
from symbol geometry, then search for a label or power symbol within a 0.5mm
tolerance. That reconstruction is the shared root cause of an entire family of
sync bugs -- wayfinder #55, #57 and #61 were each a different way for a pin's
real connectivity to be invisible to a proximity search (a bus graphic dropped
on load, a dual label matched at only one of its two positions, a pin wired
into the middle of a shared rail with no marker of its own at all). Each was
fixed by adding another special case to the search.

KiCad already computes this connectivity authoritatively -- it must, in order
to export a netlist at all. ``kicad-cli sch export netlist`` states
``net -> [(ref, pin), ...]`` directly, with no geometry, no tolerance, and no
special cases. This module reads that.

Empirical basis for adopting it (measured on the real ``acquisition_mcu``
project, 189 components / 947 pin-connections / 312 nets, kicad-cli 10.0.4):

- Against the current, fully-patched geometric path (with #61's wire-graph
  fix and #59's alias normalization applied), the two agree on **all 947**
  pin-connections -- zero missing, zero extra, zero disagreements. Adopting
  the netlist is therefore behavior-preserving on real input, not a rewrite
  with new semantics.
- With #61's wire-graph fix disabled, the geometric path misses **57**
  pin-connections that this module resolves correctly with no rail-specific
  code -- i.e. the netlist structurally subsumes that fix rather than
  reimplementing it.

Deliberately NOT a replacement for ``_get_pin_labels()``
--------------------------------------------------------
``_get_pin_labels()`` has a second consumer with a fundamentally different
need: ``APISynchronizer._reconcile_component_pins()`` uses it to obtain the
actual, editable ``Label`` *object* at each pin, so sync can retext, move or
delete that label in place. A netlist contains net names only -- no geometry,
no object identity -- so it can never serve that role. ``_get_pin_labels()``
therefore stays, and this module is layered in front of it only for the
preallocator's read-only signature extraction, with the geometric path kept
as an automatic fallback (see ``load_project_connectivity``).

Netlist source matters
----------------------
Hierarchy preservation is a property of *which exporter produced the file*,
not of the format. ``kicad-cli`` (Eeschema 10.0.4) emits a full nested
``(sheetpath (names "/ComputeSetup/WiFi/"))`` per component and hierarchical
net names. circuit-synth's own ``NetlistExporter`` (``tool "Circuit-Synth
Exporter v0.1.0"``) flattens every sheetpath to ``"/"``. This module always
exports a fresh netlist with ``kicad-cli`` and never consumes a pre-existing
``.net`` file sitting in a project directory, which may have come from either.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Optional

import sexpdata

logger = logging.getLogger(__name__)

# KiCad synthesizes a placeholder net name of this shape for a pin that is
# genuinely on no net at all (including explicit no-connects), e.g.
# "unconnected-(U1-NC-PadK16)". It is a naming artifact, not a net.
_UNCONNECTED_RE = re.compile(r"^unconnected-\(.*\)$")

# How long to allow kicad-cli to run before giving up and falling back.
_EXPORT_TIMEOUT_S = 120


def _sym(value):
    """sexpdata yields bare atoms as Symbol; normalize to str for comparison."""
    return str(value) if isinstance(value, sexpdata.Symbol) else value


def _children(node, tag: str):
    """Yield direct child lists of ``node`` whose head atom equals ``tag``."""
    if not isinstance(node, list):
        return
    for item in node:
        if isinstance(item, list) and item and _sym(item[0]) == tag:
            yield item


def _child(node, tag: str):
    for item in _children(node, tag):
        return item
    return None


def _value(node, tag: str):
    """Return the single value of a ``(tag "value")`` child, or None."""
    item = _child(node, tag)
    if item is None or len(item) < 2:
        return None
    return _sym(item[1])


def normalize_net_name(raw_name: Optional[str]) -> Optional[str]:
    """Translate a KiCad exported net name into circuit-synth's own net-name
    vocabulary (the bare ``Net.name`` the Python side uses).

    Two transformations, both required for the two sides to be comparable:

    1. **Strip the hierarchical sheet path.** KiCad qualifies a net by the
       sheet it lives in: a net local to a sub-sheet exports as
       ``/ComputeSetup/MCU/VCAP0``, and even a project-global net exports as
       ``/DGND``. circuit-synth's ``Net.name`` is always the bare name
       (``VCAP0``, ``DGND``), so the path prefix is removed.

       This matches the vocabulary the geometric path produced too (it read
       bare on-schematic label text), so it introduces no new ambiguity
       relative to the previous behavior. It does mean two same-named nets
       local to two *different* sheets collapse to one token -- a
       pre-existing property of the matcher, not something introduced here,
       and measured as not occurring at all on the real project (0 basename
       collisions across 312 nets). Sheet-scoped matching in
       ``reference_preallocator`` further limits the blast radius.

    2. **Map KiCad's ``unconnected-(...)`` placeholder to ``None``**, so a
       genuinely unwired pin yields the same ``("NC",)`` signature token a
       blank label used to.

    Note that circuit-synth's own auto-generated net names (``N$1``) survive
    this unchanged apart from losing their path prefix, so
    ``reference_preallocation.AUTO_NET_RE`` still recognizes them and the
    peer-based ("ANON") resolution keeps working.
    """
    if not raw_name:
        return None
    if _UNCONNECTED_RE.match(raw_name):
        return None
    name = raw_name
    if name.startswith("/"):
        name = name.rsplit("/", 1)[-1]
    return name or None


def parse_netlist_connectivity(
    netlist_path,
) -> Optional[Dict[str, Dict[str, Optional[str]]]]:
    """Parse a KiCad ``kicadsexpr`` netlist into ``{ref: {pin: net_name}}``.

    Reference designators are unique across a whole KiCad project, so a flat
    ref-keyed mapping is unambiguous and no sheet keying is needed here --
    the caller already knows which sheet each component is on.

    An unconnected pin is recorded with a ``None`` net rather than omitted,
    so the pin still contributes its ``("NC",)`` token to the component's
    signature.

    Returns ``None`` (never raises) if the file is missing or unparseable, so
    a caller can fall back to another connectivity source.
    """
    path = Path(netlist_path)
    if not path.is_file():
        logger.debug("Netlist not found at %s", path)
        return None

    try:
        data = sexpdata.loads(path.read_text())
    except Exception:
        logger.debug("Could not parse netlist %s", path, exc_info=True)
        return None

    nets_section = _child(data, "nets")
    if nets_section is None:
        logger.debug("Netlist %s has no (nets ...) section", path)
        return None

    connectivity: Dict[str, Dict[str, Optional[str]]] = {}
    for net in _children(nets_section, "net"):
        net_name = normalize_net_name(_value(net, "name"))
        for node in _children(net, "node"):
            ref = _value(node, "ref")
            pin = _value(node, "pin")
            if ref is None or pin is None:
                continue
            connectivity.setdefault(str(ref), {})[str(pin)] = net_name

    logger.debug(
        "Parsed netlist %s: %d components, %d pin-connections",
        path,
        len(connectivity),
        sum(len(v) for v in connectivity.values()),
    )
    return connectivity


def export_netlist(schematic_path, output_path=None) -> Optional[Path]:
    """Export a fresh netlist for ``schematic_path`` using ``kicad-cli``.

    Always exports fresh rather than reusing any ``.net`` already sitting in
    the project directory: such a file may have been written by
    circuit-synth's own exporter, which flattens all sheetpaths (see this
    module's docstring) and would silently degrade sheet-aware matching.

    ``kicad-cli sch export netlist`` only reads the schematic; it never
    modifies the project. The output is written to ``output_path`` (a caller-
    supplied temporary location by default), never into the project.

    Returns the netlist path, or ``None`` if kicad-cli is unavailable or the
    export fails -- callers fall back to geometric extraction.
    """
    schematic = Path(schematic_path)
    if not schematic.is_file():
        logger.debug("Schematic not found for netlist export: %s", schematic)
        return None

    if shutil.which("kicad-cli") is None:
        logger.debug("kicad-cli not on PATH; cannot export netlist")
        return None

    if output_path is None:
        handle = tempfile.NamedTemporaryFile(suffix=".net", delete=False)
        handle.close()
        output_path = handle.name
    output = Path(output_path)

    try:
        result = subprocess.run(
            [
                "kicad-cli",
                "sch",
                "export",
                "netlist",
                "--format",
                "kicadsexpr",
                "-o",
                str(output),
                str(schematic),
            ],
            capture_output=True,
            text=True,
            timeout=_EXPORT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("kicad-cli netlist export failed for %s", schematic, exc_info=True)
        return None

    if result.returncode != 0 or not output.is_file():
        logger.debug(
            "kicad-cli netlist export returned %s for %s: %s",
            result.returncode,
            schematic,
            (result.stderr or "").strip(),
        )
        return None

    return output


def load_project_connectivity(
    schematic_path,
) -> Optional[Dict[str, Dict[str, Optional[str]]]]:
    """Export and parse a netlist for ``schematic_path`` in one step.

    Returns ``{ref: {pin: net_name}}``, or ``None`` if the netlist could not
    be produced or parsed -- which is a normal, supported outcome (kicad-cli
    absent, an unloadable schematic, a sandboxed environment), and is why the
    caller keeps the geometric path as a fallback rather than treating this
    as fatal.
    """
    netlist_path = export_netlist(schematic_path)
    if netlist_path is None:
        return None
    try:
        return parse_netlist_connectivity(netlist_path)
    finally:
        try:
            Path(netlist_path).unlink()
        except OSError:
            pass
