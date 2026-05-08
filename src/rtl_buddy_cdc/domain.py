"""Assign each flop to a clock domain and find register-to-register CDC paths.

For an MVP analyzer working on flattened netlists we make a simple but
load-bearing assumption: every flop's CLK pin connects directly to a
top-level clock port. This holds for the canonical golden fixture and
for most hand-written RTL. Real designs can route clocks through gates
(clock muxes, ICGs) — that's a follow-up: ``trace_clock_root`` is the
hook where we'll grow that walk.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from rtl_buddy_cdc.flops import Flop, find_flops
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


def trace_clock_root(module: Module, bit: Bit) -> str | None:
    """Resolve a CLK net bit to the top-level port driving it.

    Currently only the trivial direct-port case is handled. Cells in the
    path return ``None``, which the caller treats as "domain unknown".
    """
    port = module.port_of_bit(bit)
    if port is not None and port.direction == "input":
        return port.name
    return None


def assign_domains(module: Module) -> list[FlopDomain]:
    return [
        FlopDomain(flop=f, clock=trace_clock_root(module, f.clk))
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
