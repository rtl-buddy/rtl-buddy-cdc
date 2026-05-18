"""Coverage tests for slang-frontend ``case`` statement completeness.

Issues:

- rtl-buddy-cdc#84 — compile-time-constant case-expr must fold to walk
  only the live arm. Today every item walks unconditionally, so dead
  arms' fanin leaks into the deferred-emission mux tree and the rule
  pack reports false-positive crossings through statically-unreachable
  case items. Companion to #72's if/else fold.

- rtl-buddy-cdc#85 — runtime case-arm bodies must walk with a
  per-item enable (``case_expr == match``) pushed onto the enable
  stack so writes accumulate with the right select bit. Today each
  arm walks unconditionally, the drain collapses them to a
  last-write-wins value, and the rule pack's ``_is_gated_bus_crossing``
  can't fire on the standard FSM "load bus inside a case arm" shape.

The tests below pin both contracts:

- A constant case-expr produces flops whose D fanin contains only the
  live arm's RHS bits — dead arms' ports do not appear anywhere
  reachable from D.
- A dynamic case-expr produces flops whose D is a ``$mux`` tree whose
  ``S`` traces back through an ``$eq`` cell wired to the case
  expression and the matching constant.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc.frontend import Frontend, elaborate

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
if not PYSLANG_INSTALLED:
    pytest.skip(
        "pyslang not installed — slang case-completeness tests are gated on it",
        allow_module_level=True,
    )

FLOP_TYPES = frozenset({"$dff", "$adff", "$dffe", "$adffe", "$sdff", "$sdffe"})


def _elaborate(tmp_path: Path, src: str, top: str = "m"):
    sv = tmp_path / f"{top}.sv"
    sv.write_text(src)
    return elaborate([sv], top, frontend=Frontend.slang)


def _flops(module) -> list:
    return [c for c in module.cells.values() if c.type in FLOP_TYPES]


def _build_drivers(mod) -> dict:
    drv = {}
    for name, cell in mod.cells.items():
        for port, bits in cell.connections.items():
            if port in ("Q", "Y"):
                for b in bits:
                    if isinstance(b, int):
                        drv[b] = (name, port)
    return drv


def _reachable_bits(mod, start_bit) -> set:
    """All bits reachable backwards from ``start_bit`` through comb cells.

    Used to assert that a port's bits appear in (or are absent from)
    the fanin of a flop's D — the underlying question whenever we
    want to know whether a particular RHS made it into the netlist.
    """
    drivers = _build_drivers(mod)
    seen: set = set()
    frontier = [start_bit]
    walk_types = {
        "$mux",
        "$not",
        "$and",
        "$or",
        "$xor",
        "$eq",
        "$ne",
        "$add",
        "$sub",
    }
    while frontier:
        b = frontier.pop()
        if b in seen or not isinstance(b, int):
            continue
        seen.add(b)
        drv = drivers.get(b)
        if drv is None:
            continue
        cell = mod.cells[drv[0]]
        if cell.type not in walk_types:
            continue
        for port, bits in cell.connections.items():
            if port in ("Q", "Y"):
                continue
            for x in bits:
                frontier.append(x)
    return seen


# --- #84: compile-time-constant case-expr folds ---------------------------


def test_const_case_walks_only_live_arm(tmp_path: Path) -> None:
    """Parameter-driven ``case (MODE)`` with three arms — only the
    matching arm's RHS should appear in the flop's D fanin. The dead
    arms' input ports must not be reachable from D.

    Mirrors the #84 repro: ``child #(.MODE(0))`` instantiations should
    produce a netlist with no fanin from the ``MODE=1`` arm."""
    src = """
    module m #(parameter int MODE = 0) (
        input  logic clk, rst_n,
        input  logic d_a, d_b,
        output logic q
    );
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) q <= 1'b0;
            else case (MODE)
                0:       q <= d_a;
                1:       q <= d_b;
                default: q <= 1'b0;
            endcase
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1, f"expected one flop for q; got {len(flops)}"
    d_bit = flops[0].connections["D"][0]
    seen = _reachable_bits(mod, d_bit)

    d_a_bit = mod.ports["d_a"].bits[0]
    d_b_bit = mod.ports["d_b"].bits[0]
    assert d_a_bit in seen, (
        f"d_a (MODE=0 arm RHS) should reach the flop's D; reachable={sorted(seen)[:20]}"
    )
    assert d_b_bit not in seen, (
        f"d_b (MODE=1 arm — dead) must not reach the flop's D; "
        f"reachable={sorted(seen)[:20]}. Const-fold dropped — dead arms leak fanin."
    )


def test_const_case_default_when_no_match(tmp_path: Path) -> None:
    """``case (3)`` with arms 0/1 and a default — the default must be
    the only arm walked (no explicit match), and arms 0/1's RHSs must
    not appear in the flop's D fanin."""
    src = """
    module m (
        input  logic clk, rst_n,
        input  logic d_a, d_b, d_def,
        output logic q
    );
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) q <= 1'b0;
            else case (3)
                0:       q <= d_a;
                1:       q <= d_b;
                default: q <= d_def;
            endcase
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1
    d_bit = flops[0].connections["D"][0]
    seen = _reachable_bits(mod, d_bit)

    d_def_bit = mod.ports["d_def"].bits[0]
    d_a_bit = mod.ports["d_a"].bits[0]
    d_b_bit = mod.ports["d_b"].bits[0]
    assert d_def_bit in seen, "default arm's RHS should reach flop D"
    assert d_a_bit not in seen, "arm 0 is dead (case-expr=3); RHS must not reach D"
    assert d_b_bit not in seen, "arm 1 is dead (case-expr=3); RHS must not reach D"


# --- #85: dynamic case-expr emits per-arm enable --------------------------


def test_dynamic_case_emits_mux_with_eq_select(tmp_path: Path) -> None:
    """The FSM-style "load bus inside a case arm" shape — the flop's D
    must be a ``$mux`` whose ``S`` traces back through an ``$eq`` cell
    wired to the case expression and the matching constant.

    Without per-arm enable inference, the arm's write accumulates
    unconditionally, the drain collapses to D = bus_in, and the rule
    pack's gated-bus detector can't recognise that ``state`` gates
    the load."""
    src = """
    module m (
        input  logic       clk, rst_n,
        input  logic [1:0] state,
        input  logic [3:0] bus_in,
        output logic [3:0] addr_q
    );
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) addr_q <= 4'd0;
            else case (state)
                2'd0: addr_q <= bus_in;
                default: ;
            endcase
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1, f"expected one flop for addr_q; got {len(flops)}"
    flop = flops[0]
    d_bits = flop.connections["D"]
    drivers = _build_drivers(mod)
    drv = drivers.get(d_bits[0])
    assert drv is not None, "flop D has no driver"
    drv_cell = mod.cells[drv[0]]
    assert drv_cell.type == "$mux", (
        f"expected $mux driving D for case-arm gated load; got {drv_cell.type}. "
        "Without per-arm enable, the rule pack's gated-bus detector can't see "
        "the state-gating on this FSM-style load."
    )
    # The mux's S must be driven (transitively) by an $eq cell —
    # that's the ``state == 2'd0`` equality. Walk one or two levels
    # back through $or (multi-match items chain ORs) to find it.
    s_bit = drv_cell.connections["S"][0]
    seen = _reachable_bits(mod, s_bit)
    cell_types = set()
    for b in seen:
        d = drivers.get(b)
        if d is not None:
            cell_types.add(mod.cells[d[0]].type)
    assert "$eq" in cell_types, (
        f"mux S should resolve to an $eq cell (state == 2'd0); reachable cell "
        f"types: {cell_types}"
    )

    # And the mux must wire the case expression's source (state) into
    # the eq cell — bus_in must reach the data side (B) of the mux.
    bus_bit = mod.ports["bus_in"].bits[0]
    state_bit = mod.ports["state"].bits[0]
    b_bits = drv_cell.connections["B"]
    assert d_bits[0] != bus_bit, (
        "without enable inference, D is wired straight to bus_in"
    )
    assert bus_bit in b_bits, f"$mux B should be bus_in; got {b_bits}"
    assert state_bit in seen, (
        "state should reach the mux S through the $eq cell — confirms the "
        "case-expr is wired into the gating bit"
    )


def test_dynamic_case_default_negates_explicit_matches(tmp_path: Path) -> None:
    """``default`` arm's enable must be the negation of the OR of the
    explicit-match equalities. Two arms + default, with the default
    writing a distinct LHS — the default's write should sit under a
    ``$not`` cell wrapping the OR of the two ``$eq``s."""
    src = """
    module m (
        input  logic       clk, rst_n,
        input  logic [1:0] sel,
        input  logic       d_def,
        output logic       q_def
    );
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) q_def <= 1'b0;
            else case (sel)
                2'd0: ;
                2'd1: ;
                default: q_def <= d_def;
            endcase
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1
    flop = flops[0]
    drivers = _build_drivers(mod)
    drv = drivers.get(flop.connections["D"][0])
    assert drv is not None, "no driver for q_def flop D"
    drv_cell = mod.cells[drv[0]]
    assert drv_cell.type == "$mux", (
        f"expected $mux driving D for default-only conditional write; "
        f"got {drv_cell.type}"
    )
    # Reachable cells from the mux S should include a $not cell —
    # that's the negation of the OR of explicit-match equalities.
    s_bit = drv_cell.connections["S"][0]
    seen = _reachable_bits(mod, s_bit)
    cell_types = set()
    for b in seen:
        d = drivers.get(b)
        if d is not None:
            cell_types.add(mod.cells[d[0]].type)
    assert "$not" in cell_types, (
        f"default-arm enable must include a $not (negate the OR of explicit "
        f"matches); reachable types: {cell_types}"
    )
    assert "$eq" in cell_types, (
        f"explicit-match equalities must be present beneath the $not; "
        f"reachable types: {cell_types}"
    )


def test_dynamic_case_multi_match_item_ors_equalities(tmp_path: Path) -> None:
    """``2'd0, 2'd1: addr_q <= bus_in;`` — the item's enable is the
    OR of two equalities. The mux S should resolve to a ``$or`` of
    two ``$eq`` cells."""
    src = """
    module m (
        input  logic       clk, rst_n,
        input  logic [1:0] sel,
        input  logic [3:0] bus_in,
        output logic [3:0] addr_q
    );
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) addr_q <= 4'd0;
            else case (sel)
                2'd0, 2'd1: addr_q <= bus_in;
                default: ;
            endcase
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1
    drivers = _build_drivers(mod)
    drv = drivers.get(flops[0].connections["D"][0])
    assert drv is not None
    mux = mod.cells[drv[0]]
    assert mux.type == "$mux"
    s_drv = drivers.get(mux.connections["S"][0])
    assert s_drv is not None, "mux S has no driver"
    s_cell = mod.cells[s_drv[0]]
    assert s_cell.type == "$or", (
        f"multi-match item's enable should be an $or of equalities; "
        f"got driver type {s_cell.type}"
    )
    # Each input of the $or should land on an $eq.
    for port in ("A", "B"):
        eq_drv = drivers.get(s_cell.connections[port][0])
        assert eq_drv is not None
        assert mod.cells[eq_drv[0]].type == "$eq", (
            f"$or input {port} should be driven by $eq (per-match equality)"
        )


# --- regression: existing stmt-walker contract -----------------------------


def test_existing_state_machine_still_emits_distinct_lvalues(tmp_path: Path) -> None:
    """Regression guard for ``test_slang_stmt_walker.test_case_statement_emits_all_lvalues``:
    distinct LHSs across arms must still each emit a flop. With per-arm
    enable + deferred emission they now collapse to one flop per LHS
    (state / a / b / c = 4 flops), where the previous walker emitted
    one per *site*. ``≥ 4`` is the stable contract either way."""
    src = """
    module m (
        input  logic clk, rst_n,
        input  logic [1:0] sel,
        output logic [1:0] state,
        output logic a, b, c
    );
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) begin
                state <= 2'd0;
                a <= 1'b0; b <= 1'b0; c <= 1'b0;
            end else begin
                case (sel)
                    2'd0: begin state <= 2'd1; a <= 1'b1; end
                    2'd1: begin state <= 2'd2; b <= 1'b1; end
                    2'd2: begin state <= 2'd3; c <= 1'b1; end
                    default: state <= 2'd0;
                endcase
            end
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) >= 4, (
        f"expected ≥ 4 flops (one per distinct lvalue state/a/b/c); got {len(flops)}"
    )
