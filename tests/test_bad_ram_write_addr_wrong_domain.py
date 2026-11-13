"""Memory write port fed from the wrong clock domain (#326).

The RAM is written and read on ``wr_clk`` only, so the storage never
crosses a clock and CDC-026 is silent. The write *address* is an
``rd_clk`` counter wired straight into the ``wr_clk`` write port: a write
port is a sink in its write-clock domain, so this is an ordinary
unsynchronised 4-bit crossing into it, and CDC-004 must fire on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import filter_async, find_crossings
from rtl_buddy_cdc.memory import find_memories
from rtl_buddy_cdc.rules import memory_crossings, run_all as run_all_rules

NAME = "bad_ram_write_addr_wrong_domain"
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


def test_crossing_lands_on_the_write_port() -> None:
    module, async_crossings, _ = _load()
    (mem,) = find_memories(module)
    (wport,) = mem.write_ports
    (c,) = async_crossings
    assert c.dst_flop.name == wport.endpoint
    assert (c.src_clock, c.dst_clock, c.width) == ("rd_clk", "wr_clk", 4)


def test_cdc_004_fires_alone() -> None:
    module, async_crossings, spec = _load()
    assert memory_crossings(module, async_crossings, spec) == []
    violations = run_all_rules(module, async_crossings, spec)
    assert [v.rule_id for v in violations] == ["CDC-004"]
    assert violations[0].crossing is not None
    assert violations[0].crossing.dst_flop.cell.type == "$mem_port"
