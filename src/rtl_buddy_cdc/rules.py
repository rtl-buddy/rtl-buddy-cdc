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


def _sync_chain_depth(
    module: Module,
    head: Flop,
    head_clock: str,
    domains: dict[str, str | None],
    q_to_flop: dict[Bit, Flop],
) -> int:
    """Length of the synchronizer chain that starts at ``head``.

    A chain extends to the next flop iff:

    - the next flop is in the same clock domain, and
    - the next flop's D pin is driven solely by the previous flop's Q
      bit on the same lane (i.e. ``D[i]`` == previous-flop's ``Q[i]``).

    Returns the count of dst-domain flops *including* ``head``. Callers
    typically check ``depth >= 2``.
    """
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
        consumers: list[Flop] = []
        for f in find_flops(module):
            if f.cell.name in visited:
                continue
            if next_q in f.d and len(f.d) == 1 and f.d[0] == next_q:
                consumers.append(f)
        if len(consumers) != 1:
            break
        nxt = consumers[0]
        if domains.get(nxt.cell.name) != head_clock:
            break
        depth += 1
        visited.add(nxt.cell.name)
        current = nxt
    return depth


# --- rules ------------------------------------------------------------------


def check_cdc_002(
    module: Module, crossings: list[Crossing], min_depth: int = 2
) -> list[Violation]:
    """CDC-002 — Insufficient synchronizer depth on a control crossing.

    Applies to single-bit crossings only (multi-bit buses use a
    different correctness pattern — see CDC-004).
    """
    violations: list[Violation] = []
    domains = {fd.flop.cell.name: fd.clock for fd in assign_domains(module)}
    q_to_flop = _q_to_flop(module)
    _ = q_to_flop  # reserved: future rules will need fast Q→flop lookup

    for c in crossings:
        if c.width != 1:
            continue
        depth = _sync_chain_depth(module, c.dst_flop, c.dst_clock, domains, q_to_flop)
        if depth < min_depth:
            violations.append(
                Violation(
                    rule_id="CDC-002",
                    severity="error",
                    message=(
                        f"insufficient synchronizer depth on "
                        f"{c.src_clock} → {c.dst_clock} crossing: "
                        f"found {depth} flop(s), expected >= {min_depth} "
                        f"(dst flop: {c.dst_flop.name})"
                    ),
                    crossing=c,
                )
            )
    return violations


RULES: dict[str, RuleFn] = {
    "CDC-002": check_cdc_002,
}


def run_all(module: Module, crossings: list[Crossing]) -> list[Violation]:
    out: list[Violation] = []
    for rule in RULES.values():
        out.extend(rule(module, crossings))
    return out
