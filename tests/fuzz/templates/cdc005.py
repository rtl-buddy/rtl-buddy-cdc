"""CDC-005 — reconvergent synchronisers.

One source-domain flop fans out to two independent 2FF synchroniser
chains in the destination domain, then the chains' outputs are
recombined combinationally. Each chain individually filters
metastability, but variable resolution times can give different
synced values for one or two dst cycles — the AND/OR of the chains
can observe a transient state that never existed at the source.

CDC-005 must fire once (one finding per offending recombination).
CDC-001 / CDC-002 stay silent — each chain is structurally a valid
2FF synchroniser.
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

    logic sync_a_meta, sync_a_q;
    logic sync_b_meta, sync_b_q;
    always_ff @(posedge dst_clk or negedge rst_n)
        if (!rst_n) sync_a_meta <= 1'b0;
        else        sync_a_meta <= src_q;
    always_ff @(posedge dst_clk or negedge rst_n)
        if (!rst_n) sync_a_q <= 1'b0;
        else        sync_a_q <= sync_a_meta;
    always_ff @(posedge dst_clk or negedge rst_n)
        if (!rst_n) sync_b_meta <= 1'b0;
        else        sync_b_meta <= src_q;
    always_ff @(posedge dst_clk or negedge rst_n)
        if (!rst_n) sync_b_q <= 1'b0;
        else        sync_b_q <= sync_b_meta;

    assign q_out = sync_a_q & ~sync_b_q;
endmodule
"""

_SDC = """\
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports d_in]
"""


class ReconvergentSync:
    """One src flop, two parallel 2FF chains, recombined downstream."""

    name = "cdc005_reconvergent_sync"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        top = f"fuzz_{cls.name}"
        yield RenderedCase(
            template_name=cls.name,
            case_id=top,
            sv=_TEMPLATE.format(top=top),
            sdc=_SDC,
            top=top,
            params={},
            expected=(ExpectedFinding("CDC-005", Op.GE, 1),),
            forbidden=(
                ExpectedFinding("CDC-001", Op.ZERO),
                ExpectedFinding("CDC-002", Op.ZERO),
            ),
        )
