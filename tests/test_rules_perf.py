"""Micro-benchmark for the rule pack on a synthetic large module.

Guards against regressions in the structural-context memoisation
introduced for issue #12. Before that change ``assign_domains`` was
called 7× per ``run_all`` invocation and ``_sync_chain_depth`` re-scanned
every flop per chain-extension step. On a tiny IP-block-sized fixture
those redundancies are invisible; the moment a design grows past a few
hundred flops they dominate, so a synthetic test is the only way to
catch a quadratic regression early.

The module is built directly as :class:`netlist.Module` dataclasses —
no Yosys / pyslang dependency — so this test runs everywhere the
suite does. Time budgets are intentionally generous; the goal is to
catch order-of-magnitude regressions, not jitter.
"""

from __future__ import annotations

import time

from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.netlist import Cell, Module, Port
from rtl_buddy_cdc.rules import run_all
from rtl_buddy_cdc.sdc import parse as parse_sdc


def _build_synthetic_module(num_pairs: int = 250) -> Module:
    """Build a flat module with ``num_pairs`` independent src→dst flop
    pairs across two clock domains. Each pair is a CDC-001 violation
    (no second-stage synchronizer), so the rule pack actually walks the
    full crossing list rather than short-circuiting on an empty input.

    Bit layout (ints; "0"/"1" reserved):
      2          → src_clk port bit
      3          → dst_clk port bit
      4..4+N-1   → src flop D bits (also wired in from src_data port)
      4+N..      → src flop Q bits = dst flop D bits (direct crossing)
      …          → dst flop Q bits
    """
    next_bit = 2
    src_clk_bit = next_bit
    next_bit += 1
    dst_clk_bit = next_bit
    next_bit += 1

    src_d_bits = list(range(next_bit, next_bit + num_pairs))
    next_bit += num_pairs
    crossing_bits = list(range(next_bit, next_bit + num_pairs))
    next_bit += num_pairs
    dst_q_bits = list(range(next_bit, next_bit + num_pairs))
    next_bit += num_pairs

    ports: dict[str, Port] = {
        "src_clk": Port(name="src_clk", direction="input", bits=(src_clk_bit,)),
        "dst_clk": Port(name="dst_clk", direction="input", bits=(dst_clk_bit,)),
        "src_data": Port(name="src_data", direction="input", bits=tuple(src_d_bits)),
        "dst_data": Port(name="dst_data", direction="output", bits=tuple(dst_q_bits)),
    }

    cells: dict[str, Cell] = {}
    for i in range(num_pairs):
        src_name = f"src_ff_{i}"
        dst_name = f"dst_ff_{i}"
        cells[src_name] = Cell(
            name=src_name,
            type="$dff",
            connections={
                "CLK": (src_clk_bit,),
                "D": (src_d_bits[i],),
                "Q": (crossing_bits[i],),
            },
        )
        cells[dst_name] = Cell(
            name=dst_name,
            type="$dff",
            connections={
                "CLK": (dst_clk_bit,),
                "D": (crossing_bits[i],),
                "Q": (dst_q_bits[i],),
            },
        )

    return Module(name="synthetic_perf", ports=ports, cells=cells, netnames={})


_PERF_SDC = """
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
"""


def test_run_all_scales_to_500_flops_under_one_second() -> None:
    """500-flop / 250-crossing module must run through ``run_all`` in
    well under a second. Before the #12 memoisation refactor this test
    would run in ~2-3 s (depending on host) because of the 7× rebuild
    of ``assign_domains`` and the per-step ``find_flops`` scan inside
    ``_sync_chain_depth``.

    1.0 s is a comfortable ceiling — it lets the test stay in CI's
    fast-suite budget while catching order-of-magnitude regressions.
    """
    module = _build_synthetic_module(num_pairs=250)
    spec = parse_sdc(_PERF_SDC)
    crossings = find_crossings(module)

    start = time.perf_counter()
    violations = run_all(module, crossings, spec)
    elapsed = time.perf_counter() - start

    # Every pair is an unsynchronized direct flop→flop crossing → CDC-001.
    assert len(violations) == 250, (
        f"expected 250 CDC-001 violations, got {len(violations)} "
        f"({violations[0].rule_id if violations else 'none'})"
    )
    assert all(v.rule_id == "CDC-001" for v in violations)
    assert elapsed < 1.0, (
        f"run_all took {elapsed:.3f}s on a 500-flop synthetic module — "
        "regression vs the #12 memoisation refactor?"
    )
