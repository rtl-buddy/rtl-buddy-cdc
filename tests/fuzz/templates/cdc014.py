"""CDC-014 — combinational logic between sync stages.

A 2FF chain on dst_clk with an inverter sitting between the stages.
CDC-001 would say "no second-stage synchronizer" (chain walker
stops at the gate); CDC-014 fires with the correct framing via the
``_chain_has_inter_stage_comb`` deferral.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic d_in,
    output logic q_out
);
    logic src_q;
    always_ff @(posedge src_clk) src_q <= d_in;

    logic sync_meta;
    always_ff @(posedge dst_clk) sync_meta <= src_q;

    // Comb gate (inverter) between stages 1 and 2.
    wire sync_meta_n = ~sync_meta;

    logic sync_q;
    always_ff @(posedge dst_clk) sync_q <= sync_meta_n;

    assign q_out = sync_q;
endmodule
"""

_SDC = """\
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports d_in]
"""


class CombBetweenStages:
    name = "cdc014_comb_between_stages"

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
            expected=(ExpectedFinding("CDC-014", Op.GE, 1),),
            forbidden=(ExpectedFinding("CDC-001", Op.ZERO),),
        )
