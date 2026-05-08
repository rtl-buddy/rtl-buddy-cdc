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
from rtl_buddy_cdc.flops import Flop, find_flops
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


RuleFn = Callable[[Module, list[Crossing], "ClockSpec | None"], list[Violation]]


# --- helpers ----------------------------------------------------------------


def _q_to_flop(module: Module) -> dict[Bit, Flop]:
    """Map each bit driven by a flop's Q pin back to the source flop."""
    out: dict[Bit, Flop] = {}
    for f in find_flops(module):
        for b in f.q:
            if isinstance(b, int):
                out[b] = f
    return out


def _bit_drivers(module: Module) -> dict[Bit, tuple[str, str, int]]:
    """Map each bit to the (cell_name, output_port, bit_idx) that drives it.

    Yosys $-prefixed combinational cells emit on ``Y``; FFs emit on
    ``Q``. Bits that originate from a top-level input port aren't
    listed here — callers should fall back to :meth:`Module.port_of_bit`.
    """
    out: dict[Bit, tuple[str, str, int]] = {}
    for cell in module.cells.values():
        for port_name in ("Y", "Q"):
            bits = cell.connections.get(port_name, ())
            for idx, b in enumerate(bits):
                if isinstance(b, int):
                    out[b] = (cell.name, port_name, idx)
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
    q_to_flop: dict[Bit, Flop],
    reader_counts: dict[Bit, int] | None = None,
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
    """
    if reader_counts is None:
        reader_counts = _bit_reader_count(module)

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
        nxt: Flop | None = None
        for f in find_flops(module):
            if f.cell.name in visited:
                continue
            if len(f.d) == 1 and f.d[0] == next_q:
                nxt = f
                break
        if nxt is None:
            break
        if domains.get(nxt.cell.name) != head_clock:
            break
        depth += 1
        visited.add(nxt.cell.name)
        current = nxt
    return depth


# --- rules ------------------------------------------------------------------


def check_cdc_001(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
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
    violations: list[Violation] = []
    domains = {fd.flop.cell.name: fd.clock for fd in assign_domains(module)}
    q_to_flop = _q_to_flop(module)
    _ = q_to_flop  # reserved: future rules will need fast Q→flop lookup

    for c in crossings:
        if c.width != 1:
            continue
        depth = _sync_chain_depth(module, c.dst_flop, c.dst_clock, domains, q_to_flop)
        if depth < 2:
            violations.append(
                Violation(
                    rule_id="CDC-001",
                    severity="error",
                    message=(
                        f"unsynchronized control crossing "
                        f"{c.src_clock} → {c.dst_clock}: "
                        f"destination flop {c.dst_flop.name} has no "
                        f"second-stage synchronizer (chain depth = {depth})"
                    ),
                    crossing=c,
                )
            )
    return violations


def check_cdc_002(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
    required_depth: int = 2,
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
    violations: list[Violation] = []
    domains = {fd.flop.cell.name: fd.clock for fd in assign_domains(module)}
    q_to_flop = _q_to_flop(module)
    _ = q_to_flop

    for c in crossings:
        if c.width != 1:
            continue
        depth = _sync_chain_depth(module, c.dst_flop, c.dst_clock, domains, q_to_flop)
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
                )
            )
    return violations


def check_cdc_003(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
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
    violations: list[Violation] = []
    domains = {fd.flop.cell.name: fd.clock for fd in assign_domains(module)}
    q_to_flop = _q_to_flop(module)
    reader_counts = _bit_reader_count(module)

    for c in crossings:
        if c.width != 1:
            continue
        if c.min_hops < 1:
            continue
        depth = _sync_chain_depth(
            module, c.dst_flop, c.dst_clock, domains, q_to_flop, reader_counts
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
                    f"(src flop: {c.src_flop.name}, "
                    f"sync first stage: {c.dst_flop.name})"
                ),
                crossing=c,
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


def check_cdc_004(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
) -> list[Violation]:
    """CDC-004 — Multi-bit bus crossing without gating or gray-coding.

    A multi-bit data path that crosses clock domains needs an extra
    coherence mechanism on top of per-bit synchronization, because
    individual bits can settle on different destination cycles.
    Acceptable patterns are:

    - **Handshake / load-enable gating** — destination flops only
      sample the bus when a synchronized control signal allows it
      (the golden ``ip_cdc_handshake`` shape).
    - **Gray-coded counters** — only one bit changes per source cycle
      (FIFO pointer pattern; not yet recognized by this rule).

    The current implementation accepts handshake-style gating via the
    ``_is_gated_bus_crossing`` heuristic and flags everything else.
    """
    violations: list[Violation] = []
    domains = {fd.flop.cell.name: fd.clock for fd in assign_domains(module)}
    drivers = _bit_drivers(module)

    for c in crossings:
        if c.width <= 1:
            continue
        if _is_gated_bus_crossing(module, c, domains, drivers):
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
            )
        )
    return violations


def check_cdc_005(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,  # noqa: ARG001
) -> list[Violation]:
    """CDC-005 — Reconvergent synchronizers.

    Fires when a single source-domain flop fans out to two or more
    *independent* synchronizer chains in the same destination domain.
    Each chain individually filters metastability, but their resolution
    times can differ by a destination cycle, so any downstream logic
    that recombines the synchronized outputs may observe a value pair
    that was never simultaneously true at the source.

    The MVP detection is purely structural: it groups single-bit
    crossings by ``(src_flop, dst_clock)`` and reports when a single
    source feeds two-or-more sync first stages. We deliberately don't
    try (yet) to prove that the recombination actually happens
    downstream — having the redundant synchronizers is itself a code
    smell worth surfacing.
    """
    violations: list[Violation] = []
    domains = {fd.flop.cell.name: fd.clock for fd in assign_domains(module)}
    q_to_flop = _q_to_flop(module)
    reader_counts = _bit_reader_count(module)

    # Group single-bit, properly synchronized crossings by source flop
    # + destination domain. We require depth>=2 so we don't double-fire
    # against CDC-001 (the depth==1 cases will be reported there).
    grouped: dict[tuple[str, str], list[Crossing]] = defaultdict(list)
    for c in crossings:
        if c.width != 1:
            continue
        depth = _sync_chain_depth(
            module, c.dst_flop, c.dst_clock, domains, q_to_flop, reader_counts
        )
        if depth < 2:
            continue
        grouped[(c.src_flop.cell.name, c.dst_clock)].append(c)

    for (src_name, dst_clk), group in grouped.items():
        if len(group) < 2:
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
            )
        )
    return violations


def check_cdc_006(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,
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
    violations: list[Violation] = []
    domains = {fd.flop.cell.name: fd.clock for fd in assign_domains(module)}
    q_to_flop = _q_to_flop(module)
    drivers = _bit_drivers(module)
    reader_counts = _bit_reader_count(module)

    clock_ports: set[str] = set()
    if clock_spec is not None:
        for clk in clock_spec.clocks.values():
            clock_ports.update(clk.ports)

    for f in find_flops(module):
        my_clk = domains.get(f.cell.name)
        if my_clk is None:
            continue
        # We only fire on flops that are bona fide synchronizer first
        # stages — i.e. the chain on the destination side is >= 2.
        depth = _sync_chain_depth(module, f, my_clk, domains, q_to_flop, reader_counts)
        if depth < 2:
            continue
        fanin_flops, fanin_ports = _backward_fanin(module, f.d, drivers)
        # If a real source-domain flop exists in the fanin, this
        # crossing is already covered by CDC-001/-002/-003 logic.
        if fanin_flops:
            continue
        ungated_ports = sorted(p for p in fanin_ports if p not in clock_ports)
        if not ungated_ports:
            continue
        violations.append(
            Violation(
                rule_id="CDC-006",
                severity="error",
                message=(
                    f"glitchy combinational source on synchronizer "
                    f"{f.cell.name} (clk={my_clk}): D pin is driven "
                    f"by combinational logic with no registering flop, "
                    f"reaching unregistered top-level port(s): "
                    f"{', '.join(ungated_ports)}"
                ),
            )
        )
    return violations


def check_cdc_007(
    module: Module,
    crossings: list[Crossing],  # noqa: ARG001
    clock_spec: ClockSpec | None = None,
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
    violations: list[Violation] = []
    domains = {fd.flop.cell.name: fd.clock for fd in assign_domains(module)}
    drivers = _bit_drivers(module)

    def _async(a: str, b: str) -> bool:
        # If the user supplied an SDC, defer to its clock-groups
        # statement. Without an SDC every distinct domain is treated
        # as asynchronous (conservative — surfaces possible issues).
        if clock_spec is None:
            return a != b
        ca = clock_spec.clock_for_port(a) or a
        cb = clock_spec.clock_for_port(b) or b
        return clock_spec.are_async(ca, cb)

    for f in find_flops(module):
        arst_bits = f.cell.connections.get("ARST", ())
        if not arst_bits:
            continue
        my_clk = domains.get(f.cell.name)
        if my_clk is None:
            continue
        fanin_flops = _backward_flop_fanin(module, arst_bits, drivers)
        for src_name in fanin_flops:
            src_clk = domains.get(src_name)
            if src_clk is None or src_clk == my_clk:
                continue
            if not _async(my_clk, src_clk):
                continue
            violations.append(
                Violation(
                    rule_id="CDC-007",
                    severity="error",
                    message=(
                        f"async reset crossing: flop {f.cell.name} "
                        f"(clk={my_clk}) has ARST driven by flop "
                        f"{src_name} from a different domain "
                        f"(clk={src_clk}); add a reset synchronizer "
                        f"in the {my_clk} domain"
                    ),
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
}


def run_all(
    module: Module,
    crossings: list[Crossing],
    clock_spec: ClockSpec | None = None,
) -> list[Violation]:
    out: list[Violation] = []
    for rule in RULES.values():
        out.extend(rule(module, crossings, clock_spec))
    return out
