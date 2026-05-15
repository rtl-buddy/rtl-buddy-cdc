"""CDC rule checks.

Each rule is a small function ``check_<rule>(...) -> list[Violation]``
operating on the netlist + the list of crossings (already filtered to
asynchronous pairs by the caller). Adding a rule is intentionally a
self-contained edit: register the function in :data:`RULES` and you're
done.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

from rtl_buddy_cdc.domain import Crossing, assign_domains
from rtl_buddy_cdc.flops import FF_CELL_TYPES, Flop, find_flops
from rtl_buddy_cdc.netlist import Bit, Module
from rtl_buddy_cdc.sdc import ClockSpec


@dataclass(frozen=True)
class Violation:
    rule_id: str
    severity: str  # "error" | "warning" | "info"
    message: str
    # Most rules attach the data crossing they fired on; rules that
    # operate on a different shape (e.g. reset crossings) leave it None.
    crossing: Crossing | None = None
    # The single cell most directly responsible — used by structured
    # reporters (JSON/SARIF) to surface a source location via the
    # cell's ``attributes["src"]`` field. Falls back to the crossing's
    # dst flop if not set.
    cell_name: str | None = None


# ``ctx`` is keyword-only on every rule; using ``Callable[..., ...]`` because
# typing.Callable can't express a kw-only argument without a Protocol.
RuleFn = Callable[..., list[Violation]]


# --- shared rule context ----------------------------------------------------


@dataclass(frozen=True)
class _RuleContext:
    """Structural views every rule wants. Computed once in :func:`run_all`
    and threaded into each ``check_cdc_NNN`` via the keyword-only
    ``ctx=`` argument; rules called standalone (a test invoking
    ``check_cdc_002`` directly) lazy-build their own context.

    The cache exists because the rule pack used to recompute these
    views per-rule. ``assign_domains`` ran 7×, ``find_flops`` ran
    transitively many more times, and ``_sync_chain_depth``'s inner
    loop called ``find_flops`` every chain-extension step (O(N) per
    step). On a 90-flop block the overhead is invisible; the moment
    rule fan-out crosses a few hundred flops it dominates.
    """

    module: Module
    clock_spec: ClockSpec | None
    flops: tuple[Flop, ...]
    domains: dict[str, str | None]
    bit_drivers: dict[Bit, tuple[str, str, int]]
    # Forward index: maps each bit to every (cell_name, input_port_name,
    # index) consuming it. Symmetric to ``bit_drivers``. Used by
    # :func:`_forward_reachable_flops` for downstream-cone walks (e.g.
    # the CDC-005 reconvergence filter). Built once per ``run_all``.
    bit_consumers: dict[Bit, tuple[tuple[str, str, int], ...]]
    reader_counts: dict[Bit, int]
    user_syncs: frozenset[str]
    user_grays: frozenset[str]
    # Reverse index used by ``_sync_chain_depth``: for every flop whose
    # ``D`` is exactly one bit wide, map that bit to the flop. The
    # chain walker previously did ``for f in find_flops(module):`` per
    # step; this turns each step into an O(1) lookup.
    d_bit_to_single_bit_flop: dict[Bit, Flop]


def _build_context(module: Module, clock_spec: ClockSpec | None) -> _RuleContext:
    """Compute the per-``run_all`` cached views in one pass.

    Pure function of ``(module, clock_spec)``; safe to call multiple
    times but pointless — the whole point is amortising the work
    across rules.
    """
    flops = tuple(find_flops(module))
    flop_domains = assign_domains(module)
    domains = {fd.flop.cell.name: fd.clock for fd in flop_domains}

    bit_drivers: dict[Bit, tuple[str, str, int]] = {}
    for cell in module.cells.values():
        for port_name in ("Y", "Q"):
            for idx, b in enumerate(cell.connections.get(port_name, ())):
                if isinstance(b, int):
                    bit_drivers[b] = (cell.name, port_name, idx)

    # Forward index: same shape as ``bit_drivers`` but for *inputs*.
    # CLK is excluded (clock pins are walked separately by the clock-
    # network tracer); other pin names — D, A, B, S, SEL, EN, ARST, … —
    # all count as data consumers.
    consumers_builder: dict[Bit, list[tuple[str, str, int]]] = {}
    for cell in module.cells.values():
        for port_name, bits in cell.connections.items():
            if port_name in _OUTPUT_PINS or port_name == "CLK":
                continue
            for idx, b in enumerate(bits):
                if isinstance(b, int):
                    consumers_builder.setdefault(b, []).append(
                        (cell.name, port_name, idx)
                    )
    bit_consumers: dict[Bit, tuple[tuple[str, str, int], ...]] = {
        bit: tuple(entries) for bit, entries in consumers_builder.items()
    }

    reader_counts: dict[Bit, int] = {}
    for cell in module.cells.values():
        for port_name, bits in cell.connections.items():
            if port_name in {"Y", "Q", "CLK"}:
                continue
            for b in bits:
                if isinstance(b, int):
                    reader_counts[b] = reader_counts.get(b, 0) + 1

    d_bit_to_single_bit_flop: dict[Bit, Flop] = {}
    for f in flops:
        if len(f.d) == 1 and isinstance(f.d[0], int):
            d_bit_to_single_bit_flop[f.d[0]] = f

    return _RuleContext(
        module=module,
        clock_spec=clock_spec,
        flops=flops,
        domains=domains,
        bit_drivers=bit_drivers,
        bit_consumers=bit_consumers,
        reader_counts=reader_counts,
        user_syncs=frozenset(user_sync_flop_names(module)),
        user_grays=frozenset(user_gray_flop_names(module)),
        d_bit_to_single_bit_flop=d_bit_to_single_bit_flop,
    )


# --- helpers ----------------------------------------------------------------


# SV attributes that mark a flop as a user-vetted synchronizer first
# stage. Attach to the wire/reg declaration the flop drives, e.g.::
#
#     (* cdc_sync *) logic dst_q;
#
# Yosys preserves the attribute on the *netname* (not the cell), so
# we map back from a tagged netname's bits to the flop whose Q
# produces them. Multiple aliases are accepted so projects using
# Spyglass-style or Vivado-style tags don't have to rename.
USER_SYNC_ATTRS: frozenset[str] = frozenset({"cdc_sync", "synchronizer", "async_reg"})


def user_sync_flop_names(module: Module) -> set[str]:
    """Return cell names of flops whose Q is named via a wire annotated
    with one of the :data:`USER_SYNC_ATTRS`. These are treated by
    CDC-001 / CDC-002 / CDC-003 / CDC-006 as "trust-me, this is a
    correctly-engineered synchronizer" — the structural rule passes
    skip them."""
    sync_bits: set[Bit] = set()
    for nn in module.netnames.values():
        if USER_SYNC_ATTRS & set(nn.attributes):
            for b in nn.bits:
                if isinstance(b, int):
                    sync_bits.add(b)
    if not sync_bits:
        return set()
    out: set[str] = set()
    for f in find_flops(module):
        if any(isinstance(b, int) and b in sync_bits for b in f.q):
            out.add(f.cell.name)
    return out


# Companion to USER_SYNC_ATTRS: an explicit gray-coded promise. Attach
# to the source-side bus wire (the gray counter's output) to suppress
# CDC-004 false positives when the structural detector can't see the
# multi-bit sync chain (e.g. across a module boundary that hasn't been
# flattened, or when the chain is implemented in a non-canonical way).
USER_GRAY_ATTRS: frozenset[str] = frozenset({"cdc_gray", "gray_code"})


def user_gray_flop_names(module: Module) -> set[str]:
    """Cell names of source flops whose Q is named via a wire annotated
    with ``(* cdc_gray *)``. CDC-004 treats those bus crossings as safe
    by fiat (the user is asserting only one bit changes per cycle)."""
    gray_bits: set[Bit] = set()
    for nn in module.netnames.values():
        if USER_GRAY_ATTRS & set(nn.attributes):
            for b in nn.bits:
                if isinstance(b, int):
                    gray_bits.add(b)
    if not gray_bits:
        return set()
    out: set[str] = set()
    for f in find_flops(module):
        if any(isinstance(b, int) and b in gray_bits for b in f.q):
            out.add(f.cell.name)
    return out


# Pin names whose connections are *outputs* of a cell (so we never walk
# backward through them). All other pins are treated as inputs for the
# purpose of fanin walks.
_OUTPUT_PINS: frozenset[str] = frozenset({"Y", "Q"})


def _backward_fanin(
    module: Module,
    start_bits: tuple[Bit, ...],
    drivers: dict[Bit, tuple[str, str, int]],
    max_depth: int = 12,
) -> tuple[set[str], set[str]]:
    """Reverse-BFS through combinational cells.

    Returns ``(flop_cell_names, input_port_names)`` — every flop ``Q``
    and every top-level *input* port reached. Useful when a rule needs
    to distinguish "the upstream is a registered flop" from "the
    upstream is an external pin".
    """
    flops: set[str] = set()
    ports: set[str] = set()
    seen: set[Bit] = set()
    frontier: list[tuple[Bit, int]] = [(b, 0) for b in start_bits if isinstance(b, int)]
    while frontier:
        nxt: list[tuple[Bit, int]] = []
        for bit, depth in frontier:
            if bit in seen:
                continue
            seen.add(bit)
            drv = drivers.get(bit)
            if drv is None:
                port = module.port_of_bit(bit)
                if port is not None and port.direction == "input":
                    ports.add(port.name)
                continue
            cell_name, port_name, _idx = drv
            cell = module.cells[cell_name]
            if port_name == "Q":
                flops.add(cell_name)
                continue
            if depth >= max_depth:
                continue
            for in_port, in_bits in cell.connections.items():
                if in_port in _OUTPUT_PINS:
                    continue
                for b in in_bits:
                    if isinstance(b, int):
                        nxt.append((b, depth + 1))
        frontier = nxt
    return flops, ports


def _forward_reachable_flops(
    module: Module,
    start_bits: tuple[Bit, ...],
    consumers: dict[Bit, tuple[tuple[str, str, int], ...]],
    max_depth: int = 12,
) -> set[str]:
    """Forward-BFS through combinational cells, mirror of
    :func:`_backward_fanin`.

    Starts at ``start_bits`` (typically a flop's ``Q`` bits), walks
    each consumer cell forward through its output bits, and returns
    the set of flop cell names whose ``D`` pin was reached. Stops at
    every flop boundary — flops aren't crossed, they're the answer.
    Stops at ``max_depth`` hops to bound the walk on huge designs;
    the ``seen`` set keeps the walk linear regardless.

    See also :func:`_forward_reachable_cells` for a broader variant
    that also records comb-cell consumers on the path — used by
    CDC-005's reconvergence filter, which must catch unregistered
    recombination too (e.g. a top-level output port driven directly
    by a comb cell combining two synchronized values).
    """
    reached: set[str] = set()
    seen: set[Bit] = set()
    frontier: list[tuple[Bit, int]] = [(b, 0) for b in start_bits if isinstance(b, int)]
    while frontier:
        nxt: list[tuple[Bit, int]] = []
        for bit, depth in frontier:
            if bit in seen:
                continue
            seen.add(bit)
            for cell_name, port_name, _idx in consumers.get(bit, ()):
                cell = module.cells[cell_name]
                if cell.type in FF_CELL_TYPES:
                    # Reached a flop — recorded if it's the D input, but
                    # never crossed regardless of which pin we hit.
                    if port_name == "D":
                        reached.add(cell_name)
                    continue
                if depth >= max_depth:
                    continue
                # Combinational cell — enqueue every output bit.
                for out_port in _OUTPUT_PINS:
                    for b in cell.connections.get(out_port, ()):
                        if isinstance(b, int):
                            nxt.append((b, depth + 1))
        frontier = nxt
    return reached


def _forward_reachable_cells(
    module: Module,
    start_bits: tuple[Bit, ...],
    consumers: dict[Bit, tuple[tuple[str, str, int], ...]],
    max_depth: int = 12,
) -> set[str]:
    """Like :func:`_forward_reachable_flops` but records EVERY cell
    whose input is touched on the way — flops (via their D pin) and
    intermediate combinational cells alike.

    Recommended over the flop-only variant when the reconvergence
    point isn't necessarily a flop: e.g. CDC-005 must fire on a
    fixture whose two synchronized values recombine into a
    combinational reduction driving an unregistered output port.
    Two chains "reconverge" the moment any cell observes both
    values — registered or not.
    """
    reached: set[str] = set()
    seen: set[Bit] = set()
    frontier: list[tuple[Bit, int]] = [(b, 0) for b in start_bits if isinstance(b, int)]
    while frontier:
        nxt: list[tuple[Bit, int]] = []
        for bit, depth in frontier:
            if bit in seen:
                continue
            seen.add(bit)
            for cell_name, _port_name, _idx in consumers.get(bit, ()):
                cell = module.cells[cell_name]
                reached.add(cell_name)
                if cell.type in FF_CELL_TYPES:
                    # Flop boundary — recorded above, but don't cross.
                    continue
                if depth >= max_depth:
                    continue
                for out_port in _OUTPUT_PINS:
                    for b in cell.connections.get(out_port, ()):
                        if isinstance(b, int):
                            nxt.append((b, depth + 1))
        frontier = nxt
    return reached


def _backward_flop_fanin(
    module: Module,
    start_bits: tuple[Bit, ...],
    drivers: dict[Bit, tuple[str, str, int]],
    max_depth: int = 12,
) -> set[str]:
    """Backward BFS through combinational cells; return the set of flop
    cell names whose ``Q`` is reached.

    Traversal stops at flop ``Q`` outputs, top-level ports, or constants.
    The depth bound exists only to keep pathological fanins bounded; for
    well-structured CDC patterns the answer converges quickly.
    """
    flops: set[str] = set()
    seen: set[Bit] = set()
    frontier: list[tuple[Bit, int]] = [(b, 0) for b in start_bits if isinstance(b, int)]
    while frontier:
        nxt: list[tuple[Bit, int]] = []
        for bit, depth in frontier:
            if bit in seen:
                continue
            seen.add(bit)
            drv = drivers.get(bit)
            if drv is None:
                # bit comes from a top-level port or is a constant; the
                # caller will treat it as "untracked source" and decide
                continue
            cell_name, port, _idx = drv
            cell = module.cells[cell_name]
            if port == "Q":
                flops.add(cell_name)
                continue
            # combinational cell — walk back through every input pin
            if depth >= max_depth:
                continue
            for in_port, in_bits in cell.connections.items():
                if in_port in _OUTPUT_PINS:
                    continue
                for b in in_bits:
                    if isinstance(b, int):
                        nxt.append((b, depth + 1))
        frontier = nxt
    return flops


def _bit_reader_count(module: Module) -> dict[Bit, int]:
    """Count every (cell, port-bit) site that reads each net bit.

    Both flop D and combinational inputs count; flop CLK is excluded
    because clock connections aren't part of the data fanout. Two reads
    of the same bit by the same cell on different ports count
    independently — both are real loads.
    """
    counts: dict[Bit, int] = {}
    for cell in module.cells.values():
        # Outputs of $-prefixed primitives use the ``Y`` port; flop Q is
        # also an output. Both are intentionally ignored: we want input
        # readers, not drivers.
        for port_name, bits in cell.connections.items():
            if port_name in {"Y", "Q", "CLK"}:
                continue
            for b in bits:
                if isinstance(b, int):
                    counts[b] = counts.get(b, 0) + 1
    return counts


def _sync_chain_depth(
    module: Module,
    head: Flop,
    head_clock: str,
    domains: dict[str, str | None],
    reader_counts: dict[Bit, int] | None = None,
    *,
    d_bit_to_single_bit_flop: dict[Bit, Flop] | None = None,
) -> int:
    """Length of the synchronizer chain that starts at ``head``.

    A chain extends to the next flop iff:

    - the head's Q bit has *exactly one* reader anywhere in the
      module (any extra reader — combinational or otherwise — means
      the synchronized value is already in use; the chain ends),
    - that reader is a 1-bit flop on its D pin in the same clock
      domain.

    Returns the count of dst-domain flops *including* ``head``. Callers
    typically check ``depth >= 2``.

    When ``d_bit_to_single_bit_flop`` is supplied (from
    :class:`_RuleContext`), the chain extension step is an O(1) dict
    lookup instead of an O(N) ``find_flops`` scan. The argument is
    optional so callers that haven't built a context still work
    (the lazy-build path in each rule when ``ctx=None``).
    """
    if reader_counts is None:
        reader_counts = _bit_reader_count(module)
    if d_bit_to_single_bit_flop is None:
        d_bit_to_single_bit_flop = {
            f.d[0]: f
            for f in find_flops(module)
            if len(f.d) == 1 and isinstance(f.d[0], int)
        }

    depth = 1
    current = head
    visited = {current.cell.name}
    while True:
        # 1-bit chains only at this stage — multi-bit syncs are bus
        # crossings, handled by a separate rule.
        if len(current.q) != 1 or len(current.q) != len(current.d):
            break
        next_q = current.q[0]
        if not isinstance(next_q, int):
            break
        # If the bit is consumed by anything other than a single follow-
        # on flop D pin, the chain ends here (the value is "in use").
        if reader_counts.get(next_q, 0) != 1:
            break
        nxt = d_bit_to_single_bit_flop.get(next_q)
        if nxt is None or nxt.cell.name in visited:
            break
        if domains.get(nxt.cell.name) != head_clock:
            break
        depth += 1
        visited.add(nxt.cell.name)
        current = nxt
    return depth


def _sync_chain_flops(
    module: Module,
    head: Flop,
    head_clock: str,
    domains: dict[str, str | None],
    reader_counts: dict[Bit, int],
    d_bit_to_single_bit_flop: dict[Bit, Flop],
) -> tuple[Flop, ...]:
    """Same walk as :func:`_sync_chain_depth` but returns the ordered
    list of chain flops (starting at ``head``).

    Phase-2 of CDC-005 needs the terminal flop's Q to start the
    forward-cone walk for the reconvergence filter; this helper is
    the shared truth between "how long is the chain" and "what's the
    chain's tail".
    """
    out: list[Flop] = [head]
    current = head
    visited = {current.cell.name}
    while True:
        if len(current.q) != 1 or len(current.q) != len(current.d):
            break
        next_q = current.q[0]
        if not isinstance(next_q, int):
            break
        if reader_counts.get(next_q, 0) != 1:
            break
        nxt = d_bit_to_single_bit_flop.get(next_q)
        if nxt is None or nxt.cell.name in visited:
            break
        if domains.get(nxt.cell.name) != head_clock:
            break
        out.append(nxt)
        visited.add(nxt.cell.name)
        current = nxt
    return tuple(out)


# --- rules ------------------------------------------------------------------


def check_cdc_001(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-001 — Unsynchronized control crossing.

    A single-bit register-to-register CDC where the destination flop has
    no follow-on synchronizer flop in the same domain. This is the
    classic "metastability bug": the source-domain signal lands on a
    single flop and is then used directly, with no second stage to
    filter metastable values.

    Multi-bit (bus) crossings deliberately bypass this check — their
    correctness pattern is gating or gray-coding, handled by CDC-004.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []

    for c in crossings:
        if c.width != 1:
            continue
        if c.dst_flop.cell.name in ctx.user_syncs:
            continue  # user vouches for the synchronizer shape
        depth = _sync_chain_depth(
            module,
            c.dst_flop,
            c.dst_clock,
            ctx.domains,
            ctx.reader_counts,
            d_bit_to_single_bit_flop=ctx.d_bit_to_single_bit_flop,
        )
        if depth < 2:
            src_desc = (
                f"flop {c.src_flop.name}" if c.src_flop is not None else c.src_name
            )
            violations.append(
                Violation(
                    rule_id="CDC-001",
                    severity="error",
                    message=(
                        f"unsynchronized control crossing "
                        f"{c.src_clock} → {c.dst_clock} "
                        f"(src: {src_desc}): destination flop "
                        f"{c.dst_flop.name} has no second-stage "
                        f"synchronizer (chain depth = {depth})"
                    ),
                    crossing=c,
                    cell_name=c.dst_flop.cell.name,
                )
            )
    return violations


def check_cdc_002(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
    required_depth: int = 2,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-002 — Insufficient synchronizer depth.

    Fires when a control crossing has a synchronizer chain present
    (depth >= 2, so CDC-001 is satisfied) but shorter than the
    site-required minimum. The default ``required_depth`` of 2 keeps
    this rule silent unless a project explicitly raises the bar (e.g.
    3 stages for high-speed/low-MTBF designs).
    """
    if required_depth <= 2:
        return []
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []

    for c in crossings:
        if c.width != 1:
            continue
        if c.dst_flop.cell.name in ctx.user_syncs:
            continue
        depth = _sync_chain_depth(
            module,
            c.dst_flop,
            c.dst_clock,
            ctx.domains,
            ctx.reader_counts,
            d_bit_to_single_bit_flop=ctx.d_bit_to_single_bit_flop,
        )
        if 2 <= depth < required_depth:
            violations.append(
                Violation(
                    rule_id="CDC-002",
                    severity="warning",
                    message=(
                        f"insufficient synchronizer depth on "
                        f"{c.src_clock} → {c.dst_clock} crossing: "
                        f"found {depth} flop(s), required >= {required_depth} "
                        f"(dst flop: {c.dst_flop.name})"
                    ),
                    crossing=c,
                    cell_name=c.dst_flop.cell.name,
                )
            )
    return violations


def check_cdc_003(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-003 — Combinational logic on the way to a synchronizer.

    Fires when a single-bit crossing reaches a destination synchronizer
    (chain depth >= 2, so CDC-001/-002 don't already cover it) but the
    path from the source flop to the synchronizer's first stage passes
    through one or more combinational cells (``min_hops >= 1``).

    The classic failure mode: two source-domain flops are combined by a
    gate (AND/OR/MUX) and the gate output is sampled by the
    synchronizer. The two source flops can transition on different src
    clock cycles, producing a glitch on the gate output that a 2FF
    synchronizer cannot reliably filter — the destination may sample
    a transient value that never existed as a real source state.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []

    for c in crossings:
        if c.width != 1:
            continue
        if c.min_hops < 1:
            continue
        if c.is_port_sourced:
            # CDC-006 already covers comb logic from a top-level port
            # to a synchronizer; don't double-fire.
            continue
        if c.dst_flop.cell.name in ctx.user_syncs:
            continue
        depth = _sync_chain_depth(
            module,
            c.dst_flop,
            c.dst_clock,
            ctx.domains,
            ctx.reader_counts,
            d_bit_to_single_bit_flop=ctx.d_bit_to_single_bit_flop,
        )
        if depth < 2:
            # CDC-001 covers this crossing already; don't double-fire.
            continue
        violations.append(
            Violation(
                rule_id="CDC-003",
                severity="error",
                message=(
                    f"combinational logic between source flop and "
                    f"synchronizer on {c.src_clock} → {c.dst_clock} "
                    f"crossing: {c.min_hops} cell(s) on path "
                    f"(src: {c.src_name}, "
                    f"sync first stage: {c.dst_flop.name})"
                ),
                crossing=c,
                cell_name=c.dst_flop.cell.name,
            )
        )
    return violations


def _is_gated_bus_crossing(
    module: Module,
    crossing: Crossing,
    domains: dict[str, str | None],
    drivers: dict[Bit, tuple[str, str, int]],
) -> bool:
    """Heuristic: the bus crossing is properly gated by a dst-domain
    handshake (so CDC-004 should not fire).

    The pattern we accept: the cell that directly drives the
    destination flop's ``D`` pin is a single ``$mux``, and the mux's
    ``S`` (select) input is driven by combinational/sequential logic
    whose entire fanin in the netlist sits in the destination clock
    domain. That matches the golden ``ip_cdc_handshake`` shape and
    the standard "load on synced req-edge" pattern in textbooks.

    A more rigorous check would also verify that the mux's hold-input
    is the destination flop's own ``Q`` (proper feedback hold), but we
    leave that as a follow-up — the current shape catches the bus
    crossing patterns we care about without false-positives on the
    golden fixture.
    """
    dst_clock = crossing.dst_clock
    # All D bits should be driven by the same cell for the gating
    # interpretation to apply. If the bus is split across multiple
    # drivers, give up and treat as ungated (conservative).
    driver_cells: set[str] = set()
    for d_bit in crossing.dst_flop.d:
        if not isinstance(d_bit, int):
            continue
        drv = drivers.get(d_bit)
        if drv is None:
            return False
        driver_cells.add(drv[0])
    if len(driver_cells) != 1:
        return False
    drv_cell = module.cells[next(iter(driver_cells))]
    if drv_cell.type != "$mux":
        return False
    s_bits = drv_cell.connections.get("S", ())
    if not s_bits:
        return False
    s_fanin_flops = _backward_flop_fanin(module, s_bits, drivers)
    if not s_fanin_flops:
        return False
    # Every flop reached in the select-fanin must be in the dst clock
    # domain. A single src-domain flop in there means the "gate" is
    # itself a cross-domain signal — not a valid handshake.
    return all(domains.get(name) == dst_clock for name in s_fanin_flops)


def _is_gray_encoded_source(
    module: Module,
    src_flop: Flop,
    drivers: dict[Bit, tuple[str, str, int]],
    max_back_hops: int = 8,
) -> bool:
    """Detect the canonical gray-encoding pattern in the source flop's
    fanin.

    A gray counter computes ``g = b ^ (b >> 1)`` where ``b`` is the
    binary value. ``b >> 1`` is a logical right-shift of one
    position, so the shifted operand's bit ``i`` is the unshifted
    operand's bit ``i+1``. After Yosys flattening the right-shift
    becomes pure wire-routing — the ``$xor`` cell sees::

        A = (b[0], b[1], ..., b[N-1])
        B = (b[1], b[2], ..., b[N-1], '0')   # MSB padded with constant

    We walk the source flop's D fanin backward through combinational
    cells (bounded by ``max_back_hops``) looking for any ``$xor``
    whose A/B pair satisfies ``A[i+1] == B[i]`` for ``i < N-1`` and
    ``B[N-1]`` is a constant. That signature is essentially unique to
    gray encoding and is what async-FIFO pointers produce.
    """
    seen: set[Bit] = set()
    frontier: list[tuple[Bit, int]] = [(b, 0) for b in src_flop.d if isinstance(b, int)]
    while frontier:
        nxt: list[tuple[Bit, int]] = []
        for bit, depth in frontier:
            if bit in seen or depth > max_back_hops:
                continue
            seen.add(bit)
            drv = drivers.get(bit)
            if drv is None:
                continue
            cell_name, port_name, _idx = drv
            if port_name == "Q":
                # Reached a flop — that's another register's output.
                # Don't traverse into a different domain's logic.
                continue
            cell = module.cells[cell_name]
            if cell.type == "$xor":
                a_bits = cell.connections.get("A", ())
                b_bits = cell.connections.get("B", ())
                n = len(a_bits)
                if (
                    n >= 2
                    and len(b_bits) == n
                    and all(
                        isinstance(a_bits[i + 1], int)
                        and isinstance(b_bits[i], int)
                        and a_bits[i + 1] == b_bits[i]
                        for i in range(n - 1)
                    )
                    and isinstance(b_bits[n - 1], str)
                ):
                    return True
            for in_port, in_bits in cell.connections.items():
                if in_port in _OUTPUT_PINS:
                    continue
                for b in in_bits:
                    if isinstance(b, int):
                        nxt.append((b, depth + 1))
        frontier = nxt
    return False


def _is_multibit_sync_first_stage(
    module: Module,
    dst_flop: Flop,
    dst_clock: str,
    domains: dict[str, str | None],
) -> bool:
    """Test whether ``dst_flop`` is the first stage of a multi-bit
    synchronizer chain.

    Canonical shape: a width-N flop whose ``Q`` drives the ``D`` of
    another width-N flop in the same clock domain, lane-for-lane (so
    ``Q[i] == nextflop.D[i]`` for every ``i``). This is exactly what
    ``ip_cdc_sync`` produces for ``WIDTH > 1`` after Yosys flatten —
    one multi-bit cell per stage, lane-aligned. Async-FIFO pointer
    crossings use this exact pattern with gray-coded data, which is
    safe because at most one bit changes per source cycle.

    Lane-wise alignment is what makes this *gray-friendly*: a generic
    multi-bit sync chain doesn't promise gray-coding in the source,
    but in practice the only well-known reason to wire one up is for
    a gray-coded crossing. We therefore accept it as the gray-code
    structural case; users who want stricter behaviour can keep
    asserting via ``set_clock_groups`` and the rule pass.
    """
    if len(dst_flop.q) < 2:
        return False
    width = len(dst_flop.q)
    if len(dst_flop.d) != width:
        return False
    q_bits = tuple(dst_flop.q)
    if not all(isinstance(b, int) for b in q_bits):
        return False
    for f in find_flops(module):
        if f.cell.name == dst_flop.cell.name:
            continue
        if domains.get(f.cell.name) != dst_clock:
            continue
        if len(f.d) != width:
            continue
        if tuple(f.d) == q_bits:
            return True
    return False


def check_cdc_004(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-004 — Multi-bit bus crossing without gating or gray-coding.

    A multi-bit data path that crosses clock domains needs an extra
    coherence mechanism on top of per-bit synchronization, because
    individual bits can settle on different destination cycles.
    Three patterns are accepted:

    - **Handshake / load-enable gating** — destination flops only
      sample the bus when a synchronized control signal allows it
      (the golden ``ip_cdc_handshake`` shape).
    - **Gray-coded crossing into a multi-bit sync chain** — the
      destination is itself a multi-bit synchronizer (e.g. async-FIFO
      pointer sync), so each lane is independently filtered for
      metastability. This is correct iff the source actually toggles
      one bit at a time; gray-coded counters guarantee that.
    - **User-asserted gray-coding** — the source wire is annotated
      ``(* cdc_gray *)`` (or ``(* gray_code *)``), telling the
      analyzer to trust the gray-counter promise even when the
      structural detector can't see the sync chain.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []

    for c in crossings:
        if c.width <= 1:
            continue
        if c.src_flop is None:
            # CDC-004 reasons about gray encoding at a register source;
            # port-sourced crossings can't be gray-encoded by the same
            # mechanism. Bus crossings from typed ports are vanishingly
            # rare in practice; defer to user judgment.
            continue
        if c.src_flop.cell.name in ctx.user_grays:
            # Explicit user assertion of gray-coding.
            continue
        if _is_multibit_sync_first_stage(
            module, c.dst_flop, c.dst_clock, ctx.domains
        ) and _is_gray_encoded_source(module, c.src_flop, ctx.bit_drivers):
            # Structural gray-coded crossing: source has the canonical
            # gray-encode XOR pattern AND the destination is a
            # multi-bit synchronizer chain. This is the async-FIFO
            # pointer shape and is correct by construction.
            continue
        if _is_gated_bus_crossing(module, c, ctx.domains, ctx.bit_drivers):
            continue
        violations.append(
            Violation(
                rule_id="CDC-004",
                severity="error",
                message=(
                    f"unprotected bus crossing on {c.src_clock} → "
                    f"{c.dst_clock}: {c.width}-bit path with no "
                    f"recognized gating or gray-coding "
                    f"(src flop: {c.src_flop.name}, "
                    f"dst flop: {c.dst_flop.name})"
                ),
                crossing=c,
                cell_name=c.dst_flop.cell.name,
            )
        )
    return violations


def check_cdc_005(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-005 — Reconvergent synchronizers.

    Fires when a single source-domain flop fans out to two or more
    *independent* synchronizer chains in the same destination domain
    AND the synchronized outputs reconverge downstream. Each chain
    individually filters metastability, but their resolution times
    can differ by a destination cycle, so any downstream logic that
    recombines the synchronized outputs may observe a value pair
    that was never simultaneously true at the source.

    Phase 2 (issue #33) filters out groups whose forward cones don't
    intersect — having redundant synchronizers without a
    recombination point isn't itself a bug, just a code smell, and
    pure-structural reporting on it was the rule's biggest
    false-positive source.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []

    # Group single-bit, properly synchronized crossings by source flop
    # + destination domain. We require depth>=2 so we don't double-fire
    # against CDC-001 (the depth==1 cases will be reported there).
    grouped: dict[tuple[str, str], list[Crossing]] = defaultdict(list)
    for c in crossings:
        if c.width != 1:
            continue
        if c.src_flop is None:
            # Reconvergence is defined by a single source flop fanning
            # out to multiple sync chains; port sources don't have the
            # metastability-resolution behaviour the rule is testing.
            continue
        depth = _sync_chain_depth(
            module,
            c.dst_flop,
            c.dst_clock,
            ctx.domains,
            ctx.reader_counts,
            d_bit_to_single_bit_flop=ctx.d_bit_to_single_bit_flop,
        )
        if depth < 2:
            continue
        grouped[(c.src_flop.cell.name, c.dst_clock)].append(c)

    for (src_name, dst_clk), group in grouped.items():
        if len(group) < 2:
            continue
        # Reconvergence filter (issue #33): walk forward from each
        # chain's *terminal* flop's Q and look for any downstream cell
        # reached by ≥2 chains. The walk uses
        # :func:`_forward_reachable_cells` so a recombination point
        # that isn't itself a flop (e.g. a comb reduction driving an
        # unregistered top-level port) still counts. Chain-internal
        # flops are excluded so chains never "reconverge on
        # themselves" — only genuinely downstream cells count. If no
        # downstream cell is reached by 2+ chains, the group's
        # redundancy is harmless and we don't fire.
        appears_in: dict[str, int] = defaultdict(int)
        for c in group:
            chain = _sync_chain_flops(
                module,
                c.dst_flop,
                c.dst_clock,
                ctx.domains,
                ctx.reader_counts,
                ctx.d_bit_to_single_bit_flop,
            )
            terminal = chain[-1]
            chain_internal = {f.cell.name for f in chain}
            reached = _forward_reachable_cells(
                module,
                start_bits=terminal.q,
                consumers=ctx.bit_consumers,
            )
            for cell_name in reached - chain_internal:
                appears_in[cell_name] += 1
        if not any(count >= 2 for count in appears_in.values()):
            continue

        dst_names = sorted(c.dst_flop.cell.name for c in group)
        violations.append(
            Violation(
                rule_id="CDC-005",
                severity="warning",
                message=(
                    f"reconvergent synchronizers: source flop "
                    f"{src_name} drives {len(group)} independent sync "
                    f"chains in domain {dst_clk} (dst flops: "
                    f"{', '.join(dst_names)}); independent metastability "
                    f"resolution can produce mismatched synchronized "
                    f"values when these outputs recombine"
                ),
                crossing=group[0],
                cell_name=src_name,
            )
        )
    return violations


def check_cdc_006(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-006 — Glitchy combinational source on a control crossing.

    Fires when a synchronizer first stage's ``D`` pin traces back —
    through combinational logic only — to one or more top-level input
    ports without an intervening source-domain flop. The synchronizer
    can therefore sample a transient comb-output value that never
    represented a stable input state.

    A "synchronizer first stage" here is a flop with chain depth >= 2
    in its own clock domain (so CDC-001 doesn't already cover the
    case). Top-level ports declared as clocks in the SDC are
    intentionally ignored — they aren't logic data, just a clock
    signal whose level is irrelevant to data correctness.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []

    clock_ports: set[str] = set()
    if clock_spec is not None:
        for clk in clock_spec.clocks.values():
            clock_ports.update(clk.ports)

    for f in ctx.flops:
        my_clk = ctx.domains.get(f.cell.name)
        if my_clk is None:
            continue
        # User-marked synchronizers are explicitly trusted regardless
        # of the input shape.
        if f.cell.name in ctx.user_syncs:
            continue
        # We only fire on flops that are bona fide synchronizer first
        # stages — i.e. the chain on the destination side is >= 2.
        depth = _sync_chain_depth(
            module,
            f,
            my_clk,
            ctx.domains,
            ctx.reader_counts,
            d_bit_to_single_bit_flop=ctx.d_bit_to_single_bit_flop,
        )
        if depth < 2:
            continue
        fanin_flops, fanin_ports = _backward_fanin(module, f.d, ctx.bit_drivers)
        # If a real source-domain flop exists in the fanin, this
        # crossing is already covered by CDC-001/-002/-003 logic.
        if fanin_flops:
            continue
        # Drop ports that are top-level clocks (clock leakage into
        # data paths is a CDC-008 shape, not CDC-006) and ports that
        # the SDC has explicitly typed into the *same* domain as the
        # destination flop via `set_input_delay -clock <my_clk>`.
        my_clock_name: str | None = None
        if clock_spec is not None:
            my_clock_name = clock_spec.clock_for_port(my_clk) or my_clk
        flagged_ports: list[tuple[str, str | None]] = []
        for p in sorted(fanin_ports):
            if p in clock_ports:
                continue
            port_clk: str | None = None
            if clock_spec is not None:
                port_clk = clock_spec.port_clock.get(p)
            if (
                port_clk is not None
                and my_clock_name is not None
                and clock_spec is not None
            ):
                # Both ends typed: only fire when the port's clock
                # resolves to a different root than the sync's clock.
                # If they match, the user has asserted same-domain
                # timing — no CDC issue.
                if clock_spec.resolve(port_clk) == clock_spec.resolve(my_clock_name):
                    continue
            flagged_ports.append((p, port_clk))
        if not flagged_ports:
            continue
        port_descs = ", ".join(
            f"{p}" if pc is None else f"{p} (clock={pc})" for p, pc in flagged_ports
        )
        violations.append(
            Violation(
                rule_id="CDC-006",
                severity="error",
                message=(
                    f"glitchy combinational source on synchronizer "
                    f"{f.cell.name} (clk={my_clk}): D pin is driven "
                    f"by combinational logic with no registering flop, "
                    f"reaching unregistered top-level port(s): "
                    f"{port_descs}"
                ),
                cell_name=f.cell.name,
            )
        )
    return violations


def _clock_network_cells(
    module: Module,
    drivers: dict[Bit, tuple[str, str, int]],
) -> set[str]:
    """Cells whose output transitively drives some flop's ``CLK`` pin.

    These cells form the legitimate clock-distribution network
    (buffers, ICGs, clock muxes, dividers, etc.); reading a clock
    signal on one of their input pins is *expected*, not a bug.
    Used by CDC-008 to suppress false positives on intentional clock
    gating / muxing structures.
    """
    cells: set[str] = set()
    seen_bits: set[Bit] = set()
    frontier: list[Bit] = []
    for f in find_flops(module):
        if isinstance(f.clk, int):
            frontier.append(f.clk)
    while frontier:
        nxt: list[Bit] = []
        for bit in frontier:
            if bit in seen_bits:
                continue
            seen_bits.add(bit)
            drv = drivers.get(bit)
            if drv is None:
                continue
            cell_name, _out_port, _idx = drv
            if cell_name in cells:
                continue
            cells.add(cell_name)
            cell = module.cells[cell_name]
            for in_port, in_bits in cell.connections.items():
                if in_port in _OUTPUT_PINS:
                    continue
                for b in in_bits:
                    if isinstance(b, int):
                        nxt.append(b)
        frontier = nxt
    return cells


def check_cdc_008(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-008 — Clock signal used as data.

    Fires when a bit that drives any flop's ``CLK`` pin (or that's
    declared as a clock in the SDC) is also wired into a non-clock
    pin on a cell that isn't part of the clock-distribution network.

    Clocks ride on dedicated low-skew networks; sampling them as data
    delivers the high-frequency edge toggle, breaks STA, and almost
    always indicates a wiring mistake. The CLK pin itself, cells on
    the clock network (buffers, ICGs, clock muxes, dividers — anything
    whose output transitively drives a flop CLK), and top-level
    *output* ports (forwarding a clock off-chip) are intentionally
    not flagged.
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []

    # Build the set of net bits that act as clocks: union of every
    # flop's CLK pin and every bit covered by an SDC create_clock
    # port.
    clock_bits: set[Bit] = set()
    for f in ctx.flops:
        if isinstance(f.clk, int):
            clock_bits.add(f.clk)
    if clock_spec is not None:
        for clk in clock_spec.clocks.values():
            for port_name in clk.ports:
                port = module.ports.get(port_name)
                if port is None:
                    continue
                for b in port.bits:
                    if isinstance(b, int):
                        clock_bits.add(b)
    if not clock_bits:
        return violations

    # Map each clock bit to a human-readable label, preferring the
    # top-level port name where applicable.
    def _label(bit: Bit) -> str:
        port = module.port_of_bit(bit)
        return port.name if port is not None else f"net@{bit}"

    # Compute the clock-distribution network once: every cell whose
    # output eventually feeds some flop CLK is exempt.
    clock_net_cells = _clock_network_cells(module, ctx.bit_drivers)

    # Walk every cell connection and report each (clock_bit, cell, pin)
    # triple where the bit appears on a non-CLK input pin. We dedupe
    # at the (bit, cell, pin) granularity so a multi-bit pin reporting
    # the same clock once doesn't blow up to N violations.
    seen: set[tuple[Bit, str, str]] = set()
    for cell in module.cells.values():
        if cell.name in clock_net_cells:
            continue
        for port_name, bits in cell.connections.items():
            if port_name == "CLK" or port_name in _OUTPUT_PINS:
                continue
            for idx, b in enumerate(bits):
                if not isinstance(b, int) or b not in clock_bits:
                    continue
                key = (b, cell.name, port_name)
                if key in seen:
                    continue
                seen.add(key)
                violations.append(
                    Violation(
                        rule_id="CDC-008",
                        severity="error",
                        message=(
                            f"clock signal {_label(b)} used as data: "
                            f"connected to {cell.name}.{port_name}[{idx}] "
                            f"(cell type {cell.type})"
                        ),
                        cell_name=cell.name,
                    )
                )
    return violations


def check_cdc_007(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,
    *,
    ctx: _RuleContext | None = None,
) -> list[Violation]:
    """CDC-007 — Reset crossing without a reset synchronizer.

    Fires when a flop's asynchronous reset pin (``ARST``) is driven by
    another flop sitting in a *different* asynchronous clock domain.
    The classic safe pattern is "async-assert, sync-deassert": the
    reset is asserted immediately but its deassertion is filtered
    through a 2FF chain on the destination clock — and crucially, the
    reset signal itself originates from a top-level pin or from a
    reset synchronizer in the *destination* domain, not from a flop in
    a foreign domain.

    Reset signals coming from top-level ports are intentionally
    accepted (they're the user's responsibility to drive correctly,
    e.g. the reset synchronizer's ARST input).
    """
    if ctx is None:
        ctx = _build_context(module, clock_spec)
    violations: list[Violation] = []

    def _async(a: str, b: str) -> bool:
        # If the user supplied an SDC, defer to its clock-groups
        # statement. Without an SDC every distinct domain is treated
        # as asynchronous (conservative — surfaces possible issues).
        if clock_spec is None:
            return a != b
        ca = clock_spec.clock_for_port(a) or a
        cb = clock_spec.clock_for_port(b) or b
        return clock_spec.are_async(ca, cb)

    # Collect ARST → (foreign source flop) edges. Group by
    # (src_flop_name, dst_clk) so a single async source feeding many
    # destination flops in the same domain becomes ONE violation that
    # lists every destination — matches how a real reset distribution
    # tree is reviewed.
    edges: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for f in ctx.flops:
        arst_bits = f.cell.connections.get("ARST", ())
        if not arst_bits:
            continue
        my_clk = ctx.domains.get(f.cell.name)
        if my_clk is None:
            continue
        fanin_flops = _backward_flop_fanin(module, arst_bits, ctx.bit_drivers)
        for src_name in fanin_flops:
            src_clk = ctx.domains.get(src_name)
            if src_clk is None or src_clk == my_clk:
                continue
            if not _async(my_clk, src_clk):
                continue
            edges[(src_name, src_clk, my_clk)].append(f.cell.name)

    for (src_name, src_clk, dst_clk), dst_flops in edges.items():
        dsts = sorted(set(dst_flops))
        # Choose representative cell for source-location reporting:
        # the first destination (alphabetically) — the source flop's
        # location would also work, but the destination is what the
        # user has to fix.
        repr_cell = dsts[0]
        if len(dsts) == 1:
            dst_desc = f"destination flop: {dsts[0]}"
        else:
            dst_desc = (
                f"{len(dsts)} destination flops share this source "
                f"(reset distribution tree): "
                f"{', '.join(dsts[:3])}" + (", ..." if len(dsts) > 3 else "")
            )
        violations.append(
            Violation(
                rule_id="CDC-007",
                severity="error",
                message=(
                    f"async reset crossing: flop(s) in clk={dst_clk} "
                    f"have ARST driven by flop {src_name} from a "
                    f"different domain (clk={src_clk}); add a reset "
                    f"synchronizer in the {dst_clk} domain. {dst_desc}"
                ),
                cell_name=repr_cell,
            )
        )
    return violations


RULES: dict[str, RuleFn] = {
    "CDC-001": check_cdc_001,
    "CDC-002": check_cdc_002,
    "CDC-003": check_cdc_003,
    "CDC-004": check_cdc_004,
    "CDC-005": check_cdc_005,
    "CDC-006": check_cdc_006,
    "CDC-007": check_cdc_007,
    "CDC-008": check_cdc_008,
}


def run_all(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,
    required_depth: int = 2,
) -> list[Violation]:
    # Build the cached structural views once and thread them through
    # every rule. See :class:`_RuleContext` for the motivation —
    # before this change, ``assign_domains`` / ``find_flops`` /
    # ``_bit_drivers`` were each rebuilt per rule, with
    # ``_sync_chain_depth`` re-scanning every flop per chain-extension
    # step (the worst hot path).
    ctx = _build_context(module, clock_spec)
    out: list[Violation] = []
    for rule_id, rule in RULES.items():
        if rule_id == "CDC-002":
            out.extend(
                check_cdc_002(module, crossings, clock_spec, required_depth, ctx=ctx)
            )
        else:
            out.extend(rule(module, crossings, clock_spec, ctx=ctx))
    return out
