"""Reset-domain analysis — parallel to :mod:`rtl_buddy_cdc.domain`.

Foundation for the broader RDC (Reset Domain Crossing) work tracked in
issue #107. This module exposes the structural facts the rule pack
needs to reason about resets without each rule re-walking the netlist:

* :class:`ResetSource` — describes the upstream origin of a flop's
  reset pin (top-level port vs. driven by another flop, polarity,
  sync vs. async).
* :class:`ResetDomain` — the per-flop assignment (flop name → reset
  source, or ``None`` when the flop has no reset pin at all).
* :func:`assign_reset_domains` — runs the per-flop walk once and
  returns the full map.

The classifier deliberately stays close to what the Yosys netlist
hands us. Polarity comes straight from the ``$adff*`` / ``$sdff*``
parameter (Yosys preserves it as a multi-bit binary string). Sync vs.
async is derived from the cell type: ``$adff*`` / ``$aldff*`` /
``$dffsr*`` are async, ``$sdff*`` are sync. The reset source is
classified by walking the reset bit back through the standard
backward-fanin: a top-level input port → ``"port"``; a flop's ``Q``
output → ``"inferred"`` (with the driving flop's cell name as the
identifier); a constant → ``"constant"``; anything else (combinational
expression, untracked driver) → ``"comb"``.

Out of scope here, by design:

* The :class:`ResetSource` ``clock`` field (the clock that samples a
  sync reset) is not populated yet — it requires the rule-side context
  to land cleanly. Stubbed for forward-compat; consumers should treat
  ``None`` as "not yet determined".
* Reset-synchronizer recognition lives in a follow-up PR alongside the
  first rule that needs it (RDC-001's recognizer wants the flop +
  clock-domain pair, not just the structural reset shape).
* No rules consume this module yet. The existing CDC-007 walk in
  :mod:`rtl_buddy_cdc.rules` is unchanged; rerooting it onto the new
  data model is a follow-up so the contract change (rule_id, message
  text) can be reviewed independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rtl_buddy_cdc.flops import find_flops
from rtl_buddy_cdc.netlist import Bit, Module

# Cell types with an asynchronous reset pin. Each maps to the pin name
# carrying the reset and the parameter that documents its polarity.
# ``$dffsr`` / ``$dffsre`` are SR-latch flops with separate ``SET`` and
# ``CLR`` async pins; we surface ``CLR`` as the canonical reset to
# match the dominant industry idiom (active-low clear). ``SET`` semantics
# are different enough that we punt on them in this foundation pass —
# the rule pack can layer SET-specific handling on top later.
_ASYNC_RESET_PINS: dict[str, tuple[str, str]] = {
    "$adff": ("ARST", "ARST_POLARITY"),
    "$adffe": ("ARST", "ARST_POLARITY"),
    "$aldff": ("ALOAD", "ALOAD_POLARITY"),
    "$aldffe": ("ALOAD", "ALOAD_POLARITY"),
    "$dffsr": ("CLR", "CLR_POLARITY"),
    "$dffsre": ("CLR", "CLR_POLARITY"),
}

# Cell types with a synchronous reset pin.
_SYNC_RESET_PINS: dict[str, tuple[str, str]] = {
    "$sdff": ("SRST", "SRST_POLARITY"),
    "$sdffe": ("SRST", "SRST_POLARITY"),
    "$sdffce": ("SRST", "SRST_POLARITY"),
}


ResetType = Literal["sync", "async"]
ResetPolarity = Literal["high", "low"]
ResetSourceKind = Literal["port", "inferred", "constant", "comb"]


@dataclass(frozen=True)
class ResetSource:
    """The upstream origin of a flop's reset pin.

    ``name`` identifies the source: a top-level port name for
    ``source="port"``, the driver flop's cell name for
    ``source="inferred"``, the literal constant for
    ``source="constant"``, or an empty string for ``source="comb"``
    (the analyzer doesn't currently summarise combinational reset
    expressions).
    """

    name: str
    polarity: ResetPolarity
    type: ResetType
    source: ResetSourceKind
    # The clock that samples a sync reset, or ``None`` for async
    # resets and for sync resets whose context isn't available yet.
    # Stubbed; populated by the rule-side context in a later PR.
    clock: str | None = None


@dataclass(frozen=True)
class ResetDomain:
    """A flop's reset assignment.

    ``reset`` is ``None`` for plain ``$dff`` / ``$dffe`` cells (no
    reset pin); consumers should treat that as "power-on initialisation
    only".
    """

    flop: str
    reset: ResetSource | None


def assign_reset_domains(module: Module) -> dict[str, ResetDomain]:
    """For each flop in ``module``, classify its reset pin (if any).

    Returns ``{cell_name: ResetDomain}`` covering every flop. Plain
    ``$dff`` / ``$dffe`` cells get a ``ResetDomain`` whose ``reset`` is
    ``None``; reset-bearing cells get a populated :class:`ResetSource`
    derived from the cell type, its polarity parameter, and a one-hop
    walk back through the netlist's bit-drivers table.
    """
    drivers = _bit_drivers(module)
    out: dict[str, ResetDomain] = {}
    for f in find_flops(module):
        ctype = f.cell.type
        if ctype in _ASYNC_RESET_PINS:
            pin, polarity_param = _ASYNC_RESET_PINS[ctype]
            reset_type: ResetType = "async"
        elif ctype in _SYNC_RESET_PINS:
            pin, polarity_param = _SYNC_RESET_PINS[ctype]
            reset_type = "sync"
        else:
            out[f.cell.name] = ResetDomain(flop=f.cell.name, reset=None)
            continue

        bits = f.cell.connections.get(pin, ())
        if not bits:
            # Defensive: a flop typed as reset-bearing but missing the
            # pin connection is malformed. Treat it as "no reset" rather
            # than raising — better to skip than crash on an exotic netlist.
            out[f.cell.name] = ResetDomain(flop=f.cell.name, reset=None)
            continue

        polarity = _polarity_from_param(f.cell.parameters.get(polarity_param, "1"))
        rst_bit = bits[0]
        name, kind = _classify_reset_source(module, rst_bit, drivers)
        out[f.cell.name] = ResetDomain(
            flop=f.cell.name,
            reset=ResetSource(
                name=name,
                polarity=polarity,
                type=reset_type,
                source=kind,
            ),
        )
    return out


# --- helpers ----------------------------------------------------------------


def _bit_drivers(module: Module) -> dict[Bit, tuple[str, str]]:
    """Mirror of :func:`rtl_buddy_cdc.domain._bit_drivers`.

    Built locally so the reset pass doesn't depend on importing from
    :mod:`rtl_buddy_cdc.domain` (which would create a layering tangle
    once the rule pack also wires this in). The map is small and the
    walk is O(cells); the duplication is cheap.
    """
    out: dict[Bit, tuple[str, str]] = {}
    for cell in module.cells.values():
        for port_name in ("Y", "Q"):
            for b in cell.connections.get(port_name, ()):
                if isinstance(b, int):
                    out[b] = (cell.name, port_name)
    return out


def _classify_reset_source(
    module: Module,
    bit: Bit,
    drivers: dict[Bit, tuple[str, str]],
) -> tuple[str, ResetSourceKind]:
    """One-hop classification of the bit driving a reset pin."""
    if not isinstance(bit, int):
        return (str(bit), "constant")
    port = module.port_of_bit(bit)
    if port is not None and port.direction == "input":
        return (port.name, "port")
    drv = drivers.get(bit)
    if drv is None:
        # Untracked driver (e.g. inout or a cell we don't recognise);
        # the safest classification is "comb" — the rule pack can refuse
        # to reason about such resets rather than misclassify them.
        return ("", "comb")
    cell_name, out_port = drv
    if out_port == "Q":
        return (cell_name, "inferred")
    return ("", "comb")


def _polarity_from_param(raw: str) -> ResetPolarity:
    """Decode Yosys' parameter polarity string.

    Yosys emits parameters as binary strings (``'1'``, ``'0'``, or the
    32-bit padded form like ``'00000000000000000000000000000001'``).
    A trailing ``1`` bit means active-high; anything else (including
    the empty string) is treated as active-low — the conservative
    default for the active-low reset idiom that dominates the field.
    """
    if not raw:
        return "low"
    return "high" if raw[-1] == "1" else "low"
