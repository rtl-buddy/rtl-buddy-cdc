"""CDC rule checks.

Each rule is a small function ``check_<rule>(...) -> list[Violation]``
operating on the netlist + the list of crossings (already filtered to
asynchronous pairs by the caller). Adding a rule is intentionally a
self-contained edit: register the function in :data:`RULES` and you're
done.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from rtl_buddy_cdc.domain import Crossing, assign_domains
from rtl_buddy_cdc.flops import Flop, find_flops
from rtl_buddy_cdc.netlist import Bit, Module


@dataclass(frozen=True)
class Violation:
    rule_id: str
    severity: str  # "error" | "warning" | "info"
    message: str
    crossing: Crossing


RuleFn = Callable[[Module, list[Crossing]], list[Violation]]


# --- helpers ----------------------------------------------------------------


def _q_to_flop(module: Module) -> dict[Bit, Flop]:
    """Map each bit driven by a flop's Q pin back to the source flop."""
    out: dict[Bit, Flop] = {}
    for f in find_flops(module):
        for b in f.q:
            if isinstance(b, int):
                out[b] = f
    return out


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


def check_cdc_001(module: Module, crossings: list[Crossing]) -> list[Violation]:
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


def check_cdc_003(module: Module, crossings: list[Crossing]) -> list[Violation]:
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


RULES: dict[str, RuleFn] = {
    "CDC-001": check_cdc_001,
    "CDC-002": check_cdc_002,
    "CDC-003": check_cdc_003,
}


def run_all(module: Module, crossings: list[Crossing]) -> list[Violation]:
    out: list[Violation] = []
    for rule in RULES.values():
        out.extend(rule(module, crossings))
    return out
