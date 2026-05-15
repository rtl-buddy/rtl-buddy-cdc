"""Coverage tests for the always_ff body walker's procedural
for-loop / foreach support.

Issue: rtl-buddy-cdc#59 — :func:`_emit_assignments_in` does not
recurse through :class:`ForLoopStatement` or
:class:`ForeachLoopStatement`. The standard SV synchronizer chain
idiom — ``for (int i = 1; i < STAGES; i++) chain[i] <= chain[i-1];``
— silently emits zero flops because the body's nonblocking assigns
use the loop variable ``i`` as a selector, which only has a value
*during* iteration. Without virtual unrolling at elaboration time
the analyzer can't see ``ip_cdc_sync`` / ``ip_cdc_fifo`` and async
crossings through every standard CDC IP disappear.

The tests below pin the unrolling contract: a procedural loop with
compile-time-constant bounds must produce one flop per iteration
(modulo opt_clean dedup that yosys also does), with the body's
loop-variable references resolved to the current iteration's value.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc.frontend import Frontend, elaborate

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
if not PYSLANG_INSTALLED:
    pytest.skip(
        "pyslang not installed — slang for-loop tests are gated on it",
        allow_module_level=True,
    )

FLOP_TYPES = frozenset({"$dff", "$adff", "$dffe", "$adffe", "$sdff", "$sdffe"})


def _flop_count(module) -> int:
    return sum(1 for c in module.cells.values() if c.type in FLOP_TYPES)


def _elaborate(tmp_path: Path, src: str, top: str = "m"):
    sv = tmp_path / f"{top}.sv"
    sv.write_text(src)
    return elaborate([sv], top, frontend=Frontend.slang)


# --- canonical for-loop shapes --------------------------------------------


def test_for_loop_emits_one_flop_per_iteration(tmp_path: Path) -> None:
    """``for (int i = 0; i < 4; i++) q[i] <= d[i];`` — bounded by a
    literal constant. The walker must unroll the body 4 times,
    rebinding ``i`` to 0..3 so each iteration's ``q[i] <= d[i]``
    resolves to a single-bit flop."""
    src = """
    module m (
        input  logic clk,
        input  logic [3:0] d,
        output logic [3:0] q
    );
        always_ff @(posedge clk) begin
            for (int i = 0; i < 4; i++)
                q[i] <= d[i];
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 4, (
        f"expected 4 flops from for-loop[N=4]; got {_flop_count(mod)} "
        f"(cells: {sorted(c.type for c in mod.cells.values())})"
    )


def test_for_loop_with_parameterised_bound(tmp_path: Path) -> None:
    """Same shape but the stop is a parameter (compile-time constant).
    The unroller must fold the parameter to its concrete value."""
    src = """
    module m #(parameter int STAGES = 3) (
        input  logic clk,
        input  logic [STAGES-1:0] d,
        output logic [STAGES-1:0] q
    );
        always_ff @(posedge clk) begin
            for (int i = 0; i < STAGES; i++)
                q[i] <= d[i];
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 3, (
        f"expected 3 flops from for-loop[STAGES=3]; got {_flop_count(mod)}"
    )


def test_for_loop_with_loop_var_arithmetic(tmp_path: Path) -> None:
    """The textbook sync-chain shape: ``chain[i] <= chain[i-1]`` for
    i in [1, STAGES). Each iteration's RHS references a *different*
    bit of the same vector, so the walker has to fold ``i-1`` for
    every i it visits."""
    src = """
    module m #(parameter int STAGES = 3) (
        input  logic clk, d,
        output logic q
    );
        logic [STAGES-1:0] chain;
        always_ff @(posedge clk) begin
            chain[0] <= d;
            for (int i = 1; i < STAGES; i++)
                chain[i] <= chain[i-1];
        end
        assign q = chain[STAGES-1];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    # 1 flop for chain[0] (the bare ExpressionStatement) + 2 from the
    # for-loop unroll (i=1, i=2). Total = 3.
    assert _flop_count(mod) == 3, (
        f"expected 3 flops (1 bare + 2 unrolled); got {_flop_count(mod)}"
    )


def test_ip_cdc_sync_shape_emits_full_chain(tmp_path: Path) -> None:
    """The ``ip_cdc_sync`` body shape: synchronous-reset if/else with
    a for-loop in each arm. The reset branch's for-loop is fanout
    (constant-driven reset across all stages); the data branch has
    the cascade.

    Uses a packed-array chain rather than the production IP's packed
    ``[WIDTH-1:0]`` × unpacked ``[STAGES]`` shape — unpacked-array
    bit allocation in the slang frontend is a separate gap and the
    walker-side contract this fixture pins is independent of it.
    The cascade emits STAGES-1 flops after the unroll, plus one for
    the bare ``chain[0] <= d`` site.
    """
    src = """
    module m #(parameter int STAGES = 3) (
        input  logic clk, rst_n, d,
        output logic q
    );
        logic [STAGES-1:0] chain;
        always_ff @(posedge clk) begin
            if (!rst_n) begin
                for (int i = 0; i < STAGES; i++)
                    chain[i] <= 1'b0;
            end else begin
                chain[0] <= d;
                for (int i = 1; i < STAGES; i++)
                    chain[i] <= chain[i-1];
            end
        end
        assign q = chain[STAGES-1];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    # Data branch only (the reset arm is folded into the $adff's
    # ARST value, not separate flops). 1 bare site + (STAGES-1)
    # unrolled = 3 flops for STAGES=3.
    n = _flop_count(mod)
    assert n == 3, f"expected 3 flops (1 bare + STAGES-1=2 unrolled); got {n}"


# --- step variations ------------------------------------------------------


def test_for_loop_decrementing(tmp_path: Path) -> None:
    """``for (int i = N-1; i >= 0; i--)`` — same trip count, opposite
    iteration order. Equally common in shift-register / chain
    patterns."""
    src = """
    module m (
        input  logic clk,
        input  logic [3:0] d,
        output logic [3:0] q
    );
        always_ff @(posedge clk) begin
            for (int i = 3; i >= 0; i--)
                q[i] <= d[i];
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 4, (
        f"expected 4 flops from decrementing for-loop; got {_flop_count(mod)}"
    )


def test_for_loop_step_by_two(tmp_path: Path) -> None:
    """``i += 2`` step. Less common but valid SV; trip count = 3
    here (i = 0, 2, 4)."""
    src = """
    module m (
        input  logic clk,
        input  logic [5:0] d,
        output logic [5:0] q
    );
        always_ff @(posedge clk) begin
            for (int i = 0; i < 6; i += 2)
                q[i] <= d[i];
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    # Only even bits get a flop; odd bits stay unconnected.
    assert _flop_count(mod) == 3, f"expected 3 flops (i=0,2,4); got {_flop_count(mod)}"


# --- non-foldable bounds: must NOT crash ----------------------------------


def test_for_loop_with_runtime_bound_is_skipped_cleanly(tmp_path: Path) -> None:
    """Stop expression references a runtime signal — the loop trip
    count isn't compile-time computable. The walker must NOT crash;
    it should skip the loop body with no flop emitted (consistent
    with the existing fall-through-on-unknown policy in the walker
    tail)."""
    src = """
    module m (
        input  logic clk,
        input  logic [3:0] n,
        input  logic [15:0] d,
        output logic [15:0] q
    );
        always_ff @(posedge clk) begin
            for (int i = 0; i < n; i++)
                q[i] <= d[i];
        end
    endmodule
    """
    # No assertion on flop count — the contract is "doesn't crash".
    mod = _elaborate(tmp_path, src)
    # Just make sure elaboration completed and produced a Module.
    assert mod.name == "m"
