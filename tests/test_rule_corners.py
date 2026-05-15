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
    _is_gated_bus_crossing,  # noqa: PLC2701
    _trace_through_bus_buffers,  # noqa: PLC2701
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


# --- CDC-004 gating-shape extensions (issues #34, #35) ---------------------
#
# Phase 1: pure-helper test for the buffer-walker.
# Phase 2: end-to-end shape-1 ($dffe.EN) and shape-2 (mux-on-D through a
# buffer chain) tests, plus a budget-exceeded guard that confirms a 3-hop
# chain still trips CDC-004.


def _build_buffered_mux_to_d_module(num_buffers: int) -> Module:
    """Multi-bit gated bus crossing with ``num_buffers`` ``$_BUF_`` cells
    between the gating mux and the dst flop's ``D`` pin.

    Topology (per lane; the module is 2-bit wide)::

        src_q.Q[i] ─┐
                    [$mux Y[i]] ─[$_BUF_]…[$_BUF_]─> dst_q.D[i]
        dst_q.Q[i] ─┘
                       ▲
                       │ S=en_sync.Q (dst-domain control)

    With ``num_buffers <= _GATING_BUF_BUDGET`` the gating detector
    should accept; beyond the budget the trace bails out before
    reaching the mux and CDC-004 must fire.
    """
    src_clk_bit = 2
    dst_clk_bit = 3
    src_d0_bit, src_d1_bit = 4, 5
    src_q0_bit, src_q1_bit = 6, 7
    en_d_bit = 8
    en_q_bit = 9
    mux_y0_bit, mux_y1_bit = 10, 11
    dst_q0_bit, dst_q1_bit = 12, 13

    # Allocate intermediate net IDs for the buffer chain. Each stage
    # is two lanes wide. ``stage_bits[0]`` sits between mux Y and the
    # first buffer; ``stage_bits[k>0]`` sits between buffers k-1 and
    # k. The final buffer writes mux_y* directly into the flop D,
    # which is wired through the last stage's nets.
    nxt = 100
    chain_stages: list[tuple[int, int]] = []
    for _ in range(num_buffers):
        chain_stages.append((nxt, nxt + 1))
        nxt += 2
    # mux's actual Y output: first chain stage's nets if buffered,
    # else the dst flop's D bits directly.
    if num_buffers == 0:
        mux_y = (mux_y0_bit, mux_y1_bit)
        dst_d = (mux_y0_bit, mux_y1_bit)
    else:
        mux_y = chain_stages[0]
        dst_d = (mux_y0_bit, mux_y1_bit)

    ports: dict[str, Port] = {
        "src_clk": Port(name="src_clk", direction="input", bits=(src_clk_bit,)),
        "dst_clk": Port(name="dst_clk", direction="input", bits=(dst_clk_bit,)),
        "src_d": Port(name="src_d", direction="input", bits=(src_d0_bit, src_d1_bit)),
        "en_d": Port(name="en_d", direction="input", bits=(en_d_bit,)),
        "dst_q": Port(name="dst_q", direction="output", bits=(dst_q0_bit, dst_q1_bit)),
    }
    cells: dict[str, Cell] = {
        "src_q": Cell(
            name="src_q",
            type="$dff",
            connections={
                "CLK": (src_clk_bit,),
                "D": (src_d0_bit, src_d1_bit),
                "Q": (src_q0_bit, src_q1_bit),
            },
        ),
        "en_sync": Cell(
            name="en_sync",
            type="$dff",
            connections={
                "CLK": (dst_clk_bit,),
                "D": (en_d_bit,),
                "Q": (en_q_bit,),
            },
        ),
        "load_mux": Cell(
            name="load_mux",
            type="$mux",
            connections={
                "A": (dst_q0_bit, dst_q1_bit),  # hold (dst flop's Q)
                "B": (src_q0_bit, src_q1_bit),  # load (src flop's Q)
                "S": (en_q_bit,),
                "Y": mux_y,
            },
        ),
        "dst_q": Cell(
            name="dst_q",
            type="$dff",
            connections={
                "CLK": (dst_clk_bit,),
                "D": dst_d,
                "Q": (dst_q0_bit, dst_q1_bit),
            },
        ),
    }
    # Chain the buffers. Buffer k reads stage k, writes stage k+1;
    # the last buffer writes ``mux_y0_bit/mux_y1_bit`` (the dst flop's
    # actual D nets). Single-bit $_BUF_, two per stage (one per lane).
    for k in range(num_buffers):
        src_vec = chain_stages[k]
        if k == num_buffers - 1:
            dst_vec = (mux_y0_bit, mux_y1_bit)
        else:
            dst_vec = chain_stages[k + 1]
        for lane in range(2):
            cells[f"buf_h{k}_b{lane}"] = Cell(
                name=f"buf_h{k}_b{lane}",
                type="$_BUF_",
                connections={
                    "A": (src_vec[lane],),
                    "Y": (dst_vec[lane],),
                },
            )
    return Module(name="buffered_mux_to_d", ports=ports, cells=cells, netnames={})


_TWO_CLOCK_SDC = """
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
"""


def _async_crossings(module: Module, spec):
    crossings = find_crossings(module)
    return [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]


def test_trace_through_bus_buffers_single_hop() -> None:
    """One ``$_BUF_`` between mux and D: the trace should land on the
    mux output, not on the buffer cell."""
    module = _build_buffered_mux_to_d_module(num_buffers=1)
    ctx = _build_context(module, clock_spec=None)
    # dst_q.D bits are 10 / 11; trace each backward through the buffer.
    drv0 = _trace_through_bus_buffers(module, 10, ctx.bit_drivers)
    drv1 = _trace_through_bus_buffers(module, 11, ctx.bit_drivers)
    assert drv0 is not None and drv0[0] == "load_mux"
    assert drv1 is not None and drv1[0] == "load_mux"


def test_trace_through_bus_buffers_budget_exceeded() -> None:
    """A 3-hop chain (budget=2) must stop at the last buffer rather
    than returning the originating mux. The caller's cell-type check
    will then see a ``$_BUF_`` and reject."""
    module = _build_buffered_mux_to_d_module(num_buffers=3)
    ctx = _build_context(module, clock_spec=None)
    drv = _trace_through_bus_buffers(module, 10, ctx.bit_drivers)
    assert drv is not None
    assert module.cells[drv[0]].type == "$_BUF_"


def test_cdc_004_silent_with_one_buffer_between_mux_and_d() -> None:
    """End-to-end: a single buffer between the gating mux and the
    dst flop's D pin keeps the gating-shape detector happy. Matches
    the ``good_buffered_gated_bus_crossing`` fixture's shape but is
    fully self-contained."""
    module = _build_buffered_mux_to_d_module(num_buffers=1)
    spec = parse_sdc(_TWO_CLOCK_SDC)
    crossings = _async_crossings(module, spec)
    # One bus crossing src_clk → dst_clk into dst_q.
    assert len(crossings) >= 1
    violations = run_all(module, crossings, spec)
    cdc_004 = [v for v in violations if v.rule_id == "CDC-004"]
    assert cdc_004 == []


def test_cdc_004_fires_with_three_buffers_between_mux_and_d() -> None:
    """Budget-exceeded regression guard for issue #35: 3 buffers
    between the mux and D are one too many. The trace bails out
    before reaching the mux, the gating shape fails to match, and
    CDC-004 fires on the bus crossing."""
    module = _build_buffered_mux_to_d_module(num_buffers=3)
    spec = parse_sdc(_TWO_CLOCK_SDC)
    crossings = _async_crossings(module, spec)
    violations = run_all(module, crossings, spec)
    cdc_004 = [v for v in violations if v.rule_id == "CDC-004"]
    assert len(cdc_004) == 1
    assert "unprotected bus crossing" in cdc_004[0].message


def _build_dffe_gated_module(en_clock_bit: int) -> Module:
    """Multi-bit bus crossing into a ``$dffe`` whose EN comes from a
    flop on ``en_clock_bit``. When ``en_clock_bit`` is the dst clock,
    the EN-gating shape should accept; when it's the src clock, it
    must reject (the gate is itself cross-domain)."""
    src_clk_bit = 2
    dst_clk_bit = 3
    src_d0, src_d1 = 4, 5
    src_q0, src_q1 = 6, 7
    en_d = 8
    en_q = 9
    dst_q0, dst_q1 = 10, 11

    ports: dict[str, Port] = {
        "src_clk": Port(name="src_clk", direction="input", bits=(src_clk_bit,)),
        "dst_clk": Port(name="dst_clk", direction="input", bits=(dst_clk_bit,)),
        "src_d": Port(name="src_d", direction="input", bits=(src_d0, src_d1)),
        "en_d": Port(name="en_d", direction="input", bits=(en_d,)),
        "dst_q": Port(name="dst_q", direction="output", bits=(dst_q0, dst_q1)),
    }
    cells: dict[str, Cell] = {
        "src_q": Cell(
            name="src_q",
            type="$dff",
            connections={
                "CLK": (src_clk_bit,),
                "D": (src_d0, src_d1),
                "Q": (src_q0, src_q1),
            },
        ),
        "en_q": Cell(
            name="en_q",
            type="$dff",
            connections={
                "CLK": (en_clock_bit,),
                "D": (en_d,),
                "Q": (en_q,),
            },
        ),
        "dst_q": Cell(
            name="dst_q",
            type="$dffe",
            connections={
                "CLK": (dst_clk_bit,),
                "EN": (en_q,),
                "D": (src_q0, src_q1),
                "Q": (dst_q0, dst_q1),
            },
        ),
    }
    return Module(name="dffe_gated", ports=ports, cells=cells, netnames={})


def test_cdc_004_silent_with_dffe_en_in_dst_domain() -> None:
    """Shape 1: ``$dffe`` destination whose EN fanin is all dst-domain.
    Matches the textbook handshake/load-enable idiom; CDC-004 must
    stay silent."""
    module = _build_dffe_gated_module(en_clock_bit=3)  # dst_clk
    spec = parse_sdc(_TWO_CLOCK_SDC)
    crossings = _async_crossings(module, spec)
    violations = run_all(module, crossings, spec)
    cdc_004 = [v for v in violations if v.rule_id == "CDC-004"]
    assert cdc_004 == []


def test_cdc_004_fires_when_dffe_en_is_src_domain() -> None:
    """Negative shape-1 case: ``$dffe`` destination whose EN comes
    from a src-domain flop. The "gate" is itself a cross-domain
    signal — the rule must still fire, otherwise we'd accept any
    ``$dffe`` as automatically gated."""
    module = _build_dffe_gated_module(en_clock_bit=2)  # src_clk
    spec = parse_sdc(_TWO_CLOCK_SDC)
    crossings = _async_crossings(module, spec)
    # Sanity: there must be at least the bus crossing visible.
    bus_crossings = [c for c in crossings if c.width >= 2]
    assert bus_crossings
    violations = run_all(module, crossings, spec)
    cdc_004 = [v for v in violations if v.rule_id == "CDC-004"]
    assert len(cdc_004) >= 1


def test_is_gated_bus_crossing_rejects_dffe_without_en() -> None:
    """If a flop happens to be ``$dffe`` typed but has no ``EN``
    connection (or EN is constant), shape 1 must not accept it on
    the basis of the cell type alone. Falling back to shape 2 is
    still allowed."""
    module = _build_dffe_gated_module(en_clock_bit=3)
    # Strip the EN connection. Frozen dataclass — rebuild the cell.
    old = module.cells["dst_q"]
    new_conns = {k: v for k, v in old.connections.items() if k != "EN"}
    module.cells["dst_q"] = Cell(
        name=old.name,
        type=old.type,
        connections=new_conns,
        parameters=old.parameters,
        attributes=old.attributes,
    )
    spec = parse_sdc(_TWO_CLOCK_SDC)
    crossings = _async_crossings(module, spec)
    ctx = _build_context(module, clock_spec=spec)
    bus_crossings = [c for c in crossings if c.width >= 2]
    assert bus_crossings
    # The shape-1 detector should now reject; with no mux on D
    # shape-2 also fails, so the overall result is "not gated".
    assert not _is_gated_bus_crossing(
        module, bus_crossings[0], ctx.domains, ctx.bit_drivers
    )


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
