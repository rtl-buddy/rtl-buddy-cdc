"""Regression sentinel: CDC-019 independently-synced one-hot decode (rtl-buddy-cdc#204).

A common combinational decoder (here a 2-to-4 one-hot) generates N
parallel 1-bit signals. Each is registered in the source domain as
its own WIDTH=1 flop, then independently 2FF-synchronised in the
destination domain. CDC-004 doesn't fire because each lane is
structurally 1-bit — but the lanes are *related* in the comb logic
upstream, and the dst can transiently observe incoherent
combinations between the independently-resolved samples (e.g.
``4'b0010`` mid-transition between ``4'b0001`` and ``4'b0100``).

Stage-3 Layer A (rtl-buddy-cdc#221): the SDC clock periods sweep
across :data:`TWO_CLOCK_PERIODS` so CDC-019 crosses the ≥10× bar
without changing the structural shape — purely an SDC-only
multiplier per the issue's Layer-A guidance.

Asserts CDC-019 fires once per case on the shared decoder. Before
the rule existed, ``run_all`` produced zero findings on this shape.
"""

from __future__ import annotations

from collections.abc import Iterator

from ._sweep import TWO_CLOCK_PERIODS, case_suffix
from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic [1:0] sel,
    output logic [3:0] sync_out
);
    // One-hot decoder — shared comb cell whose output bits all
    // change together on a sel transition.
    logic [3:0] one_hot;
    always_comb begin
        one_hot = '0;
        one_hot[sel] = 1'b1;
    end

    // Four separate WIDTH=1 src-domain flops. From the analyzer's
    // structural view each is an independent 1-bit register; from
    // the failure-mode view they are four lanes of one decode.
    logic d0, d1, d2, d3;
    always_ff @(posedge src_clk) begin
        d0 <= one_hot[0];
        d1 <= one_hot[1];
        d2 <= one_hot[2];
        d3 <= one_hot[3];
    end

    // Four independent 2FF chains — each resolves metastability on
    // its own schedule. dst can sample incoherent combinations
    // (e.g. {{1,1,0,0}} during a 0001 → 0100 transition).
    logic d0_m, d0_s, d1_m, d1_s, d2_m, d2_s, d3_m, d3_s;
    always_ff @(posedge dst_clk) begin
        d0_m <= d0; d0_s <= d0_m;
        d1_m <= d1; d1_s <= d1_m;
        d2_m <= d2; d2_s <= d2_m;
        d3_m <= d3; d3_s <= d3_m;
    end

    assign sync_out = {{d3_s, d2_s, d1_s, d0_s}};
endmodule
"""

_SDC_TEMPLATE = """\
create_clock -name src_clk -period {src_period} [get_ports src_clk]
create_clock -name dst_clk -period {dst_period} [get_ports dst_clk]
set_clock_groups -asynchronous -group {{src_clk}} -group {{dst_clk}}
set_input_delay -clock src_clk 1.0 [get_ports sel]
"""


class GapG4OneHotDecodeIndependentSync:
    """Regression sentinel: shared comb decoder with independent per-bit sync fires CDC-019."""

    name = "gap_g4_onehot_decode_independent_sync"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        for src_period, dst_period in TWO_CLOCK_PERIODS:
            top = f"fuzz_{cls.name}_{case_suffix(src_period, dst_period)}"
            sv = _TEMPLATE.format(top=top)
            sdc = _SDC_TEMPLATE.format(src_period=src_period, dst_period=dst_period)
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=sv,
                sdc=sdc,
                top=top,
                params={"src_period": src_period, "dst_period": dst_period},
                expected=(ExpectedFinding("CDC-019", Op.EQ, 1),),
                # CDC-004 misses this — each src flop is structurally
                # 1-bit, so the multi-bit-bus detector doesn't see the
                # related lanes. Pin that explicitly so a future widening
                # of CDC-004 to walk back through shared comb cells
                # doesn't silently double-count.
                forbidden=(ExpectedFinding("CDC-004", Op.ZERO),),
            )
