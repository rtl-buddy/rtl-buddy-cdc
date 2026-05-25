"""Analyzer-differential runner.

For each rendered case, drive it through Yosys, load the netlist
with :mod:`rtl_buddy_cdc.netlist`, run ``run_all_rules``, and diff
the resulting findings against the template's
:class:`ExpectedFinding` and ``forbidden`` claims.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

from .templates import ALL_TEMPLATES
from .templates.base import RenderedCase
from .yosys_cache import build, yosys_available


def collect_cases() -> list[RenderedCase]:
    """Enumerate every concrete case across every registered template.

    The order is deterministic: insertion-order of ``ALL_TEMPLATES``
    × insertion-order yielded by each template's ``cases``.
    """
    out: list[RenderedCase] = []
    for template in ALL_TEMPLATES:
        for case in template.cases():
            out.append(case)
    return out


@dataclass(frozen=True)
class CaseResult:
    case: RenderedCase
    fired: dict[str, int]
    cache_hit: bool
    failures: tuple[str, ...]


def run_case(case: RenderedCase) -> CaseResult:
    """Build, analyze, and diff a single case. Never raises on
    analyzer findings — failures are recorded in the result."""
    if not yosys_available():
        raise RuntimeError("yosys not on PATH")
    cached = build(case)
    module = netlist.load(cached.json_path)
    # Write the SDC to a temp adjacent to the cached json so the
    # parser can read it; cache it next to the json so subsequent
    # runs see the same path.
    sdc_path = cached.json_path.with_suffix(".sdc")
    if not sdc_path.exists():
        sdc_path.write_text(case.sdc)
    spec = sdc_mod.parse_file(sdc_path)
    # Mirrors what cli.py does between SDC parse and find_crossings:
    # mark unconstrained top-level inputs with UNCONSTRAINED_SENTINEL
    # so CDC-011 can see them.
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(module, port_clock=spec.port_clock)

    required_depth = int(case.params.get("required_depth", 2))
    violations = run_all_rules(module, crossings, spec, required_depth=required_depth)

    fired = Counter(v.rule_id for v in violations)
    failures: list[str] = []

    for expected in case.expected:
        actual = fired.get(expected.rule_id, 0)
        if not expected.check(actual):
            failures.append(
                f"{expected.rule_id}: expected {expected.op.value} "
                f"{expected.count}, got {actual} "
                f"(fired={dict(fired)})"
            )
    for forbid in case.forbidden:
        actual = fired.get(forbid.rule_id, 0)
        if not forbid.check(actual):
            failures.append(
                f"{forbid.rule_id}: expected to stay silent, "
                f"got {actual} (fired={dict(fired)})"
            )

    return CaseResult(
        case=case,
        fired=dict(fired),
        cache_hit=cached.cache_hit,
        failures=tuple(failures),
    )


def iter_results() -> Iterator[CaseResult]:
    """Stream-run the full corpus. Used by the coverage reporter."""
    for case in collect_cases():
        yield run_case(case)
