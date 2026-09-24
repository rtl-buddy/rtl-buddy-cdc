"""Unit tests for ``_packed_shift_register_depth`` (issue #264).

Drives the structural recogniser directly with synthetic ``Flop``s so
each accept/reject branch is pinned without needing a full Yosys
fixture per case.
"""

from __future__ import annotations

from rtl_buddy_cdc.flops import Flop
from rtl_buddy_cdc.netlist import Bit, Cell, Module
from rtl_buddy_cdc.domain import Crossing
from rtl_buddy_cdc.rules import (
    _bit_drivers,
    _build_context,
    _crossing_enters_through_reset_mux_only,
    _constant_leg_reset_mux_data_bits,
    _packed_shift_register_depth,
)


def _flop(d: tuple[Bit, ...], q: tuple[Bit, ...]) -> Flop:
    cell = Cell(name="sr", type="$dff", connections={"D": d, "Q": q})
    return Flop(cell=cell, clk=1, d=d, q=q)


def _mux(
    a: tuple[Bit, ...],
    b: tuple[Bit, ...],
    y: tuple[Bit, ...],
    s: Bit = 99,
    cell_type: str = "$mux",
) -> Cell:
    return Cell(
        name="rst_mux",
        type=cell_type,
        connections={"A": a, "B": b, "S": (s,), "Y": y},
    )


def _module(head: Flop, *cells: Cell) -> Module:
    every = {head.cell.name: head.cell, **{c.name: c for c in cells}}
    return Module(name="m", ports={}, cells=every, netnames={})


def _depth_through(
    head: Flop, reader_counts: dict[Bit, int], *cells: Cell
) -> int | None:
    """Drive the recogniser the way ``_sync_chain_depth`` does: with the
    module and its driver map, so the mux look-through is live."""
    module = _module(head, *cells)
    return _packed_shift_register_depth(
        head, reader_counts, module, _bit_drivers(module)
    )


# --- issue #301: synchronous-reset mux look-through ------------------------
#
# ``if (rst) q <= <const>; else q <= {q[0], d};`` leaves a multi-bit
# ``$mux`` in front of ``D`` after ``proc``: the shift vector on one leg,
# the constant reset value on the other, the reset on ``S``. The flop's
# own bits are ints (10, 11, …); Yosys writes constants as string bits.


def test_reset_mux_data_on_a_leg_is_depth_two() -> None:
    """Active-high reset to all-zeros: Yosys puts the shift vector on
    ``A`` and the constant on ``B`` (``Y = S ? B : A``)."""
    head = _flop(d=(7, 8), q=(10, 11))
    mux = _mux(a=(5, 10), b=("0", "0"), y=(7, 8))
    assert _depth_through(head, {7: 1, 8: 1, 10: 1, 11: 0}, mux) == 2


def test_reset_mux_data_on_b_leg_is_depth_two() -> None:
    """Active-low reset: the legs are swapped, constant on ``A``."""
    head = _flop(d=(7, 8), q=(10, 11))
    mux = _mux(a=("0", "0"), b=(5, 10), y=(7, 8))
    assert _depth_through(head, {7: 1, 8: 1, 10: 1, 11: 0}, mux) == 2


def test_reset_mux_all_ones_constant_is_depth_two() -> None:
    """The reset value is irrelevant — all-ones resets the same way."""
    head = _flop(d=(7, 8), q=(10, 11))
    mux = _mux(a=(5, 10), b=("1", "1"), y=(7, 8))
    assert _depth_through(head, {7: 1, 8: 1, 10: 1, 11: 0}, mux) == 2


def test_reset_mux_mixed_and_x_constant_leg_is_depth_two() -> None:
    """``x`` / ``z`` lanes count as constants: a don't-care reset value
    still moves no data between lanes. A mixed ``{1'b1, 1'bx}`` reset
    value is the same story."""
    head = _flop(d=(7, 8), q=(10, 11))
    mux = _mux(a=(5, 10), b=("x", "1"), y=(7, 8))
    assert _depth_through(head, {7: 1, 8: 1, 10: 1, 11: 0}, mux) == 2


def test_three_stage_packed_shift_behind_reset_mux() -> None:
    head = _flop(d=(7, 8, 9), q=(10, 11, 12))
    mux = _mux(a=(5, 10, 11), b=("0", "0", "0"), y=(7, 8, 9))
    counts = {7: 1, 8: 1, 9: 1, 10: 1, 11: 1, 12: 0}
    assert _depth_through(head, counts, mux) == 3


def test_enable_mux_with_own_q_leg_still_rejected() -> None:
    """A load-enable mux holds the register by feeding its own ``Q``
    back on the other leg. Those are ``int`` bits, not constants, so the
    look-through declines and the depth-1 verdict of #264 stands."""
    head = _flop(d=(7, 8), q=(10, 11))
    mux = _mux(a=(5, 10), b=(10, 11), y=(7, 8))
    assert _depth_through(head, {7: 1, 8: 1, 10: 2, 11: 1}, mux) is None


def test_mux_with_live_non_constant_other_leg_rejected() -> None:
    """Anything live on the other leg (a second data source) is not a
    reset and stays rejected."""
    head = _flop(d=(7, 8), q=(10, 11))
    mux = _mux(a=(5, 10), b=(20, 21), y=(7, 8))
    assert _depth_through(head, {7: 1, 8: 1, 10: 1, 11: 0}, mux) is None


def test_mux_with_both_legs_constant_rejected() -> None:
    """A register that never shifts isn't a synchroniser."""
    head = _flop(d=(7, 8), q=(10, 11))
    mux = _mux(a=("1", "1"), b=("0", "0"), y=(7, 8))
    assert _depth_through(head, {7: 1, 8: 1, 10: 0, 11: 0}, mux) is None


def test_pmux_rejected() -> None:
    """``$pmux`` concatenates ``len(S)`` legs of ``WIDTH`` bits into
    ``B`` — a priority structure, explicitly out of scope."""
    head = _flop(d=(7, 8), q=(10, 11))
    mux = _mux(a=(5, 10), b=("0", "0"), y=(7, 8), cell_type="$pmux")
    assert _depth_through(head, {7: 1, 8: 1, 10: 1, 11: 0}, mux) is None


def test_mux_output_with_extra_reader_rejected() -> None:
    """If a ``Y`` bit feeds something besides the flop's ``D``, the
    pre-flop value of an earlier stage escapes the chain."""
    head = _flop(d=(7, 8), q=(10, 11))
    mux = _mux(a=(5, 10), b=("0", "0"), y=(7, 8))
    assert _depth_through(head, {7: 1, 8: 2, 10: 1, 11: 0}, mux) is None


def test_mux_output_not_exactly_the_d_vector_rejected() -> None:
    """Only a mux whose ``Y`` *is* the flop's ``D``, lane for lane, is
    looked through; a partial / reordered overlap is not."""
    head = _flop(d=(7, 8), q=(10, 11))
    mux = _mux(a=(5, 10, 6), b=("0", "0", "0"), y=(7, 8, 30))
    assert _depth_through(head, {7: 1, 8: 1, 30: 1, 10: 1, 11: 0}, mux) is None


def test_chained_reset_then_enable_mux_rejected() -> None:
    """Only one mux level is looked through: a reset mux feeding an
    enable mux is out of scope and still reads as depth 1."""
    head = _flop(d=(7, 8), q=(10, 11))
    enable = Cell(
        name="en_mux",
        type="$mux",
        connections={"A": (20, 21), "B": (10, 11), "S": (98,), "Y": (7, 8)},
    )
    reset = _mux(a=(5, 10), b=("0", "0"), y=(20, 21))
    counts = {7: 1, 8: 1, 20: 1, 21: 1, 10: 2, 11: 1}
    assert _depth_through(head, counts, enable, reset) is None


def test_first_stage_in_use_behind_reset_mux_stops_at_depth_one() -> None:
    """The exactly-one-reader rule still applies to the lane ``Q`` bits.
    Behind a reset mux that single reader is the mux's data leg; a
    second reader means the first stage is already in use."""
    head = _flop(d=(7, 8), q=(10, 11))
    mux = _mux(a=(5, 10), b=("0", "0"), y=(7, 8))
    assert _depth_through(head, {7: 1, 8: 1, 10: 2, 11: 0}, mux) is None


def test_no_module_means_no_look_through() -> None:
    """The ``module`` / ``bit_drivers`` arguments are optional, so a
    caller without a driver map gets the pre-#301 behaviour."""
    head = _flop(d=(7, 8), q=(10, 11))
    assert _packed_shift_register_depth(head, {7: 1, 8: 1, 10: 1, 11: 0}) is None


def test_two_stage_packed_shift_is_depth_two() -> None:
    # D = {Q[0], ext}: lane 0 samples external bit 5, lane 1 shifts in
    # Q[0]. Q[0] is read only by the next lane (count 1); the tap Q[1]
    # has no further reader.
    head = _flop(d=(5, 10), q=(10, 11))
    assert _packed_shift_register_depth(head, {10: 1, 11: 0}) == 2


def test_tap_consumed_by_single_cell_still_depth_two() -> None:
    # The synchronised tap Q[1] feeds exactly one downstream cell
    # (reader count 1) rather than a bare output port — the chain still
    # terminates at depth 2 because that lane has no follow-on shift.
    head = _flop(d=(5, 10), q=(10, 11))
    assert _packed_shift_register_depth(head, {10: 1, 11: 1}) == 2


def test_three_stage_packed_shift_is_depth_three() -> None:
    head = _flop(d=(5, 10, 11), q=(10, 11, 12))
    assert _packed_shift_register_depth(head, {10: 1, 11: 1, 12: 0}) == 3


def test_first_stage_in_use_stops_at_depth_one() -> None:
    # Q[0] read by both the shift lane and an external consumer → the
    # synchronised value is in use after one flop; not a deep sync.
    head = _flop(d=(5, 10), q=(10, 11))
    assert _packed_shift_register_depth(head, {10: 2, 11: 0}) is None


def test_single_bit_flop_rejected() -> None:
    head = _flop(d=(5,), q=(10,))
    assert _packed_shift_register_depth(head, {10: 0}) is None


def test_mismatched_d_q_width_rejected() -> None:
    head = _flop(d=(5,), q=(10, 11))
    assert _packed_shift_register_depth(head, {}) is None


def test_non_int_bits_rejected() -> None:
    assert _packed_shift_register_depth(_flop(d=(5, 10), q=(10, "x")), {}) is None
    assert _packed_shift_register_depth(_flop(d=(5, "0"), q=(10, 11)), {}) is None


def test_repeated_q_bit_rejected() -> None:
    head = _flop(d=(5, 10), q=(10, 10))
    assert _packed_shift_register_depth(head, {10: 1}) is None


def test_fanout_q_bit_rejected() -> None:
    # Q[0] feeds two D lanes — not a clean linear shift register.
    head = _flop(d=(5, 10, 10), q=(10, 11, 12))
    assert _packed_shift_register_depth(head, {10: 2, 11: 0, 12: 0}) is None


def test_pure_feedback_no_external_input_rejected() -> None:
    # Both lanes feed back internally — no freshly sampled crossing bit.
    head = _flop(d=(11, 10), q=(10, 11))
    assert _packed_shift_register_depth(head, {10: 1, 11: 1}) is None


def test_two_external_lanes_bus_rejected() -> None:
    # Two independent external inputs → a bus register, not a single
    # packed synchroniser.
    head = _flop(d=(5, 6), q=(10, 11))
    assert _packed_shift_register_depth(head, {10: 0, 11: 0}) is None


# --- direct coverage of the look-through helper's guards -------------------


def test_look_through_helper_rejects_empty_and_constant_d() -> None:
    head = _flop(d=(), q=(10, 11))
    module = _module(head)
    drivers = _bit_drivers(module)
    assert _constant_leg_reset_mux_data_bits(head, module, drivers, {}) is None

    const_d = _flop(d=("0", "0"), q=(10, 11))
    module = _module(const_d)
    assert (
        _constant_leg_reset_mux_data_bits(const_d, module, _bit_drivers(module), {})
        is None
    )


def test_look_through_helper_rejects_width_mismatched_mux() -> None:
    """A mux whose A/B/Y widths disagree with ``D`` isn't the reset
    shape (defensive: Yosys never emits one)."""
    head = _flop(d=(7, 8), q=(10, 11))
    mux = _mux(a=(5,), b=("0",), y=(7, 8))
    assert _depth_through(head, {7: 1, 8: 1, 10: 1, 11: 0}, mux) is None


def test_bit_drivers_built_on_demand_when_only_module_given() -> None:
    """``bit_drivers`` is optional — the recogniser rebuilds it from the
    module when a caller has no cached map."""
    head = _flop(d=(7, 8), q=(10, 11))
    mux = _mux(a=(5, 10), b=("0", "0"), y=(7, 8))
    module = _module(head, mux)
    counts = {7: 1, 8: 1, 10: 1, 11: 0}
    assert _packed_shift_register_depth(head, counts, module) == 2


def test_cdc_003_reset_mux_exemption_needs_a_source_flop() -> None:
    """The CDC-003 exemption keys on the source flop's ``Q`` reaching the
    mux data leg, so a boundary-sourced crossing (no ``src_flop``) can't
    claim it (#301)."""
    head = _flop(d=(7, 8), q=(10, 11))
    mux = _mux(a=(5, 10), b=("0", "0"), y=(7, 8))
    module = _module(head, mux)
    ctx = _build_context(module, None)
    crossing = Crossing(
        src_clock="src_clk",
        dst_flop=head,
        dst_clock="dst_clk",
        min_hops=1,
        width=1,
        src_flop=None,
    )
    assert not _crossing_enters_through_reset_mux_only(module, crossing, ctx)
