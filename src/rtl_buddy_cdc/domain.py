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
from typing import TypedDict

from rtl_buddy_cdc.flops import FF_CELL_TYPES, Flop, find_flops
from rtl_buddy_cdc.netlist import Bit, Module


class _CrossingGroup(TypedDict):
    """Per (src_flop, dst_flop) accumulator inside ``find_crossings``.

    Clocks are stored as ``str`` (not ``str | None``) because the
    insertion site has already filtered out untraceable-clock flops —
    putting that invariant in the type lets the ``Crossing(...)`` site
    pass through without further narrowing.
    """

    src_flop: "Flop"
    src_clock: str
    dst_flop: "Flop"
    dst_clock: str
    min_hops: int
    bits: set[Bit]


class _PortCrossingGroup(TypedDict):
    """Per (src_port, dst_flop) accumulator for port-sourced crossings."""

    port: str
    src_clock: str
    dst_flop: "Flop"
    dst_clock: str
    min_hops: int
    bits: set[Bit]


@dataclass(frozen=True)
class FlopDomain:
    flop: Flop
    clock: str | None  # top-level port name, or None if untraceable


@dataclass(frozen=True)
class Crossing:
    """A fanout path that crosses domains, ending at a flop's ``D`` pin.

    The source endpoint is either a flop (the typical case — register-
    to-register) or a top-level input port that the SDC has typed with
    ``set_input_delay -clock <c>``. Exactly one of ``src_flop`` and
    ``src_port`` is set.

    ``min_hops`` is the shortest combinational-cell count on the path.
    For flop sources, ``min_hops == 0`` means a direct flop-to-flop
    wire (the classic synchronizer first stage). For port sources,
    ``min_hops`` is always ≥ 0 and reflects the depth of comb logic
    between the port and the destination's ``D`` pin.

    ``width`` is the number of distinct destination ``D`` bits
    reachable from the source on this crossing (>1 means a bus
    crossing). Port-sourced crossings are width 1.
    """

    src_clock: str
    dst_flop: Flop
    dst_clock: str
    min_hops: int
    width: int
    src_flop: Flop | None = None
    src_port: str | None = None

    @property
    def src_name(self) -> str:
        """Human-readable identifier for the source endpoint."""
        if self.src_flop is not None:
            return self.src_flop.name
        if self.src_port is not None:
            return f"port {self.src_port}"
        return "<unknown>"

    @property
    def is_port_sourced(self) -> bool:
        return self.src_port is not None


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
    bit_to_clock: dict[Bit, str] | None = None,
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

    When ``bit_to_clock`` is provided (typically derived from
    ``ClockSpec.pin_clocks``), the walk *stops* at any bit that is part
    of a net named as a ``create_generated_clock`` target and returns
    that generated clock's name rather than continuing back to the top
    input port. This is what models SoC clock-forwarding chains where
    each block declares its forwarded clock at an internal pin.

    Returns ``None`` when no candidate root resolves within
    ``max_depth`` cells; the caller treats that as "domain unknown".
    """
    if drivers is None:
        drivers = _bit_drivers(module)
    seen: set[Bit] = set()
    return _trace(module, bit, drivers, seen, max_depth, bit_to_clock or {})


def _trace(
    module: Module,
    bit: Bit,
    drivers: dict[Bit, tuple[str, str]],
    seen: set[Bit],
    depth: int,
    bit_to_clock: dict[Bit, str],
) -> str | None:
    if not isinstance(bit, int) or depth <= 0 or bit in seen:
        return None
    seen.add(bit)

    # Stop at a generated clock declared on an internal pin: this bit
    # belongs to the net where the new clock identity takes over.
    pin_clk = bit_to_clock.get(bit)
    if pin_clk is not None:
        return pin_clk

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
            return _trace(module, a[0], drivers, seen, depth - 1, bit_to_clock)
        return None

    # Clock gate — look at both inputs; if exactly one resolves to a
    # clock port, that's our root. If both resolve, the cell is
    # combining two clock domains and we (conservatively) pick the
    # first; a stricter check would emit a violation, but we leave
    # that to a future rule.
    if ctype in _GATE_TYPES:
        a = cell.connections.get("A", ())
        b = cell.connections.get("B", ())
        a_root = (
            _trace(module, a[0], drivers, set(seen), depth - 1, bit_to_clock)
            if a
            else None
        )
        b_root = (
            _trace(module, b[0], drivers, set(seen), depth - 1, bit_to_clock)
            if b
            else None
        )
        return a_root or b_root

    # Clock mux — return whichever side resolves.
    if ctype in _MUX_TYPES:
        for in_port in ("A", "B"):
            in_bits = cell.connections.get(in_port, ())
            if in_bits:
                root = _trace(
                    module, in_bits[0], drivers, set(seen), depth - 1, bit_to_clock
                )
                if root is not None:
                    return root
        return None

    # Clock divider — flop Q output. Trace the source flop's CLK back
    # to its root.
    if ctype in FF_CELL_TYPES and out_port == "Q":
        clk_bits = cell.connections.get("CLK", ())
        if clk_bits:
            return _trace(module, clk_bits[0], drivers, seen, depth - 1, bit_to_clock)

    return None


def _build_bit_to_clock(
    module: Module, pin_clocks: dict[str, str] | None
) -> dict[Bit, str]:
    """Expand SDC pin-target paths (``u_a/clk_out``) to a bit→clock map.

    Yosys' flattened netlist preserves hierarchical wire names with
    ``.`` as separator (e.g. ``u_a.clk_out``). The SDC convention is
    ``/``. We normalise the SDC form to match before looking up the
    netname and harvesting its bits.
    """
    out: dict[Bit, str] = {}
    if not pin_clocks:
        return out
    for pin_path, clk_name in pin_clocks.items():
        nn_key = pin_path.replace("/", ".")
        nn = module.netnames.get(nn_key)
        if nn is None:
            continue
        for b in nn.bits:
            if isinstance(b, int):
                # First writer wins. If two generated clocks target
                # the same net, the SDC is internally inconsistent;
                # we don't try to repair it here.
                out.setdefault(b, clk_name)
    return out


def assign_domains(
    module: Module, pin_clocks: dict[str, str] | None = None
) -> list[FlopDomain]:
    drivers = _bit_drivers(module)
    bit_to_clock = _build_bit_to_clock(module, pin_clocks)
    return [
        FlopDomain(
            flop=f,
            clock=trace_clock_root(module, f.clk, drivers, bit_to_clock=bit_to_clock),
        )
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


def find_crossings(
    module: Module,
    max_hops: int = 4,
    port_clock: dict[str, str] | None = None,
    pin_clocks: dict[str, str] | None = None,
) -> list[Crossing]:
    """Find every fanout path whose endpoints are in different domains.

    The walk starts from each flop's ``Q`` bits and follows readers up to
    ``max_hops`` combinational cells before giving up. Reaching another
    flop's ``D`` pin produces a :class:`Crossing` if the two flops are in
    distinct, known domains.

    When ``port_clock`` is supplied (typically from
    :class:`ClockSpec.port_clock`), every top-level input port that the
    SDC has typed is also walked forward; if the walk lands on a flop's
    ``D`` pin in a different clock domain than the port's typed clock,
    a port-sourced :class:`Crossing` is emitted (``src_port`` set,
    ``src_flop`` left None). The destination's clock is the flop's
    physical CLK domain; the port's clock is treated as the source
    domain for the async-pair check.
    """
    domains = {fd.flop.cell.name: fd for fd in assign_domains(module, pin_clocks)}
    consumers = _build_bit_consumers(module)
    flop_by_d_bit: dict[Bit, list[Flop]] = defaultdict(list)
    for fd in domains.values():
        for b in fd.flop.d:
            if isinstance(b, int):
                flop_by_d_bit[b].append(fd.flop)

    # Grouped per (src_flop, dst_flop) pair so a multi-bit bus or a fanout
    # that hits the same destination flop on multiple D bits collapses to
    # one Crossing record.
    grouped: dict[tuple[str, str], _CrossingGroup] = {}

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
                            "src_flop": src_fd.flop,
                            "src_clock": src_fd.clock,
                            "dst_flop": dst_fd.flop,
                            "dst_clock": dst_fd.clock,
                            "min_hops": hops,
                            "bits": {bit},
                        }
                    else:
                        g["bits"].add(bit)
                        if hops < g["min_hops"]:
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

    out_crossings: list[Crossing] = [
        Crossing(
            src_flop=g["src_flop"],
            src_clock=g["src_clock"],
            dst_flop=g["dst_flop"],
            dst_clock=g["dst_clock"],
            min_hops=g["min_hops"],
            width=len(g["bits"]),
        )
        for g in grouped.values()
    ]

    # Port-sourced crossings: walk forward from each typed input port
    # and record any flop's D pin reached in a different clock domain.
    if port_clock:
        from rtl_buddy_cdc.flops import FF_CELL_TYPES

        port_grouped: dict[tuple[str, str], _PortCrossingGroup] = {}
        for port_name, port_clk_name in port_clock.items():
            port = module.ports.get(port_name)
            if port is None or port.direction != "input":
                continue
            seen = {}
            frontier = [(b, 0) for b in port.bits if isinstance(b, int)]
            for b, h in frontier:
                seen[b] = h
            while frontier:
                next_frontier = []
                for bit, hops in frontier:
                    for dst_flop in flop_by_d_bit.get(bit, ()):
                        dst_fd = domains[dst_flop.cell.name]
                        if dst_fd.clock is None:
                            continue
                        # Skip when the port's typed clock matches the
                        # destination's clock domain (no crossing).
                        if dst_fd.clock == port_clk_name:
                            continue
                        key = (port_name, dst_flop.cell.name)
                        pg = port_grouped.get(key)
                        if pg is None:
                            port_grouped[key] = {
                                "port": port_name,
                                "src_clock": port_clk_name,
                                "dst_flop": dst_fd.flop,
                                "dst_clock": dst_fd.clock,
                                "min_hops": hops,
                                "bits": {bit},
                            }
                        else:
                            pg["bits"].add(bit)
                            if hops < pg["min_hops"]:
                                pg["min_hops"] = hops
                    if hops >= max_hops:
                        continue
                    for cell_name, _port, _idx in consumers.get(bit, ()):
                        cell = module.cells[cell_name]
                        if cell.type in {"$scopeinfo"}:
                            continue
                        if cell.type in FF_CELL_TYPES:
                            continue
                        for ob in _cell_outputs(cell):
                            if not isinstance(ob, int):
                                continue
                            prev = seen.get(ob)
                            new_hops = hops + 1
                            if prev is None or new_hops < prev:
                                seen[ob] = new_hops
                                next_frontier.append((ob, new_hops))
                frontier = next_frontier

        for pg in port_grouped.values():
            out_crossings.append(
                Crossing(
                    src_clock=pg["src_clock"],
                    dst_flop=pg["dst_flop"],
                    dst_clock=pg["dst_clock"],
                    min_hops=pg["min_hops"],
                    width=len(pg["bits"]),
                    src_port=pg["port"],
                )
            )

    return out_crossings


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
