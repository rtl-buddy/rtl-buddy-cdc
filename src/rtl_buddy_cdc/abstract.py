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

from rtl_buddy_cdc.domain import _bit_drivers, trace_clock_root
from rtl_buddy_cdc.flops import flop_clk_pin, is_ff_cell
from rtl_buddy_cdc.netlist import Bit, BoundarySummary, Cell, Module, PortBoundary
from rtl_buddy_cdc.sdc import UNCONSTRAINED_SENTINEL as _UNCONSTRAINED, ClockSpec

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
    """
    drivers = _bit_drivers(parent)
    from rtl_buddy_cdc.domain import _build_bit_to_clock

    bit_to_clock = _build_bit_to_clock(parent, pin_clocks)
    roots: set[str] = set()
    clock_pins: set[str] = set()

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
            root = trace_clock_root(parent, bits[0], drivers, bit_to_clock=bit_to_clock)
            clock_pins.add(pin)
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
                bit_to_clock=bit_to_clock,
                allow_divider=False,
            )
            if root is not None and _is_known_clock(root, spec):
                clock_pins.add(pin)
                roots.add(root)
    return _InstanceClocks(roots=frozenset(roots), clock_pins=frozenset(clock_pins))


def _instance_clock(
    parent: Module,
    instance: Cell,
    *,
    spec: ClockSpec | None = None,
    pin_clocks: dict[str, str] | None = None,
) -> str | None:
    """Resolve the *single* clock root feeding a blackbox instance.

    Back-compat single-root view used by the diagnostic helper and the
    name-allow-list fixtures. Returns the sole root when the instance
    resolves to exactly one, else ``None`` (no clock pin, an unresolved
    pin, or — deliberately — a multi-clock instance, which the
    summariser declines via :func:`_instance_clocks`).
    """
    ic = _instance_clocks(parent, instance, spec=spec, pin_clocks=pin_clocks)
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
) -> BoundarySummary | None:
    """Summarise a blackboxed single-clock subtree to its port boundary.

    ``parent`` is the (flattened) top module, ``instance`` the ordinary
    cell whose ``type`` is the blackbox module name, and ``sub`` the
    blackbox sibling :class:`Module` (zero cells, real name). ``spec``
    is the parsed SDC used to confirm the subtree really is
    single-clock.

    Returns a :class:`BoundarySummary` keyed by output/inout port name,
    or ``None`` when the subtree is *not* provably single-clock (the
    caller then leaves the instance to be analysed by the normal flat
    walk — never abstracted away).

    Each output/inout port is summarised at ``src_clock`` = the
    subtree's single domain with ``synchronised=False``: the subtree
    retimes internally (single domain, no internal crossing) but we do
    not assume it synchronises data *leaving* it, so a downstream sink
    in a different domain is still flagged as a crossing the parent
    must check. ``src_clock=None`` (clock pin that didn't resolve) is a
    legitimate conservative unconstrained source.
    """
    ic = _instance_clocks(parent, instance, sub, spec=spec, pin_clocks=pin_clocks)
    # The subtree's clock set: ALL distinct roots driving the instance's
    # clock pins (FIX 1). A dual-clock IP (e.g. ``wr_clk`` / ``rd_clk``)
    # therefore presents >=2 roots and is declined below — its internal
    # clkA->clkB crossing is no longer silently abstracted away. The
    # empty set is a genuinely combinational boundary.
    if not is_single_clock_subtree(set(ic.roots), spec):
        return None
    # The single capture clock is the sole root, or None when the set is
    # empty (combinational) — never a "first one wins" pick.
    clk = next(iter(ic.roots)) if ic.roots else None

    # Result-preserving by construction (P3/#257): the boundary summary
    # seeds output-side virtual *sources* (``ports``) AND input-side
    # virtual *sinks* (``input_ports``, captured in the boundary's own
    # ``clock`` domain). A foreign-domain signal the parent drives into
    # a data input no longer vanishes — ``find_crossings`` re-creates the
    # crossing the flattened subtree would have reported at its first
    # internal flop. So we abstract the single-clock subtree even when a
    # foreign-domain input enters it; the P2 over-conservative refusal
    # (``_instance_inputs_same_domain``) is retired.
    src_clock = clk  # may be None => conservative unconstrained source
    ports: dict[str, PortBoundary] = {}
    input_ports: dict[str, PortBoundary] = {}
    for port in sub.ports.values():
        if port.direction in ("output", "inout"):
            ports[port.name] = PortBoundary(
                port=port.name,
                src_clock=src_clock,
                synchronised=False,
                width=len(port.bits),
            )
        if port.direction in ("input", "inout") and port.name not in ic.clock_pins:
            # The sink domain for data entering the boundary is the
            # subtree's own clock (what its first internal flop samples
            # on). ``src_clock`` here names that capture domain. Clock
            # pins are excluded by the *traced* determination (FIX 1),
            # not just the name allow-list — so a single-clock block
            # whose clock pin is e.g. ``wr_clk`` does not get a
            # clock-as-data sink, while a genuine data input named
            # ``clk_foo`` that does not trace to a clock still seeds.
            # They carry distribution into the subtree, not data, and
            # must never become a virtual sink (that would re-introduce
            # the CDC-008 clock-as-data shape).
            input_ports[port.name] = PortBoundary(
                port=port.name,
                src_clock=src_clock,
                synchronised=False,
                width=len(port.bits),
            )
    return BoundarySummary(
        module=sub.name,
        ports=ports,
        clock=clk,
        input_ports=input_ports,
    )


def boundary_instance_clocks(parent: Module) -> set[str]:
    """Diagnostic: the clock-pin domains every blackbox instance carries.

    Not used in the main path; handy for callers / tests that want to
    see what domains the parent feeds into its boundary cells.
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
            root = trace_clock_root(parent, bits[0], drivers)
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
) -> frozenset[str]:
    """The set of port names determined to be clock pins on ``instance``.

    Public accessor for FIX 4 (CDC-008): a clock net wired into a
    blackbox's CLOCK pin is distribution and must not fire clock-as-data,
    but a clock wired into a genuine DATA input still must. Uses the same
    traced determination as the summariser, so the two never disagree.
    """
    return _instance_clocks(
        parent, instance, sub, spec=spec, pin_clocks=pin_clocks
    ).clock_pins
