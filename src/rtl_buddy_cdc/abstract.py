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

from rtl_buddy_cdc.domain import _bit_drivers, trace_clock_root
from rtl_buddy_cdc.flops import flop_clk_pin, is_ff_cell
from rtl_buddy_cdc.netlist import Bit, BoundarySummary, Cell, Module, PortBoundary
from rtl_buddy_cdc.sdc import ClockSpec

# Yosys cell-port names a blackbox instance may use to carry a clock.
# A summarised subtree is described by its *data* boundary; the clock
# pin is consumed here (to learn the subtree's domain) and not emitted
# as a data port.
_CLOCK_PIN_NAMES: frozenset[str] = frozenset({"CLK", "clk", "C", "clock", "clk_i"})


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


def _instance_clock(
    parent: Module,
    instance: Cell,
    *,
    pin_clocks: dict[str, str] | None = None,
) -> str | None:
    """Resolve the clock domain feeding a blackbox instance's clock pin.

    The summarised subtree's domain is whatever the *parent* drives
    into the instance's clock pin. We trace that net back to its
    top-level clock root with the same machinery domain assignment
    uses, so a forwarded / generated clock resolves consistently.

    Returns ``None`` when the instance exposes no recognised clock pin
    (a data-only / combinational boundary, e.g. the P1 ``leaf``
    fixture) or the pin doesn't trace to a clock.
    """
    drivers = _bit_drivers(parent)
    from rtl_buddy_cdc.domain import _build_bit_to_clock

    bit_to_clock = _build_bit_to_clock(parent, pin_clocks)
    for pin in _CLOCK_PIN_NAMES:
        bits = instance.connections.get(pin)
        if bits:
            root = trace_clock_root(parent, bits[0], drivers, bit_to_clock=bit_to_clock)
            if root is not None:
                return root
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
    clk = _instance_clock(parent, instance, pin_clocks=pin_clocks)
    # The subtree's clock set: the single resolved instance clock (or
    # empty for a combinational boundary). A data-only boundary is
    # trivially single-clock; anything with a resolved clock must pass
    # the detector against the SDC.
    clocks: set[str] = {clk} if clk is not None else set()
    if not is_single_clock_subtree(clocks, spec):
        return None

    src_clock = clk  # may be None => conservative unconstrained source
    ports: dict[str, PortBoundary] = {}
    for port in sub.ports.values():
        if port.direction not in ("output", "inout"):
            continue
        ports[port.name] = PortBoundary(
            port=port.name,
            src_clock=src_clock,
            synchronised=False,
            width=len(port.bits),
        )
    return BoundarySummary(module=sub.name, ports=ports)


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
