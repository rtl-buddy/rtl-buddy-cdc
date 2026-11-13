"""Observability of netlist cells (rtl-buddy-cdc#338).

A cell whose outputs can reach no observable point of the design cannot
cause a CDC failure: whatever goes metastable or glitches in it is never
seen. Yosys ``opt_clean`` deletes exactly these cells, so a netlist
written without it used to get different verdicts from the same design
written with it (an unloaded ``$not`` of a clock fired CDC-008; the
loop-variable register ``proc`` leaves for a ``for`` loop in a reset
branch fired CDC-004 and CDC-011). The analyzer now removes them itself,
so its verdict does not depend on whether the flow ran ``opt_clean``.

What is live
~~~~~~~~~~~~

Backward reachability from the *observation points*:

- every bit of an ``output`` / ``inout`` port of the module;
- every bit of a netname carrying a truthy ``keep`` attribute;
- every input pin of a cell that is not a known pure Yosys cell: a
  blackbox / greybox / library / hierarchical instance (its pins are
  read through the port directions of the blackbox definition when the
  netlist carries it, otherwise *every* pin counts as an input), a
  memory, a formal or ``$print`` cell, any cell type this module does
  not list — and any cell with a truthy ``keep`` attribute.

A known pure cell (a flop, a latch, a coarse or gate-level combinational
cell, see :func:`pure_cell_outputs`) is live iff one of its output bits
is live, and then all of its input pins are live: ``D``, ``EN``, the
reset pins and the clock pin alike, so the clock network of a live flop
is live.

A flop is therefore live only if its ``Q`` reaches an observation point
(possibly through other live flops). A flop whose ``Q`` is unread, or
read only by its own next-state logic, is dead together with its input
cone, and a crossing into it is not reported — the same flop
``opt_clean`` deletes. Its fan-in stays live if anything else reads it.

Conservatism
~~~~~~~~~~~~

Liveness only ever removes cells it can prove unobservable: unknown cell
types are observation points, never pruned, and a module with no
observation point at all (no output port, no instance, no ``keep``) is a
fragment rather than a design that computes nothing, so it is analysed
whole. The pass is linear in the number of cell pins.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping

from rtl_buddy_cdc.cells import GATE_LOGIC_TYPES, MUX_PINS, parse_gate_storage_cell
from rtl_buddy_cdc.flops import FF_CELL_TYPES
from rtl_buddy_cdc.netlist import Bit, Cell, Module

_Q = frozenset({"Q"})
_Y = frozenset({"Y"})

# Coarse Yosys cells with no side effect, keyed to their output pins. A
# type missing here is never pruned.
_COARSE_PURE: dict[str, frozenset[str]] = {
    **{t: _Q for t in FF_CELL_TYPES},
    **{t: _Q for t in ("$ff", "$dlatch", "$adlatch", "$dlatchsr", "$sr")},
    **{
        t: _Y
        for t in (
            "$buf",
            "$pos",
            "$neg",
            "$not",
            "$and",
            "$or",
            "$xor",
            "$xnor",
            "$reduce_and",
            "$reduce_or",
            "$reduce_xor",
            "$reduce_xnor",
            "$reduce_bool",
            "$logic_not",
            "$logic_and",
            "$logic_or",
            "$shl",
            "$shr",
            "$sshl",
            "$sshr",
            "$shift",
            "$shiftx",
            "$lt",
            "$le",
            "$eq",
            "$ne",
            "$eqx",
            "$nex",
            "$ge",
            "$gt",
            "$add",
            "$sub",
            "$mul",
            "$div",
            "$mod",
            "$divfloor",
            "$modfloor",
            "$pow",
            "$mux",
            "$pmux",
            "$bmux",
            "$demux",
            "$bwmux",
            "$bweqx",
            "$tribuf",
            "$slice",
            "$concat",
            "$lut",
            "$sop",
            "$macc",
            "$macc_v2",
        )
    },
    "$alu": frozenset({"X", "Y", "CO"}),
    "$fa": frozenset({"X", "Y"}),
    "$lcu": frozenset({"CO"}),
}

_GATE_PURE: frozenset[str] = (
    GATE_LOGIC_TYPES
    | frozenset(t for t in MUX_PINS if t.startswith("$_"))
    | frozenset({"$_BUF_", "$_NOT_", "$_TBUF_"})
)


def pure_cell_outputs(cell_type: str) -> frozenset[str] | None:
    """Output pins of a known side-effect-free Yosys cell type, or
    ``None`` when the type is not one (and so is never pruned)."""
    pins = _COARSE_PURE.get(cell_type)
    if pins is not None:
        return pins
    if cell_type in _GATE_PURE:
        return _Y
    if cell_type == "$_FF_" or parse_gate_storage_cell(cell_type) is not None:
        return _Q
    return None


def _truthy(raw: str | None) -> bool:
    """A Yosys attribute value read as a flag (``"1"``, ``"00…01"``,
    ``"true"``)."""
    if raw is None:
        return False
    return any(ch not in "0" for ch in str(raw))


def live_cells(
    module: Module, blackboxes: Mapping[str, Module] | None = None
) -> frozenset[str] | None:
    """Names of the cells of ``module`` that can affect an observation
    point (see the module docstring), or ``None`` when every cell is
    live — nothing is dead, or ``module`` has no observation point.

    ``blackboxes`` supplies the port directions of instance cells; an
    instance of a module not in it has every pin treated as an input.
    """
    roots: list[Bit] = []
    for port in module.ports.values():
        if port.direction != "input":
            roots.extend(port.bits)
    for net in module.netnames.values():
        if _truthy(net.attributes.get("keep")):
            roots.extend(net.bits)

    # Driver index over the prunable cells only: a bit driven by an
    # observation-point cell needs nothing more.
    drivers: dict[int, list[Cell]] = {}
    inputs: dict[str, list[Bit]] = {}
    observed: set[str] = set()
    for cell in module.cells.values():
        outs = pure_cell_outputs(cell.type)
        if outs is None or _truthy(cell.attributes.get("keep")):
            observed.add(cell.name)
            roots.extend(_instance_inputs(cell, blackboxes))
            continue
        ins: list[Bit] = []
        for pin, bits in cell.connections.items():
            if pin in outs:
                for b in bits:
                    if isinstance(b, int):
                        drivers.setdefault(b, []).append(cell)
            else:
                ins.extend(bits)
        inputs[cell.name] = ins

    if not any(isinstance(b, int) for b in roots):
        return None

    live: set[str] = set(observed)
    seen: set[int] = set()
    stack = [b for b in roots if isinstance(b, int)]
    while stack:
        bit = stack.pop()
        if bit in seen:
            continue
        seen.add(bit)
        for cell in drivers.get(bit, ()):
            if cell.name in live:
                continue
            live.add(cell.name)
            stack.extend(b for b in inputs[cell.name] if isinstance(b, int))
    if len(live) == len(module.cells):
        return None
    return frozenset(live)


def _instance_inputs(cell: Cell, blackboxes: Mapping[str, Module] | None) -> list[Bit]:
    """The input-side bits of an observation-point cell: its input and
    inout pins when the netlist defines the instantiated module, else
    every pin."""
    definition = (blackboxes or {}).get(cell.type)
    out: list[Bit] = []
    for pin, bits in cell.connections.items():
        port = definition.ports.get(pin) if definition is not None else None
        if port is not None and port.direction == "output":
            continue
        out.extend(bits)
    return out


def prune_dead_cells(
    module: Module, blackboxes: Mapping[str, Module] | None = None
) -> Module:
    """``module`` without the cells :func:`live_cells` proves dead.

    Ports and netnames are kept as they are (a netname whose driver was
    pruned is simply undriven, like after ``opt_clean``); the live cells
    keep every connection, and by construction everything that drives a
    live cell is live too. Returns ``module`` itself when nothing is
    dead.
    """
    live = live_cells(module, blackboxes)
    if live is None:
        return module
    return dataclasses.replace(
        module,
        cells={name: c for name, c in module.cells.items() if name in live},
    )
