"""CDC-003 — combinational logic between source flop and sync first stage.

Source flop's Q passes through an AND gate before the dst-domain
first-stage flop. The gate can glitch when its second input
transitions, and the glitch is sampled by the sync stage. CDC-003
fires; CDC-001 stays silent (the chain itself is structurally a
2FF — the comb is what's flagged).
"""

from __future__ import annotations

from collections.abc import Iterator
from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    input  logic mask_in,
    output logic q_out
);
    logic src_q;
    always_ff @(posedge src_clk or negedge rst_n)
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;

    logic mask_q;
    always_ff @(posedge dst_clk or negedge rst_n)
        if (!rst_n) mask_q <= 1'b0;
        else        mask_q <= mask_in;

    // Combinational gate between source flop and sync first stage.
    // This is the CDC-003 shape: src_q & mask_q is a comb output
    // that may glitch when mask_q transitions on dst_clk near a
    // src_q transition.
    logic comb_out;
    assign comb_out = src_q & mask_q;

    logic sync_meta;
    always_ff @(posedge dst_clk or negedge rst_n)
        if (!rst_n) sync_meta <= 1'b0;
        else        sync_meta <= comb_out;

    logic sync_q;
    always_ff @(posedge dst_clk or negedge rst_n)
        if (!rst_n) sync_q <= 1'b0;
        else        sync_q <= sync_meta;

    assign q_out = sync_q;
endmodule
"""

_SDC_TEMPLATE = """\
create_clock -name src_clk -period {src_period} [get_ports src_clk]
create_clock -name dst_clk -period {dst_period} [get_ports dst_clk]
set_clock_groups -asynchronous -group {{src_clk}} -group {{dst_clk}}
set_input_delay -clock src_clk 1.0 [get_ports d_in]
set_input_delay -clock dst_clk 1.0 [get_ports mask_in]
"""


class CombBeforeSync:
    """AND gate between src flop and sync first stage."""

    name = "cdc003_comb_before_sync"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        period_pairs = [(10.0, 7.5), (5.0, 11.3)]
        for src_period, dst_period in period_pairs:
            params = {"src_period": src_period, "dst_period": dst_period}
            top = f"fuzz_{cls.name}_{int(src_period * 10)}_{int(dst_period * 10)}"
            sv = _TEMPLATE.format(top=top)
            sdc = _SDC_TEMPLATE.format(src_period=src_period, dst_period=dst_period)
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=sv,
                sdc=sdc,
                top=top,
                params=params,
                expected=(ExpectedFinding("CDC-003", Op.GE, 1),),
                # CDC-001 / -002 stay silent: the chain itself is
                # depth 2. The comb-before-sync shape is CDC-003's
                # alone.
                forbidden=(
                    ExpectedFinding("CDC-001", Op.ZERO),
                    ExpectedFinding("CDC-002", Op.ZERO),
                ),
            )
