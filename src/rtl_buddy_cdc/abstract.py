"""Auto-abstract single-clock subtrees (CDC scaling phase 2, #256).

A subtree whose entire clock set sits inside one synchronous /
async-safe domain carries **no internal crossing**: every register in
it is clocked from the same domain, so register→register paths inside
never cross. Such a subtree can be summarised down to its port
boundary (the P0/#254 :class:`~rtl_buddy_cdc.netlist.BoundarySummary`)
and analysed as a boundary cell rather than flattened flop-by-flop —
the highest-leverage, safe scaling win. The user no longer needs to
know which subtrees are single-clock; this module decides it off the
SDC.

Two pure helpers:

- :func:`is_single_clock_subtree` — the *detector* (subtask 2a). Given
  a candidate's clock set and the parsed SDC, decide whether the whole
  set collapses to one async-safe domain.
- :func:`summarise_subtree` — the *summariser* (subtask 2b). Build a
  :class:`BoundarySummary` for a blackboxed module from the way its
  parent drives the instance's clock pin, so the parent's boundary
  cell can re-seed the crossings the flattened subtree would have
  produced **at its boundary** without walking the internals.

Both are I/O-free and live in the analyzer layer; the orchestration
that loads the blackbox siblings and attaches the summaries lives in
``cli.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rtl_buddy_cdc.domain import _bit_drivers, trace_clock_root
from rtl_buddy_cdc.flops import flop_clk_pin, is_ff_cell
from rtl_buddy_cdc.netlist import Bit, BoundarySummary, Cell, Module, PortBoundary
from rtl_buddy_cdc.primitives import clock_pin_role, port_domain
from rtl_buddy_cdc.sdc import UNCONSTRAINED_SENTINEL as _UNCONSTRAINED, ClockSpec

if TYPE_CHECKING:
    from rtl_buddy_cdc.compositional import ModuleAnalysis

# Yosys cell-port names a blackbox instance may use to carry a clock.
# A summarised subtree is described by its *data* boundary; the clock
# pin is consumed here (to learn the subtree's domain) and not emitted
# as a data port.
_CLOCK_PIN_NAMES: frozenset[str] = frozenset({"CLK", "clk", "C", "clock", "clk_i"})

# Substrings that mark a port name as clock-like for the FIX-1 traced
# clock-pin classifier. A *non*-allow-listed port is only treated as a
# clock pin when its name looks like a clock (``wr_clk`` / ``rd_clk`` /
# ``clk_a`` / ``core_clock`` …) AND its driver traces to a declared
# clock. The name gate is what keeps a genuine DATA input that happens
# to be (mis)wired to a clock net — the CDC-008 clock-as-data bug FIX 4
# must still catch — from being silently absorbed as a clock pin and
# thereby exempted. Clock pins are conventionally named; data pins are
# not.
_CLOCK_NAME_HINTS: tuple[str, ...] = ("clk", "clock", "ck")

# Yosys combinational output pin names. Used by the #273 clock-output
# forward walk to tell a cell's driven bits from the bits it reads
# (``Q`` is a flop output, but the walk never enters a flop anyway).
_COMB_OUTPUT_PINS: frozenset[str] = frozenset({"Y", "Q"})


def is_single_clock_subtree(clocks: set[str], spec: ClockSpec) -> bool:
    """True iff ``clocks`` collapses to a single async-safe domain.

    A subtree carries no internal crossing when every clock driving it
    resolves (via ``create_generated_clock`` master chains) to the same
    root *and* no two of its clocks are declared asynchronous /
    false-pathed against each other. The empty set (a purely
    combinational subtree) and the singleton set are trivially
    single-clock.

    ``None`` and the unconstrained sentinel never count as a known
    single clock — a subtree we can't pin to one domain must be walked,
    not abstracted (conservative: we never *drop* a crossing we cannot
    prove absent).
    """
    known = {c for c in clocks if c}
    if len(known) != len(clocks):
        # A None / empty clock slipped in: domain unknown, don't abstract.
        return False
    if len(known) <= 1:
        return True
    # Multiple named clocks: single-domain only if they're pairwise
    # synchronous (no async / exclusive / false-path partition among
    # them). ``are_async`` already folds generated→master resolution.
    clock_list = sorted(known)
    for i, a in enumerate(clock_list):
        for b in clock_list[i + 1 :]:
            if spec.are_async(a, b):
                return False
            if spec.is_unreachable_crossing(a, b):
                return False
            if spec.resolve(a) != spec.resolve(b):
                # Different roots, not declared async: from a CDC
                # standpoint we can't prove they're the same domain, so
                # the subtree might carry a (silent) crossing. Be
                # conservative and refuse to abstract.
                return False
    return True


@dataclass(frozen=True)
class _InstanceClocks:
    """The clock determination for one blackbox instance.

    ``roots`` is the distinct set of top-level clock roots driving the
    instance's clock pins; ``clock_pins`` is the set of *port names*
    determined to be clock pins (so they can be excluded from data-sink
    seeding). A port is a clock pin when its name is in
    :data:`_CLOCK_PIN_NAMES` **or** its parent-side driver traces to a
    clock root — the name allow-list is only a hint, not the authority
    (FIX 1, soundness audit of #259).
    """

    roots: frozenset[str]
    clock_pins: frozenset[str]
    # (#261) The pin -> root MAPPING, sorted, with ``None`` for a clock
    # pin whose driver did not resolve. ``roots`` alone cannot serve as
    # the compositional cache key: two instances of a dual-clock module
    # wired src/dest in opposite directions carry the same root *set* but
    # need opposite per-port domains — the same collision §4.10 records
    # for the sync-primitive path. Keying on the mapping is a strict
    # refinement, so identical instances still share one analysis.
    pin_roots: tuple[tuple[str, str | None], ...] = ()

    def pin_root_map(self) -> dict[str, str | None]:
        """The clock pin -> parent root mapping as a dict."""
        return dict(self.pin_roots)


def _looks_like_clock_name(port: str) -> bool:
    """True iff ``port`` is conventionally named like a clock pin.

    Used by the FIX-1 traced clock-pin classifier to gate non-allow-listed
    ports: a clock pin is conventionally named (``wr_clk`` / ``rd_clk`` /
    ``clk_a`` / ``core_clock``), a data pin is not. The gate keeps a
    genuine data input that is (mis)wired to a clock net from being
    absorbed as a clock pin, so CDC-008 (FIX 4) still fires on it.
    """
    low = port.lower()
    return any(h in low for h in _CLOCK_NAME_HINTS)


def _is_known_clock(root: str, spec: ClockSpec | None) -> bool:
    """True iff ``root`` names an SDC-declared clock (or clock port).

    Used to decide whether a non-allow-listed blackbox port that *traces*
    to ``root`` is a clock pin. A data input wired straight from a
    top-level *data* port (e.g. ``d_in``) traces to that port name but is
    not a clock — only a ``create_clock`` /
    ``create_generated_clock`` identity counts. Without an SDC we cannot
    prove a non-allow-listed port is a clock, so it is treated as data
    (conservative for abstraction: an unrecognised second clock that we
    misread as data would only *over*-decline, never drop a crossing —
    but with no SDC there are no domains to cross anyway).
    """
    if spec is None:
        return False
    if root in spec.clocks:
        return True
    clk = spec.clock_for_port(root)
    # An input typed ``<unconstrained>`` (no create_clock, just a sentinel
    # so CDC-011 can fire) is explicitly NOT a real clock — a data input
    # straight from such a port must stay data, not be read as a clock
    # pin. A port whose typed clock is itself a declared clock counts.
    return clk is not None and clk != _UNCONSTRAINED and clk in spec.clocks


def _instance_clocks(
    parent: Module,
    instance: Cell,
    sub: Module | None = None,
    *,
    spec: ClockSpec | None = None,
    pin_clocks: dict[str, str] | None = None,
    max_depth: int = 16,
) -> _InstanceClocks:
    """Determine the clock SET (and clock-pin port names) of a subtree.

    The historical (pre-#259) implementation only recognised a clock
    pin whose *name* was in :data:`_CLOCK_PIN_NAMES` and returned a
    single root, so a dual-clock IP whose clock pins are ``wr_clk`` /
    ``rd_clk`` (or ``clk_a`` / ``clk_b``) silently resolved to a single
    domain and was abstracted as if combinational — its internal
    clkA->clkB crossing vanished. We instead inspect **every** input
    port of the instance: a port is a clock pin if its name is in the
    allow-list OR its parent-side driver traces (via
    :func:`trace_clock_root`, which only follows clock-network shapes)
    to a clock root. The full set of distinct roots flows to
    :func:`is_single_clock_subtree`, so >=2 distinct roots correctly
    decline abstraction.

    When ``sub`` is given, the instance's *input* ports are taken from
    the blackbox module's port directions (the authoritative input
    list). Without it we fall back to every connected pin that isn't a
    known output pin — used by the diagnostic helper. Output ports of a
    blackbox are never clock pins.

    ``max_depth`` is the clock-trace hop budget forwarded to
    :func:`trace_clock_root` (default 16, surfaced as
    ``--clock-trace-depth``). It MUST match the budget the crossing walk
    uses on the same run: the abstraction decision and the crossing walk
    have to resolve the same set of clock roots, or a deep clock pin the
    walk would resolve could be dropped here and collapse a genuinely
    multi-clock boundary to a (false) single-clock summary — the
    silent-drop hazard the brief forbids. See issue #263.
    """
    drivers = _bit_drivers(parent)
    from rtl_buddy_cdc.domain import _build_bit_to_clock

    bit_to_clock = _build_bit_to_clock(parent, pin_clocks)
    roots: set[str] = set()
    clock_pins: set[str] = set()
    pin_roots: dict[str, str | None] = {}

    if sub is not None:
        candidate_ports = [
            p.name for p in sub.ports.values() if p.direction in ("input", "inout")
        ]
    else:
        candidate_ports = list(instance.connections)

    for pin in candidate_ports:
        bits = instance.connections.get(pin)
        if not bits:
            continue
        if pin in _CLOCK_PIN_NAMES:
            # Name-allow-listed clock pin: trace with the full clock
            # walker (divider rule included — a forwarded / divided clock
            # on an explicit clock pin is legitimate). It is a clock pin
            # regardless of whether the trace resolves.
            root = trace_clock_root(
                parent,
                bits[0],
                drivers,
                max_depth=max_depth,
                bit_to_clock=bit_to_clock,
            )
            clock_pins.add(pin)
            pin_roots[pin] = root
            if root is not None:
                roots.add(root)
        elif _looks_like_clock_name(pin):
            # Non-allow-listed but clock-NAMED port (``wr_clk`` / ``rd_clk``
            # / ``clk_a`` …): a clock pin only if its driver is on the
            # *clock distribution network* (ports / buffers / gates /
            # muxes) AND resolves to a declared clock. ``allow_divider=False``
            # rejects a flop Q-as-divider step — the shape of an ordinary
            # data path launched by a flop — so a data input driven by a
            # foreign-domain flop is never misread as a second clock. The
            # name gate (above) means a genuine DATA input mis-wired to a
            # clock net is NOT absorbed here, so CDC-008 (FIX 4) still
            # fires on it.
            root = trace_clock_root(
                parent,
                bits[0],
                drivers,
                max_depth=max_depth,
                bit_to_clock=bit_to_clock,
                allow_divider=False,
            )
            if root is not None and _is_known_clock(root, spec):
                clock_pins.add(pin)
                pin_roots[pin] = root
                roots.add(root)
    return _InstanceClocks(
        roots=frozenset(roots),
        clock_pins=frozenset(clock_pins),
        pin_roots=tuple(sorted(pin_roots.items())),
    )


def _instance_clock(
    parent: Module,
    instance: Cell,
    *,
    spec: ClockSpec | None = None,
    pin_clocks: dict[str, str] | None = None,
    max_depth: int = 16,
) -> str | None:
    """Resolve the *single* clock root feeding a blackbox instance.

    Back-compat single-root view used by the diagnostic helper and the
    name-allow-list fixtures. Returns the sole root when the instance
    resolves to exactly one, else ``None`` (no clock pin, an unresolved
    pin, or — deliberately — a multi-clock instance, which the
    summariser declines via :func:`_instance_clocks`).
    """
    ic = _instance_clocks(
        parent, instance, spec=spec, pin_clocks=pin_clocks, max_depth=max_depth
    )
    if len(ic.roots) == 1:
        return next(iter(ic.roots))
    return None


def summarise_subtree(
    parent: Module,
    instance: Cell,
    sub: Module,
    spec: ClockSpec,
    *,
    pin_clocks: dict[str, str] | None = None,
    max_depth: int = 16,
    analysis: "ModuleAnalysis | None" = None,
) -> BoundarySummary | None:
    """Summarise a blackboxed subtree to its port boundary.

    ``parent`` is the (flattened) top module, ``instance`` the ordinary
    cell whose ``type`` is the blackbox module name, and ``sub`` the
    blackbox sibling :class:`Module`. ``spec`` is the parsed SDC.

    Returns a :class:`BoundarySummary` keyed by port name, or ``None``
    when the subtree cannot be soundly abstracted (the caller then leaves
    the instance opaque and reports it as a ``CDC-BBX`` coverage finding
    — never silently dropped).

    **Without ``analysis`` (a stub blackbox: zero cells).** Unchanged
    from #256/#257/#259. The subtree must be provably single-clock or it
    is declined; every output/inout port is summarised at ``src_clock`` =
    that single domain with ``synchronised=False`` (we do not assume the
    IP synchronises data leaving it), and every non-clock input/inout
    port becomes a virtual sink captured in the same domain.

    **With ``analysis`` (a greybox: the module kept its cells, #261).**
    The module's internals have been run through the ordinary pipeline by
    :func:`rtl_buddy_cdc.compositional.analyse_module`, so the facts the
    star-collapse used to erase are available and get lifted:

    - Every port carries its proven ``sync_depth`` / ``synchronised``
      bit, so the boundary rules see the chain the IP really has.
    - ``reconvergent_inputs`` travels with the summary, which is what
      lets the #259 reconvergence gate stand down for this instance.
    - A **multi-clock** module is no longer declined outright: each port
      is stamped with the domain that actually captures / launches it, so
      the collapse becomes per-domain instead of a single hub. The single
      -clock case deliberately keeps its whole-block domain for every
      port — identical to the pre-#261 result, so nothing regresses.

    Three sub-cases still decline, because no sound summary exists:

    1. any internal flop whose clock domain did not resolve
       (:attr:`~rtl_buddy_cdc.compositional.ModuleAnalysis.resolved`) —
       the pass cannot see the crossings that flop takes part in, so
       claiming coverage would be the exact silent drop #253/#259 closed;
    2. a multi-clock module with an **ambiguous input port** — one
       captured in two internal domains cannot be expressed by a single
       virtual sink, and picking either domain would drop the other's
       crossing;
    3. a multi-clock module with **no resolvable clock roots at all**.

    ``max_depth`` is forwarded to :func:`_instance_clocks` (default 16,
    surfaced as ``--clock-trace-depth``). It must match the budget the
    crossing walk uses so the abstraction decision sees the same clock
    roots — otherwise a deep clock pin the walk resolves could be missed
    here and collapse a multi-clock boundary to a false single-clock
    summary, silently dropping its internal crossing. See issue #263.
    """
    ic = _instance_clocks(
        parent, instance, sub, spec=spec, pin_clocks=pin_clocks, max_depth=max_depth
    )
    # The subtree's clock set: ALL distinct roots driving the instance's
    # clock pins (FIX 1). A dual-clock IP (e.g. ``wr_clk`` / ``rd_clk``)
    # therefore presents >=2 roots. Without internals that is a decline —
    # its internal clkA->clkB crossing would otherwise be silently
    # abstracted away. The empty set is a genuinely combinational boundary.
    single_clock = is_single_clock_subtree(set(ic.roots), spec)
    if analysis is None:
        if not single_clock:
            return None
        return _summarise_single_clock(sub, ic, None)
    if not analysis.resolved:
        # Internals present but not fully domain-resolved: refuse to vouch.
        return None
    if single_clock:
        return _summarise_single_clock(sub, ic, analysis)
    return _summarise_multi_clock(sub, ic, analysis)


def _summarise_single_clock(
    sub: Module,
    ic: _InstanceClocks,
    analysis: "ModuleAnalysis | None",
) -> BoundarySummary:
    """The classic star-collapse onto one domain (#256/#257).

    Every port is stamped with the subtree's single clock — including
    ports the compositional pass could not attribute individually, which
    is deliberate: with one internal domain there is nothing else they
    could be, and keeping the whole-block answer makes the greybox result
    bit-identical to the stub-blackbox one apart from the added
    ``sync_depth`` / ``synchronised`` / reconvergence facts.
    """
    # The single capture clock is the sole root, or None when the set is
    # empty (combinational) — never a "first one wins" pick.
    clk = next(iter(ic.roots)) if ic.roots else None
    # Result-preserving by construction (P3/#257): the boundary summary
    # seeds output-side virtual *sources* (``ports``) AND input-side
    # virtual *sinks* (``input_ports``, captured in the boundary's own
    # ``clock`` domain). A foreign-domain signal the parent drives into
    # a data input no longer vanishes — ``find_crossings`` re-creates the
    # crossing the flattened subtree would have reported at its first
    # internal flop.
    ports: dict[str, PortBoundary] = {}
    input_ports: dict[str, PortBoundary] = {}
    for port in sub.ports.values():
        facts = analysis.ports.get(port.name) if analysis is not None else None
        if port.direction in ("output", "inout"):
            ports[port.name] = PortBoundary(
                port=port.name,
                src_clock=clk,
                synchronised=facts.synchronised if facts is not None else False,
                width=len(port.bits),
                sync_depth=facts.sync_depth if facts is not None else None,
                user_synchronised=(
                    facts.user_synchronised if facts is not None else False
                ),
            )
        if port.direction in ("input", "inout") and port.name not in ic.clock_pins:
            # Clock pins are excluded by the *traced* determination (FIX
            # 1), not just the name allow-list: they carry distribution
            # into the subtree, not data, and must never become a virtual
            # sink (that would re-introduce the CDC-008 clock-as-data
            # shape).
            input_ports[port.name] = PortBoundary(
                port=port.name,
                src_clock=clk,
                synchronised=facts.synchronised if facts is not None else False,
                width=len(port.bits),
                sync_depth=facts.sync_depth if facts is not None else None,
                user_synchronised=(
                    facts.user_synchronised if facts is not None else False
                ),
            )
    return BoundarySummary(
        module=sub.name,
        ports=ports,
        clock=clk,
        input_ports=input_ports,
        internal_analysed=analysis is not None,
        reconvergent_inputs=(
            analysis.reconvergent_inputs if analysis is not None else frozenset()
        ),
    )


def _summarise_multi_clock(
    sub: Module,
    ic: _InstanceClocks,
    analysis: "ModuleAnalysis",
) -> BoundarySummary | None:
    """Per-port summary of a MULTI-clock module whose internals were analysed.

    The gate #259 could not lift without this: a dual-clock IP carries a
    real internal crossing, and with the internals opaque there was no
    honest way to place its ports in a domain. Now each port is stamped
    with the domain that genuinely captures (input) or launches (output)
    it, so the parent's boundary walk is exact on both sides while the
    internal crossing is analysed once, on the module itself.

    Declines (``None``) when the module has no resolvable clock roots at
    all, or when an input port is captured in **two** internal domains —
    one virtual sink cannot stand for two capture domains, and choosing
    either would drop the other's crossing.
    """
    if not analysis.clock_roots:
        return None
    ports: dict[str, PortBoundary] = {}
    input_ports: dict[str, PortBoundary] = {}
    for port in sub.ports.values():
        if port.name in ic.clock_pins:
            continue
        # ``analyse_module`` describes every port except the traced clock
        # pins, which are exactly what the loop above skips.
        facts = analysis.ports[port.name]
        if port.direction in ("output", "inout"):
            # ``facts.clock`` None (comb feed-through, or launched from
            # several domains) becomes the ``<unconstrained>`` source that
            # crosses to every known-domain sink — the documented
            # conservative direction.
            ports[port.name] = PortBoundary(
                port=port.name,
                src_clock=facts.clock,
                synchronised=facts.synchronised,
                width=len(port.bits),
                sync_depth=facts.sync_depth,
                user_synchronised=facts.user_synchronised,
            )
        if port.direction in ("input", "inout"):
            if facts.ambiguous:
                return None
            if facts.clock is None:
                # Nothing sequential captures this input, so there is no
                # crossing INTO the block here. Whatever it feeds leaves
                # through an output, which is seeded as its own source
                # (unconstrained when it is a comb feed-through), so the
                # path is still covered.
                continue
            input_ports[port.name] = PortBoundary(
                port=port.name,
                src_clock=facts.clock,
                synchronised=facts.synchronised,
                width=len(port.bits),
                sync_depth=facts.sync_depth,
                user_synchronised=facts.user_synchronised,
            )
    return BoundarySummary(
        module=sub.name,
        ports=ports,
        clock=None,
        input_ports=input_ports,
        internal_analysed=True,
        reconvergent_inputs=analysis.reconvergent_inputs,
    )


def summarise_sync_primitive(
    parent: Module,
    instance: Cell,
    sub: Module,
    spec: ClockSpec,
    *,
    pin_clocks: dict[str, str] | None = None,
    max_depth: int = 16,
) -> BoundarySummary | None:
    """Summarise a recognised CDC macro (issue #275) as a *synchroniser*.

    The sibling of :func:`summarise_subtree` for instances whose module
    type is in the sanctioned-primitive registry
    (:mod:`rtl_buddy_cdc.primitives`). The generic summariser declines
    these — an XPM CDC macro is dual-clock by construction, so
    :func:`is_single_clock_subtree` is False and the instance becomes a
    ``CDC-BBX`` coverage finding while its crossing silently vanishes.
    Here the multi-clock-ness is the *point*: the macro is the
    synchroniser.

    The summary exploits the family's rigid port convention:

    - Each **output** port is stamped with the domain that actually
      drives it — ``dest_*`` outputs at the ``dest_clk`` root, ``src_*``
      outputs (``xpm_cdc_handshake.src_rcv``) at the ``src_clk`` root —
      and marked ``synchronised=True``. The port *width* is taken from
      the parent-side connection, not from ``sub.ports``: a ``dynports``
      blackbox stub reports its default-parameter width, not the
      instantiated one.
    - ``input_ports`` is left **empty**, so no virtual sink is seeded.
      A crossing into a recognised synchroniser's data input is safe by
      construction — that is the whole reason the macro exists — and
      with no boundary sink the reconvergence gate can never trip on it
      either.

    Note what this does *not* suppress: because each output carries the
    domain it is really driven in, a ``dest_out`` consumed by a flop in
    some *third* domain is still emitted as a crossing by the existing
    ``dst_clock != src_clock`` test in
    :func:`~rtl_buddy_cdc.domain.find_crossings`. We accept the crossing
    the macro handles and keep the one it doesn't.

    Returns ``None`` — deferring to the generic path, i.e. declining —
    when the instance's destination-side clock pin cannot be identified.
    That happens when ``dest_clk`` is driven by something that doesn't
    resolve to a declared clock (an undeclared port, an unresolved
    generated clock); refusing to vouch for the macro there is the
    conservative reading, and CDC-021 / CDC-BBX then say why.
    """
    ic = _instance_clocks(
        parent, instance, sub, spec=spec, pin_clocks=pin_clocks, max_depth=max_depth
    )
    if not ic.clock_pins:
        return None
    drivers = _bit_drivers(parent)
    from rtl_buddy_cdc.domain import _build_bit_to_clock

    bit_to_clock = _build_bit_to_clock(parent, pin_clocks)

    roles: dict[str, str | None] = {}
    unclassified: list[str | None] = []
    for pin in sorted(ic.clock_pins):
        bits = instance.connections.get(pin)
        root = (
            trace_clock_root(
                parent, bits[0], drivers, max_depth=max_depth, bit_to_clock=bit_to_clock
            )
            if bits
            else None
        )
        role = clock_pin_role(pin)
        if role is None:
            unclassified.append(root)
        else:
            roles.setdefault(role, root)
    if "dest" not in roles:
        # A single unclassifiable clock pin is the destination clock (the
        # only clock the macro has). Two or more and we can't tell which
        # side is which — decline rather than guess.
        if len(ic.clock_pins) == 1 and unclassified:
            roles["dest"] = unclassified[0]
        else:
            return None
    dest_clock = roles["dest"]
    # A macro with no separate source clock pin (``xpm_cdc_sync_rst`` /
    # ``xpm_cdc_async_rst`` take an asynchronous *reset*, not a clock)
    # drives everything from the destination side.
    src_clock = roles.get("src", dest_clock)

    ports: dict[str, PortBoundary] = {}
    for port in sub.ports.values():
        if port.direction not in ("output", "inout"):
            continue
        if port.name in ic.clock_pins:
            continue
        conn = instance.connections.get(port.name)
        width = len(conn) if conn else len(port.bits)
        side = port_domain(instance.type, port.name)
        ports[port.name] = PortBoundary(
            port=port.name,
            src_clock=src_clock if side == "src" else dest_clock,
            synchronised=True,
            width=width,
        )
    return BoundarySummary(
        module=sub.name,
        ports=ports,
        clock=dest_clock,
        input_ports={},
    )


def boundary_instance_clocks(parent: Module, *, max_depth: int = 16) -> set[str]:
    """Diagnostic: the clock-pin domains every blackbox instance carries.

    Not used in the main path; handy for callers / tests that want to
    see what domains the parent feeds into its boundary cells.
    ``max_depth`` is the clock-trace hop budget (default 16).
    """
    drivers = _bit_drivers(parent)
    out: set[str] = set()
    for cell in parent.cells.values():
        if is_ff_cell(cell.type):
            continue
        clk_bits = flop_clk_pin(cell)
        bits: tuple[Bit, ...] | None = clk_bits
        if bits is None:
            for pin in _CLOCK_PIN_NAMES:
                b = cell.connections.get(pin)
                if b:
                    bits = b
                    break
        if bits:
            root = trace_clock_root(parent, bits[0], drivers, max_depth=max_depth)
            if root is not None:
                out.add(root)
    return out


def instance_clock_pins(
    parent: Module,
    instance: Cell,
    sub: Module | None = None,
    *,
    spec: ClockSpec | None = None,
    pin_clocks: dict[str, str] | None = None,
    max_depth: int = 16,
) -> frozenset[str]:
    """The set of port names determined to be clock pins on ``instance``.

    Public accessor for FIX 4 (CDC-008): a clock net wired into a
    blackbox's CLOCK pin is distribution and must not fire clock-as-data,
    but a clock wired into a genuine DATA input still must. Uses the same
    traced determination as the summariser, so the two never disagree.
    """
    return _instance_clocks(
        parent, instance, sub, spec=spec, pin_clocks=pin_clocks, max_depth=max_depth
    ).clock_pins


def _bit_consumers(parent: Module) -> dict[Bit, list[tuple[str, str]]]:
    """Map each net bit to the ``(cell_name, pin)`` sites that read it.

    Unlike :func:`~rtl_buddy_cdc.domain._build_bit_consumers` this keeps
    ``CLK`` connections: the clock-output walk below is *looking* for a
    clock pin, so dropping them would blind it to the very sink it must
    find.
    """
    out: dict[Bit, list[tuple[str, str]]] = {}
    for cell in parent.cells.values():
        for pin, bits in cell.connections.items():
            for b in bits:
                if isinstance(b, int):
                    out.setdefault(b, []).append((cell.name, pin))
    return out


def blackbox_clock_pins_by_module(
    parent: Module,
    blackboxes: dict[str, Module],
    *,
    spec: ClockSpec | None = None,
    pin_clocks: dict[str, str] | None = None,
    max_depth: int = 16,
) -> dict[str, frozenset[str]]:
    """Per blackbox module type, the UNION of its instances' clock pins.

    A clock-forwarding mesh wires one tile's ``clk_out`` into the next
    tile's ``clk_in``. The *downstream* instance's ``clk_in`` does not
    trace to a declared clock (its driver is an opaque boundary output),
    so :func:`_instance_clocks` cannot classify it on its own. But the
    *upstream* instance of the same module type is driven from a real
    clock, and that instance proves ``clk_in`` is a clock pin **of the
    module**. Taking the union across instances is what lets the
    clock-output walk (:func:`clock_driving_output_ports`) recognise the
    forwarded clock's sink structurally, without falling back to
    guessing from the pin's name (which would misread ``dest_ack`` — it
    contains the ``ck`` hint — as a clock pin). See issue #273.
    """
    out: dict[str, set[str]] = {}
    for cell in parent.cells.values():
        sub = blackboxes.get(cell.type)
        if sub is None:
            continue
        pins = _instance_clocks(
            parent, cell, sub, spec=spec, pin_clocks=pin_clocks, max_depth=max_depth
        ).clock_pins
        out.setdefault(cell.type, set()).update(pins)
    return {k: frozenset(v) for k, v in out.items()}


def _clock_sink_bits(
    parent: Module,
    clock_pins_by_module: dict[str, frozenset[str]],
    *,
    spec: ClockSpec | None,
    pin_clocks: dict[str, str] | None,
) -> frozenset[Bit]:
    """Bits that are *consumed as a clock* somewhere in ``parent``.

    Exactly the three sink kinds issue #273 enumerates, and no name
    guessing beyond them:

    1. a flop ``CLK`` / ``C`` pin (:func:`~rtl_buddy_cdc.flops.flop_clk_pin`),
    2. a **clock input pin of a blackbox / boundary instance**, taken from
       the module-level union in :func:`blackbox_clock_pins_by_module`,
    3. an **SDC-declared clock net** — a ``create_clock`` port (input or
       output: forwarding a clock off-chip counts) or a
       ``create_generated_clock`` internal-pin target (``pin_clocks``).
    """
    sinks: set[Bit] = set()
    for cell in parent.cells.values():
        if is_ff_cell(cell.type):
            for b in flop_clk_pin(cell) or ():
                if isinstance(b, int):
                    sinks.add(b)
            continue
        for pin in clock_pins_by_module.get(cell.type, frozenset()):
            for b in cell.connections.get(pin, ()):
                if isinstance(b, int):
                    sinks.add(b)
    from rtl_buddy_cdc.domain import _build_bit_to_clock

    sinks.update(_build_bit_to_clock(parent, pin_clocks))
    if spec is not None:
        for clk in spec.clocks.values():
            for port_name in clk.ports:
                port = parent.ports.get(port_name)
                if port is None:
                    continue
                sinks.update(b for b in port.bits if isinstance(b, int))
    return frozenset(sinks)


def clock_driving_output_ports(
    parent: Module,
    instance: Cell,
    sub: Module,
    *,
    blackboxes: dict[str, Module] | None = None,
    spec: ClockSpec | None = None,
    pin_clocks: dict[str, str] | None = None,
    max_depth: int = 16,
    clock_pins_by_module: dict[str, frozenset[str]] | None = None,
    sink_bits: frozenset[Bit] | None = None,
    consumers: dict[Bit, list[tuple[str, str]]] | None = None,
) -> tuple[str, ...]:
    """Output ports of ``instance`` that drive a clock (issue #273).

    Blackboxing a module that *generates or forwards* a clock silently
    elides the clock network: the forwarded clock leaves an opaque
    boundary output, downstream consumers go domain-unknown (or vanish
    entirely when they are abstracted too), and CDC-008 loses the
    clock-distribution status of the cells feeding the boundary. So such
    a candidate is **declined**, exactly like a not-provably-single-clock
    one — see :func:`~rtl_buddy_cdc.hierarchy.compose_boundaries`.

    An output bit "drives a clock" when it reaches one of the three
    :func:`_clock_sink_bits` kinds, directly or through a bounded
    forward walk over ordinary combinational cells (buffers, inverters,
    gates, muxes). The walk stops at flops (a flop's ``D`` is a data
    sink, not clock forwarding — its ``CLK`` is already a sink kind) and
    at other blackbox boundaries (opaque; we cannot see through them,
    and their clock pins are a sink kind in their own right). The hop
    budget is ``max_depth``, the same clock-trace budget the rest of the
    boundary machinery threads from ``--clock-trace-depth``, so a deep
    clock-buffer chain resolves here exactly as far as the tracer would
    resolve it going the other way.

    Returns the offending output port names, sorted, or an empty tuple —
    the overwhelmingly common case of a module whose outputs carry only
    data, which must never be declined.

    Pure: reads ``parent`` / ``sub`` / ``spec`` only. The three cached
    keyword arguments let :func:`compose_boundaries` build the
    per-parent maps once and reuse them across every instance.
    """
    bb = blackboxes or {}
    if clock_pins_by_module is None:
        clock_pins_by_module = blackbox_clock_pins_by_module(
            parent, bb, spec=spec, pin_clocks=pin_clocks, max_depth=max_depth
        )
    if sink_bits is None:
        sink_bits = _clock_sink_bits(
            parent, clock_pins_by_module, spec=spec, pin_clocks=pin_clocks
        )
    if consumers is None:
        consumers = _bit_consumers(parent)
    if not sink_bits:
        return ()

    own_clock_pins = clock_pins_by_module.get(instance.type, frozenset())
    offenders: list[str] = []
    for port in sub.ports.values():
        if port.direction not in ("output", "inout"):
            continue
        if port.name in own_clock_pins:
            # An ``inout`` that the instance is *driven* on is a clock
            # pin, not a clock the subtree generates.
            continue
        bits = instance.connections.get(port.name)
        if not bits:
            continue
        if _reaches_clock_sink(
            parent, bits, sink_bits, consumers, bb, max_depth=max_depth
        ):
            offenders.append(port.name)
    return tuple(sorted(offenders))


def _reaches_clock_sink(
    parent: Module,
    start: tuple[Bit, ...],
    sink_bits: frozenset[Bit],
    consumers: dict[Bit, list[tuple[str, str]]],
    blackboxes: dict[str, Module],
    *,
    max_depth: int,
) -> bool:
    """Bounded forward walk: does any of ``start`` reach a clock sink?

    Breadth-first over combinational fanout; ``max_depth`` bounds the
    number of cell hops, mirroring the clock tracer's budget in the
    opposite direction.
    """
    frontier: list[Bit] = [b for b in start if isinstance(b, int)]
    seen: set[Bit] = set()
    for _hop in range(max_depth + 1):
        if not frontier:
            return False
        nxt: list[Bit] = []
        for bit in frontier:
            if bit in sink_bits:
                return True
            if bit in seen:
                continue
            seen.add(bit)
            for cell_name, pin in consumers.get(bit, ()):
                cell = parent.cells[cell_name]
                if is_ff_cell(cell.type) or cell.type in blackboxes:
                    # Flop D / opaque boundary: not clock forwarding.
                    # (Their clock pins are sink kinds, matched above.)
                    continue
                if pin in _COMB_OUTPUT_PINS:
                    continue
                for out_pin in _COMB_OUTPUT_PINS:
                    nxt.extend(
                        b
                        for b in cell.connections.get(out_pin, ())
                        if isinstance(b, int)
                    )
        frontier = nxt
    return False
