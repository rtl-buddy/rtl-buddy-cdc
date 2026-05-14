"""Targeted regression tests for rule-pack code paths no committed
fixture exercises (issue #14).

The paired bad/good fixtures under ``tests/fixtures/`` give us broad
coverage, but a handful of internal branches don't naturally fall out
of any of them. Rather than build full SystemVerilog + SDC + Yosys-
JSON fixtures for each, we construct the netlist directly as
``netlist.Module`` dataclasses — same approach as
``tests/test_rules_perf.py``. Self-contained, no toolchain dependency.
"""

from __future__ import annotations

from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.netlist import Cell, Module, Port
from rtl_buddy_cdc.rules import run_all
from rtl_buddy_cdc.sdc import parse as parse_sdc


# --- CDC-006 clock-port suppression (issue #14, gap 2) ----------------------


def _build_clock_as_comb_to_sync_module() -> Module:
    """A clock signal reaches a synchronizer's D pin through a single
    combinational cell. Layout:

        src_clk ──[$buf]──> sync_ff_A.D ──> sync_ff_A.Q ──> sync_ff_B.D
        dst_clk ────────────> sync_ff_A.CLK
        dst_clk ────────────> sync_ff_B.CLK
        sync_ff_B.Q ────────> dst_q (output)

    Both sync_ff_* flops are clocked by ``dst_clk`` and form a depth-2
    sync chain. The path from sync_ff_A's D backward through the buffer
    lands on the ``src_clk`` top-level *clock* port — exactly the shape
    that ``rules.check_cdc_006``'s ``if p in clock_ports: continue``
    branch is meant to suppress. CDC-008 separately fires on the
    buffer reading a clock bit on its A pin.
    """
    src_clk_bit = 2
    dst_clk_bit = 3
    mid_bit = 4
    q_a_bit = 5
    output_bit = 6

    ports: dict[str, Port] = {
        "src_clk": Port(name="src_clk", direction="input", bits=(src_clk_bit,)),
        "dst_clk": Port(name="dst_clk", direction="input", bits=(dst_clk_bit,)),
        "dst_q": Port(name="dst_q", direction="output", bits=(output_bit,)),
    }
    cells: dict[str, Cell] = {
        "clk_data_buf": Cell(
            name="clk_data_buf",
            type="$buf",
            connections={"A": (src_clk_bit,), "Y": (mid_bit,)},
        ),
        "sync_ff_A": Cell(
            name="sync_ff_A",
            type="$dff",
            connections={
                "CLK": (dst_clk_bit,),
                "D": (mid_bit,),
                "Q": (q_a_bit,),
            },
        ),
        "sync_ff_B": Cell(
            name="sync_ff_B",
            type="$dff",
            connections={
                "CLK": (dst_clk_bit,),
                "D": (q_a_bit,),
                "Q": (output_bit,),
            },
        ),
    }
    return Module(name="clock_as_comb_to_sync", ports=ports, cells=cells, netnames={})


_CLOCK_AS_DATA_SDC = """
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
"""


def test_cdc_006_suppresses_clock_port_in_fanin() -> None:
    """CDC-006 fans backward from a synchronizer's D pin and reports
    any unregistered top-level port it reaches. The rule deliberately
    *suppresses* ports declared as clocks in the SDC: a clock signal
    arriving on a flop's D pin via comb is a CDC-008 finding ("clock
    used as data"), not a CDC-006 finding ("glitchy comb source").

    Without this branch the analyzer would double-report every clock-
    as-data shape — once correctly as CDC-008 and once incorrectly as
    CDC-006. This test guards the de-duplication.
    """
    module = _build_clock_as_comb_to_sync_module()
    spec = parse_sdc(_CLOCK_AS_DATA_SDC)
    crossings = find_crossings(module)
    violations = run_all(module, crossings, spec)
    rule_ids = sorted({v.rule_id for v in violations})
    assert "CDC-008" in rule_ids, (
        f"expected CDC-008 to fire on the buffer reading src_clk; got rules: {rule_ids}"
    )
    assert "CDC-006" not in rule_ids, (
        "CDC-006 must NOT fire — its fanin reaches only a clock port, "
        "which the rule suppresses to avoid double-reporting with "
        f"CDC-008. Got: {rule_ids}"
    )


# --- find_crossings hop budget (issue #14, gap 3) ---------------------------


def _build_deep_comb_module(num_buffers: int) -> Module:
    """Two flops in different domains connected by ``num_buffers``
    chained ``$buf`` cells. The shortest combinational distance between
    the source flop's Q and the destination flop's D pin equals
    ``num_buffers``; ``find_crossings``'s ``max_hops`` budget must be
    at least that value for the crossing to be discovered.
    """
    src_clk_bit = 2
    dst_clk_bit = 3
    src_d_bit = 4
    src_q_bit = 5
    # One intermediate bit per buffer output.
    buf_out_bits = list(range(6, 6 + num_buffers))
    dst_q_bit = buf_out_bits[-1] + 1

    ports: dict[str, Port] = {
        "src_clk": Port(name="src_clk", direction="input", bits=(src_clk_bit,)),
        "dst_clk": Port(name="dst_clk", direction="input", bits=(dst_clk_bit,)),
        "src_d": Port(name="src_d", direction="input", bits=(src_d_bit,)),
        "dst_q": Port(name="dst_q", direction="output", bits=(dst_q_bit,)),
    }
    cells: dict[str, Cell] = {
        "src_ff": Cell(
            name="src_ff",
            type="$dff",
            connections={
                "CLK": (src_clk_bit,),
                "D": (src_d_bit,),
                "Q": (src_q_bit,),
            },
        ),
    }
    # Chain of buffers: src_q_bit → buf0 → buf1 → ... → buf{N-1} → dst_ff.D
    prev = src_q_bit
    for i, out_bit in enumerate(buf_out_bits):
        cells[f"buf_{i}"] = Cell(
            name=f"buf_{i}",
            type="$buf",
            connections={"A": (prev,), "Y": (out_bit,)},
        )
        prev = out_bit
    cells["dst_ff"] = Cell(
        name="dst_ff",
        type="$dff",
        connections={
            "CLK": (dst_clk_bit,),
            "D": (prev,),
            "Q": (dst_q_bit,),
        },
    )
    return Module(name="deep_comb_chain", ports=ports, cells=cells, netnames={})


def test_find_crossings_hop_budget_drops_paths_beyond_default() -> None:
    """The default ``max_hops=4`` budget caps comb-cone walks. A
    crossing reached only via a 5-buffer chain is correctly classified
    as "no longer directly connecting" — important load-bearing
    contract because we don't want a rule false-fire on a path that
    Yosys' synthesis might restructure beyond recognition.
    """
    module = _build_deep_comb_module(num_buffers=5)
    crossings = find_crossings(module)  # default max_hops=4
    assert crossings == [], (
        "expected zero crossings at default max_hops=4 with a 5-buffer "
        f"chain; got {[(c.src_name, c.dst_flop.name, c.min_hops) for c in crossings]}"
    )


def test_find_crossings_hop_budget_finds_path_when_raised() -> None:
    """Raising ``max_hops`` to ≥5 reveals the same crossing the default
    budget hid. Pins the contract that the budget is the *only* knob
    controlling discovery depth — there's no hidden second cap."""
    module = _build_deep_comb_module(num_buffers=5)
    crossings = find_crossings(module, max_hops=5)
    assert len(crossings) == 1, (
        f"expected exactly one crossing at max_hops=5; got {len(crossings)}"
    )
    c = crossings[0]
    assert c.src_flop is not None and c.src_flop.cell.name == "src_ff"
    assert c.dst_flop.cell.name == "dst_ff"
    assert c.min_hops == 5  # 5 buffers between them


def test_find_crossings_hop_budget_boundary_at_exact_match() -> None:
    """At a chain length exactly equal to ``max_hops``, the crossing is
    detected. Pins the inclusive boundary — easy to flip by an off-by-
    one in the BFS frontier check."""
    module = _build_deep_comb_module(num_buffers=4)
    crossings = find_crossings(module)  # default max_hops=4
    assert len(crossings) == 1
    assert crossings[0].min_hops == 4
