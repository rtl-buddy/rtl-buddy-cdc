"""Dual-clock RAM with no write-domain control on the read side (#326).

Formerly the #176 coverage-gap sentinel ``unsupported_dualport_ram_crossing``,
which pinned that the analyzer saw *nothing* here: Yosys keeps the RAM as
``$memwr_v2`` / ``$memrd`` cells and the flop-to-flop walk could not cross
them. Memory ports are now crossing endpoints and the storage path is
CDC-026's subject, so the fixture is an ordinary bad_ fixture: CDC-026
fires once, naming the memory and both clocks, and nothing else does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import filter_async, find_crossings
from rtl_buddy_cdc.reporter import _source_location
from rtl_buddy_cdc.rules import memory_crossings, run_all as run_all_rules

NAME = "bad_dualport_ram_crossing"
FIX_DIR = Path(__file__).parent / "fixtures" / NAME
JSON = FIX_DIR / f"{NAME}.json"
SDC = FIX_DIR / f"{NAME}.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(
        module, port_clock=spec.port_clock, clock_for_port=spec.clock_for_port
    )
    return module, filter_async(crossings, spec), spec


def test_no_flop_crossing_but_one_memory_crossing(context) -> None:
    """Every flop / port path is single-domain; the only crossing is the
    storage array itself, and nothing from wr_clk gates the read side."""
    module, async_crossings, spec = context
    assert async_crossings == []
    records = memory_crossings(module, async_crossings, spec)
    assert [(m.memory, m.write_clock, m.read_clock) for m in records] == [
        ("mem", "wr_clk", "rd_clk")
    ]
    assert records[0].async_per_sdc
    assert records[0].control == ()


def test_cdc_026_fires_alone(context) -> None:
    module, async_crossings, spec = context
    violations = run_all_rules(module, async_crossings, spec)
    assert [v.rule_id for v in violations] == ["CDC-026"]
    v = violations[0]
    assert v.severity == "error"
    assert "`mem`" in v.message
    assert "`wr_clk`" in v.message and "`rd_clk`" in v.message
    # Anchored on the read port cell, so the report carries the RTL line
    # of the `rd_data <= mem[rd_addr]` read.
    loc = _source_location(module, v.cell_name)
    assert loc is not None
    assert loc["file"].endswith(f"{NAME}.sv")
