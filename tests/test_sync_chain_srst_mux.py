"""Unit tests for the separate-flop synchronous-reset mux step-over
(issue #304, the follow-up to #301's packed form).

``if (rst) s2 <= 1'b0; else s2 <= s1;`` lowers, on the yosys frontend,
to ``s1.Q → $mux → s2.D`` — the reset mux is left un-folded after
``proc``. Synthetic ``Flop``/``Cell`` graphs drive the chain walkers
and the inter-stage-comb detector directly so each accept/reject
branch is pinned without a Yosys fixture per case.

Bit numbering: flop ``s<i>`` has ``Q`` bit ``10 + i`` and, when it sits
behind a reset mux, ``D`` bit ``20 + i`` (the mux ``Y``). Bit ``1`` is
the clock, ``5`` the crossing source, ``99`` the reset.
"""

from __future__ import annotations

from rtl_buddy_cdc.domain import Crossing
from rtl_buddy_cdc.flops import Flop
from rtl_buddy_cdc.netlist import Bit, Cell, Module, Port
from rtl_buddy_cdc.rules import (
    _bit_drivers,
    _bit_reader_count,
    _build_context,
    _chain_has_inter_stage_comb,
    _srst_mux_data_bit_to_single_bit_flop,
    _sync_chain_depth,
    _sync_chain_flops,
    check_cdc_001,
    check_cdc_014,
)

CLK: Bit = 1
SRC: Bit = 5
RST: Bit = 99


def _flop(name: str, d: Bit, q: Bit, clk: Bit = CLK) -> Flop:
    cell = Cell(
        name=name, type="$dff", connections={"CLK": (clk,), "D": (d,), "Q": (q,)}
    )
    return Flop(cell=cell, clk=clk, d=(d,), q=(q,))


def _mux(
    name: str,
    a: Bit,
    b: Bit,
    y: Bit,
    s: Bit = RST,
    cell_type: str = "$mux",
) -> Cell:
    return Cell(
        name=name,
        type=cell_type,
        connections={"A": (a,), "B": (b,), "S": (s,), "Y": (y,)},
    )


def _and(name: str, a: Bit, b: Bit, y: Bit) -> Cell:
    return Cell(name=name, type="$and", connections={"A": (a,), "B": (b,), "Y": (y,)})


def _module(
    *flops: Flop, cells: tuple[Cell, ...] = (), clocks: tuple[Bit, ...] = (CLK,)
) -> Module:
    every: dict[str, Cell] = {f.cell.name: f.cell for f in flops}
    every.update({c.name: c for c in cells})
    ports = {
        f"clk{i}": Port(name=f"clk{i}", direction="input", bits=(b,))
        for i, b in enumerate(clocks)
    }
    ports["rst"] = Port(name="rst", direction="input", bits=(RST,))
    ports["src"] = Port(name="src", direction="input", bits=(SRC,))
    return Module(name="m", ports=ports, cells=every, netnames={})


def _domains(*flops: Flop, clock: str = "clk0") -> dict[str, str | None]:
    return {f.cell.name: clock for f in flops}


def _two_stage_srst(b_const: bool = True) -> tuple[Flop, Flop, Cell, Module]:
    """``s1 → rst_mux → s2`` with the constant on ``B`` (active-high
    reset, data on ``A``) or on ``A`` (active-low, data on ``B``)."""
    s1 = _flop("s1", d=SRC, q=11)
    s2 = _flop("s2", d=22, q=12)
    mux = (
        _mux("rst_mux", a=11, b="0", y=22)
        if b_const
        else _mux("rst_mux", a="1", b=11, y=22)
    )
    return s1, s2, mux, _module(s1, s2, cells=(mux,))


# --- the index ---------------------------------------------------------------


def test_index_maps_data_leg_bit_to_the_flop_behind_the_reset_mux() -> None:
    s1, s2, _mux_cell, module = _two_stage_srst()
    d_map = {SRC: s1, 22: s2}
    idx = _srst_mux_data_bit_to_single_bit_flop(
        module, d_map, _bit_drivers(module), _bit_reader_count(module)
    )
    assert idx == {11: s2}


def test_index_covers_both_reset_polarities() -> None:
    for b_const in (True, False):
        s1, s2, _mux_cell, module = _two_stage_srst(b_const)
        idx = _srst_mux_data_bit_to_single_bit_flop(
            module, {SRC: s1, 22: s2}, _bit_drivers(module), _bit_reader_count(module)
        )
        assert idx == {11: s2}, b_const


def test_index_skips_an_enable_mux() -> None:
    """``if (en) s2 <= s1;`` — the other leg is ``s2``'s own ``Q``, a
    live bit, not a constant: not a reset, not indexed."""
    s1 = _flop("s1", d=SRC, q=11)
    s2 = _flop("s2", d=22, q=12)
    en_mux = _mux("en_mux", a=12, b=11, y=22)
    module = _module(s1, s2, cells=(en_mux,))
    idx = _srst_mux_data_bit_to_single_bit_flop(
        module, {SRC: s1, 22: s2}, _bit_drivers(module), _bit_reader_count(module)
    )
    assert idx == {}


def test_index_skips_a_mux_with_a_live_other_leg() -> None:
    s1 = _flop("s1", d=SRC, q=11)
    s2 = _flop("s2", d=22, q=12)
    other = _flop("other", d=SRC, q=13)
    mux = _mux("sel_mux", a=11, b=13, y=22)
    module = _module(s1, s2, other, cells=(mux,))
    idx = _srst_mux_data_bit_to_single_bit_flop(
        module,
        {SRC: s1, 22: s2, SRC + 100: other},
        _bit_drivers(module),
        _bit_reader_count(module),
    )
    assert idx == {}


def test_index_skips_a_constant_data_leg() -> None:
    """Both legs constant: the flop never takes data — nothing to walk to."""
    s2 = _flop("s2", d=22, q=12)
    mux = _mux("rst_mux", a="1", b="0", y=22)
    module = _module(s2, cells=(mux,))
    idx = _srst_mux_data_bit_to_single_bit_flop(
        module, {22: s2}, _bit_drivers(module), _bit_reader_count(module)
    )
    assert idx == {}


def test_index_skips_a_mux_output_with_an_extra_reader() -> None:
    """The mux ``Y`` also feeds a gate: the pre-flop value escapes the
    chain, so the mux is not treated as the flop's own reset."""
    s1, s2, mux, _module_ = _two_stage_srst()
    leak = _and("leak", a=22, b=RST, y=30)
    module = _module(s1, s2, cells=(mux, leak))
    idx = _srst_mux_data_bit_to_single_bit_flop(
        module, {SRC: s1, 22: s2}, _bit_drivers(module), _bit_reader_count(module)
    )
    assert idx == {}


def test_index_keeps_first_flop_when_two_muxes_share_a_data_bit() -> None:
    """Deterministic on a shared data bit; the walkers reject it anyway
    because the bit then has two readers."""
    s1 = _flop("s1", d=SRC, q=11)
    s2 = _flop("s2", d=22, q=12)
    s3 = _flop("s3", d=23, q=13)
    m2 = _mux("m2", a=11, b="0", y=22)
    m3 = _mux("m3", a=11, b="0", y=23)
    module = _module(s1, s2, s3, cells=(m2, m3))
    idx = _srst_mux_data_bit_to_single_bit_flop(
        module,
        {SRC: s1, 22: s2, 23: s3},
        _bit_drivers(module),
        _bit_reader_count(module),
    )
    assert idx == {11: s2}
    assert _sync_chain_depth(module, s1, "clk0", _domains(s1, s2, s3)) == 1


# --- the chain walkers -------------------------------------------------------


def test_sync_chain_depth_steps_over_the_reset_mux() -> None:
    for b_const in (True, False):
        s1, s2, _mux_cell, module = _two_stage_srst(b_const)
        assert _sync_chain_depth(module, s1, "clk0", _domains(s1, s2)) == 2, b_const


def test_sync_chain_depth_uses_the_supplied_index() -> None:
    """The lazy path and the context path agree."""
    s1, s2, _mux_cell, module = _two_stage_srst()
    ctx = _build_context(module, None)
    assert ctx.srst_mux_data_bit_to_single_bit_flop == {11: s2}
    depth = _sync_chain_depth(
        module,
        s1,
        "clk0",
        _domains(s1, s2),
        ctx.reader_counts,
        d_bit_to_single_bit_flop=ctx.d_bit_to_single_bit_flop,
        bit_drivers=ctx.bit_drivers,
        srst_mux_data_bit_to_single_bit_flop=ctx.srst_mux_data_bit_to_single_bit_flop,
    )
    assert depth == 2


def test_sync_chain_depth_three_stages_each_behind_a_reset_mux() -> None:
    s1 = _flop("s1", d=21, q=11)
    s2 = _flop("s2", d=22, q=12)
    s3 = _flop("s3", d=23, q=13)
    cells = (
        _mux("m1", a=SRC, b="0", y=21),
        _mux("m2", a=12 - 1, b="0", y=22),
        _mux("m3", a=12, b="0", y=23),
    )
    module = _module(s1, s2, s3, cells=cells)
    assert _sync_chain_depth(module, s1, "clk0", _domains(s1, s2, s3)) == 3
    assert _sync_chain_flops(
        module,
        s1,
        "clk0",
        _domains(s1, s2, s3),
        _bit_reader_count(module),
        {21: s1, 22: s2, 23: s3},
    ) == (s1, s2, s3)


def test_sync_chain_depth_mixed_direct_and_reset_mux_hops() -> None:
    """Stage 2 has a sync reset, stage 3 does not (``s2.Q`` → ``s3.D``)."""
    s1 = _flop("s1", d=SRC, q=11)
    s2 = _flop("s2", d=22, q=12)
    s3 = _flop("s3", d=12, q=13)
    module = _module(s1, s2, s3, cells=(_mux("m2", a=11, b="0", y=22),))
    assert _sync_chain_depth(module, s1, "clk0", _domains(s1, s2, s3)) == 3


def test_sync_chain_depth_stops_at_an_enable_mux() -> None:
    s1 = _flop("s1", d=SRC, q=11)
    s2 = _flop("s2", d=22, q=12)
    module = _module(s1, s2, cells=(_mux("en_mux", a=12, b=11, y=22),))
    assert _sync_chain_depth(module, s1, "clk0", _domains(s1, s2)) == 1


def test_sync_chain_depth_stops_at_a_foreign_domain_stage() -> None:
    s1, s2, _mux_cell, module = _two_stage_srst()
    domains: dict[str, str | None] = {"s1": "clk0", "s2": "other_clk"}
    assert _sync_chain_depth(module, s1, "clk0", domains) == 1


def test_sync_chain_depth_stops_when_head_q_has_an_extra_reader() -> None:
    s1, s2, mux, _module_ = _two_stage_srst()
    tap = _and("tap", a=11, b=RST, y=30)
    module = _module(s1, s2, cells=(mux, tap))
    assert _sync_chain_depth(module, s1, "clk0", _domains(s1, s2)) == 1


def test_sync_chain_flops_steps_over_the_reset_mux_lazily_and_with_index() -> None:
    s1, s2, _mux_cell, module = _two_stage_srst()
    counts = _bit_reader_count(module)
    d_map = {SRC: s1, 22: s2}
    assert _sync_chain_flops(module, s1, "clk0", _domains(s1, s2), counts, d_map) == (
        s1,
        s2,
    )
    assert _sync_chain_flops(
        module, s1, "clk0", _domains(s1, s2), counts, d_map, {11: s2}
    ) == (s1, s2)


def test_pmux_is_not_stepped_over() -> None:
    s1 = _flop("s1", d=SRC, q=11)
    s2 = _flop("s2", d=22, q=12)
    module = _module(s1, s2, cells=(_mux("pm", a=11, b="0", y=22, cell_type="$pmux"),))
    assert _sync_chain_depth(module, s1, "clk0", _domains(s1, s2)) == 1


# --- the inter-stage-comb detector (CDC-014 / CDC-001 deferral) -------------


def _crossing(head: Flop) -> Crossing:
    return Crossing(
        src_clock="src_clk",
        dst_flop=head,
        dst_clock="clk0",
        min_hops=0,
        width=1,
        src_flop=None,
    )


def test_inter_stage_comb_ignores_the_reset_mux() -> None:
    for b_const in (True, False):
        s1, _s2, _mux_cell, module = _two_stage_srst(b_const)
        ctx = _build_context(module, None)
        assert ctx.domains["s1"] == ctx.domains["s2"] is not None
        assert _chain_has_inter_stage_comb(s1, ctx) is None, b_const


def test_inter_stage_comb_still_sees_a_gate() -> None:
    s1 = _flop("s1", d=SRC, q=11)
    s2 = _flop("s2", d=22, q=12)
    module = _module(s1, s2, cells=(_and("g", a=11, b=RST, y=22),))
    ctx = _build_context(module, None)
    assert _chain_has_inter_stage_comb(s1, ctx) == s2


def test_inter_stage_comb_still_sees_an_enable_mux() -> None:
    s1 = _flop("s1", d=SRC, q=11)
    s2 = _flop("s2", d=22, q=12)
    module = _module(s1, s2, cells=(_mux("en_mux", a=12, b=11, y=22),))
    ctx = _build_context(module, None)
    assert _chain_has_inter_stage_comb(s1, ctx) == s2


def test_cdc_014_and_cdc_001_silent_on_reset_mux_between_stages() -> None:
    s1, _s2, _mux_cell, module = _two_stage_srst()
    ctx = _build_context(module, None)
    crossings = [_crossing(s1)]
    assert check_cdc_014(module, crossings, None, ctx=ctx) == []
    assert check_cdc_001(module, crossings, None, ctx=ctx) == []


def test_cdc_014_still_fires_on_enable_mux_between_stages() -> None:
    s1 = _flop("s1", d=SRC, q=11)
    s2 = _flop("s2", d=22, q=12)
    module = _module(s1, s2, cells=(_mux("en_mux", a=12, b=11, y=22),))
    ctx = _build_context(module, None)
    crossings = [_crossing(s1)]
    assert [v.rule_id for v in check_cdc_014(module, crossings, None, ctx=ctx)] == [
        "CDC-014"
    ]
    assert check_cdc_001(module, crossings, None, ctx=ctx) == []
