"""Coverage tests for slang-frontend if/else both-arms emission.

Issue: rtl-buddy-cdc#64 follow-up. The original PR (#65 / merged) only
handled the no-else case ``if (cond) q <= ...;`` — one flop per LHS
wrapped in a hold-feedback mux. The if/else both-arms case
``if (cond) q <= a; else q <= b;`` was deferred: walking both arms
unconditionally emits *two* flops with the same Q (a netlist
malformity) and the rule pack can't see the conditional select.

This module pins the contract: a single flop per LHS in the
procedural block, with a mux tree that picks between the two RHSs
based on the enclosing condition. Surfaced on tiny-NPU's
``ip_dtnpu_mxp_cor`` mu_clk always_ff body — every cor's
``mu_cmd_data_out`` / ``mu_acc_data_out`` ripple has this exact
shape, and the over-emission blocks ml→mu crossing detection.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc.frontend import Frontend, elaborate

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
if not PYSLANG_INSTALLED:
    pytest.skip(
        "pyslang not installed — slang if/else-both-arms tests are gated on it",
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


# --- one flop per LHS, mux tree from both arms ---------------------------


def test_if_else_both_arms_single_flop_with_mux(tmp_path: Path) -> None:
    """``if (cond) q <= a; else q <= b;`` — must produce one flop for
    ``q`` whose D is a mux selecting between ``a`` and ``b``. With the
    old walker we'd get *two* flops with the same Q (netlist
    malformity); after deferred emission the writes collapse to one
    flop per LHS."""
    src = """
    module m (input logic clk, rst_n, cond, a_in, b_in, output logic q);
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) q <= 1'b0;
            else if (cond) q <= a_in;
            else           q <= b_in;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1, (
        f"expected one flop for q after deferred emission; got {len(flops)}. "
        "Two flops with same Q means the if/else arms emitted independently "
        "(the pre-deferred-emission bug)."
    )
    # The flop's D should reach a $mux. The mux's S is (a function of)
    # cond, and its data inputs are a_in and b_in (in some order — the
    # mux-tree builder may invert).
    flop = flops[0]
    d_bit = flop.connections["D"][0]
    drivers = _build_drivers(mod)
    drv = drivers.get(d_bit)
    assert drv is not None, "flop D has no driver — no mux emitted"
    drv_cell = mod.cells[drv[0]]
    assert drv_cell.type == "$mux", (
        f"expected $mux driving D; got {drv_cell.type}"
    )


def test_if_else_both_arms_walks_both_rhs(tmp_path: Path) -> None:
    """Both RHS expressions must be reachable from the resulting flop's
    D. Bits of ``a_in`` and ``b_in`` (port LSBs) must both appear in
    the mux tree's fanin."""
    src = """
    module m (input logic clk, rst_n, cond, a_in, b_in, output logic q);
        always_ff @(posedge clk or negedge rst_n) begin
            if (!rst_n) q <= 1'b0;
            else if (cond) q <= a_in;
            else           q <= b_in;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1
    a_bit = mod.ports["a_in"].bits[0]
    b_bit = mod.ports["b_in"].bits[0]
    # Collect everything reachable from D via $mux / $not / $and (the
    # cells the mux-tree builder produces).
    drivers = _build_drivers(mod)
    seen = set()
    frontier = [flops[0].connections["D"][0]]
    walk_types = {"$mux", "$not", "$and", "$or"}
    while frontier:
        bit = frontier.pop()
        if bit in seen or not isinstance(bit, int):
            continue
        seen.add(bit)
        drv = drivers.get(bit)
        if drv is None:
            continue
        cell = mod.cells[drv[0]]
        if cell.type not in walk_types:
            continue
        for port, bits in cell.connections.items():
            if port in ("Q", "Y"):
                continue
            for b in bits:
                frontier.append(b)
    assert a_bit in seen, (
        f"a_in bit {a_bit} should reach the flop's D through the mux tree; "
        f"reachable: {sorted(seen)[:20]}..."
    )
    assert b_bit in seen, (
        f"b_in bit {b_bit} should reach the flop's D through the mux tree; "
        f"reachable: {sorted(seen)[:20]}..."
    )


# --- mxp_cor mu_clk shape (the production case) --------------------------


def test_mxp_cor_mu_clk_shape(tmp_path: Path) -> None:
    """The exact ``ip_dtnpu_mxp_cor`` mu_clk body shape: if/else with
    both arms writing the same output, source-clock signal feeding
    one of the arms. Today this produces two flops with the same Q
    (and the ml→mu crossing isn't visible because the analyzer sees
    two independent flops, not one mux-gated bus crossing)."""
    src = """
    module m (
        input  logic       mu_clk, mu_rst_n,
        input  logic       match_ism_turn,
        input  logic [3:0] ml_cmd_data,
        input  logic [3:0] mu_cmd_data_in,
        output logic [3:0] mu_cmd_data_out
    );
        always_ff @(posedge mu_clk or negedge mu_rst_n) begin
            if (!mu_rst_n) mu_cmd_data_out <= 4'd0;
            else if (match_ism_turn) mu_cmd_data_out <= ml_cmd_data;
            else                     mu_cmd_data_out <= mu_cmd_data_in;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1, (
        f"expected ONE flop for mu_cmd_data_out (mxp_cor mu_clk shape); "
        f"got {len(flops)}. Multiple flops means each arm emitted "
        "independently — the pre-deferred-emission bug that hides "
        "the ml→mu crossing through the mux."
    )
