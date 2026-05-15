"""Coverage tests for slang-frontend compile-time-constant if/else
folding.

Issue: rtl-buddy-cdc#72 — after the deferred-emission walker (PR #70)
landed, an ``if/else`` with a compile-time-constant condition emits
**both** arms' fanin into the resulting flop's mux tree. The dead
arm is structurally unreachable at runtime (Yosys-flatten + opt_clean
prunes it) but the slang frontend doesn't fold, so the rule pack
reports crossings through the dead path.

Surfaced on tiny-NPU's MXP: 12 non-diagonal cors each report 3
md→mr crossings (the `if (IS_DIAGONAL) ... else ...` mr_clk body
where `IS_DIAGONAL` is statically false). 36 false-positive
crossings on a 4×4 array.

The tests below pin the contract: a constant-foldable condition
collapses to walk only the live arm, and the dead arm's RHS does
not appear in any cell connection of the resulting flop.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc.frontend import Frontend, elaborate

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
if not PYSLANG_INSTALLED:
    pytest.skip(
        "pyslang not installed — slang const-cond-fold tests are gated on it",
        allow_module_level=True,
    )

FLOP_TYPES = frozenset({"$dff", "$adff", "$dffe", "$adffe", "$sdff", "$sdffe"})


def _elaborate(tmp_path: Path, src: str, top: str = "m"):
    sv = tmp_path / f"{top}.sv"
    sv.write_text(src)
    return elaborate([sv], top, frontend=Frontend.slang)


def _flops(module) -> list:
    return [c for c in module.cells.values() if c.type in FLOP_TYPES]


def _reachable_bits(module, start_bit) -> set:
    """All bits reachable backwards from ``start_bit`` through comb
    cells. Used to assert that a port's bits don't reach the flop's D
    when the arm that referenced them is statically dead."""
    drivers = {}
    for name, cell in module.cells.items():
        for port, bits in cell.connections.items():
            if port in ("Q", "Y"):
                for b in bits:
                    if isinstance(b, int):
                        drivers[b] = (name, port)
    seen = set()
    frontier = [start_bit]
    walk_types = {"$mux", "$not", "$and", "$or", "$xor", "$add", "$sub"}
    while frontier:
        b = frontier.pop()
        if b in seen or not isinstance(b, int):
            continue
        seen.add(b)
        drv = drivers.get(b)
        if drv is None:
            continue
        cell = module.cells[drv[0]]
        if cell.type not in walk_types:
            continue
        for port, bits in cell.connections.items():
            if port in ("Q", "Y"):
                continue
            for x in bits:
                frontier.append(x)
    return seen


# --- literal constant condition -------------------------------------------


def test_if_literal_true_takes_only_true_arm(tmp_path: Path) -> None:
    """``if (1'b1) q <= a; else q <= b;`` — the b path is dead. The
    resulting flop's D bits must not reach ``b_in``'s port bit."""
    src = """
    module m (input logic clk, a_in, b_in, output logic q);
        always_ff @(posedge clk) begin
            if (1'b1) q <= a_in;
            else      q <= b_in;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1, f"expected one flop; got {len(flops)}"
    d_bit = flops[0].connections["D"][0]
    a_bit = mod.ports["a_in"].bits[0]
    b_bit = mod.ports["b_in"].bits[0]
    # Live arm: a_in must drive D directly (no mux needed for a folded condition).
    assert d_bit == a_bit, f"expected D=a_in directly; got D={d_bit}, a_in={a_bit}"
    # Dead arm: b_in must not appear anywhere in the reachable fanin.
    reachable = _reachable_bits(mod, d_bit) | {d_bit}
    assert b_bit not in reachable, (
        f"b_in bit {b_bit} must not be reachable from D's fanin; "
        "the dead arm should be pruned."
    )


def test_if_literal_false_takes_only_false_arm(tmp_path: Path) -> None:
    """Mirror case: ``if (1'b0)`` — the a path is dead."""
    src = """
    module m (input logic clk, a_in, b_in, output logic q);
        always_ff @(posedge clk) begin
            if (1'b0) q <= a_in;
            else      q <= b_in;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1
    d_bit = flops[0].connections["D"][0]
    a_bit = mod.ports["a_in"].bits[0]
    b_bit = mod.ports["b_in"].bits[0]
    assert d_bit == b_bit, f"expected D=b_in directly; got D={d_bit}, b_in={b_bit}"
    reachable = _reachable_bits(mod, d_bit) | {d_bit}
    assert a_bit not in reachable, (
        f"a_in bit {a_bit} must not be reachable; the false-true arm is dead."
    )


# --- parameter-folded condition ------------------------------------------


def test_if_parameter_false_takes_only_false_arm(tmp_path: Path) -> None:
    """The headline mxp_cor case: ``if (USE_A) q <= a; else q <= b;``
    where ``USE_A`` is a per-instance parameter bound at elaboration.
    The non-diagonal cor pattern (IS_DIAGONAL=0)."""
    src = """
    module child #(parameter bit USE_A = 0) (
        input  logic clk,
        input  logic d_a,
        input  logic d_b,
        output logic q
    );
        always_ff @(posedge clk) begin
            if (USE_A) q <= d_a;
            else       q <= d_b;
        end
    endmodule

    module m (input logic clk, in_a, in_b, output logic q_out);
        child #(.USE_A(1'b0)) u_b (.clk(clk), .d_a(in_a), .d_b(in_b), .q(q_out));
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1
    d_bit = flops[0].connections["D"][0]
    in_a_bit = mod.ports["in_a"].bits[0]
    in_b_bit = mod.ports["in_b"].bits[0]
    # The instance has USE_A=0, so the live arm assigns d_b → drives D directly.
    assert d_bit == in_b_bit, (
        f"expected D=in_b (the live arm); got D={d_bit}, in_b={in_b_bit}"
    )
    reachable = _reachable_bits(mod, d_bit) | {d_bit}
    assert in_a_bit not in reachable, (
        f"in_a bit {in_a_bit} must not be reachable through D's mux; "
        f"USE_A=0 means the a-arm is statically dead. Reachable={sorted(reachable)[:10]}"
    )


def test_if_parameter_true_takes_only_true_arm(tmp_path: Path) -> None:
    """Mirror: ``USE_A = 1`` selects the a-arm."""
    src = """
    module child #(parameter bit USE_A = 0) (
        input  logic clk,
        input  logic d_a,
        input  logic d_b,
        output logic q
    );
        always_ff @(posedge clk) begin
            if (USE_A) q <= d_a;
            else       q <= d_b;
        end
    endmodule

    module m (input logic clk, in_a, in_b, output logic q_out);
        child #(.USE_A(1'b1)) u_a (.clk(clk), .d_a(in_a), .d_b(in_b), .q(q_out));
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1
    d_bit = flops[0].connections["D"][0]
    in_a_bit = mod.ports["in_a"].bits[0]
    in_b_bit = mod.ports["in_b"].bits[0]
    assert d_bit == in_a_bit, (
        f"expected D=in_a (the live arm); got D={d_bit}, in_a={in_a_bit}"
    )
    reachable = _reachable_bits(mod, d_bit) | {d_bit}
    assert in_b_bit not in reachable, (
        f"in_b bit {in_b_bit} must not be reachable; USE_A=1 → b-arm is dead."
    )


# --- runtime condition still emits the mux -------------------------------


def test_runtime_condition_still_emits_mux(tmp_path: Path) -> None:
    """Defensive: a runtime condition (not foldable) must still emit
    the full mux tree. Without this, the const-fold path would
    accidentally collapse legitimate runtime if/else."""
    src = """
    module m (input logic clk, cond, a_in, b_in, output logic q);
        always_ff @(posedge clk) begin
            if (cond) q <= a_in;
            else      q <= b_in;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1
    d_bit = flops[0].connections["D"][0]
    reachable = _reachable_bits(mod, d_bit) | {d_bit}
    a_bit = mod.ports["a_in"].bits[0]
    b_bit = mod.ports["b_in"].bits[0]
    assert a_bit in reachable and b_bit in reachable, (
        "runtime if/else must keep both arms' fanin in the mux tree; "
        f"a_in={a_bit}, b_in={b_bit}, reachable={sorted(reachable)[:10]}"
    )
