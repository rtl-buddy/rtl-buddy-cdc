"""Coverage tests for slang-frontend 2-D unpacked-array port aliasing
through generates.

Issue: rtl-buddy-cdc#69 — ``_element_select_bits`` expects the
inner ``.value`` to be a bare ``NamedValueExpression`` and bails on
nested element selects (``arr[i][j]``). When such a select appears
in a port connection inside a generate instantiation, the child
instance's port-internal bits get fresh allocations instead of
aliasing to the parent's bit pool, severing the bit chain through
a tile array.

The tests below pin the contract: an ``ElementSelectExpression``
whose ``.value`` is itself an ``ElementSelectExpression`` resolves
to the correct slice of the underlying named variable, and port
connections using that shape propagate bit identity from parent to
child.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc.frontend import Frontend, elaborate

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
if not PYSLANG_INSTALLED:
    pytest.skip(
        "pyslang not installed — slang 2-D port-alias tests are gated on it",
        allow_module_level=True,
    )

FLOP_TYPES = frozenset({"$dff", "$adff", "$dffe", "$adffe", "$sdff", "$sdffe"})


def _elaborate(tmp_path: Path, src: str, top: str = "m"):
    sv = tmp_path / f"{top}.sv"
    sv.write_text(src)
    return elaborate([sv], top, frontend=Frontend.slang)


def _flops(module) -> list:
    return [c for c in module.cells.values() if c.type in FLOP_TYPES]


# --- nested element-select resolution -------------------------------------


def test_nested_element_select_returns_correct_slice(tmp_path: Path) -> None:
    """``arr[i][j]`` where arr is ``logic [W-1:0] arr [R][C]`` must
    resolve to the right ``W``-bit slice. The flop captures the
    selected element and its D bits should match the slice that
    ``arr[i][j]`` picks out of the underlying variable."""
    src = """
    module m (
        input  logic       clk,
        input  logic [3:0] in_data,
        input  logic [1:0] sel_r, sel_c,
        output logic [3:0] out_data
    );
        logic [3:0] arr [2][2];
        // Constant-folded select — element [1][0] of the 2x2 array.
        always_ff @(posedge clk) out_data <= arr[1][0];
        assign arr[0][0] = in_data;
        assign arr[0][1] = in_data;
        assign arr[1][0] = in_data;
        assign arr[1][1] = in_data;
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 1, f"expected one flop; got {len(flops)}"
    d_bits = flops[0].connections["D"]
    # The D bits should not be a fresh allocation — they should match
    # the underlying ``arr`` variable's bits for the [1][0] slice.
    arr_nn = mod.netnames.get("arr")
    assert arr_nn is not None, "arr netname not allocated"
    # arr layout: bits 0..3 are [0][0], 4..7 are [0][1], 8..11 are [1][0],
    # 12..15 are [1][1] (4-bit element, C=2 inner stride, 2 row stride).
    expected_slice = arr_nn.bits[8:12]
    assert tuple(d_bits) == tuple(expected_slice), (
        f"expected D to be the [1][0] slice {expected_slice}; "
        f"got {d_bits} (suggests nested select wasn't resolved)"
    )


# --- child-port aliasing through generate ---------------------------------


def test_child_port_aliases_2d_unpacked_through_generate(tmp_path: Path) -> None:
    """The headline tiny-NPU shape: a generate-for instantiates a
    chain of children; each child's input port is wired to a 2-D
    unpacked-array element parameterised by the genvar. The child's
    Q (a flop) at iteration ``c`` must alias to its D's source at
    iteration ``c-1``, so a ripple chain's bit identity propagates."""
    src = """
    module child (input logic clk, input logic d, output logic q);
        always_ff @(posedge clk) q <= d;
    endmodule

    module m (input logic clk, input logic d_in, output logic q_out);
        logic wire2d [1][3];
        assign wire2d[0][2] = d_in;
        for (genvar c = 0; c < 2; c++) begin : g
            child u_c (
                .clk(clk),
                .d(wire2d[0][c+1]),
                .q(wire2d[0][c])
            );
        end
        assign q_out = wire2d[0][0];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 2, f"expected 2 child flops; got {len(flops)}"
    # Order flops by instance label (g[0] then g[1])
    flops_by_inst = {}
    for f in flops:
        if "g[0]" in f.name:
            flops_by_inst[0] = f
        elif "g[1]" in f.name:
            flops_by_inst[1] = f
    assert 0 in flops_by_inst and 1 in flops_by_inst, (
        f"expected one flop in each generate iteration; got {list(flops_by_inst.keys())}"
    )
    # The chain: g[1].q drives wire2d[0][1] which drives g[0].d.
    # So g[0]'s D bit must equal g[1]'s Q bit.
    g1_q = flops_by_inst[1].connections["Q"][0]
    g0_d = flops_by_inst[0].connections["D"][0]
    assert g0_d == g1_q, (
        f"chain broken: g[0].D={g0_d} should alias to g[1].Q={g1_q} via "
        "wire2d[0][1]. Bit allocations don't share — the 2-D unpacked-array "
        "port-aliasing gap is still present."
    )


def test_child_port_aliases_packed_and_2d_unpacked(tmp_path: Path) -> None:
    """Same chain but the inner element is a packed W-bit bus
    (the production ``logic [CMD_W-1:0] mesh [R][C+1]`` shape from
    tiny-NPU's MXP). Tests that the W-bit element alias propagates,
    not just single-bit elements."""
    src = """
    module child #(parameter int W = 4) (
        input  logic         clk,
        input  logic [W-1:0] d,
        output logic [W-1:0] q
    );
        always_ff @(posedge clk) q <= d;
    endmodule

    module m #(parameter int W = 4) (
        input  logic         clk,
        input  logic [W-1:0] d_in,
        output logic [W-1:0] q_out
    );
        logic [W-1:0] mesh [1][3];
        assign mesh[0][2] = d_in;
        for (genvar c = 0; c < 2; c++) begin : g
            child #(.W(W)) u_c (
                .clk(clk),
                .d(mesh[0][c+1]),
                .q(mesh[0][c])
            );
        end
        assign q_out = mesh[0][0];
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flops = _flops(mod)
    assert len(flops) == 2, f"expected 2 child flops; got {len(flops)}"
    flops_by_inst = {0: None, 1: None}
    for f in flops:
        for k in flops_by_inst:
            if f"g[{k}]" in f.name:
                flops_by_inst[k] = f
    assert all(flops_by_inst.values()), (
        f"missing one of the per-iteration flops: {flops_by_inst}"
    )
    g1_q = flops_by_inst[1].connections["Q"]
    g0_d = flops_by_inst[0].connections["D"]
    assert tuple(g0_d) == tuple(g1_q), (
        f"W-bit chain broken: g[0].D={g0_d} should alias to g[1].Q={g1_q}"
    )
