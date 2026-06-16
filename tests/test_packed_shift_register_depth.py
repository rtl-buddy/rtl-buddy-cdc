"""Unit tests for ``_packed_shift_register_depth`` (issue #264).

Drives the structural recogniser directly with synthetic ``Flop``s so
each accept/reject branch is pinned without needing a full Yosys
fixture per case.
"""

from __future__ import annotations

from rtl_buddy_cdc.flops import Flop
from rtl_buddy_cdc.netlist import Bit, Cell
from rtl_buddy_cdc.rules import _packed_shift_register_depth


def _flop(d: tuple[Bit, ...], q: tuple[Bit, ...]) -> Flop:
    cell = Cell(name="sr", type="$dff", connections={"D": d, "Q": q})
    return Flop(cell=cell, clk=1, d=d, q=q)


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
