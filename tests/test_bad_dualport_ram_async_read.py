"""Dual-clock RAM read through an asynchronous read port (#326).

The read data passes combinationally (``mem[rd_addr_q] ^ rd_mask``) into an
``rd_clk`` register; the read address is a free-running ``rd_clk`` counter
with no write-domain information. CDC-026 must fire — its read domain taken
from the capturing register — and nothing else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import filter_async, find_crossings
from rtl_buddy_cdc.memory import find_memories
from rtl_buddy_cdc.rules import memory_crossings, run_all as run_all_rules

NAME = "bad_dualport_ram_async_read"
FIX_DIR = Path(__file__).parent / "fixtures" / NAME


def _load():
    json_path = FIX_DIR / f"{NAME}.json"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(FIX_DIR / f"{NAME}.sdc")
    crossings = find_crossings(
        module, port_clock=spec.port_clock, clock_for_port=spec.clock_for_port
    )
    return module, filter_async(crossings, spec), spec


def test_read_port_is_asynchronous() -> None:
    module, _, _ = _load()
    (mem,) = find_memories(module)
    assert [p.clocked for p in mem.read_ports] == [False]
    assert [p.clocked for p in mem.write_ports] == [True]


def test_cdc_026_fires_alone() -> None:
    module, async_crossings, spec = _load()
    records = memory_crossings(module, async_crossings, spec)
    assert [(m.write_clock, m.read_clock, m.control) for m in records] == [
        ("wr_clk", "rd_clk", ())
    ]
    violations = run_all_rules(module, async_crossings, spec)
    assert [v.rule_id for v in violations] == ["CDC-026"]
