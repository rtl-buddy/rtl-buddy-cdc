"""CDC-016 — opposite-edge synchroniser.

A 2-stage sync chain on the destination clock whose stages disagree
on clock-pin polarity (stage 1 ``posedge dst_clk``, stage 2
``negedge dst_clk``). CDC-001/-002 see a structurally valid 2FF
chain and stay silent; CDC-016 fires on the adjacent-stage
polarity mismatch.

**Regression sentinel** (rtl-buddy-cdc#193): this template doubles
as a partitioning regression check for the CDC-001/-002 deferral
plumbing. The chain walker (``_sync_chain_depth``), the
inter-stage-comb deferral (``_chain_has_inter_stage_comb``), and
the polarity helper (``_clk_polarity``) are shared between CDC-001
/ -014 / -015 / -016. A refactor that breaks the partitioning
would cause CDC-001 to start firing on this chain and mislead the
user into "adding a 2FF chain you already have". The
``forbidden=(CDC-001, ZERO)`` clause below is the canary —
independently derived from the hand-authored fixture's assertion
in ``tests/test_bad_opposite_edge_sync.py``. Do not remove or
weaken either when pruning the fuzz corpus.
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
    output logic q_out
);
    logic src_q;
    always_ff @(posedge src_clk or negedge rst_n)
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;

    logic sync_meta;
    always_ff @(posedge dst_clk or negedge rst_n)
        if (!rst_n) sync_meta <= 1'b0;
        else        sync_meta <= src_q;

    logic sync_q;
    always_ff @(negedge dst_clk or negedge rst_n)
        if (!rst_n) sync_q <= 1'b0;
        else        sync_q <= sync_meta;

    assign q_out = sync_q;
endmodule
"""

_SDC_TEMPLATE = """\
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports d_in]
"""


class OppositeEdgeChain:
    """2-stage chain with posedge → negedge polarity flip."""

    name = "cdc016_opposite_edge"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        top = f"fuzz_{cls.name}"
        sv = _TEMPLATE.format(top=top)
        yield RenderedCase(
            template_name=cls.name,
            case_id=top,
            sv=sv,
            sdc=_SDC_TEMPLATE,
            top=top,
            params={},
            expected=(ExpectedFinding("CDC-016", Op.EQ, 1),),
            forbidden=(
                ExpectedFinding("CDC-001", Op.ZERO),
                ExpectedFinding("CDC-002", Op.ZERO),
            ),
        )
