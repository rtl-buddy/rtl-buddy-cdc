"""Regression sentinel: CDC-018 cascaded synchroniser (rtl-buddy-cdc#212).

A dst-domain sync chain whose depth exceeds the textbook minimum
(2FF). Classic pattern: two engineers each add their own sync
chain, or a refactor leaves the original chain in place while
adding a wrapper, producing 4+ back-to-back same-domain flops
where 2 would do. CDC-001 / CDC-002 stay silent because the chain
is structurally well-formed; CDC-018 surfaces the code-review
smell.

Stage-3 Layer A (rtl-buddy-cdc#221): the SDC clock periods sweep
across :data:`TWO_CLOCK_PERIODS` so CDC-018 crosses the ≥10× bar
without changing the structural shape — purely an SDC-only
multiplier per the issue's Layer-A guidance.

Asserts CDC-018 fires once per case on the 4-deep chain. Before the
rule existed, ``run_all`` produced zero findings on this shape.
"""

from __future__ import annotations

from collections.abc import Iterator

from ._sweep import TWO_CLOCK_PERIODS, case_suffix
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

    // 4-deep sync chain in dst_clk — first 2 are a proper sync,
    // last 2 are cascaded (no functional purpose).
    logic meta, sync_q, sync2_meta, sync2_q;
    always_ff @(posedge dst_clk) begin
        meta       <= src_q;
        sync_q     <= meta;
        sync2_meta <= sync_q;
        sync2_q    <= sync2_meta;
    end

    assign q_out = sync2_q;
endmodule
"""

_SDC_TEMPLATE = """\
create_clock -name src_clk -period {src_period} [get_ports src_clk]
create_clock -name dst_clk -period {dst_period} [get_ports dst_clk]
set_clock_groups -asynchronous -group {{src_clk}} -group {{dst_clk}}
set_input_delay -clock src_clk 1.0 [get_ports d_in]
"""


class GapG2CascadedSynchroniser:
    """Regression sentinel: 4-deep cascaded sync chain fires CDC-018."""

    name = "gap_g2_cascaded_synchroniser"

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
                expected=(ExpectedFinding("CDC-018", Op.EQ, 1),),
                # CDC-001/002 silent because the chain is structurally
                # well-formed at depth >= required_depth. Pin both.
                forbidden=(
                    ExpectedFinding("CDC-001", Op.ZERO),
                    ExpectedFinding("CDC-002", Op.ZERO),
                ),
            )
