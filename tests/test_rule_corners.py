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
from rtl_buddy_cdc.rules import (
    _build_context,  # noqa: PLC2701
    _forward_reachable_cells,  # noqa: PLC2701
    _forward_reachable_flops,  # noqa: PLC2701
    run_all,
)
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


# --- _forward_reachable_flops (issue #32) -----------------------------------
#
# Phase 1: pure-helper unit tests. The two acceptance-criteria
# scenarios — two source flops whose Qs share a downstream cone, and
# two source flops with disjoint downstream cones — exercise the
# intersection-vs-disjoint distinction that CDC-005's phase-2 filter
# will key off. Hand-built ``Module``s avoid a Yosys / SV dependency.


def _build_shared_downstream_cone_module() -> Module:
    """Two source flops whose Qs feed an ``$or`` whose Y bit drives a
    third (downstream) flop.

        src_a.Q ─┐
                 ├─[$or]──> downstream_ff.D ──> downstream_ff.Q
        src_b.Q ─┘

    Forward-reachability from EITHER source's Q must include
    ``downstream_ff`` — that's the reconvergence point.
    """
    clk_bit = 2
    src_a_d_bit = 3
    src_a_q_bit = 4
    src_b_d_bit = 5
    src_b_q_bit = 6
    or_y_bit = 7
    dst_q_bit = 8

    ports: dict[str, Port] = {
        "clk": Port(name="clk", direction="input", bits=(clk_bit,)),
        "src_a_d": Port(name="src_a_d", direction="input", bits=(src_a_d_bit,)),
        "src_b_d": Port(name="src_b_d", direction="input", bits=(src_b_d_bit,)),
        "dst_q": Port(name="dst_q", direction="output", bits=(dst_q_bit,)),
    }
    cells: dict[str, Cell] = {
        "src_a": Cell(
            name="src_a",
            type="$dff",
            connections={
                "CLK": (clk_bit,),
                "D": (src_a_d_bit,),
                "Q": (src_a_q_bit,),
            },
        ),
        "src_b": Cell(
            name="src_b",
            type="$dff",
            connections={
                "CLK": (clk_bit,),
                "D": (src_b_d_bit,),
                "Q": (src_b_q_bit,),
            },
        ),
        "or_gate": Cell(
            name="or_gate",
            type="$or",
            connections={"A": (src_a_q_bit,), "B": (src_b_q_bit,), "Y": (or_y_bit,)},
        ),
        "downstream_ff": Cell(
            name="downstream_ff",
            type="$dff",
            connections={
                "CLK": (clk_bit,),
                "D": (or_y_bit,),
                "Q": (dst_q_bit,),
            },
        ),
    }
    return Module(name="shared_cone", ports=ports, cells=cells, netnames={})


def _build_disjoint_cones_module() -> Module:
    """Two source flops whose Qs feed unrelated downstream flops; no
    shared cone.

        src_a.Q ──[$buf]──> ff_a.D ──> ff_a.Q
        src_b.Q ──[$buf]──> ff_b.D ──> ff_b.Q
    """
    clk_bit = 2
    src_a_d_bit = 3
    src_a_q_bit = 4
    src_b_d_bit = 5
    src_b_q_bit = 6
    a_buf_y_bit = 7
    b_buf_y_bit = 8
    ff_a_q_bit = 9
    ff_b_q_bit = 10

    ports: dict[str, Port] = {
        "clk": Port(name="clk", direction="input", bits=(clk_bit,)),
        "src_a_d": Port(name="src_a_d", direction="input", bits=(src_a_d_bit,)),
        "src_b_d": Port(name="src_b_d", direction="input", bits=(src_b_d_bit,)),
        "ff_a_q": Port(name="ff_a_q", direction="output", bits=(ff_a_q_bit,)),
        "ff_b_q": Port(name="ff_b_q", direction="output", bits=(ff_b_q_bit,)),
    }
    cells: dict[str, Cell] = {
        "src_a": Cell(
            name="src_a",
            type="$dff",
            connections={
                "CLK": (clk_bit,),
                "D": (src_a_d_bit,),
                "Q": (src_a_q_bit,),
            },
        ),
        "src_b": Cell(
            name="src_b",
            type="$dff",
            connections={
                "CLK": (clk_bit,),
                "D": (src_b_d_bit,),
                "Q": (src_b_q_bit,),
            },
        ),
        "buf_a": Cell(
            name="buf_a",
            type="$buf",
            connections={"A": (src_a_q_bit,), "Y": (a_buf_y_bit,)},
        ),
        "buf_b": Cell(
            name="buf_b",
            type="$buf",
            connections={"A": (src_b_q_bit,), "Y": (b_buf_y_bit,)},
        ),
        "ff_a": Cell(
            name="ff_a",
            type="$dff",
            connections={
                "CLK": (clk_bit,),
                "D": (a_buf_y_bit,),
                "Q": (ff_a_q_bit,),
            },
        ),
        "ff_b": Cell(
            name="ff_b",
            type="$dff",
            connections={
                "CLK": (clk_bit,),
                "D": (b_buf_y_bit,),
                "Q": (ff_b_q_bit,),
            },
        ),
    }
    return Module(name="disjoint_cones", ports=ports, cells=cells, netnames={})


def test_forward_reachable_flops_shared_cone() -> None:
    """Both source flops' forward cones include the downstream flop
    they jointly feed via the ``$or`` — the intersection is non-empty.
    This is the shape CDC-005's phase-2 filter classifies as
    "truly reconvergent"."""
    module = _build_shared_downstream_cone_module()
    ctx = _build_context(module, clock_spec=None)
    a_reach = _forward_reachable_flops(
        module,
        start_bits=(4,),  # src_a.Q
        consumers=ctx.bit_consumers,
    )
    b_reach = _forward_reachable_flops(
        module,
        start_bits=(6,),  # src_b.Q
        consumers=ctx.bit_consumers,
    )
    assert "downstream_ff" in a_reach
    assert "downstream_ff" in b_reach
    assert a_reach & b_reach == {"downstream_ff"}


def test_forward_reachable_flops_disjoint_cones() -> None:
    """Source flops with no shared downstream cell produce disjoint
    reachable sets. Phase-2's filter will skip the CDC-005 violation
    on a group whose pairs all look like this."""
    module = _build_disjoint_cones_module()
    ctx = _build_context(module, clock_spec=None)
    a_reach = _forward_reachable_flops(
        module, start_bits=(4,), consumers=ctx.bit_consumers
    )
    b_reach = _forward_reachable_flops(
        module, start_bits=(6,), consumers=ctx.bit_consumers
    )
    assert a_reach == {"ff_a"}
    assert b_reach == {"ff_b"}
    assert a_reach & b_reach == set()


def test_forward_reachable_flops_stops_at_d_pin() -> None:
    """The walk never crosses a flop boundary — once a ``D`` pin is
    reached, the destination flop is recorded but its ``Q`` is NOT
    walked further. Without this, a long pipeline would have every
    downstream flop in the reachable set, defeating the filter."""
    # Reuse the disjoint module: src_a's Q reaches ff_a via buf_a. If
    # the walk crossed ff_a, it would continue down ff_a.Q to ff_a_q
    # port — but there's nothing past ff_a (and even if there were,
    # we wouldn't want to include it).
    module = _build_disjoint_cones_module()
    ctx = _build_context(module, clock_spec=None)
    reached = _forward_reachable_flops(
        module, start_bits=(4,), consumers=ctx.bit_consumers
    )
    # Exactly the single downstream flop, never more.
    assert reached == {"ff_a"}


def test_forward_reachable_cells_includes_comb_recombination() -> None:
    """The cells variant records EVERY cell whose input is on the cone
    — flops via D, comb cells via any input. CDC-005's filter uses it
    so an unregistered comb-cell recombination (an ``$and`` directly
    driving an output port) still counts as reconvergence."""
    module = _build_shared_downstream_cone_module()
    ctx = _build_context(module, clock_spec=None)
    a_reach = _forward_reachable_cells(
        module, start_bits=(4,), consumers=ctx.bit_consumers
    )
    b_reach = _forward_reachable_cells(
        module, start_bits=(6,), consumers=ctx.bit_consumers
    )
    # Both chains pass through the ``$or`` and reach the downstream FF.
    assert "or_gate" in a_reach
    assert "or_gate" in b_reach
    assert "downstream_ff" in a_reach
    assert "downstream_ff" in b_reach


def test_forward_reachable_cells_disjoint_when_cones_disjoint() -> None:
    """When the two source flops feed independent comb+flop trees with
    no shared cell, the cells-variant intersection is empty — exactly
    the case CDC-005's phase-2 filter must NOT fire on."""
    module = _build_disjoint_cones_module()
    ctx = _build_context(module, clock_spec=None)
    a_reach = _forward_reachable_cells(
        module, start_bits=(4,), consumers=ctx.bit_consumers
    )
    b_reach = _forward_reachable_cells(
        module, start_bits=(6,), consumers=ctx.bit_consumers
    )
    assert a_reach == {"buf_a", "ff_a"}
    assert b_reach == {"buf_b", "ff_b"}
    assert a_reach & b_reach == set()


def test_forward_reachable_flops_respects_max_depth() -> None:
    """A ``max_depth`` of 0 forbids the helper from walking past the
    initial frontier; any flop reached must be a direct consumer of
    a ``start_bits`` net. Cycle / explosion guard for huge designs."""
    module = _build_shared_downstream_cone_module()
    ctx = _build_context(module, clock_spec=None)
    # max_depth=0: src_a.Q (bit 4) is consumed by ``or_gate`` (a comb
    # cell) — to reach ``downstream_ff`` the walk would have to cross
    # ``or_gate`` (one hop). At depth=0 we refuse to enqueue or_gate's
    # output, so downstream_ff is unreachable.
    reached = _forward_reachable_flops(
        module, start_bits=(4,), consumers=ctx.bit_consumers, max_depth=0
    )
    assert reached == set()
    # max_depth=1 reveals it.
    reached_one_hop = _forward_reachable_flops(
        module, start_bits=(4,), consumers=ctx.bit_consumers, max_depth=1
    )
    assert reached_one_hop == {"downstream_ff"}
