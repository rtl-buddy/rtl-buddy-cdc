"""P1 depth flag (issue #263) — the clock-trace hop budget is configurable.

On a deep clock tree the fixed 16-hop budget in ``trace_clock_root`` gives up
and leaves flops domain-unknown. ``deep_clock_divider_chain`` clocks a flop
through a 30-stage ripple divider, so its clock root is ~30 divider hops above
``clk_a`` — beyond the default budget.

The change threads a ``max_depth`` parameter through ``assign_domains`` /
``find_crossings`` (default 16) and surfaces it as ``--clock-trace-depth`` on
both ``analyze`` and ``lint``. Raising it resolves the deep flop:

  - at the default depth the flop is counted in ``summary.domain_unknown``;
  - at ``--clock-trace-depth 40`` it resolves to ``clk_a`` and the count drops.

Raising the budget only ever resolves MORE flops, never fewer. The fixture
also carries a genuine clk_a→clk_b crossing whose endpoints resolve at any
depth, so the test pins PARITY: the crossing/violation counts are identical
at depth 16 and depth 40 — the deeper walk adds a resolved domain, it never
perturbs a crossing already found.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import OutputFormat, _analyze_and_report  # noqa: PLC2701
from rtl_buddy_cdc.domain import assign_domains, find_crossings

FIX_DIR = Path(__file__).parent / "fixtures" / "deep_clock_divider_chain"
JSON = FIX_DIR / "deep_clock_divider_chain.json"
SDC = FIX_DIR / "deep_clock_divider_chain.sdc"


def _skip_if_missing() -> None:
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")


def _crossing_keys(crossings: list) -> list[tuple]:
    """A depth-insensitive identity for each crossing, for set comparison."""
    return sorted(
        (
            c.src_name,
            c.dst_flop.cell.name,
            c.src_clock,
            c.dst_clock,
            c.min_hops,
            c.width,
        )
        for c in crossings
    )


def test_deep_flop_unresolved_at_default_depth() -> None:
    """At the default budget (16) the deep-divider-clocked flops are
    domain-unknown — the chain is genuinely deeper than 16 hops."""
    _skip_if_missing()
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    domains = assign_domains(
        module, pin_clocks=spec.pin_clocks, clock_for_port=spec.clock_for_port
    )
    unresolved = [d.flop.cell.name for d in domains if d.clock is None]
    assert unresolved, "expected deep-chain flops to be unresolved at depth 16"


def test_deep_flop_resolves_at_raised_depth() -> None:
    """At ``--clock-trace-depth 40`` every flop resolves — the deeper budget
    lets the divider chain trace back to ``clk_a``."""
    _skip_if_missing()
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    d16 = assign_domains(
        module,
        pin_clocks=spec.pin_clocks,
        clock_for_port=spec.clock_for_port,
        max_depth=16,
    )
    d40 = assign_domains(
        module,
        pin_clocks=spec.pin_clocks,
        clock_for_port=spec.clock_for_port,
        max_depth=40,
    )
    unk16 = {d.flop.cell.name for d in d16 if d.clock is None}
    unk40 = {d.flop.cell.name for d in d40 if d.clock is None}
    # Raising the budget only ever resolves MORE flops, never fewer.
    assert unk40 < unk16
    assert not unk40, "expected all flops resolved at depth 40"
    # The flops that resolved at the deeper budget all resolved to clk_a.
    resolved_deeper = {
        d.flop.cell.name: d.clock for d in d40 if d.flop.cell.name in unk16
    }
    assert set(resolved_deeper.values()) == {"clk_a"}


def test_crossings_parity_across_depths() -> None:
    """PARITY: the genuine clk_a→clk_b crossing is identical at depth 16 and
    depth 40 — the deeper walk resolves more domains but never drops, gains,
    or perturbs a crossing."""
    _skip_if_missing()
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)

    def cross(depth: int) -> list:
        return find_crossings(
            module,
            port_clock=spec.port_clock,
            pin_clocks=spec.pin_clocks,
            clock_for_port=spec.clock_for_port,
            max_depth=depth,
        )

    c16 = cross(16)
    c40 = cross(40)
    assert _crossing_keys(c16) == _crossing_keys(c40)
    # And the parity anchor is non-vacuous: there really is a crossing.
    assert len(c16) >= 1


def test_cli_flag_resolves_deep_flop(tmp_path: Path) -> None:
    """End to end through ``analyze``: ``summary.domain_unknown`` is non-zero
    at the default depth and zero at ``--clock-trace-depth 40``, while the
    crossing/violation counts stay byte-identical."""
    _skip_if_missing()
    default_out = tmp_path / "default.json"
    _analyze_and_report(JSON, SDC, None, OutputFormat.json, default_out)
    default = json.loads(default_out.read_text())

    deep_out = tmp_path / "deep.json"
    _analyze_and_report(
        JSON, SDC, None, OutputFormat.json, deep_out, clock_trace_depth=40
    )
    deep = json.loads(deep_out.read_text())

    assert default["summary"]["domain_unknown"] > 0
    assert deep["summary"]["domain_unknown"] == 0
    # PARITY: raising the budget must not move crossings or violations.
    assert deep["summary"]["crossings"] == default["summary"]["crossings"]
    assert deep["summary"]["violations"] == default["summary"]["violations"]
    assert default["summary"]["crossings"] >= 1
