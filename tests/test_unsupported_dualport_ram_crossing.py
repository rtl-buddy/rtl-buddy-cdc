"""Sentinel fixture for the dual-port RAM coverage gap (issue #176).

rtl-buddy-cdc is a flop-based analyzer. Designs that cross clock
domains through a dual-clock memory (write port in clock A, read
port in clock B) flow through ``$memrd`` / ``$memwr_v2`` cells the
rule pack does not model — the storage boundary is invisible to the
crossing walker.

This test pins that behaviour. The fixture writes a small register
array from ``wr_clk`` and reads it back from ``rd_clk``; the two
clocks are declared asynchronous. A vendor memory-compiler CDC
report is the right tool for this shape; rtl-buddy-cdc reports
nothing on the memory-side data path today. If a future change
either (a) starts reporting crossings here without explicit
memory-domain support, or (b) the existing zero-finding behaviour
changes, this test surfaces it as a regression so the doc gap can
be re-evaluated.

See README.md "Unsupported patterns" and issue #176.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "unsupported_dualport_ram_crossing"
JSON = FIX_DIR / "unsupported_dualport_ram_crossing.json"
SDC = FIX_DIR / "unsupported_dualport_ram_crossing.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_memory_crossing_invisible_to_analyzer(context) -> None:
    """The wr_clk → mem → rd_clk hazard flows through ``$memwr_v2`` /
    ``$memrd`` cells the rule pack does not walk. No async crossings
    are detected, no rules fire — this is the documented coverage
    gap, not a clean design."""
    _module, crossings, _spec = context

    async_crossings = [c for c in crossings if c.async_per_sdc]
    assert async_crossings == [], (
        "regression: dual-port RAM crossing is now being detected — "
        "this used to be a known coverage gap (see README.md "
        '"Unsupported patterns" and issue #176). If the analyzer has '
        "grown memory-domain support, update the README and remove "
        "this sentinel."
    )


def test_no_rule_violations_on_memory_design(context) -> None:
    """Same sentinel from the rule-pack side. Zero violations today;
    if any rule starts firing here, the doc gap needs to be revisited."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    assert violations == [], (
        "regression: rules now fire on dual-port RAM crossing — "
        f"got {[(v.rule_id, v.message) for v in violations]}. "
        "See issue #176."
    )
