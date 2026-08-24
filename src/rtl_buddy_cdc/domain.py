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
from typing import TYPE_CHECKING, TypedDict

from rtl_buddy_cdc.flops import (
    Flop,
    find_flops,
    flop_clk_pin,
    is_ff_cell,
    is_latch_cell,
)
from rtl_buddy_cdc.netlist import Bit, BoundarySummary, Cell, Module

if TYPE_CHECKING:
    from rtl_buddy_cdc.sdc import ClockSpec


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
class ClockCombine:
    """A clock-network node where >=2 distinct DECLARED clocks combine.

    Recorded at the exact moment :func:`_pick_combining_root` *declines*
    (issue #269): a gate (``$and`` / ``$or`` …) or a clock-path
    transparent latch (``$dlatch`` / ``$_DLATCH_*``) whose legs resolve
    to two or more distinct declared clocks. The downstream clock net
    physically toggles on both, so the tracer refuses to assert either
    leg and every flop behind it is left domain-unknown (#263).

    A **mux** is deliberately not a combine — it *selects* one of its
    clock inputs — so it never reaches the decline site and never
    produces one of these records.

    ``net`` is the combined output net's name (the driven CLK net),
    ``cell`` / ``cell_type`` the combining cell, ``clocks`` the sorted,
    canonical declared-clock names that met there, and ``sinks`` the
    sorted flop cell names whose ``CLK`` trace reached this node.

    Pure data. The record is what CDC-023 reports; producing it never
    changes a domain or a crossing.
    """

    net: str
    cell: str
    cell_type: str
    clocks: tuple[str, ...]
    sinks: tuple[str, ...] = ()


@dataclass(frozen=True)
class InferredClockCandidate:
    """An undeclared internal net used as a clock by many flops (P3/#263).

    A *candidate* generated clock: a single net bit driven by a flop ``Q``
    or by a clock-gate / latch (ICG) output that fans out to
    ``fanout`` (>= a threshold) flop ``CLK`` pins, and is **not** already
    named as a clock — neither a top-level input port nor a
    ``create_generated_clock`` target (``bit_to_clock``). It is a hint
    that the user may have forgotten a ``create_generated_clock`` so the
    forwarded clock gets its own SDC identity.

    This is **advisory only**. Emitting it never changes a flop's domain
    or any crossing — flops behind an undeclared internal clock stay
    domain-unknown unless a real clock-root trace already resolves them
    (the divider/latch clauses of :func:`trace_clock_root`, which fire
    independently of this report). The fanout heuristic alone never
    assigns a domain; see issue #263 and the §4.7 reporter contract.

    ``driver`` is the human-readable ``cell.name`` of the driving cell;
    ``driver_kind`` is ``"flop"`` for a flop-``Q`` driver or ``"gate"``
    for an ICG / clock-gate / latch output. ``example_sinks`` is a
    bounded, sorted sample of the flop cell names whose ``CLK`` the net
    drives (the full count is ``fanout``).
    """

    driver: str
    driver_kind: str
    fanout: int
    example_sinks: tuple[str, ...]


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

    ``scan_mode`` (issue #45) marks a crossing whose **destination**
    flop is clocked through a DFT scan structure — a clock mux or clock
    gate whose control traces back to a top-level port the user tagged
    with one of ``rules.SCAN_MODE_ATTRS``. It is a *tag*, never a drop:
    the crossing is emitted, counted and reported exactly as before,
    and only an explicit ``--ignore-scan-mode`` makes the rule pack
    skip it. Stamped from :func:`find_scan_mode_flops`, which reads the
    fact off the ordinary clock-root walk rather than re-deriving it.
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
    scan_mode: bool = False

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
        for port_name, pb in summary.input_ports.items():
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
            # PER-PORT capture domain (#261). For a single-clock summary
            # this is ``BoundarySummary.clock`` on every port, exactly as
            # before. For a MULTI-clock block the compositional pass
            # resolves each input to the domain that really captures it
            # (an async FIFO's ``wdata`` on ``wr_clk``, ``rd_en`` on
            # ``rd_clk``), so the star-collapse becomes per-domain rather
            # than one hub — which is what lets such a block be summarised
            # at all instead of declined.
            sink_domains.append(
                FlopDomain(
                    flop=sink_flop,
                    clock=pb.src_clock if pb.src_clock is not None else summary.clock,
                )
            )
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

# Cell types whose output lane ``i`` depends only on input lane ``i`` —
# width-preserving bitwise ops and mux data paths. For these, a bit entering on
# lane ``idx`` propagates only to output ``Y[idx]`` rather than to every output
# bit. Anything *not* listed here (adders/shifts with carry, reductions,
# comparisons, mux selects, memory) keeps the conservative all-outputs fan-out,
# so a genuine cross-lane path is never dropped. This is the data-fanout analog
# of the clock-network sets above; see ``_lane_targets`` and issue #258.
_LANE_ALIGNED_TYPES: frozenset[str] = frozenset(
    {
        "$and",
        "$or",
        "$xor",
        "$xnor",
        "$not",
        "$buf",
        "$pos",
        "$mux",
        "$pmux",
        "$bwmux",
        "$_AND_",
        "$_OR_",
        "$_XOR_",
        "$_XNOR_",
        "$_NOT_",
        "$_NAND_",
        "$_NOR_",
        "$_ANDNOT_",
        "$_ORNOT_",
        "$_BUF_",
        "$_MUX_",
    }
)


# Callback signature for the clock-combining decline recorder
# (``(cell_name, cell_type, driven_bit, declared_clocks)``). Invoked at
# the ONE site that declines — :func:`_pick_combining_root` — so a
# consumer's "this is a combine" can never drift from the tracer's
# "I declined here". See :func:`find_clock_combines` and issue #269.
CombineSink = Callable[[str, str, Bit, frozenset[str]], None]


# Callback signature for the clock-network *control* recorder
# (``(cell_name, cell_type, control_bits)``). Invoked from the clock
# mux / gate / latch clauses of :func:`_trace` at the moment the walk
# resolves *through* one of them, with the bits of the inputs that
# steer the selection rather than carry the clock: a ``$mux``'s ``S``
# pin, and a gate's or latch's non-clock (enable) legs.
#
# Same philosophy as :data:`CombineSink` (#269): the fact is recorded
# at the site the walk already visits, so a consumer's "this clock is
# muxed by X" cannot drift from the tracer's own idea of the clock
# network. :func:`find_scan_mode_flops` is the only consumer today.
#
# Purely observational — it never changes the walk's outcome.
ClockControlSink = Callable[[str, str, tuple[Bit, ...]], None]


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
    clock_identity: Callable[[str], str | None] | None = None,
    on_combine: CombineSink | None = None,
    on_clock_control: ClockControlSink | None = None,
) -> str | None:
    """Resolve a CLK net bit to the top-level port that ultimately drives it.

    Handles common clock-network shapes:

    - direct top-level port (the trivial case),
    - single-input buffers and inverters (transparent),
    - two-input clock gates (``$and``/``$or``…) where exactly one of
      the inputs traces back to a clock port — the other is treated
      as the gate's enable; when ``clock_identity`` is supplied and two
      *different* declared clocks combine on the gate (or a clock-path
      latch), the walk declines (returns ``None``) rather than silently
      picking one leg, so the flop is surfaced as under-resolved,
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

    ``on_combine`` is an optional recorder invoked once per
    clock-combining **decline** (see :data:`CombineSink`). It observes
    the walk; it never changes its outcome. :func:`find_clock_combines`
    uses it to build the CDC-023 findings from the tracer's own decline
    events rather than re-deriving them.

    ``on_clock_control`` is the sibling recorder for the *control*
    inputs of the selection / gating cells the walk resolves through
    (see :data:`ClockControlSink`): a clock mux's ``S``, a clock gate's
    or latch's non-clock leg. :func:`find_scan_mode_flops` uses it to
    spot a DFT scan structure in a flop's clock network (#45). Like
    ``on_combine`` it observes the walk and never changes its outcome.

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
        clock_identity=clock_identity,
        on_combine=on_combine,
        on_clock_control=on_clock_control,
    )


def _pick_combining_root(
    roots: list[str],
    clock_identity: Callable[[str], str | None] | None,
    on_decline: Callable[[frozenset[str]], None] | None = None,
) -> str | None:
    """Choose the single clock root of a combining cell, or decline.

    ``roots`` are the resolved (non-``None``) roots of a gate's / latch's
    legs, in leg order. ``clock_identity`` maps a raw root to its
    canonical declared-clock name (or ``None`` for a leg that is *not* a
    declared clock — e.g. a plain enable port). With it:

    - **two or more distinct declared clocks** combine on one net — a
      genuine clock-mixing point whose downstream domain is ambiguous;
      return ``None`` so the flop is surfaced as under-resolved rather
      than silently asserted to one leg (#263 soundness hardening);
    - **exactly one** declared clock among the legs — return that leg
      (the normal ICG: clock + non-clock enable), regardless of leg
      order, so a clock-on-enable shape resolves correctly too;
    - **no** declared clock among the legs (an undeclared internal root,
      or no SDC clock context) — fall back to the first resolved root,
      the pre-#263 first-leg-wins behaviour.

    When ``clock_identity`` is ``None`` (callers with no clock-set
    context) the behaviour is unconditionally first-leg-wins, unchanged.
    Two legs that map to the *same* clock (e.g. ``ck0_a`` / ``ck0_b`` of
    one ``create_clock``) are one identity, not a combine — see #166.

    ``on_decline`` (issue #269) is called with the set of declared clock
    names that met here, at the *one* line that declines. CDC-023's
    findings are built from those calls, which is what makes
    "the rule fired" and "the tracer declined" the same event by
    construction rather than by two predicates agreeing.
    """
    distinct = list(dict.fromkeys(roots))
    if not distinct:
        return None
    if clock_identity is None:
        return distinct[0]
    ids = [(r, clock_identity(r)) for r in distinct]
    real = {cid for (_, cid) in ids if cid is not None}
    if len(real) >= 2:
        # Genuine clock-combining node — decline loudly.
        if on_decline is not None:
            on_decline(frozenset(real))
        return None
    if real:
        for r, cid in ids:
            if cid is not None:
                return r  # the single declared-clock leg, any leg order
    return distinct[0]  # no declared clock among legs — prior behaviour


def _trace(
    module: Module,
    bit: Bit,
    drivers: dict[Bit, tuple[str, str]],
    seen: set[Bit],
    depth: int,
    bit_to_clock: dict[Bit, str],
    *,
    allow_divider: bool = True,
    clock_identity: Callable[[str], str | None] | None = None,
    on_combine: CombineSink | None = None,
    on_clock_control: ClockControlSink | None = None,
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

    def _leg(b: Bit) -> str | None:
        return _trace(
            module,
            b,
            drivers,
            set(seen),
            depth - 1,
            bit_to_clock,
            allow_divider=allow_divider,
            clock_identity=clock_identity,
            on_combine=on_combine,
            on_clock_control=on_clock_control,
        )

    def _record(clocks: frozenset[str]) -> None:
        """Forward this cell's decline to the recorder, if any."""
        if on_combine is not None:
            on_combine(cell.name, ctype, bit, clocks)

    def _record_control(control_bits: tuple[Bit, ...]) -> None:
        """Forward this cell's steering inputs to the recorder, if any.

        Called only once the cell has actually *resolved* — an
        unresolved leg tells a consumer nothing about the clock that
        reaches the flop."""
        if on_clock_control is not None and control_bits:
            on_clock_control(cell.name, ctype, control_bits)

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
                clock_identity=clock_identity,
                on_combine=on_combine,
                on_clock_control=on_clock_control,
            )
        return None

    # Clock gate — a two-input ICG shape ($and/$or…) where normally one
    # leg is the clock and the other an enable. Explore both legs; if
    # exactly one resolves to a declared clock, that is the root. If two
    # *different* declared clocks combine here, the cell mixes clock
    # domains and the downstream flop's domain is ambiguous — decline
    # (``_pick_combining_root`` returns None), surfacing the flop as
    # under-resolved rather than silently asserting one leg (#263).
    if ctype in _GATE_TYPES:
        roots: list[str] = []
        legs: list[tuple[Bit, str | None]] = []
        for in_port in ("A", "B"):
            in_bits = cell.connections.get(in_port, ())
            if in_bits:
                r = _leg(in_bits[0])
                legs.append((in_bits[0], r))
                if r is not None:
                    roots.append(r)
        picked = _pick_combining_root(roots, clock_identity, _record)
        if picked is not None:
            # Every leg that is NOT the clock the gate resolved to is,
            # by the ICG reading this clause already takes, an enable —
            # i.e. a control input. Report those bits.
            _record_control(tuple(b for b, r in legs if r != picked))
        return picked

    # Clock mux — return whichever side resolves. A mux *selects* one of
    # its clock inputs (it does not combine them), so first-resolves-wins
    # is correct and it is deliberately NOT routed through the combining
    # decline above.
    if ctype in _MUX_TYPES:
        for in_port in ("A", "B"):
            in_bits = cell.connections.get(in_port, ())
            if in_bits:
                root = _leg(in_bits[0])
                if root is not None:
                    # The select steers which leg reaches the flop; it
                    # is the one input of a clock mux that is not a
                    # clock. This is where a DFT scan mux is seen (#45).
                    _record_control(tuple(cell.connections.get("S", ())))
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
                clock_identity=clock_identity,
                on_combine=on_combine,
                on_clock_control=on_clock_control,
            )

    # Clock-path transparent latch — a latch-based ICG (or any clock
    # routed through a $dlatch / $_DLATCH_*) where the clock enters on
    # either the data pin (D) or the enable pin (EN coarse / E
    # gate-level). The clock can arrive on either leg depending on the
    # ICG coding style: ``always_latch if(en) gclk = clk;`` puts the
    # clock on D, while a latch whose enable IS the gated clock puts it
    # on EN. Explore every leg and resolve through the SAME combining
    # rule as the gate clause: one declared clock + a non-clock enable
    # resolves to that clock (normal ICG); two *different* declared
    # clocks combining decline (#263 soundness — never silently assert
    # one leg of a genuine clock-combining latch). Never makes a crossing
    # disappear: it only resolves a previously domain-unknown flop to a
    # verified upstream clock root, or declines.
    #
    # Gated on ``allow_divider`` for the same soundness reason as the
    # divider clause: with the FIX-1 ``allow_divider=False`` blackbox
    # clock-pin classifier, a *data* net latched by a $dlatch must not
    # resolve to a clock and be misread as a clock net.
    if allow_divider and is_latch_cell(ctype) and out_port == "Q":
        roots = []
        legs = []
        for in_port in ("D", "EN", "E"):
            in_bits = cell.connections.get(in_port, ())
            if in_bits:
                r = _leg(in_bits[0])
                legs.append((in_bits[0], r))
                if r is not None:
                    roots.append(r)
        picked = _pick_combining_root(roots, clock_identity, _record)
        if picked is not None:
            _record_control(tuple(b for b, r in legs if r != picked))
        return picked

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


def _clock_identity_fn(
    clock_for_port: Callable[[str], str | None] | None,
    generated_clocks: set[str],
) -> Callable[[str], str | None]:
    """Build the declared-clock predicate the combine-decline rule uses.

    Returns a callable mapping a raw trace root to its canonical
    declared-clock name, or ``None`` when the root is not a declared
    clock at all.

    A leg is a *declared clock* if the SDC maps its port into a named
    clock (``clock_for_port``) or it is a ``create_generated_clock``
    target. Non-clock legs (a plain enable port, an undeclared internal
    net) return ``None`` so they never count toward a clock-combining
    decline. Used by the gate / latch clauses of :func:`_trace` to tell
    a real two-clock combine apart from the common clock + enable-port
    ICG (#263).

    Extracted to module scope (#269) so :func:`assign_domains` and
    :func:`find_clock_combines` share **one** predicate: the rule that
    reports a combine and the tracer that declines on it cannot disagree
    about what counts as a declared clock.
    """

    def clock_identity(root: str) -> str | None:
        if clock_for_port is not None:
            named = clock_for_port(root)
            # The synthesized ``<unconstrained>`` sentinel — stamped on
            # every *untyped* input port by
            # ``synthesize_unconstrained_inputs`` before ``assign_domains``
            # on the CLI path — is NOT a real clock identity. It must never
            # count as a competing clock, or a normal ICG whose enable comes
            # from an untyped input port (clk + enable-port) would be misread
            # as a two-clock combine and wrongly decline, silently dropping
            # that flop's crossings. Mirrors the SDC sentinel; see
            # ``_UNKNOWN_BOUNDARY_CLOCK``.
            if named is not None and named != _UNKNOWN_BOUNDARY_CLOCK:
                return named
        if root in generated_clocks:
            return root
        return None

    return clock_identity


def assign_domains(
    module: Module,
    pin_clocks: dict[str, str] | None = None,
    *,
    clock_for_port: Callable[[str], str | None] | None = None,
    max_depth: int = 16,
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

    ``max_depth`` is the clock-trace hop budget handed to
    :func:`trace_clock_root` per flop (default 16). Raising it lets a
    deeper clock tree (a long divider / buffer / ICG chain) resolve
    without a code change; it only ever resolves MORE flops, never
    fewer, so the default keeps results identical to a fixed-16 walk.
    Surfaced as ``--clock-trace-depth`` on the CLI. See issue #263.
    """
    drivers = _bit_drivers(module)
    bit_to_clock = _build_bit_to_clock(module, pin_clocks)
    clock_identity = _clock_identity_fn(clock_for_port, set(bit_to_clock.values()))

    out: list[FlopDomain] = []
    for f in find_flops(module):
        root = trace_clock_root(
            module,
            f.clk,
            drivers,
            max_depth=max_depth,
            bit_to_clock=bit_to_clock,
            clock_identity=clock_identity,
        )
        if root is not None and clock_for_port is not None:
            resolved = clock_for_port(root)
            if resolved is not None:
                root = resolved
        out.append(FlopDomain(flop=f, clock=root))
    return out


# Default minimum number of flop CLK pins a candidate net must drive
# before it is reported as a possible undeclared generated clock. Four
# is a deliberately conservative floor: a forwarded/divided clock in an
# SoC typically clocks a whole register bank, while a lone toggle-flop
# whose Q happens to feed one or two CLK pins (a divide-by-2 with a tiny
# fanout) is too noisy to flag. The threshold is advisory tuning only —
# raising or lowering it changes only what gets *reported*, never a
# domain or a crossing.
_INFERRED_CLOCK_FANOUT_THRESHOLD = 4

# How many sink-flop cell names to keep per candidate in
# ``example_sinks``. The full fanout count is always ``fanout``; this is
# just a pointer at the affected bank so the report stays bounded.
_INFERRED_CLOCK_SINK_SAMPLE = 5


def _declared_clock_bits(
    module: Module,
    clock_for_port: Callable[[str], str | None] | None,
    bits: set[Bit],
) -> set[Bit]:
    """Subset of ``bits`` that lies on a net the SDC already names a clock.

    ``create_generated_clock`` targets reach us through ``pin_clocks`` →
    :func:`_build_bit_to_clock`, but a plain ``create_clock [get_pins
    <net>]`` on an internal pin lands its target in ``Clock.ports``,
    reachable only through :meth:`~rtl_buddy_cdc.sdc.ClockSpec.clock_for_port`
    — the same lookup the ``clock_identity`` combine predicate in
    :func:`assign_domains` uses. Both forms *declare* the net, so the
    inferred-clock advisory has to consult both or it re-reports a clock
    the user already wrote down (#270).

    Only the caller's candidate ``bits`` are considered, so the netname
    scan stays a single cheap pass and ``clock_for_port`` is asked about
    a handful of nets rather than every wire in the netlist.

    Netnames carry Yosys' flattened ``.`` separator while SDC pin paths
    use ``/``; both spellings are tried so a hierarchical pin target
    matches. The ``<unconstrained>`` sentinel is not a clock identity
    (see ``clock_identity``) and never counts as declared.
    """
    out: set[Bit] = set()
    if clock_for_port is None or not bits:
        return out
    for nn in module.netnames.values():
        hit = bits.intersection(b for b in nn.bits if isinstance(b, int))
        if not hit:
            continue
        named = clock_for_port(nn.name)
        if named is None:
            named = clock_for_port(nn.name.replace(".", "/"))
        if named is None or named == _UNKNOWN_BOUNDARY_CLOCK:
            continue
        out |= hit
    return out


def find_inferred_clock_candidates(
    module: Module,
    pin_clocks: dict[str, str] | None = None,
    *,
    clock_for_port: Callable[[str], str | None] | None = None,
    threshold: int = _INFERRED_CLOCK_FANOUT_THRESHOLD,
) -> list[InferredClockCandidate]:
    """Report internal nets that *look like* undeclared generated clocks.

    A candidate is a single net bit that

    1. drives at least ``threshold`` flop ``CLK`` pins,
    2. is produced by a flop ``Q`` or by a clock-gate / ICG / latch
       output (the same cell families :func:`trace_clock_root` treats as
       clock-network nodes), and
    3. is **not** already a declared clock — not a top-level input
       port, not a ``create_generated_clock`` target (``pin_clocks`` →
       ``bit_to_clock``), and not the target of a plain ``create_clock
       [get_pins <net>]`` on an internal pin (``clock_for_port`` →
       :func:`_declared_clock_bits`, #270).

    The intent is to point the user at a forwarded/divided clock they
    forgot to declare with ``create_generated_clock``, so its downstream
    flops stop landing in ``domain_unknown``.

    **Purely advisory.** This function reads the netlist and returns a
    report; it assigns no domain and emits no crossing. The caller must
    not feed its output back into :func:`assign_domains` /
    :func:`find_crossings`. Resolving a flop behind such a net is allowed
    ONLY when :func:`trace_clock_root` already follows the divider/latch
    to a real root — never on this fanout heuristic. See issue #263.
    """
    drivers = _bit_drivers(module)
    bit_to_clock = _build_bit_to_clock(module, pin_clocks)

    # Tally, per net bit, the flop CLK pins it drives. We sort the sink
    # names so the sample (and any equality test) is deterministic.
    clk_sinks: dict[Bit, list[str]] = defaultdict(list)
    for f in find_flops(module):
        if isinstance(f.clk, int):
            clk_sinks[f.clk].append(f.cell.name)

    # Nets that clear the fanout floor and carry no generated-clock
    # identity are the only ones we need an SDC clock-name lookup for.
    declared_bits = _declared_clock_bits(
        module,
        clock_for_port,
        {
            b
            for b, s in clk_sinks.items()
            if len(s) >= threshold and b not in bit_to_clock
        },
    )

    out: list[InferredClockCandidate] = []
    for bit, sinks in clk_sinks.items():
        if len(sinks) < threshold:
            continue
        # Already a named clock — not "forgotten". A top-level input port
        # IS the declared clock root; a generated-clock pin is declared
        # via SDC; and a plain ``create_clock`` aimed at an internal pin
        # declares the net just as firmly (#270). Either way there is
        # nothing for the user to add.
        if bit in bit_to_clock or bit in declared_bits:
            continue
        port = module.port_of_bit(bit)
        if port is not None and port.direction == "input":
            continue
        drv = drivers.get(bit)
        if drv is None:
            continue
        cell_name, out_port = drv
        cell = module.cells.get(cell_name)
        if cell is None:
            continue
        ctype = cell.type
        if is_ff_cell(ctype) and out_port == "Q":
            kind = "flop"
        elif ctype in _GATE_TYPES or (is_latch_cell(ctype) and out_port == "Q"):
            kind = "gate"
        else:
            # Driven by something that is not a recognised clock-network
            # source (e.g. an adder output wired into a CLK pin). That is
            # a different smell (CDC-008 territory); not a forwarded-clock
            # candidate, so we do not report it here.
            continue
        ordered = sorted(sinks)
        out.append(
            InferredClockCandidate(
                driver=cell_name,
                driver_kind=kind,
                fanout=len(ordered),
                example_sinks=tuple(ordered[:_INFERRED_CLOCK_SINK_SAMPLE]),
            )
        )
    # Stable order: widest fanout first, then driver name.
    out.sort(key=lambda c: (-c.fanout, c.driver))
    return out


# How many sink-flop cell names a CDC-023 finding quotes inline. The
# full list stays on ``ClockCombine.sinks``; the message names a few so
# a wide clock bank doesn't produce an unreadable line.
_CLOCK_COMBINE_SINK_SAMPLE = 3


def _bit_net_names(module: Module) -> dict[Bit, str]:
    """Map each net bit to a human-readable net name.

    Yosys emits both user-written names and auto-generated ``$...``
    names for the same bit; the user-written one is what a designer can
    search for, so it wins when both exist.
    """
    out: dict[Bit, str] = {}
    for nn in module.netnames.values():
        auto = nn.name.startswith("$")
        for b in nn.bits:
            if not isinstance(b, int):
                continue
            prev = out.get(b)
            if prev is None or (prev.startswith("$") and not auto):
                out[b] = nn.name
    return out


def find_clock_combines(
    module: Module,
    pin_clocks: dict[str, str] | None = None,
    *,
    clock_for_port: Callable[[str], str | None] | None = None,
    max_depth: int = 16,
) -> list[ClockCombine]:
    """Report every clock-network node the tracer *declined* on (#269).

    Runs the ordinary per-flop clock-root walk — same
    :func:`trace_clock_root`, same ``max_depth``, same
    :func:`_clock_identity_fn` predicate :func:`assign_domains` uses —
    with a recorder attached, and returns one :class:`ClockCombine` per
    distinct combining node reached.

    The findings therefore come from the tracer's own decline events
    rather than from a second, independent walk: a node appears here
    **iff** :func:`_pick_combining_root` declined on it, which is what
    keeps CDC-023 and the #263 decline from drifting apart as either
    side evolves.

    Events are deduplicated per ``(cell, driven bit)`` — the same gate is
    visited once per flop behind it, and possibly more than once per flop
    when the clock network reconverges — with the declared-clock sets
    unioned and the reaching flops collected as ``sinks``.

    Note that a declined node does not *always* leave its sinks
    domain-unknown: a decline on one leg of an upstream **mux** still
    lets the mux resolve through its other leg. The combining node is a
    real design smell either way, which is why the report is keyed on
    the node rather than on the unresolved flop.

    Returns ``[]`` when fewer than two clocks are declared — with at most
    one clock identity in play no combine is possible, so the walk is
    skipped entirely.
    """
    bit_to_clock = _build_bit_to_clock(module, pin_clocks)
    clock_identity = _clock_identity_fn(clock_for_port, set(bit_to_clock.values()))
    ports = [p.name for p in module.ports.values() if p.direction == "input"]
    declared = {c for c in (clock_identity(p) for p in ports) if c is not None}
    declared |= set(bit_to_clock.values())
    if len(declared) < 2:
        return []

    drivers = _bit_drivers(module)
    clocks_at: dict[tuple[str, Bit], set[str]] = defaultdict(set)
    sinks_at: dict[tuple[str, Bit], set[str]] = defaultdict(set)
    types_at: dict[tuple[str, Bit], str] = {}

    for f in find_flops(module):
        sink_name = f.cell.name

        def record(
            cell: str,
            cell_type: str,
            bit: Bit,
            clocks: frozenset[str],
            _sink: str = sink_name,
        ) -> None:
            key = (cell, bit)
            clocks_at[key] |= clocks
            sinks_at[key].add(_sink)
            types_at[key] = cell_type

        trace_clock_root(
            module,
            f.clk,
            drivers,
            max_depth=max_depth,
            bit_to_clock=bit_to_clock,
            clock_identity=clock_identity,
            on_combine=record,
        )

    net_names = _bit_net_names(module)
    out = [
        ClockCombine(
            net=net_names.get(bit, f"{cell}.Y"),
            cell=cell,
            cell_type=types_at[(cell, bit)],
            clocks=tuple(sorted(clocks)),
            sinks=tuple(sorted(sinks_at[(cell, bit)])),
        )
        for (cell, bit), clocks in clocks_at.items()
    ]
    # Stable order: net name, then cell — the report must not depend on
    # netlist dict ordering.
    out.sort(key=lambda c: (c.net, c.cell))
    return out


def find_scan_mode_flops(
    module: Module,
    *,
    is_scan_control: Callable[[tuple[Bit, ...]], bool],
    pin_clocks: dict[str, str] | None = None,
    clock_for_port: Callable[[str], str | None] | None = None,
    max_depth: int = 16,
) -> set[str]:
    """Cell names of flops clocked through a DFT scan structure (#45).

    Runs the ordinary per-flop clock-root walk — same
    :func:`trace_clock_root`, same ``max_depth``, same
    :func:`_clock_identity_fn` predicate :func:`assign_domains` uses —
    with the :data:`ClockControlSink` recorder attached, and returns the
    flops whose walk passed through a selection / gating cell whose
    control inputs satisfy ``is_scan_control``.

    The predicate is **injected** rather than implemented here: what
    counts as a scan control is an SV-attribute question owned by
    ``rules.SCAN_MODE_ATTRS``, and ``rules`` imports ``domain``, not the
    other way round. :func:`rtl_buddy_cdc.rules.scan_mode_clock_select_flops`
    supplies the real one (backward-walk the control bits to the
    top-level input ports and test them against the tagged set); tests
    supply trivial ones. It is called with the raw control bits of one
    cell and answers yes/no.

    Same construction as :func:`find_clock_combines` (#269), for the
    same reason: the fact is read off the walk that already resolves
    the clock, so "this flop is clocked through a scan mux" and "the
    tracer walked a mux to get here" cannot drift apart. A second,
    independent traversal is exactly what this avoids.

    Report-only in the same sense the combine report is: the returned
    names are stamped onto :attr:`Crossing.scan_mode` and change no
    domain, no crossing, and — absent ``--ignore-scan-mode`` — no
    finding.

    Caveat, deliberately on the conservative side of the tag: the
    recorder fires while a leg is being *explored*, so a cell an
    enclosing gate later declines on can still be recorded. That can
    only ever mark MORE crossings as scan-related, never fewer, and the
    tag alone suppresses nothing.
    """
    drivers = _bit_drivers(module)
    bit_to_clock = _build_bit_to_clock(module, pin_clocks)
    clock_identity = _clock_identity_fn(clock_for_port, set(bit_to_clock.values()))

    out: set[str] = set()
    for f in find_flops(module):
        hits: list[tuple[Bit, ...]] = []

        def record(
            _cell: str,
            _cell_type: str,
            control_bits: tuple[Bit, ...],
            _sink: list[tuple[Bit, ...]] = hits,
        ) -> None:
            _sink.append(control_bits)

        trace_clock_root(
            module,
            f.clk,
            drivers,
            max_depth=max_depth,
            bit_to_clock=bit_to_clock,
            clock_identity=clock_identity,
            on_clock_control=record,
        )
        if any(is_scan_control(bits) for bits in hits):
            out.add(f.cell.name)
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
    max_depth: int = 16,
    scan_mode_flops: frozenset[str] = frozenset(),
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

    ``max_depth`` is forwarded to :func:`assign_domains` (default 16)
    so the per-flop domain identities that feed the crossing walk use
    the same clock-trace hop budget the CLI exposes as
    ``--clock-trace-depth``. Raising it only resolves more flops; the
    crossing walk's own ``max_hops`` data-fanout budget is unrelated
    and unchanged. See issue #263.

    ``scan_mode_flops`` (issue #45, typically from
    :func:`find_scan_mode_flops`) names the flops clocked through a DFT
    scan structure. Every emitted crossing landing on one is stamped
    ``scan_mode=True``. Tagging only — the set never adds, drops or
    reshapes a crossing, so a run that passes it and a run that does
    not produce the same crossings in the same order.
    """
    domains = {
        fd.flop.cell.name: fd
        for fd in assign_domains(
            module, pin_clocks, clock_for_port=clock_for_port, max_depth=max_depth
        )
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
                # flop, propagating along the data lanes it actually drives
                # (see _lane_targets — bit-precise for bitwise/mux, all-outputs
                # otherwise).
                for cell_name, cport, idx in consumers.get(bit, ()):
                    cell = module.cells[cell_name]
                    if cell.type in {"$scopeinfo"}:
                        continue
                    # Skip flops as transit nodes — we already check above
                    # whether we landed on a flop's D pin.
                    if is_ff_cell(cell.type):
                        continue
                    out_bits = _lane_targets(cell, cport, idx)
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
            scan_mode=g["dst_flop"].cell.name in scan_mode_flops,
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
                    for cell_name, cport, idx in consumers.get(bit, ()):
                        cell = module.cells[cell_name]
                        if cell.type in {"$scopeinfo"}:
                            continue
                        if is_ff_cell(cell.type):
                            continue
                        for ob in _lane_targets(cell, cport, idx):
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
                    scan_mode=pg["dst_flop"].cell.name in scan_mode_flops,
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
                    scan_mode=bg["dst_flop"].cell.name in scan_mode_flops,
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


def filter_async(crossings: list[Crossing], spec: "ClockSpec") -> list[Crossing]:
    """Keep only the crossings the rule pack should see.

    A crossing survives iff its endpoints resolve to *different* clocks
    (so a generated clock folds back into its master first), the resolved
    roots are not in different exclusive groups (logically / physically
    exclusive clocks never coexist, so the path is unreachable), and the
    roots are declared asynchronous via ``set_clock_groups -asynchronous``
    or ``set_false_path -from/-to``.

    Lives here rather than in ``cli.py`` because the compositional
    per-module pass (#261) must filter a *subtree's* crossings with
    exactly the same predicate the top-level run uses — two answers to
    "is this crossing async?" is how an abstracted run and a flat run
    start to disagree. ``cli._filter_async`` is a thin alias.
    """
    out: list[Crossing] = []
    for c in crossings:
        a = spec.clock_for_port(c.src_clock) or c.src_clock
        b = spec.clock_for_port(c.dst_clock) or c.dst_clock
        if spec.is_unreachable_crossing(a, b):
            continue
        if spec.are_async(a, b):
            out.append(c)
    return out


def _cell_outputs(cell):
    out_ports = _OUTPUT_PORTS_BY_TYPE.get(cell.type, _DEFAULT_OUTPUT_PORTS)
    bits: list = []
    for p in out_ports:
        bits.extend(cell.connections.get(p, ()))
    return bits


def _lane_targets(cell, port: str, idx: int):
    """Output bits reachable from input ``port[idx]`` of ``cell``.

    For a width-preserving bitwise / mux-data cell, input lane ``idx`` drives
    only output ``Y[idx]`` — so the data fanout stays bit-precise instead of
    fanning a single source bit across the whole bus. Falls back to
    ``_cell_outputs`` (all outputs) for every other cell type and for any port
    whose width doesn't match ``Y`` (e.g. a mux select, or the wide ``B`` of a
    ``$pmux``), which is a sound over-approximation. This removes the O(width^2)
    fanout the all-outputs walk does on wide buses without dropping any real
    cross-lane path; see issue #258.
    """
    if cell.type in _LANE_ALIGNED_TYPES:
        ybits = cell.connections.get("Y", ())
        inbits = cell.connections.get(port, ())
        if len(inbits) == len(ybits) and 0 <= idx < len(ybits):
            return [ybits[idx]]
    return _cell_outputs(cell)
