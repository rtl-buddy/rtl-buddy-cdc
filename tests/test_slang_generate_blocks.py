"""Coverage tests for slang-frontend traversal of SV ``generate`` blocks.

Issue: rtl-buddy-cdc#53 — ``slang.py:_emit_for_member`` silently drops
``GenerateBlockSymbol`` and ``GenerateBlockArraySymbol``, so any
``always_ff`` nested inside ``generate`` is invisible to the analyzer.
On real designs (array-of-cores, parameterized fabrics) this produces a
near-empty ``Module`` and a confident false PASS.

The tests below pin the expected behaviour: every flop reachable from
the top — regardless of how many generate levels enclose it — must
appear in the produced ``Module.cells`` as a ``$dff`` / ``$adff`` cell.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc.frontend import Frontend, elaborate

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
if not PYSLANG_INSTALLED:
    pytest.skip(
        "pyslang not installed — slang generate-block tests are gated on it",
        allow_module_level=True,
    )

FLOP_TYPES = frozenset({"$dff", "$adff", "$dffe", "$adffe", "$sdff", "$sdffe"})


def _flop_count(module) -> int:
    return sum(1 for c in module.cells.values() if c.type in FLOP_TYPES)


def _elaborate(tmp_path: Path, src: str, top: str = "m"):
    sv = tmp_path / f"{top}.sv"
    sv.write_text(src)
    return elaborate([sv], top, frontend=Frontend.slang)


# --- 1-D generate-for ------------------------------------------------------


def test_generate_for_array_emits_all_flops(tmp_path: Path) -> None:
    """``for (genvar i = 0; i < N; i++)`` array of flops — the slang
    frontend must emit N $adff cells, one per index."""
    src = """
    module m #(parameter int N = 4) (
        input  logic clk,
        input  logic rst_n,
        input  logic [N-1:0] d,
        output logic [N-1:0] q
    );
        genvar i;
        generate
            for (i = 0; i < N; i++) begin : g_bit
                always_ff @(posedge clk or negedge rst_n) begin
                    if (!rst_n) q[i] <= 1'b0;
                    else        q[i] <= d[i];
                end
            end
        endgenerate
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 4, (
        f"expected 4 flops from generate-for[N=4]; got {_flop_count(mod)} "
        f"(cells: {sorted(c.type for c in mod.cells.values())})"
    )


def test_generate_for_uses_index_in_hier_prefix(tmp_path: Path) -> None:
    """Flops inside ``begin : g_bit`` should carry the labeled index
    in their cell name, matching yosys-flatten convention
    ``g_bit[0].…`` / ``g_bit[1].…``."""
    src = """
    module m (
        input  logic clk,
        input  logic rst_n,
        input  logic [1:0] d,
        output logic [1:0] q
    );
        genvar i;
        generate
            for (i = 0; i < 2; i++) begin : g_bit
                always_ff @(posedge clk or negedge rst_n) begin
                    if (!rst_n) q[i] <= 1'b0;
                    else        q[i] <= d[i];
                end
            end
        endgenerate
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    names = sorted(mod.cells.keys())
    has_idx0 = any("g_bit[0]" in n or "g_bit.0" in n for n in names)
    has_idx1 = any("g_bit[1]" in n or "g_bit.1" in n for n in names)
    assert has_idx0 and has_idx1, (
        f"expected indexed labels g_bit[0]/g_bit[1] in cell names; got {names}"
    )


# --- 2-D nested generate-for (mimics MXP slice array) ----------------------


def test_nested_generate_for_2d_emits_all_flops(tmp_path: Path) -> None:
    """Nested 2-D generate matches the tiny-NPU MXP slice array shape.
    For R=2, C=3 the body has 1 flop → expect 6 total."""
    src = """
    module m #(parameter int R = 2, parameter int C = 3) (
        input  logic clk,
        input  logic rst_n,
        input  logic [R*C-1:0] d,
        output logic [R*C-1:0] q
    );
        genvar r, c;
        generate
            for (r = 0; r < R; r++) begin : g_row
                for (c = 0; c < C; c++) begin : g_col
                    always_ff @(posedge clk or negedge rst_n) begin
                        if (!rst_n) q[r*C + c] <= 1'b0;
                        else        q[r*C + c] <= d[r*C + c];
                    end
                end
            end
        endgenerate
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    assert _flop_count(mod) == 6, (
        f"expected 6 flops from 2-D generate-for (R=2, C=3); got {_flop_count(mod)}"
    )


# --- conditional generate (generate-if) ------------------------------------


def test_generate_if_taken_branch_emits_flops(tmp_path: Path) -> None:
    """``generate if (cond) ... else ...`` — only the taken branch
    contributes flops. cond=true here, so the if-branch's two flops
    must appear and the else-branch's three must not."""
    src = """
    module m #(parameter bit USE_SYNC = 1) (
        input  logic clk, rst_n, d,
        output logic q
    );
        generate
            if (USE_SYNC) begin : g_sync
                logic meta_q, sync_q;
                always_ff @(posedge clk or negedge rst_n) begin
                    if (!rst_n) meta_q <= 1'b0;
                    else        meta_q <= d;
                end
                always_ff @(posedge clk or negedge rst_n) begin
                    if (!rst_n) sync_q <= 1'b0;
                    else        sync_q <= meta_q;
                end
                assign q = sync_q;
            end else begin : g_passthrough
                logic a, b, c;
                always_ff @(posedge clk or negedge rst_n) begin
                    if (!rst_n) a <= 1'b0;
                    else        a <= d;
                end
                always_ff @(posedge clk or negedge rst_n) begin
                    if (!rst_n) b <= 1'b0;
                    else        b <= d;
                end
                always_ff @(posedge clk or negedge rst_n) begin
                    if (!rst_n) c <= 1'b0;
                    else        c <= d;
                end
                assign q = a;
            end
        endgenerate
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    n = _flop_count(mod)
    # USE_SYNC=1: g_sync branch has meta_q + sync_q = 2 flops; g_passthrough
    # is unelaborated and contributes 0.
    assert n == 2, f"expected 2 flops from g_sync branch only; got {n}"


def test_generate_if_untaken_branch_emits_zero(tmp_path: Path) -> None:
    """Same shape with USE_SYNC=0 — the else-branch's three flops must
    appear and the if-branch must not contribute."""
    src = """
    module m #(parameter bit USE_SYNC = 0) (
        input  logic clk, rst_n, d,
        output logic q
    );
        generate
            if (USE_SYNC) begin : g_sync
                logic meta_q, sync_q;
                always_ff @(posedge clk or negedge rst_n) begin
                    if (!rst_n) meta_q <= 1'b0;
                    else        meta_q <= d;
                end
                always_ff @(posedge clk or negedge rst_n) begin
                    if (!rst_n) sync_q <= 1'b0;
                    else        sync_q <= meta_q;
                end
                assign q = sync_q;
            end else begin : g_passthrough
                logic a, b, c;
                always_ff @(posedge clk or negedge rst_n) begin
                    if (!rst_n) a <= 1'b0;
                    else        a <= d;
                end
                always_ff @(posedge clk or negedge rst_n) begin
                    if (!rst_n) b <= 1'b0;
                    else        b <= d;
                end
                always_ff @(posedge clk or negedge rst_n) begin
                    if (!rst_n) c <= 1'b0;
                    else        c <= d;
                end
                assign q = a;
            end
        endgenerate
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    n = _flop_count(mod)
    assert n == 3, f"expected 3 flops from g_passthrough branch only; got {n}"


# --- generate inside an instantiated child (depth-2 hierarchy) -------------


def test_child_module_generate_for_visible_from_top(tmp_path: Path) -> None:
    """The headline tiny-NPU shape: top instantiates a child, child
    body has a generate-for. The top elaboration must see the
    generate-for flops of the child through its instance."""
    src = """
    module child #(parameter int N = 4) (
        input  logic clk,
        input  logic rst_n,
        input  logic [N-1:0] d,
        output logic [N-1:0] q
    );
        genvar i;
        generate
            for (i = 0; i < N; i++) begin : g_bit
                always_ff @(posedge clk or negedge rst_n) begin
                    if (!rst_n) q[i] <= 1'b0;
                    else        q[i] <= d[i];
                end
            end
        endgenerate
    endmodule

    module m (
        input  logic clk, rst_n,
        input  logic [3:0] d,
        output logic [3:0] q
    );
        child #(.N(4)) u_child (.*);
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    n = _flop_count(mod)
    assert n == 4, (
        f"expected 4 flops from child's generate-for through u_child; got {n}"
    )
    assert any("u_child" in name for name in mod.cells.keys()), (
        f"expected u_child prefix on inlined cell names; got {sorted(mod.cells.keys())}"
    )
