"""Positive-counterpart fixtures.

For each "bad_*" fixture there's a "good_*" version implementing the
textbook fix. These tests pin the analyzer's *acceptance shape* —
they catch false positives if a rule gets tightened in a way that
flags a known-correct pattern.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_ROOT = Path(__file__).parent / "fixtures"

# Each entry is (fixture_dir_name, expected_async_crossing_count). The
# count is asserted so we don't accidentally regress to "0 crossings,
# trivially passes" — the analyzer must actually be exercising the
# crossing path on each fixture.
GOOD_FIXTURES = [
    ("good_2ff_sync", 1),
    ("good_registered_before_sync", 1),
    ("good_registered_source", 1),
    ("good_reset_sync", 1),
    # Generated clock: divider Q is a flop, but its CLK traces back to
    # the master via trace_clock_root, so the analyzer assigns all
    # flops to the master domain — zero structural crossings.
    ("good_generated_clock_div2", 0),
    # Exclusive clocks: structural pass sees 1 ck0→ck1 flop→flop pair,
    # but _filter_async drops it via is_unreachable_crossing.
    ("good_exclusive_clock_mux", 0),
    # set_false_path is equivalent to set_clock_groups -asynchronous
    # for CDC; 1 async crossing landing in a 2FF synchronizer.
    ("good_false_path_pair", 1),
    # set_input_delay -clock dst_clk types the data ports as same-
    # domain as the destination — CDC-006 must not fire.
    ("good_input_delay_domain", 0),
]


@pytest.mark.parametrize("name, expected_async", GOOD_FIXTURES)
def test_good_fixture_has_no_violations(name: str, expected_async: int) -> None:
    fix_dir = FIX_ROOT / name
    json_path = fix_dir / f"{name}.json"
    sdc_path = fix_dir / f"{name}.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")

    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    crossings = find_crossings(module)
    async_crossings = []
    for c in crossings:
        a = spec.clock_for_port(c.src_clock) or c.src_clock
        b = spec.clock_for_port(c.dst_clock) or c.dst_clock
        if spec.is_unreachable_crossing(a, b):
            continue
        if spec.are_async(a, b):
            async_crossings.append(c)
    assert len(async_crossings) == expected_async, (
        f"{name}: expected {expected_async} async crossing(s), "
        f"got {len(async_crossings)} — fixture may have regressed"
    )

    violations = run_all_rules(module, async_crossings, spec)
    assert violations == [], (
        f"{name}: expected zero violations, got "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )


def test_cdc_002_fires_when_required_depth_raised() -> None:
    """A 2-stage synchronizer is silent at the default required_depth=2
    but must fire CDC-002 when the project raises the bar to 3."""
    name = "good_2ff_sync"
    fix_dir = FIX_ROOT / name
    json_path = fix_dir / f"{name}.json"
    sdc_path = fix_dir / f"{name}.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")

    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    crossings = find_crossings(module)
    async_crossings = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]

    assert run_all_rules(module, async_crossings, spec, required_depth=2) == []
    raised = run_all_rules(module, async_crossings, spec, required_depth=3)
    assert len(raised) == 1 and raised[0].rule_id == "CDC-002"
