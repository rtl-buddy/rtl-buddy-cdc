"""Assign each flop to a clock domain and find register-to-register CDC paths.

The clock-domain assignment walks each flop's ``CLK`` net backward
through buffers, inverters, integrated clock gates, and clock-divider
flops to find the originating top-level clock port. Direct
port-to-flop wiring is the trivial case; everything else uses the
small heuristic in :func:`trace_clock_root`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from rtl_buddy_cdc.flops import FF_CELL_TYPES, Flop, find_flops
from rtl_buddy_cdc.netlist import Bit, Module


@dataclass(frozen=True)
class FlopDomain:
    flop: Flop
    clock: str | None  # top-level port name, or None if untraceable


@dataclass(frozen=True)
class Crossing:
    """A register-to-register fanout path that crosses domains.

    ``src_flop`` drives ``dst_flop`` through ``min_hops`` combinational
    cells in the shortest path between them. ``min_hops == 0`` means a
    direct flop-to-flop wire — the classic pre-synchronizer first
    stage. ``width`` is the number of distinct destination D bits
    reachable from the source flop on this crossing (>1 means a bus
    crossing).
    """

    src_flop: Flop
    src_clock: str
    dst_flop: Flop
    dst_clock: str
    min_hops: int
    width: int


# Cell types that act as a transparent buffer on the clock network —
# inversion is irrelevant to clock-domain identity.
_BUFFER_TYPES: frozenset[str] = frozenset(
    {"$not", "$logic_not", "$buf", "$pos", "$reduce_bool", "$_BUF_", "$_NOT_"}
)
# Cell types that combine two clock-network signals (one is the actual
# clock, the other an enable) — common ICG shapes.
_GATE_TYPES: frozenset[str] = frozenset(
    {"$and", "$or", "$logic_and", "$logic_or", "$xor", "$xnor"}
)
# Cell types that select one of several clock candidates.
_MUX_TYPES: frozenset[str] = frozenset({"$mux", "$pmux"})


def _bit_drivers(module: Module) -> dict[Bit, tuple[str, str]]:
    """Map each net bit to the ``(cell_name, output_port)`` that drives it.

    Yosys combinational primitives emit on ``Y``; FFs emit on ``Q``.
    Top-level input ports are not represented here — callers fall back
    to :meth:`Module.port_of_bit` for those.
    """
    out: dict[Bit, tuple[str, str]] = {}
    for cell in module.cells.values():
        for port_name in ("Y", "Q"):
            for b in cell.connections.get(port_name, ()):
                if isinstance(b, int):
                    out[b] = (cell.name, port_name)
    return out


def trace_clock_root(
    module: Module,
    bit: Bit,
    drivers: dict[Bit, tuple[str, str]] | None = None,
    max_depth: int = 16,
) -> str | None:
    """Resolve a CLK net bit to the top-level port that ultimately drives it.

    Handles common clock-network shapes:

    - direct top-level port (the trivial case),
    - single-input buffers and inverters (transparent),
    - two-input clock gates (``$and``/``$or``…) where exactly one of
      the inputs traces back to a clock port — the other is treated
      as the gate's enable,
    - clock muxes — both candidate roots are explored, the first one
      that resolves wins (the analyzer can't statically know which
      side ``S`` selects),
    - clock dividers — a flop's ``Q`` is followed back to the flop's
      own ``CLK`` pin, which is the upstream clock root.

    Returns ``None`` when no candidate root resolves within
    ``max_depth`` cells; the caller treats that as "domain unknown".
    """
    if drivers is None:
        drivers = _bit_drivers(module)
    seen: set[Bit] = set()
    return _trace(module, bit, drivers, seen, max_depth)


def _trace(
    module: Module,
    bit: Bit,
    drivers: dict[Bit, tuple[str, str]],
    seen: set[Bit],
    depth: int,
) -> str | None:
    if not isinstance(bit, int) or depth <= 0 or bit in seen:
        return None
    seen.add(bit)

    port = module.port_of_bit(bit)
    if port is not None and port.direction == "input":
        return port.name

    drv = drivers.get(bit)
    if drv is None:
        return None
    cell_name, out_port = drv
    cell = module.cells[cell_name]
    ctype = cell.type

    # Transparent through single-input cells.
    if ctype in _BUFFER_TYPES:
        a = cell.connections.get("A", ())
        if a:
            return _trace(module, a[0], drivers, seen, depth - 1)
        return None

    # Clock gate — look at both inputs; if exactly one resolves to a
    # clock port, that's our root. If both resolve, the cell is
    # combining two clock domains and we (conservatively) pick the
    # first; a stricter check would emit a violation, but we leave
    # that to a future rule.
    if ctype in _GATE_TYPES:
        a = cell.connections.get("A", (None,))
        b = cell.connections.get("B", (None,))
        a_root = _trace(module, a[0], drivers, set(seen), depth - 1) if a else None
        b_root = _trace(module, b[0], drivers, set(seen), depth - 1) if b else None
        return a_root or b_root

    # Clock mux — return whichever side resolves.
    if ctype in _MUX_TYPES:
        for in_port in ("A", "B"):
            in_bits = cell.connections.get(in_port, ())
            if in_bits:
                root = _trace(module, in_bits[0], drivers, set(seen), depth - 1)
                if root is not None:
                    return root
        return None

    # Clock divider — flop Q output. Trace the source flop's CLK back
    # to its root.
    if ctype in FF_CELL_TYPES and out_port == "Q":
        clk_bits = cell.connections.get("CLK", ())
        if clk_bits:
            return _trace(module, clk_bits[0], drivers, seen, depth - 1)

    return None


def assign_domains(module: Module) -> list[FlopDomain]:
    drivers = _bit_drivers(module)
    return [
        FlopDomain(flop=f, clock=trace_clock_root(module, f.clk, drivers))
        for f in find_flops(module)
    ]


def _build_bit_consumers(
    module: Module,
) -> dict[Bit, list[tuple[str, str, int]]]:
    """Map each net bit to the (cell_name, port_name, bit_idx) sites that read it.

    Flop ``CLK`` connections are intentionally excluded: clock pins aren't
    part of the data fanout we're tracing.
    """
    consumers: dict[Bit, list[tuple[str, str, int]]] = defaultdict(list)
    for cell in module.cells.values():
        for port_name, bits in cell.connections.items():
            if port_name == "CLK":
                continue
            for idx, b in enumerate(bits):
                if isinstance(b, int):
                    consumers[b].append((cell.name, port_name, idx))
    return consumers


def find_crossings(module: Module, max_hops: int = 4) -> list[Crossing]:
    """Find every flop→flop path whose endpoints are in different domains.

    The walk starts from each flop's ``Q`` bits and follows readers up to
    ``max_hops`` combinational cells before giving up. Reaching another
    flop's ``D`` pin produces a :class:`Crossing` if the two flops are in
    distinct, known domains.
    """
    domains = {fd.flop.cell.name: fd for fd in assign_domains(module)}
    consumers = _build_bit_consumers(module)
    flop_by_d_bit: dict[Bit, list[Flop]] = defaultdict(list)
    for fd in domains.values():
        for b in fd.flop.d:
            if isinstance(b, int):
                flop_by_d_bit[b].append(fd.flop)

    # Grouped per (src_flop, dst_flop) pair so a multi-bit bus or a fanout
    # that hits the same destination flop on multiple D bits collapses to
    # one Crossing record.
    grouped: dict[tuple[str, str], dict[str, object]] = {}

    for src_fd in domains.values():
        if src_fd.clock is None:
            continue
        # Per source-flop visited set, keyed by net bit. Hop count is the
        # number of *cells* traversed, so we record the minimum hop count
        # at which each bit was first seen.
        seen: dict[Bit, int] = {}
        # frontier: list[(bit, hops_to_here)]
        frontier: list[tuple[Bit, int]] = [
            (b, 0) for b in src_fd.flop.q if isinstance(b, int)
        ]
        for b, h in frontier:
            seen[b] = h

        while frontier:
            next_frontier: list[tuple[Bit, int]] = []
            for bit, hops in frontier:
                # Did we land on any flop's D pin?
                for dst_flop in flop_by_d_bit.get(bit, ()):
                    if dst_flop.cell.name == src_fd.flop.cell.name:
                        continue
                    dst_fd = domains[dst_flop.cell.name]
                    if dst_fd.clock is None or dst_fd.clock == src_fd.clock:
                        continue
                    key = (src_fd.flop.cell.name, dst_flop.cell.name)
                    g = grouped.get(key)
                    if g is None:
                        grouped[key] = {
                            "src_fd": src_fd,
                            "dst_fd": dst_fd,
                            "min_hops": hops,
                            "bits": {bit},
                        }
                    else:
                        g["bits"].add(bit)  # type: ignore[union-attr]
                        if hops < g["min_hops"]:  # type: ignore[operator]
                            g["min_hops"] = hops
                if hops >= max_hops:
                    continue
                # Push the bit through every consumer cell that isn't a
                # flop, propagating to all that cell's output bits.
                for cell_name, _port, _idx in consumers.get(bit, ()):
                    cell = module.cells[cell_name]
                    if cell.type in {"$scopeinfo"}:
                        continue
                    # Skip flops as transit nodes — we already check above
                    # whether we landed on a flop's D pin.
                    from rtl_buddy_cdc.flops import FF_CELL_TYPES

                    if cell.type in FF_CELL_TYPES:
                        continue
                    out_bits = _cell_outputs(cell)
                    for ob in out_bits:
                        if not isinstance(ob, int):
                            continue
                        prev = seen.get(ob)
                        new_hops = hops + 1
                        if prev is None or new_hops < prev:
                            seen[ob] = new_hops
                            next_frontier.append((ob, new_hops))
            frontier = next_frontier

    return [
        Crossing(
            src_flop=g["src_fd"].flop,  # type: ignore[index]
            src_clock=g["src_fd"].clock,  # type: ignore[index]
            dst_flop=g["dst_fd"].flop,  # type: ignore[index]
            dst_clock=g["dst_fd"].clock,  # type: ignore[index]
            min_hops=g["min_hops"],  # type: ignore[arg-type]
            width=len(g["bits"]),  # type: ignore[arg-type]
        )
        for g in grouped.values()
    ]


# --- helpers ----------------------------------------------------------------

# Yosys $-prefixed primitives expose their outputs as the ``Y`` port (or
# ``Q`` for state cells, handled separately). Map cell type → set of port
# names whose connections are outputs.
_OUTPUT_PORTS_BY_TYPE: dict[str, frozenset[str]] = {
    # logic / arith / mux all use Y
}

_DEFAULT_OUTPUT_PORTS: frozenset[str] = frozenset({"Y"})


def _cell_outputs(cell):
    out_ports = _OUTPUT_PORTS_BY_TYPE.get(cell.type, _DEFAULT_OUTPUT_PORTS)
    bits: list = []
    for p in out_ports:
        bits.extend(cell.connections.get(p, ()))
    return bits
