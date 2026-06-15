"""Assign each flop to a clock domain and find register-to-register CDC paths.

The clock-domain assignment walks each flop's ``CLK`` net backward
through buffers, inverters, integrated clock gates, and clock-divider
flops to find the originating top-level clock port. Direct
port-to-flop wiring is the trivial case; everything else uses the
small heuristic in :func:`trace_clock_root`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypedDict

from rtl_buddy_cdc.flops import Flop, find_flops, flop_clk_pin, is_ff_cell
from rtl_buddy_cdc.netlist import Bit, BoundarySummary, Cell, Module


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


class _BoundaryCrossingGroup(TypedDict):
    """Per (instance.port, dst_flop) accumulator for boundary-sourced
    crossings — an auto-abstracted subtree's output port re-seeded as a
    virtual source in place of the flattened internal flops."""

    instance: str
    boundary_port: str
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

    ``src_boundary`` / ``dst_boundary`` (P0/#254) name a
    ``(instance, port)`` endpoint on an auto-abstracted blackbox
    boundary cell. ``src_boundary`` is set when the source is a
    summarised subtree's output port (a virtual source seeded at the
    subtree's domain in place of the flattened internal flops);
    ``dst_boundary`` is reserved for data *entering* a boundary in a
    foreign domain (P3). Setting them does not change the public JSON
    contract — they are additive optional fields on the single
    ``Crossing`` type, never a forked one.
    """

    src_clock: str
    dst_flop: Flop
    dst_clock: str
    min_hops: int
    width: int
    src_flop: Flop | None = None
    src_port: str | None = None
    src_boundary: tuple[str, str] | None = None
    dst_boundary: tuple[str, str] | None = None

    @property
    def src_name(self) -> str:
        """Human-readable identifier for the source endpoint."""
        if self.src_flop is not None:
            return self.src_flop.name
        if self.src_port is not None:
            return f"port {self.src_port}"
        if self.src_boundary is not None:
            inst, port = self.src_boundary
            return f"boundary {inst}.{port}"
        return "<unknown>"

    @property
    def is_port_sourced(self) -> bool:
        return self.src_port is not None

    @property
    def is_boundary_sourced(self) -> bool:
        return self.src_boundary is not None


# Clock name stamped on a boundary-sourced crossing whose subtree
# domain didn't resolve to a named clock. Mirrors the SDC
# unconstrained sentinel so ``are_async`` treats it as async to every
# real clock (a sink in any known domain is conservatively a crossing).
_UNKNOWN_BOUNDARY_CLOCK = "<unconstrained>"

# Cell ``type`` stamped on a synthetic boundary-*sink* flop standing in
# for a blackbox input pin (P3/#257). Deliberately not a recognised FF
# type, so ``is_ff_cell`` is False and rule helpers that special-case
# real flops (CDC-008's clock-pin exemption, the dffe-EN gating check)
# treat it as an ordinary opaque destination. The fake clock-pin bit
# never participates in clock tracing — the sink's domain is recorded
# directly on its :class:`FlopDomain`.
_BOUNDARY_SINK_TYPE = "$boundary_sink"
_BOUNDARY_SINK_CLK: Bit = "<boundary-sink-clk>"


def _boundary_for(
    boundaries: dict[str, "BoundarySummary"],
    instance: str,
    module_type: str,
) -> "BoundarySummary | None":
    """Resolve a boundary instance's summary, instance key first.

    ``compose_boundaries`` (P3/#257) keys the boundary map by *instance*
    path so the same module type instantiated in two different clock
    domains gets a correct per-instance summary. Hand-built callers /
    the legacy single-domain shape may still key by module *type*; the
    type key is the fallback. Instance-first then type means both forms
    work without double-counting (each cell resolves to one summary).
    """
    summary = boundaries.get(instance)
    if summary is not None:
        return summary
    return boundaries.get(module_type)


def _boundary_sink_flops(
    module: Module,
    boundaries: dict[str, "BoundarySummary"],
) -> tuple[list[FlopDomain], dict[str, tuple[str, str]]]:
    """Build virtual-sink :class:`FlopDomain`s for blackbox input pins.

    For each boundary instance and each summarised *input* port, a
    synthetic :class:`Flop` stands in for the boundary input pin the
    flattened subtree's first internal flop would have captured. Its
    ``D`` bits are the parent's connection to that input port and its
    clock domain is the boundary's own resolved clock
    (``BoundarySummary.clock``) — so the existing flop/port/boundary
    forward walks, on reaching those ``D`` bits from a foreign-domain
    source, emit a crossing *into* the boundary (the mirror of the
    output-port virtual-source seeding). ``None`` (unconstrained
    boundary clock) is left as the destination clock so a foreign source
    still crosses; same-domain data does not (``dst_clock == src_clock``
    is filtered at record-creation).

    Returns ``(sink_domains, lookup)`` where ``lookup`` maps each
    synthetic flop cell name to the ``(instance, port)`` pair the
    emitting walk stamps onto ``Crossing.dst_boundary``.
    """
    sink_domains: list[FlopDomain] = []
    lookup: dict[str, tuple[str, str]] = {}
    for inst_name, cell in module.cells.items():
        summary = _boundary_for(boundaries, inst_name, cell.type)
        if summary is None or not summary.input_ports:
            continue
        for port_name in summary.input_ports:
            conn = cell.connections.get(port_name, ())
            d_bits = tuple(b for b in conn if isinstance(b, int))
            if not d_bits:
                continue
            sink_name = f"{inst_name}.{port_name}"
            sink_cell = Cell(
                name=sink_name,
                type=_BOUNDARY_SINK_TYPE,
                connections={"D": d_bits},
            )
            sink_flop = Flop(cell=sink_cell, clk=_BOUNDARY_SINK_CLK, d=d_bits, q=())
            sink_domains.append(FlopDomain(flop=sink_flop, clock=summary.clock))
            lookup[sink_name] = (inst_name, port_name)
    return sink_domains, lookup


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
    *,
    allow_divider: bool = True,
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
    return _trace(
        module,
        bit,
        drivers,
        seen,
        max_depth,
        bit_to_clock or {},
        allow_divider=allow_divider,
    )


def _trace(
    module: Module,
    bit: Bit,
    drivers: dict[Bit, tuple[str, str]],
    seen: set[Bit],
    depth: int,
    bit_to_clock: dict[Bit, str],
    *,
    allow_divider: bool = True,
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
            return _trace(
                module,
                a[0],
                drivers,
                seen,
                depth - 1,
                bit_to_clock,
                allow_divider=allow_divider,
            )
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
            _trace(
                module,
                a[0],
                drivers,
                set(seen),
                depth - 1,
                bit_to_clock,
                allow_divider=allow_divider,
            )
            if a
            else None
        )
        b_root = (
            _trace(
                module,
                b[0],
                drivers,
                set(seen),
                depth - 1,
                bit_to_clock,
                allow_divider=allow_divider,
            )
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
                    module,
                    in_bits[0],
                    drivers,
                    set(seen),
                    depth - 1,
                    bit_to_clock,
                    allow_divider=allow_divider,
                )
                if root is not None:
                    return root
        return None

    # Clock divider — flop Q output. Trace the source flop's CLK back
    # to its root. Handles both higher-level ($dff*) and gate-level
    # ($_DFF*) families via flop_clk_pin. Suppressed when
    # ``allow_divider`` is False: a *data* net driven by a flop Q would
    # otherwise resolve to that flop's clock and be misread as a clock
    # net — the FIX-1 clock-pin classifier of a non-allow-listed
    # blackbox port must not make that mistake.
    if allow_divider and is_ff_cell(ctype) and out_port == "Q":
        clk_bits = flop_clk_pin(cell)
        if clk_bits:
            return _trace(
                module,
                clk_bits[0],
                drivers,
                seen,
                depth - 1,
                bit_to_clock,
                allow_divider=allow_divider,
            )

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
    module: Module,
    pin_clocks: dict[str, str] | None = None,
    *,
    clock_for_port: Callable[[str], str | None] | None = None,
) -> list[FlopDomain]:
    """Build per-flop ``FlopDomain`` records from clock-network tracing.

    When ``clock_for_port`` is supplied (typically
    :meth:`rtl_buddy_cdc.sdc.ClockSpec.clock_for_port`), the raw name
    returned by :func:`trace_clock_root` is normalised through it
    before being stored. This matters when the trace stops at a port
    that the SDC declared as part of a named clock — e.g.
    ``create_clock -name ck0 [get_ports {ck0_a ck0_b}]`` followed by a
    clock mux whose ``B`` leg is ``ck0_b``: without normalisation the
    downstream flop's domain leaks as ``"ck0_b"`` (the port name) when
    the SDC-canonical answer is ``"ck0"``. See issue #166.
    """
    drivers = _bit_drivers(module)
    bit_to_clock = _build_bit_to_clock(module, pin_clocks)
    out: list[FlopDomain] = []
    for f in find_flops(module):
        root = trace_clock_root(module, f.clk, drivers, bit_to_clock=bit_to_clock)
        if root is not None and clock_for_port is not None:
            resolved = clock_for_port(root)
            if resolved is not None:
                root = resolved
        out.append(FlopDomain(flop=f, clock=root))
    return out


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
    *,
    clock_for_port: Callable[[str], str | None] | None = None,
    boundaries: dict[str, "BoundarySummary"] | None = None,
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

    The ``clock_for_port`` keyword mirrors :func:`assign_domains` —
    when supplied (typically ``ClockSpec.clock_for_port``), the
    per-flop clock names that flow into each emitted ``Crossing``
    carry the SDC-canonical clock name rather than the raw port
    name a clock-mux trace stopped on. Without it, the dst_clock can
    leak as e.g. ``ck0_b`` while ``flop_domains[].clock`` shows
    ``ck0`` — two views of the same domain disagreeing. See
    issue #166.
    """
    domains = {
        fd.flop.cell.name: fd
        for fd in assign_domains(module, pin_clocks, clock_for_port=clock_for_port)
    }
    consumers = _build_bit_consumers(module)

    # Synthetic boundary-*sink* flops (P3/#257): a blackbox input pin in
    # a domain foreign to the boundary's own clock is a crossing INTO the
    # subtree. Fold these into ``domains`` / ``flop_by_d_bit`` so the
    # flop-, port-, and boundary-source walks below all reach them with
    # no special-casing; the emitting site stamps ``dst_boundary`` from
    # ``boundary_sink_lookup``. The synthetic flops are never in
    # ``module.cells`` and so never affect rule context built from the
    # real netlist.
    boundary_sink_lookup: dict[str, tuple[str, str]] = {}
    if boundaries:
        sink_domains, boundary_sink_lookup = _boundary_sink_flops(module, boundaries)
        for fd in sink_domains:
            domains[fd.flop.cell.name] = fd

    flop_by_d_bit: dict[Bit, list[Flop]] = defaultdict(list)
    for fd in domains.values():
        for b in fd.flop.d:
            if isinstance(b, int):
                flop_by_d_bit[b].append(fd.flop)

    def _dst_boundary_of(dst_flop: Flop) -> tuple[str, str] | None:
        return boundary_sink_lookup.get(dst_flop.cell.name)

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
                    if is_ff_cell(cell.type):
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
            dst_boundary=_dst_boundary_of(g["dst_flop"]),
        )
        for g in grouped.values()
    ]

    # Port-sourced crossings: walk forward from each typed input port
    # and record any flop's D pin reached in a different clock domain.
    if port_clock:
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
                        if is_ff_cell(cell.type):
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
                    dst_boundary=_dst_boundary_of(pg["dst_flop"]),
                )
            )

    # Boundary-sourced crossings (P2/#256): an auto-abstracted
    # single-clock subtree's output ports are re-seeded as virtual
    # sources at the subtree's domain, in place of the flattened
    # internal flops. We walk forward from each boundary instance's
    # connected output bits the same way the port-walk does; a sink in
    # a different domain is a crossing the parent must check
    # (``synchronised`` ports never reach here — the summariser drops
    # them before they become a boundary). The instance is the parent
    # cell whose ``type`` is the summarised module name.
    if boundaries:
        bnd_grouped: dict[tuple[str, str, str], _BoundaryCrossingGroup] = {}
        for inst_name, cell in module.cells.items():
            summary = _boundary_for(boundaries, inst_name, cell.type)
            if summary is None:
                continue
            for port_name, pb in summary.ports.items():
                src_clk_name = pb.src_clock
                conn = cell.connections.get(port_name, ())
                bnd_seen: dict[Bit, int] = {}
                bnd_frontier: list[tuple[Bit, int]] = [
                    (b, 0) for b in conn if isinstance(b, int)
                ]
                for b, h in bnd_frontier:
                    bnd_seen[b] = h
                while bnd_frontier:
                    bnd_next: list[tuple[Bit, int]] = []
                    for bit, hops in bnd_frontier:
                        for dst_flop in flop_by_d_bit.get(bit, ()):
                            dst_fd = domains[dst_flop.cell.name]
                            if dst_fd.clock is None:
                                continue
                            if (
                                src_clk_name is not None
                                and dst_fd.clock == src_clk_name
                            ):
                                continue
                            bnd_key = (inst_name, port_name, dst_flop.cell.name)
                            bg = bnd_grouped.get(bnd_key)
                            if bg is None:
                                bnd_grouped[bnd_key] = {
                                    "instance": inst_name,
                                    "boundary_port": port_name,
                                    "src_clock": src_clk_name
                                    if src_clk_name is not None
                                    else _UNKNOWN_BOUNDARY_CLOCK,
                                    "dst_flop": dst_fd.flop,
                                    "dst_clock": dst_fd.clock,
                                    "min_hops": hops,
                                    "bits": {bit},
                                }
                            else:
                                bg["bits"].add(bit)
                                if hops < bg["min_hops"]:
                                    bg["min_hops"] = hops
                        if hops >= max_hops:
                            continue
                        for cell_name, _port, _idx in consumers.get(bit, ()):
                            c = module.cells[cell_name]
                            if c.type in {"$scopeinfo"}:
                                continue
                            if is_ff_cell(c.type):
                                continue
                            for ob in _cell_outputs(c):
                                if not isinstance(ob, int):
                                    continue
                                prev = bnd_seen.get(ob)
                                new_hops = hops + 1
                                if prev is None or new_hops < prev:
                                    bnd_seen[ob] = new_hops
                                    bnd_next.append((ob, new_hops))
                    bnd_frontier = bnd_next

        for bg in bnd_grouped.values():
            out_crossings.append(
                Crossing(
                    src_clock=bg["src_clock"],
                    dst_flop=bg["dst_flop"],
                    dst_clock=bg["dst_clock"],
                    min_hops=bg["min_hops"],
                    width=len(bg["bits"]),
                    src_boundary=(bg["instance"], bg["boundary_port"]),
                    dst_boundary=_dst_boundary_of(bg["dst_flop"]),
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
